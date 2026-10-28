import csv
import io
import json

import pytest

from app import auth, export, review
from app.db import connect, init_db
from app.review import ReviewError

HONG, KIM = 1, 2  # 아래 fixture 가 만드는 두 로그인 계정의 user_id


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    assert auth.create_user(c, "hong", "홍길동", "hong-password-1") == HONG
    assert auth.create_user(c, "kim", "김철수", "kim-password-22") == KIM
    # 수집본, 기관 레코드 (가상 데이터)
    c.execute("INSERT INTO collection_run(started_at, base_date, status, is_baseline) "
              "VALUES('2026-10-01 06:00:00','2026-10-01','성공',1)")
    c.execute("INSERT INTO collection_run(started_at, base_date, status) "
              "VALUES('2026-10-02 06:00:00','2026-10-02','성공')")
    recs = [
        (1, "G0001", "가상정부", None, None, "2000-01-01", None),
        (1, "G2000", "가상정보화국", "G0001", None, "2005-03-01", None),
        (2, "G0001", "가상정부", None, None, "2000-01-01", None),
        (2, "G2000", "가상정보화국", "G0001", None, "2005-03-01", "2026-10-02"),
        (2, "G2100", "가상디지털국", "G0001", "G2000", "2026-10-02", None),
    ]
    for r in recs:
        c.execute("INSERT INTO gcc_record(collection_id, gcc_code, org_name, parent_code, "
                  "prev_org_code, created_date, abolished_date) VALUES(?,?,?,?,?,?,?)", r)
    c.execute("INSERT INTO review_case(primary_code, title, change_summary, detected_at) "
              "VALUES('G2100','가상디지털국','가상디지털국 신설','2026-10-02')")
    c.execute("INSERT INTO law_source(case_id, law_name, role, effective_date, fetched_at) "
              "VALUES(1,'가상부 직제','개편 후','2026-10-02','2026-10-02 06:10:00')")
    c.execute("INSERT INTO law_provision(law_source_id, article_ref, text) "
              "VALUES(1,'제5조','디지털국은 정보화 업무를 담당한다.')")
    c.execute("INSERT INTO relation_candidate(case_id, from_code, to_code, from_name, to_name, "
              "relation_type, valid_from, date_status, proposed_json) "
              "VALUES(1,'G2000','G2100','가상정보화국','가상디지털국','승계','2026-10-02','확인','{}')")
    c.execute("INSERT INTO task_move_candidate(case_id, from_code, to_code, from_name, to_name, "
              "task_description, move_scope, effective_date, date_status, proposed_json) "
              "VALUES(1,'G2000','G2100','가상정보화국','가상디지털국','정보화 정책 수립','일부',"
              "'2026-10-02','확인','{}')")
    c.execute("INSERT INTO evidence(candidate_type, candidate_id, provision_id, excerpt, reason) "
              "VALUES('relation',1,1,'발췌','이유')")
    c.execute("INSERT INTO evidence(candidate_type, candidate_id, provision_id, excerpt, reason) "
              "VALUES('task_move',1,1,'발췌','이유')")
    return c


def count(c, table, where="1=1"):
    return c.execute(f"SELECT COUNT(*) n FROM {table} WHERE {where}").fetchone()["n"]


def status(c, table="relation_candidate"):
    return c.execute(f"SELECT review_status FROM {table} WHERE candidate_id=1").fetchone()[0]


def test_approve_creates_confirmed_org_and_log(conn):
    review.decide(conn, "relation", 1, "승인", HONG)
    assert status(conn) == "승인 완료"
    rel = conn.execute("SELECT * FROM confirmed_relation").fetchone()
    assert rel["relation_type"] == "승계"
    assert rel["approved_by"] == HONG and rel["approved_at"]
    assert rel["source_candidate_id"] == 1 and rel["is_current"] == 1
    # 기관 반영: 출발·도착 기관 모두 최신 GCC 수집본 값으로
    names = {r["org_name"]: r for r in conn.execute("SELECT * FROM confirmed_org_info")}
    assert set(names) == {"가상정보화국", "가상디지털국"}
    assert names["가상정보화국"]["valid_to"] == "2026-10-02"
    assert names["가상디지털국"]["valid_from"] == "2026-10-02"
    # 상위기관 org 생성
    assert count(conn, "org_identifier", "id_value='G0001'") == 1
    # 근거 연결 (CSV 로 확인) + 이력
    assert count(conn, "review_log", "action='승인' AND reviewer_id=1") == 1
    assert "가상부 직제 제5조" in export.export_csv(conn)


