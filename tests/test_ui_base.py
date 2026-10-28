"""공통 디자인 시스템(base.html·_ui.html), 로그인·설정 화면 안내, 403 안내 화면 시험."""
import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import auth, web
from app.db import connect

from .web_helpers import csrf_from, login, make_users, post


@pytest.fixture
def ids(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "ui.db"))
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


def render(src: str, **ctx) -> str:
    return web.templates.env.from_string('{% from "_ui.html" import badge, steps, can_do, empty_state, '
                                         'error_box, loading, skeleton, toasts %}' + src).render(**ctx)


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> 짝 불일치 (열린 태그: {self.stack[-3:]})")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


def assert_balanced(html: str):
    p = _Balance()
    p.feed(html)
    p.close()
    assert not p.errors and not p.stack, (p.errors, p.stack)


# ---------------------------------------------------------------- 컴포넌트
def test_badges_have_icon_and_text_not_only_color():
    html = render("{% for k in ['src','ai','appr','unk','lack','conflict','fail'] %}{{ badge(k) }}{% endfor %}"
                  "{{ badge('unk', '날짜 미확인') }}")
    for label in ("원천 기재", "AI 제안(후보)", "승인 결과", "미확인", "자료 부족", "원천 충돌", "실패", "날짜 미확인"):
        assert label in html
    assert html.count('class="badge-icon" aria-hidden="true"') == 8
    for kind in ("src", "ai", "appr", "unk", "lack", "conflict", "fail"):
        assert f'class="badge {kind}"' in html
    assert_balanced(html)


def test_steps_component_states_and_current_step():
    html = render("{{ steps(['done','error','current','todo'], notes=['', '재시도 대기', '후보 2건', '']) }}")
    names = re.findall(r'class="step-name">\d\. (.*?)</span>', html)
    assert names == ["변경 탐지", "AI 분석", "담당자 검토", "확정 저장"]
    assert re.findall(r'class="step (\w+)"', html) == ["done", "error", "current", "todo"]
    assert html.count('aria-current="step"') == 1 and "후보 2건" in html and "실패 · 재시도 대기" in html
    assert render("{{ steps(['bogus']) }}").count('class="step todo"') == 4  # 알 수 없는 상태는 대기로
    assert_balanced(html)


def test_can_do_empty_error_loading_toast_markup():
    html = render("{{ can_do(['자동1'], ['승인1']) }}"
                  "{{ empty_state('새로운 변경사항이 없습니다', '변경이 없었습니다.', "
                  "[{'label':'지금 실행','href':'/settings#run','primary':true}]) }}"
                  "{{ error_box('연결 실패', retry_href='/x') }}{{ error_box('b', retry_form='f1') }}{{ error_box('c') }}"
                  "{{ loading('불러오는 중') }}{{ skeleton(2) }}"
                  "{{ toasts(ok='저장했습니다', error='문제', ok_link=('/', '목록에서 확인')) }}")
    assert 'class="can-do"' in html and "시스템이 자동으로 하는 것" in html and "담당자 승인이 필요한 것" in html
    assert 'class="empty-state"' in html and 'class="btn primary" href="/settings#run"' in html
    assert html.count('class="error-box" role="alert"') == 3
    assert 'href="/x"' in html and 'data-retry-form="f1"' in html and "data-retry>" in html
    assert 'class="loading" role="status" aria-live="polite"' in html and html.count("<span></span>") == 2
    assert 'class="toast-region" aria-live="polite"' in html and 'class="toast error" role="alert"' in html
    assert "목록에서 확인" in html and html.count("data-dismiss") == 2
    assert_balanced(html)


