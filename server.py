"""
server.py — FastAPI + WebSocket bridge
Bot holati va balance'ni HTML dashboardga real-time uzatadi.
Ishga tushirish: uvicorn server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import os
import threading
import time
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from bot import TronPickBot, CONFIG_FILE

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Global state ────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.accounts: list[dict[str, Any]] = []   # {name, balance, timeLeft, status}
        self.bots:     dict[str, TronPickBot] = {}  # email → bot instance
        self.lock = threading.Lock()

state = BotState()
connected_clients: set[WebSocket] = set()


# ─── WebSocket broadcast ─────────────────────────────────────────────────────

async def broadcast(payload: dict) -> None:
    dead: set[WebSocket] = set()
    for ws in connected_clients:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


def push_state() -> None:
    """Called from bot threads — schedules a broadcast on the event loop."""
    with state.lock:
        snapshot = list(state.accounts)
    asyncio.run(broadcast({"type": "state", "accounts": snapshot}))


# ─── Bot worker (one thread per account) ─────────────────────────────────────

def _account_index(email: str) -> int:
    with state.lock:
        for i, acc in enumerate(state.accounts):
            if acc["email"] == email:
                return i
    return -1


def _update_account(email: str, **fields) -> None:
    idx = _account_index(email)
    if idx == -1:
        return
    with state.lock:
        state.accounts[idx].update(fields)
    push_state()


def run_bot(email: str, password: str, api_key: str, username: str) -> None:
    def on_status(s: str):
        _update_account(email, status=s)

    bot = TronPickBot(email=email, password=password, api_key=api_key, status_callback=on_status)

    with state.lock:
        state.bots[email] = bot

    _update_account(email, status="INITIALIZING")
    time.sleep(1)

    ok, _ = bot.login()
    if not ok:
        _update_account(email, status="ERROR")
        return

    bot.update_info()
    _update_account(
        email,
        status="LOGGED IN",
        balance=bot.balance,
        level=bot.level,
        timeLeft=max(0, bot.target_timestamp - int(time.time())),
    )

    while True:
        now = int(time.time())
        time_left = bot.target_timestamp - now

        _update_account(email, timeLeft=max(0, time_left), balance=bot.balance)

        if time_left <= 0:
            bot.update_info()
            if int(time.time()) >= bot.target_timestamp:
                bot.claim()
                time.sleep(2)
                bot.update_info()
            _update_account(
                email,
                balance=bot.balance,
                timeLeft=max(0, bot.target_timestamp - int(time.time())),
            )

        time.sleep(1)


# ─── REST endpoints ───────────────────────────────────────────────────────────

@app.get("/")
async def serve_dashboard():
    return FileResponse(os.path.join(os.path.dirname(__file__), "dashboard.html"))


@app.post("/api/accounts")
async def add_account(payload: dict):
    """
    Body: { "email": "...", "password": "...", "api_key": "...", "username": "..." }
    Adds the account to state and spins up a bot thread.
    """
    email    = payload.get("email", "").strip()
    password = payload.get("password", "").strip()
    api_key  = payload.get("api_key", "").strip()
    username = payload.get("username") or email.split("@")[0].upper()

    if not email or not password or not api_key:
        return {"ok": False, "error": "Missing fields"}

    if _account_index(email) != -1:
        return {"ok": False, "error": "Already running"}

    entry = {
        "email":    email,
        "username": username,
        "balance":  "0.000000",
        "level":    "Stone",
        "timeLeft": 0,
        "status":   "PENDING",
    }
    with state.lock:
        state.accounts.append(entry)

    # Save to alpha.json (append-mode list)
    _persist_account({"email": email, "password": password, "api_key": api_key, "username": username})

    t = threading.Thread(target=run_bot, args=(email, password, api_key, username), daemon=True)
    t.start()

    push_state()
    return {"ok": True}


@app.get("/api/accounts")
async def list_accounts():
    with state.lock:
        return {"accounts": list(state.accounts)}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)

    # Send current state immediately on connect
    with state.lock:
        snapshot = list(state.accounts)
    await ws.send_text(json.dumps({"type": "state", "accounts": snapshot}))

    try:
        while True:
            await ws.receive_text()   # keep-alive; client sends ping
    except WebSocketDisconnect:
        connected_clients.discard(ws)


# ─── Persistence helpers ──────────────────────────────────────────────────────

def _persist_account(entry: dict) -> None:
    """Append account to alpha.json list (creates file if missing)."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = [data]           # migrate legacy single-account format
        else:
            data = []

        emails = {a["email"] for a in data if isinstance(a, dict)}
        if entry["email"] not in emails:
            data.append(entry)
            with open(CONFIG_FILE, "w") as f:
                json.dump(data, f, indent=4)
    except Exception:
        pass


def _load_saved_accounts() -> None:
    """Auto-start bots for accounts saved in alpha.json on server startup."""
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        for acc in data:
            email    = acc.get("email", "")
            password = acc.get("password", "")
            api_key  = acc.get("api_key", "")
            username = acc.get("username") or email.split("@")[0].upper()
            if email and password and api_key:
                entry = {
                    "email":    email,
                    "username": username,
                    "balance":  "0.000000",
                    "level":    "Stone",
                    "timeLeft": 0,
                    "status":   "PENDING",
                }
                with state.lock:
                    state.accounts.append(entry)
                t = threading.Thread(
                    target=run_bot, args=(email, password, api_key, username), daemon=True
                )
                t.start()
    except Exception:
        pass


@app.on_event("startup")
async def on_startup():
    _load_saved_accounts()
