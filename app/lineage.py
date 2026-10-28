"""기능 담당 조직 계보도 데이터 조립 (교육 발표용, 읽기 전용).

세 가지 층을 항상 구분한다.
  - 원천 기재: 엑셀 가져오기 참조 테이블(ref_*). 파일의 Canonical/확인 상태도 '원천 파일의 상태'일 뿐 우리 승인이 아니다.
  - 검토 후보: 우리 시스템 AI 후보 중 승인 대기/보류/기각.
  - 승인 결과: 우리 시스템 확정 데이터(is_current=1). 업무 이동 화살표는 이 층의 confirmed_task_move 에만 그린다.
이 모듈은 SELECT 만 실행하며 DB 에 아무것도 쓰지 않는다.
"""
from __future__ import annotations

MAX_NODES = 16
MAX_SEARCH = 20
# 계보도는 최대 세 줄: 기관 단위(top, 최상위 소속 기관과 전·후신) / 중간 소속(mid) / 선택 기관(main, '')
ROW_ORDER = ("top", "mid", "")
ROW_TITLE = {"top": "기관 단위 계보 — 최상위 소속 기관과 전·후신",
             "mid": "중간 소속 기관 (상위기관 사슬)",
             "": "하위 조직 계보 — 선택 기관과 전·후신"}
BAND_BADGE = {"top": "최상위 소속 기관", "mid": "중간 소속 기관"}
NO_PARENT = (None, "", "0000000", "NULL")  # GCC 에서 상위기관이 없는 최상위 기관을 뜻하는 값
LAYERS = ("원천 기재", "검토 후보", "승인 결과")
SUCCESSION_TYPES = ("GCC_PREVIOUS_CODE_LINK", "SUCCEEDS", "SUCCEEDS_CANDIDATE", "TRANSFER_CANDIDATE")
TYPE_LABEL = {
    "GCC_PREVIOUS_CODE_LINK": "이전기관 코드 링크",
    "SUCCEEDS": "승계 관계(원천 파일 표기)",
    "SUCCEEDS_CANDIDATE": "승계 후보(원천 파일 표기)",
    "TRANSFER_CANDIDATE": "이관 후보(원천 파일 표기)",
    "GCC_PARENT_LINK": "상위기관 링크",
}
CANDIDATE_OPEN = ("승인 대기", "보류", "기각")


# ------------------------------------------------------------------ 표시 도우미
def _clean(text) -> str:
    return " ".join(str(text or "").split())


def date_text(value, status=None) -> str:
    """값이 없거나 '미확인'이면 반드시 '미확인'. 임의로 채우지 않는다."""
    if not value or status == "미확인":
        return "미확인"
    return str(value) + (" (원천 충돌)" if status == "원천 충돌" else "")


def period_text(start, end) -> str:
    return f"{start or '미확인'} ~ {end or '미확인'}"


def _units(ch: str) -> float:
    return 1.0 if ord(ch) > 0x2E80 else 0.56  # 한글·한자는 1em, 영문·숫자는 좁게


def wrap(text: str, max_units: float, max_lines: int | None = None) -> list[str]:
    lines, cur, w = [], "", 0.0
    for ch in _clean(text):
        u = _units(ch)
        if w + u > max_units and cur:
            lines.append(cur)
            cur, w = "", 0.0
        cur += ch
        w += u
    if cur:
        lines.append(cur)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip()[:-1] + "…"
    return lines or [""]


def _short(text, n: int) -> str:
    t = _clean(text)
    return t if len(t) <= n else t[: n - 1] + "…"


# ------------------------------------------------------------------ 검색·기본 기관
def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def org_codes(conn, org_id: int) -> dict:
    out = {"GCC": [], "COMMON": [], "OG": []}
    for r in conn.execute("SELECT id_type, id_value FROM org_identifier WHERE org_id=? ORDER BY id_value", (org_id,)):
        out[r["id_type"]].append(r["id_value"])
    return out


