# -*- coding: utf-8 -*-
"""
candidates.py — 하나의 '최적해' 대신, 성격이 다른 실현가능 후보안 여러 개를 만든다. 결정론적.

목적함수는 면적·연결 길이·hard 제약을 하나의 숫자로 섞는다. 그 섞는 비율은 사람이 정할 일이라
엔진이 하나만 골라 내밀면 사람은 결과를 믿거나 버리는 것밖에 할 수 없다. 그래서 같은 hard 제약을
모두 지키는 해 중에서 서로 다른 장점을 가진 것을 몇 개 골라 나란히 보여 주고, 사람이 고른다.

  · 가장 작게      — 건축 면적이 가장 작은 해
  · 동선 짧게      — 실·설비 사이 연결 길이(배관·동선 물량)가 가장 짧은 해
  · 덜 바꾸게      — 현황 배치에서 실이 가장 적게 움직인 해 (현황이 있을 때)
  · 균형          — 면적과 연결 길이를 각각 최솟값 대비 비율로 더해 가장 작은 해

탐색 프로필 (모두 같은 hard 제약, 같은 seed 계열 — 재현 가능):
  base      원래 목적함수, 재시작마다의 최선해를 모두 모은다
  link      연결 길이 가중치 ×4 — 면적을 조금 내주더라도 가까이 붙이는 해
  near      현황에서 출발해 낮은 온도로 — 현황을 다듬는 해
모든 해는 hard 우선 수복(ga_core.repair_hard)을 거친 뒤, 원래 목적함수로 다시 평가한다.
"""
from __future__ import annotations

import copy
import math
from typing import Dict, List, Optional

import ga_core as G

LABELS = {
    "smallest": ("가장 작게", "건축 면적이 가장 작은 안"),
    "shortest": ("동선 짧게", "실·설비 사이 연결 길이가 가장 짧은 안 — 면적은 조금 더 쓸 수 있다"),
    "closest": ("덜 바꾸게", "현황에서 실이 가장 적게 움직인 안 — 공사·운영 변화가 작다"),
    "balanced": ("균형", "면적과 연결 길이를 함께 본 안"),
}


def _variant(spec: G.Spec, **mult) -> G.Spec:
    sp = copy.copy(spec)
    w = dict(spec.weights)
    for k, m in mult.items():
        w[k] = spec.wt(k) * m
    sp.weights = w
    sp._moves = None
    return sp


def _displacement(geo: Dict[int, dict], ageo: Dict[int, dict]) -> Optional[float]:
    """현황 대비 실 중심 이동 평균 (m). 층이 바뀐 실은 층고 대신 10 m 를 더한다."""
    ds = []
    for i, a in ageo.items():
        g = geo.get(i)
        if g is None:
            continue
        d = math.hypot(g["x"] + g["w"] / 2 - a["x"] - a["w"] / 2, g["y"] + g["d"] / 2 - a["y"] - a["d"] / 2)
        if g["f"] != a["f"]:
            d += 10.0
        ds.append(d)
    return round(sum(ds) / len(ds), 2) if ds else None


def generate(spec: G.Spec, *, iters: int, restarts: int, seed: int,
             init: "G.Solution" = None, asis_sol=None, k: int = 4, tick=None) -> List[dict]:
    """
    반환: 후보 목록 [{key, label, why, sol, F, footprint, link, hard, moved_m, profile}]
    첫 번째가 원래 목적함수 F 최소(= 예전의 단일 최적해와 같은 기준) 이다.
    """
    pool = []          # (profile, sol)

    keep: list = []
    G.optimize(spec, iters=iters, restarts=restarts, seed=seed, verbose=False, init=init, keep=keep, tick=tick)
    pool += [("base", s) for s, _ in keep]

    sp_link = _variant(spec, link=4.0)
    init_l = None
    if init is not None:
        init_l = init.clone()
        init_l.spec = sp_link              # 출발해도 바뀐 가중치로 잰다
    keep = []
    G.optimize(sp_link, iters=iters, restarts=max(2, restarts // 2), seed=seed + 101, verbose=False,
               init=init_l, keep=keep)
    pool += [("link", s) for s, _ in keep]

    if init is not None:
        keep = []
        G.optimize(spec, iters=iters, restarts=1, seed=seed + 202, verbose=False, init=init.clone(),
                   keep=keep, T0=max(1.0, abs(init.evaluate()) * 0.005))
        pool += [("near", s) for s, _ in keep]

    ageo = None
    if asis_sol is not None:
        ageo = asis_sol.evaluate(detail=True)[2]

    rows = []
    for prof, s in pool:
        s.spec = spec                      # 원래 목적함수로 다시 잰다
        s, F = G.repair_penetrations(s, s.evaluate(), verbose=False)
        s, F = G.repair_hard(s)
        F, t, geo, env, viols, _sh = s.evaluate(detail=True)
        hard = [v for v in viols if v.hard and v.amount > 1e-6]
        rows.append(dict(profile=prof, sol=s, F=F, footprint=round(env[0] * env[1], 3),
                         link=round(t["link"] / max(spec.wt("link"), 1e-9), 3),
                         hard=len(hard), hard_sum=sum(v.amount for v in hard),
                         moved_m=_displacement(geo, ageo) if ageo else None))

    feas = [r for r in rows if r["hard"] == 0]
    if not feas:                               # 실현가능 해가 없으면 위반이 가장 적은 해 하나만 (경고와 함께)
        r = min(rows, key=lambda r: (r["hard_sum"], r["F"]))
        r.update(key="best", label="위반 최소", why="필수 요건 위반이 남은 안. 채택할 수 없다")
        return [r]

    def pick(key, fn):
        r = min(feas, key=fn)
        return dict(r, key=key, label=LABELS[key][0], why=LABELS[key][1])

    a0 = min(r["footprint"] for r in feas)
    l0 = max(min(r["link"] for r in feas), 1e-9)
    out = [min(feas, key=lambda r: r["F"])]
    out[0] = dict(out[0], key="objective", label="목적함수 최소", why="원래 목적함수(면적·연결·제약을 섞은 값)가 가장 작은 안")
    out.append(pick("smallest", lambda r: (r["footprint"], r["F"])))
    out.append(pick("shortest", lambda r: (r["link"], r["F"])))
    if ageo:
        out.append(pick("closest", lambda r: (r["moved_m"], r["F"])))
    out.append(pick("balanced", lambda r: (r["footprint"] / a0 + r["link"] / l0, r["F"])))

    # 같은 해(면적·연결 길이가 사실상 같음)는 한 번만 — 먼저 나온 이름에 다른 이름을 덧붙인다
    uniq: List[dict] = []
    for r in out:
        same = next((u for u in uniq if abs(u["footprint"] - r["footprint"]) < 0.01 * max(1.0, u["footprint"]) / 100
                     and abs(u["link"] - r["link"]) < 1e-3 * max(1.0, u["link"])), None)
        if same is not None:
            if r["label"] not in same["label"]:
                same["label"] += " · " + r["label"]
            continue
        uniq.append(r)
    return uniq[:k + 1]
