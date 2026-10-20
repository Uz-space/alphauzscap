"""
main.py — TronPick Bot + FastAPI server
"""

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import requests
import secrets
import string
import threading
import time
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials


# ═══════════════════════════════════════════════════════════════════════════════
# BOT ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_fp(length: int = 16) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


class TronPickBot:
    def __init__(self, email: str, password: str, api_key: str, status_callback=None):
        self.session          = requests.Session()
        self.email            = email
        self.password         = password
        self.api_key          = api_key
        self.domain           = "tronpick.io"
        self.balance          = 0.000000
        self.level            = "Stone"
        self.target_timestamp = 0
        self.fp               = _generate_fp()
        self.ua               = (
            "Mozilla/5.0 (Linux; Android 12; V2029 Build/SP1A.210812.003) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.6167.164 Mobile Safari/537.36"
        )
        self.headers = {
            'User-Agent':      self.ua,
            'Accept':          'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin':          f'https://{self.domain}',
            'Referer':         f'https://{self.domain}/login.php',
            'Sec-Fetch-Dest':  'empty',
            'Sec-Fetch-Mode':  'cors',
            'Sec-Fetch-Site':  'same-origin',
        }
        self.status_callback = status_callback

    def set_status(self, state: str) -> None:
        if self.status_callback:
            self.status_callback(state)

    def solve_captcha(self) -> str | None:
        try:
            self.set_status("PROCESSING")
            res = requests.post(
                "https://api.sctg.xyz/in.php",
                data={
                    'key':     self.api_key,
                    'method':  'turnstile',
                    'sitekey': '0x4AAAAAAAW74HiAaujGhyeV',
                    'pageurl': f'https://{self.domain}/faucet.php',
                    'json':    1,
                },
                timeout=30,
            ).json()
            if res.get('status') != 1:
                return None

            rid = res.get('request')
            for _ in range(40):
                time.sleep(3)
                g = requests.get(
                    f"https://api.sctg.xyz/res.php"
                    f"?key={self.api_key}&action=get&id={rid}&json=1",
                    timeout=30,
                ).json()
                if g.get('status') == 1:
                    return g.get('request')
                if g.get('request') == 'ERROR_CAPTCHA_UNSOLVABLE':
                    break
            return None
        except Exception:
            self.set_status("ERROR")
            return None

    def login(self) -> tuple[bool, str]:
        try:
            self.set_status("CONNECTING")
            self.session.cookies.set('fp', self.fp, domain=self.domain)
            try:
                self.session.get(
                    f"https://{self.domain}/login.php",
                    headers={'User-Agent': self.ua}, timeout=20,
                )
            except Exception:
                self.session.get(
                    f"https://{self.domain}/login.php",
                    headers={'User-Agent': self.ua}, timeout=30,
                )

            csrf = self.session.cookies.get('csrf_cookie_name')
            if not csrf:
                self.set_status("ERROR")
                return False, "CSRF Missing"

            token = self.solve_captcha()
            if not token:
                self.set_status("ERROR")
                return False, "Captcha Failed"

            res = self.session.post(
                f"https://{self.domain}/process.php",
                data={
                    'action':            'login',
                    'email':             self.email,
                    'password':          self.password,
                    'captcha_type':      '3',
                    'c_captcha_response': token,
                    'csrf_test_name':    csrf,
                },
                headers=self.headers,
                timeout=30,
            ).json()

            if res.get('ret') == 1:
                self.set_status("LOGGED IN")
                return True, "Success"

            self.set_status("ERROR")
            return False, res.get('mes', 'Unknown error')
        except Exception:
            self.set_status("ERROR")
            return False, "Network Error"

    def update_info(self) -> bool:
        try:
            res = self.session.get(
                f"https://{self.domain}/faucet.php",
                headers={'User-Agent': self.ua}, timeout=20,
            )
            bal = (
                re.search(r'user_balance">([\d.]+)', res.text) or
                re.search(r'id="user_balance">([\d.]+)', res.text)
            )
            if bal:
                self.balance = float(bal.group(1))

            lvl = re.search(r'Your level is\s*<b>(.*?)</b>', res.text)
            if lvl:
                self.level = lvl.group(1)

            tmr = (
                re.search(r'show_countdown_clock\((\d+)\)', res.text) or
                re.search(r'var\s+autocontrol\s*=\s*(\d+);', res.text) or
                re.search(r'countDownDate\s*=\s*(\d+);', res.text)
            )
            self.target_timestamp = int(time.time()) + int(tmr.group(1)) if tmr else int(time.time()) + 3600
            return True
        except Exception:
            return False

    def claim(self) -> None:
        try:
            self.set_status("PROCESSING")
            token = self.solve_captcha()
            if not token:
                self.set_status("ERROR")
                return

            csrf     = self.session.cookies.get('csrf_cookie_name')
            ts       = int(time.time())
            data_str = f"{random.randint(100, 200)}:{random.randint(10, 50)}:{ts}"
            xor_key  = "0542f6c18bc7906d742a8401d0b5ef7f50ee304bff4f032348a4ceb3fd2d6bb1"
            hashed   = base64.b64encode(
                "".join(
                    chr(ord(c) ^ ord(xor_key[i % len(xor_key)]))
                    for i, c in enumerate(data_str)
                ).encode()
            ).decode()

            res = self.session.post(
                f"https://{self.domain}/process.php",
                data={
                    'action':            'claim_hourly_faucet',
                    'hash':              hashed,
                    'captcha_type':      '3',
                    'c_captcha_response': token,
                    'csrf_test_name':    csrf,
                },
                headers=self.headers,
                timeout=30,
            ).json()

            self.set_status("SUCCESS" if res.get('ret') == 1 else "ERROR")
            self.update_info()
        except Exception:
            self.set_status("ERROR")


# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI SERVER
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
security = HTTPBearer()

DATA_FILE  = "users.json"
TOKENS:     dict[str, str]                          = {}
_data_lock = threading.Lock()

user_bots:   dict[str, dict[str, TronPickBot]]      = {}
user_state:  dict[str, list[dict[str, Any]]]         = {}
ws_pool:     dict[str, set[WebSocket]]               = {}
state_locks: dict[str, threading.Lock]               = {}

# Global event loop reference for safe cross-thread async communication
_loop: asyncio.AbstractEventLoop = None

@app.on_event("startup")
async def on_startup():
    global _loop
    _loop = asyncio.get_running_loop()


def _load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"users": {}}


def _save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _create_token(username: str) -> str:
    token = secrets.token_hex(32)
    TOKENS[token] = username
    return token


def _get_username(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    username = TOKENS.get(credentials.credentials)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token")
    return username


def _ensure(username: str) -> None:
    if username not in user_bots:
        user_bots[username]   = {}
        user_state[username]  = []
        ws_pool[username]     = set()
        state_locks[username] = threading.Lock()


def _acc_index(username: str, name: str) -> int:
    with state_locks[username]:
        for i, a in enumerate(user_state[username]):
            if a["name"] == name:
                return i
    return -1


async def _push(username: str) -> None:
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


def _update_acc(username: str, name: str, **fields) -> None:
    idx = _acc_index(username, name)
    if idx == -1:
        return
    with state_locks[username]:
        user_state[username][idx].update(fields)
    
    if _loop and _loop.is_running():
        asyncio.run_coroutine_threadsafe(_push(username), _loop)


def _run_bot(username: str, email: str, password: str, api_key: str) -> None:
    bot = TronPickBot(
        email=email, password=password, api_key=api_key,
        status_callback=lambda s: _update_acc(username, email, status=s),
    )
    user_bots[username][email] = bot

    _update_acc(username, email, status="INITIALIZING")
    time.sleep(1)

    ok, _ = bot.login()
    if not ok:
        _update_acc(username, email, status="ERROR")
        return

    bot.update_info()
    time_left = max(0, bot.target_timestamp - int(time.time()))
    _update_acc(username, email, status="LOGGED IN", balance=bot.balance, timeLeft=time_left)

    while True:
        current_time = int(time.time())
        time_left = max(0, bot.target_timestamp - current_time)
        _update_acc(username, email, timeLeft=time_left, balance=bot.balance)
        
        if time_left <= 0:
            bot.update_info()
            if int(time.time()) >= bot.target_timestamp:
                bot.claim()
                time.sleep(2)
                bot.update_info()
            time_left = max(0, bot.target_timestamp - int(time.time()))
            _update_acc(username, email, balance=bot.balance, timeLeft=time_left)
        
        time.sleep(1)


def _boot_bots(username: str, accounts: list) -> None:
    _ensure(username)
    for acc in accounts:
        email, password, api_key = acc.get("name",""), acc.get("password",""), acc.get("api_key","")
        if not email or not password or not api_key:
            continue
        if _acc_index(username, email) != -1:
            continue
        with state_locks[username]:
            user_state[username].append({
                "name": email,
                "password": password,
                "balance": 0.000000,
                "timeLeft": 3600,
                "status": "PENDING"
            })
        threading.Thread(target=_run_bot, args=(username, email, password, api_key), daemon=True).start()


@app.post("/api/register")
async def register(payload: dict):
    username = payload.get("username", "").strip().lower()
    password = payload.get("password", "").strip()
    if not username or not password:
        return {"ok": False, "error": "Maydonlarni to'ldiring"}
    with _data_lock:
        data = _load_data()
        if username in data["users"]:
            return {"ok": False, "error": "Bu username band"}
        data["users"][username] = {"password_hash": _hash(password), "accounts": []}
        _save_data(data)
    _ensure(username)
    return {"ok": True, "token": _create_token(username), "username": username}


@app.post("/api/login")
async def login(payload: dict):
    username = payload.get("username", "").strip().lower()
    password = payload.get("password", "").strip()
    with _data_lock:
        data = _load_data()
    user = data["users"].get(username)
    if not user or user["password_hash"] != _hash(password):
        return {"ok": False, "error": "Login yoki parol noto'g'ri"}
    _ensure(username)
    _boot_bots(username, user.get("accounts", []))
    return {"ok": True, "token": _create_token(username), "username": username}


@app.post("/api/accounts")
async def add_account(payload: dict, username: str = Depends(_get_username)):
    email    = payload.get("name", "").strip()
    password = payload.get("password", "").strip()
    api_key  = payload.get("api_key", "").strip()
    if not email or not password or not api_key:
        return {"ok": False, "error": "Ma'lumotlar to'liq emas"}
    _ensure(username)
    if _acc_index(username, email) != -1:
        return {"ok": False, "error": "Bu akkaunt allaqachon qo'shilgan"}
    
    with state_locks[username]:
        user_state[username].append({
            "name": email, "password": password,
            "balance": 0.000000, "timeLeft": 3600, "status": "PENDING"
        })
    with _data_lock:
        data = _load_data()
        accs = data["users"][username].get("accounts", [])
        if not any(a["name"] == email for a in accs):
            accs.append({"name": email, "password": password, "api_key": api_key})
            data["users"][username]["accounts"] = accs
            _save_data(data)
            
    threading.Thread(target=_run_bot, args=(username, email, password, api_key), daemon=True).start()
    asyncio.create_task(_push(username))
    return {"ok": True}


@app.get("/api/accounts")
async def list_accounts(username: str = Depends(_get_username)):
    _ensure(username)
    with state_locks[username]:
        return {"accounts": list(user_state[username])}


@app.post("/api/accounts/update")
async def update_account(payload: dict, username: str = Depends(_get_username)):
    email    = payload.get("name", "").strip()
    password = payload.get("password", "").strip()
    if not email or not password:
        return {"ok": False, "error": "Ma'lumotlar yetishmayapti"}
    with _data_lock:
        data = _load_data()
        accs = data["users"].get(username, {}).get("accounts", [])
        acc  = next((a for a in accs if a["name"] == email), None)
        if not acc:
            return {"ok": False, "error": "Akkaunt topilmadi"}
        acc["password"] = password
        _save_data(data)
    _ensure(username)
    _update_acc(username, email, status="RESTARTING")
    threading.Thread(
        target=_run_bot,
        args=(username, email, password, acc.get("api_key","")),
        daemon=True,
    ).start()
    return {"ok": True}


@app.post("/api/accounts/delete")
async def delete_account(payload: dict, username: str = Depends(_get_username)):
    email = payload.get("name", "").strip()
    if not email:
        return {"ok": False, "error": "Email ko'rsatilmagan"}
    with _data_lock:
        data = _load_data()
        accs = data["users"].get(username, {}).get("accounts", [])
        new  = [a for a in accs if a["name"] != email]
        if len(new) == len(accs):
            return {"ok": False, "error": "Akkaunt topilmadi"}
        data["users"][username]["accounts"] = new
        _save_data(data)
    _ensure(username)
    with state_locks[username]:
        user_state[username] = [a for a in user_state[username] if a["name"] != email]
    user_bots.get(username, {}).pop(email, None)
    asyncio.create_task(_push(username))
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = ""):
    username = TOKENS.get(token)
    if not username:
        await ws.close(code=4001)
        return
    await ws.accept()
    _ensure(username)
    ws_pool[username].add(ws)
    with state_locks[username]:
        snapshot = list(user_state[username])
    await ws.send_text(json.dumps({"type": "state", "accounts": snapshot}))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_pool[username].discard(ws)


@app.get("/")
async def serve_dashboard():
    return FileResponse(os.path.join(os.path.dirname(__file__), "dashboard.html"))
