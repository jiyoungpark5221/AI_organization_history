"""법령 확보와 AI 분석. 결과는 항상 '후보'로만 저장하며 확정 데이터는 건드리지 않는다."""
import json
from datetime import datetime

from .db import now, tx


class AnalysisError(Exception):
    """LLM 응답이 정해진 구조가 아니다."""


ROLES = ("개편 전", "개편 후")
MOVE_SCOPES = ("전체", "일부", "범위 미확인")
DATE_STATUSES = ("확인", "미확인", "원천 충돌")


# ---------- 검증 ----------

def _text(obj: dict, key: str, where: str, required: bool = True) -> str | None:
    v = obj.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise AnalysisError(f"{where}: '{key}' 값이 없습니다")
        return None
    if not isinstance(v, str):
        raise AnalysisError(f"{where}: '{key}' 는 문자열이어야 합니다")
    return v.strip()


def _date(obj: dict, key: str, where: str) -> str | None:
    v = obj.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise AnalysisError(f"{where}: '{key}' 날짜 형식 오류")
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise AnalysisError(f"{where}: '{key}' 는 YYYY-MM-DD 이어야 합니다: {v}") from None
    return v


def _evidence(obj: dict, where: str) -> list[dict]:
    ev = obj.get("evidence")
    if not isinstance(ev, list):
        raise AnalysisError(f"{where}: 'evidence' 는 목록이어야 합니다")
    out = []
    for i, e in enumerate(ev):
        w = f"{where}.evidence[{i}]"
        if not isinstance(e, dict):
            raise AnalysisError(f"{w}: 객체가 아닙니다")
        role = _text(e, "role", w)
        if role not in ROLES:
            raise AnalysisError(f"{w}: role 은 {ROLES} 중 하나여야 합니다")
        out.append({
            "role": role,
            "article_ref": _text(e, "article_ref", w),
            "excerpt": _text(e, "excerpt", w, required=False),
            "reason": _text(e, "reason", w, required=False),
        })
    return out


def _date_status(obj: dict, where: str) -> str:
    st = _text(obj, "date_status", where)
    if st not in DATE_STATUSES:
        raise AnalysisError(f"{where}: date_status 는 {DATE_STATUSES} 중 하나여야 합니다")
    return st


def validate_analysis(data: dict) -> dict:
    """LLM 응답 구조를 검증하고 정규화한 사본을 돌려준다. 구조 오류는 AnalysisError."""
    if not isinstance(data, dict):
        raise AnalysisError("응답이 객체가 아닙니다")
    summary = _text(data, "summary", "응답")
    rels, moves = data.get("relations"), data.get("task_moves")
    if not isinstance(rels, list) or not isinstance(moves, list):
        raise AnalysisError("'relations' 와 'task_moves' 는 목록이어야 합니다")

    out_rel = []
    for i, r in enumerate(rels):
        w = f"relations[{i}]"
        if not isinstance(r, dict):
            raise AnalysisError(f"{w}: 객체가 아닙니다")
        st = _date_status(r, w)
        vf, vt = _date(r, "valid_from", w), _date(r, "valid_to", w)
        if st == "미확인":
            vf = vt = None
        out_rel.append({
            "from_code": _text(r, "from_code", w), "to_code": _text(r, "to_code", w),
            "relation_type": _text(r, "relation_type", w),
            "valid_from": vf, "valid_to": vt, "date_status": st,
            "evidence": _evidence(r, w),
        })

    out_mv = []
    for i, m in enumerate(moves):
        w = f"task_moves[{i}]"
        if not isinstance(m, dict):
            raise AnalysisError(f"{w}: 객체가 아닙니다")
        scope = _text(m, "move_scope", w)
        if scope not in MOVE_SCOPES:
            raise AnalysisError(f"{w}: move_scope 는 {MOVE_SCOPES} 중 하나여야 합니다")
        st = _date_status(m, w)
        ed = _date(m, "effective_date", w)
        if st == "미확인":
            ed = None
        out_mv.append({
            "from_code": _text(m, "from_code", w), "to_code": _text(m, "to_code", w),
            "task_description": _text(m, "task_description", w),
            "move_scope": scope, "effective_date": ed, "date_status": st,
            "evidence": _evidence(m, w),
        })
    return {"summary": summary, "relations": out_rel, "task_moves": out_mv}


# ---------- 법령 확보 ----------

