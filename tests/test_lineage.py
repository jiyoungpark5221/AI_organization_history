"""기능 담당 조직 계보도(읽기 전용) 시험. 작은 픽스처는 SQL 로 직접 만들고, 실제 엑셀이 있으면 가져온 뒤 확인한다."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import lineage, web
from app.db import connect, init_db, now

from .web_helpers import login, make_users

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "환경부_GCC_CAMS_기관관계업무_마스터_Pilot_v03_원자료보완.xlsx"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "t.db"))
    web._initialized.clear()
    web._login_failures.clear()
    ids = make_users()
    with TestClient(web.app) as c:
        assert login(c, "hong").status_code == 303
        conn = connect()
        init_db(conn)
        yield c, conn, ids
        conn.close()


# ---------------------------------------------------------------- 픽스처 도우미
def org(conn, name, gcc=None, common=None, og=None):
    oid = conn.execute("INSERT INTO org(org_name) VALUES(?)", (name,)).lastrowid
    for t, v in (("GCC", gcc), ("COMMON", common), ("OG", og)):
        if v:
            conn.execute("INSERT INTO org_identifier(org_id,id_type,id_value) VALUES(?,?,?)", (oid, t, v))
    return oid


def import_id(conn):
    row = conn.execute("SELECT import_id FROM import_run LIMIT 1").fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO import_run(file_name,file_sha256,imported_at,report_json) VALUES('t.xlsx','x',?,'{}')",
                        (now(),)).lastrowid


def ref_rel(conn, rid, typ, frm, to, layer="SOURCE_LINK", status="SOURCE_EXPLICIT", obs=(None, None), valid=(None, None)):
    conn.execute(
        "INSERT INTO ref_org_relation(relation_id,import_id,layer,relation_type,from_org_id,to_org_id,observed_from,observed_to,"
        "relation_valid_from,relation_valid_to,review_status_src,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, import_id(conn), layer, typ, frm, to, obs[0], obs[1], valid[0], valid[1], status, "시험 근거"))


def ref_work(conn, wid, oid, text, og_raw="1480041"):
    conn.execute(
        "INSERT INTO ref_org_work(work_id,import_id,org_id,og_code_raw,representative_name,raw_work_text,source_page,"
        "source_sequence,work_period_status) VALUES(?,?,?,?,?,?,?,?,?)",
        (wid, import_id(conn), oid, og_raw, "시험기관", text, "2", "1", "미확인"))


def ref_record(conn, sid, oid, code, start=None, end=None):
    conn.execute("INSERT INTO ref_org_record(source_row_id,import_id,org_id,source_system,source_code,source_start,source_end) "
                 "VALUES(?,?,?,?,?,?,?)", (sid, import_id(conn), oid, "GCC", code, start, end))


def make_case(conn, code, title="시험 건"):
    return conn.execute("INSERT INTO review_case(primary_code,title,change_summary,detected_at,analysis_status) "
                        "VALUES(?,?,?,?, '완료')", (code, title, "요약", now())).lastrowid


def confirmed_move(conn, frm, to, user, current=1, text="청사 정보시스템 운영", date="2026-10-02", dstat="확인"):
    return conn.execute(
        "INSERT INTO confirmed_task_move(from_org_id,to_org_id,task_description,move_scope,effective_date,date_status,"
        "source_candidate_id,approved_by,approved_at,is_current) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (frm, to, text, "전체", date, dstat, 1, user, now(), current)).lastrowid


def confirmed_rel(conn, frm, to, user, rtype="승계"):
    return conn.execute(
        "INSERT INTO confirmed_relation(from_org_id,to_org_id,relation_type,valid_from,date_status,source_candidate_id,"
        "approved_by,approved_at,is_current) VALUES(?,?,?,?,?,?,?,?,1)",
        (frm, to, rtype, None, "미확인", 1, user, now())).lastrowid


def cand_move(conn, case_id, fcode, tcode, status="승인 대기", date=None, dstat="미확인"):
    return conn.execute(
        "INSERT INTO task_move_candidate(case_id,from_code,to_code,from_name,to_name,task_description,move_scope,effective_date,"
        "date_status,proposed_json,review_status) VALUES(?,?,?,?,?,?,?,?,?,'{}',?)",
        (case_id, fcode, tcode, "옛기관", "새기관", "후보 업무", "일부", date, dstat, status)).lastrowid


def cand_rel(conn, case_id, fcode, tcode, status="승인 대기", date=None, dstat="미확인"):
    return conn.execute(
        "INSERT INTO relation_candidate(case_id,from_code,to_code,from_name,to_name,relation_type,valid_from,date_status,"
        "proposed_json,review_status) VALUES(?,?,?,?,?,?,?,?,'{}',?)",
        (case_id, fcode, tcode, "옛기관", "새기관", "승계", date, dstat, status)).lastrowid


def frag(client, case_id, **params):
    r = client.get(f"/cases/{case_id}/lineage", params=params)
    assert r.status_code == 200
    return r.text


def arrows(html):
    return len(re.findall(r'data-arrow="1"', html))


def graph(html):
    """범례를 제외한 계보도 SVG 영역만 잘라 낸다."""
    m = re.search(r'<div class="lin-svgwrap">(.*?)</svg>', html, re.S)
    assert m, "계보도 SVG 가 없습니다."
    return m.group(1)


def tables_count(conn):
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}


# ---------------------------------------------------------------- 1. 로그인 필수
def test_login_required(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "x.db"))
    web._initialized.clear()
    make_users()
    with TestClient(web.app) as anon:
        r = anon.get("/cases/1/lineage", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_detail_has_toggle_button_and_unknown_case_404(env):
    c, conn, _ = env
    cid = make_case(conn, "1480041")
    html = c.get(f"/cases/{cid}").text
    assert "계보도 보기" in html and f'data-url="/cases/{cid}/lineage"' in html
    assert c.get("/cases/9999/lineage").status_code == 404


# ---------------------------------------------------------------- 2. 화살표는 승인된 업무 이동에만
def test_arrow_only_for_current_confirmed_task_move(env):
    c, conn, ids = env
    old = org(conn, "이전기관", "G1", "ENV-1")
    new = org(conn, "후속기관", "G2", "ENV-2")
    ref_rel(conn, "R1", "GCC_PREVIOUS_CODE_LINK", new, old)          # 원천 이전기관 링크: 선, 화살표 없음
    ref_rel(conn, "R2", "SUCCEEDS", new, old, layer="CANONICAL_RELATION", status="CONFIRMED_CROSS_SOURCE")
    ref_work(conn, "W1", old, "정보시스템 운영")
    cid = make_case(conn, "G2")
    cand_move(conn, cid, "G1", "G2")
    cand_rel(conn, cid, "G1", "G2")
    html = frag(c, cid)
    assert arrows(html) == 0                                         # 후보·원천만 있으면 화살표 0
    assert "업무 승계는 확인되지 않음(연결자료 부족)" in html
    assert "후보(미확정) 업무 승계" in html

    confirmed_move(conn, old, new, ids["hong"], current=0)           # 정정으로 비유효가 된 행
    assert arrows(frag(c, cid)) == 0
    confirmed_move(conn, old, new, ids["hong"], current=1)
    html = frag(c, cid)
    assert arrows(html) == 1 and 'marker-end="url(#lin-arrow)"' in html
    assert "업무 승계는 확인되지 않음" not in html
    assert "승인자 홍길동" in html


def test_previous_code_link_is_relation_not_work_succession(env):
    c, conn, _ = env
    old = org(conn, "이전기관", "G1")
    new = org(conn, "후속기관", "G2")
    ref_rel(conn, "R1", "GCC_PREVIOUS_CODE_LINK", new, old)
    cid = make_case(conn, "G2")
    html = frag(c, cid)
    assert arrows(html) == 0
    assert "기관관계 (화살표 없음)" in html and "업무 승계 ▶" not in html


# ---------------------------------------------------------------- 3. 날짜 '미확인'과 관측기간 구분
def test_dates_unknown_and_observed_period_separate(env):
    c, conn, _ = env
    old = org(conn, "이전기관", "G1")
    new = org(conn, "후속기관", "G2")
    ref_rel(conn, "R1", "GCC_PREVIOUS_CODE_LINK", new, old, obs=("2017-03-20", "2018-01-22"))
    ref_record(conn, "S1", new, "G2", start=None, end="2018-01-22")
    ref_work(conn, "W1", new, "업무 원문")
    cid = make_case(conn, "G2")
    cand_rel(conn, cid, "G1", "G2", date="2026-10-02", dstat="미확인")  # 값이 있어도 상태가 미확인이면 미확인
    html = frag(c, cid)
    assert "관계 유효기간: 미확인" in html
    assert "원천 관측기간(관계·업무 유효기간 아님): 2017-03-20 ~ 2018-01-22" in html
    assert "미확인 ~ 2018-01-22" in html                              # 기관 시작일 공란은 미확인
    assert "적용일: 미확인" in html and "2026-10-02" not in html
    assert "업무 유효기간 미확인 ~ 미확인" in html                      # 업무 기간은 존립/관측기간으로 채우지 않는다


# ---------------------------------------------------------------- 4. 층 구분
def test_layer_badges_and_canonical_not_our_approval(env):
    c, conn, ids = env
    a = org(conn, "기관A", "G1")
    b = org(conn, "기관B", "G2")
    d = org(conn, "기관D", "G3")
    ref_rel(conn, "R1", "SUCCEEDS", b, a, layer="CANONICAL_RELATION", status="CONFIRMED_CROSS_SOURCE")
    ref_work(conn, "W1", a, "원천 업무")
    cid = make_case(conn, "G1")
    cand_rel(conn, cid, "G1", "G3")
    confirmed_rel(conn, a, d, ids["kim"])
    html = frag(c, cid)
    for label in ("원천 기재", "검토 후보", "승인 결과"):
        assert label in html
    assert "원천 파일의 검토 상태: CONFIRMED_CROSS_SOURCE (우리 시스템 승인 아님)" in html
    g = graph(html)
    assert 'class="e-src"' in g and 'class="e-cand"' in g and 'class="e-appr"' in g
    assert "승인자 김철수" in html                                    # 승인자는 로그인 계정 표시 이름
    # 원천 파일의 CONFIRMED 표기가 '승인 결과' 층으로 분류되지 않는다 (승인 결과 간선은 확정 테이블 1개뿐)
    assert len(re.findall(r'<line [^>]*class="e-appr"', g)) == 1


# ---------------------------------------------------------------- 5. 자료 부족 / 자료 없음
def test_insufficient_and_empty_messages(env):
    c, conn, _ = env
    with_work = org(conn, "업무만 있는 기관", "G1")
    ref_work(conn, "W1", with_work, "업무 하나")
    c1 = make_case(conn, "G1")
    html = frag(c, c1)
    assert "연결자료 부족" in html and "업무 하나" in html and arrows(html) == 0

    org(conn, "자료 없는 기관", "G9")
    c2 = make_case(conn, "G9")
    html = frag(c, c2)
    assert "표시할 자료 없음" in html

    c3 = make_case(conn, "NOPE")                                      # 가져온 기관 자료에 없는 건
    html = frag(c, c3)                                                # 코드 기준 모드: 안내 + 자료 없음
    assert "기관을 검색해 선택하면" in html and "표시할 자료 없음" in html


# ---------------------------------------------------------------- 6. OG 전거ID 미확보
def test_og_id_unavailable_and_og_code_raw_not_confused(env):
    c, conn, _ = env
    a = org(conn, "기관A", "1480041", "ENV-ORG-01238")
    ref_work(conn, "W1", a, "자연환경보전에 관한 기본정책의 수립", og_raw="1480041")
    cid = make_case(conn, "1480041")
    html = frag(c, cid)
    assert "OG 전거ID: 미확보" in html and "OG 전거ID 미확보" in html
    assert "OG_CODE 원문 1480041" in html
    assert "OG 전거ID: 1480041" not in html
    b = org(conn, "OG 확보 기관", "G7", og="OG-777")
    assert "OG 전거ID: OG-777" in frag(c, make_case(conn, "G7"))


# ---------------------------------------------------------------- 7. 읽기 전용
def test_read_only(env):
    c, conn, ids = env
    old = org(conn, "이전기관", "G1")
    new = org(conn, "후속기관", "G2")
    ref_rel(conn, "R1", "GCC_PREVIOUS_CODE_LINK", new, old)
    ref_work(conn, "W1", old, "업무")
    cid = make_case(conn, "G2")
    cand_move(conn, cid, "G1", "G2")
    confirmed_move(conn, old, new, ids["hong"])
    before = tables_count(conn)
    frag(c, cid)
    frag(c, cid, org=old)
    frag(c, cid, q="이전")
    assert tables_count(conn) == before


# ---------------------------------------------------------------- 8. 기관 검색
def test_search_by_name_code_common_id_and_limit(env):
    c, conn, _ = env
    for i in range(25):
        org(conn, f"검색기관{i:02d}", f"SG{i:02d}", f"ENV-S-{i:02d}")
    org(conn, "다른 기관", "ZZ1", "ENV-ZZ")
    cid = make_case(conn, "ZZ1")
    html = frag(c, cid, q="검색기관")
    assert len(re.findall(r'data-lineage-url="/cases/\d+/lineage\?org=\d+"', html)) == 20
    assert "최대 20건" in html
    assert "다른 기관" in frag(c, cid, q="ZZ1") and "다른 기관" in frag(c, cid, q="ENV-ZZ")
    assert "검색 결과가 없습니다" in frag(c, cid, q="없는기관명")
    assert len(lineage.search_orgs(conn, "검색기관", limit=50)) == 25
    # 검색 결과의 기관을 선택하면 그 기관의 계보도가 나온다
    target = conn.execute("SELECT org_id FROM org WHERE org_name='검색기관03'").fetchone()[0]
    assert "검색기관03" in frag(c, cid, org=target)


# ---------------------------------------------------------------- 9. 노드 16개 제한
def test_node_limit_16(env):
    c, conn, ids = env
    assert lineage.MAX_NODES == 16
    root = org(conn, "중심 기관", "R0")
    for i in range(20):
        o = org(conn, f"상대기관{i:02d}", f"C{i:02d}")
        confirmed_rel(conn, o, root, ids["hong"])
    cid = make_case(conn, "R0")
    html = frag(c, cid)
    assert len(re.findall(r'<g class="node ', html)) == 16
    assert "일부만 표시" in html
    g = lineage.build(conn, org_id=root)
    assert len(g["nodes"]) == 16 and g["truncated"] and g["omitted"] == 5


def test_node_limit_not_truncated_at_exactly_16(env):
    c, conn, ids = env
    root = org(conn, "중심 기관", "R0")
    for i in range(15):
        o = org(conn, f"상대기관{i:02d}", f"C{i:02d}")
        confirmed_rel(conn, o, root, ids["hong"])
    g = lineage.build(conn, org_id=root)
    assert len(g["nodes"]) == 16 and not g["truncated"]


# ---------------------------------------------------------------- 두 줄 구조 (기관 단위 줄 + 하위 조직 줄)
def ref_record_parent(conn, sid, oid, parent="0000000"):
    conn.execute("INSERT INTO ref_org_record(source_row_id,import_id,org_id,source_system,source_code,parent_code) "
                 "VALUES(?,?,?, 'GCC', ?, ?)", (sid, import_id(conn), oid, "C" + sid, parent))


def ref_record_cams(conn, sid, oid):
    conn.execute("INSERT INTO ref_org_record(source_row_id,import_id,org_id,source_system,source_code,source_start,source_end) "
                 "VALUES(?,?,?, 'CAMS_OG', NULL, '1993-10-15', '1994-12-23')", (sid, import_id(conn), oid))


def two_level_fixture(conn):
    """환경청 <- 환경처 <- 환경부 (+후신 기후에너지환경부), 환경부 > 자연보전국 > 자연정책과 <- 환경처 자연정책과(소속 자료 없음)."""
    o = {}
    o["청"] = org(conn, "환경청", "1790279", "ENV-ORG-01533")
    o["처"] = org(conn, "환경처", "1790268", "ENV-ORG-01532")
    o["부"] = org(conn, "환경부", "1480000", "ENV-ORG-00361")
    o["기후"] = org(conn, "기후에너지환경부", "1482000", "ENV-ORG-00001")
    o["국"] = org(conn, "환경부 자연보전국", "1480038", "ENV-ORG-01216")
    o["과"] = org(conn, "환경부 자연보전국 자연정책과", "1480041", "ENV-ORG-01238")
    o["생태"] = org(conn, "환경부 자연보전국 자연생태정책과", "1480898", "ENV-ORG-01235")
    o["옛과"] = org(conn, "환경처 자연보전국 자연정책과", None, "ENV-ORG-01557")
    parent_code = {"청": "0000000", "처": "0000000", "부": "0000000", "기후": "0000000",
                   "국": "1480000", "과": "1480038", "생태": "1480038"}
    for i, (name, pc) in enumerate(parent_code.items()):
        ref_record_parent(conn, f"G{i}", o[name], pc)
    # GCC 상위기관 링크(from=하위, to=상위)
    ref_rel(conn, "P1", "GCC_PARENT_LINK", o["과"], o["국"])
    ref_rel(conn, "P2", "GCC_PARENT_LINK", o["국"], o["부"])
    ref_rel(conn, "P3", "GCC_PARENT_LINK", o["생태"], o["국"])
    # 승계 관계(원천 파일: from=후속, to=이전). R4 는 먼저 만나는 짧은 링크(충돌 검토 상태)
    ref_rel(conn, "R1", "SUCCEEDS", o["처"], o["청"], layer="CANONICAL_RELATION", status="CONFIRMED_CROSS_SOURCE")
    ref_rel(conn, "R2", "SUCCEEDS", o["부"], o["처"], layer="CANONICAL_RELATION", status="CONFIRMED_CROSS_SOURCE")
    ref_rel(conn, "R3", "SUCCEEDS", o["기후"], o["부"], layer="CANONICAL_RELATION", status="NEEDS_REVIEW")
    ref_rel(conn, "R0", "GCC_PREVIOUS_CODE_LINK", o["부"], o["청"], status="CONFLICT_REVIEW")
    ref_rel(conn, "R5", "SUCCEEDS", o["생태"], o["과"], layer="CANONICAL_RELATION", status="GCC_CONFIRMED_OG_NOT_FOUND")
    ref_rel(conn, "R6", "SUCCEEDS", o["과"], o["옛과"], layer="CANONICAL_RELATION", status="CONFIRMED_OG_RELATION")
    ref_record_cams(conn, "CAMS1", o["옛과"])
    return o


def test_two_rows_top_institution_mid_and_lower_unit(env):
    c, conn, _ = env
    o = two_level_fixture(conn)
    g = lineage.build(conn, org_id=o["과"])
    by = {n["name"]: n for n in g["nodes"]}
    assert g["rows"] == ["top", "mid", ""]
    for name in ("환경부", "환경처", "환경청", "기후에너지환경부"):
        assert by[name]["band"] == "top"
    assert by["환경부 자연보전국"]["band"] == "mid"
    for name in ("환경부 자연보전국 자연정책과", "환경부 자연보전국 자연생태정책과", "환경처 자연보전국 자연정책과"):
        assert by[name]["band"] == ""
    cid = make_case(conn, "NOPE")
    html = frag(c, cid, org=o["과"])
    assert 'data-row="top"' in html and 'data-row="mid"' in html and 'data-row="main"' in html
    assert "기관 단위 계보" in html and "하위 조직 계보" in html


def test_canonical_followed_with_direction_and_predecessor_left_of_successor(env):
    c, conn, _ = env
    o = two_level_fixture(conn)
    g = lineage.build(conn, org_id=o["과"])
    lvl = {n["name"]: n["level"] for n in g["nodes"]}
    # 원천 파일 from=후속: 환경부(후속) -> 환경처(이전) -> 환경청(더 이전), 기후에너지환경부는 환경부의 후속
    assert lvl["환경청"] < lvl["환경처"] < lvl["환경부"] < lvl["기후에너지환경부"]
    # 짧은 링크(환경부->환경청, CONFLICT_REVIEW)가 먼저 만나도 환경청이 환경처와 같은 열이 되지 않는다
    assert lvl["환경청"] != lvl["환경처"]
    # 하위 조직 줄: 환경처 자연정책과(이전) < 자연정책과 < 자연생태정책과(후속)
    assert lvl["환경처 자연보전국 자연정책과"] < lvl["환경부 자연보전국 자연정책과"] < lvl["환경부 자연보전국 자연생태정책과"]
    # 상위기관(국, 부)은 선택 기관과 같은 열: 소속 관계는 위아래로 이어진다
    assert lvl["환경부 자연보전국"] == lvl["환경부"] == lvl["환경부 자연보전국 자연정책과"] == 0
    cid = make_case(conn, "NOPE")
    html = frag(c, cid, org=o["과"])
    assert "원천 파일의 검토 상태: CONFIRMED_CROSS_SOURCE (우리 시스템 승인 아님)" in html
    assert "원천 파일의 상태: CONFLICT_REVIEW" in html
    assert arrows(html) == 0
    assert "업무 승계는 확인되지 않음(연결자료 부족)" in html


def test_unlinked_node_marked_and_not_connected(env):
    c, conn, _ = env
    o = two_level_fixture(conn)
    g = lineage.build(conn, org_id=o["과"])
    by = {n["name"]: n for n in g["nodes"]}
    old = by["환경처 자연보전국 자연정책과"]
    assert "소속 연결 미확인" in old["unlinked"]
    # 소속 선(parent)이 이 노드로 이어지지 않는다
    assert not [e for e in g["edges"] if e["kind"] == "parent" and old["key"] in (e["start"], e["end"])]
    # 최상위 기관(상위기관 코드 없음)과 소속이 이어진 기관은 '미확인'으로 표시하지 않는다
    for name in ("환경부", "환경처", "환경청", "환경부 자연보전국", "환경부 자연보전국 자연정책과"):
        assert by[name]["unlinked"] == "", name
    cid = make_case(conn, "NOPE")
    html = frag(c, cid, org=o["과"])
    assert "소속 연결 미확인" in html and "선으로 잇지 않았습니다" in html


def test_root_without_parent_data_shows_single_row_and_notice(env):
    c, conn, _ = env
    a = org(conn, "소속자료 없는 과", "X1")
    b = org(conn, "후속 과", "X2")
    ref_rel(conn, "R1", "SUCCEEDS", b, a, layer="CANONICAL_RELATION", status="NEEDS_REVIEW")
    cid = make_case(conn, "X1")
    g = lineage.build(conn, org_id=a)
    assert g["rows"] == [""]
    assert any("소속(상위기관) 자료가 없어" in t for _, t in g["notices"])
    html = frag(c, cid, org=a)
    assert "소속 연결 미확인" in html and 'data-row="top"' not in html


def test_arrows_only_for_confirmed_task_move_in_both_rows(env):
    c, conn, ids = env
    o = two_level_fixture(conn)
    cid = make_case(conn, "NOPE")
    cand_move(conn, cid, "1790268", "1480000")      # 검토 후보: 화살표 없음
    assert arrows(frag(c, cid, org=o["과"])) == 0
    confirmed_move(conn, o["처"], o["부"], ids["hong"], dstat="미확인", date=None)    # 기관 단위 승인
    confirmed_move(conn, o["과"], o["생태"], ids["hong"], dstat="미확인", date=None)  # 하위 조직 승인
    html = frag(c, cid, org=o["과"])
    assert arrows(html) == 2
    assert "업무 승계는 확인되지 않음" not in html
    assert "적용일: 미확인" in html


# ---------------------------------------------------------------- 10. 실제 엑셀
@pytest.mark.skipif(not XLSX.exists(), reason="환경부 엑셀 파일이 폴더에 없음")
def test_real_excel_nature_policy_lineage(env):
    from app import import_xlsx

    c, conn, _ = env
    import_xlsx.run_import(conn, XLSX)
    oid = conn.execute("SELECT org_id FROM org_identifier WHERE id_type='COMMON' AND id_value='ENV-ORG-01238'").fetchone()[0]
    cid = make_case(conn, "NOPE-CODE")
    before = tables_count(conn)
    html = frag(c, cid, org=oid)
    assert tables_count(conn) == before                               # 읽기 전용
    assert "자연정책과" in html
    assert "자연환경보전에 관한 기본정책의 수립" in html                 # 원천 업무 표시
    assert "원천 기재" in html and "미확인" in html
    assert arrows(html) == 0                                          # 승인 결과가 없으므로 화살표 0
    assert "업무 승계는 확인되지 않음(연결자료 부족)" in html
    assert "우리 시스템 승인 아님" in html                              # 원천 파일의 Canonical 표기
    assert "OG 전거ID: 미확보" in html
    assert "일부만 표시" not in html or lineage.MAX_NODES == 16
    # 검색으로도 찾을 수 있다
    assert "자연정책과" in frag(c, cid, q="ENV-ORG-01238")


@pytest.mark.skipif(not XLSX.exists(), reason="환경부 엑셀 파일이 폴더에 없음")
def test_real_excel_two_rows_connection_range(env):
    """엑셀에서 실제로 확인되는 연결 범위만 그린다: 소속은 자연정책과>자연보전국>환경부까지, 환경처 시절 과는 끊김."""
    from app import import_xlsx

    c, conn, _ = env
    import_xlsx.run_import(conn, XLSX)
    oid = conn.execute("SELECT org_id FROM org_identifier WHERE id_type='COMMON' AND id_value='ENV-ORG-01238'").fetchone()[0]
    g = lineage.build(conn, org_id=oid)
    by = {n["name"]: n for n in g["nodes"]}
    assert g["rows"] == ["top", "mid", ""] and not g["truncated"]
    for name in ("환경청", "환경처", "환경부", "기후에너지환경부"):
        assert by[name]["band"] == "top", name
    assert by["환경부 자연보전국"]["band"] == "mid"
    assert by["환경청"]["level"] < by["환경처"]["level"] < by["환경부"]["level"] < by["기후에너지환경부"]["level"]
    old = by["환경처 자연보전국 자연정책과"]
    assert old["band"] == "" and "소속 연결 미확인" in old["unlinked"]
    assert not [e for e in g["edges"] if e["kind"] == "parent" and old["key"] in (e["start"], e["end"])]
    assert not g["has_arrow"]
    cid = make_case(conn, "NOPE-CODE")
    before = tables_count(conn)
    html = frag(c, cid, org=oid)
    assert tables_count(conn) == before
    assert arrows(html) == 0 and "소속 연결 미확인" in html
    assert "원천 관측기간(관계·업무 유효기간 아님)" in html
