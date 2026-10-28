"""화면 e2e 시험: 임시 DB + sample_data(가상 예시)로 수집 -> 승인 -> CSV 까지."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import web
from app.db import connect

from .web_helpers import login, make_users, post


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "t.db"))
    web._initialized.clear()
    web._login_failures.clear()
    make_users()
    with TestClient(web.app) as c:
        assert login(c, "hong").status_code == 303  # 모든 화면은 로그인 후에만 열린다
        yield c


def run_day(client, day):
    r = post(client, "/settings/run", {"day": day})
    assert r.status_code == 200
    return r


def case_ids(client, status="전체"):
    html = client.get("/", params={"status": status}).text
    return [int(i) for i in re.findall(r'href="/cases/(\d+)"', html)]


def decide(client, case_id, kind, cid, **form):
    return post(client, f"/cases/{case_id}/candidates/{kind}/{cid}/decide", form)


def first_candidate(case_html):
    m = re.search(r'id="c-(relation|task_move)-(\d+)"', case_html)
    return m.group(1), int(m.group(2))


def test_empty_state_and_pages_open(client):
    html = client.get("/").text
    assert "새로운 변경사항이 없습니다" in html
    assert "수집 기록 없음" in html
    assert client.get("/settings").status_code == 200
    assert client.get("/cases/999").status_code == 404


def test_baseline_then_changes_listed(client):
    run_day(client, "2026-10-01")
    html = client.get("/").text
    assert "비교 기준본" in html and "새로운 변경사항이 없습니다" in html  # 전체 신설 처리 없음
    run_day(client, "2026-10-02")
    assert len(case_ids(client, "승인 대기")) == 2
    page = client.get("/").text
    assert "승인 대기 건수" in page and "2건" in page
    # 자료 부족 건은 배지로 정상 건과 구분된다.
    assert "자료 부족" in page


def test_no_change_message_after_repeat(client):
    run_day(client, "2026-10-01")
    run_day(client, "2026-10-02")
    run_day(client, "2026-10-03")  # 10-02 와 같은 자료
    assert len(case_ids(client)) == 2  # 중복 생성 없음
    assert "변경 없음" in client.get("/").text


def test_detail_shows_changes_evidence_and_editable_values(client):
    run_day(client, "2026-10-01")
    run_day(client, "2026-10-02")
    cid = case_ids(client)[0]
    html = client.get(f"/cases/{cid}").text
    for text in ("변경 전·후", "AI 분석·근거", "반영 예정 내용", "조항 원문 보기", "승인", "수정 후 승인", "보류", "기각"):
        assert text in html
    assert "<details>" in html


def test_approve_updates_filters_and_csv(client):
    run_day(client, "2026-10-01")
    run_day(client, "2026-10-02")
    assert "구분" in client.get("/export.csv").text.lstrip("﻿") and len(
        client.get("/export.csv").text.strip().splitlines()) == 1  # 승인 전에는 헤더만

    for cid in case_ids(client, "승인 대기"):
        html = client.get(f"/cases/{cid}").text
        for kind, cand in re.findall(r'id="c-(relation|task_move)-(\d+)"', html):
            r = decide(client, cid, kind, int(cand), action="승인")
            assert "처리했습니다" in r.text

    assert "새로운 변경사항이 없습니다" in client.get("/", params={"status": "승인 대기"}).text
    assert len(case_ids(client, "승인 완료")) == 2
    csv_res = client.get("/export.csv")
    assert csv_res.headers["content-disposition"].startswith("attachment")
    assert csv_res.content.startswith("﻿".encode("utf-8"))
    body = csv_res.content.decode("utf-8-sig")
    assert "홍길동" in body and len(body.strip().splitlines()) > 1
    # 처리 이력에는 로그인 계정의 표시 이름이 나온다.
    cid = case_ids(client)[0]
    assert "홍길동" in client.get(f"/cases/{cid}").text


def test_hold_and_reject_not_in_csv(client):
    run_day(client, "2026-10-01")
    run_day(client, "2026-10-02")
    cid = case_ids(client)[0]
    kind, cand = first_candidate(client.get(f"/cases/{cid}").text)
    decide(client, cid, kind, cand, action="보류")
    assert cid in case_ids(client, "보류")
    r = decide(client, cid, kind, cand, action="기각", reason="근거 부족")  # 보류는 다시 처리 가능
    assert "처리했습니다" in r.text
    assert len(client.get("/export.csv").text.strip().splitlines()) == 1


def test_failed_edit_shows_error_and_keeps_pending(client):
    run_day(client, "2026-10-01")
    run_day(client, "2026-10-02")
    cid = case_ids(client)[0]
    html = client.get(f"/cases/{cid}").text
    kind, cand = first_candidate(html)
    field = "relation_type" if kind == "relation" else "task_description"
    r = decide(client, cid, kind, cand, action="수정 후 승인", **{field: "수정한 값"})  # 사유 없음
    assert "사유" in r.text and "alert" in r.text
    conn = connect()
    status = conn.execute(
        f"SELECT review_status FROM {'relation_candidate' if kind == 'relation' else 'task_move_candidate'} "
        "WHERE candidate_id=?", (cand,)).fetchone()[0]
    conn.close()
    assert status == "승인 대기"


def test_edit_and_approve_saves_modified_value(client):
    run_day(client, "2026-10-01")
    run_day(client, "2026-10-02")
    for cid in case_ids(client):
        html = client.get(f"/cases/{cid}").text
        for kind, cand in re.findall(r'id="c-(relation|task_move)-(\d+)"', html):
            if kind == "relation":
                r = decide(client, cid, kind, int(cand), action="수정 후 승인",
                           reason="유형 정정", relation_type="정정된유형", valid_from="", valid_to="",
                           date_status="미확인")
                assert "처리했습니다" in r.text
                body = client.get("/export.csv").content.decode("utf-8-sig")
                assert "정정된유형" in body
                conn = connect()
                log = conn.execute("SELECT * FROM review_log WHERE action='수정 후 승인'").fetchone()
                conn.close()
                assert log["reason"] == "유형 정정"
                return
    pytest.fail("기관관계 후보가 없습니다")


def test_settings_save_upload_and_run(client, tmp_path):
    r = post(client, "/settings", {"schedule_time": "25:99", "gcc_dir": "a", "law_dir": "b", "llm_dir": "c"})
    assert "HH:MM" in r.text
    gcc_dir = tmp_path / "gcc"
    r = post(client, "/settings", {"schedule_time": "06:30", "gcc_dir": str(gcc_dir),
                                   "law_dir": "sample_data/law", "llm_dir": "sample_data/llm"})
    assert "저장했습니다" in r.text and "06:30" in r.text
    bad = post(client, "/settings/upload-gcc", {"day": "2026-10-01"},
               files={"file": ("x.json", b"not json", "application/json")})
    assert "형식 오류" in bad.text
    good = {"complete": True, "records": [{"gcc_code": "X1", "org_name": "가상기관", "parent_code": None,
                                           "prev_org_code": None, "created_date": None, "abolished_date": None}]}
    ok = post(client, "/settings/upload-gcc", {"day": "2026-10-01"},
              files={"file": ("x.json", json.dumps(good).encode(), "application/json")})
    assert "저장했습니다" in ok.text and (gcc_dir / "2026-10-01.json").exists()
    assert "비교 기준본" in run_day(client, "2026-10-01").text


def test_collection_failure_banner(client):
    r = run_day(client, "2030-01-01")  # 자료 없음 -> 수집 실패
    assert "실패" in r.text
    html = client.get("/").text
    assert "GCC 수집 실패" in html and "마지막 정상 수집본" in html
