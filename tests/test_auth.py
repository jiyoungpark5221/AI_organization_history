"""로그인·세션·CSRF·사용자ID 자동 기록 시험 (가상 예시 데이터)."""
import re

import pytest
from fastapi.testclient import TestClient

from app import auth, review, web
from app.db import connect

from .web_helpers import USERS, csrf_from, login, make_users, post


@pytest.fixture
def ids(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "t.db"))
    web._initialized.clear()
    web._login_failures.clear()
    return make_users()


@pytest.fixture
def anon(ids):
    with TestClient(web.app) as c:
        yield c


@pytest.fixture
def hong(ids):
    with TestClient(web.app) as c:
        assert login(c, "hong").status_code == 303
        yield c


def db(sql, args=()):
    conn = connect()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def run_days(client):
    for day in ("2026-10-01", "2026-10-02"):
        assert post(client, "/settings/run", {"day": day}).status_code == 200


def candidates(client):
    out = []
    for cid in re.findall(r'href="/cases/(\d+)"', client.get("/", params={"status": "전체"}).text):
        html = client.get(f"/cases/{cid}").text
        out += [(int(cid), k, int(c)) for k, c in re.findall(r'id="c-(relation|task_move)-(\d+)"', html)]
    return out


def decide_url(case_id, kind, cand):
    return f"/cases/{case_id}/candidates/{kind}/{cand}/decide"


