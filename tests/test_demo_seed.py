"""교육 시연용 스크립트(app.demo_seed) 시험. 실제 엑셀이 폴더에 있으면 그것으로 확인하고(없으면 skip),
운영 DB 보호·덮어쓰기 규칙은 엑셀 없이 확인한다."""
import csv
import io
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, db, demo_seed, export, import_xlsx, lineage, web

from .web_helpers import login

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "환경부_GCC_CAMS_기관관계업무_마스터_Pilot_v03_원자료보완.xlsx"
needs_xlsx = pytest.mark.skipif(not XLSX.exists(), reason="환경부 엑셀 파일이 폴더에 없음")
PW = "demo-pass-1234"
WARN = demo_seed.WARN


def make_sqlite(path: Path) -> Path:
    conn = db.connect(str(path))
    db.init_db(conn)
    conn.close()
    return path


def norm(t) -> str:
    return " ".join(str(t or "").split())


def counts(conn, *tables) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


# ---------------------------------------------------------------- 운영 DB 보호·덮어쓰기 (엑셀 불필요)
def test_refuses_operational_db_paths(tmp_path, monkeypatch):
    ops = tmp_path / "ops.db"
    monkeypatch.setenv("APP_DB", str(ops))
    with pytest.raises(demo_seed.DemoError, match="운영"):
        demo_seed.prepare_target(ops, reset=True)          # APP_DB 가 가리키는 DB
    monkeypatch.delenv("APP_DB")
    with pytest.raises(demo_seed.DemoError, match="운영"):
        demo_seed.prepare_target(demo_seed.APP_DEFAULT_DB, reset=True)   # 기본 앱 DB
    with pytest.raises(demo_seed.DemoError):
        demo_seed.prepare_target(":memory:", reset=False)


def test_main_returns_error_for_operational_db_and_keeps_file(tmp_path, monkeypatch, capsys):
    ops = make_sqlite(tmp_path / "ops.db")
    monkeypatch.setenv("APP_DB", str(ops))
    before = ops.read_bytes()
    rc = demo_seed.main(["어딘가.xlsx", "--db", str(ops), "--reset"])
    assert rc == 1 and "운영" in capsys.readouterr().err
    assert ops.read_bytes() == before


def test_existing_demo_db_needs_reset_and_only_sqlite_is_removed(tmp_path):
    p = make_sqlite(tmp_path / "demo.db")
    with pytest.raises(demo_seed.DemoError, match="--reset"):
        demo_seed.prepare_target(p, reset=False)
    assert p.exists()
    demo_seed.prepare_target(p, reset=True)
    assert not p.exists()
    other = tmp_path / "notes.db"
    other.write_text("SQLite 가 아닌 파일", encoding="utf-8")
    with pytest.raises(demo_seed.DemoError, match="SQLite 파일이 아니라서"):
        demo_seed.prepare_target(other, reset=True)
    assert other.exists()


def test_missing_excel_leaves_no_demo_db(tmp_path, capsys):
    p = tmp_path / "demo.db"
    rc = demo_seed.main([str(tmp_path / "없는파일.xlsx"), "--db", str(p)])
    assert rc == 1 and "찾을 수 없습니다" in capsys.readouterr().err
    assert not p.exists()


# ---------------------------------------------------------------- 실제 엑셀로 시연 DB 만들기
@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    sha_before = import_xlsx.sha256_of(XLSX)
    out = {}
    for name, approve in (("pending", False), ("approved", True)):
        path = d / f"{name}.db"
        conn = db.connect(str(path))
        db.init_db(conn)
        out[name] = (path, demo_seed.seed(conn, XLSX, approve=approve, password=PW))
        conn.close()
    out["sha_changed"] = import_xlsx.sha256_of(XLSX) != sha_before
    return out


def open_db(seeded, name):
    return db.connect(str(seeded[name][0]))


@needs_xlsx
def test_excel_file_untouched(seeded):
    assert seeded["sha_changed"] is False
    assert seeded["pending"][1]["import_report"]["source_file_unchanged"] is True


@needs_xlsx
def test_demo_case_and_candidates_marked_as_demo_assumption(seeded):
    conn = open_db(seeded, "pending")
    case = conn.execute("SELECT * FROM review_case").fetchall()
    assert len(case) == 1
    assert case[0]["title"].startswith("[교육 시연용]")
    assert "교육 시연용 가정 — 엑셀에서 확인된 사실이 아님" in case[0]["change_summary"]
    assert case[0]["primary_code"] == "1480041"
    cands = conn.execute("SELECT * FROM task_move_candidate ORDER BY candidate_id").fetchall()
    assert len(cands) == 2
    for c in cands:
        assert c["review_status"] == "승인 대기" and c["date_status"] == "미확인" and c["effective_date"] is None
        assert c["move_scope"] == "일부"
        assert WARN in c["proposed_json"]
    assert [(c["from_code"], c["to_code"]) for c in cands] == [("1480041", "1480898"), ("1790268", "1480000")]
    # 근거: 법령이 아니라는 표시, 출처 URL 없음, 근거/사유에 시연용 가정 명시
    law = conn.execute("SELECT * FROM law_source").fetchall()
    assert len(law) == 1 and "법령 아님" in law[0]["law_name"] and "교육 시연용 가정" in law[0]["law_name"]
    assert law[0]["source_url"] is None
    ev = conn.execute("SELECT * FROM evidence").fetchall()
    assert len(ev) == 2 and all(WARN in e["reason"] and "엑셀 원문 인용" in e["excerpt"] for e in ev)