# ---------------------------------------------------------------- 공통 레이아웃
def test_base_layout_has_tokens_skip_link_once_script_and_current_nav(hong):
    html = hong.get("/settings").text
    assert "--on-accent" in html and "prefers-color-scheme: dark" in html and ":focus-visible" in html
    assert "prefers-reduced-motion" in html
    assert 'class="skip" href="#main"' in html and 'id="main"' in html
    assert "form[data-once]" in html and "pageshow" in html and "data-once-copy" in html
    assert re.search(r'href="/settings" class="active" aria-current="page"', html)
    assert "홍길동" in html and "로그아웃" in html
    assert_balanced(html)


def test_list_and_case_pages_still_render_balanced(hong):
    assert_balanced(hong.get("/").text)


# ---------------------------------------------------------------- 로그인 안내
def test_login_page_guides_account_request_and_next_screen(anon):
    first = anon.get("/settings", follow_redirects=False)
    assert first.headers["location"] == "/login?next=/settings"           # 처음 접속은 이유 없이
    page = anon.get("/login", params={"next": "/settings"}).text
    assert "로그인한 담당자만 볼 수 있습니다" in page and "계정은 관리자에게 요청" in page
    assert "python -m app.users create" in page and "data-once" in page
    assert "로그인 후 시스템이 해 두는 일" in page
    assert "로그인한 담당자만" not in anon.get("/login").text            # 그냥 연 로그인 화면엔 안내 없음
    assert_balanced(page)


def test_failed_login_message_is_generic(anon):
    bad_pw, bad_id = login(anon, "hong", "wrong-password"), login(anon, "nobody", "whatever-123")
    msg = lambda r: re.search(r'class="alert">(.*?)</div>', r.text, re.S).group(1)
    assert msg(bad_pw) == msg(bad_id) and "hong" not in msg(bad_pw)


def test_expired_session_redirects_with_reason_and_shows_relogin_message(ids, anon):
    assert login(anon, "kim").status_code == 303
    conn = connect()
    auth.deactivate_user(conn, "kim")
    conn.close()
    r = anon.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/settings&reason=expired"
    page = anon.get(r.headers["location"]).text
    assert "다시 로그인해 주세요" in page
    assert "다시 로그인해 주세요" not in anon.get("/login", params={"reason": "zzz"}).text


# ---------------------------------------------------------------- 403 안내 화면
def test_csrf_rejection_shows_refresh_guide_with_403(hong):
    r = hong.post("/settings/run", data={"day": "2026-10-01"},
                  headers={"referer": "http://testserver/settings?x=1"})
    assert r.status_code == 403
    assert "화면이 오래되어 다시 시도해 주세요" in r.text and "아무것도 저장되지 않았습니다" in r.text
    assert 'href="/settings?x=1"' in r.text and "새로고침해서 다시 시도" in r.text
    assert_balanced(r.text)
    evil = hong.post("/settings/run", data={"day": "2026-10-01"}, headers={"referer": "https://evil.example/x"})
    assert evil.status_code == 403 and 'class="btn primary" href="/"' in evil.text
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM collection_run").fetchone()[0] == 0
    conn.close()


def test_login_csrf_rejection_also_uses_guide_page(anon):
    r = anon.post("/login", data={"login_id": "hong", "password": "x"})
    assert r.status_code == 403 and "화면이 오래되어" in r.text and "로그인 화면으로" in r.text


def test_other_http_errors_keep_default_handler(hong):
    r = hong.get("/cases/999999")
    assert r.status_code == 404 and "해당 검토 건이 없습니다" in r.text and "화면이 오래되어" not in r.text


# ---------------------------------------------------------------- 설정 화면
def test_settings_two_column_guide_and_once_forms(hong):
    html = hong.get("/settings").text
    assert "자동으로 처리되는 일" in html and "사람이 직접 해야 하는 일" in html
    assert html.count("data-once") >= 4  # 저장·업로드·실행 + 로그아웃 아닌 폼 3개 + 스크립트 선택자
    for fid in ('id="save-form"', 'id="upload-form"', 'id="run"'):
        assert fid in html
    assert html.count("data-loading hidden") == 2


