"""승인·보류·기각 처리와 확정 데이터 정정 (IMPLEMENTATION_PLAN.md 5.2, 5.3).

승인은 후보 단위 단일 트랜잭션이다. 하나라도 실패하면 확정 데이터·기관 반영·검토 이력·상태 변경이
모두 취소되어 후보는 이전 상태로 남고, 담당자가 입력한 수정 내용(draft_json)은 재시도용으로 보존된다.
"""
import json
import re
from datetime import datetime

from .auth import get_active_user
from .db import now, tx


class ReviewError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 한국어 메시지."""


KINDS = {"relation": "relation_candidate", "task_move": "task_move_candidate"}
EDITABLE = {
    "relation": ["relation_type", "valid_from", "valid_to", "date_status"],
    "task_move": ["task_description", "move_scope", "effective_date", "date_status"],
}
ACTIONS = ("승인", "수정 후 승인", "보류", "기각")
DATE_STATUSES = ("확인", "미확인", "원천 충돌")
MOVE_SCOPES = ("전체", "일부", "범위 미확인")
OPEN_STATUSES = ("승인 대기", "보류")
NEW_STATUS = {"승인": "승인 완료", "수정 후 승인": "승인 완료", "보류": "보류", "기각": "기각"}

FIELD_TYPES = {
    "relation_type": "text",
    "task_description": "text",
    "org_name": "text",
    "valid_from": "date",
    "valid_to": "date",
    "effective_date": "date",
    "date_status": "status",
    "move_scope": "scope",
}
FIELD_LABELS = {
    "relation_type": "관계 유형",
    "task_description": "업무 내용",
    "org_name": "기관명",
    "valid_from": "시작일",
    "valid_to": "종료일",
    "effective_date": "적용일",
    "date_status": "날짜 확인 상태",
    "move_scope": "이동 범위",
}

# 정정 대상: target -> (테이블, 정정 가능한 컬럼)
CORRECTION_TARGETS = {
    "org_info": ("confirmed_org_info", ["org_name", "valid_from", "valid_to", "date_status"]),
    "relation": ("confirmed_relation", EDITABLE["relation"]),
    "task_move": ("confirmed_task_move", EDITABLE["task_move"]),
}


# ---------------------------------------------------------------- 값 검증

def _clean(field: str, value):
    ftype = FIELD_TYPES[field]
    label = FIELD_LABELS[field]
    if value is None:
        value = ""
    value = str(value).strip()
    if ftype == "date":
        if not value:
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ReviewError(f"{label}은(는) YYYY-MM-DD 형식이어야 합니다.")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ReviewError(f"{label}이(가) 올바른 날짜가 아닙니다: {value}")
        return value
    if ftype == "text":
        if not value:
            raise ReviewError(f"{label}은(는) 비워 둘 수 없습니다.")
        return value
    if ftype == "status":
        if value not in DATE_STATUSES:
            raise ReviewError(f"{label}은(는) {', '.join(DATE_STATUSES)} 중 하나여야 합니다.")
        return value
    if value not in MOVE_SCOPES:
        raise ReviewError(f"{label}은(는) {', '.join(MOVE_SCOPES)} 중 하나여야 합니다.")
    return value


def _date_keys(values: dict) -> list[str]:
    return [k for k in values if FIELD_TYPES.get(k) == "date"]


def _normalize(values: dict) -> dict:
    """날짜 확인 상태가 '미확인'이면 날짜는 비운다(특정 시점 확정에 쓰지 않기 위해)."""
    out = dict(values)
    if out.get("date_status") == "미확인":
        for k in _date_keys(out):
            out[k] = None
    return out


def _apply_edits(base: dict, edits: dict, allowed: list[str]) -> dict:
    unknown = [k for k in edits if k not in allowed]
    if unknown:
        raise ReviewError(f"수정할 수 없는 항목입니다: {', '.join(unknown)}")
    out = dict(base)
    for key, value in edits.items():
        out[key] = _clean(key, value)
    out = _normalize(out)
    dates = _date_keys(out)
    if out.get("date_status") == "확인" and dates and all(out[k] is None for k in dates):
        raise ReviewError("날짜 확인 상태가 '확인'이면 날짜를 입력해야 합니다. 모르면 '미확인'으로 두세요.")
    return out


def _proposed(kind: str, row) -> dict:
    return {k: row[k] for k in EDITABLE[kind]}


def candidate_values(row) -> dict:
    """화면에 채울 값: 후보의 제안값 위에 저장된 수정 입력(draft_json)을 덮어쓴 것."""
    kind = "relation" if "relation_type" in row.keys() else "task_move"
    values = _proposed(kind, row)
    if row["draft_json"]:
        draft = json.loads(row["draft_json"])
        values.update({k: draft[k] for k in EDITABLE[kind] if k in draft})
    return values


# ---------------------------------------------------------------- 기관 반영

def _latest_record(conn, code: str):
    return conn.execute(
        "SELECT g.* FROM gcc_record g JOIN collection_run c ON c.collection_id = g.collection_id "
        "WHERE c.status = '성공' AND g.gcc_code = ? ORDER BY c.collection_id DESC LIMIT 1",
        (code,),
    ).fetchone()


def _ensure_org(conn, code: str, name_hint: str | None = None) -> int:
    row = conn.execute(
        "SELECT org_id FROM org_identifier WHERE id_type='GCC' AND id_value=?", (code,)
    ).fetchone()
    if row:
        return row["org_id"]
    rec = _latest_record(conn, code)
    name = (rec["org_name"] if rec else None) or name_hint or code
    org_id = conn.execute("INSERT INTO org(org_name) VALUES(?)", (name,)).lastrowid
    conn.execute(
        "INSERT INTO org_identifier(org_id, id_type, id_value) VALUES(?, 'GCC', ?)", (org_id, code)
    )
    return org_id


def _update_org_info(conn, code, name_hint, kind, candidate_id, user_id, ts) -> int:
    """GCC 최신 수집본 기준으로 기관 확정정보를 추가/갱신한다. 과거 행은 보존한다."""
    org_id = _ensure_org(conn, code, name_hint)
    rec = _latest_record(conn, code)
    if rec is None:
        return org_id
    parent_id = _ensure_org(conn, rec["parent_code"]) if rec["parent_code"] else None
    new = {
        "org_name": rec["org_name"],
        "parent_org_id": parent_id,
        "valid_from": rec["created_date"],
        "valid_to": rec["abolished_date"],
        "date_status": "확인" if (rec["created_date"] or rec["abolished_date"]) else "미확인",
    }
    cur = conn.execute(
        "SELECT * FROM confirmed_org_info WHERE org_id=? AND is_current=1", (org_id,)
    ).fetchone()
    if cur is not None:
        if all(cur[k] == v for k, v in new.items()):
            return org_id
        # 담당자가 정정한 값은 이후 수집본으로 자동 덮어쓰지 않는다.
        corrected = conn.execute(
            "SELECT 1 FROM correction_log WHERE target_type='org_info' AND new_target_id=?",
            (cur["confirmed_id"],),
        ).fetchone()
        if corrected:
            return org_id
        conn.execute(
            "UPDATE confirmed_org_info SET is_current=0 WHERE confirmed_id=?", (cur["confirmed_id"],)
        )
    conn.execute(
        "INSERT INTO confirmed_org_info(org_id, org_name, parent_org_id, valid_from, valid_to, "
        "date_status, source_candidate_id, source_candidate_type, approved_by, approved_at, "
        "is_current, supersedes_id) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",
        (
            org_id, new["org_name"], new["parent_org_id"], new["valid_from"], new["valid_to"],
            new["date_status"], candidate_id, kind, user_id, ts,
            cur["confirmed_id"] if cur is not None else None,
        ),
    )
    return org_id


def _confirm(conn, kind, row, final, user_id, ts) -> None:
    cid = row["candidate_id"]
    from_id = _update_org_info(conn, row["from_code"], row["from_name"], kind, cid, user_id, ts)
    to_id = _update_org_info(conn, row["to_code"], row["to_name"], kind, cid, user_id, ts)
    if kind == "relation":
        conn.execute(
            "INSERT INTO confirmed_relation(from_org_id, to_org_id, relation_type, valid_from, "
            "valid_to, date_status, source_candidate_id, approved_by, approved_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (from_id, to_id, final["relation_type"], final["valid_from"], final["valid_to"],
             final["date_status"], cid, user_id, ts),
        )
    else:
        conn.execute(
            "INSERT INTO confirmed_task_move(from_org_id, to_org_id, task_description, move_scope, "
            "effective_date, date_status, source_candidate_id, approved_by, approved_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (from_id, to_id, final["task_description"], final["move_scope"],
             final["effective_date"], final["date_status"], cid, user_id, ts),
        )


def _write_review_log(conn, kind, candidate_id, action, before, after, reason, user_id, ts) -> None:
    conn.execute(
        "INSERT INTO review_log(candidate_type, candidate_id, action, before_value, after_value, "
        "reason, reviewer_id, reviewed_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            kind, candidate_id, action,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            reason or None, user_id, ts,
        ),
    )


def _require_active_user(conn, user_id) -> None:
    """처리자는 로그인 계정(user_id)이어야 하며, 비활성 계정은 처리할 수 없다."""
    if get_active_user(conn, user_id) is None:
        raise ReviewError("로그인한 사용자만 처리할 수 있습니다.")


# ---------------------------------------------------------------- 검토 처리

def decide(conn, kind, candidate_id, action, user_id, reason="", edits=None) -> None:
    """user_id 는 로그인한 계정의 고정 ID. 승인자·처리자는 이 값으로만 기록한다."""
    table = KINDS.get(kind)
    if table is None:
        raise ReviewError("알 수 없는 후보 종류입니다.")
    if action not in ACTIONS:
        raise ReviewError("알 수 없는 처리입니다.")
    _require_active_user(conn, user_id)
    reason = (reason or "").strip()
    row = conn.execute(f"SELECT * FROM {table} WHERE candidate_id=?", (candidate_id,)).fetchone()
    if row is None:
        raise ReviewError("후보를 찾을 수 없습니다.")
    if row["review_status"] not in OPEN_STATUSES:
        raise ReviewError(f"이미 처리된 후보입니다 ({row['review_status']}).")

    proposed = _proposed(kind, row)
    final = None
    if action == "수정 후 승인":
        if not reason:
            raise ReviewError("수정 후 승인에는 사유를 입력해야 합니다.")
        if not edits:
            raise ReviewError("수정할 내용을 입력하세요.")
        final = _apply_edits(proposed, edits, EDITABLE[kind])
        if final == _normalize(proposed):
            raise ReviewError("제안값과 달라진 내용이 없습니다. 그대로 승인하려면 '승인'을 누르세요.")
        # 승인 처리가 실패해도 재시도할 수 있도록 트랜잭션 밖에서 먼저 보존한다.
        conn.execute(
            f"UPDATE {table} SET draft_json=? WHERE candidate_id=?",
            (json.dumps(final, ensure_ascii=False), candidate_id),
        )
    elif action == "승인":
        final = _normalize(proposed)
    elif action == "기각" and not reason:
        raise ReviewError("기각에는 사유를 입력해야 합니다.")

    ts = now()
    approving = final is not None
    with tx(conn):
        # 같은 후보를 동시에/이중으로 처리하지 못하도록 조건부로 선점한다.
        cur = conn.execute(
            f"UPDATE {table} SET review_status=? WHERE candidate_id=? "
            "AND review_status IN ('승인 대기','보류')",
            (NEW_STATUS[action], candidate_id),
        )
        if cur.rowcount != 1:
            raise ReviewError("이미 처리된 후보입니다.")
        if approving:
            _confirm(conn, kind, row, final, user_id, ts)
            conn.execute(f"UPDATE {table} SET draft_json=NULL WHERE candidate_id=?", (candidate_id,))
        _write_review_log(
            conn, kind, candidate_id, action,
            proposed if approving else None, final, reason, user_id, ts,
        )


def case_review_state(conn, case_id: int) -> str:
    statuses = set()
    for table in KINDS.values():
        for r in conn.execute(f"SELECT review_status FROM {table} WHERE case_id=?", (case_id,)):
            statuses.add(r["review_status"])
    if not statuses:
        return "후보 없음"
    for state in ("승인 대기", "보류", "승인 완료"):
        if state in statuses:
            return state
    return "기각"


# ---------------------------------------------------------------- 정정

def _write_correction_log(conn, target, old_id, new_id, old, new, reason, user_id, ts) -> None:
    conn.execute(
        "INSERT INTO correction_log(target_type, target_id, new_target_id, old_value, new_value, "
        "reason, corrected_by, corrected_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            target, old_id, new_id,
            json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False),
            reason, user_id, ts,
        ),
    )


def correct(conn, target, confirmed_id, edits, reason, user_id) -> int:
    """확정 데이터를 정정한다. 기존 행은 지우지 않고 비유효로 바꾸며, 새 행이 현재 유효한 값이 된다."""
    if target not in CORRECTION_TARGETS:
        raise ReviewError("알 수 없는 정정 대상입니다.")
    table, allowed = CORRECTION_TARGETS[target]
    _require_active_user(conn, user_id)
    reason = (reason or "").strip()
    if not reason:
        raise ReviewError("정정 사유를 입력하세요.")
    if not edits:
        raise ReviewError("정정할 내용을 입력하세요.")
    row = conn.execute(f"SELECT * FROM {table} WHERE confirmed_id=?", (confirmed_id,)).fetchone()
    if row is None:
        raise ReviewError("정정할 확정 데이터를 찾을 수 없습니다.")
    if not row["is_current"]:
        raise ReviewError("현재 유효한 값만 정정할 수 있습니다.")

    old = {k: row[k] for k in allowed}
    new = _apply_edits(old, edits, allowed)
    if new == old:
        raise ReviewError("달라진 내용이 없습니다.")

    ts = now()
    with tx(conn):
        columns = [k for k in row.keys() if k != "confirmed_id"]
        values = {k: row[k] for k in columns}
        values.update(new)
        values["is_current"] = 1
        values["supersedes_id"] = confirmed_id
        new_id = conn.execute(
            f"INSERT INTO {table}({', '.join(columns)}) VALUES({', '.join('?' for _ in columns)})",
            [values[k] for k in columns],
        ).lastrowid
        conn.execute(f"UPDATE {table} SET is_current=0 WHERE confirmed_id=?", (confirmed_id,))
        _write_correction_log(conn, target, confirmed_id, new_id, old, new, reason, user_id, ts)
    return new_id