def test_edit_and_approve_logs_before_after_and_reason(conn):
    review.decide(conn, "relation", 1, "수정 후 승인", HONG, reason="적용일 정정",
                  edits={"valid_from": "2026-10-05", "relation_type": "승계",
                         "valid_to": "", "date_status": "확인"})
    rel = conn.execute("SELECT * FROM confirmed_relation").fetchone()
    assert rel["valid_from"] == "2026-10-05"
    log = conn.execute("SELECT * FROM review_log").fetchone()
    assert log["action"] == "수정 후 승인" and log["reason"] == "적용일 정정"
    assert json.loads(log["before_value"])["valid_from"] == "2026-10-02"
    assert json.loads(log["after_value"])["valid_from"] == "2026-10-05"
    assert conn.execute("SELECT draft_json FROM relation_candidate").fetchone()[0] is None


def test_edit_validation(conn):
    bad = [
        dict(reason="", edits={"valid_from": "2026-10-05"}),                 # 사유 없음
        dict(reason="x", edits=None),                                        # 수정 없음
        dict(reason="x", edits={"valid_from": "2026/10/05"}),                # 날짜 형식
        dict(reason="x", edits={"valid_from": "2026-02-31"}),                # 없는 날짜
        dict(reason="x", edits={"date_status": "모름"}),                     # 허용값 아님
        dict(reason="x", edits={"valid_from": "2026-10-02"}),                # 제안값과 동일
        dict(reason="x", edits={"valid_from": "", "date_status": "확인"}),   # 확인인데 날짜 없음
        dict(reason="x", edits={"id": 3}),                                   # 수정 불가 항목
    ]
    for kw in bad:
        with pytest.raises(ReviewError):
            review.decide(conn, "relation", 1, "수정 후 승인", HONG, **kw)
    assert status(conn) == "승인 대기" and count(conn, "confirmed_relation") == 0
    for bad_user in (None, 999):  # 로그인 계정이 아니면 처리할 수 없다
        with pytest.raises(ReviewError):
            review.decide(conn, "relation", 1, "승인", bad_user)


def test_hold_and_reject_keep_log_but_no_confirmed(conn):
    review.decide(conn, "relation", 1, "보류", HONG, reason="근거 확인 필요")
    assert status(conn) == "보류"
    with pytest.raises(ReviewError):
        review.decide(conn, "task_move", 1, "기각", HONG)  # 기각 사유 필수
    review.decide(conn, "task_move", 1, "기각", HONG, reason="근거 부족")
    assert status(conn, "task_move_candidate") == "기각"
    assert count(conn, "review_log") == 2
    for t in ("confirmed_relation", "confirmed_task_move", "confirmed_org_info"):
        assert count(conn, t) == 0
    assert export.export_csv(conn).strip().count("\n") == 0  # 헤더만
    # 보류 후보는 다시 승인할 수 있다
    review.decide(conn, "relation", 1, "승인", HONG)
    assert status(conn) == "승인 완료" and count(conn, "confirmed_relation") == 1


def test_failure_rolls_back_everything_and_keeps_draft(conn, monkeypatch):
    original = review._write_review_log

    def boom(*a, **k):
        raise RuntimeError("로그 기록 실패")

    monkeypatch.setattr(review, "_write_review_log", boom)
    with pytest.raises(RuntimeError):
        review.decide(conn, "relation", 1, "수정 후 승인", HONG, reason="정정",
                      edits={"valid_from": "2026-10-05", "date_status": "확인"})
    assert status(conn) == "승인 대기"
    for t in ("confirmed_relation", "confirmed_org_info", "review_log", "org", "org_identifier"):
        assert count(conn, t) == 0, t
    row = conn.execute("SELECT * FROM relation_candidate").fetchone()
    assert review.candidate_values(row)["valid_from"] == "2026-10-05"  # 수정 입력 보존

    monkeypatch.setattr(review, "_write_review_log", original)
    review.decide(conn, "relation", 1, "수정 후 승인", HONG, reason="정정",
                  edits={"valid_from": "2026-10-05", "date_status": "확인"})
    assert status(conn) == "승인 완료"
    assert conn.execute("SELECT valid_from FROM confirmed_relation").fetchone()[0] == "2026-10-05"


def test_double_approval_blocked(conn):
    review.decide(conn, "relation", 1, "승인", HONG)
    with pytest.raises(ReviewError):
        review.decide(conn, "relation", 1, "승인", KIM)
    with pytest.raises(ReviewError):
        review.decide(conn, "relation", 1, "기각", KIM, reason="x")
    assert count(conn, "confirmed_relation") == 1 and count(conn, "review_log") == 1


