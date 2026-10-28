import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def db_path() -> str:
    return os.environ.get("APP_DB", str(ROOT / "data" / "app.db"))


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # 트랜잭션 경계를 코드에서 직접 제어하기 위해 autocommit 모드로 연다.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))


@contextmanager
def tx(conn: sqlite3.Connection):
    """하나의 트랜잭션. 예외가 나면 전체를 롤백한다."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 설정 기본값: 연결 전에는 예시 데이터 폴더를 파일 어댑터로 읽는다.
DEFAULT_SETTINGS = {
    "schedule_time": "06:00",
    "gcc_dir": str(ROOT / "sample_data" / "gcc"),
    "law_dir": str(ROOT / "sample_data" / "law"),
    "llm_dir": str(ROOT / "sample_data" / "llm"),
}


def get_setting(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM app_setting WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS[key]


def set_setting(conn, key: str, value: str) -> None:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    conn.execute(
        "INSERT INTO app_setting(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
