"""GCC 수집과 변경 탐지.

- 실패·불완전한 수집은 gcc_record 를 저장하지 않으므로 비교 기준본으로 쓰이지 않는다.
- 최초 성공 수집은 기준본으로만 등록하고 변경 탐지는 하지 않는다.
- 목록에서 사라진 코드는 '목록에서 사라짐'으로만 기록하며 폐지로 확정하지 않는다.
"""
import hashlib
import json

from .db import now, tx

# gcc_record 필드 -> change_type
FIELD_TYPES = {
    "org_name": "명칭",
    "parent_code": "소속",
    "prev_org_code": "이전기관",
    "created_date": "생성 정보",
    "abolished_date": "폐지 정보",
}
RECORD_FIELDS = ["gcc_code", *FIELD_TYPES]


def change_key(code: str, field: str, old, new) -> str:
    raw = f"{code}|{field}|{'' if old is None else old}|{'' if new is None else new}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _norm(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _validate(result) -> tuple[list[dict] | None, str | None]:
    """-> (정규화된 레코드, 불완전 사유). 사유가 있으면 레코드는 None."""
    if not isinstance(result, dict) or not isinstance(result.get("records"), list):
        return None, "응답 형식 오류: records 없음"
    if not result.get("complete"):
        return None, "불완전한 자료: complete=false"
    records, seen = [], set()
    for i, rec in enumerate(result["records"]):
        if not isinstance(rec, dict):
            return None, f"레코드 {i}: 객체가 아님"
        norm = {k: _norm(rec.get(k)) for k in RECORD_FIELDS}
        if not norm["gcc_code"] or not norm["org_name"]:
            return None, f"레코드 {i}: gcc_code/org_name 비어있음"
        if norm["gcc_code"] in seen:
            return None, f"gcc_code 중복: {norm['gcc_code']}"
        seen.add(norm["gcc_code"])
        records.append(norm)
    return records, None


def _load(conn, collection_id: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT * FROM gcc_record WHERE collection_id=?", (collection_id,)
    ).fetchall()
    return {r["gcc_code"]: dict(r) for r in rows}