def test_past_org_info_preserved(conn):
    review.decide(conn, "relation", 1, "승인", HONG)
    old_id = conn.execute(
        "SELECT confirmed_id FROM confirmed_org_info WHERE org_name='가상정보화국'").fetchone()[0]
    # 이후 수집본에서 명칭이 바뀐 상황: 다음 승인 때 기존 행을 지우지 않고 새 행으로 갱신
    conn.execute("INSERT INTO collection_run(started_at, base_date, status) "
                 "VALUES('2026-10-03 06:00:00','2026-10-03','성공')")
    conn.execute("INSERT INTO gcc_record(collection_id, gcc_code, org_name, parent_code, "
                 "created_date, abolished_date) VALUES(3,'G2000','가상정보화국(종료)','G0001',"
                 "'2005-03-01','2026-10-02')")
    review.decide(conn, "task_move", 1, "승인", HONG)
    rows = conn.execute("SELECT * FROM confirmed_org_info WHERE org_id="
                        "(SELECT org_id FROM org_identifier WHERE id_value='G2000') "
                        "ORDER BY confirmed_id").fetchall()
    assert len(rows) == 2
    assert rows[0]["confirmed_id"] == old_id and rows[0]["is_current"] == 0
    assert rows[0]["org_name"] == "가상정보화국"  # 과거 값 보존
    assert rows[1]["is_current"] == 1 and rows[1]["supersedes_id"] == old_id
    assert rows[1]["org_name"] == "가상정보화국(종료)"


def test_unknown_date_approved_without_date(conn):
    conn.execute("UPDATE relation_candidate SET date_status='미확인'")
    review.decide(conn, "relation", 1, "승인", HONG)
    rel = conn.execute("SELECT * FROM confirmed_relation").fetchone()
    assert rel["date_status"] == "미확인" and rel["valid_from"] is None


def test_relation_and_task_move_decided_separately(conn):
    review.decide(conn, "relation", 1, "승인", HONG)
    assert review.case_review_state(conn, 1) == "승인 대기"
    review.decide(conn, "task_move", 1, "기각", HONG, reason="근거 부족")
    assert review.case_review_state(conn, 1) == "승인 완료"
    assert count(conn, "confirmed_relation") == 1 and count(conn, "confirmed_task_move") == 0
    conn.execute("INSERT INTO review_case(primary_code, title, change_summary, detected_at) "
                 "VALUES('X','x','x','2026-10-02')")
    assert review.case_review_state(conn, 2) == "후보 없음"


def test_correction_keeps_history_and_rolls_back_on_failure(conn, monkeypatch):
    review.decide(conn, "relation", 1, "승인", HONG)
    with pytest.raises(ReviewError):
        review.correct(conn, "relation", 1, {"relation_type": "개칭"}, "", KIM)
    with pytest.raises(ReviewError):
        review.correct(conn, "relation", 1, {"relation_type": "승계"}, "사유", KIM)  # 변경 없음

    original = review._write_correction_log
    monkeypatch.setattr(review, "_write_correction_log",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("실패")))
    with pytest.raises(RuntimeError):
        review.correct(conn, "relation", 1, {"relation_type": "개칭"}, "명칭 오기", KIM)
    assert count(conn, "confirmed_relation") == 1 and count(conn, "correction_log") == 0
    assert conn.execute("SELECT is_current FROM confirmed_relation").fetchone()[0] == 1

    monkeypatch.setattr(review, "_write_correction_log", original)
    new_id = review.correct(conn, "relation", 1, {"relation_type": "개칭"}, "명칭 오기", KIM)
    rows = conn.execute("SELECT * FROM confirmed_relation ORDER BY confirmed_id").fetchall()
    assert len(rows) == 2
    assert rows[0]["is_current"] == 0 and rows[0]["relation_type"] == "승계"
    assert rows[1]["confirmed_id"] == new_id and rows[1]["is_current"] == 1
    assert rows[1]["supersedes_id"] == 1 and rows[1]["relation_type"] == "개칭"
    log = conn.execute("SELECT * FROM correction_log").fetchone()
    assert log["reason"] == "명칭 오기" and log["corrected_by"] == KIM and log["corrected_at"]
    assert json.loads(log["old_value"])["relation_type"] != "개칭"
    assert json.loads(log["new_value"])["relation_type"] == "개칭"
    # 이미 대체된 행은 다시 정정할 수 없다
    with pytest.raises(ReviewError):
        review.correct(conn, "relation", 1, {"relation_type": "분리"}, "사유", KIM)
    # CSV 는 정정된 값만
    out = list(csv.DictReader(io.StringIO(export.export_csv(conn))))
    assert len(out) == 1 and out[0]["관계유형"] == "개칭"


