"""기능 담당 조직 계보도 — 타임라인 보기 (읽기 전용).

행 = 조직ID(공통기관ID, 없으면 GCC 코드). 회색 막대 = 원천 기재 기간(조직 존속, 승인된 존립기간 아님),
주황 막대 = 우리 시스템에서 승인된 업무 이동(confirmed_task_move, is_current=1)으로 확인된 기능 담당 구간만.
엑셀 원천 업무 원문은 담당기간이 아니므로 '원천 업무 기재 N건 · 담당기간 미확인' 표식만 붙인다.
날짜가 없으면 지어내지 않고 '미확인'으로 두며, 화살표는 날짜가 있는 승인된 업무 이동에만 그린다.
기관 집합은 lineage.build 를 재사용하고, 이 모듈은 SELECT 만 실행한다.
"""
from __future__ import annotations

import calendar
import json
from datetime import date

from . import lineage

ORG_MODE = "__org__"  # '기관 단위(기능 미선택)'
CANDIDATE_OPEN = ("승인 대기", "보류", "기각")
SUCC_LABEL = {
    "SUCCEEDS": "승계 관계",
    "SUCCEEDS_CANDIDATE": "승계 후보",
    "TRANSFER_CANDIDATE": "이관 후보",
    "GCC_PREVIOUS_CODE_LINK": "이전기관 코드 링크",
}

# SVG 배치 상수
W, LABEL_W, PAD_R, TOP, ROW_H, BAR_H = 1000, 220, 24, 18, 60, 26
X0, X1 = LABEL_W + 10, W - PAD_R


def _clean(t) -> str:
    return " ".join(str(t or "").split())


def _short(t, n) -> str:
    t = _clean(t)
    return t if len(t) <= n else t[: n - 1] + "…"


