# -*- coding: utf-8 -*-
"""
decompose.py — 계층 분해 (규모 문제에 대한 대응)

왜 필요한가
-----------
정확해 MILP 는 25기기 수준에서 이미 무너진다 (Ejeh 2019: COGP 25기기 갭 11.8%,
10,000초 제한). 같은 논문 원문: "이전의 동시 최적화 접근들은 11기 미만의 배치
문제만 합리적 시간 내에 풀 수 있었다." 시설배치 문헌도 엄밀 MILP 는 부서 13개가
한계다. SMR 보조건물은 수백 개 실·기기 규모이므로 분해는 선택이 아니다.

3단계
  1) 구역 배정   — 목적 = 구역 횡단 연결 비용 최소화
                   (138기기 화학플랜트 사례: 프레임 횡단 연결 247 → 17개, 횡단배관 −582.7 m)
  2) 층·표고 배정 — 층수-비용 트레이드오프 곡선 산출
                   (Ejeh 2019 요소플랜트 1층 260,942 → 4층 117,431, −55.0%)
  3) 층내 상세배치 — Sequence-Pair + 메타휴리스틱 (ga_core / nsga)

이 파일은 1)과 2)를 구현한다. 3)은 ga_core.optimize / nsga.nsga2 이다.
난수는 전부 seed 로 고정된다.
"""
from __future__ import annotations

import copy
import json
import math
import random
from typing import Dict, List, Optional, Sequence, Tuple

import ga_core as G


# ---------------------------------------------------------------------------
# 1단계 — 구역 배정
# ---------------------------------------------------------------------------

def _link_matrix(spec: G.Spec) -> Dict[Tuple[int, int], float]:
    idx = {b.id: i for i, b in enumerate(spec.blocks)}
    M: Dict[Tuple[int, int], float] = {}
    for lk in spec.links:
        i, j = idx.get(lk.a), idx.get(lk.b)
        if i is None or j is None or i == j:
            continue
        k = (min(i, j), max(i, j))
        M[k] = M.get(k, 0.0) + lk.cost
    return M


def _cross_cost(M: Dict[Tuple[int, int], float], asg: List[int]) -> Tuple[float, int]:
    c, n = 0.0, 0
    for (i, j), w in M.items():
        if asg[i] != asg[j]:
            c += w
            n += 1
    return c, n


def _footprint(b: G.Block) -> float:
    return (b.w + 2 * b.clearance) * (b.d + 2 * b.clearance)