@needs_xlsx
def test_task_text_is_quoted_from_excel_not_invented(seeded):
    conn = open_db(seeded, "pending")
    works = import_xlsx.read_workbook(XLSX)["work"]

    def common(row):
        return import_xlsx.clean(row.get("COMMON_ORG_ID"))

    # 공통기관ID 로 엑셀에서 직접 확인한다: 환경처 = ENV-ORG-01532, 자연정책과 = ENV-ORG-01238
    first_env = sorted((w for w in works if common(w) == "ENV-ORG-01532"), key=lambda w: w["WORK_ID"])[0]
    nature = {norm(w["RAW_WORK_TEXT"]) for w in works if common(w) == "ENV-ORG-01238"}
    cands = conn.execute("SELECT task_description FROM task_move_candidate ORDER BY candidate_id").fetchall()
    assert norm(cands[0]["task_description"]) in nature                      # 엑셀 원문에 실제로 있는 항목
    assert norm(cands[0]["task_description"]) == "자연환경보전에 관한 기본정책의 수립"
    assert norm(cands[1]["task_description"]) == norm(first_env["RAW_WORK_TEXT"])   # 환경처 업무 원문 중 첫 항목


@needs_xlsx
def test_candidates_linked_to_imported_orgs_by_gcc_identifier(seeded):
    conn = open_db(seeded, "pending")
    for code in ("1480041", "1480898", "1790268", "1480000"):
        row = conn.execute("SELECT o.org_name, i.org_id FROM org_identifier i JOIN org o ON o.org_id=i.org_id "
                           "WHERE i.id_type='GCC' AND i.id_value=?", (code,)).fetchone()
        assert row, code
        assert conn.execute("SELECT COUNT(*) FROM org_identifier WHERE org_id=? AND id_type='COMMON'",
                            (row["org_id"],)).fetchone()[0] == 1
    names = conn.execute("SELECT from_name, to_name FROM task_move_candidate ORDER BY candidate_id").fetchall()
    assert names[0]["from_name"] == "환경부 자연보전국 자연정책과" and names[0]["to_name"] == "환경부 자연보전국 자연생태정책과"
    assert names[1]["from_name"] == "환경처" and names[1]["to_name"] == "환경부"
    # OG 전거ID 는 확보되지 않았으므로 만들지 않는다
    assert conn.execute("SELECT COUNT(*) FROM org_identifier WHERE id_type='OG'").fetchone()[0] == 0


@needs_xlsx
@pytest.mark.parametrize("name", ["pending", "approved"])
def test_no_fake_collection_or_change_data(seeded, name):
    conn = open_db(seeded, name)
    assert counts(conn, "collection_run", "gcc_record", "change_event") == \
        {"collection_run": 0, "gcc_record": 0, "change_event": 0}
    assert counts(conn, "relation_candidate", "confirmed_relation", "confirmed_org_info") == \
        {"relation_candidate": 0, "confirmed_relation": 0, "confirmed_org_info": 0}


def self_org(conn):
    return conn.execute("SELECT org_id FROM org_identifier WHERE id_type='GCC' AND id_value='1480041'").fetchone()[0]


@needs_xlsx
def test_pending_state_has_no_confirmed_data_and_no_arrows(seeded):
    conn = open_db(seeded, "pending")
    assert counts(conn, "confirmed_task_move", "review_log") == {"confirmed_task_move": 0, "review_log": 0}
    g = lineage.build(conn, org_id=self_org(conn))
    assert not g["has_arrow"] and not any(e["arrow"] for e in g["edges"])
    assert export.export_csv(conn).strip().splitlines() == [",".join(export.HEADER)]
    # 후보는 검토 후보 층에 나오고 원천 기재와 구분된다
    assert any("검토 후보" in n["layers"] for n in g["nodes"])


