# -*- coding: utf-8 -*-
"""
nsga.py — 다목적 최적화 (NSGA-II)

왜 다목적인가
-------------
가중합 단일 목적 SA 는 "가중치를 미리 정해야" 한다. 그런데 가중치가 곧 설계 판단이고,
그 근거를 대기 어렵다는 것이 실무에서 가장 논쟁이 되는 지점이다.
→ 3개 목적의 Pareto 프론트를 만들고 **설계자가 트레이드오프를 보고 고르게 한다.**
   책임소재가 명확해진다. (Autodesk Project Discover 2017 워크플로의 원형)

목적 3개 (HPR1000 보조건물 NSGA-III 구성과 동일)
  f1 건물 체적          f2 연결비용(배관·케이블)      f3 방사선구역 분산
  나머지(w4~w9, 제약 라이브러리)는 제약/페널티로 남긴다.

제약 처리: Deb 의 constrained-domination.
  - 실현가능(위반 0)이 실현불가를 항상 지배한다
  - 둘 다 실현불가면 위반량이 작은 쪽이 지배한다
  - 둘 다 실현가능하면 통상 Pareto 지배
  → 페널티 가중치를 조정하지 않아도 hard 제약이 목적을 밀어내지 못한다.

표현·목적함수·제약은 ga_core 를 그대로 재사용한다. seed 고정으로 재현 가능.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Sequence, Tuple

from ga_core import Solution, Spec, _mutate

OBJ_NAMES = ("volume_m3", "link_cost", "zone_dispersion")


# ---------------------------------------------------------------------------
# 개체
# ---------------------------------------------------------------------------

class Individual:
    __slots__ = ("sol", "obj", "cv", "rank", "crowd")

    def __init__(self, sol: Solution):
        self.sol = sol
        self.obj, self.cv = sol.objectives()
        self.rank = 0
        self.crowd = 0.0

    @property
    def feasible(self) -> bool:
        return self.cv <= 1e-6


def dominates(a: Individual, b: Individual) -> bool:
    """Deb 의 constrained-domination."""
    if a.feasible != b.feasible:
        return a.feasible
    if not a.feasible:
        return a.cv < b.cv - 1e-12
    le = all(x <= y + 1e-12 for x, y in zip(a.obj, b.obj))
    lt = any(x < y - 1e-12 for x, y in zip(a.obj, b.obj))
    return le and lt


def fast_nondominated_sort(pop: List[Individual]) -> List[List[int]]:
    n = len(pop)
    S = [[] for _ in range(n)]
    cnt = [0] * n
    fronts: List[List[int]] = [[]]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if dominates(pop[i], pop[j]):
                S[i].append(j)
            elif dominates(pop[j], pop[i]):
                cnt[i] += 1
        if cnt[i] == 0:
            pop[i].rank = 0
            fronts[0].append(i)
    k = 0
    while fronts[k]:
        nxt: List[int] = []
        for i in fronts[k]:
            for j in S[i]:
                cnt[j] -= 1
                if cnt[j] == 0:
                    pop[j].rank = k + 1
                    nxt.append(j)
        k += 1
        fronts.append(nxt)
    fronts.pop()
    return fronts


def crowding_distance(pop: List[Individual], front: Sequence[int]):
    for i in front:
        pop[i].crowd = 0.0
    m = len(pop[front[0]].obj) if front else 0
    for k in range(m):
        idx = sorted(front, key=lambda i: pop[i].obj[k])
        pop[idx[0]].crowd = pop[idx[-1]].crowd = float("inf")
        lo, hi = pop[idx[0]].obj[k], pop[idx[-1]].obj[k]
        rng = hi - lo
        if rng <= 1e-12:
            continue
        for t in range(1, len(idx) - 1):
            pop[idx[t]].crowd += (pop[idx[t + 1]].obj[k] -
                                  pop[idx[t - 1]].obj[k]) / rng


# ---------------------------------------------------------------------------
# 교차 — Sequence-Pair 순서 보존
# ---------------------------------------------------------------------------

def crossover(pa: Solution, pb: Solution, rng: random.Random) -> Solution:
    """
    층 배정·회전·터빈 방위각은 균등 교차, 순열은 '부모 A 의 상대순서 우선 +
    나머지는 부모 B 순서' 로 잇는다. 층 배정이 바뀌면 층별 구성원이 달라지므로
    구성원 집합을 먼저 확정한 뒤 순서를 채운다.
    """
    sp = pa.spec
    c = pa.clone()
    for i in range(c.n):
        if rng.random() < 0.5:
            c.rot[i] = pb.rot[i]
        if sp.blocks[i].floor is None and rng.random() < 0.5:
            c.floor[i] = pb.floor[i]
    for i in list(c.turbine_azimuth):
        if rng.random() < 0.5 and i in pb.turbine_azimuth:
            c.turbine_azimuth[i] = pb.turbine_azimuth[i]

    members: List[List[int]] = [[] for _ in range(sp.n_floors)]
    for i in range(c.n):
        for f in c.sp_floors(i):
            members[f].append(i)

    def merge(order_a: List[int], order_b: List[int], want: List[int]) -> List[int]:
        ws = set(want)
        out = [i for i in order_a if i in ws]
        seen = set(out)
        out += [i for i in order_b if i in ws and i not in seen]
        seen |= set(out)
        out += [i for i in want if i not in seen]
        return out

    c.gp = [merge(pa.gp[f], pb.gp[f], members[f]) for f in range(sp.n_floors)]
    c.gm = [merge(pa.gm[f], pb.gm[f], members[f]) for f in range(sp.n_floors)]
    return c


def tournament(pop: List[Individual], rng: random.Random) -> Individual:
    a, b = rng.randrange(len(pop)), rng.randrange(len(pop))
    ia, ib = pop[a], pop[b]
    if dominates(ia, ib):
        return ia
    if dominates(ib, ia):
        return ib
    if ia.rank != ib.rank:
        return ia if ia.rank < ib.rank else ib
    return ia if ia.crowd > ib.crowd else ib


# ---------------------------------------------------------------------------
# 본체
# ---------------------------------------------------------------------------

def nsga2(spec: Spec, pop_size: int = 60, generations: int = 60,
          seed: int = 20260826, p_cross: float = 0.9, n_mut: int = 3,
          verbose: bool = True, progress=None
          ) -> Tuple[List[Individual], List[Individual]]:
    """
    NSGA-II 실행.

    반환: (최종 Pareto 프론트, 최종 전체 집단)
    """
    rng = random.Random(seed)
    pop = [Individual(Solution(spec, rng)) for _ in range(pop_size)]
    fronts = fast_nondominated_sort(pop)
    for fr in fronts:
        crowding_distance(pop, fr)

    for gen in range(generations):
        off: List[Individual] = []
        while len(off) < pop_size:
            pa = tournament(pop, rng).sol
            pb = tournament(pop, rng).sol
            ch = crossover(pa, pb, rng) if rng.random() < p_cross else pa.clone()
            for _ in range(1 + rng.randrange(n_mut)):
                _mutate(ch, rng)
            off.append(Individual(ch))
        merged = pop + off
        fronts = fast_nondominated_sort(merged)
        newpop: List[Individual] = []
        for fr in fronts:
            crowding_distance(merged, fr)
            if len(newpop) + len(fr) <= pop_size:
                newpop += [merged[i] for i in fr]
            else:
                rest = sorted(fr, key=lambda i: -merged[i].crowd)
                newpop += [merged[i] for i in rest[:pop_size - len(newpop)]]
                break
        pop = newpop
        fronts = fast_nondominated_sort(pop)
        for fr in fronts:
            crowding_distance(pop, fr)
        if verbose and (gen % 10 == 0 or gen == generations - 1):
            f0 = [pop[i] for i in fronts[0]]
            nf = sum(1 for p in pop if p.feasible)
            print(f"  gen {gen+1:3d}/{generations}: front={len(f0)} "
                  f"feasible={nf}/{len(pop)} "
                  f"best_vol={min(p.obj[0] for p in f0):,.0f} m³")
        if progress:
            progress(gen, pop)

    front = [pop[i] for i in fast_nondominated_sort(pop)[0]]
    front.sort(key=lambda p: (not p.feasible, p.obj[0]))
    return front, pop


def front_table(front: List[Individual]) -> List[dict]:
    """Pareto 프론트를 사람이 고를 수 있는 표로."""
    rows = []
    for k, ind in enumerate(front):
        F, terms, geo, env, viols, _ = ind.sol.evaluate(detail=True)
        W, D, H = env
        rows.append(dict(
            index=k,
            feasible=ind.feasible,
            constraint_violation=round(ind.cv, 6),
            volume_m3=round(ind.obj[0], 1),
            link_cost=round(ind.obj[1], 1),
            zone_dispersion=round(ind.obj[2], 3),
            envelope=dict(W=round(W, 2), D=round(D, 2), H=round(H, 2),
                          footprint_m2=round(W * D, 1)),
            weighted_F=round(F, 1),
            terms={k2: round(v, 3) for k2, v in sorted(terms.items()) if abs(v) > 1e-9},
            hard_violations=[str(v) for v in viols if v.hard and v.amount > 1e-6],
        ))
    return rows


def knee_point(front: List[Individual]) -> Optional[int]:
    """
    참고용 knee 해 — 정규화 목적벡터의 L2 노름이 가장 작은 해.
    ※ '추천안' 이 아니다. 선택은 설계자가 한다. 표 정렬 편의를 위한 것.
    """
    feas = [i for i, p in enumerate(front) if p.feasible]
    pool = feas or list(range(len(front)))
    if not pool:
        return None
    m = len(front[0].obj)
    lo = [min(front[i].obj[k] for i in pool) for k in range(m)]
    hi = [max(front[i].obj[k] for i in pool) for k in range(m)]
    best, bi = float("inf"), None
    for i in pool:
        s = 0.0
        for k in range(m):
            r = hi[k] - lo[k]
            s += ((front[i].obj[k] - lo[k]) / r) ** 2 if r > 1e-12 else 0.0
        if s < best:
            best, bi = s, i
    return bi