# ---------------------------------------------------------------- 접근 차단
def test_every_route_requires_login(anon):
    gets = ["/", "/cases/1", "/settings", "/export.csv"]
    for url in gets:
        r = anon.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), url
    posts = [decide_url(1, "relation", 1), "/settings", "/settings/upload-gcc", "/settings/run", "/logout"]
    for url in posts:
        r = anon.post(url, data={"action": "승인", "day": "2026-10-01"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), url
    assert db("SELECT COUNT(*) FROM review_log")[0][0] == 0
    assert db("SELECT COUNT(*) FROM collection_run")[0][0] == 0
    # 로그인 화면 자체는 열린다
    assert anon.get("/login").status_code == 200


def test_csv_not_downloadable_without_login(anon):
    r = anon.get("/export.csv", follow_redirects=False)
    assert r.status_code == 303 and "text/csv" not in r.headers.get("content-type", "")


# ---------------------------------------------------------------- 로그인
def test_login_success_and_generic_failure(anon):
    bad_pw = login(anon, "hong", "wrong-password")
    bad_id = login(anon, "nobody", "whatever-123")
    assert bad_pw.status_code == bad_id.status_code == 401
    msg = lambda r: re.search(r'class="alert">(.*?)</div>', r.text, re.S).group(1)
    assert msg(bad_pw) == msg(bad_id)  # 계정 존재 여부를 알려주지 않는다
    ok = login(anon, "hong")
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    page = anon.get("/")
    assert page.status_code == 200 and "홍길동" in page.text and "로그아웃" in page.text
    assert db("SELECT last_login_at FROM app_user WHERE login_id='hong'")[0][0]


def test_login_next_only_internal_paths(anon):
    for evil in ("//evil.example/x", "https://evil.example/", "/\\evil.example"):
        token = csrf_from(anon.get("/login", params={"next": evil}).text)
        r = anon.post("/login", data={"login_id": "hong", "password": USERS["hong"][1], "csrf_token": token,
                                      "next": evil}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", evil
        anon.post("/logout", data={"csrf_token": csrf_from(anon.get("/").text)})
    r = anon.get("/settings", follow_redirects=False)
    assert r.headers["location"] == "/login?next=/settings"
    token = csrf_from(anon.get("/login", params={"next": "/settings"}).text)
    r = anon.post("/login", data={"login_id": "hong", "password": USERS["hong"][1], "csrf_token": token,
                                  "next": "/settings"}, follow_redirects=False)
    assert r.headers["location"] == "/settings"


def test_session_cookie_flags_and_tampering(anon):
    token = csrf_from(anon.get("/login").text)
    r = anon.post("/login", data={"login_id": "hong", "password": USERS["hong"][1], "csrf_token": token},
                  follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    value = anon.cookies.get("session")
    payload, sig = value.rsplit(".", 1)
    anon.cookies.set("session", payload + "." + ("0" * len(sig)))
    assert anon.get("/", follow_redirects=False).status_code == 303  # 변조된 서명
    anon.cookies.set("session", "garbage")
    assert anon.get("/", follow_redirects=False).status_code == 303


def test_logout_clears_session(hong):
    r = hong.post("/logout", data={"csrf_token": hong.csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert hong.get("/", follow_redirects=False).status_code == 303


def test_login_throttle_after_repeated_failures(anon):
    for _ in range(web.MAX_LOGIN_FAILURES):
        assert login(anon, "hong", "wrong-password").status_code == 401
    assert login(anon, "hong").status_code == 401  # 올바른 비밀번호도 잠시 거부된다


# ---------------------------------------------------------------- 비활성 계정
def test_inactive_account_cannot_login_and_live_session_is_cut(ids, anon):
    with TestClient(web.app) as live:
        assert login(live, "kim").status_code == 303
        assert live.get("/").status_code == 200
        conn = connect()
        auth.deactivate_user(conn, "kim")
        conn.close()
        assert live.get("/", follow_redirects=False).status_code == 303   # 기존 세션도 즉시 차단
        assert live.get("/export.csv", follow_redirects=False).status_code == 303
    assert login(anon, "kim").status_code == 401                            # 새 로그인도 불가


# ---------------------------------------------------------------- 승인자 자동 기록
def test_approver_is_logged_in_user_and_cannot_be_forged(ids, hong):
    run_days(hong)
    cid, kind, cand = candidates(hong)[0]
    r = post(hong, decide_url(cid, kind, cand), {
        "action": "승인", "reviewer": "김철수", "reviewer_id": str(ids["kim"]), "approved_by": str(ids["kim"]),
        "user_id": str(ids["kim"]), "corrected_by": str(ids["kim"]),
    })
    assert "처리했습니다" in r.text
    assert db("SELECT reviewer_id FROM review_log")[0][0] == ids["hong"]
    table = "confirmed_relation" if kind == "relation" else "confirmed_task_move"
    assert {row[0] for row in db(f"SELECT approved_by FROM {table}")} == {ids["hong"]}
    assert {row[0] for row in db("SELECT approved_by FROM confirmed_org_info")} == {ids["hong"]}
    assert 'name="reviewer"' not in hong.get(f"/cases/{cid}").text  # 담당자명 입력칸 없음


def test_two_accounts_recorded_separately_and_csv_uses_display_name(ids, hong):
    run_days(hong)
    cands = candidates(hong)
    rel = next(c for c in cands if c[1] == "relation")
    mov = next(c for c in cands if c[1] == "task_move")
    with TestClient(web.app) as kim:
        assert login(kim, "kim").status_code == 303
        assert "처리했습니다" in post(hong, decide_url(*rel[:1], rel[1], rel[2]), {"action": "승인"}).text
        assert "처리했습니다" in post(kim, decide_url(mov[0], mov[1], mov[2]), {"action": "승인"}).text
        logs = {r[0]: r[1] for r in db("SELECT candidate_type, reviewer_id FROM review_log")}
        assert logs == {"relation": ids["hong"], "task_move": ids["kim"]}
        body = kim.get("/export.csv").content.decode("utf-8-sig")
        assert "홍길동" in body and "김철수" in body
        page = kim.get(f"/cases/{rel[0]}").text
        assert "홍길동" in page  # 처리 이력은 표시 이름으로 보인다


def test_web_approval_failure_rolls_back_keeps_edit_and_retry_succeeds(ids, hong, monkeypatch):
    run_days(hong)
    cid, kind, cand = next(c for c in candidates(hong) if c[1] == "relation")
    original = review._write_review_log
    monkeypatch.setattr(review, "_write_review_log",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("이력 기록 실패")))
    form = {"action": "수정 후 승인", "reason": "유형 정정", "relation_type": "정정된유형",
            "valid_from": "2026-10-05", "valid_to": "", "date_status": "확인"}
    r = post(hong, decide_url(cid, kind, cand), form)
    assert "저장하지 않았습니다" in r.text
    assert db("SELECT review_status FROM relation_candidate WHERE candidate_id=?", (cand,))[0][0] == "승인 대기"
    for t in ("confirmed_relation", "confirmed_org_info", "review_log"):
        assert db(f"SELECT COUNT(*) FROM {t}")[0][0] == 0, t
    assert "정정된유형" in db("SELECT draft_json FROM relation_candidate WHERE candidate_id=?", (cand,))[0][0]
    assert "정정된유형" in hong.get(f"/cases/{cid}").text  # 화면에 수정 입력이 다시 채워진다

    monkeypatch.setattr(review, "_write_review_log", original)
    assert "처리했습니다" in post(hong, decide_url(cid, kind, cand), form).text
    assert db("SELECT approved_by, relation_type FROM confirmed_relation")[0][:] == (ids["hong"], "정정된유형")
    assert db("SELECT reviewer_id FROM review_log")[0][0] == ids["hong"]


# ---------------------------------------------------------------- CSRF
def test_post_without_or_with_wrong_csrf_token_is_rejected(ids, hong):
    run_days(hong)
    cid, kind, cand = candidates(hong)[0]
    url = decide_url(cid, kind, cand)
    assert hong.post(url, data={"action": "승인"}).status_code == 403
    assert hong.post(url, data={"action": "승인", "csrf_token": "wrong"}).status_code == 403
    assert hong.post("/settings/run", data={"day": "2026-10-03"}).status_code == 403
    assert hong.post("/logout", data={}).status_code == 403
    assert db("SELECT COUNT(*) FROM review_log")[0][0] == 0
    assert hong.get("/", follow_redirects=False).status_code == 200  # 로그아웃되지 않았다


def test_login_post_requires_csrf_token(anon):
    r = anon.post("/login", data={"login_id": "hong", "password": USERS["hong"][1]}, follow_redirects=False)
    assert r.status_code == 403
    anon.get("/login")
    r = anon.post("/login", data={"login_id": "hong", "password": USERS["hong"][1], "csrf_token": "x"},
                  follow_redirects=False)
    assert r.status_code == 403


# ---------------------------------------------------------------- 비밀번호 저장
def test_password_not_stored_in_plain_text(ids):
    for login_id, (_, pw) in USERS.items():
        stored = db("SELECT password_hash FROM app_user WHERE login_id=?", (login_id,))[0][0]
        assert stored.startswith("scrypt$") and pw not in stored
        assert auth.verify_password(pw, stored) and not auth.verify_password(pw + "x", stored)
    a, b = (r[0] for r in db("SELECT password_hash FROM app_user ORDER BY user_id"))
    assert a != b
    conn = connect()
    uid = auth.create_user(conn, "same1", "같은비번1", "shared-password-9")
    uid2 = auth.create_user(conn, "same2", "같은비번2", "shared-password-9")
    hashes = [r[0] for r in conn.execute("SELECT password_hash FROM app_user WHERE user_id IN (?,?)", (uid, uid2))]
    conn.close()
    assert hashes[0] != hashes[1]  # 솔트가 달라 같은 비밀번호도 다른 해시


def test_user_management_rules(ids):
    conn = connect()
    with pytest.raises(auth.AuthError):
        auth.create_user(conn, "hong", "중복", "another-password-1")
    with pytest.raises(auth.AuthError):
        auth.create_user(conn, "short", "짧은비번", "1234")
    auth.set_password(conn, "hong", "new-password-123")
    assert auth.DbAuthProvider().authenticate(conn, "hong", "new-password-123")
    assert auth.DbAuthProvider().authenticate(conn, "hong", USERS["hong"][1]) is None
    auth.deactivate_user(conn, "hong")
    assert auth.DbAuthProvider().authenticate(conn, "hong", "new-password-123") is None
    assert conn.execute("SELECT COUNT(*) FROM app_user").fetchone()[0] == 2  # 삭제하지 않는다
    conn.close()


def test_secret_key_from_env_or_random_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "x.db"))
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    k1 = auth.secret_key()
    assert (tmp_path / ".secret_key").exists() and k1 == auth.secret_key() and len(k1) >= 32
    monkeypatch.setenv("APP_SECRET_KEY", "from-env")
    assert auth.secret_key() == b"from-env"