def search_orgs(conn, q: str, limit: int = MAX_SEARCH) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    pat = _like(q)
    ids = [r["org_id"] for r in conn.execute(
        "SELECT o.org_id FROM org o LEFT JOIN org_identifier i ON i.org_id=o.org_id "
        "WHERE o.org_name LIKE ? ESCAPE '\\' OR i.id_value LIKE ? ESCAPE '\\' "
        "GROUP BY o.org_id ORDER BY o.org_name, o.org_id LIMIT ?", (pat, pat, limit))]
    res = []
    for oid in ids:
        name = conn.execute("SELECT org_name FROM org WHERE org_id=?", (oid,)).fetchone()["org_name"]
        c = org_codes(conn, oid)
        res.append({"org_id": oid, "name": name, "gcc": c["GCC"], "common": c["COMMON"][0] if c["COMMON"] else None})
    return res


def default_org_id(conn, case) -> int | None:
    """건의 기관(primary_code, 없으면 변경 건의 기관코드)에 해당하는 org 를 찾는다."""
    codes = [case["primary_code"]] + [r["gcc_code"] for r in conn.execute(
        "SELECT gcc_code FROM change_event WHERE case_id=? ORDER BY change_id", (case["case_id"],))]
    for code in codes:
        row = conn.execute("SELECT org_id FROM org_identifier WHERE id_type='GCC' AND id_value=?", (code,)).fetchone()
        if row:
            return row["org_id"]
    return None


# ------------------------------------------------------------------ 데이터 조립
def _ph(n: int) -> str:
    return ",".join("?" * n)