def parse_date(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def _known(v, status=None) -> date | None:
    """날짜 확인 상태가 '미확인'이면 값이 있어도 시간축에 쓰지 않는다."""
    return None if status == "미확인" else parse_date(v)


def _yf(d: date) -> float:
    days = 366 if calendar.isleap(d.year) else 365
    return d.year + (d.timetuple().tm_yday - 1) / days


def _ph(n: int) -> str:
    return ",".join("?" * n)


# ------------------------------------------------------------------ 데이터 조립
def build(conn, org_id: int | None = None, case=None, fn: str | None = None) -> dict:
    g = lineage.build(conn, org_id=org_id, case=case)
    t = {"root": g["root"], "mode": g["mode"], "rows": [], "events": [], "functions": [], "fn": None,
         "notices": [], "axis": None, "slider": None, "holders": {}, "arrows": [], "moves": [], "truncated": g["truncated"],
         "omitted": g["omitted"]}
    if g["root"] is None:
        return t

    nodes = g["nodes"]
    keys = [n["key"] for n in nodes]
    int_keys = [k for k in keys if isinstance(k, int)]
    code_key = {c: n["key"] for n in nodes for c in n["gcc"]}
    name_of = {n["key"]: n["name"] for n in nodes}
    codes_all = list(code_key)

    def name_by_org(oid):
        return name_of.get(oid) or (conn.execute("SELECT org_name FROM org WHERE org_id=?", (oid,)).fetchone() or {"org_name": f"기관 #{oid}"})["org_name"]

    # ---- 행과 원천 기재 기간(회색)
    rows: dict = {}
    for n in nodes:
        k = n["key"]
        row = {"key": k, "id_label": n["common"] or (n["gcc"][0] if n["gcc"] else "코드 없음"),
               "gcc": n["gcc"], "name": n["name"], "segs": [], "orange": [], "pre": [], "works_n": 0,
               "note": "", "is_root": k == g["root"], "source": ""}
        if isinstance(k, int):
            recs = conn.execute("SELECT full_name, source_code, source_start, source_end FROM ref_org_record "
                                "WHERE org_id=? AND source_system='GCC' ORDER BY source_row_id", (k,)).fetchall()
            for r in recs:
                row["segs"].append({"name": _clean(r["full_name"]) or n["name"], "code": r["source_code"],
                                    "start": parse_date(r["source_start"]), "end": parse_date(r["source_end"])})
            row["works_n"] = conn.execute("SELECT COUNT(*) FROM ref_org_work WHERE org_id=?", (k,)).fetchone()[0]
            if recs:
                row["source"] = "엑셀 원천(GCC)"
        if not row["segs"]:
            for code in n["gcc"]:
                r = conn.execute(
                    "SELECT g.org_name, g.created_date, g.abolished_date FROM gcc_record g "
                    "JOIN collection_run c ON c.collection_id=g.collection_id WHERE c.status='성공' AND g.gcc_code=? "
                    "ORDER BY g.collection_id DESC LIMIT 1", (code,)).fetchone()
                if r:
                    row["segs"].append({"name": _clean(r["org_name"]), "code": code,
                                        "start": parse_date(r["created_date"]), "end": parse_date(r["abolished_date"])})
            if row["segs"]:
                row["source"] = "우리 수집본(GCC)"
        row["segs"].sort(key=lambda s: (s["start"] is None, s["start"] or date.max))
        prev = None
        for s in row["segs"]:
            s["renamed"] = bool(prev and prev["name"] != s["name"])
            s["prev_name"] = prev["name"] if prev else None
            prev = s
        if not any(s["start"] or s["end"] for s in row["segs"]):
            row["note"] = "존속기간 미확인"
        rows[k] = row

    # ---- 추적할 기능 목록: 승인 → 후보 → 원천 업무 원문
    funcs: list[dict] = []
    seen = set()

    def add_fn(text, src):
        c = _clean(text)
        if c and c not in seen:
            seen.add(c)
            funcs.append({"text": c, "src": src})

    moves = []
    if int_keys:
        ph = _ph(len(int_keys))
        moves = conn.execute(
            f"SELECT c.*, u.display_name FROM confirmed_task_move c LEFT JOIN app_user u ON u.user_id=c.approved_by "
            f"WHERE c.is_current=1 AND c.from_org_id IN ({ph}) AND c.to_org_id IN ({ph}) AND c.from_org_id<>c.to_org_id "
            f"ORDER BY c.confirmed_id", (*int_keys, *int_keys)).fetchall()
    for m in moves:
        add_fn(m["task_description"], "승인 결과")
    cand_moves = []
    if codes_all:
        ph = _ph(len(codes_all))
        cand_moves = conn.execute(
            f"SELECT * FROM task_move_candidate WHERE review_status IN ('승인 대기','보류','기각') "
            f"AND (from_code IN ({ph}) OR to_code IN ({ph})) ORDER BY candidate_id", (*codes_all, *codes_all)).fetchall()
    for c in cand_moves:
        add_fn(c["task_description"], "검토 후보")
    for k in int_keys:
        for w in conn.execute("SELECT raw_work_text FROM ref_org_work WHERE org_id=? ORDER BY work_id", (k,)):
            add_fn(w["raw_work_text"], "원천 기재")
    t["functions"] = funcs

    fn_clean = _clean(fn) if fn else ""
    if fn == ORG_MODE:
        selected = None
    elif fn_clean and fn_clean in seen:
        selected = fn_clean
    else:
        selected = funcs[0]["text"] if funcs else None
    t["fn"] = selected

    def matches(text) -> bool:
        return selected is None or _clean(text) == selected

    # ---- 근거 (승인된 업무 이동의 원 후보 근거)
    def basis_of(m) -> str:
        parts = [f"{r['law_name']} {r['article_ref']}" for r in conn.execute(
            "SELECT s.law_name, p.article_ref FROM evidence e JOIN law_provision p ON p.provision_id=e.provision_id "
            "JOIN law_source s ON s.law_source_id=p.law_source_id WHERE e.candidate_type='task_move' AND e.candidate_id=? "
            "ORDER BY e.evidence_id", (m["source_candidate_id"],))]
        return "; ".join(dict.fromkeys(parts)) or "연결된 근거 없음"

    # ---- 주황(기능 담당) — 날짜가 있는 승인 이동만
    sel_moves = [m for m in moves if matches(m["task_description"])]
    dated = sorted([m for m in sel_moves if _known(m["effective_date"], m["date_status"])],
                   key=lambda m: (_known(m["effective_date"], m["date_status"]), m["confirmed_id"]))
    undated = [m for m in sel_moves if not _known(m["effective_date"], m["date_status"])]
    intervals = []  # (row_key, start, end|None, move)
    for m in dated:
        d = _known(m["effective_date"], m["date_status"])
        to_k, from_k = m["to_org_id"], m["from_org_id"]
        nxt = [x for x in dated if x["from_org_id"] == to_k and _clean(x["task_description"]) == _clean(m["task_description"])
               and _known(x["effective_date"], x["date_status"]) > d]
        end = None
        if nxt:
            end = _known(nxt[0]["effective_date"], nxt[0]["date_status"])
        else:
            segs = rows[to_k]["segs"]
            cont = [s for s in segs if (s["start"] is None or s["start"] <= d) and (s["end"] is None or s["end"] > d)]
            if cont:
                end = cont[0]["end"]
            else:
                later = [s["end"] for s in segs if s["end"] and s["end"] > d]
                end = max(later) if later else None
        intervals.append((to_k, d, end, m))
    t["moves"] = [{"from": m["from_org_id"], "to": to_k, "date": d, "id": m["confirmed_id"]}
                  for to_k, d, end, m in intervals]
    for to_k, d, end, m in intervals:
        rows[to_k]["orange"].append({"start": d, "end": end, "task": _clean(m["task_description"]), "id": m["confirmed_id"]})
        covered = any(k == m["from_org_id"] and s < d and (e is None or e >= d) for k, s, e, _ in intervals)
        if not covered:
            rows[m["from_org_id"]]["pre"].append({"date": d})
    t["notices"] = []
    if undated:
        t["notices"].append(("warn", f"승인됐지만 날짜 미확인이라 시간축에 표시하지 않음({len(undated)}건): "
                                     "아래 사건 표의 '날짜 미확인'에서 확인하세요."))

    # ---- 사건 표
    ev: list[dict] = []

    def event(d, typ, desc, layer):
        ev.append({"date": d, "year": d.year if d else None, "type": typ, "desc": desc, "layer": layer})

    for k, row in rows.items():
        for s in row["segs"]:
            src = row["source"] or "원천"
            if s["renamed"]:
                event(s["start"], "개칭", f"{s['prev_name']} → {s['name']} (같은 조직ID {row['id_label']} 유지, {src} 기재)",
                      "원천 기재")
            elif s["start"]:
                event(s["start"], "신설", f"{s['name']} — 원천 시작일 ({src} 기재)", "원천 기재")
            if s["end"]:
                event(s["end"], "종료", f"{s['name']} — 원천 종료일 ({src} 기재, 폐지 확정 아님)", "원천 기재")
            if not s["start"] and not s["end"]:
                event(None, "기간 미확인", f"{s['name']} — 원천 기재 시작·종료일 없음", "원천 기재")
    if codes_all:
        ph = _ph(len(codes_all))
        for r in conn.execute(
                f"SELECT ce.gcc_code, ce.old_value, ce.new_value, cr.base_date FROM change_event ce "
                f"JOIN collection_run cr ON cr.collection_id=ce.after_collection_id "
                f"WHERE ce.change_type='명칭' AND ce.gcc_code IN ({ph}) ORDER BY ce.change_id", codes_all):
            event(parse_date(r["base_date"]), "개칭",
                  f"{r['old_value']} → {r['new_value']} (수집 비교로 감지한 날짜, 개칭 시행일 미확인)", "원천 기재")
    if int_keys:
        ph = _ph(len(int_keys))
        for r in conn.execute(
                f"SELECT * FROM ref_org_relation WHERE relation_type IN ({_ph(len(SUCC_LABEL))}) "
                f"AND from_org_id IN ({ph}) AND to_org_id IN ({ph}) ORDER BY relation_id",
                (*SUCC_LABEL, *int_keys, *int_keys)):
            # 원천 파일: from=후속, to=이전
            d = parse_date(r["relation_valid_from"])
            event(d, SUCC_LABEL[r["relation_type"]],
                  f"{name_by_org(r['to_org_id'])} → {name_by_org(r['from_org_id'])} "
                  f"(원천 파일 검토 상태: {r['review_status_src'] or '미확인'}, 우리 승인 아님)", "원천 기재")
    for c in cand_moves:
        if not matches(c["task_description"]):
            continue
        event(_known(c["effective_date"], c["date_status"]), "업무 이동 후보",
              f"{_short(c['task_description'], 50)}: {c['from_name'] or c['from_code']} → {c['to_name'] or c['to_code']} "
              f"[{c['review_status']}] · 범위 {c['move_scope']} (미확정)", "검토 후보")
    if codes_all:
        ph = _ph(len(codes_all))
        for c in conn.execute(
                f"SELECT * FROM relation_candidate WHERE review_status IN ('승인 대기','보류','기각') "
                f"AND (from_code IN ({ph}) OR to_code IN ({ph})) ORDER BY candidate_id", (*codes_all, *codes_all)):
            event(_known(c["valid_from"], c["date_status"]), "관계 후보",
                  f"{c['relation_type']}: {c['from_name'] or c['from_code']} → {c['to_name'] or c['to_code']} "
                  f"[{c['review_status']}] (미확정)", "검토 후보")
    if int_keys:
        ph = _ph(len(int_keys))
        for r in conn.execute(
                f"SELECT c.*, u.display_name FROM confirmed_relation c LEFT JOIN app_user u ON u.user_id=c.approved_by "
                f"WHERE c.is_current=1 AND (c.from_org_id IN ({ph}) OR c.to_org_id IN ({ph})) ORDER BY c.confirmed_id",
                (*int_keys, *int_keys)):
            event(_known(r["valid_from"], r["date_status"]), "관계 승인",
                  f"{r['relation_type']}: {name_by_org(r['from_org_id'])} → {name_by_org(r['to_org_id'])} "
                  f"(승인 {r['display_name'] or '사용자 #' + str(r['approved_by'])})", "승인 결과")
    for m in sel_moves:
        event(_known(m["effective_date"], m["date_status"]), "업무 이관",
              f"{_short(m['task_description'], 50)}: {name_by_org(m['from_org_id'])} → {name_by_org(m['to_org_id'])} "
              f"· 범위 {m['move_scope']} · 근거 {basis_of(m)} · 승인 {m['display_name'] or '사용자 #' + str(m['approved_by'])} "
              f"({m['approved_at']})", "승인 결과")
    ev.sort(key=lambda e: (e["date"] is None, e["date"] or date.max))
    for e in ev:
        e["date_text"] = e["date"].isoformat() if e["date"] else "날짜 미확인"
    t["events"] = ev

    # ---- 시간축·슬라이더
    ds = [d for row in rows.values() for s in row["segs"] for d in (s["start"], s["end"]) if d]
    ds += [e["date"] for e in ev if e["date"]]
    ds += [o["start"] for row in rows.values() for o in row["orange"]]
    t["rows"] = sorted(rows.values(), key=lambda r: (min([s["start"] for s in r["segs"] if s["start"]] or [date.max]),
                                                      keys.index(r["key"])))
    if not ds:
        t["notices"].append(("info", "날짜 자료 없음: 표시할 원천·승인 날짜가 없어 시간축을 그리지 않습니다."))
        _org_notices(t, g)
        return t
    y_min, y_max = min(ds).year, max(ds).year
    a_min, a_max = y_min - 1, y_max + 1
    t["axis"] = {"min": a_min, "max": a_max + 1}  # 끝 연도 1월 1일까지 그린다
    t["slider"] = {"min": y_min - 1, "max": y_max + 1, "value": max(ds).year}

    holders = {}
    for y in range(t["slider"]["min"], t["slider"]["max"] + 1):
        lo, hi = date(y, 1, 1), date(y, 12, 31)
        hs = []
        for to_k, d, end, m in intervals:
            if d <= hi and (end is None or end > lo):
                hs.append({"name": rows[to_k]["name"], "id": rows[to_k]["id_label"],
                           "task": _short(m["task_description"], 40), "since": d.isoformat(),
                           "basis": basis_of(m), "by": m["display_name"] or f"사용자 #{m['approved_by']}",
                           "at": m["approved_at"]})
        holders[str(y)] = hs
    t["holders"] = holders
    _org_notices(t, g)
    return t


def _org_notices(t: dict, g: dict) -> None:
    if g["mode"] == "code":
        t["notices"].append(("info", "이 건의 기관이 가져온 기관 자료에 없어 이 건의 기관만 표시합니다. "
                                     "위에서 기관을 검색해 선택할 수 있습니다."))
    if not any(r["orange"] for r in t["rows"]):
        t["notices"].append(("info", "기능 담당 기간 미확인: 이 기능에 대해 날짜가 확인된 승인 업무 이동이 없어 "
                                     "주황 구간과 화살표 없이 조직 존속(원천 기재 기간)만 표시합니다."))
    if g["truncated"]:
        t["notices"].append(("info", f"일부만 표시: 기관이 {lineage.MAX_NODES}개를 넘어 {g['omitted']}개는 생략했습니다."))


# ------------------------------------------------------------------ SVG 배치
def layout(t: dict) -> dict:
    ax = t["axis"]
    n = len(t["rows"])
    plot_bottom = TOP + n * ROW_H
    t["svg"] = {"w": W, "h": plot_bottom + 46, "x0": X0, "x1": X1, "top": TOP, "bottom": plot_bottom, "ticks": [],
                "label_w": LABEL_W}
    if not ax:
        return t
    span = ax["max"] - ax["min"]

    def x_of(d: date) -> float:
        return round(X0 + (_yf(d) - ax["min"]) / span * (X1 - X0), 1)

    def x_year(y: int) -> float:
        return round(X0 + (y - ax["min"]) / span * (X1 - X0), 1)

    step = next(s for s in (1, 2, 5, 10, 20, 50) if span / s <= 12)
    first = ax["min"] + (-ax["min"]) % step
    t["svg"]["ticks"] = [{"x": x_year(y), "y": y} for y in range(first, ax["max"] + 1, step)]
    t["svg"]["year_x"] = {str(y): x_year(y) for y in range(t["slider"]["min"], t["slider"]["max"] + 1)}
    t["svg"]["line_x"] = x_year(t["slider"]["value"])
    row_y = {}
    for i, row in enumerate(t["rows"]):
        cy = TOP + i * ROW_H + ROW_H / 2
        row["cy"], row["by"] = cy, cy - BAR_H / 2
        row_y[row["key"]] = row
        for s in row["segs"]:
            if not s["start"] and not s["end"]:
                s["draw"] = False
                continue
            s["draw"] = True
            s["open_start"], s["open_end"] = s["start"] is None, s["end"] is None
            s["x1"] = X0 if s["open_start"] else x_of(s["start"])
            s["x2"] = X1 if s["open_end"] else x_of(s["end"])
            s["x2"] = max(s["x2"], s["x1"] + 2)
            s["label"] = _fit(s["name"], s["x2"] - s["x1"])
        for o in row["orange"]:
            o["x1"] = x_of(o["start"])
            o["open_end"] = o["end"] is None
            o["x2"] = max((X1 if o["open_end"] else x_of(o["end"])), o["x1"] + 3)
            o["label"] = _fit(o["task"], o["x2"] - o["x1"])
        for p in row["pre"]:
            p["x2"] = x_of(p["date"])
            p["x1"] = max(X0, p["x2"] - 34)
    # 화살표: 날짜 있는 승인 업무 이동마다 출발 행 → 도착 행 꺾인 선(적용일 x 위치)
    t["arrows"] = []
    for mv in t.get("moves", []):
        a, b = row_y.get(mv["from"]), row_y.get(mv["to"])
        if a is None or b is None or a is b:
            continue
        x = x_of(mv["date"])
        y1 = a["cy"] + (BAR_H / 2 if b["cy"] > a["cy"] else -BAR_H / 2)
        y2 = b["by"] - 2 if b["cy"] > a["cy"] else b["by"] + BAR_H + 2
        t["arrows"].append({"d": f"M{x - 16},{y1} H{x} V{y2}", "id": mv["id"]})
    return t


def _fit(text: str, width: float) -> str:
    cap = int(width / 14)
    if cap < 2:
        return ""
    return text if len(text) <= cap else text[: cap - 1] + "…"


def holders_json(t: dict) -> str:
    return json.dumps({"holders": t["holders"], "year_x": t.get("svg", {}).get("year_x", {})}, ensure_ascii=False)