def test_settings_error_shows_in_failing_card_with_retry(hong):
    r = post(hong, "/settings", {"schedule_time": "25:99", "gcc_dir": "a", "law_dir": "b", "llm_dir": "c"})
    assert "설정을 저장하지 못했습니다" in r.text and 'data-retry-form="save-form"' in r.text and "HH:MM" in r.text
    assert 'class="toast error"' not in r.text                         # 카드 안에만 보여 중복 표시하지 않는다
    r = post(hong, "/settings/run", {"day": "2026-01-01"})           # 해당 날짜 자료 없음 → 수집 실패
    assert "실행 중 문제가 있었습니다" in r.text and 'data-retry-form="run"' in r.text
    assert re.search(r'id="run_day" name="day" placeholder="2026-10-02" value="2026-01-01"', r.text)


def test_settings_success_toast_links_to_next_step(hong, tmp_path):
    gcc_dir = tmp_path / "gcc"
    r = post(hong, "/settings", {"schedule_time": "06:30", "gcc_dir": str(gcc_dir), "law_dir": "b", "llm_dir": "c"})
    assert 'class="toast ok"' in r.text and "반영되었습니다" in r.text
    r = post(hong, "/settings/upload-gcc", {"day": "2026-10-01"},
             files={"file": ("d.json", b'{"complete": true, "records": []}', "application/json")})
    assert "저장했습니다" in r.text and 'href="#run">지금 실행으로 이동' in r.text
    assert re.search(r'id="run_day" name="day" placeholder="2026-10-02" value="2026-10-01"', r.text)


def test_settings_query_values_are_sanitized(hong):
    html = hong.get("/settings", params={"form": "evil", "day": "<script>", "error": "x"}).text
    main = html.split('<main id="main"')[1].split("</main>")[0]
    assert "<script>" not in main and "&lt;script&gt;" not in main     # 형식에 맞지 않는 날짜는 버린다
    assert 'class="toast error"' in main and "error-box" not in main  # 알 수 없는 form 이면 상단 알림으로


def test_case_page_with_data_renders_balanced(hong):
    for day in ("2026-10-01", "2026-10-02"):
        post(hong, "/settings/run", {"day": day})
    cid = re.search(r'href="/cases/(\d+)"', hong.get("/", params={"status": "전체"}).text).group(1)
    assert_balanced(hong.get(f"/cases/{cid}").text)
    assert_balanced(hong.get("/", params={"status": "전체"}).text)


# ---------------------------------------------------------------- 샘플 배포 안내 배너
BANNER = "샘플 배포 화면입니다"


def test_sample_banner_hidden_on_local_run(ids, monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("APP_SAMPLE_BOOTSTRAP", raising=False)
    with TestClient(web.app) as c:
        assert BANNER not in c.get("/login").text
        login(c, "hong")
        assert BANNER not in c.get("/settings").text


@pytest.mark.parametrize("env", [{"APP_SAMPLE_BOOTSTRAP": "1"}, {"VERCEL": "1"}])
def test_sample_banner_shown_on_sample_deploy_including_login(ids, monkeypatch, env):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("APP_SAMPLE_BOOTSTRAP", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with TestClient(web.app) as c:
        page = c.get("/login").text
        assert BANNER in page and 'class="sample-banner" role="note"' in page
        assert "서버가 다시 시작되면 사라지고" in page and "로컬 실행 환경" in page
        assert "data-dismiss" not in page.split('class="sample-banner"')[1].split("</div>")[0]  # 닫기 없음
        assert page.index('class="sample-banner"') > page.index('<header class="top">')
        assert page.index('class="sample-banner"') < page.index('<main id="main"')
        login(c, "hong")
        assert BANNER in c.get("/settings").text
        assert_balanced(page)


def test_sample_banner_off_when_bootstrap_flag_is_not_1(ids, monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("APP_SAMPLE_BOOTSTRAP", "0")
    with TestClient(web.app) as c:
        assert BANNER not in c.get("/login").text
