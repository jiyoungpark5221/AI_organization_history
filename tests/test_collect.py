from pathlib import Path

import pytest

from app.adapters import FileGccSource, SourceError
from app.collect import change_key, collect
from app.db import connect, init_db

SAMPLE = str(Path(__file__).resolve().parent.parent / "sample_data" / "gcc")


class FakeGcc:
    def __init__(self, data):
        self.data = data  # day -> dict | Exception

    def fetch(self, day):
        v = self.data[day]
        if isinstance(v, Exception):
            raise v
        return v


def rec(code, name, parent=None, prev=None, created=None, abol=None):
    return {"gcc_code": code, "org_name": name, "parent_code": parent,
            "prev_org_code": prev, "created_date": created, "abolished_date": abol}


def ok(*records):
    return {"complete": True, "records": list(records)}


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    return c


def cases(conn):
    return conn.execute("SELECT * FROM review_case ORDER BY primary_code").fetchall()


def test_baseline_creates_no_changes(conn):
    r = collect(conn, FileGccSource(SAMPLE), "2026-10-01")
    assert r["status"] == "성공" and r["is_baseline"] is True
    assert r["change_count"] == 0 and r["new_case_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_event").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM gcc_record").fetchone()[0] == 4


def test_sample_scenario(conn):
    gcc = FileGccSource(SAMPLE)
    collect(conn, gcc, "2026-10-01")
    r = collect(conn, gcc, "2026-10-02")
    assert r["is_baseline"] is False
    assert r["change_count"] == 3 and r["new_case_count"] == 2
    g2100, g3000 = cases(conn)
    assert (g2100["primary_code"], g2100["title"]) == ("G2100", "가상디지털국")
    assert g3000["primary_code"] == "G3000"
    assert "가상통계원(G3000) 명칭: 가상통계원 → 가상통계센터" in g3000["change_summary"]
    assert "가상디지털국(G2100) 신설" in g2100["change_summary"]
    assert g2100["analysis_status"] == "대기"
    n = lambda cid: conn.execute(
        "SELECT COUNT(*) FROM change_event WHERE case_id=?", (cid,)).fetchone()[0]
    assert n(g2100["case_id"]) == 2 and n(g3000["case_id"]) == 1
    # 10-03 은 동일 자료: 변경 없음
    r3 = collect(conn, gcc, "2026-10-03")
    assert r3["change_count"] == 0 and r3["new_case_count"] == 0
    assert len(cases(conn)) == 2


def test_field_level_detection(conn):
    data = {
        "d1": ok(rec("A", "가", "P", created="2000-01-01"), rec("P", "상위")),
        "d2": ok(rec("A", "나", "Q", "X", "2001-01-01", "2026-01-01"), rec("P", "상위")),
    }
    gcc = FakeGcc(data)
    collect(conn, gcc, "d1")
    collect(conn, gcc, "d2")
    types = {r["change_type"] for r in conn.execute("SELECT change_type FROM change_event")}
    assert types == {"명칭", "소속", "이전기관", "생성 정보", "폐지 정보"}


def test_disappeared_code_is_not_abolished(conn):
    gcc = FakeGcc({"d1": ok(rec("A", "가"), rec("B", "나")), "d2": ok(rec("A", "가"))})
    collect(conn, gcc, "d1")
    collect(conn, gcc, "d2")
    row = conn.execute("SELECT * FROM change_event").fetchone()
    assert row["change_type"] == "목록에서 사라짐" and row["gcc_code"] == "B"
    assert "폐지" not in row["change_type"]
    assert conn.execute("SELECT COUNT(*) FROM change_event WHERE field_name='abolished_date'"
                        ).fetchone()[0] == 0


