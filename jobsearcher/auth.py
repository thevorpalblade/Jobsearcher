"""Users, passwords, sessions and login throttling for the web UI (docs/m10-multi-user.md).

Stdlib only: scrypt for passwords, random tokens for sessions and invites. Only a
SHA-256 of each token is stored, so a copy of the database holds no live session or
invite. Accounts are made from the CLI (`jobsearcher users add`), which prints a
one-time link where the person sets their own password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ADMIN, USER = "admin", "user"
MIN_PASSWORD, MAX_PASSWORD = 12, 1024  # an upper bound keeps hashing cheap to refuse

SESSION_IDLE = timedelta(days=14)
SESSION_MAX = timedelta(days=30)
SESSION_TOUCH = timedelta(minutes=5)  # how often last_seen is written, at most
INVITE_VALID = timedelta(hours=24)

# Login throttling: failures in the window, per username and per client address.
THROTTLE_WINDOW = timedelta(minutes=15)
MAX_FAILURES_USER = 5
MAX_FAILURES_IP = 20

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


class AuthError(ValueError):
    """A problem to show the person (bad password, expired link, ...)."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )
    b64 = base64.b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked against when the username doesn't exist, so a login takes as long either way
# and the response time doesn't reveal which usernames exist.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def check_new_password(password: str, confirm: str | None = None) -> None:
    if confirm is not None and password != confirm:
        raise AuthError("The two passwords don't match.")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AuthError("That password is too long.")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    profile: str | None
    disabled: bool

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> User:
        return cls(
            id=row["id"],
            username=row["username"],
            role=row["role"],
            profile=row["profile"],
            disabled=bool(row["disabled"]),
        )


