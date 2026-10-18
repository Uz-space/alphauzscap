import os
import json
import time
import base64
import random
import requests
import re
import string


CONFIG_FILE = "alpha.json"

COLOR_CYAN    = "hsl(180, 50%, 50%)"
COLOR_YELLOW  = "hsl(60, 50%, 50%)"
COLOR_GREEN   = "hsl(120, 50%, 50%)"
COLOR_ORANGE  = "hsl(30, 50%, 50%)"
COLOR_RED     = "hsl(0, 50%, 50%)"
COLOR_MAGENTA = "hsl(300, 50%, 50%)"
COLOR_GRAY    = "hsl(0, 0%, 50%)"


def generate_fp(length: int = 16) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


class TronPickBot:
    def __init__(self, email: str, password: str, api_key: str, status_callback=None):
        self.session = requests.Session()
        self.email = email
        self.password = password
        self.api_key = api_key
        self.domain = "tronpick.io"
        self.balance = "0.000000"
        self.level = "Stone"
        self.target_timestamp = 0
        self.fp = generate_fp()
        self.ua = (
            "Mozilla/5.0 (Linux; Android 12; V2029 Build/SP1A.210812.003) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.6167.164 Mobile Safari/537.36"
        )
        self.headers = {
            'User-Agent': self.ua,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': f'https://{self.domain}',
            'Referer': f'https://{self.domain}/login.php',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        self.status_callback = status_callback

    def set_status(self, state: str) -> None:
        if self.status_callback:
            self.status_callback(state)

    def solve_captcha(self) -> str | None:
        try:
            self.set_status("PROCESSING")
            payload = {
                'key': self.api_key,
                'method': 'turnstile',
                'sitekey': '0x4AAAAAAAW74HiAaujGhyeV',
                'pageurl': f'https://{self.domain}/faucet.php',
                'json': 1
            }
            res = requests.post(
                "https://api.sctg.xyz/in.php", data=payload, timeout=30
            ).json()
            if res.get('status') != 1:
                return None

            rid = res.get('request')
            for _ in range(40):
                time.sleep(3)
                g = requests.get(
                    f"https://api.sctg.xyz/res.php"
                    f"?key={self.api_key}&action=get&id={rid}&json=1",
                    timeout=30
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
                    headers={'User-Agent': self.ua},
                    timeout=20
                )
            except Exception:
                self.session.get(
                    f"https://{self.domain}/login.php",
                    headers={'User-Agent': self.ua},
                    timeout=30
                )

            csrf = self.session.cookies.get('csrf_cookie_name')
            if not csrf:
                self.set_status("ERROR")
                return False, "CSRF Missing"

            token = self.solve_captcha()
            if not token:
                self.set_status("ERROR")
                return False, "Captcha Failed"

            payload = {
                'action': 'login',
                'email': self.email,
                'password': self.password,
                'captcha_type': '3',
                'c_captcha_response': token,
                'csrf_test_name': csrf,
            }

            res = self.session.post(
                f"https://{self.domain}/process.php",
                data=payload,
                headers=self.headers,
                timeout=30
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
                headers={'User-Agent': self.ua},
                timeout=20
            )

            bal = (
                re.search(r'user_balance">([\d.]+)', res.text) or
                re.search(r'id="user_balance">([\d.]+)', res.text)
            )
            if bal:
                self.balance = bal.group(1)

            lvl = re.search(r'Your level is\s*<b>(.*?)</b>', res.text)
            if lvl:
                self.level = lvl.group(1)

            tmr = (
                re.search(r'show_countdown_clock\((\d+)\)', res.text) or
                re.search(r'data-time=["\'](\\d+)["\']', res.text) or
                re.search(r'var\s+autocontrol\s*=\s*(\d+);', res.text) or
                re.search(r'countDownDate\s*=\s*(\d+);', res.text)
            )

            if tmr:
                self.target_timestamp = int(time.time()) + int(tmr.group(1))
            else:
                self.target_timestamp = int(time.time())

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

            csrf = self.session.cookies.get('csrf_cookie_name')
            ts = int(time.time())
            data_str = f"{random.randint(100, 200)}:{random.randint(10, 50)}:{ts}"
            xor_key = "0542f6c18bc7906d742a8401d0b5ef7f50ee304bff4f032348a4ceb3fd2d6bb1"
            hashed = base64.b64encode(
                "".join(
                    chr(ord(c) ^ ord(xor_key[i % len(xor_key)]))
                    for i, c in enumerate(data_str)
                ).encode()
            ).decode()

            payload = {
                'action': 'claim_hourly_faucet',
                'hash': hashed,
                'captcha_type': '3',
                'c_captcha_response': token,
                'csrf_test_name': csrf,
            }

            res = self.session.post(
                f"https://{self.domain}/process.php",
                data=payload,
                headers=self.headers,
                timeout=30
            ).json()

            self.set_status("SUCCESS" if res.get('ret') == 1 else "ERROR")
            self.update_info()
        except Exception:
            self.set_status("ERROR")
