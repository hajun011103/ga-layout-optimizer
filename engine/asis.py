# -*- coding: utf-8 -*-
"""
asis.py — 현황 배치(as-is) 평가 (결정론적 영역. LLM 미개입)

도면에서 읽은 기존 배치를 **최적화 결과와 같은 목적함수**로 채점한다.
"나이브한 줄 세우기 기준안" 이 아니라 **사람이 실제로 그린 배치** 가 비교 기준이 된다.
이것이 개선율을 성과로 말할 수 있는 유일한 비교다.

asis.json
---------
{
  "_schema": "smr-ga-asis/1.0",
  "project": str,
  "boundary": {"0": [x0, y0, x1, y1], ...} | {},   # 층별 '현재 공간' 외곽 [m]
  "blocks": {id: {"x", "y", "w", "d", "floor"}},  # 본체(clearance 제외) 좌하단·치수 [m]
  "unplaced": [id, ...],                           # 좌표가 없는 행(목록표)
  "source": [{"filename", "sha256", "takeoff_sha256"}]
}
"""
from __future__ import annotations

import json
import math
import random
from typing import Dict, List, Optional, Tuple

import ga_core as G
import irhash

ASIS_SCHEMA = "smr-ga-asis/1.0"


class AsIsSolution(G.Solution):
    """좌표가 이미 정해진 해. decode() 가 순열이 아니라 asis.json 에서 좌표를 읽는다."""

    def __init__(self, spec: G.Spec, asis: dict):
        rng = random.Random(0)
        super().__init__(spec, rng)
        self.asis = asis
        bl = asis.get("blocks", {})
        self.rot = [0] * self.n
        for i, b in enumerate(spec.blocks):
            a = bl.get(b.id)
            if a is not None:
                self.floor[i] = int(a["floor"])
                # 도면 치수가 IR 치수의 회전형이면 회전으로 본다
                if (abs(a["w"] - b.d) < 1e-6 and abs(a["d"] - b.w) < 1e-6
                        and abs(b.w - b.d) > 1e-6):
                    self.rot[i] = 1
            elif b.floor is not None:
                self.floor[i] = b.floor
        for i in self.turbine_azimuth:
            self.turbine_azimuth[i] = float(self.azimuths[0])

    def clone(self):
        s = super().clone()
        s.__class__ = AsIsSolution
        s.asis = self.asis
        return s

    def decode(self):
        sp = self.spec
        bl = self.asis.get("blocks", {})
        geo: Dict[int, dict] = {}
        shadows: Dict[Tuple[int, int], dict] = {}
        xs1: List[float] = []
        ys1: List[float] = []
        for i, b in enumerate(sp.blocks):
            a = bl.get(b.id)
            if a is None:
                continue                      # 좌표 없는 블록은 현황 평가에서 빠진다
            cl = b.clearance
            w, d = b.footprint(self.rot[i])
            rec_base = dict(x=a["x"] - cl, y=a["y"] - cl, w=w, d=d)
            for f in self.floors_of(i):
                rec = dict(rec_base, f=f)
                if f == self.floor[i]:
                    geo[i] = rec
                else:
                    shadows[(i, f)] = rec
            if b.kind != "void":
                xs1.append(a["x"] + a["w"]); ys1.append(a["y"] + a["d"])

        fh = sp.floor_heights or [G._default_fh(sp)] * sp.n_floors
        base = [sum(fh[:f]) for f in range(sp.n_floors)]
        for g in list(geo.values()) + list(shadows.values()):
            g["z"] = base[g["f"]]
            g["cx"] = g["x"] + g["w"] / 2
            g["cy"] = g["y"] + g["d"] / 2
        bnd = self.asis.get("boundary") or {}
        if bnd:
            Wm = max(r[2] for r in bnd.values())
            Dm = max(r[3] for r in bnd.values())
        else:
            Wm = max(xs1, default=0.0)
            Dm = max(ys1, default=0.0)
        self.env = (Wm, Dm, sum(fh[:sp.n_floors]))
        self.fh = fh
        self.geo, self.shadows = geo, shadows
        return geo, shadows


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fp:
        return json.load(fp)