class Auth:
    """Account and session operations on the app's database (tables in store.SCHEMA)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- accounts -------------------------------------------------------------

    def create_user(self, username: str, role: str, profile: str | None) -> tuple[User, str]:
        """A new account without a password, and the one-time invite token to set it."""
        username = username.strip()
        if not username or len(username) > 64 or any(c.isspace() for c in username):
            raise AuthError("Pick a username without spaces (at most 64 characters).")
        if role not in (ADMIN, USER):
            raise AuthError(f"Unknown role {role!r}")
        if self.user_by_name(username) is not None:
            raise AuthError(f"User {username!r} already exists.")
        with self.conn:
            self.conn.execute(
                "INSERT INTO users (username, role, profile, created_at) VALUES (?, ?, ?, ?)",
                (username, role, profile, _now().isoformat()),
            )
        user = self.user_by_name(username)
        assert user is not None
        self.audit("user_created", username, f"role={role} profile={profile}")
        return user, self.new_invite(user)

    def new_invite(self, user: User) -> str:
        """A fresh one-time link token for setting the password (an earlier one stops
        working). The current password, if any, keeps working until it's used."""
        token = secrets.token_urlsafe(32)
        with self.conn:
            self.conn.execute(
                "UPDATE users SET invite_hash = ?, invite_expires = ? WHERE id = ?",
                (_token_hash(token), (_now() + INVITE_VALID).isoformat(), user.id),
            )
        self.audit("invite_created", user.username)
        return token

    def user_by_name(self, username: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return User.from_row(row) if row else None

    def user_by_invite(self, token: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE invite_hash = ? AND invite_expires > ? AND disabled = 0",
            (_token_hash(token), _now().isoformat()),
        ).fetchone()
        return User.from_row(row) if row else None

    def users(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT u.*, (SELECT MAX(last_seen) FROM sessions s WHERE s.user_id = u.id)"
            " AS last_seen, password_hash IS NOT NULL AS has_password"
            " FROM users u ORDER BY username"
        ).fetchall()

    def set_password(self, user: User, password: str, ip: str = "") -> None:
        """Set a new password (validated), use up any invite, and end every session."""
        check_new_password(password)
        with self.conn:
            self.conn.execute(
                "UPDATE users SET password_hash = ?, invite_hash = NULL, invite_expires = NULL"
                " WHERE id = ?",
                (hash_password(password), user.id),
            )
            self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
        self.audit("password_set", user.username, ip=ip)

    def change_password(self, user: User, current: str, new: str, ip: str = "") -> None:
        row = self.conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user.id,)
        ).fetchone()
        if row is None or not verify_password(current, row["password_hash"] or _DUMMY_HASH):
            self.audit("password_change_failed", user.username, ip=ip)
            raise AuthError("The current password is wrong.")
        self.set_password(user, new, ip)

    def set_disabled(self, user: User, disabled: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user.id)
            )
            if disabled:
                self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
        self.audit("user_disabled" if disabled else "user_enabled", user.username)

    # --- logging in -----------------------------------------------------------

    def throttled(self, username: str, ip: str) -> bool:
        since = (_now() - THROTTLE_WINDOW).isoformat()
        count = lambda key: self.conn.execute(  # noqa: E731
            "SELECT COUNT(*) FROM login_failures WHERE key = ? AND ts > ?", (key, since)
        ).fetchone()[0]
        return (
            count(f"user:{username.casefold()}") >= MAX_FAILURES_USER
            or count(f"ip:{ip}") >= MAX_FAILURES_IP
        )

    def authenticate(self, username: str, password: str, ip: str) -> User:
        """The user for a correct username and password. Raises AuthError with the same
        message for an unknown user, a wrong password or a disabled account."""
        username = username.strip()
        if self.throttled(username, ip):
            self.audit("login_throttled", username, ip=ip)
            raise AuthError("Too many failed attempts. Try again in 15 minutes.")
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        stored = row["password_hash"] if row is not None else None
        ok = verify_password(password[:MAX_PASSWORD], stored or _DUMMY_HASH)
        if row is None or stored is None or not ok or row["disabled"]:
            now = _now().isoformat()
            with self.conn:
                self.conn.executemany(
                    "INSERT INTO login_failures (key, ts) VALUES (?, ?)",
                    [(f"user:{username.casefold()}", now), (f"ip:{ip}", now)],
                )
                self.conn.execute(
                    "DELETE FROM login_failures WHERE ts < ?",
                    ((_now() - THROTTLE_WINDOW).isoformat(),),
                )
            self.audit("login_failed", username, ip=ip)
            raise AuthError("Wrong username or password.")
        user = User.from_row(row)
        with self.conn:
            self.conn.execute(
                "DELETE FROM login_failures WHERE key = ?", (f"user:{username.casefold()}",)
            )
        self.audit("login", user.username, ip=ip)
        return user

    # --- sessions -------------------------------------------------------------

    def create_session(self, user: User, ip: str = "", user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, last_seen, expires_at,"
                " ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _token_hash(token),
                    user.id,
                    now.isoformat(),
                    now.isoformat(),
                    (now + SESSION_MAX).isoformat(),
                    ip,
                    user_agent[:300],
                ),
            )
        return token

    def session_user(self, token: str) -> User | None:
        """The logged-in user for a session token, if the session is still valid."""
        if not token or len(token) > 200:
            return None
        row = self.conn.execute(
            "SELECT u.*, s.last_seen AS s_last_seen, s.expires_at AS s_expires"
            " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if row is None or row["disabled"]:
            return None
        now = _now()
        last_seen = datetime.fromisoformat(row["s_last_seen"])
        if now >= datetime.fromisoformat(row["s_expires"]) or now - last_seen >= SESSION_IDLE:
            self.end_session(token)
            return None
        if now - last_seen >= SESSION_TOUCH:
            with self.conn:
                self.conn.execute(
                    "UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                    (now.isoformat(), _token_hash(token)),
                )
        return User.from_row(row)

    def end_session(self, token: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    # --- audit log ------------------------------------------------------------

    def audit(self, event: str, username: str = "", detail: str = "", ip: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO audit_log (ts, event, username, detail, ip) VALUES (?, ?, ?, ?, ?)",
                (_now().isoformat(), event, username[:64], detail[:500], ip),
            )
