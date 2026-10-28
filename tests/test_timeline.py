"""계보도 타임라인 탭(읽기 전용) 시험. 작은 픽스처는 SQL 로 직접 만들고, 실제 엑셀이 있으면 가져온 뒤 확인한다."""
import re

import pytest
from fastapi.testclient import TestClient

from app import adapters, daily, db, review, timeline, web
from app.db import ROOT, connect, init_db

from .test_lineage import (XLSX, cand_move, confirmed_move, env, make_case, org, ref_rel, ref_record,  # noqa: F401
                           ref_work, tables_count)
from .web_helpers import login, make_users

needs_xlsx = pytest.mark.skipif(not XLSX.exists(), reason="환경부 엑셀 파일이 폴더에 없음")


def tl(client, case_id, **params):
    r = client.get(f"/cases/{case_id}/timeline", params=params)
    assert r.status_code == 200
    return r.text


def n_arrows(html):
    return len(re.findall(r'data-tl-arrow="1"', html))


def n_own(html):
    return len(re.findall(r'data-own="1"', html))


def two_orgs(conn):
    a = org(conn, "가상정보화국", gcc="A100", common="ENV-T-A")
    b = org(conn, "가상디지털국", gcc="B100", common="ENV-T-B")
    ref_record(conn, "S-A", a, "A100", "2000-01-01", "2010-06-01")
    ref_record(conn, "S-B", b, "B100", "2010-06-01", None)
    ref_rel(conn, "R-1", "SUCCEEDS", b, a, layer="CANONICAL_RELATION", status="CONFIRMED_CROSS_SOURCE")
    return a, b


