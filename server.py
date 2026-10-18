"""
server.py — FastAPI + JWT auth + per-user WebSocket isolation
Har bir user faqat o'z akkauntlarini ko'radi.
Ishga tushirish: uvicorn server:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from bot import TronPickBot, CONFIG_FILE

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
security = HTTPBearer()

DATA_FILE = "users.json"
TOKENS: dict[str, str] = {}          # token → username
_data_lock = threading.Lock()


# ─── Persistence ─────────────────────────────────────────────────────────────

def _load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"users": {}}          # { username: { password_hash, accounts: [...] } }


def _save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ─── Token helpers ────────────────────────────────────────────────────────────

def _create_token(username: str) -> str:
    token = secrets.token_hex(32)
    TOKENS[token] = username
    return token


def _get_username(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    username = TOKENS.get(credentials.credentials)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token")
    return username


# ─── Bot state (per-user, per-account) ───────────────────────────────────────

# user_bots[username][email] = TronPickBot instance
user_bots:    dict[str, dict[str, TronPickBot]]     = {}
# user_state[username] = list of account dicts
user_state:   dict[str, list[dict[str, Any]]]        = {}
# ws_pool[username] = set of WebSocket connections
ws_pool:      dict[str, set[WebSocket]]              = {}
state_locks:  dict[str, threading.Lock]              = {}


def _ensure_user_structures(username: str) -> None:
    if username not in user_bots:
        user_bots[username]   = {}
        user_state[username]  = []
        ws_pool[username]     = set()
        state_locks[username] = threading.Lock()


def _get_account_index(username: str, email: str) -> int:
    with state_locks[username]:
        for i, acc in enumerate(user_state[username]):
            if acc["email"] == email:
                return i
    return -1


def _update_account(username: str, email: str, **fields) -> None:
    idx = _get_account_index(username, email)
    if idx == -1:
        return
    with state_locks[username]:
        user_state[username][idx].update(fields)
    asyncio.run(_push_state(username))


async def _push_state(username: str) -> None:
    if username not in user_state:
        return
    with state_locks[username]:
        snapshot = list(user_state[username])
    dead: set[WebSocket] = set()
    for ws in ws_pool.get(username, set()):
        try:
            await ws.send_text(json.dumps({"type": "state", "accounts": snapshot}))
        except Exception:
            dead.add(ws)
    ws_pool[username].difference_update(dead)


# ─── Bot worker ───────────────────────────────────────────────────────────────

def run_bot(username: str, email: str, password: str, api_key: str, display_name: str) -> None:
    def on_status(s: str):
        _update_account(username, email, status=s)

    bot = TronPickBot(email=email, password=password, api_key=api_key, status_callback=on_status)
    user_bots[username][email] = bot

    _update_account(username, email, status="INITIALIZING")
    time.sleep(1)

    ok, _ = bot.login()
    if not ok:
        _update_account(username, email, status="ERROR")
        return

    bot.update_info()
    _update_account(
        username, email,
        status="LOGGED IN",
        balance=bot.balance,
        level=bot.level,
        timeLeft=max(0, bot.target_timestamp - int(time.time())),
    )

    while True:
        now = int(time.time())
        time_left = bot.target_timestamp - now
        _update_account(username, email, timeLeft=max(0, time_left), balance=bot.balance)

        if time_left <= 0:
            bot.update_info()
            if int(time.time()) >= bot.target_timestamp:
                bot.claim()
                time.sleep(2)
                bot.update_info()
            _update_account(
                username, email,
                balance=bot.balance,
                timeLeft=max(0, bot.target_timestamp - int(time.time())),
            )
        time.sleep(1)


# ─── Auth endpoints ───────────────────────────────────────────────────────────

@app.post("/api/register")
async def register(payload: dict):
    username = payload.get("username", "").strip().lower()
    password = payload.get("password", "").strip()
    if not username or not password:
        return {"ok": False, "error": "Fill all fields"}
    if len(username) < 3:
        return {"ok": False, "error": "Username min 3 chars"}
    if len(password) < 4:
        return {"ok": False, "error": "Password min 4 chars"}

    with _data_lock:
        data = _load_data()
        if username in data["users"]:
            return {"ok": False, "error": "Username taken"}
        data["users"][username] = {"password_hash": _hash(password), "accounts": []}
        _save_data(data)

    token = _create_token(username)
    _ensure_user_structures(username)
    return {"ok": True, "token": token, "username": username}


@app.post("/api/login")
async def login(payload: dict):
    username = payload.get("username", "").strip().lower()
    password = payload.get("password", "").strip()

    with _data_lock:
        data = _load_data()
    user = data["users"].get(username)
    if not user or user["password_hash"] != _hash(password):
        return {"ok": False, "error": "Wrong credentials"}

    token = _create_token(username)
    _ensure_user_structures(username)

    # Boot saved bots for this user
    _boot_user_bots(username, user.get("accounts", []))

    return {"ok": True, "token": token, "username": username}


@app.post("/api/change-password")
async def change_password(payload: dict, username: str = Depends(_get_username)):
    new_pass = payload.get("password", "").strip()
    if len(new_pass) < 4:
        return {"ok": False, "error": "Password min 4 chars"}
    with _data_lock:
        data = _load_data()
        if username not in data["users"]:
            return {"ok": False, "error": "User not found"}
        data["users"][username]["password_hash"] = _hash(new_pass)
        _save_data(data)
    return {"ok": True}


# ─── Account endpoints ────────────────────────────────────────────────────────

@app.post("/api/accounts")
async def add_account(payload: dict, username: str = Depends(_get_username)):
    email    = payload.get("email", "").strip()
    password = payload.get("password", "").strip()
    api_key  = payload.get("api_key", "").strip()
    display  = payload.get("display_name") or email.split("@")[0].upper()

    if not email or not password or not api_key:
        return {"ok": False, "error": "Missing fields"}

    _ensure_user_structures(username)
    if _get_account_index(username, email) != -1:
        return {"ok": False, "error": "Account already added"}

    entry = {
        "email":        email,
        "display_name": display,
        "balance":      "0.000000",
        "level":        "Stone",
        "timeLeft":     0,
        "status":       "PENDING",
    }
    with state_locks[username]:
        user_state[username].append(entry)

    # Persist
    with _data_lock:
        data = _load_data()
        accounts = data["users"][username].get("accounts", [])
        if not any(a["email"] == email for a in accounts):
            accounts.append({"email": email, "password": password, "api_key": api_key, "display_name": display})
            data["users"][username]["accounts"] = accounts
            _save_data(data)

    t = threading.Thread(target=run_bot, args=(username, email, password, api_key, display), daemon=True)
    t.start()

    asyncio.create_task(_push_state(username))
    return {"ok": True}


@app.get("/api/accounts")
async def list_accounts(username: str = Depends(_get_username)):
    _ensure_user_structures(username)
    with state_locks[username]:
        return {"accounts": list(user_state[username])}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = ""):
    username = TOKENS.get(token)
    if not username:
        await ws.close(code=4001)
        return

    await ws.accept()
    _ensure_user_structures(username)
    ws_pool[username].add(ws)

    # Send current state immediately
    with state_locks[username]:
        snapshot = list(user_state[username])
    await ws.send_text(json.dumps({"type": "state", "accounts": snapshot}))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_pool[username].discard(ws)


# ─── Static ───────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_dashboard():
    return FileResponse(os.path.join(os.path.dirname(__file__), "dashboard.html"))


# ─── Startup ──────────────────────────────────────────────────────────────────

def _boot_user_bots(username: str, accounts: list) -> None:
    """Start bot threads for all saved accounts of a user."""
    _ensure_user_structures(username)
    for acc in accounts:
        email    = acc.get("email", "")
        password = acc.get("password", "")
        api_key  = acc.get("api_key", "")
        display  = acc.get("display_name") or email.split("@")[0].upper()
        if not email or not password or not api_key:
            continue
        if _get_account_index(username, email) != -1:
            continue
        entry = {
            "email":        email,
            "display_name": display,
            "balance":      "0.000000",
            "level":        "Stone",
            "timeLeft":     0,
            "status":       "PENDING",
        }
        with state_locks[username]:
            user_state[username].append(entry)
        t = threading.Thread(target=run_bot, args=(username, email, password, api_key, display), daemon=True)
        t.start()


@app.on_event("startup")
async def on_startup():
    """On server start, do NOT auto-login users — they must login via browser to get a token."""
    pass