def _diff(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    changes = []
    for code in sorted(after.keys() - before.keys()):
        a = after[code]
        changes.append({
            "gcc_code": code, "change_type": "신설", "field_name": "기관",
            "old_value": None,
            "new_value": f"{a['org_name']} (상위: {a['parent_code'] or '없음'}, 이전기관: {a['prev_org_code'] or '없음'})",
        })
    for code in sorted(before.keys() & after.keys()):
        for field, ctype in FIELD_TYPES.items():
            if before[code][field] != after[code][field]:
                changes.append({
                    "gcc_code": code, "change_type": ctype, "field_name": field,
                    "old_value": before[code][field], "new_value": after[code][field],
                })
    for code in sorted(before.keys() - after.keys()):
        changes.append({
            "gcc_code": code, "change_type": "목록에서 사라짐", "field_name": "기관",
            "old_value": before[code]["org_name"], "new_value": None,
        })
    for c in changes:
        c["change_key"] = change_key(c["gcc_code"], c["field_name"], c["old_value"], c["new_value"])
    return changes


def _group(changes: list[dict], before: dict, after: dict) -> list[list[dict]]:
    """prev_org_code 로 서로 연결된 변경된 코드끼리 한 건으로 묶는다(union-find)."""
    codes = sorted({c["gcc_code"] for c in changes})
    parent = {c: c for c in codes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for code in codes:
        rec = after.get(code) or before.get(code)
        prev = rec["prev_org_code"] if rec else None
        if prev in parent:
            parent[find(code)] = find(prev)

    groups: dict[str, list[dict]] = {}
    for c in changes:
        groups.setdefault(find(c["gcc_code"]), []).append(c)
    return sorted(groups.values(), key=lambda g: min(c["gcc_code"] for c in g))


def _summary(c: dict, before: dict, after: dict) -> str:
    code = c["gcc_code"]
    name = (before.get(code) or after.get(code))["org_name"]
    if c["change_type"] == "신설":
        return f"{after[code]['org_name']}({code}) 신설"
    if c["change_type"] == "목록에서 사라짐":
        return f"{name}({code}) 목록에서 사라짐"
    old = c["old_value"] if c["old_value"] is not None else "(없음)"
    new = c["new_value"] if c["new_value"] is not None else "(없음)"
    return f"{name}({code}) {c['change_type']}: {old} → {new}"


def collect(conn, gcc, day: str) -> dict:
    started = now()
    error = None
    status = "성공"
    records = None
    try:
        records, why = _validate(gcc.fetch(day))
        if why:
            status, error = "불완전", why
    except Exception as e:  # SourceError 포함: 어떤 수집 실패든 기록하고 계속한다
        status, error = "실패", f"{type(e).__name__}: {e}"

    with tx(conn):
        prev = None
        if status == "성공":
            prev = conn.execute(
                "SELECT collection_id FROM collection_run WHERE status='성공' "
                "ORDER BY collection_id DESC LIMIT 1"
            ).fetchone()
        is_baseline = status == "성공" and prev is None
        cur = conn.execute(
            "INSERT INTO collection_run(source_type, started_at, finished_at, base_date, status, "
            "is_baseline, raw_ref, error_message) VALUES('GCC', ?, ?, ?, ?, ?, ?, ?)",
            (started, now(), day, status, int(is_baseline), f"gcc:{day}", error),
        )
        cid = cur.lastrowid
        change_count = new_case_count = 0

        if status == "성공":
            for r in records:
                conn.execute(
                    "INSERT INTO gcc_record(collection_id, gcc_code, org_name, parent_code, "
                    "prev_org_code, created_date, abolished_date, raw_json) VALUES(?,?,?,?,?,?,?,?)",
                    (cid, r["gcc_code"], r["org_name"], r["parent_code"], r["prev_org_code"],
                     r["created_date"], r["abolished_date"], json.dumps(r, ensure_ascii=False)),
                )
            if prev is not None:
                change_count, new_case_count = _detect(conn, prev["collection_id"], cid)
            conn.execute(
                "UPDATE collection_run SET change_count=?, new_case_count=? WHERE collection_id=?",
                (change_count, new_case_count, cid),
            )

    return {
        "collection_id": cid, "status": status, "is_baseline": is_baseline,
        "error": error, "change_count": change_count, "new_case_count": new_case_count,
    }


def _detect(conn, before_id: int, after_id: int) -> tuple[int, int]:
    """트랜잭션 안에서 호출된다. -> (새로 등록한 변경 수, 새 검토 건 수)"""
    before, after = _load(conn, before_id), _load(conn, after_id)
    changes = _diff(before, after)
    known = {r["change_key"] for r in conn.execute("SELECT change_key FROM change_event")}
    new = [c for c in changes if c["change_key"] not in known]
    if not new:
        return 0, 0

    detected = now()
    groups = _group(new, before, after)
    for group in groups:
        new_codes = [c["gcc_code"] for c in group if c["change_type"] == "신설"]
        primary = new_codes[0] if new_codes else min(c["gcc_code"] for c in group)
        title = (after.get(primary) or before[primary])["org_name"]
        summary = "; ".join(_summary(c, before, after) for c in group)
        case_id = conn.execute(
            "INSERT INTO review_case(primary_code, title, change_summary, detected_at) VALUES(?,?,?,?)",
            (primary, title, summary, detected),
        ).lastrowid
        for c in group:
            conn.execute(
                "INSERT INTO change_event(case_id, change_key, change_type, gcc_code, field_name, "
                "old_value, new_value, before_collection_id, after_collection_id, detected_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (case_id, c["change_key"], c["change_type"], c["gcc_code"], c["field_name"],
                 c["old_value"], c["new_value"], before_id, after_id, detected),
            )
    return len(new), len(groups)
