"""웹 시험 공용 도우미: 계정 만들기, 로그인, CSRF 토큰이 붙은 POST."""
import re

from app import auth
from app.db import connect, init_db

USERS = {"hong": ("홍길동", "hong-password-1"), "kim": ("김철수", "kim-password-22")}


def make_users() -> dict[str, int]:
    conn = connect()
    init_db(conn)
    ids = {login: auth.create_user(conn, login, name, pw) for login, (name, pw) in USERS.items()}
    conn.close()
    return ids


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "CSRF 토큰 입력칸이 없습니다."
    return m.group(1)


def login(client, login_id: str, password: str | None = None):
    password = password if password is not None else USERS[login_id][1]
    token = csrf_from(client.get("/login").text)
    r = client.post("/login", data={"login_id": login_id, "password": password, "csrf_token": token},
                    follow_redirects=False)
    if r.status_code == 303:
        client.csrf = csrf_from(client.get("/").text)  # 화면 상단 로그아웃 폼의 토큰
    return r


def post(client, url: str, data: dict | None = None, **kw):
    return client.post(url, data={**(data or {}), "csrf_token": client.csrf}, **kw)
