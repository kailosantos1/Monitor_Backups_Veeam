from __future__ import annotations

import secrets
import time
from collections import defaultdict

import bcrypt

from .db import Database
from .timeutil import now_utc, to_db

MIN_PASSWORD = 8


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


def check_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode())
    except ValueError:
        return False


def validate_password(pw: str) -> str | None:
    if len(pw) < MIN_PASSWORD:
        return f"A senha precisa ter pelo menos {MIN_PASSWORD} caracteres."
    if len(pw.encode("utf-8")) > 72:
        return "A senha pode ter no máximo 72 bytes."
    return None


def create_user(db: Database, username: str, password: str, role: str) -> None:
    username = username.strip()
    if not username or len(username) > 50 or any(ch.isspace() for ch in username):
        raise ValueError("Nome de usuário inválido (sem espaços, até 50 caracteres).")
    if role not in ("admin", "tecnico"):
        raise ValueError("Perfil inválido.")
    err = validate_password(password)
    if err:
        raise ValueError(err)
    with db.connect() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError(f"Usuário '{username}' já existe.")
        c.execute("INSERT INTO users(username, password_hash, role, created_at) VALUES (?,?,?,?)",
                  (username, hash_password(password), role, to_db(now_utc())))


def set_password(db: Database, username: str, password: str) -> None:
    err = validate_password(password)
    if err:
        raise ValueError(err)
    with db.connect() as c:
        n = c.execute("UPDATE users SET password_hash=? WHERE username=?",
                      (hash_password(password), username)).rowcount
    if not n:
        raise ValueError(f"Usuário '{username}' não existe.")


def authenticate(db: Database, username: str, password: str) -> dict | None:
    with db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not row:
        check_password(password, "$2b$12$" + "x" * 53)  # tempo constante
        return None
    if not row["active"] or not check_password(password, row["password_hash"]):
        return None
    return dict(row)


def get_user(db: Database, user_id: int) -> dict | None:
    with db.connect() as c:
        row = c.execute("SELECT id, username, role, active FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row and row["active"] else None


def new_csrf() -> str:
    return secrets.token_urlsafe(32)


class LoginThrottle:
    """Bloqueia um IP por 5 min após 8 tentativas erradas."""

    def __init__(self, limit: int = 8, window: int = 300):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = defaultdict(list)

    def blocked(self, ip: str) -> bool:
        now = time.monotonic()
        self.fails[ip] = [t for t in self.fails[ip] if now - t < self.window]
        return len(self.fails[ip]) >= self.limit

    def fail(self, ip: str) -> None:
        self.fails[ip].append(time.monotonic())

    def ok(self, ip: str) -> None:
        self.fails.pop(ip, None)
