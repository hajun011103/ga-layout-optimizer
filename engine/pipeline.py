# -*- coding: utf-8 -*-
"""
pipeline.py — Gate 1 확인 → 최적화 → (현황 비교) → 도면·보고서. 결정론적 영역.

CLI(run.py)와 MCP 서버가 같은 경로를 쓴다. LLM 은 이 함수를 '호출' 만 한다.

  from pipeline import optimize_ir
  summary = optimize_ir(ir, "out/", effort="normal", asis=asis_dict)
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Dict, List, Optional

import asis as ASIS
import candidates as CAND
import drawing
import ga_core as G
import irhash
import nsga
import report as REPORT
import validate_spec
from run import result_payload, turbine_overlays, write_outputs
from version import ENGINE_VERSION, IR_SCHEMA_VERSION

# 노력 수준 → (iters, restarts). 결정론을 위해 '시간' 이 아니라 '반복 수' 로 정한다.
EFFORT = {
    "quick": (6000, 2),
    "normal": (20000, 3),
    "thorough": (60000, 6),
    # deep: 블록 수로 줄이지 않는다 — 제약이 많은 큰 문제(원전 보조건물 82개 블록)에서 위반 0 해를 찾을 때.
    # 82개 블록 기준 한 번에 40~60분.
    "deep": (150000, 6),
}
NO_SCALE = {"deep"}


class GateError(Exception):
    def __init__(self, errors: List[str]):
        super().__init__("GATE 1 불통과 — ERROR %d건" % len(errors))
        self.errors = errors


class AsIsMismatch(Exception):
    pass


def effort_params(effort: str, n_blocks: int) -> Dict[str, int]:
    iters, restarts = EFFORT.get(effort, EFFORT["normal"])
    # 블록이 많으면 반복당 비용이 O(n²) 로 커진다 — 반복 수를 줄여 시간을 맞춘다.
    if n_blocks > 60 and effort not in NO_SCALE:
        iters = max(3000, int(iters * 60 / n_blocks))
    return dict(iters=iters, restarts=restarts)


def optimize_ir(ir: dict, out: str, *, mode: str = "sa", effort: str = "normal",
                iters: Optional[int] = None, restarts: Optional[int] = None,
                pop: int = 60, gens: int = 60, seed: int = 20260826,
                asis: Optional[dict] = None, pick: Optional[int] = None,
                n_candidates: int = 0,
                progress: Optional[Callable[[str, float], None]] = None) -> dict:
    """
    반환 summary (JSON 직렬화 가능). 산출물은 out/ 에 쓴다.
    hard 위반이 있으면 summary['hard_violation_count'] > 0 — 호출자는 반드시 경고해야 한다.
    """
    t_start = time.time()
    os.makedirs(out, exist_ok=True)
    say = progress or (lambda msg, frac: None)

    rep = validate_spec.check_ir(ir, probe=False)
    if rep.ERR:
        raise GateError(rep.ERR)
    ir_hash = irhash.sha256_obj(ir)
    spec = G.spec_from_dict(ir)

    if asis is not None:
        want = ir.get("asis_sha256")
        got = ASIS.asis_hash(asis)
        if want and want != got:
            raise AsIsMismatch(f"asis.json 해시 {got[:12]}… 가 IR 에 기록된 {want[:12]}… 와 다르다 "
                               f"— 현황 배치 파일이 바뀌었다. 비교 결과를 신뢰할 수 없다.")

    p = effort_params(effort, len(spec.blocks))
    if iters:
        p["iters"] = int(iters)
    if restarts:
        p["restarts"] = int(restarts)

    manifest = dict(ir_sha256=ir_hash, seed=seed, engine_version=ENGINE_VERSION,
                    ir_schema_version=IR_SCHEMA_VERSION, mode=mode, effort=effort,
                    params=dict(p, pop=pop, gens=gens, pick=pick),
                    gate1=dict(errors=0, warnings=len(rep.WARN)),
                    asis_sha256=ASIS.asis_hash(asis) if asis is not None else None)

    # ---- 나이브 기준안 -------------------------------------------------------
    base = G.baseline(spec)
    base_m = ASIS.metrics(spec, base)

    # ---- 최적화 --------------------------------------------------------------
    say("최적화 시작", 0.02)
    pareto_rows = None
    if mode == "sa":
        total = p["iters"] * p["restarts"]

        def tick(r, it, bestF):
            say(f"탐색 {r+1}/{p['restarts']}회차 · {it:,}회 반복 · 현재 최선 {bestF:,.0f}",
                0.02 + 0.9 * (r * p["iters"] + it) / max(1, total))
        init = None
        if asis is not None and asis.get("blocks"):
            init = ASIS.warm_start(spec, asis, seed)      # 첫 재시작은 현황 배치에서 출발
            manifest["warm_start"] = "asis"
        if n_candidates:
            # 후보안 여러 개 — 사람이 고른다 (candidates.py). 첫 번째가 목적함수 최소 = 대표 결과.
            a_sol0 = ASIS.AsIsSolution(spec, asis) if (asis is not None and asis.get("blocks")) else None
            cands = CAND.generate(spec, iters=p["iters"], restarts=p["restarts"], seed=seed,
                                  init=init, asis_sol=a_sol0, k=n_candidates, tick=tick)
            best, Fo = cands[0]["sol"], cands[0]["F"]
            manifest["candidates"] = len(cands)
        else:
            cands = None
            best, Fo, _ = G.optimize(spec, iters=p["iters"], restarts=p["restarts"],
                                     seed=seed, verbose=False, tick=tick, init=init)
    elif mode == "nsga":
        say(f"NSGA-II pop {pop} × gen {gens}", 0.05)
        front, _pop = nsga.nsga2(spec, pop_size=pop, generations=gens, seed=seed)
        pareto_rows = nsga.front_table(front)
        knee = nsga.knee_point(front)
        k = pick if pick is not None else (knee if knee is not None else 0)
        best = front[k].sol
        best, Fo = G.repair_penetrations(best, best.evaluate())
        manifest["pareto_pick"] = k
        with open(os.path.join(out, "pareto_front.json"), "w", encoding="utf-8") as fp:
            json.dump(pareto_rows, fp, ensure_ascii=False, indent=2)
    else:
        raise ValueError(f"알 수 없는 mode {mode!r} — sa | nsga")
    say("도면 작성", 0.94)

    opt_m = ASIS.metrics(spec, best)
    paths = write_outputs(spec, best, out, "optimized", baseline_sol=base)
    payload = result_payload(spec, best)
    res_hash = irhash.sha256_obj(payload)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    cand_rows = None
    if mode == "sa" and n_candidates:
        cand_rows = []
        for c in cands:
            m = ASIS.metrics(spec, c["sol"])
            pl = result_payload(spec, c["sol"])
            cand_rows.append(dict(key=c.get("key"), label=c.get("label"), why=c.get("why"),
                                  profile=c["profile"], metrics=m, moved_m=c.get("moved_m"),
                                  result_sha256=irhash.sha256_obj(pl), result=pl))
        with open(os.path.join(out, "candidates.json"), "w", encoding="utf-8") as fp:
            json.dump(cand_rows, fp, ensure_ascii=False, indent=2)

    # ---- 현황(도면) 비교 -------------------------------------------------------
    asis_m = cmp_ = None
    asis_overlaps: List[str] = []
    if asis is not None and asis.get("blocks"):
        a_sol = ASIS.AsIsSolution(spec, asis)
        asis_m = ASIS.metrics(spec, a_sol, tol=ASIS.ASIS_TOL)
        asis_overlaps = ASIS.overlaps(spec, a_sol)
        F, t, geo, env, viols, sh = a_sol.evaluate(detail=True)
        for f in range(spec.n_floors):
            pth = os.path.join(out, f"GA_asis_floor{f+1}.svg")
            drawing.to_svg(spec, geo, env, f, pth, shadows=sh, violations=viols,
                           title_extra="AS-IS (도면에서 추출)")
            paths.append(pth)
        cmp_ = ASIS.compare(asis_m, opt_m)
        # 솔버가 현황보다 나은 해를 못 찾았으면 그렇게 말한다 — 개선을 지어내지 않는다.
        a_key = (asis_m["hard_violation_count"] > 0, asis_m["objective"])
        o_key = (opt_m["hard_violation_count"] > 0, opt_m["objective"])
        cmp_["asis_better"] = a_key < o_key
        if cmp_["asis_better"]:
            cmp_["claimable"] = False
            cmp_["verdict"] = ("같은 기준으로 채점하면 현황 배치가 더 낫다. 이 조건에서는 요건을 지키는 "
                               "더 나은 배치를 찾지 못했다. 현황 유지를 권고한다.")
        elif opt_m["hard_violation_count"]:
            cmp_["verdict"] = "최적화안에 필수 요건 위반이 남아 채택할 수 없다. 탐색량을 늘리거나 요건을 다시 검토해야 한다."
        else:
            cmp_["verdict"] = "최적화안이 같은 기준으로 현황보다 낫고 필수 요건 위반이 없다. 설계 검토 후보로 쓸 수 있다."

    summary = dict(
        project=spec.project,
        ir_sha256=ir_hash, seed=seed, engine_version=ENGINE_VERSION,
        result_sha256=res_hash, mode=mode, params=manifest["params"],
        blocks=len(spec.blocks), floors=spec.n_floors,
        optimized=opt_m, naive_baseline=base_m,
        asis=asis_m, comparison=cmp_, asis_clearance_overlaps=asis_overlaps[:50],
        hard_violation_count=opt_m["hard_violation_count"],
        pareto=pareto_rows,
        candidates=[{k: v for k, v in c.items() if k != "result"} for c in cand_rows] if cand_rows else None,
        gate1_warnings=rep.WARN,
        outputs=sorted(os.path.basename(x) for x in paths) + ["result.json", "run_manifest.json",
                                                              "report.html"],
        wall_time_s=round(time.time() - t_start, 2),
    )
    manifest.update(result_sha256=res_hash, objective=opt_m["objective"],
                    hard_violation_count=opt_m["hard_violation_count"],
                    hard_violations=payload["hard_violations"],
                    outputs=summary["outputs"], wall_time_s=summary["wall_time_s"])
    with open(os.path.join(out, "run_manifest.json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=2)
    REPORT.write_html(summary, out, os.path.join(out, "report.html"))
    say("완료", 1.0)
    return summary