def overlaps(spec: G.Spec, sol: G.Solution) -> List[str]:
    """현황 배치에서 clearance 를 포함해 겹치는 쌍 — 도면이 이미 이격을 못 지키는 곳."""
    geo, _ = sol.decode()
    out = []
    ids = sorted(geo, key=lambda i: spec.blocks[i].id)
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            i, j = ids[a], ids[b]
            gi, gj = geo[i], geo[j]
            if gi["f"] != gj["f"]:
                continue
            if "void" in (spec.blocks[i].kind, spec.blocks[j].kind):
                continue
            ox = min(gi["x"] + gi["w"], gj["x"] + gj["w"]) - max(gi["x"], gj["x"])
            oy = min(gi["y"] + gi["d"], gj["y"] + gj["d"]) - max(gi["y"], gj["y"])
            if ox > 1e-6 and oy > 1e-6:
                out.append(f"{spec.blocks[i].id}↔{spec.blocks[j].id} "
                           f"({ox:.2f}×{oy:.2f} m, {gi['f']+1}층)")
    return out


# ---------------------------------------------------------------------------
# 비교 지표 — 현황 vs 최적화 (같은 목적함수, 같은 IR)
# ---------------------------------------------------------------------------

def metrics(spec: G.Spec, sol: G.Solution) -> dict:
    F, terms, geo, env, viols, shadows = sol.evaluate(detail=True)
    W, D, H = env
    body = 0.0
    for i, g in geo.items():
        b = spec.blocks[i]
        if b.kind == "void":
            continue
        body += b.w * b.d
    floors_used = max((g["f"] for g in geo.values()), default=0) + 1
    link_raw = terms.get("link", 0.0) / max(spec.wt("link"), 1e-12)
    hard = [v for v in viols if v.hard and v.amount > 1e-6]
    return dict(
        width_m=round(W, 3), depth_m=round(D, 3), height_m=round(H, 3),
        footprint_m2=round(W * D, 3), volume_m3=round(W * D * H, 3),
        body_area_m2=round(body, 3),
        utilization=round(body / (W * D * spec.n_floors), 4) if W * D else 0.0,
        link_cost=round(link_raw, 3),
        objective=round(F, 3),
        floors=spec.n_floors, floors_used=floors_used,
        hard_violation_count=len(hard),
        hard_violations=[dict(id=v.cid, detail=v.detail, amount=round(v.amount, 4),
                              unit=v.unit) for v in hard],
    )


def compare(asis_m: dict, opt_m: dict) -> dict:
    """개선율 = (현황 − 최적) / 현황. 양수가 개선."""
    def pct(k):
        a, b = asis_m.get(k), opt_m.get(k)
        if not a:
            return None
        return round((a - b) / a * 100.0, 2)
    return dict(
        footprint_reduction_pct=pct("footprint_m2"),
        volume_reduction_pct=pct("volume_m3"),
        link_cost_reduction_pct=pct("link_cost"),
        asis_hard_violations=asis_m["hard_violation_count"],
        optimized_hard_violations=opt_m["hard_violation_count"],
        claimable=(opt_m["hard_violation_count"] == 0),
        note=("현황 배치와 최적화 배치를 같은 IR·같은 목적함수로 평가했다. "
              "최적화 배치에 hard 위반이 있으면 개선율을 주장할 수 없다."),
    )


def asis_hash(asis: dict) -> str:
    return irhash.sha256_obj(asis)


def _topo(nodes: List[int], before, key) -> List[int]:
    """before(a,b)=True 면 a 가 b 앞. Kahn 위상정렬, 동률·순환은 key 로 결정론적 해소."""
    succ = {a: [] for a in nodes}
    indeg = {a: 0 for a in nodes}
    for a in nodes:
        for b in nodes:
            if a != b and before(a, b):
                succ[a].append(b)
                indeg[b] += 1
    out, ready = [], sorted([a for a in nodes if indeg[a] == 0], key=key)
    left = set(nodes)
    while left:
        if not ready:                     # 순환 — key 가 가장 작은 것부터 강제로 뺀다
            ready = [min(left, key=key)]
        a = ready.pop(0)
        if a not in left:
            continue
        left.discard(a)
        out.append(a)
        for b in succ[a]:
            indeg[b] -= 1
            if indeg[b] == 0 and b in left:
                ready.append(b)
        ready.sort(key=key)
    return out