def _clean_versions(raw) -> list[dict]:
    """법령 응답에서 쓸 수 있는 버전만 남긴다. 형식이 맞지 않는 것은 확보 못한 것으로 본다."""
    versions = raw.get("versions") if isinstance(raw, dict) else None
    out = []
    for v in versions if isinstance(versions, list) else []:
        if not (isinstance(v, dict) and v.get("role") in ROLES and v.get("law_name")):
            continue
        provs = [p for p in (v.get("provisions") or [])
                 if isinstance(p, dict) and p.get("article_ref") and p.get("text")]
        out.append({**v, "provisions": provs})
    return out


def _fetch_law(law, case, codes) -> tuple[list[dict], str | None]:
    try:
        return _clean_versions(law.fetch(case["primary_code"], codes)), None
    except Exception as e:  # 연결·파일·형식 오류 모두 '자료 부족'으로 다룬다
        return [], f"법령 확보 실패: {e}"


# ---------- 분석 ----------

def _reviewed_exists(conn, case_id: int) -> bool:
    for t in ("relation_candidate", "task_move_candidate"):
        if conn.execute(f"SELECT 1 FROM {t} WHERE case_id=? AND review_status<>'승인 대기' LIMIT 1",
                        (case_id,)).fetchone():
            return True
    return False


def _org_name(conn, code: str) -> str:
    row = conn.execute(
        "SELECT g.org_name FROM gcc_record g JOIN collection_run c USING(collection_id) "
        "WHERE g.gcc_code=? AND c.status='성공' ORDER BY c.collection_id DESC LIMIT 1", (code,)
    ).fetchone()
    return row["org_name"] if row else code


def _pick_change(changes: list[dict], from_code: str, to_code: str):
    for code in (to_code, from_code):
        for c in changes:
            if c["gcc_code"] == code:
                return c["change_id"]
    return changes[0]["change_id"] if changes else None


def _clear_previous(conn, case_id: int) -> None:
    for kind, t in (("relation", "relation_candidate"), ("task_move", "task_move_candidate")):
        conn.execute(
            f"DELETE FROM evidence WHERE candidate_type=? AND candidate_id IN "
            f"(SELECT candidate_id FROM {t} WHERE case_id=?)", (kind, case_id))
        conn.execute(f"DELETE FROM {t} WHERE case_id=?", (case_id,))
    conn.execute("DELETE FROM law_provision WHERE law_source_id IN "
                 "(SELECT law_source_id FROM law_source WHERE case_id=?)", (case_id,))
    conn.execute("DELETE FROM law_source WHERE case_id=?", (case_id,))


def _save_law(conn, case_id: int, versions: list[dict]) -> tuple[dict, str | None]:
    """-> ({(role, article_ref): provision_id}, 개편 후 시행일)"""
    prov_ids, after_eff = {}, None
    for v in versions:
        cur = conn.execute(
            "INSERT INTO law_source(case_id, law_name, law_ident, version_ident, role, effective_date,"
            " source_url, fetch_status, fetched_at) VALUES(?,?,?,?,?,?,?,'확보',?)",
            (case_id, v["law_name"], v.get("law_ident"), v.get("version_ident"), v["role"],
             v.get("effective_date"), v.get("source_url"), now()))
        sid = cur.lastrowid
        if v["role"] == "개편 후" and after_eff is None:
            after_eff = v.get("effective_date")
        for p in v["provisions"]:
            pc = conn.execute(
                "INSERT INTO law_provision(law_source_id, article_ref, is_addendum, text) VALUES(?,?,?,?)",
                (sid, p["article_ref"], 1 if p.get("is_addendum") else 0, p["text"]))
            prov_ids.setdefault((v["role"], p["article_ref"]), pc.lastrowid)
    return prov_ids, after_eff


def _valid_evidence(cand: dict, prov_ids: dict) -> list[tuple[int, dict]]:
    return [(prov_ids[(e["role"], e["article_ref"])], e) for e in cand["evidence"]
            if (e["role"], e["article_ref"]) in prov_ids]


