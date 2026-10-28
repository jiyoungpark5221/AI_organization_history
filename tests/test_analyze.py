import copy
import json
from pathlib import Path

import pytest

from app import analyze
from app.adapters import FileLawSource, FileLlmClient
from app.analyze import AnalysisError, process_case, process_pending, validate_analysis
from app.db import connect, init_db

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    # 최신 성공 수집본(기관명 조회용)
    c.execute("INSERT INTO collection_run(started_at, base_date, status) VALUES('t','2026-10-02','성공')")
    for code, name in (("G2000", "가상정보화국"), ("G2100", "가상디지털국"), ("G3000", "가상통계센터")):
        c.execute("INSERT INTO gcc_record(collection_id, gcc_code, org_name) VALUES(1,?,?)", (code, name))
    return c


def add_case(conn, primary, codes=None):
    cid = conn.execute(
        "INSERT INTO review_case(primary_code, title, change_summary, detected_at) VALUES(?,?,?,'2026-10-02')",
        (primary, primary, f"{primary} 변경")).lastrowid
    for i, code in enumerate(codes or [primary]):
        conn.execute(
            "INSERT INTO change_event(case_id, change_key, change_type, gcc_code, field_name, detected_at)"
            " VALUES(?,?,'명칭',?,'org_name','2026-10-02')", (cid, f"{cid}-{i}-{code}", code))
    return cid


class FakeLaw:
    def __init__(self, data=None, error=None):
        self.data, self.error = data, error

    def fetch(self, primary_code, codes):
        if self.error:
            raise self.error
        return self.data


class FakeLlm:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def analyze(self, payload):
        self.calls += 1
        r = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return copy.deepcopy(r)


def law_data(after_eff="2026-10-02", with_before=True):
    v = [{"role": "개편 후", "law_name": "가상 직제", "effective_date": after_eff,
          "provisions": [{"article_ref": "제5조", "text": "후 조항"},
                         {"article_ref": "부칙 제1조", "is_addendum": True, "text": "시행일"}]}]
    if with_before:
        v.insert(0, {"role": "개편 전", "law_name": "가상 직제", "effective_date": "2020-01-01",
                     "provisions": [{"article_ref": "제5조", "text": "전 조항"}]})
    return {"versions": v}


def ev(role="개편 후", ref="제5조"):
    return {"role": role, "article_ref": ref, "excerpt": "발췌", "reason": "이유"}


def llm_data(date="2026-10-02", relations=None, moves=None):
    rel = {"from_code": "G2000", "to_code": "G2100", "relation_type": "승계", "valid_from": date,
           "valid_to": None, "date_status": "확인", "evidence": [ev()]}
    mv = {"from_code": "G2000", "to_code": "G2100", "task_description": "청사 정보시스템 운영",
          "move_scope": "전체", "effective_date": date, "date_status": "확인", "evidence": [ev("개편 전")]}
    return {"summary": "요약", "relations": [rel] if relations is None else relations,
            "task_moves": [mv] if moves is None else moves}


def count(conn, table, where="1=1"):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def case_row(conn, cid):
    return conn.execute("SELECT * FROM review_case WHERE case_id=?", (cid,)).fetchone()


# ---------- 검증 ----------

def test_validate_normalizes_unconfirmed_date():
    d = llm_data()
    d["relations"][0]["date_status"] = "미확인"
    assert validate_analysis(d)["relations"][0]["valid_from"] is None


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("summary"),
    lambda d: d.update(relations="x"),
    lambda d: d["task_moves"][0].update(move_scope="모두"),
    lambda d: d["relations"][0].update(date_status="모름"),
    lambda d: d["relations"][0].update(valid_from="2026/10/02"),
    lambda d: d["relations"][0].update(evidence="x"),
    lambda d: d["relations"][0]["evidence"][0].update(role="기타"),
])
def test_validate_rejects_bad_structure(mutate):
    d = llm_data()
    mutate(d)
    with pytest.raises(AnalysisError):
        validate_analysis(d)
    with pytest.raises(AnalysisError):
        validate_analysis("문자열")


# ---------- 분석 ----------