def warm_start(spec: G.Spec, asis: dict, seed: int = 0) -> G.Solution:
    """
    현황 좌표 → Sequence-Pair 해 (솔버의 출발점).

    관계 규칙(Murata 1996):
      i 가 j 의 왼쪽 ⇔ Γ⁺·Γ⁻ 모두에서 i 가 앞
      i 가 j 의 아래 ⇔ Γ⁺ 에서 뒤, Γ⁻ 에서 앞
    모든 쌍의 '실제' 기하 관계(겹치지 않는 축)로 선후 그래프를 만들고 위상정렬한다.
    대각 관계는 간극이 큰 축을 택한다. 디코딩은 좌하단 압축이므로 현황 배치의
    상대 관계를 유지한 채 빈틈만 줄어든 해가 출발점이 된다.
    """
    a_sol = AsIsSolution(spec, asis)
    s = G.Solution(spec, random.Random(seed))
    s.rot = list(a_sol.rot)
    for i, b in enumerate(spec.blocks):
        if b.id in asis.get("blocks", {}) and b.floor is None:
            s.floor[i] = a_sol.floor[i]
    s._rebuild(random.Random(seed))
    geo, sh = a_sol.decode()

    def rect(i, f):
        g = geo.get(i) if (i in geo and geo[i]["f"] == f) else sh.get((i, f))
        return None if g is None else (g["x"], g["y"], g["x"] + g["w"], g["y"] + g["d"])

    for f in range(spec.n_floors):
        ids = s.gp[f]
        known = [i for i in ids if rect(i, f) is not None]
        rest = [i for i in ids if rect(i, f) is None]
        R = {i: rect(i, f) for i in known}

        # 쌍마다 관계를 '하나만' 정한다 (대각 배치에서 양방향으로 다른 관계를 고르면 순환이 생긴다).
        # 후보 4개(i←j 왼쪽, j←i 왼쪽, i 아래, j 아래) 중 간극이 가장 큰 것.
        REL: Dict[Tuple[int, int], str] = {}
        for x in range(len(known)):
            for y in range(x + 1, len(known)):
                i, j = known[x], known[y]
                a, b = R[i], R[j]
                cands = [(b[0] - a[2], "L", i, j), (a[0] - b[2], "L", j, i),
                         (b[1] - a[3], "B", i, j), (a[1] - b[3], "B", j, i)]
                g, kind, p_, q_ = max(cands, key=lambda c: (c[0], c[1] == "L"))
                REL[(i, j)] = REL[(j, i)] = (kind, p_, q_)   # p_ 가 q_ 의 왼쪽/아래
        key = lambda i: ((R[i][0] + R[i][2]) / 2 + (R[i][1] + R[i][3]) / 2, spec.blocks[i].id)

        def before_p(a, b):          # Γ⁺: a 가 b 의 왼쪽, 또는 b 가 a 의 아래
            kind, p_, q_ = REL[(a, b)]
            return (kind == "L" and p_ == a) or (kind == "B" and p_ == b)

        def before_m(a, b):          # Γ⁻: a 가 b 의 왼쪽 또는 아래
            kind, p_, q_ = REL[(a, b)]
            return p_ == a
        s.gp[f] = _topo(known, before_p, key) + rest
        s.gm[f] = _topo(known, before_m, key) + rest
    return _fit_to_asis(s, a_sol, seed)


def _fit_to_asis(s: G.Solution, a_sol: "AsIsSolution", seed: int, iters: int = 6000) -> G.Solution:
    """
    쌍별 관계가 전역적으로 일관되지 않으면 위상정렬이 순환을 끊으며 관계를 뒤집는다.
    그래서 순열만 바꾸는 결정론적 언덕오르기로 '디코딩 좌표 ↔ 현황 좌표' 편차를 줄인다.
    편차 = Σ|Δx|+|Δy| (좌하단 압축 때문에 현황보다 왼쪽·아래로 가는 것은 벌하지 않는다).
    """
    ageo, _ = a_sol.decode()
    rng = random.Random(seed ^ 0x5A5A)

    def dev(sol):
        geo, _ = sol.decode()
        t = 0.0
        for i, g in geo.items():
            a = ageo.get(i)
            if a is None:
                continue
            t += max(0.0, g["x"] - a["x"]) + max(0.0, g["y"] - a["y"])
        return t
    best, bd = s, dev(s)
    for _ in range(iters):
        if bd <= 1e-6:
            break
        c = best.clone()
        f = rng.randrange(c.spec.n_floors)
        g = c.gp[f]
        if len(g) < 2:
            continue
        a, b = rng.sample(range(len(g)), 2)
        m = rng.randrange(3)
        if m == 0:
            c.gp[f][a], c.gp[f][b] = c.gp[f][b], c.gp[f][a]
        elif m == 1:
            c.gm[f][a], c.gm[f][b] = c.gm[f][b], c.gm[f][a]
        else:
            v = c.gm[f].pop(a); c.gm[f].insert(b, v)
        d = dev(c)
        if d <= bd:
            best, bd = c, d
    return best