def test_failed_and_incomplete_are_not_baseline(conn):
    gcc = FakeGcc({
        "d0": SourceError("접속 실패"),
        "d1": {"complete": False, "records": [rec("A", "가")]},
        "d2": ok(rec("A", "가"), rec("A", "중복")),
        "d3": ok(rec("A", "")),
        "d4": ok(rec("A", "가")),
        "d5": SourceError("또 실패"),
        "d6": ok(rec("A", "가"), rec("B", "나")),
    })
    for d in ("d0", "d1", "d2", "d3"):
        r = collect(conn, gcc, d)
        assert r["status"] in ("실패", "불완전") and r["error"]
        assert r["is_baseline"] is False
    assert conn.execute("SELECT COUNT(*) FROM gcc_record").fetchone()[0] == 0
    # 첫 정상 수집은 실패 이력이 있어도 기준본이 된다 (신설 처리 없음)
    r4 = collect(conn, gcc, "d4")
    assert r4["is_baseline"] is True and r4["change_count"] == 0
    # 실패 후 정상 수집은 직전 '정상' 수집본(d4)과 비교한다
    assert collect(conn, gcc, "d5")["status"] == "실패"
    r6 = collect(conn, gcc, "d6")
    assert r6["change_count"] == 1 and r6["new_case_count"] == 1
    ev = conn.execute("SELECT * FROM change_event").fetchone()
    assert ev["change_type"] == "신설" and ev["gcc_code"] == "B"
    statuses = [r["status"] for r in conn.execute("SELECT status FROM collection_run ORDER BY 1")]
    assert statuses.count("성공") == 2


def test_rerun_same_day_does_not_duplicate(conn):
    gcc = FileGccSource(SAMPLE)
    collect(conn, gcc, "2026-10-01")
    collect(conn, gcc, "2026-10-02")
    r = collect(conn, gcc, "2026-10-02")
    assert r["change_count"] == 0 and r["new_case_count"] == 0
    assert len(cases(conn)) == 2
    assert conn.execute("SELECT COUNT(*) FROM change_event").fetchone()[0] == 3


def test_same_change_after_failed_day_not_duplicated(conn):
    """실패한 날을 사이에 두고 같은 변경이 다시 비교돼도 중복 등록되지 않는다."""
    gcc = FakeGcc({
        "d1": ok(rec("A", "가")), "d2": ok(rec("A", "나")),
        "d3": SourceError("x"), "d4": ok(rec("A", "나")),
    })
    for d in ("d1", "d2", "d3", "d4"):
        collect(conn, gcc, d)
    assert len(cases(conn)) == 1


def test_grouping_by_prev_org_code(conn):
    gcc = FakeGcc({
        "d1": ok(rec("A", "가"), rec("B", "나"), rec("C", "다"), rec("Z", "라")),
        "d2": ok(rec("A", "가", abol="2026-01-01"), rec("B", "나", abol="2026-01-01"),
                 rec("C", "다"), rec("N", "신", prev="A"), rec("M", "신2", prev="B"),
                 rec("Z", "마")),
    })
    collect(conn, gcc, "d1")
    r = collect(conn, gcc, "d2")
    # A-N, B-M 은 각각 연결, Z 는 단독 -> 3건. C 는 변경 없음.
    assert r["new_case_count"] == 3 and r["change_count"] == 5
    assert [c["primary_code"] for c in cases(conn)] == ["M", "N", "Z"]


def test_group_without_new_org_uses_smallest_code(conn):
    gcc = FakeGcc({
        "d1": ok(rec("A", "가"), rec("B", "나", prev="A")),
        "d2": ok(rec("A", "가2"), rec("B", "나2", prev="A")),
    })
    collect(conn, gcc, "d1")
    collect(conn, gcc, "d2")
    (case,) = cases(conn)
    assert case["primary_code"] == "A" and case["title"] == "가2"


def test_change_key_is_stable_and_distinct():
    assert change_key("A", "f", "1", "2") == change_key("A", "f", "1", "2")
    assert change_key("A", "f", "1", "2") != change_key("A", "f", "2", "1")
    assert len(change_key("A", "f", None, None)) == 64
