# -*- coding: utf-8 -*-
"""
run.py — 최적화 실행 (결정론적 영역. LLM 미개입)

  python3 run.py spec.json --mode sa   --iters 100000 --restarts 6
  python3 run.py spec.json --mode nsga --pop 80 --gens 80
  python3 run.py spec.json --mode hierarchical --areas 3 --sweep-floors 1-5
  python3 run.py spec.json --sweep-floors 1-6            # 층수 민감도만

Gate 1 을 통과하지 못한 IR 로는 실행되지 않는다 (ERROR 1건이라도 있으면 즉시 중단).
모든 난수는 seed 로 고정되고, {IR 해시, seed, 엔진 버전, 결과 해시} 를
run_manifest.json 에 기록한다. 같은 IR 해시 + 같은 seed → 같은 결과 해시.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
import time
from typing import Dict, List, Optional, Sequence

import constraints as CONS
import decompose
import drawing
import ga_core as G
import irhash
import nsga
import validate_spec
from version import ENGINE_VERSION, IR_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 보고
# ---------------------------------------------------------------------------

def report(tag, F, terms, env, spec):
    W, D, H = env
    print(f"\n── {tag} " + "─" * max(2, 58 - len(tag)))
    print(f"  외피        : {W:7.1f} m (W) × {D:6.1f} m (D) × {H:5.1f} m (H)")
    print(f"  건축면적    : {W*D:10,.0f} m²      건물체적 : {W*D*H:12,.0f} m³")
    print(f"  목적함수 F  : {F:12,.1f}")
    print("  ── 항별 기여 ──")
    for k, v in sorted(terms.items(), key=lambda kv: -kv[1]):
        if abs(v) < 1e-9:
            continue
        hard = k in G.HARD_TERMS or (k.startswith("constraint.")
                                     and CONS.REGISTRY[k.split(".", 1)[1]].hard)
        mark = " ← HARD" if (hard and v > 1e-6) else ""
        print(f"     {k:<26s} {v:14,.1f}  ({v/F*100:5.1f}%){mark}")


def violation_banner(viols, terms) -> List[str]:
    """★ hard 제약 위반이 0 이 아니면 결과 요약 최상단에 뜨는 경고."""
    lines = G.hard_violations(None, terms, viols)
    if not lines:
        return []
    out = ["", "!" * 74,
           "  ★★ HARD 제약 위반 — 이 해는 받아들일 수 없다 (NOT AN ACCEPTABLE SOLUTION)",
           "  도면상으로는 정상으로 보인다. 기계적으로만 확인 가능한 항목이다.",
           "!" * 74]
    for m in lines:
        out.append(f"  · {m}")
    out.append("!" * 74)
    return out


def turbine_overlays(spec: G.Spec, sol: G.Solution, geo) -> List[dict]:
    """도면에 그릴 터빈 미사일 부채꼴."""
    out = []
    for c in spec.constraints or []:
        if c.get("id") != "TURBINE-25":
            continue
        p = c.get("params") or {}
        ha = p.get("half_angle_deg", 25.0)
        for i, az in sorted(sol.turbine_azimuth.items()):
            if i in geo:
                out.append(dict(x=geo[i]["cx"], y=geo[i]["cy"], azimuth_deg=az,
                                half_angle_deg=ha, label=spec.blocks[i].id))
        for u in (p.get("other_units") or []):
            out.append(dict(x=float(u["x"]), y=float(u["y"]),
                            azimuth_deg=float(u["azimuth_deg"]),
                            half_angle_deg=ha, label=str(u.get("id", "TURBINE"))))
    return out


def result_payload(spec: G.Spec, sol: G.Solution) -> dict:
    """결과 해시 대상 — 좌표·항별 값만. 시각·경로 같은 비결정 요소는 넣지 않는다."""
    F, terms, geo, env, viols, shadows = sol.evaluate(detail=True)
    W, D, H = env
    return dict(
        envelope=[round(W, 6), round(D, 6), round(H, 6)],
        objective=round(F, 6),
        terms={k: round(v, 6) for k, v in sorted(terms.items())},
        blocks=[dict(id=spec.blocks[i].id, f=g["f"],
                     x=round(g["x"], 6), y=round(g["y"], 6), z=round(g["z"], 6),
                     w=round(g["w"], 6), d=round(g["d"], 6))
                for i, g in sorted(geo.items(), key=lambda kv: spec.blocks[kv[0]].id)],
        penetrations=[dict(id=spec.blocks[i].id, floor=f,
                           x=round(s["x"], 6), y=round(s["y"], 6))
                      for (i, f), s in sorted(shadows.items())],
        turbine_azimuth={spec.blocks[i].id: az
                         for i, az in sorted(sol.turbine_azimuth.items())},
        hard_violations=[dict(id=v.cid, amount=round(v.amount, 6), unit=v.unit,
                              blocks=v.blocks, detail=v.detail)
                         for v in viols if v.hard and v.amount > 1e-6],
    )


def write_outputs(spec: G.Spec, sol: G.Solution, outdir: str, tag: str,
                  baseline_sol: Optional[G.Solution] = None) -> List[str]:
    F, terms, geo, env, viols, shadows = sol.evaluate(detail=True)
    tb = turbine_overlays(spec, sol, geo)
    paths = []
    for f in range(spec.n_floors):
        p = os.path.join(outdir, f"GA_{tag}_floor{f+1}.svg")
        drawing.to_svg(spec, geo, env, f, p, shadows=shadows, violations=viols,
                       turbines=tb, title_extra=tag.upper())
        paths.append(p)
    paths.append(drawing.to_dxf(spec, geo, env,
                                os.path.join(outdir, f"GA_{tag}.dxf"),
                                shadows=shadows, violations=viols, turbines=tb))
    paths.append(drawing.to_json(spec, geo, env, terms, F,
                                 os.path.join(outdir, f"GA_{tag}.json"),
                                 shadows=shadows, violations=viols))
    if baseline_sol is not None:
        Fb, tbm, gb, eb, vb, shb = baseline_sol.evaluate(detail=True)
        p = os.path.join(outdir, "GA_baseline_floor1.svg")
        drawing.to_svg(spec, gb, eb, 0, p, shadows=shb, violations=vb,
                       title_extra="BASELINE (for comparison)")
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------

def parse_range(s: str) -> List[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SMR GA 배치 최적화 실행")
    ap.add_argument("spec")
    ap.add_argument("--mode", default="sa", choices=("sa", "nsga", "hierarchical"))
    ap.add_argument("--iters", type=int, default=60000)
    ap.add_argument("--restarts", type=int, default=4)
    ap.add_argument("--pop", type=int, default=60)
    ap.add_argument("--gens", type=int, default=60)
    ap.add_argument("--areas", type=int, default=3)
    ap.add_argument("--sweep-floors", default=None,
                    help="예: 1-6 또는 1,2,4 — 층수 민감도 스윕")
    ap.add_argument("--pick", type=int, default=None,
                    help="nsga 모드에서 도면으로 출력할 Pareto 해 index")
    ap.add_argument("--seed", type=int, default=20260826)
    ap.add_argument("--out", default="out")
    ap.add_argument("--skip-gate", action="store_true",
                    help="※ 시험용. Gate 1 을 건너뛴다. 실무에서 쓰지 말 것.")
    a = ap.parse_args(argv)

    with open(a.spec, encoding="utf-8") as fp:
        ir = json.load(fp)
    ir_hash = irhash.sha256_obj(ir)

    # ---- Gate 1 : ERROR 가 1건이라도 있으면 실행하지 않는다 ------------------
    rep = validate_spec.check_ir(ir, probe=False)
    if rep.ERR and not a.skip_gate:
        print("=" * 74)
        print("  GATE 1 불통과 — 최적화를 실행하지 않는다.")
        print("=" * 74)
        for m in rep.ERR:
            print(f"  [ERROR] {m}")
        print("-" * 74)
        print("  IR 을 수정한 뒤 validate_spec.py 로 다시 확인할 것.")
        return 1
    if a.skip_gate and rep.ERR:
        print("  ※ --skip-gate 로 ERROR 를 무시하고 실행한다. 결과를 설계에 쓰지 말 것.")

    spec = G.spec_from_dict(ir)
    os.makedirs(a.out, exist_ok=True)
    print(f"\n{'='*74}\n  {spec.project}\n{'='*74}")
    print(f"  블록 {len(spec.blocks)}개 · 연결 {len(spec.links)}개 · "
          f"이격제약 {len(spec.separations)}개 · 표고제약 {len(spec.elevations)}개 · "
          f"제약라이브러리 {len(spec.constraints)}건 · {spec.n_floors}개 층")
    print(f"  IR SHA-256 : {ir_hash}")
    print(f"  seed       : {a.seed} · 엔진 {ENGINE_VERSION}")

    manifest = dict(
        ir_path=os.path.relpath(a.spec),   # 절대경로(사용자 디렉터리)를 기록하지 않는다
        ir_sha256=ir_hash,
        seed=a.seed,
        engine_version=ENGINE_VERSION,
        ir_schema_version=IR_SCHEMA_VERSION,
        mode=a.mode,
        params=dict(iters=a.iters, restarts=a.restarts, pop=a.pop, gens=a.gens,
                    areas=a.areas, sweep_floors=a.sweep_floors, pick=a.pick),
        gate1=dict(errors=len(rep.ERR), warnings=len(rep.WARN),
                   skipped=bool(a.skip_gate)),
    )
    t_start = time.time()

    # ---- 층수 민감도 스윕 --------------------------------------------------
    sweep_rows = None
    # hierarchical 은 2단계에서 같은 스윕을 하므로 여기서 중복 실행하지 않는다.
    if a.sweep_floors and a.mode != "hierarchical":
        print(f"\n── 층수 민감도 스윕 ({a.sweep_floors}) ────────────────────")
        print("  근거: Ejeh 2019 — 요소플랜트 1층 260,942 → 4층 117,431 (−55.0%),")
        print("        CDU 1층 1,112,095 → 7층 592,322 (−46.7%).")
        print("        층수를 고정하면 이 개선 여지가 통째로 사라진다.")
        sweep_rows = decompose.floor_sweep(
            spec, parse_range(a.sweep_floors), iters=max(2000, a.iters // 5),
            restarts=max(1, a.restarts // 2), seed=a.seed)
        with open(os.path.join(a.out, "floor_sweep.json"), "w", encoding="utf-8") as fp:
            json.dump(sweep_rows, fp, ensure_ascii=False, indent=2)
        manifest["floor_sweep"] = sweep_rows

    # ---- 기준안 ------------------------------------------------------------
    base = G.baseline(spec)
    Fb, tb_, gb, eb, vb, shb = base.evaluate(detail=True)
    report("BASELINE (수작업식 행 배치 — 나이브한 기준안)", Fb, tb_, eb, spec)
    print("  ※ 이 기준안은 나이브하다. 여기서 나오는 개선율을 성과로 제시하지 말 것.")

    # ---- 최적화 ------------------------------------------------------------
    pareto_rows = None
    if a.mode == "sa":
        print(f"\n── OPTIMIZING (SA, {a.iters:,} iter × {a.restarts} restart) ──────")
        t0 = time.time()
        best, Fo, _ = G.optimize(spec, iters=a.iters, restarts=a.restarts,
                                 seed=a.seed)
        print(f"  소요 {time.time()-t0:.1f} s")
    elif a.mode == "nsga":
        print(f"\n── OPTIMIZING (NSGA-II, pop {a.pop} × {a.gens} gen, 3목적) ──────")
        print("  목적: ①건물 체적 ②연결비용 ③방사선구역 분산")
        print("  ※ 가중치를 미리 정하지 않는다. 설계자가 Pareto 에서 고른다.")
        t0 = time.time()
        front, _pop = nsga.nsga2(spec, pop_size=a.pop, generations=a.gens,
                                 seed=a.seed)
        print(f"  소요 {time.time()-t0:.1f} s · Pareto 해 {len(front)}개")
        pareto_rows = nsga.front_table(front)
        with open(os.path.join(a.out, "pareto_front.json"), "w",
                  encoding="utf-8") as fp:
            json.dump(pareto_rows, fp, ensure_ascii=False, indent=2)
        knee = nsga.knee_point(front)
        print("\n  ── PARETO 프론트 (설계자가 고를 것) ──")
        print(f"  {'#':>3} {'실현':>4} {'체적[m³]':>13} {'연결비용':>12} "
              f"{'구역분산':>10} {'hard위반':>8}")
        for r in pareto_rows:
            mk = " ←knee" if r["index"] == knee else ""
            print(f"  {r['index']:>3} {'OK' if r['feasible'] else 'X':>4} "
                  f"{r['volume_m3']:>13,.0f} {r['link_cost']:>12,.0f} "
                  f"{r['zone_dispersion']:>10,.2f} "
                  f"{len(r['hard_violations']):>8}{mk}")
        pick = a.pick if a.pick is not None else (knee if knee is not None else 0)
        print(f"\n  도면 출력 대상: Pareto #{pick} "
              f"({'--pick 지정' if a.pick is not None else 'knee 해 — 추천이 아니라 기본값'})")
        best = front[pick].sol
        # 선택된 해에도 관통부 정렬 국소 수복을 적용한다 (결정론적, 개선될 때만)
        best, Fo = G.repair_penetrations(best, best.evaluate())
        manifest["pareto_pick"] = pick
    else:
        print(f"\n── OPTIMIZING (계층분해 3단계) ────────────────────────────")
        res = decompose.hierarchical(
            spec, n_areas=a.areas,
            floors=parse_range(a.sweep_floors) if a.sweep_floors else (1, 2, 3, 4),
            iters=a.iters, restarts=a.restarts, seed=a.seed)
        best, spec, Fo = res["solution"], res["spec"], res["F"]
        manifest["partition"] = res["partition"]
        manifest["chosen_floors"] = res["chosen_floors"]
        with open(os.path.join(a.out, "decomposition.json"), "w",
                  encoding="utf-8") as fp:
            json.dump(dict(partition=res["partition"], sweep=res["sweep"],
                           chosen_floors=res["chosen_floors"]),
                      fp, ensure_ascii=False, indent=2)

    Fo, to_, go, eo, vo, sho = best.evaluate(detail=True)

    # ---- ★ hard 위반 경고를 최상단에 -----------------------------------------
    banner = violation_banner(vo, to_)
    for ln in banner:
        print(ln)

    report("OPTIMIZED", Fo, to_, eo, spec)

    # ---- 개선율 -------------------------------------------------------------
    print(f"\n── 개선율 (기준안 대비) ──────────────────────────────────")
    def imp(x, y):
        return (x - y) / x * 100 if x else 0.0
    Wb, Db, Hb = eb; Wo, Do, Ho = eo
    rows = [
        ("건축면적 [m²]", Wb * Db, Wo * Do),
        ("건물체적 [m³]", Wb * Db * Hb, Wo * Do * Ho),
        ("배관·케이블 항", tb_.get("link", 0), to_.get("link", 0)),
        ("구역분산 항", tb_.get("zone", 0), to_.get("zone", 0)),
        ("이격위반(hard)", tb_.get("sep", 0), to_.get("sep", 0)),
        ("총 목적함수 F", Fb, Fo),
    ]
    print(f"  {'항목':<18s}{'기준안':>16s}{'최적화':>16s}{'개선':>10s}")
    for n, x, y in rows:
        print(f"  {n:<18s}{x:>16,.0f}{y:>16,.0f}{imp(x,y):>9.1f}%")
    print("  ※ 기준안이 나이브하므로 이 개선율은 상한이다. 실제 기대치는 −10~15%.")

    # ---- 산출물 --------------------------------------------------------------
    paths = write_outputs(spec, best, a.out, "optimized", baseline_sol=base)
    payload = result_payload(spec, best)
    res_hash = irhash.sha256_obj(payload)
    with open(os.path.join(a.out, "result.json"), "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)

    manifest.update(
        result_sha256=res_hash,
        objective=round(Fo, 6),
        envelope=dict(W=round(eo[0], 3), D=round(eo[1], 3), H=round(eo[2], 3)),
        hard_violation_count=len(payload["hard_violations"]),
        hard_violations=payload["hard_violations"],
        outputs=[os.path.basename(p) for p in paths] +
                ["result.json", "run_manifest.json"],
        wall_time_s=round(time.time() - t_start, 2),
    )
    if pareto_rows is not None:
        manifest["pareto_size"] = len(pareto_rows)
        manifest["outputs"].append("pareto_front.json")
    mpath = os.path.join(a.out, "run_manifest.json")
    with open(mpath, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)

    print(f"\n── 산출물 ────────────────────────────────────────────────")
    for p in paths:
        print(f"  {p}")
    print(f"  {os.path.join(a.out, 'result.json')}")
    print(f"  {mpath}")
    print(f"\n  재현성: IR {irhash.short(ir_hash)}… + seed {a.seed} "
          f"→ 결과 {irhash.short(res_hash)}…")
    print(f"  wall_time = {manifest['wall_time_s']} s "
          f"(※ 시간은 결과 해시에 들어가지 않는다)")
    if manifest["hard_violation_count"]:
        print(f"\n  ★ hard 제약 위반 {manifest['hard_violation_count']}건 — "
              f"도면에 빨간색으로 표시됨. 이 해는 채택할 수 없다.")
        return 2
    print("\n  hard 제약 위반 0건.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
