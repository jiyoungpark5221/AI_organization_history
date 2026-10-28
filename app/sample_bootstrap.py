"""샘플 배포용 초기 데이터.

디스크가 영구 보존되지 않는 환경(Vercel 등)에서 빈 DB로 시작하면 화면에 보여줄 것이 없고 로그인할 계정도 없다.
`VERCEL` 또는 `APP_SAMPLE_BOOTSTRAP=1` 환경에서 DB 가 비어 있으면 sample_data/ 의 가상 예시 자료로 일일 작업을
돌려 검토 건을 채운다. 로그인 계정은 환경변수 SAMPLE_LOGIN_ID / SAMPLE_PASSWORD 가 모두 있을 때만 만든다
(비밀번호를 코드나 저장소에 두지 않는다). 승인 결과는 인스턴스가 바뀌면 사라지는 임시 데이터다.
"""
import os

from . import adapters, auth, daily
from .db import ROOT

SAMPLE_DAYS = ("2026-10-01", "2026-10-02", "2026-10-03")


def enabled() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("APP_SAMPLE_BOOTSTRAP") == "1")


def bootstrap(conn) -> dict:
    """DB 가 비어 있을 때만 샘플 자료를 채운다. 이미 수집 기록이 있으면 아무것도 하지 않는다."""
    result = {"collections": 0, "user_created": False}
    if not enabled():
        return result
    if conn.execute("SELECT COUNT(*) FROM collection_run").fetchone()[0] == 0:
        sample = ROOT / "sample_data"
        gcc = adapters.FileGccSource(str(sample / "gcc"))
        law = adapters.FileLawSource(str(sample / "law"))
        llm = adapters.FileLlmClient(str(sample / "llm"))
        for day in SAMPLE_DAYS:
            daily.run(conn, day, gcc, law, llm)
            result["collections"] += 1
    login_id = os.environ.get("SAMPLE_LOGIN_ID")
    password = os.environ.get("SAMPLE_PASSWORD")
    if login_id and password:
        exists = conn.execute("SELECT 1 FROM app_user WHERE login_id=?", (login_id,)).fetchone()
        if not exists:
            auth.create_user(conn, login_id, os.environ.get("SAMPLE_DISPLAY_NAME", "샘플 계정"), password)
            result["user_created"] = True
    return result
