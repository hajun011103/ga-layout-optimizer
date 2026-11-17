# -*- coding: utf-8 -*-
"""
make_viewer_data.py — docs/data.js 를 만든다 (3D 비교 페이지용 데이터).

사례마다 MCP 도구와 같은 절차로 도면을 읽고(도면 → IR → 요건 → Gate 1 → 동결),
'기능 유지 요건'(examples/function/*.json — 근거 인용) 을 적용해 결정론적 솔버로 최적화한다.
좌표는 파서(현황)와 솔버(최적화)가 만든 그대로이며, 이 스크립트는 단위만 정리한다.

  python3 tools/make_viewer_data.py            # effort thorough, seed 20260826, 사례 병렬

공개 실제 도면은 저장소에 넣지 않는다 — bench/real/public/ 에 받아 두면 포함된다.
라이선스를 확인하지 못한 모델(publish_geometry=False)은 수치만 내보내고 형상은 내보내지 않는다.
이전 data.js 의 결과(면적만 본 최적화)는 previous_area_only 로 옮겨 비교에 쓴다.
"""
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
sys.path.insert(0, os.path.join(ROOT, "server"))

import asis as ASIS          # noqa: E402
import ga_core as G          # noqa: E402
import pipeline              # noqa: E402
import tools as T            # noqa: E402

OUT = os.path.join(ROOT, "docs", "data.js")
SEED = 20260826
N_CAND = 4          # 후보안 — 사람이 고른다 (engine/candidates.py)
FN = os.path.join(ROOT, "examples", "function")
PUB = os.path.join(ROOT, "..", "bench", "real", "public")

CASES = [
    dict(key="smr_aux", src="example:smr_aux", req="smr_aux.json",
         title="원전 보조건물", desc="가상 DXF · 3개 층 · 원전급 제약 + 계단 코어·복도 기능"),
    dict(key="chem_plant", src="example:chem_plant", req="chem_plant.json",
         title="석유화학 공정 플랜트", desc="가상 벡터 PDF 1:500 · 화재·폭발 이격 + 배관대 기능"),
    dict(key="fpso_topside", src="example:fpso_topside", req="fpso_topside.json",
         title="FPSO 해상 플랜트 상부", desc="가상 SVG · 2개 데크 · 위험구역 분리 + 크레인 작업반경"),
    dict(key="hospital_or", src="example:hospital_or", req="hospital_or.json",
         title="병원 수술부", desc="가상 PNG 스캔 · 청결/오염 분리 + 수술실 규모·침대 복도"),
    dict(key="clinic_real", real=True, src=os.path.join(PUB, "clinic", "Clinic_Architectural.ifc"),
         req="clinic_us.json",
         title="실제 도면: 의료·치과 클리닉", desc="공개 BIM(IFC) · 실제 건물 · 2개 층 + 옥탑 · 실 260여 개",
         source='BSI (2020) "Medical-Dental Test Files," buildingSMART International',
         source_url="https://github.com/buildingsmart-community/Community-Sample-Test-Files",
         license="CC BY 4.0",
         changes="IfcSpace 를 직사각형 블록으로 근사하고 규모·배치를 재계산했다 (원본 파일은 수정하지 않음)",
         publish_geometry=True),
    dict(key="institute_real", real=True, src=os.path.join(PUB, "kit", "AC20-Institute-Var-2.ifc"),
         req="office_de.json",
         title="실제 도면: 연구소 건물 (독일)", desc="공개 BIM(IFC) · KIT 연구소 · 지하~다락 5개 층 · 사무실·실험실",
         source="Karlsruhe Institute of Technology, AC20-Institute-Var-2.ifc",
         source_url="https://downloads.steptools.com/docs/stpfiles/ifc/",
         license="공개 샘플 모델 — 사용 조건은 저장소 소유자가 확인 (2026-11-01)",
         changes="IfcSpace 를 직사각형 블록으로 근사하고 규모·배치를 재계산했다 (원본 파일은 수정하지 않음)",
         publish_geometry=True),
]