def test_normal_analysis(conn):
    cid = add_case(conn, "G2100", ["G2000", "G2100"])
    assert process_case(conn, cid, FakeLaw(law_data()), FakeLlm(llm_data())) == "완료"
    case = case_row(conn, cid)
    assert (case["analysis_status"], case["data_status"], case["analysis_attempts"]) == ("완료", "정상", 1)
    rel = conn.execute("SELECT * FROM relation_candidate").fetchone()
    assert (rel["from_name"], rel["to_name"], rel["review_status"]) == ("가상정보화국", "가상디지털국", "승인 대기")
    assert json.loads(rel["proposed_json"])["relation_type"] == "승계"
    assert rel["change_id"] is not None
    assert count(conn, "task_move_candidate") == 1
    assert count(conn, "evidence") == 2
    assert count(conn, "law_source") == 2 and count(conn, "law_provision") == 3
    # 확정 데이터는 건드리지 않는다
    assert count(conn, "confirmed_relation") == 0 and count(conn, "confirmed_task_move") == 0


def test_missing_law_version_is_preserved_and_flagged(conn):
    cid = add_case(conn, "G3000")
    llm = llm_data(relations=[{"from_code": "G3000", "to_code": "G3000", "relation_type": "명칭 변경",
                               "valid_from": "2026-10-02", "valid_to": None, "date_status": "확인",
                               "evidence": [ev()]}], moves=[])
    assert process_case(conn, cid, FakeLaw(law_data(with_before=False)), FakeLlm(llm)) == "완료"
    assert case_row(conn, cid)["data_status"] == "자료 부족"
    assert count(conn, "law_source") == 1 and count(conn, "relation_candidate") == 1


def test_law_fetch_failure_is_data_shortage_without_candidates(conn):
    cid = add_case(conn, "G2100")
    assert process_case(conn, cid, FakeLaw(error=OSError("연결 실패")), FakeLlm(llm_data())) == "완료"
    case = case_row(conn, cid)
    assert case["data_status"] == "자료 부족" and "연결 실패" in case["last_error"]
    assert count(conn, "relation_candidate") == 0 and count(conn, "task_move_candidate") == 0


def test_source_conflict_on_date_mismatch(conn):
    cid = add_case(conn, "G2100")
    process_case(conn, cid, FakeLaw(law_data(after_eff="2026-11-01")), FakeLlm(llm_data("2026-10-02")))
    assert case_row(conn, cid)["data_status"] == "원천 충돌"
    assert {r[0] for r in conn.execute("SELECT date_status FROM relation_candidate")} == {"원천 충돌"}
    assert {r[0] for r in conn.execute("SELECT date_status FROM task_move_candidate")} == {"원천 충돌"}


def test_shortage_outranks_conflict(conn):
    cid = add_case(conn, "G2100")
    process_case(conn, cid, FakeLaw(law_data("2026-11-01", with_before=False)), FakeLlm(llm_data()))
    assert case_row(conn, cid)["data_status"] == "자료 부족"


def test_llm_failure_then_retry_succeeds(conn):
    cid = add_case(conn, "G2100")
    llm = FakeLlm(RuntimeError("LLM 연결 오류"), {"summary": "x"}, llm_data())
    law = FakeLaw(law_data())
    assert process_pending(conn, law, llm) == [cid]
    case = case_row(conn, cid)
    assert case["analysis_status"] == "실패" and "LLM 연결 오류" in case["last_error"]
    assert count(conn, "relation_candidate") == 0
    process_pending(conn, law, llm)  # 구조 오류도 실패 처리
    assert case_row(conn, cid)["analysis_status"] == "실패"
    process_pending(conn, law, llm)
    case = case_row(conn, cid)
    assert (case["analysis_status"], case["analysis_attempts"], case["last_error"]) == ("완료", 3, None)
    assert count(conn, "relation_candidate") == 1


def test_max_attempts_stops_retry(conn):
    cid = add_case(conn, "G2100")
    llm = FakeLlm(RuntimeError("계속 실패"))
    for _ in range(4):
        process_pending(conn, FakeLaw(law_data()), llm, max_attempts=2)
    assert case_row(conn, cid)["analysis_attempts"] == 2


