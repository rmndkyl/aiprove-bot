"""AIProve arena client — keypair identity, EIP-191 session, bounty/submit surface.

Base API: https://aiprove.fun
Auth: GET /api/auth/challenge?address= -> sign challenge (personal_sign) -> POST /api/auth/verify
Long-lived: POST /api/me/agent-token (Bearer) -> ~1 year token for autonomous runs.
Referral binding: POST /api/me/referral/claim {code} (Bearer).
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request

from eth_account import Account
from eth_account.messages import encode_defunct

BASE_DEFAULT = "https://aiprove.fun"
UA_POOL = [
    "aiprove-agent/1.0 (python-urllib)",
    "Mozilla/5.0 (compatible; aiprove-agent/1.0; +https://aiprove.fun/skill.md)",
    "python-requests/2.32 aiprove-loop",
]

_CTX = ssl.create_default_context()


class ApiError(RuntimeError):
    def __init__(self, status, body, path):
        super().__init__(f"{status} {path} :: {body[:300]}")
        self.status = status
        self.body = body
        self.path = path


class Agent:
    """One AIProve identity = one EVM keypair."""

    def __init__(self, private_key: str, base: str = BASE_DEFAULT, data_dir: str = "data", ua: str | None = None):
        self.acct = Account.from_key(private_key)
        self.address = self.acct.address
        self.base = base.rstrip("/")
        self.data_dir = data_dir
        self.ua = ua or UA_POOL[0]
        self._token: str | None = None
        self._token_exp: float = 0.0
        os.makedirs(self.data_dir, exist_ok=True)

    # ---------- low level ----------
    def _req(self, path: str, body=None, tok=None, method=None, timeout=40):
        headers = {"user-agent": self.ua, "accept": "application/json"}
        data = None
        if body is not None:
            headers["content-type"] = "application/json"
            data = json.dumps(body).encode()
        if tok:
            headers["authorization"] = "Bearer " + tok
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers,
            method=method or ("POST" if data is not None else "GET"),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode(errors="replace"), path) from None
        except urllib.error.URLError as e:
            raise ApiError(0, str(e), path) from None

    # ---------- auth ----------
    def login(self) -> str:
        ch = self._req(f"/api/auth/challenge?address={self.address}")
        msg = ch.get("challenge") or ch.get("message")
        if not msg:
            raise ApiError(0, f"no challenge in {ch}", "/api/auth/challenge")
        # server returns a checksummed address in `challenge`; sign the message verbatim
        signature = Account.sign_message(encode_defunct(text=msg), private_key=self.acct.key).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        v = self._req("/api/auth/verify", {"address": ch.get("address", self.address),
                                           "signature": signature})
        token = v.get("token") or v.get("accessToken")
        if not token:
            raise ApiError(0, f"no token in {v}", "/api/auth/verify")
        self._token = token
        self._token_exp = (v.get("expiresAt") or 0) / 1000 or time.time() + 3600
        self._save_session({"token": token, "address": self.address, "expiresAt": v.get("expiresAt")})
        return token

    def _session_path(self):
        return os.path.join(self.data_dir, f"session-{self.address.lower()[:10]}.json")

    def _save_session(self, obj):
        try:
            with open(self._session_path(), "w", encoding="utf-8") as f:
                json.dump(obj, f)
        except OSError:
            pass

    def token(self, refresh_margin=600) -> str:
        if self._token and time.time() < self._token_exp - refresh_margin:
            return self._token
        p = self._session_path()
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    s = json.load(f)
                exp = (s.get("expiresAt") or 0) / 1000
                if s.get("token") and exp and time.time() < exp - refresh_margin:
                    self._token, self._token_exp = s["token"], exp
                    return s["token"]
            except (OSError, ValueError):
                pass
        return self.login()

    # ---------- reads ----------
    def bounties(self, **filters):
        q = ""
        if filters:
            q = "?" + "&".join(f"{k}={v}" for k, v in filters.items() if v is not None)
        r = self._req("/api/bounties" + q)
        return r.get("bounties", r if isinstance(r, list) else [])

    def bounty(self, slug):
        return self._req(f"/api/bounties/{slug}").get("bounty", {})

    def me(self):
        return self._req("/api/me", tok=self.token())

    def leaderboard(self):
        return self._req("/api/leaderboard")

    # ---------- writes ----------
    def enter(self, slug, handle=None):
        return self._req(f"/api/bounties/{slug}/enter", {"handle": handle} if handle else {},
                         tok=self.token())

    def submit(self, slug, payload):
        return self._req(f"/api/bounties/{slug}/submit", payload, tok=self.token())

    def claim_referral(self, code):
        return self._req("/api/me/referral/claim", {"code": code}, tok=self.token())

    def save_profile(self, profile):
        return self._req("/api/me/profile", profile, tok=self.token())

    def agent_token(self):
        return self._req("/api/me/agent-token", {}, tok=self.token())
