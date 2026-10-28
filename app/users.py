"""계정 관리 명령. 비밀번호는 명령행 인자로 받지 않는다.

  python -m app.users create <login_id> <display_name>   (비밀번호: 프롬프트 / APP_USER_PASSWORD / --password-stdin)
  python -m app.users set-password <login_id>
  python -m app.users deactivate <login_id>
"""
import argparse
import getpass
import os
import sys

from . import auth
from .db import connect, init_db


def _read_password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    env = os.environ.get("APP_USER_PASSWORD")
    if env:
        return env
    first = getpass.getpass("비밀번호: ")
    if first != getpass.getpass("비밀번호 확인: "):
        raise auth.AuthError("비밀번호가 서로 다릅니다.")
    return first


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.users")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("login_id")
    c.add_argument("display_name")
    s = sub.add_parser("set-password")
    s.add_argument("login_id")
    d = sub.add_parser("deactivate")
    d.add_argument("login_id")
    for p in (c, s):
        p.add_argument("--password-stdin", action="store_true", help="표준입력 첫 줄을 비밀번호로 사용")
    args = parser.parse_args(argv)

    conn = connect()
    init_db(conn)
    try:
        if args.cmd == "create":
            uid = auth.create_user(conn, args.login_id, args.display_name, _read_password(args))
            print(f"계정을 만들었습니다: {args.login_id} (user_id={uid})")
        elif args.cmd == "set-password":
            auth.set_password(conn, args.login_id, _read_password(args))
            print("비밀번호를 변경했습니다.")
        else:
            auth.deactivate_user(conn, args.login_id)
            print("계정을 비활성화했습니다. 과거 기록은 보존됩니다.")
    except auth.AuthError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