def test_correct_org_info_not_overwritten_by_later_approval(conn):
    review.decide(conn, "relation", 1, "승인", HONG)
    oid = conn.execute("SELECT confirmed_id FROM confirmed_org_info WHERE org_name='가상정보화국'"
                       ).fetchone()[0]
    review.correct(conn, "org_info", oid, {"org_name": "가상정보화국(정정)"}, "명칭 정정", KIM)
    review.decide(conn, "task_move", 1, "승인", HONG)
    cur = conn.execute("SELECT org_name FROM confirmed_org_info WHERE is_current=1 AND org_id="
                       "(SELECT org_id FROM org_identifier WHERE id_value='G2000')").fetchall()
    assert [r[0] for r in cur] == ["가상정보화국(정정)"]


def test_csv_only_current_approved(conn):
    review.decide(conn, "relation", 1, "승인", HONG)
    review.decide(conn, "task_move", 1, "보류", HONG)
    rows = list(csv.DictReader(io.StringIO(export.export_csv(conn))))
    assert [r["구분"] for r in rows] == ["기관관계"]
    assert rows[0]["출발기관"] == "가상정보화국" and rows[0]["도착기관"] == "가상디지털국"
    assert rows[0]["승인자"] == "홍길동" and rows[0]["근거"] == "가상부 직제 제5조"
    review.decide(conn, "task_move", 1, "승인", HONG)
    rows = list(csv.DictReader(io.StringIO(export.export_csv(conn))))
    assert [r["구분"] for r in rows] == ["기관관계", "업무이동"]
    assert rows[1]["업무내용"] == "정보화 정책 수립" and rows[1]["이동범위"] == "일부"
    assert list(rows[0].keys()) == export.HEADER


def test_inactive_account_cannot_decide_or_correct(conn):
    review.decide(conn, "relation", 1, "승인", HONG)
    auth.deactivate_user(conn, "kim")
    with pytest.raises(ReviewError):
        review.decide(conn, "task_move", 1, "승인", KIM)
    with pytest.raises(ReviewError):
        review.correct(conn, "relation", 1, {"relation_type": "개칭"}, "사유", KIM)
    assert status(conn, "task_move_candidate") == "승인 대기" and count(conn, "confirmed_task_move") == 0
    # 비활성 계정의 과거 기록은 보존된다 (계정은 삭제하지 않는다)
    review.decide(conn, "task_move", 1, "승인", HONG)
    assert count(conn, "app_user") == 2


def test_two_accounts_are_distinguished_in_records_and_csv(conn):
    review.decide(conn, "relation", 1, "승인", HONG)
    review.decide(conn, "task_move", 1, "승인", KIM)
    new_id = review.correct(conn, "relation", 1, {"relation_type": "개칭"}, "명칭 오기", KIM)
    logs = {r["candidate_type"]: r["reviewer_id"] for r in conn.execute("SELECT * FROM review_log")}
    assert logs == {"relation": HONG, "task_move": KIM}
    assert conn.execute("SELECT approved_by FROM confirmed_task_move").fetchone()[0] == KIM
    assert conn.execute("SELECT corrected_by FROM correction_log").fetchone()[0] == KIM
    # 정정한 사람은 정정 이력에 남고, 최초 승인자는 그대로다.
    assert conn.execute("SELECT approved_by FROM confirmed_relation WHERE confirmed_id=?",
                        (new_id,)).fetchone()[0] == HONG
    rows = {r["구분"]: r["승인자"] for r in csv.DictReader(io.StringIO(export.export_csv(conn)))}
    assert rows == {"기관관계": "홍길동", "업무이동": "김철수"}


def test_failed_approval_by_logged_in_user_rolls_back_and_retries(conn, monkeypatch):
    original = review._write_review_log
    monkeypatch.setattr(review, "_write_review_log",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("이력 기록 실패")))
    with pytest.raises(RuntimeError):
        review.decide(conn, "task_move", 1, "수정 후 승인", KIM, reason="범위 정정",
                      edits={"move_scope": "전체"})
    assert status(conn, "task_move_candidate") == "승인 대기"
    for t in ("confirmed_task_move", "confirmed_org_info", "review_log"):
        assert count(conn, t) == 0, t
    row = conn.execute("SELECT * FROM task_move_candidate").fetchone()
    assert review.candidate_values(row)["move_scope"] == "전체"  # 수정 입력 보존
    monkeypatch.setattr(review, "_write_review_log", original)
    review.decide(conn, "task_move", 1, "수정 후 승인", KIM, reason="범위 정정", edits={"move_scope": "전체"})
    assert conn.execute("SELECT approved_by, move_scope FROM confirmed_task_move").fetchone()[:] == (KIM, "전체")
    assert conn.execute("SELECT reviewer_id FROM review_log").fetchone()[0] == KIM
