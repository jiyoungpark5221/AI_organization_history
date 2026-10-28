"""교육 시연용 데이터 만들기.

  python -m app.demo_seed <엑셀경로> [--db data/demo.db] [--reset] [--approve] [--password-stdin]

운영 DB와 분리된 시연 전용 DB에 엑셀을 가져오고, 계보도에 화살표가 생기는 과정을 보여 줄 수 있도록
'업무 이동 후보' 2건을 승인 대기 상태로 만든다. 이 후보는 **교육 시연용 가정이며 엑셀에서 확인된 사실이 아니다**:
엑셀의 업무 승계는 전부 미확인이고, 어떤 업무가 어느 기관으로 넘어갔는지는 어디에도 적혀 있지 않다.

  ① 과 단위 : 환경부 자연보전국 자연정책과(1480041) → 환경부 자연보전국 자연생태정책과(1480898)
  ② 기관 단위: 환경처(1790268) → 환경부(1480000)
업무 내용은 엑셀 ref_org_work 의 원문을 그대로 인용한다(임의로 쓰지 않는다).

수집·변경 탐지 데이터(collection_run, gcc_record, change_event)는 만들지 않는다. 승인은 --approve 일 때만,
정식 승인 서비스(review.decide)를 시연 계정(demo)으로 호출해 처리한다.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from . import auth, db, import_xlsx, review

DEMO_LOGIN, DEMO_NAME = "demo", "시연 계정"
WARN = "교육 시연용 가정, 엑셀에서 확인된 사실 아님"
CASE_TITLE = "[교육 시연용] 자연정책과 계열 업무 이동 시연"
CASE_SUMMARY = ("교육 시연용 가정 — 엑셀에서 확인된 사실이 아님. 엑셀의 업무 승계는 모두 미확인이며, "
                "이 건의 업무 이동 후보 2건은 계보도 시연을 위해 임의로 가정한 것이다.")
LAW_NAME = "[교육 시연용 가정] 법령 아님 — 실제 법령 근거가 아님"
PRIMARY_CODE = "1480041"
DEFAULT_DB = db.ROOT / "data" / "demo.db"
APP_DEFAULT_DB = db.ROOT / "data" / "app.db"
SQLITE_MAGIC = b"SQLite format 3\x00"

# (표시, 이전 GCC 코드, 이후 GCC 코드, 원문을 가져올 기관 GCC 코드, 지정 원문(없으면 그 기관의 첫 항목))
DEMO_MOVES = (
    ("과 단위", "1480041", "1480898", "1480041", "자연환경보전에 관한 기본정책의 수립"),
    ("기관 단위", "1790268", "1480000", "1790268", None),
)


class DemoError(Exception):
    """사용자에게 그대로 보여줄 오류."""


def _same(a, b) -> bool:
    return Path(a).resolve() == Path(b).resolve()


def prepare_target(path, reset: bool) -> Path:
    """시연 DB 경로를 검증한다. 운영(앱) DB 와 같은 경로는 거부하고, 기존 시연 DB 는 --reset 일 때만 지운다."""
    if str(path) == ":memory:":
        raise DemoError("시연 DB 는 파일이어야 합니다.")
    p = Path(path)
    if _same(p, db.db_path()) or _same(p, APP_DEFAULT_DB):
        raise DemoError(f"운영(앱) DB 와 같은 경로는 사용할 수 없습니다: {p}\n"
                        "시연 전용 경로(기본 data/demo.db)를 지정하세요. 환경변수 APP_DB 가 시연 DB 를 가리키고 있다면 "
                        "비운 상태로 실행하세요.")
    if p.exists():
        if not reset:
            raise DemoError(f"이미 있는 시연 DB 입니다: {p}\n덮어쓰려면 --reset 을 지정하세요.")
        with open(p, "rb") as f:
            if f.read(16) != SQLITE_MAGIC:
                raise DemoError(f"SQLite 파일이 아니라서 지우지 않습니다: {p}")
        for suffix in ("", "-wal", "-shm", "-journal"):
            q = Path(str(p) + suffix)
            if q.exists():
                q.unlink()
    return p


def _org_id(conn, gcc_code: str) -> int:
    row = conn.execute("SELECT org_id FROM org_identifier WHERE id_type='GCC' AND id_value=?", (gcc_code,)).fetchone()
    if row is None:
        raise DemoError(f"엑셀에서 GCC 코드 {gcc_code} 기관을 찾을 수 없습니다.")
    return row["org_id"]


def _org_name(conn, org_id: int) -> str:
    return conn.execute("SELECT org_name FROM org WHERE org_id=?", (org_id,)).fetchone()["org_name"]


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _quote(conn, owner_code: str, exact: str | None) -> dict:
    """엑셀 ref_org_work 의 원문을 그대로 가져온다 (임의 작성 금지)."""
    org = _org_id(conn, owner_code)
    rows = conn.execute("SELECT * FROM ref_org_work WHERE org_id=? ORDER BY work_id", (org,)).fetchall()
    if exact is not None:
        rows = [r for r in rows if _norm(r["raw_work_text"]) == _norm(exact)]
    if not rows:
        raise DemoError(f"엑셀에서 GCC {owner_code} 기관의 업무 원문을 찾을 수 없습니다"
                        + (f": {exact}" if exact else "") + ".")
    r = rows[0]
    return {"work_id": r["work_id"], "text": r["raw_work_text"], "file": r["source_file"],
            "page": r["source_page"], "seq": r["source_sequence"], "og_code_raw": r["og_code_raw"]}


def _read_password(args) -> tuple[str, bool]:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n"), False
    env = os.environ.get("DEMO_PASSWORD")
    if env:
        return env, False
    return secrets.token_urlsafe(12), True  # 무작위: 한 번만 출력한다


def seed(conn, xlsx_path, *, approve: bool, password: str) -> dict:
    """시연 DB(conn)에 엑셀을 가져오고 시연 건·후보를 만든다. 결과 요약 dict 를 반환한다."""
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.is_file():
        raise DemoError(f"엑셀 파일을 찾을 수 없습니다: {xlsx_path}")
    report = import_xlsx.run_import(conn, xlsx_path)  # 원본은 읽기 전용으로만 연다
    if not report["source_file_unchanged"]:
        raise DemoError("엑셀 원본이 가져오기 중 바뀌었습니다. 중단합니다.")

    try:
        user_id = auth.create_user(conn, DEMO_LOGIN, DEMO_NAME, password)
    except auth.AuthError as e:
        raise DemoError(f"시연 계정을 만들 수 없습니다: {e}") from e

    plans = []
    for label, frm, to, owner, exact in DEMO_MOVES:
        plans.append({"label": label, "from_code": frm, "to_code": to, "from_org": _org_id(conn, frm),
                      "to_org": _org_id(conn, to), "quote": _quote(conn, owner, exact)})

    ts = db.now()
    cand_ids = []
    with db.tx(conn):
        case_id = conn.execute(
            "INSERT INTO review_case(primary_code, title, change_summary, detected_at, analysis_status, data_status) "
            "VALUES(?,?,?,?, '완료', '정상')",
            (PRIMARY_CODE, CASE_TITLE, CASE_SUMMARY, ts)).lastrowid
        law_id = conn.execute(
            "INSERT INTO law_source(case_id, law_name, role, fetch_status, fetched_at) VALUES(?,?, '개편 후', "
            "'시연용 가정(법령 아님)', ?)", (case_id, LAW_NAME, ts)).lastrowid
        prov_id = conn.execute(
            "INSERT INTO law_provision(law_source_id, article_ref, text) VALUES(?,?,?)",
            (law_id, "시연용 가정", f"{WARN}. 이 행은 법령 조항이 아니며 계보도 시연을 위해 만든 자리표시 근거이다.")).lastrowid
        for p in plans:
            q = p["quote"]
            proposed = {"task_description": q["text"], "move_scope": "일부", "effective_date": None,
                        "date_status": "미확인", "note": WARN, "source_work_id": q["work_id"]}
            cid = conn.execute(
                "INSERT INTO task_move_candidate(case_id, from_code, to_code, from_name, to_name, task_description, "
                "move_scope, effective_date, date_status, proposed_json, review_status) "
                "VALUES(?,?,?,?,?,?, '일부', NULL, '미확인', ?, '승인 대기')",
                (case_id, p["from_code"], p["to_code"], _org_name(conn, p["from_org"]), _org_name(conn, p["to_org"]),
                 q["text"], json.dumps(proposed, ensure_ascii=False))).lastrowid
            conn.execute(
                "INSERT INTO evidence(candidate_type, candidate_id, provision_id, excerpt, reason) VALUES('task_move',?,?,?,?)",
                (cid, prov_id,
                 f"엑셀 원문 인용(ref_org_work {q['work_id']}, {q['file']} {q['page']}쪽 {q['seq']}번): {_norm(q['text'])}",
                 f"{WARN}. 이 업무가 실제로 이동했는지는 엑셀에서 확인되지 않았다(업무 승계·날짜 미확인)."))
            cand_ids.append((p["label"], cid))

    if approve:
        for _, cid in cand_ids:
            review.decide(conn, "task_move", cid, "승인", user_id, reason=WARN)  # 정식 승인 서비스 경로
    return {"case_id": case_id, "user_id": user_id, "candidates": cand_ids, "plans": plans, "approved": approve,
            "import_report": report}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.demo_seed", description="교육 시연용 DB 만들기 (시연용 가정 포함)")
    ap.add_argument("xlsx", help="환경부 엑셀 파일 경로 (읽기 전용으로만 연다)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="시연 DB 경로 (기본 data/demo.db, 운영 DB 경로는 거부)")
    ap.add_argument("--reset", action="store_true", help="이미 있는 시연 DB 를 지우고 다시 만든다")
    ap.add_argument("--approve", action="store_true", help="시연 계정으로 후보 2건을 승인해 화살표가 보이는 상태까지 만든다")
    ap.add_argument("--password-stdin", action="store_true", help="표준입력 첫 줄을 시연 계정 비밀번호로 사용")
    args = ap.parse_args(argv)
    try:
        target = prepare_target(args.db, args.reset)
        password, generated = _read_password(args)
        conn = db.connect(str(target))
        try:
            db.init_db(conn)
            out = seed(conn, args.xlsx, approve=args.approve, password=password)
        except Exception:
            conn.close()
            for suffix in ("", "-wal", "-shm", "-journal"):  # 실패한 반쪽짜리 시연 DB 는 남기지 않는다
                Path(str(target) + suffix).unlink(missing_ok=True)
            raise
        conn.close()
    except (DemoError, import_xlsx.ImportError_, auth.AuthError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    rep = out["import_report"]
    print("교육 시연용 DB를 만들었습니다.")
    print(f"  시연 DB: {target}")
    print(f"  시연 계정: {DEMO_LOGIN} ({DEMO_NAME})")
    if generated:
        print(f"  비밀번호(이번 한 번만 표시): {password}")
    else:
        print("  비밀번호: 지정한 값(DEMO_PASSWORD 또는 --password-stdin)")
    print("  접속: PowerShell 에서")
    print(f'    $env:APP_DB = "{target}"')
    print("    .\\.venv\\Scripts\\python -m uvicorn app.web:app")
    print("    브라우저: http://127.0.0.1:8000  → 로그인 후 '[교육 시연용]' 건을 열고 '계보도 보기'")
    print(f"  엑셀 가져오기: 기관 {rep['counts'].get('orgs_in_file')}건 (원본 변경 없음={rep['source_file_unchanged']})")
    print(f"  시연 건 #{out['case_id']} 1개, 업무 이동 후보 {len(out['candidates'])}건 — "
          + ("시연 계정이 승인함(계보도에 화살표 표시)" if out["approved"] else "승인 대기(발표 중 화면에서 직접 승인)"))
    for p in out["plans"]:
        q = p["quote"]
        print(f"    - {p['label']}: {p['from_code']} → {p['to_code']} | 범위 일부 · 날짜 미확인 | 엑셀 원문: {_norm(q['text'])[:40]}")
    print(f"  ※ 경고: 이 업무 이동은 {WARN}. 수집·변경 탐지 데이터는 만들지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