def build(conn, org_id: int | None = None, case=None) -> dict:
    """기관 하나를 중심으로 계보 데이터를 조립한다 (읽기 전용)."""
    g = {"root": None, "nodes": [], "edges": [], "truncated": False, "omitted": 0, "notices": [],
         "has_arrow": False, "empty": False, "mode": "org", "rows": []}
    if org_id is not None:
        if conn.execute("SELECT 1 FROM org WHERE org_id=?", (org_id,)).fetchone() is None:
            return g
        root = org_id
    elif case is not None:
        root = default_org_id(conn, case)
        if root is None:
            root = "code:" + case["primary_code"]
            g["mode"] = "code"
    else:
        return g

    code_to_org: dict[str, int] = {}

    def key_of_code(code: str):
        if code not in code_to_org:
            row = conn.execute("SELECT org_id FROM org_identifier WHERE id_type='GCC' AND id_value=?", (code,)).fetchone()
            code_to_org[code] = row["org_id"] if row else None
        return code_to_org[code] if code_to_org[code] is not None else "code:" + code

    level: dict = {root: 0}
    band: dict = {}  # key -> 'top' | 'mid' (없으면 선택 기관 줄)
    order = [root]
    state = {"truncated": False, "omitted": 0}

    def add(key, lvl, b: str = "") -> bool:
        if key in level:
            return True
        if len(order) >= MAX_NODES:
            state["truncated"] = True
            state["omitted"] += 1
            return False
        level[key] = lvl
        order.append(key)
        if b:
            band[key] = b
        return True

    def expand_lineage(start, b: str) -> None:
        """이전기관 링크·승계 관계를 양방향 최대 2단계 따라간다 (원천 파일: from=후속 기관, to=이전 기관).
        SOURCE_LINK 와 CANONICAL_RELATION 층을 모두 따라가며, 방향은 여기서 '이전 → 후속' 열 순서로 바꾼다."""
        frontier = [start]
        for _ in (1, 2):
            nxt = []
            for k in frontier:
                if not isinstance(k, int):
                    continue
                for r in conn.execute(
                        f"SELECT from_org_id, to_org_id FROM ref_org_relation WHERE relation_type IN ({_ph(len(SUCCESSION_TYPES))}) "
                        "AND (from_org_id=? OR to_org_id=?) ORDER BY relation_id", (*SUCCESSION_TYPES, k, k)):
                    if r["from_org_id"] == k:
                        other, lvl = r["to_org_id"], level[k] - 1
                    else:
                        other, lvl = r["from_org_id"], level[k] + 1
                    if other is not None and other not in level and add(other, lvl, b):
                        nxt.append(other)
            frontier = nxt

    # 1) 선택 기관의 전·후신 계보 (하위 조직 줄)
    expand_lineage(root, "")
    # 2) 상위기관 사슬(GCC 상위기관 코드)을 따라 최상위 소속 기관까지: 마지막이 최상위(top), 그 사이는 중간 소속(mid)
    chain: list[int] = []
    if isinstance(root, int):
        cur = root
        for _ in range(6):
            r = conn.execute("SELECT to_org_id FROM ref_org_relation WHERE relation_type='GCC_PARENT_LINK' "
                             "AND from_org_id=? AND to_org_id IS NOT NULL ORDER BY relation_id LIMIT 1", (cur,)).fetchone()
            if r is None or r["to_org_id"] == root or r["to_org_id"] in chain:
                break
            chain.append(r["to_org_id"])
            cur = r["to_org_id"]
        for i, p in enumerate(chain):
            add(p, level[root], "top" if i == len(chain) - 1 else "mid")
        # 3) 최상위 소속 기관의 전·후신 계보 (기관 단위 줄)
        if chain:
            expand_lineage(chain[-1], "top")

    # 4) 승인 결과/검토 후보의 상대 기관 (우리 시스템: from=이전, to=이후)
    def gcc_codes_of(key) -> list[str]:
        if isinstance(key, int):
            return org_codes(conn, key)["GCC"]
        return [key.split(":", 1)[1]]

    for k in list(order):
        codes = gcc_codes_of(k)
        if codes:
            for tbl in ("relation_candidate", "task_move_candidate"):
                for r in conn.execute(
                        f"SELECT from_code, to_code FROM {tbl} WHERE review_status IN ('승인 대기','보류','기각') "
                        f"AND (from_code IN ({_ph(len(codes))}) OR to_code IN ({_ph(len(codes))}))", (*codes, *codes)):
                    fk, tk = key_of_code(r["from_code"]), key_of_code(r["to_code"])
                    if fk == tk:
                        continue
                    if k == tk:
                        add(fk, level[k] - 1, band.get(k, ""))
                    elif k == fk:
                        add(tk, level[k] + 1, band.get(k, ""))
        if isinstance(k, int):
            for tbl in ("confirmed_relation", "confirmed_task_move"):
                for r in conn.execute(f"SELECT from_org_id, to_org_id FROM {tbl} WHERE is_current=1 "
                                      "AND (from_org_id=? OR to_org_id=?)", (k, k)):
                    if r["from_org_id"] == r["to_org_id"]:
                        continue
                    if k == r["to_org_id"]:
                        add(r["from_org_id"], level[k] - 1, band.get(k, ""))
                    else:
                        add(r["to_org_id"], level[k] + 1, band.get(k, ""))

    # 5) 같은 줄 안에서 이전 기관이 후속 기관보다 항상 왼쪽 열에 오도록 보정 (예: 환경청 ← 환경처 ← 환경부)
    int_order = [k for k in order if isinstance(k, int)]
    if len(int_order) > 1:
        succ_edges = [(r["from_org_id"], r["to_org_id"]) for r in conn.execute(
            f"SELECT from_org_id, to_org_id FROM ref_org_relation WHERE relation_type IN ({_ph(len(SUCCESSION_TYPES))}) "
            f"AND from_org_id IN ({_ph(len(int_order))}) AND to_org_id IN ({_ph(len(int_order))})",
            (*SUCCESSION_TYPES, *int_order, *int_order))]
        for _ in range(len(order)):
            moved = False
            for succ, prev in succ_edges:
                if succ != prev and band.get(succ, "") == band.get(prev, "") and level[prev] > level[succ] - 1:
                    level[prev] = level[succ] - 1
                    moved = True
            if not moved:
                break

    keys = list(order)
    keyset = set(keys)

    # ---- 노드
    cand_names: dict[str, str] = {}
    for tbl in ("relation_candidate", "task_move_candidate"):
        for r in conn.execute(f"SELECT from_code, to_code, from_name, to_name FROM {tbl}"):
            if r["from_name"]:
                cand_names.setdefault(r["from_code"], r["from_name"])
            if r["to_name"]:
                cand_names.setdefault(r["to_code"], r["to_name"])

    nodes: dict = {}
    for k in keys:
        if isinstance(k, int):
            name = conn.execute("SELECT org_name FROM org WHERE org_id=?", (k,)).fetchone()["org_name"]
            c = org_codes(conn, k)
            node = {"key": k, "name": name, "gcc": c["GCC"], "common": c["COMMON"][0] if c["COMMON"] else None,
                    "og": c["OG"]}
        else:
            code = k.split(":", 1)[1]
            name = case["title"] if (case is not None and code == case["primary_code"]) else cand_names.get(code, code)
            node = {"key": k, "name": name, "gcc": [code], "common": None, "og": []}
        node.update({"level": level[k], "band": band.get(k, ""), "layers": [], "records": [], "works": [],
                     "work_total": 0, "cand_moves": [], "notes": [], "approved_info": None, "unlinked": ""})
        nodes[k] = node

    for k, node in nodes.items():
        if isinstance(k, int):
            for r in conn.execute("SELECT source_system, source_code, source_start, source_end FROM ref_org_record "
                                  "WHERE org_id=? ORDER BY source_system, source_code", (k,)):
                node["records"].append({"system": r["source_system"], "code": r["source_code"],
                                        "period": period_text(r["source_start"], r["source_end"])})
            works = conn.execute("SELECT * FROM ref_org_work WHERE org_id=? ORDER BY work_id", (k,)).fetchall()
            node["work_total"] = len(works)
            for w in works:
                node["works"].append({"text": _clean(w["raw_work_text"]), "page": w["source_page"],
                                      "seq": w["source_sequence"], "og_raw": w["og_code_raw"],
                                      "period": period_text(w["work_valid_from"], w["work_valid_to"]),
                                      "period_status": w["work_period_status"] or "미확인",
                                      "succession": "미확인"})
            if node["records"] or node["works"] or conn.execute(
                    "SELECT 1 FROM ref_org_relation WHERE from_org_id=? OR to_org_id=? LIMIT 1", (k, k)).fetchone():
                node["layers"].append("원천 기재")
            info = conn.execute(
                "SELECT c.*, u.display_name FROM confirmed_org_info c LEFT JOIN app_user u ON u.user_id=c.approved_by "
                "WHERE c.org_id=? AND c.is_current=1 ORDER BY c.confirmed_id DESC LIMIT 1", (k,)).fetchone()
            if info:
                node["approved_info"] = {"name": info["org_name"], "by": info["display_name"] or f"사용자 #{info['approved_by']}",
                                         "at": info["approved_at"], "period": period_text(info["valid_from"], info["valid_to"]),
                                         "date_status": info["date_status"]}
                node["layers"].append("승인 결과")

    # ---- 검토 후보 업무 승계(화살표 없음) / 자기 관계 메모
    all_codes = {c for n in nodes.values() for c in n["gcc"]}
    cand_rel_edges, cand_move_rows = [], []
    if all_codes:
        ph = _ph(len(all_codes))
        for r in conn.execute(f"SELECT * FROM relation_candidate WHERE review_status IN ('승인 대기','보류','기각') "
                              f"AND (from_code IN ({ph}) OR to_code IN ({ph})) ORDER BY candidate_id",
                              (*all_codes, *all_codes)):
            fk, tk = key_of_code(r["from_code"]), key_of_code(r["to_code"])
            if fk in keyset and tk in keyset:
                cand_rel_edges.append((fk, tk, r))
        for r in conn.execute(f"SELECT * FROM task_move_candidate WHERE review_status IN ('승인 대기','보류','기각') "
                              f"AND (from_code IN ({ph}) OR to_code IN ({ph})) ORDER BY candidate_id",
                              (*all_codes, *all_codes)):
            cand_move_rows.append(r)
    for r in cand_move_rows:
        fk, tk = key_of_code(r["from_code"]), key_of_code(r["to_code"])
        text = (f"{_short(r['task_description'], 40)} — {r['from_name'] or r['from_code']} → {r['to_name'] or r['to_code']} "
                f"[{r['review_status']}] · 이동범위 {r['move_scope']} · 적용일 {date_text(r['effective_date'], r['date_status'])}")
        for kk in {fk, tk}:
            if kk in nodes:
                nodes[kk]["cand_moves"].append(text)
                if "검토 후보" not in nodes[kk]["layers"]:
                    nodes[kk]["layers"].append("검토 후보")

    edges: list[dict] = []

    def add_edge(**e):
        e["id"] = len(edges) + 1
        edges.append(e)

    # ---- 원천 기재 관계 (선, 화살표 없음)
    if len(keys) > 0:
        int_keys = [k for k in keys if isinstance(k, int)]
        if int_keys:
            ph = _ph(len(int_keys))
            for r in conn.execute(f"SELECT * FROM ref_org_relation WHERE from_org_id IN ({ph}) AND to_org_id IN ({ph}) "
                                  "ORDER BY relation_id", (*int_keys, *int_keys)):
                t = r["relation_type"]
                if t == "GCC_PARENT_LINK":
                    start, end = r["to_org_id"], r["from_org_id"]  # 상위 → 하위 (위에서 아래)
                    label = "상위기관 링크 (원천 기재)"
                else:
                    start, end = r["to_org_id"], r["from_org_id"]  # 이전 기관 → 후속 기관
                    label = TYPE_LABEL.get(t, t or "관계") + " (원천 기재)"
                canonical = r["layer"] == "CANONICAL_RELATION"
                src_state = r["review_status_src"]
                if canonical:
                    status = f"원천 파일의 검토 상태: {src_state or '미확인'} (우리 시스템 승인 아님)"
                elif src_state not in (None, "", "SOURCE_EXPLICIT"):
                    status = f"원천 연결 — 원천 파일의 상태: {src_state} (우리 시스템 승인 아님)"
                else:
                    status = "원천 연결 — 검토 전 (우리 시스템 승인 아님)"
                valid = (period_text(r["relation_valid_from"], r["relation_valid_to"])
                         if (r["relation_valid_from"] or r["relation_valid_to"]) else "미확인")
                obs = (period_text(r["observed_from"], r["observed_to"])
                       if (r["observed_from"] or r["observed_to"]) else "없음")
                add_edge(kind="parent" if t == "GCC_PARENT_LINK" else "relation", layer="원천 기재", style="src",
                         start=start, end=end, arrow=False, label=label, status=status,
                         valid=f"관계 유효기간: {valid}", observed=f"원천 관측기간(관계·업무 유효기간 아님): {obs}",
                         evidence=_short(r["evidence"], 80), ref_id=r["relation_id"])
                if t == "TRANSFER_CANDIDATE":
                    edges[-1]["status"] += " · 업무 이동 여부는 미확인"

    # ---- 검토 후보 관계 (점선)
    for fk, tk, r in cand_rel_edges:
        text = f"{r['relation_type']} · 적용일 {date_text(r['valid_from'], r['date_status'])}"
        if fk == tk:
            nodes[fk]["notes"].append(f"관계 후보(미확정): {text} [{r['review_status']}]")
            if "검토 후보" not in nodes[fk]["layers"]:
                nodes[fk]["layers"].append("검토 후보")
            continue
        for kk in (fk, tk):
            if "검토 후보" not in nodes[kk]["layers"]:
                nodes[kk]["layers"].append("검토 후보")
        add_edge(kind="relation", layer="검토 후보", style="cand", start=fk, end=tk, arrow=False,
                 label=f"{r['relation_type']} (검토 후보)", status=f"검토 후보 — {r['review_status']} (미확정)",
                 valid=f"적용일: {date_text(r['valid_from'], r['date_status'])}"
                       + (f" ~ {r['valid_to']}" if r["valid_to"] else ""),
                 observed="", evidence="", ref_id=f"관계 후보 #{r['candidate_id']} (건 #{r['case_id']})")

    # ---- 승인 결과 (굵은 실선, 업무 이동만 화살표)
    int_keys = [k for k in keys if isinstance(k, int)]
    if int_keys:
        ph = _ph(len(int_keys))
        for r in conn.execute(
                f"SELECT c.*, u.display_name FROM confirmed_relation c LEFT JOIN app_user u ON u.user_id=c.approved_by "
                f"WHERE c.is_current=1 AND c.from_org_id IN ({ph}) AND c.to_org_id IN ({ph}) ORDER BY c.confirmed_id",
                (*int_keys, *int_keys)):
            by = r["display_name"] or f"사용자 #{r['approved_by']}"
            text = f"{r['relation_type']} · 적용일 {date_text(r['valid_from'], r['date_status'])}"
            if r["from_org_id"] == r["to_org_id"]:
                nodes[r["from_org_id"]]["notes"].append(f"승인된 관계: {text} (승인 {by})")
                if "승인 결과" not in nodes[r["from_org_id"]]["layers"]:
                    nodes[r["from_org_id"]]["layers"].append("승인 결과")
                continue
            for kk in (r["from_org_id"], r["to_org_id"]):
                if "승인 결과" not in nodes[kk]["layers"]:
                    nodes[kk]["layers"].append("승인 결과")
            add_edge(kind="relation", layer="승인 결과", style="appr", start=r["from_org_id"], end=r["to_org_id"],
                     arrow=False, label=f"{r['relation_type']} (승인)", status=f"승인 결과 — 승인자 {by}, {r['approved_at']}",
                     valid=f"적용일: {date_text(r['valid_from'], r['date_status'])}"
                           + (f" ~ {r['valid_to']}" if r["valid_to"] else ""),
                     observed="", evidence="", ref_id=f"확정 관계 #{r['confirmed_id']}")
        for r in conn.execute(
                f"SELECT c.*, u.display_name FROM confirmed_task_move c LEFT JOIN app_user u ON u.user_id=c.approved_by "
                f"WHERE c.is_current=1 AND c.from_org_id IN ({ph}) AND c.to_org_id IN ({ph}) ORDER BY c.confirmed_id",
                (*int_keys, *int_keys)):
            by = r["display_name"] or f"사용자 #{r['approved_by']}"
            if r["from_org_id"] == r["to_org_id"]:
                continue
            for kk in (r["from_org_id"], r["to_org_id"]):
                if "승인 결과" not in nodes[kk]["layers"]:
                    nodes[kk]["layers"].append("승인 결과")
            add_edge(kind="task_move", layer="승인 결과", style="appr", start=r["from_org_id"], end=r["to_org_id"],
                     arrow=True, label=f"업무 승계(승인): {_short(r['task_description'], 24)}",
                     status=f"승인 결과 — 승인자 {by}, {r['approved_at']}",
                     valid=f"적용일: {date_text(r['effective_date'], r['date_status'])} · 이동 범위: {r['move_scope']}",
                     observed="", evidence=_clean(r["task_description"]), ref_id=f"확정 업무 이동 #{r['confirmed_id']}")

    # ---- 소속 연결: 상위기관 링크가 그려진 기관 외에는 근거 없이 선을 잇지 않고 '소속 연결 미확인'으로 표시한다.
    if g["mode"] == "org":
        has_parent_edge = {e["end"] for e in edges if e["kind"] == "parent"}
        for k, n in nodes.items():
            if not isinstance(k, int) or k in has_parent_edge:
                continue
            rec = conn.execute("SELECT parent_code FROM ref_org_record WHERE org_id=? AND source_system='GCC' "
                               "AND source_code IS NOT NULL ORDER BY source_row_id LIMIT 1", (k,)).fetchone()
            if rec is None:
                n["unlinked"] = "소속 연결 미확인: 가져온 엑셀에 이 기관의 GCC 소속(상위기관) 자료가 없습니다."
            elif rec["parent_code"] in NO_PARENT:
                continue  # GCC 상 최상위 기관(상위기관 없음): 소속 연결이 필요 없다
            else:
                up = conn.execute("SELECT o.org_name FROM ref_org_relation r JOIN org o ON o.org_id=r.to_org_id "
                                  "WHERE r.relation_type='GCC_PARENT_LINK' AND r.from_org_id=? ORDER BY r.relation_id LIMIT 1",
                                  (k,)).fetchone()
                if up:
                    n["notes"].append(f"소속: {up['org_name']} (이 화면에는 표시하지 않음)")
                else:
                    n["unlinked"] = (f"소속 연결 미확인: 상위기관 코드 {rec['parent_code']} 가 "
                                     "가져온 자료에 없습니다.")

    for n in nodes.values():
        n["layers"] = [x for x in LAYERS if x in n["layers"]]
    g["root"] = root
    g["rows"] = [b for b in ROW_ORDER if any(n["band"] == b for n in nodes.values())]
    g["nodes"] = [nodes[k] for k in keys]
    g["edges"] = edges
    g["truncated"] = state["truncated"]
    g["omitted"] = state["omitted"]
    g["has_arrow"] = any(e["arrow"] for e in edges)
    g["empty"] = not edges and not any(n["works"] or n["records"] or n["cand_moves"] or n["notes"] or n["approved_info"]
                                       for n in g["nodes"])
    if g["mode"] == "code":
        g["notices"].append(("info", "이 건의 기관이 가져온 기관 자료에 없어 이 건의 검토 후보만 표시합니다. "
                                     "위에서 기관을 검색해 선택하면 원천 기재 자료를 볼 수 있습니다."))
    if g["mode"] == "org" and not chain and nodes[root]["unlinked"]:
        g["notices"].append(("info", "이 기관의 소속(상위기관) 자료가 없어 기관 단위 줄을 이을 수 없습니다 (소속 연결 미확인). "
                                     "선택 기관과 그 전·후신만 표시합니다."))
    if g["empty"]:
        g["notices"].append(("info", "표시할 자료 없음: 이 기관에 연결된 원천 기재·검토 후보·승인 결과가 없습니다."))
    elif not g["has_arrow"]:
        g["notices"].append(("warn", "업무 승계는 확인되지 않음(연결자료 부족): 승인된 업무 이동이 없어 화살표 없이 "
                                     "실제 기관관계와 담당 업무까지만 표시합니다."))
    if g["truncated"]:
        g["notices"].append(("info", f"일부만 표시: 노드가 {MAX_NODES}개를 넘어 {g['omitted']}개 기관은 생략했습니다."))
    return g