def _r(v):
    return round(float(v), 3)


def _blocks(spec, geo, fh, notes):
    base = [sum(fh[:f]) for f in range(len(fh))]
    out = []
    for i, g in sorted(geo.items(), key=lambda kv: spec.blocks[kv[0]].id):
        b = spec.blocks[i]
        cl = b.clearance
        f = int(g["f"])
        rec = dict(
            id=b.id, name=b.name, kind=b.kind,
            group=b.fire_zone or b.zone or b.kind,
            f=f, x=_r(g["x"] + cl), y=_r(g["y"] + cl),
            w=_r(g["w"] - 2 * cl), d=_r(g["d"] - 2 * cl),
            z=_r(base[f] if f < len(base) else g.get("z", 0.0)),
            h=_r(min(b.h, sum(fh[f:]) if f < len(fh) else b.h)),
            pinned=bool(b.pinned))
        if b.circulation:
            rec["circ"] = b.circulation
        if notes.get(b.id):
            rec["note"] = notes[b.id]
        out.append(rec)
    return out


def access_paths(blocks, params, cl, fh=None):
    """
    실마다 '어디로 드나드는가' — CIRC-ACCESS 와 같은 기준(문 폭 이상 맞닿음, 벽 두께+이격 허용, via_rooms)으로
    복도에서 너비 우선 탐색해 부모(복도 또는 거쳐 가는 실)와 문 위치를 돌려준다. 그림용이며 판정은 엔진이 한다.
    """
    door = float(params.get("door_m", 0.9)); tol = float(params.get("touch_tol_m", 0.35))
    via = int(params.get("via_rooms", 1))
    ctypes = set(params.get("circulation_types") or ["corridor", "stair", "elevator", "lobby"])
    kinds = set(params.get("applies_to") or ["room"])
    import re as _re
    thr = _re.compile(params["through_ok"], _re.I) if params.get("through_ok") else None
    cfor = [(_re.compile(a, _re.I), _re.compile(b, _re.I)) for a, b in (params.get("corridor_for") or [])]
    nothr = _re.compile(params["no_through"], _re.I) if params.get("no_through") else None
    noent = set(params.get("no_entry_types") or [])
    ent_ok = _re.compile(params["entry_ok"], _re.I) if params.get("entry_ok") else None
    out = {}
    blocks = list(blocks)
    if fh:                                               # 여러 층을 관통하는 동선(계단 코어)은 위층에도 있다
        for b in list(blocks):
            if not b.get("circ"):
                continue
            acc_h, k = 0.0, b["f"]
            while k < len(fh) - 1 and acc_h + fh[k] < b["h"] - 1e-6:
                acc_h += fh[k]; k += 1
                blocks.append(dict(b, f=k))
    for f in sorted({b["f"] for b in blocks}):
        fl = [b for b in blocks if b["f"] == f]
        circ = [b for b in fl if b.get("circ") in ctypes]
        rooms = [b for b in fl if not b.get("circ") and b["kind"] in kinds]

        def touch(a, b):
            t = tol + cl.get(a["id"], 0.0) + cl.get(b["id"], 0.0)
            gx = max(b["x"] - (a["x"] + a["w"]), a["x"] - (b["x"] + b["w"]))
            gy = max(b["y"] - (a["y"] + a["d"]), a["y"] - (b["y"] + b["d"]))
            if gx <= t and gy < 0 and -gy >= door:      # 좌우로 맞닿음 — 문은 세로 벽에
                y0 = max(a["y"], b["y"]); y1 = min(a["y"] + a["d"], b["y"] + b["d"])
                x = (a["x"] + a["w"] + b["x"]) / 2 if a["x"] < b["x"] else (b["x"] + b["w"] + a["x"]) / 2
                L = min(door * 1.2, y1 - y0)
                return [round(x - 0.15, 3), round((y0 + y1) / 2 - L / 2, 3), 0.3, round(L, 3)]
            if gy <= t and gx < 0 and -gx >= door:      # 위아래로 맞닿음 — 문은 가로 벽에
                x0 = max(a["x"], b["x"]); x1 = min(a["x"] + a["w"], b["x"] + b["w"])
                y = (a["y"] + a["d"] + b["y"]) / 2 if a["y"] < b["y"] else (b["y"] + b["d"] + a["y"]) / 2
                L = min(door * 1.2, x1 - x0)
                return [round((x0 + x1) / 2 - L / 2, 3), round(y - 0.15, 3), round(L, 3), 0.3]
            return None
        depth, frontier = {}, []
        def okc(r):
            base = circ
            if noent and not (ent_ok and ent_ok.search((r.get("name") or "") + " " + r["id"])):
                base = [c for c in circ if c.get("circ") not in noent]
            for rx, crx in cfor:
                if rx.search((r.get("name") or "") + " " + r["id"]):
                    return [c for c in base if crx.search((c.get("name") or "") + " " + c["id"])]
            return base
        for r in rooms:                                  # 복도에 바로 닿는 실
            best = None
            for c in okc(r):
                d = touch(r, c)
                if d:
                    best = (c, d); break
            if best:
                depth[r["id"]] = 1; frontier.append(r)
                out[r["id"]] = dict(to=best[0]["id"], door=best[1], depth=1, f=f)
        k = 1
        while frontier and k < via + 1:                  # 다른 실을 거쳐 들어가는 실
            k += 1; nxt = []
            for r in rooms:
                if r["id"] in depth:
                    continue
                for q in frontier:
                    if (thr and not thr.search((q.get("name") or "") + " " + q["id"])) or (nothr and nothr.search((q.get("name") or "") + " " + q["id"])):
                        continue                         # 통로로 쓰면 안 되는 실은 거치지 않는다
                    d = touch(r, q)
                    if d:
                        depth[r["id"]] = k; nxt.append(r)
                        out[r["id"]] = dict(to=q["id"], door=d, depth=k, f=f); break
            frontier = nxt
    return out