def partition_areas(spec: G.Spec, n_areas: int, seed: int = 20260826,
                    capacity_slack: float = 1.25,
                    max_passes: int = 40) -> dict:
    """
    블록을 n_areas 개 구역으로 나눈다. 목적 = 구역 횡단 연결비용 최소화,
    제약 = 구역별 점유면적 상한 (전체/구역수 × capacity_slack).

    결정론적: 초기해는 연결도 내림차순 시드 + 탐욕 배정, 개선은 사전순 스캔의
    이동/교환 국소탐색. seed 는 동점 처리에만 쓰인다.

    반환: {assignment, areas, cross_cost_initial, cross_cost_final,
           cross_links_initial, cross_links_final, area_footprint}
    """
    n = len(spec.blocks)
    if n_areas < 1:
        raise ValueError("n_areas >= 1")
    rng = random.Random(seed)
    M = _link_matrix(spec)
    fp = [_footprint(b) for b in spec.blocks]
    cap = sum(fp) / n_areas * capacity_slack

    deg: Dict[int, float] = {i: 0.0 for i in range(n)}
    for (i, j), w in M.items():
        deg[i] += w
        deg[j] += w

    # --- 초기해: 연결도 높은 블록을 시드로, 나머지는 '가장 강하게 붙는' 구역에 ---
    order = sorted(range(n), key=lambda i: (-deg[i], spec.blocks[i].id))
    asg = [-1] * n
    load = [0.0] * n_areas
    for a in range(min(n_areas, n)):
        asg[order[a]] = a
        load[a] += fp[order[a]]
    for i in order[n_areas:]:
        best, ba = -1.0, None
        for a in range(n_areas):
            if load[a] + fp[i] > cap and ba is not None:
                continue
            pull = sum(w for (p, q), w in M.items()
                       if (p == i and asg[q] == a) or (q == i and asg[p] == a))
            score = pull - 1e-6 * load[a]
            if score > best:
                best, ba = score, a
        ba = ba if ba is not None else min(range(n_areas), key=lambda a: load[a])
        asg[i] = ba
        load[ba] += fp[i]

    c0, k0 = _cross_cost(M, asg)

    # --- 개선: 이동 + 교환 국소탐색 ---
    def delta_move(i: int, to: int) -> float:
        frm = asg[i]
        if frm == to:
            return 0.0
        d = 0.0
        for (p, q), w in M.items():
            if p == i:
                o = q
            elif q == i:
                o = p
            else:
                continue
            was = 1.0 if asg[o] != frm else 0.0
            now = 1.0 if asg[o] != to else 0.0
            d += w * (now - was)
        return d

    for _ in range(max_passes):
        improved = False
        for i in sorted(range(n), key=lambda i: spec.blocks[i].id):
            frm = asg[i]
            for to in range(n_areas):
                if to == frm or load[to] + fp[i] > cap:
                    continue
                if delta_move(i, to) < -1e-9:
                    load[frm] -= fp[i]; load[to] += fp[i]
                    asg[i] = to
                    improved = True
                    break
        for i in sorted(range(n), key=lambda i: spec.blocks[i].id):
            for j in range(i + 1, n):
                if asg[i] == asg[j]:
                    continue
                a, b = asg[i], asg[j]
                before, _ = _cross_cost(M, asg)
                asg[i], asg[j] = b, a
                after, _ = _cross_cost(M, asg)
                if after < before - 1e-9:
                    load[a] += fp[j] - fp[i]
                    load[b] += fp[i] - fp[j]
                    improved = True
                else:
                    asg[i], asg[j] = a, b
        if not improved:
            break

    c1, k1 = _cross_cost(M, asg)
    labels = [f"A{a+1}" for a in range(n_areas)]
    return dict(
        assignment={spec.blocks[i].id: labels[asg[i]] for i in range(n)},
        areas=labels,
        cross_cost_initial=round(c0, 3), cross_cost_final=round(c1, 3),
        cross_links_initial=k0, cross_links_final=k1,
        area_footprint={labels[a]: round(load[a], 1) for a in range(n_areas)},
        capacity_m2=round(cap, 1),
    )


def apply_areas(spec: G.Spec, assignment: Dict[str, str],
                as_field: str = "structure") -> G.Spec:
    """
    1단계 결과를 Spec 에 반영한다.

    기본은 block.structure 에 넣는다 — SEISMIC-GAP 의 구조물 단위와 같은 축이며,
    hard 제약으로 승격시킬지는 사람이 결정한다. 자동으로 제약을 만들지 않는다.
    """
    sp = copy.deepcopy(spec)
    for b in sp.blocks:
        if b.id in assignment:
            setattr(b, as_field, assignment[b.id])
    return sp


# ---------------------------------------------------------------------------
# 2단계 — 층수 민감도 스윕 (층수-비용 트레이드오프 곡선)
# ---------------------------------------------------------------------------

