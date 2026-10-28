"""외부 연결 어댑터.

GCC·법제처·내부 LLM의 실제 연결 방식(주소, 인증, 응답 형식)은 확인되지 않았다.
여기서 정의하는 반환 형식은 '우리 시스템 내부 형식'이며 원천 응답 형식이 아니다.
실연계 구현은 원천 응답을 이 내부 형식으로 변환하는 클래스를 추가해 교체한다.
파일 구현은 sample_data/ 의 가상 예시 데이터를 읽는 시험용이며, 수동 파일 입력 보조 기능에도 쓴다.
"""
import json
from pathlib import Path
from typing import Protocol


class SourceError(Exception):
    """원천 자료를 가져오지 못했다."""


class GccSource(Protocol):
    def fetch(self, day: str) -> dict:
        """-> {"complete": bool, "records": [{gcc_code, org_name, parent_code,
        prev_org_code, created_date, abolished_date}, ...]}  (day = 'YYYY-MM-DD')"""


class LawSource(Protocol):
    def fetch(self, primary_code: str, codes: list[str]) -> dict:
        """-> {"versions": [{role: '개편 전'|'개편 후', law_name, law_ident, version_ident,
        effective_date, source_url, provisions: [{article_ref, is_addendum, text}]}]}"""


class LlmClient(Protocol):
    def analyze(self, payload: dict) -> dict:
        """payload = {primary_code, summary, changes, law_versions}
        -> {"summary": str,
            "relations": [{from_code, to_code, relation_type, valid_from, valid_to, date_status,
                           evidence: [{role, article_ref, excerpt, reason}]}],
            "task_moves": [{from_code, to_code, task_description, move_scope, effective_date,
                            date_status, evidence: [...]}]}
        구조 검증은 analyze.validate_analysis 가 맡는다."""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise SourceError(f"파일 없음: {path.name}") from e
    except json.JSONDecodeError as e:
        raise SourceError(f"JSON 형식 오류: {path.name}: {e}") from e


class FileGccSource:
    def __init__(self, directory: str):
        self.dir = Path(directory)

    def fetch(self, day: str) -> dict:
        return _read_json(self.dir / f"{day}.json")


class FileLawSource:
    def __init__(self, directory: str):
        self.dir = Path(directory)

    def fetch(self, primary_code: str, codes: list[str]) -> dict:
        return _read_json(self.dir / f"{primary_code}.json")


class FileLlmClient:
    """고정 응답(예시)을 돌려주는 시험용 클라이언트. 실제 LLM을 호출하지 않는다."""

    def __init__(self, directory: str):
        self.dir = Path(directory)

    def analyze(self, payload: dict) -> dict:
        return _read_json(self.dir / f"{payload['primary_code']}.json")


def from_settings(conn):
    from .db import get_setting

    return (
        FileGccSource(get_setting(conn, "gcc_dir")),
        FileLawSource(get_setting(conn, "law_dir")),
        FileLlmClient(get_setting(conn, "llm_dir")),
    )