@needs_xlsx
def test_approved_via_service_with_demo_account_and_two_arrows(seeded):
    path, res = seeded["approved"]
    conn = open_db(seeded, "approved")
    demo_id = conn.execute("SELECT user_id FROM app_user WHERE login_id='demo'").fetchone()[0]
    assert demo_id == res["user_id"]
    moves = conn.execute("SELECT * FROM confirmed_task_move").fetchall()
    assert len(moves) == 2 and all(m["is_current"] == 1 and m["approved_by"] == demo_id for m in moves)
    assert all(m["date_status"] == "미확인" and m["effective_date"] is None for m in moves)
    log = conn.execute("SELECT * FROM review_log").fetchall()
    assert len(log) == 2 and all(r["reviewer_id"] == demo_id and r["action"] == "승인" and WARN in r["reason"] for r in log)
    assert conn.execute("SELECT COUNT(*) FROM task_move_candidate WHERE review_status='승인 완료'").fetchone()[0] == 2
    g = lineage.build(conn, org_id=self_org(conn))
    arrows = [e for e in g["edges"] if e["arrow"]]
    assert len(arrows) == 2 and g["has_arrow"]
    assert all("시연 계정" in e["status"] and "적용일: 미확인" in e["valid"] for e in arrows)
    by = {n["key"]: n["name"] for n in g["nodes"]}
    assert {(by[e["start"]], by[e["end"]]) for e in arrows} == {
        ("환경부 자연보전국 자연정책과", "환경부 자연보전국 자연생태정책과"), ("환경처", "환경부")}
    assert not any("업무 승계는 확인되지 않음" in t for _, t in g["notices"])


@needs_xlsx
def test_csv_and_password_hashed(seeded):
    conn = open_db(seeded, "approved")
    rows = list(csv.reader(io.StringIO(export.export_csv(conn))))    # 업무 원문에 줄바꿈이 있어 csv 로 파싱한다
    header, body = rows[0], rows[1:]
    assert header == list(export.HEADER) and len(body) == 2
    approver, evidence = header.index("승인자"), header.index("근거")
    assert all(r[approver] == "시연 계정" for r in body)
    assert all("교육 시연용 가정" in r[evidence] for r in body)    # 근거 칸에 법령 아님 표시가 남는다
    pw_hash = conn.execute("SELECT password_hash FROM app_user WHERE login_id='demo'").fetchone()[0]
    assert PW not in pw_hash and auth.verify_password(PW, pw_hash)


@needs_xlsx
def test_web_login_demo_detail_and_two_row_lineage(seeded, monkeypatch):
    path, _ = seeded["approved"]
    monkeypatch.setenv("APP_DB", str(path))
    web._initialized.clear()
    web._login_failures.clear()
    with TestClient(web.app) as c:
        assert c.get("/", follow_redirects=False).status_code in (302, 303, 307)   # 로그인 필수
        assert login(c, "demo", PW).status_code == 303
        page = c.get("/cases/1").text
        assert "[교육 시연용]" in page and "교육 시연용 가정" in page
        html = c.get("/cases/1/lineage").text
    assert len(re.findall(r'data-arrow="1"', html)) == 2
    for row in ("top", "mid", "main"):
        assert f'data-row="{row}"' in html
    assert "소속 연결 미확인" in html and "환경청" in html and "환경처" in html
    assert "OG 전거ID: 미확보" in html


# ---------------------------------------------------------------- 명령행
@needs_xlsx
def test_cli_password_stdin_and_output(tmp_path, monkeypatch, capsys):
    p = tmp_path / "cli.db"
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.delenv("APP_DB", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("cli-pass-9999\n"))
    assert demo_seed.main([str(XLSX), "--db", str(p), "--password-stdin"]) == 0
    out = capsys.readouterr().out
    assert str(p) in out and "demo" in out and "uvicorn" in out and "127.0.0.1:8000" in out
    assert "교육 시연용 가정, 엑셀에서 확인된 사실 아님" in out
    assert "cli-pass-9999" not in out            # 지정한 비밀번호는 출력하지 않는다
    conn = db.connect(str(p))
    assert conn.execute("SELECT COUNT(*) FROM task_move_candidate WHERE review_status='승인 대기'").fetchone()[0] == 2
    assert auth.verify_password("cli-pass-9999",
                                conn.execute("SELECT password_hash FROM app_user WHERE login_id='demo'").fetchone()[0])
    conn.close()
    # 같은 경로 재실행은 --reset 없이는 거부된다
    assert demo_seed.main([str(XLSX), "--db", str(p), "--password-stdin"]) == 1


@needs_xlsx
def test_cli_random_password_printed_once_and_works(tmp_path, monkeypatch, capsys):
    p = tmp_path / "rand.db"
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.delenv("APP_DB", raising=False)
    assert demo_seed.main([str(XLSX), "--db", str(p), "--approve"]) == 0
    out = capsys.readouterr().out
    m = re.search(r"비밀번호\(이번 한 번만 표시\): (\S+)", out)
    assert m and out.count(m.group(1)) == 1
    conn = db.connect(str(p))
    assert auth.verify_password(m.group(1), conn.execute("SELECT password_hash FROM app_user WHERE login_id='demo'").fetchone()[0])
    assert conn.execute("SELECT COUNT(*) FROM confirmed_task_move").fetchone()[0] == 2
    conn.close()
