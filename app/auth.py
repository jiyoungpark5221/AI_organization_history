"""로그인 인증.

인증은 AuthProvider 인터페이스 뒤에 둔다. 기본 구현(DbAuthProvider)은 app_user 의 비밀번호 해시를 쓴다.
사내 SSO·LDAP 연동 방식은 확인되지 않았으므로 구현하지 않았고, 확정되면 같은 인터페이스의 구현체를 추가한다.
"""
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from typing import Protocol

from .db import now

MIN_PASSWORD_LENGTH = 8
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


class AuthProvider(Protocol):
    def authenticate(self, conn: sqlite3.Connection, login_id: str, password: str) -> sqlite3.Row | None:
        """로그인 성공 시 사용 가능한(is_active) app_user 행, 아니면 None. 실패 사유는 구분해 알리지 않는다."""


def hash_password(password: str) -> str:
    """'scrypt$n$r$p$솔트$해시' 형식. 솔트와 비용 인자를 함께 저장한다."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# 존재하지 않는 계정에도 같은 비용의 검증을 수행해 응답 시간으로 계정 존재 여부가 드러나지 않게 한다.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


class DbAuthProvider:
    def authenticate(self, conn, login_id, password):
        row = conn.execute("SELECT * FROM app_user WHERE login_id=?", ((login_id or "").strip(),)).fetchone()
        ok = verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
        if row is None or not ok or not row["is_active"]:
            return None
        conn.execute("UPDATE app_user SET last_login_at=? WHERE user_id=?", (now(), row["user_id"]))
        return conn.execute("SELECT * FROM app_user WHERE user_id=?", (row["user_id"],)).fetchone()


class AuthError(ValueError):
    pass


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")


def create_user(conn, login_id: str, display_name: str, password: str) -> int:
    login_id, display_name = (login_id or "").strip(), (display_name or "").strip()
    if not login_id or not display_name:
        raise AuthError("로그인 ID와 표시 이름은 비울 수 없습니다.")
    _check_password(password)
    if conn.execute("SELECT 1 FROM app_user WHERE login_id=?", (login_id,)).fetchone():
        raise AuthError("이미 있는 로그인 ID입니다.")
    return conn.execute(
        "INSERT INTO app_user(login_id, display_name, password_hash, created_at) VALUES(?,?,?,?)",
        (login_id, display_name, hash_password(password), now()),
    ).lastrowid


def _get_by_login(conn, login_id: str):
    row = conn.execute("SELECT * FROM app_user WHERE login_id=?", ((login_id or "").strip(),)).fetchone()
    if row is None:
        raise AuthError("해당 로그인 ID가 없습니다.")
    return row


def set_password(conn, login_id: str, password: str) -> None:
    row = _get_by_login(conn, login_id)
    _check_password(password)
    conn.execute("UPDATE app_user SET password_hash=? WHERE user_id=?", (hash_password(password), row["user_id"]))


def deactivate_user(conn, login_id: str) -> None:
    """계정은 삭제하지 않는다(과거 승인·정정 기록 보존). 비활성화하면 로그인과 기존 세션이 모두 막힌다."""
    row = _get_by_login(conn, login_id)
    conn.execute("UPDATE app_user SET is_active=0 WHERE user_id=?", (row["user_id"],))


def get_active_user(conn, user_id) -> sqlite3.Row | None:
    if not isinstance(user_id, int):
        return None
    return conn.execute("SELECT * FROM app_user WHERE user_id=? AND is_active=1", (user_id,)).fetchone()


# ---------------------------------------------------------------- 서명 키

def secret_key() -> bytes:
    """세션 서명 키. APP_SECRET_KEY 환경변수, 없으면 DB 폴더의 무작위 키 파일(최초 1회 생성)을 쓴다."""
    env = os.environ.get("APP_SECRET_KEY")
    if env:
        return env.encode("utf-8")
    from pathlib import Path

    from .db import ROOT, db_path

    path = db_path()
    folder = ROOT / "data" if path == ":memory:" else Path(path).resolve().parent
    folder.mkdir(parents=True, exist_ok=True)
    key_file = folder / ".secret_key"
    if not key_file.exists():
        try:
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    return key_file.read_text(encoding="ascii").strip().encode("utf-8")
