"""샘플 배포용 초기 데이터(Vercel 등 영구 디스크가 없는 환경)."""
import sqlite3

import pytest

from app import db, sample_bootstrap


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "boot.db"))
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("VERCEL", "APP_SAMPLE_BOOTSTRAP", "SAMPLE_LOGIN_ID", "SAMPLE_PASSWORD", "SAMPLE_DISPLAY_NAME", "APP_DB"):
        monkeypatch.delenv(k, raising=False)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_disabled_by_default_does_nothing(conn):
    assert sample_bootstrap.bootstrap(conn) == {"collections": 0, "user_created": False}
    assert _count(conn, "collection_run") == 0 and _count(conn, "app_user") == 0


def test_fills_sample_cases_when_enabled(conn, monkeypatch):
    monkeypatch.setenv("APP_SAMPLE_BOOTSTRAP", "1")
    assert sample_bootstrap.bootstrap(conn)["collections"] == 3
    assert _count(conn, "review_case") == 2          # 10-02 변경 → 건 2개, 10-03 재수집은 중복 없음
    assert _count(conn, "relation_candidate") >= 1
    # 확정 데이터·검토 이력은 샘플이라도 승인 전에는 비어 있다
    assert _count(conn, "confirmed_relation") == 0 and _count(conn, "review_log") == 0


def test_second_call_does_not_duplicate(conn, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    sample_bootstrap.bootstrap(conn)
    before = (_count(conn, "collection_run"), _count(conn, "review_case"))
    assert sample_bootstrap.bootstrap(conn)["collections"] == 0
    assert (_count(conn, "collection_run"), _count(conn, "review_case")) == before


def test_user_created_only_when_both_env_set(conn, monkeypatch):
    monkeypatch.setenv("APP_SAMPLE_BOOTSTRAP", "1")
    monkeypatch.setenv("SAMPLE_LOGIN_ID", "sample")
    sample_bootstrap.bootstrap(conn)
    assert _count(conn, "app_user") == 0             # 비밀번호가 없으면 계정을 만들지 않는다
    monkeypatch.setenv("SAMPLE_PASSWORD", "pw-for-test-1234")
    assert sample_bootstrap.bootstrap(conn)["user_created"] is True
    row = conn.execute("SELECT password_hash FROM app_user WHERE login_id='sample'").fetchone()
    assert "pw-for-test-1234" not in row["password_hash"]
    assert sample_bootstrap.bootstrap(conn)["user_created"] is False


def test_db_path_uses_tmp_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    assert db.db_path() == "/tmp/app.db"
    monkeypatch.setenv("APP_DB", "x.db")
    assert db.db_path() == "x.db"