def _write_candidates(conn, case_id, result, prov_ids, after_eff, changes) -> bool:
    """후보와 근거를 저장한다. -> 원천 충돌 여부"""
    conflict = False
    specs = (
        ("relation", "relation_candidate", result["relations"], "valid_from",
         ("relation_type", "valid_from", "valid_to")),
        ("task_move", "task_move_candidate", result["task_moves"], "effective_date",
         ("task_description", "move_scope", "effective_date")),
    )
    for kind, table, cands, date_key, fields in specs:
        for cand in cands:
            evs = _valid_evidence(cand, prov_ids)
            if not evs:  # 근거 없는 후보는 만들지 않는다
                continue
            status = cand["date_status"]
            if status == "원천 충돌" or (
                    cand[date_key] and after_eff and cand[date_key] != after_eff):
                status = "원천 충돌"
                conflict = True
            proposed = {k: cand[k] for k in (*fields, "from_code", "to_code")}
            proposed["date_status"] = status
            cols = {
                "case_id": case_id,
                "change_id": _pick_change(changes, cand["from_code"], cand["to_code"]),
                "from_code": cand["from_code"], "to_code": cand["to_code"],
                "from_name": _org_name(conn, cand["from_code"]),
                "to_name": _org_name(conn, cand["to_code"]),
                **{k: cand[k] for k in fields},
                "date_status": status,
                "proposed_json": json.dumps(proposed, ensure_ascii=False),
            }
            if table == "relation_candidate":
                cols["valid_to"] = cand["valid_to"]
            keys = list(cols)
            cur = conn.execute(
                f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' * len(keys))})",
                [cols[k] for k in keys])
            for pid, e in evs:
                conn.execute(
                    "INSERT INTO evidence(candidate_type, candidate_id, provision_id, excerpt, reason)"
                    " VALUES(?,?,?,?,?)", (kind, cur.lastrowid, pid, e["excerpt"], e["reason"]))
    return conflict


def _fail(conn, case_id: int, message: str) -> str:
    conn.execute("UPDATE review_case SET analysis_status='실패', last_error=? WHERE case_id=?",
                 (message, case_id))
    return "실패"


def process_case(conn, case_id: int, law, llm) -> str:
    case = conn.execute("SELECT * FROM review_case WHERE case_id=?", (case_id,)).fetchone()
    if case is None:
        raise KeyError(f"검토 건 없음: {case_id}")
    if _reviewed_exists(conn, case_id):  # 검토가 시작된 건은 새 분석으로 바꾸지 않는다
        return case["analysis_status"]

    conn.execute("UPDATE review_case SET analysis_attempts=analysis_attempts+1,"
                 " analysis_status='분석 중' WHERE case_id=?", (case_id,))
    changes = [dict(r) for r in conn.execute(
        "SELECT change_id, change_type, gcc_code, field_name, old_value, new_value "
        "FROM change_event WHERE case_id=? ORDER BY change_id", (case_id,))]

    versions, law_error = _fetch_law(law, case, sorted({c["gcc_code"] for c in changes}))
    roles = {v["role"] for v in versions}
    data_status = "자료 부족" if set(ROLES) - roles else "정상"

    try:
        raw = llm.analyze({"primary_code": case["primary_code"], "summary": case["change_summary"],
                           "changes": changes, "law_versions": versions})
        result = validate_analysis(raw)
    except Exception as e:  # LLM 호출 실패·구조 오류 모두 재시도 대상
        return _fail(conn, case_id, f"AI 분석 실패: {e}")

    try:
        with tx(conn):
            _clear_previous(conn, case_id)
            prov_ids, after_eff = _save_law(conn, case_id, versions)
            conflict = _write_candidates(conn, case_id, result, prov_ids, after_eff, changes)
            if data_status == "정상" and conflict:
                data_status = "원천 충돌"
            conn.execute(
                "UPDATE review_case SET analysis_status='완료', data_status=?, last_error=? "
                "WHERE case_id=?", (data_status, law_error, case_id))
    except Exception as e:  # tx 가 롤백된 뒤 실패를 기록한다
        return _fail(conn, case_id, f"저장 실패: {e}")
    return "완료"


def process_pending(conn, law, llm, max_attempts: int = 5) -> list[int]:
    rows = conn.execute(
        "SELECT case_id FROM review_case WHERE analysis_attempts<? AND ("
        " analysis_status IN ('대기','실패') OR (analysis_status='완료' AND data_status='자료 부족'))"
        " ORDER BY case_id", (max_attempts,)).fetchall()
    done = []
    for r in rows:
        if _reviewed_exists(conn, r["case_id"]):
            continue
        process_case(conn, r["case_id"], law, llm)
        done.append(r["case_id"])
    return done