# ---------------------------------------------------------------- 기본 규칙
def test_login_required(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "x.db"))
    web._initialized.clear()
    make_users()
    with TestClient(web.app) as anon:
        r = anon.get("/cases/1/timeline", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_detail_has_two_tabs_and_relation_tab_unchanged(env):
    c, conn, _ = env
    a, _b = two_orgs(conn)
    cid = make_case(conn, "A100")
    page = c.get(f"/cases/{cid}").text
    assert f'data-tab-url="/cases/{cid}/timeline"' in page and f'data-tab-url="/cases/{cid}/lineage"' in page
    assert f'data-url="/cases/{cid}/timeline"' in page            # 기본 탭 = 타임라인
    assert ">타임라인<" in page and ">관계도<" in page
    rel = c.get(f"/cases/{cid}/lineage", params={"org": a}).text
    assert 'class="lin-svgwrap"' in rel and "범례" in rel          # 관계도는 기존 내용 그대로
    assert c.get("/cases/9999/timeline").status_code == 404


def test_read_only(env):
    c, conn, ids = env
    a, b = two_orgs(conn)
    confirmed_move(conn, a, b, ids["hong"], date="2010-06-01")
    cid = make_case(conn, "A100")
    before = tables_count(conn)
    tl(c, cid, org=a)
    tl(c, cid, org=a, fn=timeline.ORG_MODE)
    assert tables_count(conn) == before


def test_orange_only_for_dated_current_confirmed_move(env):
    c, conn, ids = env
    a, b = two_orgs(conn)
    confirmed_move(conn, a, b, ids["hong"], date="2010-06-01")
    confirmed_move(conn, b, a, ids["hong"], current=0, date="2012-01-01")   # 정정으로 비유효가 된 행
    cid = make_case(conn, "A100")
    cand_move(conn, cid, "A100", "B100", date="2011-01-01", dstat="확인")    # 후보는 칠하지 않는다
    ref_work(conn, "W-1", a, "청사 정보시스템 운영")                           # 원천 업무도 칠하지 않는다
    t = timeline.build(conn, org_id=a, fn="청사 정보시스템 운영")
    own = {r["name"]: r["orange"] for r in t["rows"]}
    assert len(own["가상디지털국"]) == 1 and own["가상정보화국"] == []
    assert own["가상디지털국"][0]["start"].isoformat() == "2010-06-01" and own["가상디지털국"][0]["end"] is None
    html = tl(c, cid, org=a, fn="청사 정보시스템 운영")
    assert n_own(html) == 1 and n_arrows(html) == 1
    assert "이관 전 담당(시작 미확인)" in html and "담당 종료 미확인" in html


def test_undated_confirmed_move_not_on_axis(env):
    c, conn, ids = env
    a, b = two_orgs(conn)
    confirmed_move(conn, a, b, ids["hong"], date=None, dstat="미확인")
    confirmed_move(conn, a, b, ids["hong"], text="다른 업무", date="2009-01-01", dstat="미확인")  # 값이 있어도 미확인이면 제외
    cid = make_case(conn, "A100")
    html = tl(c, cid, org=a, fn=timeline.ORG_MODE)
    assert n_own(html) == 0 and n_arrows(html) == 0
    assert "승인됐지만 날짜 미확인이라 시간축에 표시하지 않음(2건)" in html
    t = timeline.build(conn, org_id=a, fn=timeline.ORG_MODE)
    moved = [e for e in t["events"] if e["type"] == "업무 이관"]
    assert len(moved) == 2 and all(e["date_text"] == "날짜 미확인" for e in moved)
    assert t["events"][-1]["date"] is None                               # 날짜 미확인은 맨 아래


def test_arrow_count_equals_dated_moves_and_chain(env):
    c, conn, ids = env
    a, b = two_orgs(conn)
    cc = org(conn, "가상정보센터", gcc="C100")
    ref_record(conn, "S-C", cc, "C100", "2015-03-01", None)
    confirmed_move(conn, a, b, ids["hong"], date="2010-06-01")
    confirmed_move(conn, b, cc, ids["kim"], date="2015-03-01")
    cid = make_case(conn, "B100")
    t = timeline.build(conn, org_id=b)
    assert t["fn"] == "청사 정보시스템 운영"
    own = {r["name"]: r["orange"] for r in t["rows"]}
    assert own["가상디지털국"][0]["end"].isoformat() == "2015-03-01"     # 다음 승인 이동까지
    assert own["가상정보센터"][0]["end"] is None
    pre = {r["name"]: r["pre"] for r in t["rows"]}
    assert len(pre["가상정보화국"]) == 1 and pre["가상디지털국"] == []      # 이미 담당이 확인된 구간에는 표식 없음
    assert n_arrows(tl(c, cid, org=b)) == 2


def test_rename_dashed_in_same_row(env):
    c, conn, _ = env
    a = org(conn, "새이름과", gcc="R100", common="ENV-T-R")
    ref_record(conn, "S-R1", a, "R100", "2000-01-01", "2009-12-31")
    conn.execute("UPDATE ref_org_record SET full_name='옛이름과' WHERE source_row_id='S-R1'")
    ref_record(conn, "S-R2", a, "R101", "2010-01-01", None)
    conn.execute("UPDATE ref_org_record SET full_name='새이름과' WHERE source_row_id='S-R2'")
    cid = make_case(conn, "R100")
    t = timeline.build(conn, org_id=a)
    assert len(t["rows"]) == 1 and len(t["rows"][0]["segs"]) == 2 and t["rows"][0]["id_label"] == "ENV-T-R"
    html = tl(c, cid, org=a)
    assert len(re.findall(r'data-rename="1"', html)) == 1
    assert "옛이름과 → 새이름과 (같은 조직ID ENV-T-R 유지" in html


def test_unknown_dates_marked_not_invented(env):
    c, conn, _ = env
    a = org(conn, "날짜없는과", gcc="N100")
    ref_record(conn, "S-N", a, "N100", None, None)
    b = org(conn, "끝모르는과", gcc="E100")
    ref_record(conn, "S-E", b, "E100", "2001-05-05", None)
    ref_rel(conn, "R-NE", "SUCCEEDS", b, a, layer="CANONICAL_RELATION", status="NEEDS_REVIEW")
    cid = make_case(conn, "N100")
    t = timeline.build(conn, org_id=a)
    rows = {r["name"]: r for r in t["rows"]}
    assert rows["날짜없는과"]["note"] == "존속기간 미확인"
    html = tl(c, cid, org=a)
    assert "종료 미확인" in html and "존속기간 미확인" in html
    assert t["slider"] == {"min": 2000, "max": 2002, "value": 2001}     # 있는 날짜(2001)만 사용
    dates = {e["date_text"] for e in t["events"]}
    assert dates <= {"2001-05-05", "날짜 미확인"}
    assert "폐지 확정" not in html.replace("폐지 확정 아님", "")


def test_source_work_marker_without_orange(env):
    c, conn, _ = env
    a = org(conn, "업무만있는과", gcc="W100")
    ref_record(conn, "S-W", a, "W100", "1995-01-01", "2005-01-01")
    ref_work(conn, "W-1", a, "자연환경보전에 관한 기본정책의 수립")
    ref_work(conn, "W-2", a, "전국 생태축의 구축에 관한 사항")
    cid = make_case(conn, "W100")
    html = tl(c, cid, org=a)
    assert "원천 업무 기재 2건 · 담당기간 미확인" in html and n_own(html) == 0
    assert "[원천 기재] 자연환경보전에 관한 기본정책의 수립" in html
    assert "확인된 담당 조직 없음(담당기간 미확인)" in html


def test_event_layers_and_canonical_not_our_approval(env):
    c, conn, ids = env
    a, b = two_orgs(conn)
    confirmed_move(conn, a, b, ids["hong"], date="2010-06-01")
    cid = make_case(conn, "A100")
    cand_move(conn, cid, "A100", "B100")
    t = timeline.build(conn, org_id=a, fn=timeline.ORG_MODE)
    layers = {e["type"]: e["layer"] for e in t["events"]}
    assert layers["승계 관계"] == "원천 기재" and layers["업무 이동 후보"] == "검토 후보" and layers["업무 이관"] == "승인 결과"
    succ = next(e for e in t["events"] if e["type"] == "승계 관계")
    assert "가상정보화국 → 가상디지털국" in succ["desc"] and "우리 승인 아님" in succ["desc"]
    assert succ["date_text"] == "날짜 미확인"
    html = tl(c, cid, org=a, fn=timeline.ORG_MODE)
    for badge in ("원천 기재", "검토 후보", "승인 결과"):
        assert f">{badge}</span>" in html


def test_slider_range_default_and_holder(env):
    c, conn, ids = env
    a, b = two_orgs(conn)
    confirmed_move(conn, a, b, ids["kim"], date="2010-06-01")
    cid = make_case(conn, "A100")
    t = timeline.build(conn, org_id=a)
    assert t["slider"] == {"min": 1999, "max": 2011, "value": 2010}
    assert t["holders"]["2005"] == []
    h = t["holders"]["2010"]
    assert [x["name"] for x in h] == ["가상디지털국"] and h[0]["by"] == "김철수" and h[0]["id"] == "ENV-T-B"
    assert t["holders"]["2011"][0]["name"] == "가상디지털국"             # 종료 미확인이면 이후도 담당
    html = tl(c, cid, org=a)
    assert 'id="tl-year"' in html and 'value="2010"' in html and "가상디지털국 (ENV-T-B)" in html
    assert '<script type="application/json" id="tl-data">' in html and '"2005": []' in html
    # 기준 연도 이후 사건은 흐리게
    assert re.search(r'data-year="2010" class="\s*"', html) or 'data-year="2010" class=""' in html


def test_function_default_order(env):
    c, conn, ids = env
    a, b = two_orgs(conn)
    ref_work(conn, "W-1", a, "원천 업무 첫 항목")
    assert timeline.build(conn, org_id=a)["fn"] == "원천 업무 첫 항목"
    cid = make_case(conn, "A100")
    cand_move(conn, cid, "A100", "B100")
    assert timeline.build(conn, org_id=a)["fn"] == "후보 업무"
    confirmed_move(conn, a, b, ids["hong"], text="승인된 업무", date="2010-06-01")
    t = timeline.build(conn, org_id=a)
    assert t["fn"] == "승인된 업무" and [f["src"] for f in t["functions"]] == ["승인 결과", "검토 후보", "원천 기재"]
    assert timeline.build(conn, org_id=a, fn=timeline.ORG_MODE)["fn"] is None
    assert timeline.build(conn, org_id=a, fn="없는 기능")["fn"] == "승인된 업무"
    lone = org(conn, "아무것도없는과", gcc="Z100")
    assert timeline.build(conn, org_id=lone)["fn"] is None


# ---------------------------------------------------------------- 가상 샘플·실제 엑셀·시연
def test_sample_g2100_approval_creates_orange_and_arrow(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "s.db"))
    web._initialized.clear()
    web._login_failures.clear()
    ids = make_users()
    conn = connect()
    init_db(conn)
    sample = ROOT / "sample_data"
    gcc, law, llm = (adapters.FileGccSource(str(sample / "gcc")), adapters.FileLawSource(str(sample / "law")),
                     adapters.FileLlmClient(str(sample / "llm")))
    for day in ("2026-10-01", "2026-10-02"):
        daily.run(conn, day, gcc, law, llm)
    case_id = conn.execute("SELECT case_id FROM review_case WHERE primary_code='G2100'").fetchone()[0]
    cand = conn.execute("SELECT candidate_id FROM task_move_candidate WHERE case_id=?", (case_id,)).fetchone()[0]
    with TestClient(web.app) as c:
        assert login(c, "hong").status_code == 303
        before = tl(c, case_id)
        assert n_own(before) == 0 and n_arrows(before) == 0
        review.decide(conn, "task_move", cand, "승인", ids["hong"])
        html = tl(c, case_id)
    assert n_own(html) == 1 and n_arrows(html) == 1
    assert "청사 정보시스템 운영" in html and "가상디지털국" in html
    conn.close()


