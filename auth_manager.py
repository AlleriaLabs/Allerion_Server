"""Password accounts and revocable bearer sessions for Allerion.

The legacy TOKEN remains a one-time bootstrap credential and backward-compatible
machine credential. Passwords are PBKDF2-HMAC-SHA256 hashed; raw passwords and
session tokens are never persisted.
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

_ITERATIONS = 310_000
_SESSION_SECONDS = 30 * 24 * 60 * 60


class AuthManager:
    def __init__(self, data_dir):
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        self.path = os.path.join(data_dir, "auth.sqlite3")
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self):
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at INTEGER NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id)")
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))

    @staticmethod
    def _password_record(password):
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
        return salt, digest

    @staticmethod
    def _valid_username(username):
        return isinstance(username, str) and 3 <= len(username.strip()) <= 32 and all(
            c.isalnum() or c in "._-" for c in username.strip()
        )

    @staticmethod
    def _valid_password(password):
        return isinstance(password, str) and 12 <= len(password) <= 128

    def status(self):
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM users WHERE disabled=0").fetchone()[0]
        return {"setup_required": count == 0, "accounts": count, "password_auth": True}

    def create_first_admin(self, username, password, bootstrap_token, expected_token):
        if not expected_token or not hmac.compare_digest(str(bootstrap_token or ""), expected_token):
            raise PermissionError("The one-time server setup credential was rejected")
        if not self._valid_username(username):
            raise ValueError("Username must be 3–32 characters and use letters, numbers, dot, underscore or hyphen")
        if not self._valid_password(password):
            raise ValueError("Password must be at least 12 characters")
        salt, digest = self._password_record(password)
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count:
                raise PermissionError("Initial account setup has already been completed")
            db.execute("INSERT INTO users(username,password_salt,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                       (username.strip(), salt, digest, "admin", int(time.time())))
        return self.login(username, password)

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("Username and password are required")
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND disabled=0",
                             (username.strip(),)).fetchone()
        if not row:
            raise PermissionError("Invalid username or password")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                        bytes(row["password_salt"]), _ITERATIONS)
        if not hmac.compare_digest(candidate, bytes(row["password_hash"])):
            raise PermissionError("Invalid username or password")
        raw = secrets.token_urlsafe(32)
        now = int(time.time())
        expires = now + _SESSION_SECONDS
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (digest, row["id"], expires, now))
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        return {"access_token": raw, "token_type": "Bearer", "expires_at": expires,
                "user": {"id": row["id"], "username": row["username"], "role": row["role"]}}

    def authenticate(self, token):
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = int(time.time())
        with self._connect() as db:
            row = db.execute("""SELECT users.id,users.username,users.role
                FROM sessions JOIN users ON users.id=sessions.user_id
                WHERE sessions.token_hash=? AND sessions.expires_at>? AND users.disabled=0""",
                (digest, now)).fetchone()
        return dict(row) if row else None

    def create_user(self, actor, username, password, role="user"):
        if not actor or actor.get("role") != "admin":
            raise PermissionError("Administrator access is required")
        if not self._valid_username(username):
            raise ValueError("Username must be 3–32 characters and use letters, numbers, dot, underscore or hyphen")
        if not self._valid_password(password):
            raise ValueError("Password must be at least 12 characters")
        if role not in ("admin", "user"):
            raise ValueError("Role must be admin or user")
        salt, digest = self._password_record(password)
        try:
            with self._connect() as db:
                cur = db.execute("INSERT INTO users(username,password_salt,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                                 (username.strip(), salt, digest, role, int(time.time())))
                return {"id": cur.lastrowid, "username": username.strip(), "role": role}
        except sqlite3.IntegrityError:
            raise ValueError("That username already exists")

    def list_users(self, actor):
        if not actor or actor.get("role") != "admin":
            raise PermissionError("Administrator access is required")
        with self._connect() as db:
            rows = db.execute("SELECT id,username,role,created_at,disabled FROM users ORDER BY id").fetchall()
        return {"users": [dict(row) for row in rows]}


    def revoke(self, token):
        """Revoke a bearer session immediately (logout)."""
        if not token:
            return False
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as db:
            cur = db.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
        return cur.rowcount > 0