# ------------------------------------------------------------------ SVG 배치
NODE_W, GAP, PAD, FS, LINE_H, LINE_UNITS = 290, 70, 20, 14, 20, 18.5
ROW_LABEL_H, ROW_GAP = 30, 14


def _node_lines(n: dict) -> list[tuple[str, str]]:
    L: list[tuple[str, str]] = []
    for t in wrap(n["name"], LINE_UNITS - 2, 3):
        L.append((t, "title"))
    L.append(("GCC 코드: " + (", ".join(n["gcc"]) if n["gcc"] else "없음"), "meta"))
    L.append(("공통기관ID: " + (n["common"] or "없음"), "meta"))
    L.append(("OG 전거ID: " + (", ".join(n["og"]) if n["og"] else "미확보"), "meta"))
    L.append(("구분: " + (" · ".join(n["layers"]) if n["layers"] else "해당 없음"), "meta"))
    if n["unlinked"]:
        for t in wrap(n["unlinked"], LINE_UNITS, 3):
            L.append((t, "unk"))
    if n["approved_info"]:
        a = n["approved_info"]
        for t in wrap(f"[승인 결과] 기관정보 승인 — {a['by']}", LINE_UNITS, 2):
            L.append((t, "appr"))
    if n["records"]:
        L.append(("원천 관측기간 (유효기간 아님)", "sec"))
        for r in n["records"][:3]:
            L.append((f"{r['system']} {r['code'] or '코드없음'}: {r['period']}", "small"))
    if n["works"]:
        L.append(("담당 업무 [원천 기재 · 승계 미확인]", "sec"))
        shown = 5 if not n["band"] else 2  # 기관 단위·중간 줄은 노드를 작게 유지한다
        for w in n["works"][:shown]:
            for i, t in enumerate(wrap(w["text"], LINE_UNITS - 1, 2)):
                L.append((("· " if i == 0 else "  ") + t, "work"))
        if n["work_total"] > shown:
            L.append((f"외 {n['work_total'] - shown}건", "small"))
    if n["cand_moves"]:
        L.append(("후보(미확정) 업무 승계", "sec"))
        for t0 in n["cand_moves"][:2]:
            for t in wrap(t0, LINE_UNITS - 1, 3):
                L.append((t, "cand"))
        if len(n["cand_moves"]) > 2:
            L.append((f"외 {len(n['cand_moves']) - 2}건", "small"))
    for note in n["notes"][:2]:
        for t in wrap(note, LINE_UNITS, 2):
            L.append((t, "cand"))
    return L


