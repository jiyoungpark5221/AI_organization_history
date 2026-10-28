"""변경·승인 목록, 변경 상세·승인, 설정 화면. 모든 화면은 로그인이 필요하다."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import adapters, auth, daily, lineage, review
from .db import DEFAULT_SETTINGS, connect, get_setting, init_db, set_setting
from .export import export_csv

app = FastAPI(title="기관 연혁·업무 변동 승인 도구")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

FILTERS = ["승인 대기", "보류", "승인 완료", "기각", "전체"]
KINDS = {"relation": "기관관계 후보", "task_move": "업무 이동 후보"}
DATE_STATUS = ["확인", "미확인", "원천 충돌"]
MOVE_SCOPE = ["전체", "일부", "범위 미확인"]
FIELD_LABELS = {
    "relation_type": "관계 유형",
    "valid_from": "적용일(시작)",
    "valid_to": "종료일",
    "task_description": "이동한 업무 내용",
    "move_scope": "이동 범위",
    "effective_date": "적용일",
    "date_status": "날짜 확인 상태",
}
FIELD_OPTIONS = {"date_status": DATE_STATUS, "move_scope": MOVE_SCOPE}
CAND_TABLE = {"relation": "relation_candidate", "task_move": "task_move_candidate"}

_initialized: set[str] = set()


def _open():
    from .db import db_path

    conn = connect()
    path = db_path()
    if path not in _initialized:
        init_db(conn)
        if path != ":memory:":
            _initialized.add(path)
    return conn


async def get_conn():
    # SQLite 연결은 만든 스레드에서만 쓸 수 있으므로, 연결을 쓰는 엔드포인트는 모두 async 로 두어
    # 이벤트 루프 스레드 한 곳에서 처리한다. (작은 내부 도구라 짧은 DB 호출은 그대로 둔다.)
    conn = _open()
    try:
        yield conn
    finally:
        conn.close()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _case_url(case_id: int, key: str, msg: str, anchor: str = "") -> str:
    return f"/cases/{case_id}?{key}={quote(msg)}{anchor}"

# ---------------------------------------------------------------- 로그인 세션
# 세션은 서명된 쿠키(HttpOnly, SameSite=Lax)이며 user_id 와 CSRF 토큰, 발급 시각만 담는다.
# 사용자 활성 여부는 매 요청마다 DB 에서 확인하므로 비활성화된 계정은 즉시 차단된다.
SESSION_COOKIE = "session"
SESSION_MAX_AGE = 12 * 60 * 60
MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 60
LOGIN_ERROR = "로그인 ID 또는 비밀번호가 올바르지 않거나 잠시 후 다시 시도해야 합니다."

auth_provider: auth.AuthProvider = auth.DbAuthProvider()
# 연속 실패 카운터(프로세스 메모리). 서버를 여러 개 띄우거나 재시작하면 공유·유지되지 않는다.
_login_failures: dict[str, tuple[int, float]] = {}


class NotAuthenticated(Exception):
    def __init__(self, next_url: str = "/", had_session: bool = False):
        self.next_url = next_url
        self.had_session = had_session


def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    sig = hmac.new(auth.secret_key(), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def _read_session(request: Request) -> dict | None:
    value = request.cookies.get(SESSION_COOKIE)
    if not value:
        return None
    try:
        raw, sig = value.rsplit(".", 1)
        expected = hmac.new(auth.secret_key(), raw.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw))
        if time.time() - data["iat"] > SESSION_MAX_AGE or not isinstance(data["csrf"], str):
            return None
        return data
    except Exception:  # 형식이 잘못되었거나 변조된 쿠키는 세션이 없는 것으로 본다.
        return None


def _start_session(response, request: Request, uid: int | None, csrf: str | None = None) -> str:
    csrf = csrf or secrets.token_urlsafe(24)
    response.set_cookie(
        SESSION_COOKIE, _sign({"uid": uid, "csrf": csrf, "iat": int(time.time())}),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )
    return csrf


def _safe_next(value: str | None) -> str:
    """로그인 후 복귀 경로는 이 사이트 안의 경로만 허용한다."""
    v = value or "/"
    if not v.startswith("/") or v.startswith("//") or "\\" in v or "\r" in v or "\n" in v:
        return "/"
    return v


async def require_user(request: Request, conn=Depends(get_conn)):
    sess = _read_session(request)
    user = auth.get_active_user(conn, sess.get("uid")) if sess else None
    if user is None:
        nxt = request.url.path + ("?" + request.url.query if request.url.query else "")
        raise NotAuthenticated(nxt if request.method == "GET" else "/", bool(sess and sess.get("uid")))
    request.state.user = user
    request.state.csrf = sess["csrf"]
    return user


def _csrf_ok(form, expected: str) -> bool:
    token = str(form.get("csrf_token") or "")
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


async def require_user_csrf(request: Request, user=Depends(require_user)):
    form = await request.form()
    if not _csrf_ok(form, request.state.csrf):
        raise HTTPException(status_code=403, detail="요청을 확인할 수 없습니다(CSRF). 화면을 새로 고친 뒤 다시 시도하세요.")
    return user


@app.exception_handler(NotAuthenticated)
async def _not_authenticated(request: Request, exc: NotAuthenticated):
    resp = _redirect("/login?next=" + quote(exc.next_url, safe="/"))
    if exc.had_session:
        resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _locked(login_id: str) -> bool:
    count, last = _login_failures.get(login_id.lower(), (0, 0.0))
    return count >= MAX_LOGIN_FAILURES and time.time() - last < LOGIN_LOCK_SECONDS


def _record_failure(login_id: str) -> None:
    count, last = _login_failures.get(login_id.lower(), (0, 0.0))
    if count >= MAX_LOGIN_FAILURES and time.time() - last >= LOGIN_LOCK_SECONDS:
        count = 0
    _login_failures[login_id.lower()] = (count + 1, time.time())


def _login_page(request: Request, csrf: str, next_url: str, error: str = "", status_code: int = 200):
    request.state.user = None
    request.state.csrf = csrf
    return templates.TemplateResponse(request, "login.html", {"next_url": next_url, "error": error},
                                      status_code=status_code)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/", conn=Depends(get_conn)):
    sess = _read_session(request)
    if sess and auth.get_active_user(conn, sess.get("uid")):
        return _redirect(_safe_next(next))
    resp = _login_page(request, sess["csrf"] if sess else secrets.token_urlsafe(24), _safe_next(next))
    if not sess:
        _start_session(resp, request, None, request.state.csrf)
    return resp


@app.post("/login")
async def login_submit(request: Request, conn=Depends(get_conn)):
    form = await request.form()
    sess = _read_session(request)
    if not sess or not _csrf_ok(form, sess["csrf"]):
        raise HTTPException(status_code=403, detail="요청을 확인할 수 없습니다(CSRF). 로그인 화면을 새로 고친 뒤 다시 시도하세요.")
    login_id = str(form.get("login_id") or "").strip()
    password = str(form.get("password") or "")
    next_url = _safe_next(str(form.get("next") or "/"))
    user = None if _locked(login_id) else auth_provider.authenticate(conn, login_id, password)
    if user is None:
        _record_failure(login_id)
        return _login_page(request, sess["csrf"], next_url, LOGIN_ERROR, status_code=401)
    _login_failures.pop(login_id.lower(), None)
    resp = _redirect(next_url)
    _start_session(resp, request, user["user_id"])  # 로그인할 때마다 세션·CSRF 토큰을 새로 발급한다.
    return resp


@app.post("/logout")
async def logout(user=Depends(require_user_csrf)):
    resp = _redirect("/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp



# ---------------------------------------------------------------- 화면 1
@app.get("/", response_class=HTMLResponse)
async def case_list(request: Request, status: str = "승인 대기", q: str = "", conn=Depends(get_conn),
                    user=Depends(require_user)):
    if status not in FILTERS:
        status = "승인 대기"
    cases = conn.execute("SELECT * FROM review_case ORDER BY detected_at DESC, case_id DESC").fetchall()
    rows, pending = [], 0
    for c in cases:
        state = review.case_review_state(conn, c["case_id"])
        if state == "승인 대기":
            pending += 1
        if status != "전체" and state != status:
            continue
        if q and q.strip() not in c["title"]:
            continue
        badges = []
        if c["data_status"] != "정상":
            badges.append((c["data_status"], "warn"))
        if c["analysis_status"] != "완료":
            badges.append(("분석 " + c["analysis_status"], "bad" if c["analysis_status"] == "실패" else "info"))
        rows.append({"case": c, "state": state, "badges": badges})

    last = conn.execute("SELECT * FROM collection_run ORDER BY collection_id DESC LIMIT 1").fetchone()
    failed = conn.execute("SELECT COUNT(*) FROM review_case WHERE analysis_status='실패'").fetchone()[0]
    running = conn.execute("SELECT COUNT(*) FROM review_case WHERE analysis_status='분석 중'").fetchone()[0]
    waiting = conn.execute("SELECT COUNT(*) FROM review_case WHERE analysis_status='대기'").fetchone()[0]
    alerts = []
    if last and last["status"] != "성공":
        alerts.append(f"GCC 수집 {last['status']} ({last['base_date']}): {last['error_message'] or '사유 없음'} "
                      "— 마지막 정상 수집본을 유지하고 있습니다.")
    if failed:
        alerts.append(f"AI 분석 실패 {failed}건: 재시도 대상으로 남아 있습니다.")
    if running:
        alerts.append(f"분석이 끝나지 않은 건 {running}건(분석 중 상태): 중단되었는지 확인하세요.")
    return templates.TemplateResponse(request, "list.html", {
        "rows": rows, "status": status, "q": q, "filters": FILTERS, "pending": pending,
        "last": last, "alerts": alerts, "failed": failed, "running": running, "waiting": waiting,
        "total_cases": len(cases),
    })


# ---------------------------------------------------------------- 화면 2
def _candidate_view(conn, kind: str, row) -> dict:
    cid = row["candidate_id"]
    evidences = conn.execute(
        "SELECT e.excerpt, e.reason, p.article_ref, p.text, p.is_addendum, "
        "       l.law_name, l.role, l.effective_date, l.source_url "
        "FROM evidence e JOIN law_provision p ON p.provision_id=e.provision_id "
        "JOIN law_source l ON l.law_source_id=p.law_source_id "
        "WHERE e.candidate_type=? AND e.candidate_id=? ORDER BY e.evidence_id", (kind, cid)).fetchall()
    logs = conn.execute(
        "SELECT l.*, u.display_name AS reviewer_name FROM review_log l "
        "LEFT JOIN app_user u ON u.user_id = l.reviewer_id "
        "WHERE l.candidate_type=? AND l.candidate_id=? ORDER BY l.review_id",
        (kind, cid)).fetchall()
    values = review.candidate_values(row)
    fields = [{"name": f, "label": FIELD_LABELS.get(f, f), "value": values.get(f) or "",
               "options": FIELD_OPTIONS.get(f)} for f in review.EDITABLE[kind]]
    return {"kind": kind, "kind_label": KINDS[kind], "row": row, "fields": fields, "evidences": evidences,
            "logs": logs, "has_draft": bool(row["draft_json"]),
            "can_decide": row["review_status"] in ("승인 대기", "보류")}


@app.get("/cases/{case_id}", response_class=HTMLResponse)
async def case_detail(request: Request, case_id: int, error: str = "", ok: str = "", conn=Depends(get_conn),
                      user=Depends(require_user)):
    case = conn.execute("SELECT * FROM review_case WHERE case_id=?", (case_id,)).fetchone()
    if case is None:
        return HTMLResponse("해당 검토 건이 없습니다.", status_code=404)
    changes = conn.execute("SELECT * FROM change_event WHERE case_id=? ORDER BY change_id", (case_id,)).fetchall()
    law_versions = []
    for ls in conn.execute("SELECT * FROM law_source WHERE case_id=? ORDER BY role DESC, law_source_id",
                           (case_id,)).fetchall():
        provs = conn.execute("SELECT * FROM law_provision WHERE law_source_id=? ORDER BY provision_id",
                             (ls["law_source_id"],)).fetchall()
        law_versions.append({"src": ls, "provisions": provs})
    cands = []
    for kind, table in CAND_TABLE.items():
        for row in conn.execute(f"SELECT * FROM {table} WHERE case_id=? ORDER BY candidate_id", (case_id,)):
            cands.append(_candidate_view(conn, kind, row))
    return templates.TemplateResponse(request, "case.html", {
        "case": case, "changes": changes, "law_versions": law_versions, "cands": cands,
        "state": review.case_review_state(conn, case_id), "error": error, "ok": ok,
    })


@app.get("/cases/{case_id}/lineage", response_class=HTMLResponse)
async def case_lineage(request: Request, case_id: int, org: str = "", q: str = "", conn=Depends(get_conn),
                       user=Depends(require_user)):
    """기능 담당 조직 계보도 조각(교육 발표용, 읽기 전용). 상세 화면의 '계보도 보기' 패널이 불러온다."""
    case = conn.execute("SELECT * FROM review_case WHERE case_id=?", (case_id,)).fetchone()
    if case is None:
        return HTMLResponse("해당 검토 건이 없습니다.", status_code=404)
    selected = int(org) if org.strip().isdigit() else None
    g = lineage.build(conn, org_id=selected) if selected is not None else lineage.build(conn, case=case)
    if g["root"] is not None:
        lineage.layout(g)
    results = lineage.search_orgs(conn, q) if q.strip() else None
    return templates.TemplateResponse(request, "_lineage.html", {
        "case": case, "g": g, "q": q, "results": results, "selected_org": selected,
    })


@app.post("/cases/{case_id}/candidates/{kind}/{cid}/decide")
async def decide(request: Request, case_id: int, kind: str, cid: int, conn=Depends(get_conn),
                 user=Depends(require_user_csrf)):
    form = await request.form()
    action = (form.get("action") or "").strip()
    reason = (form.get("reason") or "").strip()
    anchor = f"#c-{kind}-{cid}"
    if kind not in CAND_TABLE:
        return _redirect(_case_url(case_id, "error", "알 수 없는 후보 유형입니다."))
    edits = None
    if action == "수정 후 승인":
        edits = {f: ((form.get(f) or "").strip() or None) for f in review.EDITABLE[kind]}
    try:
        # 처리자는 세션의 로그인 계정으로만 정해진다. 폼 값으로 사용자를 지정하는 경로는 없다.
        review.decide(conn, kind, cid, action, user["user_id"], reason=reason, edits=edits)
        resp = _redirect(_case_url(case_id, "ok", f"{KINDS[kind]} #{cid}: {action} 처리했습니다.", anchor))
    except review.ReviewError as e:
        resp = _redirect(_case_url(case_id, "error", str(e), anchor))
    except Exception as e:  # 저장 실패 시 500 대신 안내하고 승인 대기 상태를 유지한다.
        resp = _redirect(_case_url(case_id, "error", f"처리 중 오류가 발생해 저장하지 않았습니다: {e}", anchor))
    return resp


@app.get("/export.csv")
async def export(conn=Depends(get_conn), user=Depends(require_user)):
    body = "﻿" + export_csv(conn)
    return Response(content=body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="approved_results.csv"'})


# ---------------------------------------------------------------- 설정
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, error: str = "", ok: str = "", conn=Depends(get_conn),
                        user=Depends(require_user)):
    values = {k: get_setting(conn, k) for k in DEFAULT_SETTINGS}
    return templates.TemplateResponse(request, "settings.html", {"s": values, "error": error, "ok": ok})


@app.post("/settings")
async def settings_save(schedule_time: str = Form(...), gcc_dir: str = Form(...), law_dir: str = Form(...),
                  llm_dir: str = Form(...), conn=Depends(get_conn),
                        user=Depends(require_user_csrf)):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", schedule_time.strip()):
        return _redirect("/settings?error=" + quote("수집 시각은 HH:MM 형식으로 입력하세요."))
    for k, v in (("schedule_time", schedule_time), ("gcc_dir", gcc_dir), ("law_dir", law_dir), ("llm_dir", llm_dir)):
        if not v.strip():
            return _redirect("/settings?error=" + quote("비어 있는 설정이 있습니다."))
        set_setting(conn, k, v.strip())
    return _redirect("/settings?ok=" + quote("설정을 저장했습니다."))


@app.post("/settings/upload-gcc")
async def upload_gcc(day: str = Form(...), file: UploadFile = File(...), conn=Depends(get_conn),
                     user=Depends(require_user_csrf)):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.strip()):
        return _redirect("/settings?error=" + quote("날짜는 YYYY-MM-DD 형식으로 입력하세요."))
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise ValueError('최상위에 "records" 목록이 있어야 합니다.')
    except (UnicodeDecodeError, ValueError) as e:  # JSONDecodeError 는 ValueError 의 하위 클래스
        return _redirect("/settings?error=" + quote(f"파일 형식 오류: {e}"))
    target = Path(get_setting(conn, "gcc_dir"))
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{day.strip()}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return _redirect("/settings?ok=" + quote(f"{day.strip()} 자료를 저장했습니다. '지금 실행'으로 수집하세요."))


@app.post("/settings/run")
def run_now(day: str = Form(...), user=Depends(require_user_csrf)):
    # 분석(향후 LLM 호출)이 오래 걸릴 수 있어 스레드풀에서 실행하며 자체 연결을 쓴다.
    day = day.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return _redirect("/settings?error=" + quote("날짜는 YYYY-MM-DD 형식으로 입력하세요."))
    conn = _open()
    try:
        gcc, law, llm = adapters.from_settings(conn)
        result = daily.run(conn, day, gcc, law, llm)
    finally:
        conn.close()
    text = daily.format_result(result).replace("\n", " / ")
    failed = result["collection"].get("status") != "성공" or result.get("analysis_error")
    return _redirect("/settings?" + ("error=" if failed else "ok=") + quote(text))