def _metrics(m, viols, tol=1e-6):
    hard = [v for v in viols if v.hard and v.amount > tol]
    return dict(width=m["width_m"], depth=m["depth_m"], footprint=m["footprint_m2"],
                volume=m["volume_m3"], hard=m["hard_violation_count"], link=m["link_cost"],
                violations=[dict(id=v.cid, detail=v.detail, blocks=list(v.blocks)) for v in hard])


def run_case(c):
    tmp = tempfile.mkdtemp(prefix=f"viewer-{c['key']}-")
    try:
        t = T.Tools(T.Workspace(os.path.join(tmp, "ws"), allow_local_paths=True, allow_url_fetch=False))
        with open(os.path.join(FN, c["req"]), encoding="utf-8") as fp:
            req = json.load(fp)
        if c["src"].startswith("example:"):
            ex = c["src"].split(":", 1)[1]
            pid = t.ga_load_example(name=ex)["project"]["project_id"]
            t.ga_apply_example_requirements(project_id=pid, name=ex)
        else:
            r = t.ga_import_drawing(path=c["src"], options=req.get("import_options"))
            pid = t.ga_create_project(takeoff_ids=[r["takeoff_id"]], project_name=c["key"],
                                      options=req.get("import_options"))["project_id"]
        patch = {k: v for k, v in req.items() if not k.startswith("_") and k != "import_options"}
        t.ga_update_ir(project_id=pid, patch=patch, reason=f"기능 유지 요건 {c['req']}")
        if not t.ga_validate(project_id=pid)["passed"]:
            raise SystemExit(f"{c['key']}: Gate 1 실패")
        t.ga_freeze(project_id=pid, approved_by="make_viewer_data", acknowledged_warnings=True)
        d, ir = t._load_ir(pid)
        asis = t.ws.rjson(os.path.join(d, "asis.json"))
        out = os.path.join(tmp, "out")
        s = pipeline.optimize_ir(ir, out, effort="thorough", seed=SEED, asis=asis, n_candidates=N_CAND)

        spec = G.spec_from_dict(ir)
        notes = {b["id"]: b["_program"] for b in ir["blocks"] if b.get("_program")}
        a_sol = ASIS.AsIsSolution(spec, asis)
        a_viols = a_sol.evaluate(detail=True)[4]
        fh = list(a_sol.fh)
        with open(os.path.join(out, "result.json"), encoding="utf-8") as fp:
            res = json.load(fp)
        idx = {b.id: i for i, b in enumerate(spec.blocks)}
        geo = {idx[b["id"]]: dict(x=b["x"], y=b["y"], w=b["w"], d=b["d"], f=b["f"], z=b["z"])
               for b in res["blocks"]}
        o_viols = [type("V", (), dict(cid=v["id"], detail=v["detail"], amount=v["amount"],
                                      hard=True, blocks=v["blocks"])) for v in res["hard_violations"]]
        cmp_ = s["comparison"]
        reqs = [dict(id=x["id"], why=x.get("_why", "")) for x in req.get("set", {}).get("constraints", [])]
        acc = next((x["params"] for x in req.get("set", {}).get("constraints", []) if x["id"] == "CIRC-ACCESS"), None)
        clmap = {b.id: b.clearance for b in spec.blocks if b.clearance}
        rec = dict(
            key=c["key"], title=c["title"], desc=c["desc"], floors=spec.n_floors, floor_heights=fh,
            asis=dict(blocks=_blocks(spec, a_sol.geo, fh, {}), **_metrics(s["asis"], a_viols, ASIS.ASIS_TOL)),
            optimized=dict(blocks=_blocks(spec, geo, fh, notes), **_metrics(s["optimized"], o_viols)),
            footprint_reduction_pct=cmp_["footprint_reduction_pct"],
            asis_better=bool(cmp_.get("asis_better")), verdict=cmp_["verdict"],
            removed=res.get("removed", []), program=ir.get("_program_report", []),
            requirements=reqs, req_note=req.get("_note", ""), req_sources=req.get("_sources", {}),
            result_sha256=s["result_sha256"], seed=SEED)
        with open(os.path.join(out, "candidates.json"), encoding="utf-8") as fp:
            cands = json.load(fp)
        am = s["asis"]
        rec["candidates"] = []
        for k in cands:
            r2 = k["result"]
            g2 = {idx[b["id"]]: dict(x=b["x"], y=b["y"], w=b["w"], d=b["d"], f=b["f"], z=b["z"]) for b in r2["blocks"]}
            v2 = [type("V", (), dict(cid=v["id"], detail=v["detail"], amount=v["amount"], hard=True,
                                     blocks=v["blocks"])) for v in r2["hard_violations"]]
            m = k["metrics"]
            lay = dict(blocks=_blocks(spec, g2, fh, notes), **_metrics(m, v2), removed=r2.get("removed", []))
            if acc:
                lay["access"] = access_paths(lay["blocks"], acc, clmap, fh)
            rec["candidates"].append(dict(
                key=k["key"], label=k["label"], why=k["why"], profile=k["profile"], moved_m=k.get("moved_m"),
                pct=round((1 - m["footprint_m2"] / am["footprint_m2"]) * 100, 2),
                asis_better=(am["hard_violation_count"] > 0, am["objective"]) < (m["hard_violation_count"] > 0, m["objective"]),
                result_sha256=k["result_sha256"], layout=lay))
        if acc:
            rec["asis"]["access"] = access_paths(rec["asis"]["blocks"], acc, clmap, fh)
            rec["optimized"]["access"] = access_paths(rec["optimized"]["blocks"], acc, clmap, fh)
        rs = req.get("_research")
        if rs:
            with open(os.path.join(FN, rs["file"]), encoding="utf-8") as fp:
                R = json.load(fp)
            ap = set(rs.get("applied", []))
            rec["research"] = dict(
                summary=R.get("summary_ko", ""), note=rs.get("note", ""),
                open_questions=R.get("open_questions", [])[:8],
                items=[dict(id=x["id"], what=x.get("what_ko", ""), why=x.get("why_ko", ""), rule=x.get("rule", ""),
                            status=(x.get("status") or "").split()[0].split("(")[0], priority=x.get("priority", ""),
                            applied=x["id"] in ap,
                            source={k: (x.get("source") or {}).get(k) for k in ("title", "section", "url")})
                       for x in R.get("requirements", [])])
        for k in ("real", "source", "source_url", "license", "changes"):
            if k in c:
                rec[k] = c[k]
        if c.get("real") and not c.get("publish_geometry", False):
            rec["asis"]["blocks"] = []
            rec["optimized"]["blocks"] = []
            for k in rec["candidates"]:
                k["layout"]["blocks"] = []
            rec["geometry_withheld"] = True
        return rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _previous():
    """지난 data.js 의 결과 — 면적만 본 최적화. 이미 옮겨 둔 것이 있으면 그것을 유지한다."""
    if not os.path.exists(OUT):
        return {}
    with open(OUT, encoding="utf-8") as fp:
        src = fp.read()
    try:
        old = json.loads(src[src.index("=") + 1:].strip().rstrip(";"))
    except ValueError:
        return {}
    # 기준선(면적만 본 실행)은 이미 previous_area_only 로 옮겨져 있다 — 그것만 이어받는다.
    # (예전에는 없으면 지난 결과를 기준선으로 썼는데, 기능 유지 결과가 '이전(면적만)' 칸에 들어가는 버그가 됐다.)
    return {c["key"]: c["previous_area_only"] for c in old if c.get("previous_area_only")}