def layout(g: dict) -> dict:
    """노드/선 좌표를 계산한다 (서버 렌더링 SVG 용). 줄(top/mid/선택 기관)별로 위에서 아래로 쌓고, 열은 계보 순서를 공유한다."""
    nodes = g["nodes"]
    for n in nodes:
        n["lines"] = _node_lines(n)
        n["h"] = 2 * 10 + len(n["lines"]) * LINE_H
        n["w"] = NODE_W
    levels = sorted({n["level"] for n in nodes})
    col_of = {lv: i for i, lv in enumerate(levels)}
    width = PAD * 2 + len(levels) * NODE_W + max(len(levels) - 1, 0) * GAP
    y, rows = PAD, []
    for b in ROW_ORDER:
        row_nodes = [n for n in nodes if n["band"] == b]
        if not row_nodes:
            continue
        top_y = y
        y += ROW_LABEL_H
        stack: dict = {}
        for n in sorted(row_nodes, key=lambda x: (x["level"], 0 if x["key"] == g["root"] else 1)):
            n["x"] = PAD + col_of[n["level"]] * (NODE_W + GAP)
            n["y"] = y + stack.get(n["level"], 0)
            stack[n["level"]] = stack.get(n["level"], 0) + n["h"] + 24
        row_h = max(stack.values())
        rows.append({"key": b, "label": ROW_TITLE[b], "y": top_y, "h": ROW_LABEL_H + row_h - 12})
        y += row_h + ROW_GAP
    height = y + PAD - ROW_GAP
    by_key = {n["key"]: n for n in nodes}
    pair_count: dict = {}
    for e in g["edges"]:
        pair_count[frozenset((e["start"], e["end"]))] = pair_count.get(frozenset((e["start"], e["end"])), 0) + 1
    pair_seen: dict = {}
    for e in g["edges"]:
        a, b = by_key[e["start"]], by_key[e["end"]]
        pk = frozenset((e["start"], e["end"]))
        i = pair_seen.get(pk, 0)
        pair_seen[pk] = i + 1
        off = (i - (pair_count[pk] - 1) / 2) * 22
        if a["x"] == b["x"]:  # 같은 열: 위/아래로 연결
            up, down = (a, b) if a["y"] <= b["y"] else (b, a)
            sx = ex = up["x"] + NODE_W / 2 + off
            if a is up:
                e["x1"], e["y1"], e["x2"], e["y2"] = sx, up["y"] + up["h"], ex, down["y"]
            else:
                e["x1"], e["y1"], e["x2"], e["y2"] = sx, down["y"], ex, up["y"] + up["h"]
            e["lx"], e["ly"] = sx + 8, (up["y"] + up["h"] + down["y"]) / 2
            e["anchor"] = "start"
        else:
            if a["x"] < b["x"]:
                e["x1"], e["x2"] = a["x"] + NODE_W, b["x"]
            else:
                e["x1"], e["x2"] = a["x"], b["x"] + NODE_W
            e["y1"], e["y2"] = a["y"] + a["h"] / 2 + off, b["y"] + b["h"] / 2 + off
            e["lx"], e["ly"] = (e["x1"] + e["x2"]) / 2, (e["y1"] + e["y2"]) / 2 - 6
            e["anchor"] = "middle"
        e["label_lines"] = wrap(e["label"], 9, 3)
    g["layout"] = {"width": width, "height": max(height, 60), "col_count": len(levels), "rows": rows}
    return g
