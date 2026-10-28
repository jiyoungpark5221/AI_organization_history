"""확정 데이터 CSV 내보내기. 현재 유효한(is_current=1) 승인 결과만 포함한다."""
import csv
import io

HEADER = [
    "구분", "출발기관", "도착기관", "관계유형", "업무내용", "이동범위",
    "적용일", "종료일", "날짜확인상태", "근거", "승인자", "승인시각",
]


def _org_name(conn, org_id: int) -> str:
    row = conn.execute(
        "SELECT org_name FROM confirmed_org_info WHERE org_id=? AND is_current=1", (org_id,)
    ).fetchone()
    if row:
        return row["org_name"]
    row = conn.execute("SELECT org_name FROM org WHERE org_id=?", (org_id,)).fetchone()
    return row["org_name"] if row else ""


def _evidence_text(conn, candidate_type: str, candidate_id: int) -> str:
    items = []
    for r in conn.execute(
        "SELECT ls.law_name, lp.article_ref FROM evidence e "
        "JOIN law_provision lp ON lp.provision_id = e.provision_id "
        "JOIN law_source ls ON ls.law_source_id = lp.law_source_id "
        "WHERE e.candidate_type=? AND e.candidate_id=? ORDER BY e.evidence_id",
        (candidate_type, candidate_id),
    ):
        text = f"{r['law_name']} {r['article_ref']}"
        if text not in items:
            items.append(text)
    return "; ".join(items)


def export_csv(conn) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(HEADER)

    def clean(v):
        return "" if v is None else v

    for r in conn.execute(
        "SELECT r.*, u.display_name AS approver FROM confirmed_relation r "
        "LEFT JOIN app_user u ON u.user_id = r.approved_by "
        "WHERE r.is_current=1 ORDER BY r.approved_at, r.confirmed_id"
    ):
        writer.writerow([clean(v) for v in (
            "기관관계", _org_name(conn, r["from_org_id"]), _org_name(conn, r["to_org_id"]),
            r["relation_type"], "", "", r["valid_from"], r["valid_to"], r["date_status"],
            _evidence_text(conn, "relation", r["source_candidate_id"]),
            r["approver"], r["approved_at"],
        )])
    for r in conn.execute(
        "SELECT r.*, u.display_name AS approver FROM confirmed_task_move r "
        "LEFT JOIN app_user u ON u.user_id = r.approved_by "
        "WHERE r.is_current=1 ORDER BY r.approved_at, r.confirmed_id"
    ):
        writer.writerow([clean(v) for v in (
            "업무이동", _org_name(conn, r["from_org_id"]), _org_name(conn, r["to_org_id"]),
            "", r["task_description"], r["move_scope"], r["effective_date"], "", r["date_status"],
            _evidence_text(conn, "task_move", r["source_candidate_id"]),
            r["approver"], r["approved_at"],
        )])
    return out.getvalue()
