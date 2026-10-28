"""일일 작업: GCC 수집 -> 변경 탐지 -> (신규 건) 법령 확보·AI 분석.

외부 스케줄러(cron, Windows 작업 스케줄러 등)가 `python -m app.daily` 를 호출한다.
"""
import argparse
import sys
from datetime import date

from . import adapters, analyze, collect as collect_mod
from .db import connect, init_db


def run(conn, day: str, gcc, law, llm) -> dict:
    """수집이 실패해도 대기 중인 분석의 재시도는 진행한다."""
    result: dict = {"day": day}
    try:
        result["collection"] = collect_mod.collect(conn, gcc, day)
    except Exception as e:  # 수집 모듈 내부 오류도 작업 전체를 멈추지 않는다.
        result["collection"] = {"status": "실패", "error": f"{type(e).__name__}: {e}"}
    try:
        result["analyzed_cases"] = analyze.process_pending(conn, law, llm)
    except Exception as e:
        result["analyzed_cases"] = []
        result["analysis_error"] = f"{type(e).__name__}: {e}"
    return result


def format_result(result: dict) -> str:
    c = result["collection"]
    lines = [f"[{result['day']}] 수집: {c.get('status')}"]
    if c.get("error"):
        lines.append(f"  수집 오류: {c['error']}")
    if c.get("is_baseline"):
        lines.append("  최초 수집본을 비교 기준본으로 등록했습니다.")
    elif c.get("status") == "성공":
        lines.append(f"  변경 {c.get('change_count', 0)}건, 신규 검토 건 {c.get('new_case_count', 0)}건")
        if not c.get("change_count"):
            lines.append("  변경 없음")
    lines.append(f"  분석 처리: {len(result.get('analyzed_cases', []))}건")
    if result.get("analysis_error"):
        lines.append(f"  분석 오류: {result['analysis_error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GCC 수집·변경 탐지·분석 일일 작업")
    parser.add_argument("--date", default=date.today().isoformat(), help="기준일 YYYY-MM-DD (기본: 오늘)")
    args = parser.parse_args(argv)
    conn = connect()
    try:
        init_db(conn)
        gcc, law, llm = adapters.from_settings(conn)
        result = run(conn, args.date, gcc, law, llm)
    finally:
        conn.close()
    print(format_result(result))
    return 0 if result["collection"].get("status") == "성공" and not result.get("analysis_error") else 1


if __name__ == "__main__":
    sys.exit(main())
