import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time


ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TTL_SECONDS = 600
MAX_ATTEMPTS = 5
MAX_DEVICES = 32


class Pairing:
    def __init__(self, directory, clock=time.monotonic):
        self.path = directory / "paired-devices.sqlite3"
        self.clock = clock
        self.lock = threading.Lock()
        self.code = None
        self.deadline = 0
        self.remaining = 0

    def issue(self):
        with self.lock:
            raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
            self.code = raw
            self.deadline = self.clock() + TTL_SECONDS
            self.remaining = MAX_ATTEMPTS
            return raw[:4] + "-" + raw[4:]

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=3)
        db.execute("CREATE TABLE IF NOT EXISTS devices (digest TEXT PRIMARY KEY)")
        return db

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def redeem(self, code):
        with self.lock:
            if self.code is None or self.clock() >= self.deadline or self.remaining <= 0:
                self.code = None
                return 401, {"error": "pairing_unavailable"}
            self.remaining -= 1
            valid = isinstance(code, str) and code.isascii() and re.fullmatch(
                r"[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}", code.strip())
            candidate = code.strip().upper().replace("-", "") if valid else ""
            if not hmac.compare_digest(candidate, self.code):
                if self.remaining == 0:
                    self.code = None
                return 401, {"error": "invalid_pairing_code"}
            token = secrets.token_urlsafe(32)
            db = self._connect()
            try:
                with db:
                    if db.execute("SELECT count(*) FROM devices").fetchone()[0] >= MAX_DEVICES:
                        return 409, {"error": "device_limit"}
                    db.execute("INSERT INTO devices VALUES (?)", (self.digest(token),))
            finally:
                db.close()
            self.code = None
            return 200, {"token": token}

    def authorized(self, token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return False
        with self.lock:
            if not self.path.exists():
                return False
            db = self._connect()
            try:
                return db.execute("SELECT 1 FROM devices WHERE digest = ?", (self.digest(token),)).fetchone() is not None
            finally:
                db.close()

    def revoke_all(self):
        with self.lock:
            self.code = None
            db = self._connect()
            try:
                with db:
                    db.execute("DELETE FROM devices")
            finally:
                db.close()