def floor_sweep(spec: G.Spec, floors: Sequence[int], iters: int = 20000,
                restarts: int = 2, seed: int = 20260826,
                floor_height: Optional[float] = None,
                verbose: bool = True) -> List[dict]:
    """
    층수를 바꿔가며 최적값을 표로 낸다.

    근거: Ejeh 2019 민감도 — 요소플랜트 1층 260,942 → 4층 117,431 (−55.0%),
    CDU 1층 1,112,095 → 7층 592,322 (−46.7%). 층수를 고정하면 이 개선 여지가
    통째로 사라진다.

    주의: floor(고정층) 이 지정된 블록은 새 층수 범위 밖으로 나갈 수 있다.
    그런 블록은 최상층으로 clamp 하고 그 사실을 행에 기록한다 — 조용히 바꾸지 않는다.
    """
    rows: List[dict] = []
    base_fh = floor_height or (
        (sum(spec.floor_heights) / len(spec.floor_heights))
        if spec.floor_heights else G._default_fh(spec))
    for nf in floors:
        sp = copy.deepcopy(spec)
        sp.n_floors = nf
        sp.floor_heights = [base_fh] * nf
        clamped = []
        for b in sp.blocks:
            if b.floor is not None and b.floor >= nf:
                clamped.append(f"{b.id}:{b.floor}→{nf-1}")
                b.floor = nf - 1
        best, F, _ = G.optimize(sp, iters=iters, restarts=restarts, seed=seed,
                                verbose=False)
        Fv, terms, geo, env, viols, _ = best.evaluate(detail=True)
        W, D, H = env
        hv = G.hard_violations(sp, terms, viols)
        rows.append(dict(
            n_floors=nf,
            F=round(Fv, 1),
            footprint_m2=round(W * D, 1),
            volume_m3=round(W * D * H, 1),
            envelope=dict(W=round(W, 2), D=round(D, 2), H=round(H, 2)),
            link_term=round(terms.get("link", 0.0), 1),
            hard_violation_count=len(hv),
            hard_violations=hv,
            clamped_fixed_floors=clamped,
        ))
        if verbose:
            r = rows[-1]
            print(f"  {nf}층: F={r['F']:>14,.0f}  면적={r['footprint_m2']:>9,.0f} m²  "
                  f"체적={r['volume_m3']:>11,.0f} m³  hard위반 {r['hard_violation_count']}건")
    if rows:
        f1 = rows[0]["F"]
        for r in rows:
            r["delta_vs_first_pct"] = round((r["F"] - f1) / f1 * 100, 2) if f1 else 0.0
    return rows


# ---------------------------------------------------------------------------
# 전체 파이프라인
# ---------------------------------------------------------------------------

def hierarchical(spec: G.Spec, n_areas: int = 3,
                 floors: Sequence[int] = (1, 2, 3, 4),
                 iters: int = 20000, restarts: int = 2,
                 seed: int = 20260826, verbose: bool = True) -> dict:
    """1) 구역 배정 → 2) 층수 스윕 → 3) 최적 층수에서 상세배치."""
    if verbose:
        print("── 1단계: 구역 배정 (횡단 연결비용 최소화) ─────────────")
    part = partition_areas(spec, n_areas, seed=seed)
    if verbose:
        print(f"  횡단 연결 {part['cross_links_initial']} → {part['cross_links_final']}개, "
              f"횡단 비용 {part['cross_cost_initial']:,.1f} → {part['cross_cost_final']:,.1f}")
    sp = apply_areas(spec, part["assignment"])

    if verbose:
        print("── 2단계: 층수 민감도 스윕 ──────────────────────────────")
    sweep = floor_sweep(sp, floors, iters=iters, restarts=restarts, seed=seed,
                        verbose=verbose)
    feas = [r for r in sweep if r["hard_violation_count"] == 0] or sweep
    best_row = min(feas, key=lambda r: r["F"])

    if verbose:
        print(f"── 3단계: {best_row['n_floors']}층에서 상세배치 ─────────────")
    sp3 = copy.deepcopy(sp)
    sp3.n_floors = best_row["n_floors"]
    fh = (sum(spec.floor_heights) / len(spec.floor_heights)) if spec.floor_heights \
        else G._default_fh(spec)
    sp3.floor_heights = [fh] * sp3.n_floors
    for b in sp3.blocks:
        if b.floor is not None and b.floor >= sp3.n_floors:
            b.floor = sp3.n_floors - 1
    best, F, _ = G.optimize(sp3, iters=iters * 2, restarts=restarts + 1,
                            seed=seed, verbose=verbose)
    return dict(partition=part, sweep=sweep, chosen_floors=best_row["n_floors"],
                solution=best, spec=sp3, F=F)