@needs_xlsx
def test_real_excel_nature_policy_timeline(env):
    from app import import_xlsx

    c, conn, _ = env
    import_xlsx.run_import(conn, XLSX)
    oid = conn.execute("SELECT org_id FROM org_identifier WHERE id_type='COMMON' AND id_value='ENV-ORG-01238'").fetchone()[0]
    cid = make_case(conn, "NOPE-CODE")
    before = tables_count(conn)
    html = tl(c, cid, org=oid)
    assert tables_count(conn) == before
    assert n_own(html) == 0 and n_arrows(html) == 0
    assert "원천 업무 기재 11건 · 담당기간 미확인" in html
    assert "자연정책과" in html and "확인된 담당 조직 없음(담당기간 미확인)" in html
    assert "우리 승인 아님" in html


@needs_xlsx
def test_demo_seed_approved_undated_moves(tmp_path, monkeypatch):
    from app import demo_seed

    path = tmp_path / "demo.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    demo_seed.seed(conn, XLSX, approve=True, password="demo-pass-1234")
    conn.close()
    monkeypatch.setenv("APP_DB", str(path))
    web._initialized.clear()
    web._login_failures.clear()
    with TestClient(web.app) as c:
        assert login(c, "demo", "demo-pass-1234").status_code == 303
        html = c.get("/cases/1/timeline", params={"fn": timeline.ORG_MODE}).text
        rel = c.get("/cases/1/lineage").text
    assert n_arrows(html) == 0 and "승인됐지만 날짜 미확인이라 시간축에 표시하지 않음(2건)" in html
    assert len(re.findall(r'data-arrow="1"', rel)) == 2