def test_task_move_without_valid_evidence_is_not_created(conn):
    cid = add_case(conn, "G2100")
    bad = {"from_code": "G2000", "to_code": "G2100", "task_description": "근거 없는 업무", "move_scope": "일부",
           "effective_date": None, "date_status": "미확인", "evidence": [ev(ref="제99조")]}
    none = dict(bad, task_description="근거 목록 비어 있음", evidence=[])
    process_case(conn, cid, FakeLaw(law_data()), FakeLlm(llm_data(moves=[bad, none])))
    assert count(conn, "task_move_candidate") == 0
    assert count(conn, "relation_candidate") == 1


def test_reprocess_does_not_duplicate(conn):
    cid = add_case(conn, "G2100")
    law, llm = FakeLaw(law_data(with_before=False)), FakeLlm(llm_data())
    process_case(conn, cid, law, llm)
    assert process_pending(conn, law, llm) == [cid]  # 자료 부족 건은 재시도 대상
    # 개편 전 버전이 없으면 그 조항만 근거로 든 업무 이동은 근거가 사라져 만들어지지 않는다
    assert (count(conn, "relation_candidate"), count(conn, "task_move_candidate"),
            count(conn, "evidence"), count(conn, "law_source")) == (1, 0, 1, 1)
    # 이후 자료가 확보되면 정상으로 바뀌고 역시 중복 없음
    law.data = law_data()
    process_pending(conn, law, llm)
    assert case_row(conn, cid)["data_status"] == "정상"
    assert (count(conn, "relation_candidate"), count(conn, "task_move_candidate"),
            count(conn, "evidence"), count(conn, "law_source")) == (1, 1, 2, 2)
    assert process_pending(conn, law, llm) == []  # 정상 완료 건은 대상이 아님


def test_reviewed_case_is_not_reanalyzed(conn):
    cid = add_case(conn, "G2100")
    law, llm = FakeLaw(law_data(with_before=False)), FakeLlm(llm_data())
    process_case(conn, cid, law, llm)
    conn.execute("UPDATE relation_candidate SET review_status='승인 완료'")
    calls = llm.calls
    assert process_pending(conn, law, llm) == []
    assert process_case(conn, cid, law, llm) == "완료"
    assert llm.calls == calls and count(conn, "relation_candidate") == 1


def test_save_failure_rolls_back_and_marks_failed(conn, monkeypatch):
    cid = add_case(conn, "G2100")

    def boom(*a, **k):
        raise RuntimeError("DB 오류")

    monkeypatch.setattr(analyze, "_write_candidates", boom)
    assert process_case(conn, cid, FakeLaw(law_data()), FakeLlm(llm_data())) == "실패"
    assert count(conn, "law_source") == 0  # 롤백
    assert "DB 오류" in case_row(conn, cid)["last_error"]


# ---------- 샘플 파일 ----------

def test_sample_files_expected_results(conn):
    law, llm = FileLawSource(str(SAMPLE / "law")), FileLlmClient(str(SAMPLE / "llm"))
    g2100 = add_case(conn, "G2100", ["G2000", "G2100"])
    g3000 = add_case(conn, "G3000")
    assert process_pending(conn, law, llm) == [g2100, g3000]

    c = case_row(conn, g2100)
    assert (c["analysis_status"], c["data_status"]) == ("완료", "정상")
    assert count(conn, "relation_candidate", f"case_id={g2100}") == 1
    assert count(conn, "task_move_candidate", f"case_id={g2100}") == 1  # 근거 없는 업무 이동은 버려짐
    mv = conn.execute("SELECT * FROM task_move_candidate").fetchone()
    assert (mv["task_description"], mv["move_scope"], mv["date_status"]) == ("청사 정보시스템 운영", "전체", "확인")

    c = case_row(conn, g3000)
    assert (c["analysis_status"], c["data_status"]) == ("완료", "자료 부족")
    assert count(conn, "relation_candidate", f"case_id={g3000}") == 1
    assert count(conn, "task_move_candidate", f"case_id={g3000}") == 0