def main():
    prev = _previous()
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
    keep = []
    if only:   # 나머지 사례는 기존 data.js 를 그대로 둔다
        with open(OUT, encoding="utf-8") as fp:
            txt = fp.read()
        keep = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
    jobs = []
    for c in CASES:
        if only and c["key"] not in only:
            continue
        if not c["src"].startswith("example:") and not os.path.exists(c["src"]):
            print(f"  {c['key']}: 도면 파일 없음 ({c['src']}) — 건너뛴다. 받는 곳: {c.get('source_url')}")
            continue
        jobs.append(c)
    with mp.Pool(min(len(jobs), max(1, os.cpu_count() or 1))) as pool:
        cases = pool.map(run_case, jobs)
    if only:
        got = {c["key"]: c for c in cases}
        cases = [got.get(c["key"], c) for c in keep]
    for c in cases:
        if c["key"] in prev:
            c["previous_area_only"] = prev[c["key"]]
        p = c.get("previous_area_only")
        print(f"  {c['key']:15s} 현황 {c['asis']['footprint']:>9,.0f} m² (위반 {c['asis']['hard']}) → "
              f"기능 유지 {c['optimized']['footprint']:>9,.0f} m² (위반 {c['optimized']['hard']}) "
              f"{c['footprint_reduction_pct']:+.1f}% · 인접 {c['asis']['link']:,.0f}→{c['optimized']['link']:,.0f}"
              + (f" | 이전(면적만) {p['pct']}%" if p else ""))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fp:
        fp.write("// 자동 생성: python3 tools/make_viewer_data.py — 손으로 고치지 말 것\n")
        fp.write("window.GA_CASES = ")
        json.dump(cases, fp, ensure_ascii=False, separators=(",", ":"))
        fp.write(";\n")
    print(f"→ {os.path.relpath(OUT, ROOT)} ({os.path.getsize(OUT):,} bytes)")


if __name__ == "__main__":
    main()
