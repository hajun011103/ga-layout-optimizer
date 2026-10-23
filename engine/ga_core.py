# -*- coding: utf-8 -*-
"""
ga_core.py — SMR General Arrangement(GA) 공간 최적화 엔진 (결정론적 영역)

이 파일에는 LLM 이 개입하지 않는다. 입력은 Gate 1 을 통과해 동결된 IR 뿐이고,
난수는 전부 seed 로 고정된다. 같은 IR 해시 + 같은 seed 는 같은 결과 해시를 낸다.

표현(representation)
-------------------
층(floor)별 Sequence-Pair (Γ⁺, Γ⁻)  [Murata et al., IEEE TCAD 1996]
 - 두 순열만으로 겹치지 않는 compact packing 이 항상 생성된다
   → 중첩 제약이 표현 단계에서 배제되고 탐색공간 전체가 실현가능해진다
 - 수평·수직 제약그래프의 최장경로로 좌표 결정 (O(n²))
 - 유지보수 이격은 블록 치수를 clearance 만큼 팽창시켜 중첩 금지에 흡수시킨다

추가 유전자
 - rot[i]              : 90° 회전 비트
 - floor[i]            : 층 배정 (기준층)
 - turbine_azimuth[i]  : 터빈 방위각 (이산) — TURBINE-25 금지 부채꼴의 방향
 - 다층 관통 기기는 상위 층 Sequence-Pair 에 '그림자'로 함께 들어간다
   → 상위 층 중첩이 표현 단계에서 배제되고, 남는 것은 관통부 정렬오차뿐이다

의존성: Python 표준 라이브러리만. (폐쇄망 설치 요건 — 유지할 것)
"""

from __future__ import annotations
import json
import math
import random
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import constraints as CONS
from constraints import EvalContext, Violation
from version import ENGINE_VERSION, IR_SCHEMA_VERSION

# App. R III.G.2 의 20 ft. 이것이 GA 의 실질 그리드 모듈이다.
DEFAULT_GRID_M = 6.096


# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------

@dataclass
class Block:
    """배치 대상 단위 — 기기(pump, HX...) 또는 구역(room/compartment)."""
    id: str
    name: str
    w: float                      # 폭 [m] (본체)
    d: float                      # 깊이 [m] (본체)
    h: float = 4.0                # 높이 [m]
    clearance: float = 0.0        # 사방 유지보수/접근 이격 [m] (본체에 더해짐)
    rotatable: bool = True        # 90도 회전 허용
    floor: Optional[int] = None   # 고정 기준층 (None 이면 최적화 대상)
    zone: Optional[str] = None    # 방사선/계통 구역 라벨
    weight: float = 0.0           # 중량 [ton]
    need_exterior: bool = False   # 외벽 접면 필요(반출입/급기/방출)
    need_crane: bool = False      # 폴라/브리지 크레인 커버리지 필요
    fixed_xy: Optional[Tuple[float, float]] = None

    # ---- 제약 라이브러리가 참조하는 속성 (constraints.py 선택자) ----
    fire_zone: Optional[str] = None      # 화재구역 라벨
    train: Optional[str] = None          # 다중성 계열 (A/B/C/D)
    structure: Optional[str] = None      # 내진 이격 단위(건물)
    vital_area: Optional[str] = None     # 보안 중요구역
    success_path: Optional[str] = None   # App. R 고온정지 성공경로 계열
    opening: bool = False                # 대형 미방호 개구부(해치·계단)
    turbine: bool = False                # 터빈 — 방위각이 결정변수가 된다

    # ---- 도면 입력(현황 배치)에서 오는 속성 ----
    kind: str = "equipment"              # equipment | room | obstacle | void | opening
    pinned: bool = False                 # True 면 fixed_xy(중심)·floor 에 '정확히' 고정된 장애물.
                                         # Sequence-Pair 에 들어가지 않고, 나머지 블록이 피해 간다.

    def footprint(self, rot: int) -> Tuple[float, float]:
        """clearance 를 포함한 실제 점유 치수. rot=1 이면 90도 회전."""
        w = self.w + 2 * self.clearance
        d = self.d + 2 * self.clearance
        return (d, w) if rot else (w, d)


@dataclass
class Link:
    """블록 간 연결 — 배관/케이블/덕트/동선."""
    a: str
    b: str
    cost: float = 1.0
    kind: str = "pipe"            # pipe | cable | duct | route
    vertical_factor: float = 3.0  # 층간 이동의 상대 비용 (λ = 3~5)


@dataclass
class SeparationRule:
    """두 블록(또는 두 zone)의 최소 이격 — 외곽 간 간극으로 측정."""
    a: str
    b: str
    min_dist: float
    by_zone: bool = False
    hard: bool = True
    basis: Optional[str] = None   # 근거 조항 (추적성 표에 그대로 실린다)


@dataclass
class ElevationRule:
    """중력 구동(피동안전) 표고 순서: above 가 below 보다 최소 dz 높아야 함."""
    above: str
    below: str
    min_dz: float = 0.0
    basis: Optional[str] = None


@dataclass
class Spec:
    """최적화 문제 전체 정의 — Gate 1 에서 동결되는 Design Intent IR."""
    project: str
    blocks: List[Block]
    links: List[Link] = field(default_factory=list)
    separations: List[SeparationRule] = field(default_factory=list)
    elevations: List[ElevationRule] = field(default_factory=list)
    constraints: List[dict] = field(default_factory=list)   # 제약 라이브러리 선택
    n_floors: int = 1
    floor_heights: List[float] = field(default_factory=list)
    max_w: Optional[float] = None
    max_d: Optional[float] = None
    aspect_max: float = 2.5
    crane_bay: Optional[Tuple[float, float, float, float]] = None
    grid_m: float = DEFAULT_GRID_M
    weights: Dict[str, float] = field(default_factory=dict)
    source: Dict[str, Any] = field(default_factory=dict)    # 원본 IR dict (해시용)

    DEFAULT_W = dict(
        volume=1.0,        # w1 : 건물 체적
        area=0.0,          # w1': 건축면적
        link=1.0,          # w2 : 배관/케이블/덕트 물량
        zone=0.3,          # w3 : 방사선구역 분산 (RZDI 계열)
        sep=50.0,          # w4 : 이격 위반 (hard)
        sep_soft=5.0,      # w4': 이격 위반 (soft)
        exterior=20.0,     # w5 : 외벽 접면 위반
        crane=20.0,        # w6 : 크레인 커버리지 위반
        aspect=30.0,       # w7 : 외피 형상비 위반
        ecc=0.5,           # w8 : 질량 편심
        elev=50.0,         # w9 : 중력구동 표고 순서 위반
        envelope=200.0,    # 부지 한계 초과
        fixed=50.0,        # 좌표 고정 위반
    )

    def wt(self, k: str) -> float:
        return self.weights.get(k, self.DEFAULT_W[k])


# 목적함수 항 중 'hard 제약' 에 해당하는 것 — 최종해에서 0 이어야 한다
HARD_TERMS = ("sep", "exterior", "crane", "elev", "envelope", "fixed")


# ---------------------------------------------------------------------------
# Sequence-Pair 패킹
# ---------------------------------------------------------------------------

def pack_sequence_pair(dims: List[Tuple[float, float]],
                       gp: List[int], gm: List[int]
                       ) -> Tuple[List[float], List[float], float, float]:
    """
    Sequence-Pair (gp=Γ⁺, gm=Γ⁻) 를 좌하단 정렬 packing 으로 디코딩.

    관계 규칙 (Murata 1996):
      pos⁺(i) < pos⁺(j) and pos⁻(i) < pos⁻(j)  ->  i 는 j 의 '왼쪽'
      pos⁺(i) > pos⁺(j) and pos⁻(i) < pos⁻(j)  ->  i 는 j 의 '아래'
    각각 수평/수직 제약그래프의 최장경로로 좌표를 얻는다. 겹침이 원천적으로 없다.
    """
    n = len(dims)
    pp = [0] * n
    pm = [0] * n
    for k, i in enumerate(gp):
        pp[i] = k
    for k, i in enumerate(gm):
        pm[i] = k

    x = [0.0] * n
    for i in gp:                       # pos⁺ 오름차순 → '왼쪽' 선행자가 먼저 확정
        best = 0.0
        for j in range(n):
            if j != i and pp[j] < pp[i] and pm[j] < pm[i]:
                v = x[j] + dims[j][0]
                if v > best:
                    best = v
        x[i] = best

    y = [0.0] * n
    for i in gm:                       # pos⁻ 오름차순 → '아래' 선행자가 먼저 확정
        best = 0.0
        for j in range(n):
            if j != i and pp[j] > pp[i] and pm[j] < pm[i]:
                v = y[j] + dims[j][1]
                if v > best:
                    best = v
        y[i] = best

    W = max((x[i] + dims[i][0] for i in range(n)), default=0.0)
    D = max((y[i] + dims[i][1] for i in range(n)), default=0.0)
    return x, y, W, D


def legalize_around(ids: List[int], dims: List[Tuple[float, float]],
                    x: List[float], y: List[float],
                    fixed: Sequence[Tuple[int, float, float, float, float]]
                    ) -> Tuple[List[float], List[float]]:
    """
    Sequence-Pair 압축 배치를 고정 장애물(pinned) 주변으로 합법화한다.

    블록을 (x+y, x, y, id) 순으로 하나씩 놓으며, 이미 놓인 것(장애물 포함)과
    겹치면 오른쪽 또는 위쪽 중 '덜 움직이는' 방향으로 밀어낸다. 결정론적이고
    겹침 0 을 보장한다. 밀려난 만큼 외피가 커지므로 SA 가 순열로 이를 학습한다.
    """
    placed = [(fx, fy, fx + fw, fy + fd) for (_i, fx, fy, fw, fd) in fixed]
    nx, ny = list(x), list(y)
    order = sorted(range(len(ids)), key=lambda k: (x[k] + y[k], x[k], y[k], ids[k]))
    eps = 1e-9
    for k in order:
        w, d = dims[k]
        cx, cy = nx[k], ny[k]
        def first_hit(px, py):
            for r in placed:
                if px < r[2] - eps and r[0] < px + w - eps and py < r[3] - eps and r[1] < py + d - eps:
                    return r
            return None
        for _ in range(8 * (len(placed) + 1)):
            hit = first_hit(cx, cy)
            if hit is None:
                break
            dx, dy = hit[2] - cx, hit[3] - cy
            if dx <= dy:
                cx = hit[2]
            else:
                cy = hit[3]
        if first_hit(cx, cy) is not None:
            # 안전망: 놓인 것 전체의 오른쪽으로 보낸다 (항상 빈 자리).
            cx = max(r[2] for r in placed)
        nx[k], ny[k] = cx, cy
        placed.append((cx, cy, cx + w, cy + d))
    return nx, ny


# ---------------------------------------------------------------------------
# 해(solution) 표현
# ---------------------------------------------------------------------------

DEFAULT_AZIMUTHS = tuple(range(0, 180, 15))


class Solution:
    """층별 Sequence-Pair + 회전 + 층배정 + 터빈 방위각."""

    def __init__(self, spec: Spec, rng: random.Random):
        self.spec = spec
        self.n = len(spec.blocks)
        self.idx = {b.id: i for i, b in enumerate(spec.blocks)}
        self.rot = [0] * self.n
        self.floor = [
            (b.floor if b.floor is not None else rng.randrange(spec.n_floors))
            for b in spec.blocks
        ]
        self.azimuths = _allowed_azimuths(spec)
        self.turbine_azimuth: Dict[int, float] = {}
        for i, b in enumerate(spec.blocks):
            if b.turbine:
                self.turbine_azimuth[i] = float(rng.choice(self.azimuths))
        self.gp: List[List[int]] = []
        self.gm: List[List[int]] = []
        self._rebuild(rng)

    # -- 다층 관통 -------------------------------------------------------
    def span_of(self, i: int, f: int) -> int:
        """블록 i 를 기준층 f 에 두었을 때 점유하는 층 수 (>=1)."""
        sp = self.spec
        fh = sp.floor_heights or [_default_fh(sp)] * sp.n_floors
        h = sp.blocks[i].h
        acc, k = 0.0, 0
        # 층수를 넘어가도 필요한 층수를 그대로 센다. 잘라 버리면 '지붕을 뚫는 탱크'
        # 가 조용히 정상으로 보고된다 — TALL-EQUIP 이 이것을 잡아야 한다.
        while acc < h - 1e-9 and k < 2 * sp.n_floors + 2:
            acc += fh[f + k] if f + k < sp.n_floors else fh[-1]
            k += 1
        return max(1, k)

    def floors_of(self, i: int) -> List[int]:
        f = self.floor[i]
        k = self.span_of(i, f)
        return [g for g in range(f, min(f + k, self.spec.n_floors))]

    def sp_floors(self, i: int) -> List[int]:
        """Sequence-Pair 에 들어가는 층. 고정 장애물(pinned)은 순열에 넣지 않는다."""
        return [] if self.spec.blocks[i].pinned else self.floors_of(i)

    def _rebuild(self, rng: random.Random):
        self.gp = [[] for _ in range(self.spec.n_floors)]
        self.gm = [[] for _ in range(self.spec.n_floors)]
        for i in range(self.n):
            for f in self.sp_floors(i):
                self.gp[f].append(i)
                self.gm[f].append(i)
        for f in range(self.spec.n_floors):
            rng.shuffle(self.gp[f])
            rng.shuffle(self.gm[f])

    def _unplace(self, i: int):
        for f in range(self.spec.n_floors):
            if i in self.gp[f]:
                self.gp[f].remove(i)
            if i in self.gm[f]:
                self.gm[f].remove(i)

    def _place(self, i: int, rng: random.Random):
        for f in self.sp_floors(i):
            self.gp[f].insert(rng.randrange(len(self.gp[f]) + 1), i)
            self.gm[f].insert(rng.randrange(len(self.gm[f]) + 1), i)

    def clone(self) -> "Solution":
        s = object.__new__(Solution)
        s.spec, s.n, s.idx = self.spec, self.n, self.idx
        s.rot = list(self.rot)
        s.floor = list(self.floor)
        s.azimuths = self.azimuths
        s.turbine_azimuth = dict(self.turbine_azimuth)
        s.gp = [list(g) for g in self.gp]
        s.gm = [list(g) for g in self.gm]
        return s

    # ------------------------------------------------------------------
    def decode(self) -> Tuple[Dict[int, dict], Dict[Tuple[int, int], dict]]:
        """
        각 블록의 (x, y, w, d, floor, z) 확정.

        반환: (geo, shadows)
          geo[i]           기준층 배치
          shadows[(i, f)]  다층 관통 기기의 상위층 배치 (f > 기준층)
        """
        sp = self.spec
        geo: Dict[int, dict] = {}
        shadows: Dict[Tuple[int, int], dict] = {}
        Wm = Dm = 0.0
        pinned = self._pinned_rects()
        for f in range(sp.n_floors):
            # 다층 관통 기기의 위층 점유는 순열로 따로 배치하지 않는다 — 기준층 좌표를
            # 그대로 고정 장애물로 둔다. 관통부 정렬이 페널티가 아니라 '구조적으로' 0 이 된다.
            # (위층 순열에 남아 있는 관통 기기 항목은 디코딩에서 건너뛴다.)
            through = [(i, geo[i]["x"], geo[i]["y"], geo[i]["w"], geo[i]["d"])
                       for i in self.gp[f] if self.floor[i] < f and i in geo]
            fixed = pinned.get(f, []) + through
            ids = [i for i in self.gp[f] if self.floor[i] == f]
            if ids:
                local = {b: k for k, b in enumerate(ids)}
                dims = [sp.blocks[i].footprint(self.rot[i]) for i in ids]
                gp = [local[i] for i in self.gp[f] if i in local]
                gm = [local[i] for i in self.gm[f] if i in local]
                x, y, W, D = pack_sequence_pair(dims, gp, gm)
                if fixed:
                    # 고정 장애물·관통부를 피해 가도록 결정론적으로 밀어낸다 (겹침은 여전히 0).
                    x, y = legalize_around(ids, dims, x, y, fixed)
            else:
                x, y, dims = [], [], []
            for (i, rx, ry, rw, rd) in through:
                shadows[(i, f)] = dict(x=rx, y=ry, w=rw, d=rd, f=f)
                Wm, Dm = max(Wm, rx + rw), max(Dm, ry + rd)
            for k, i in enumerate(ids):
                rec = dict(x=x[k], y=y[k], w=dims[k][0], d=dims[k][1], f=f)
                Wm, Dm = max(Wm, rec["x"] + rec["w"]), max(Dm, rec["y"] + rec["d"])
                if self.floor[i] == f:
                    geo[i] = rec
                else:
                    shadows[(i, f)] = rec
            for (i, rx, ry, rw, rd) in fixed:
                rec = dict(x=rx, y=ry, w=rw, d=rd, f=f)
                if sp.blocks[i].kind != "void":
                    # 배치 금지 영역(void)은 외피를 키우지 않는다 — 빈 모서리일 뿐이다.
                    Wm, Dm = max(Wm, rx + rw), max(Dm, ry + rd)
                if self.floor[i] == f:
                    geo[i] = rec
                else:
                    shadows[(i, f)] = rec

        fh = sp.floor_heights or [_default_fh(sp)] * sp.n_floors
        base = [sum(fh[:f]) for f in range(sp.n_floors)]
        for i, g in geo.items():
            g["z"] = base[g["f"]]
            g["cx"] = g["x"] + g["w"] / 2
            g["cy"] = g["y"] + g["d"] / 2
        for (i, f), g in shadows.items():
            g["z"] = base[f]
            g["cx"] = g["x"] + g["w"] / 2
            g["cy"] = g["y"] + g["d"] / 2
        self.env = (Wm, Dm, sum(fh[:sp.n_floors]))
        self.fh = fh
        self.geo, self.shadows = geo, shadows
        return geo, shadows

    def _pinned_rects(self) -> Dict[int, List[Tuple[int, float, float, float, float]]]:
        """층별 고정 장애물 사각형 (clearance 포함, 좌하단 기준). fixed_xy 는 중심이다."""
        out: Dict[int, List[Tuple[int, float, float, float, float]]] = {}
        for i, b in enumerate(self.spec.blocks):
            if not b.pinned:
                continue
            w, d = b.footprint(0)
            cx, cy = b.fixed_xy
            for f in self.floors_of(i):
                out.setdefault(f, []).append((i, cx - w / 2, cy - d / 2, w, d))
        return out

    # ------------------------------------------------------------------
    def context(self) -> EvalContext:
        geo, shadows = self.decode()
        ctx = EvalContext(self.spec, geo, self.env, shadows)
        ctx.turbine_azimuth = self.turbine_azimuth
        ctx.tall_span = {i: self.span_of(i, self.floor[i]) for i in range(self.n)}
        return ctx

    # ------------------------------------------------------------------
    def evaluate(self, detail: bool = False):
        """
        목적함수 F 계산.

        detail=True 이면 (F, 항별 dict, geo, env, 위반목록, shadows) 를 반환한다.
        hard 제약 위반은 '항별 값' 과 '위반 목록' 양쪽에서 기계적으로 확인해야 한다.
        도면을 눈으로 봐서는 0.10 m 위반이 보이지 않는다.
        """
        sp = self.spec
        ctx = self.context()
        geo, shadows = ctx.geo, ctx.shadows
        W, D, H = self.env
        t: Dict[str, float] = {}

        # --- (1) 공간 항 : 체적 / 면적 -------------------------------------
        t["volume"] = sp.wt("volume") * (W * D * H)
        t["area"] = sp.wt("area") * (W * D)

        # --- (2) 연결 항 : 배관/케이블 물량 (가중 맨해튼 거리) ---------------
        c = 0.0
        for lk in sp.links:
            i, j = self.idx.get(lk.a), self.idx.get(lk.b)
            if i is None or j is None or i not in geo or j not in geo:
                continue
            gi, gj = geo[i], geo[j]
            dz = abs(gi["z"] - gj["z"])
            c += lk.cost * (abs(gi["cx"] - gj["cx"]) + abs(gi["cy"] - gj["cy"])
                            + lk.vertical_factor * dz)
        t["link"] = sp.wt("link") * c

        # --- (3) 구역 분산 항 (RZDI 계열) ------------------------------------
        t["zone"] = sp.wt("zone") * zone_dispersion(sp, geo)

        # --- (4) 이격 위반 항 -------------------------------------------------
        bviols: List[Violation] = []
        vh, vs, sep_v = separation_violation(sp, geo, ctx)
        bviols.extend(sep_v)
        t["sep"] = sp.wt("sep") * vh
        t["sep_soft"] = sp.wt("sep_soft") * vs

        # --- (5) 외벽 접면 요구 -----------------------------------------------
        ex = 0.0
        for i, g in geo.items():
            if sp.blocks[i].need_exterior:
                dd = min(g["x"], g["y"], max(0.0, W - (g["x"] + g["w"])),
                         max(0.0, D - (g["y"] + g["d"])))
                ex += dd
                if dd > 1e-9:
                    bviols.append(Violation(
                        "EXTERIOR",
                        f"{sp.blocks[i].id}: 외벽 접면 요구인데 외벽에서 {dd:.2f} m "
                        f"떨어져 있음 (급배기·외기취입·반출입 동선)",
                        dd, "m", True, [sp.blocks[i].id],
                        marker=dict(kind="rect", floor=g["f"],
                                    a=(g["x"], g["y"], g["w"], g["d"]))))
        t["exterior"] = sp.wt("exterior") * ex

        # --- (6) 크레인 커버리지 ----------------------------------------------
        cr = 0.0
        if sp.crane_bay:
            rx0, ry0, rx1, ry1 = sp.crane_bay
            bx0, by0, bx1, by1 = rx0 * W, ry0 * D, rx1 * W, ry1 * D
            for i, g in geo.items():
                if sp.blocks[i].need_crane:
                    ox = max(0.0, max(bx0 - g["x"], (g["x"] + g["w"]) - bx1))
                    oy = max(0.0, max(by0 - g["y"], (g["y"] + g["d"]) - by1))
                    cr += ox + oy
                    if ox + oy > 1e-9:
                        bviols.append(Violation(
                            "CRANE",
                            f"{sp.blocks[i].id}: 크레인 베이를 Δx={ox:.2f} "
                            f"Δy={oy:.2f} m 벗어남 — 중량물 취급 불가",
                            ox + oy, "m", True, [sp.blocks[i].id],
                            marker=dict(kind="rect", floor=g["f"],
                                        a=(g["x"], g["y"], g["w"], g["d"]))))
        t["crane"] = sp.wt("crane") * cr

        # --- (7) 외피 형상비 / 부지 한계 ---------------------------------------
        lo, hi = (min(W, D), max(W, D))
        t["aspect"] = sp.wt("aspect") * max(0.0, (hi / lo if lo > 0 else 0) - sp.aspect_max)
        envv = 0.0
        if sp.max_w:
            envv += max(0.0, W - sp.max_w)
        if sp.max_d:
            envv += max(0.0, D - sp.max_d)
        if envv > 1e-9:
            bviols.append(Violation(
                "ENVELOPE",
                f"외피 {W:.1f}×{D:.1f} m 가 부지/구조 한계 "
                f"{sp.max_w}×{sp.max_d} m 를 초과",
                envv, "m", True, []))
        t["envelope"] = sp.wt("envelope") * envv

        # --- (8) 질량 편심 (내진 비틀림) ----------------------------------------
        mw = sum(sp.blocks[i].weight for i in geo) or 1.0
        cmx = sum(sp.blocks[i].weight * geo[i]["cx"] for i in geo) / mw
        cmy = sum(sp.blocks[i].weight * geo[i]["cy"] for i in geo) / mw
        t["ecc"] = sp.wt("ecc") * math.hypot(cmx - W / 2, cmy - D / 2)

        # --- (9) 표고 순서 제약 (피동 중력구동) — SMR 고유 ------------------------
        el = 0.0
        for r in sp.elevations:
            i, j = self.idx.get(r.above), self.idx.get(r.below)
            if i is None or j is None or i not in geo or j not in geo:
                continue
            v = max(0.0, (geo[j]["z"] + r.min_dz) - geo[i]["z"])
            el += v
            if v > 1e-9:
                bviols.append(Violation(
                    "ELEV",
                    f"{r.above}(EL {geo[i]['z']:.2f}) 가 {r.below}"
                    f"(EL {geo[j]['z']:.2f}) 보다 {r.min_dz:g} m 이상 높아야 하는데 "
                    f"{v:.2f} m 부족 — 중력구동 수두 미확보 (근거: {r.basis or '미기재'})",
                    v, "m", True, [r.above, r.below],
                    marker=dict(kind="rect", floor=geo[i]["f"],
                                a=(geo[i]["x"], geo[i]["y"], geo[i]["w"], geo[i]["d"]))))
        t["elev"] = sp.wt("elev") * el

        # --- (10) 좌표 고정 위반 -----------------------------------------------
        fx = 0.0
        for i, g in geo.items():
            fp = sp.blocks[i].fixed_xy
            if fp:
                v = math.dist((g["cx"], g["cy"]), fp)
                fx += v
                if v > 1e-9:
                    bviols.append(Violation(
                        "FIXED",
                        f"{sp.blocks[i].id}: 고정 좌표 {fp} 에서 {v:.2f} m 이탈",
                        v, "m", True, [sp.blocks[i].id],
                        marker=dict(kind="rect", floor=g["f"],
                                    a=(g["x"], g["y"], g["w"], g["d"]))))
        t["fixed"] = sp.wt("fixed") * fx

        # --- (11) 제약 라이브러리 -----------------------------------------------
        viols, cterms = CONS.evaluate_all(ctx)
        t.update(cterms)
        viols = bviols + viols

        F = sum(t.values())
        if detail:
            return F, t, geo, (W, D, H), viols, shadows
        return F

    # ------------------------------------------------------------------
    def objectives(self) -> Tuple[List[float], float]:
        """
        NSGA 용 3목적 + 제약위반 스칼라.

        목적: ①건물 체적 ②연결비용 ③방사선구역 분산
        (HPR1000 보조건물 NSGA-III 구성과 동일. 나머지는 제약/페널티로 남긴다.)
        """
        F, t, geo, env, viols, _ = self.evaluate(detail=True)
        W, D, H = env
        f1 = W * D * H
        f2 = t["link"] / max(self.spec.wt("link"), 1e-9)
        f3 = zone_dispersion(self.spec, geo)
        cv = sum(t.get(k, 0.0) for k in HARD_TERMS)
        cv += sum(v for k, v in t.items()
                  if k.startswith("constraint.")
                  and CONS.REGISTRY[k.split(".", 1)[1]].hard)
        return [f1, f2, f3], cv


def _default_fh(sp: Spec) -> float:
    return max((b.h for b in sp.blocks), default=4.0) + 1.5


def _allowed_azimuths(sp: Spec) -> Tuple[float, ...]:
    for c in sp.constraints or []:
        if c.get("id") == "TURBINE-25":
            p = c.get("params") or {}
            if p.get("azimuth_deg") is not None:
                return (float(p["azimuth_deg"]),)
            if p.get("allowed_azimuths_deg"):
                return tuple(float(a) for a in p["allowed_azimuths_deg"])
    return tuple(float(a) for a in DEFAULT_AZIMUTHS)


def zone_dispersion(sp: Spec, geo: Dict[int, dict]) -> float:
    """동일 zone 블록의 중심 분산 (RZDI 유사). 순수 기하량."""
    zs: Dict[str, List[dict]] = {}
    for i, g in geo.items():
        z = sp.blocks[i].zone
        if z:
            zs.setdefault(z, []).append(g)
    disp = 0.0
    for z, gs in zs.items():
        if len(gs) < 2:
            continue
        mx = sum(g["cx"] for g in gs) / len(gs)
        my = sum(g["cy"] for g in gs) / len(gs)
        mz = sum(g["z"] for g in gs) / len(gs)
        disp += sum(math.dist((g["cx"], g["cy"], g["z"]), (mx, my, mz))
                    for g in gs) / len(gs)
    return disp


def separation_violation(sp: Spec, geo: Dict[int, dict], ctx: EvalContext
                         ) -> Tuple[float, float, List[Violation]]:
    """이격 위반 — 중심거리가 아니라 '외곽 간 간극' 으로 측정."""
    from geometry import gap, rect_of
    idx = ctx.idx
    vh = vs = 0.0
    out: List[Violation] = []
    for r in sp.separations:
        if r.by_zone:
            A = [i for i in geo if sp.blocks[i].zone == r.a]
            B = [i for i in geo if sp.blocks[i].zone == r.b]
            pairs = [(i, j) for i in A for j in B if i != j]
        else:
            i, j = idx.get(r.a), idx.get(r.b)
            pairs = [(i, j)] if (i is not None and j is not None) else []
        for i, j in pairs:
            if i not in geo or j not in geo:
                continue
            if geo[i]["f"] != geo[j]["f"]:
                continue          # 다른 층 = 바닥슬래브로 분리 (FIRE-HATCH 가 병합을 본다)
            g = gap(rect_of(geo[i]), rect_of(geo[j]), "euclid")
            v = max(0.0, r.min_dist - g)
            if v <= 1e-9:
                continue
            if r.hard:
                vh += v
                out.append(Violation(
                    "SEP",
                    f"{sp.blocks[i].id}↔{sp.blocks[j].id}: 간극 {g:.2f} m < "
                    f"요구 {r.min_dist:g} m (근거: {r.basis or '미기재'})",
                    v, "m", True, [sp.blocks[i].id, sp.blocks[j].id],
                    marker=dict(kind="pair", floor=geo[i]["f"],
                                a=rect_of(geo[i]), b=rect_of(geo[j]))))
            else:
                vs += v
    return vh, vs, out


def hard_violations(spec: Spec, terms: Dict[str, float],
                    viols: Sequence[Violation]) -> List[str]:
    """최종해에서 0 이어야 하는 항 중 0 이 아닌 것을 문자열로 반환."""
    out = [str(v) for v in viols if v.hard and v.amount > 1e-6]
    # 안전망: 항 값은 0 이 아닌데 위반 목록이 비어 있으면 계산 경로가 어긋난 것이다.
    for k in HARD_TERMS:
        v = terms.get(k, 0.0)
        if v > 1e-6 and not any(x.cid == k.upper() for x in viols):
            out.append(f"[{k.upper()}] 항 값 {v:,.3f} 인데 위반 상세가 없음 "
                       f"— 엔진 내부 불일치. 보고할 것.")
    return out


# ---------------------------------------------------------------------------
# Simulated Annealing
# ---------------------------------------------------------------------------

MOVES = ("swap_p", "swap_m", "swap_both", "rotate", "move_floor", "shift",
         "turbine_rotate", "sync_pen")


def optimize(spec: Spec, iters: int = 60000, restarts: int = 4,
             seed: int = 20260826, T0: float = None, T1: float = 1e-3,
             verbose: bool = True, progress=None, tick=None, init: "Solution" = None):
    """
    SA + 다중 재시작. seed 고정으로 완전 재현 가능.
    init 이 주어지면 첫 재시작을 그 해에서 출발한다 (현황 배치 warm start).
    최선해 추적이므로 결과는 init 보다 나빠지지 않는다.
    """
    rng = random.Random(seed)
    best_all, bestF_all, hist_all = None, float("inf"), []

    for r in range(restarts):
        cur = init.clone() if (init is not None and r == 0) else Solution(spec, rng)
        curF = cur.evaluate()
        best, bestF = cur.clone(), curF
        T0r = T0 or max(1.0, abs(curF) * 0.08)
        hist = []
        for it in range(iters):
            T = T0r * (T1 / T0r) ** (it / max(1, iters - 1))
            cand = cur.clone()
            _mutate(cand, rng)
            cF = cand.evaluate()
            if cF < curF or rng.random() < math.exp(-(cF - curF) / max(T, 1e-9)):
                cur, curF = cand, cF
                if cF < bestF:
                    best, bestF = cand.clone(), cF
            if it % 200 == 0:
                hist.append(bestF)
                if tick and it % 1000 == 0:
                    tick(r, it, bestF)       # 진행 표시 전용 — 탐색에 영향 없음
        if verbose:
            print(f"  restart {r+1}/{restarts}: F = {bestF:,.1f}")
        if progress:
            progress(r, bestF)
        if bestF < bestF_all:
            best_all, bestF_all, hist_all = best, bestF, hist

    # 마무리: 관통부 정렬 국소 수복 (결정론적, 개선될 때만 채택)
    if best_all is not None:
        best_all, bestF_all = repair_penetrations(best_all, bestF_all,
                                                  verbose=verbose)
    return best_all, bestF_all, hist_all


def repair_penetrations(sol: Solution, F0: float = None, max_rounds: int = 4,
                        verbose: bool = False) -> Tuple[Solution, float]:
    """
    다층 관통 기기의 상위층 순열 위치를 전수 탐색으로 국소 수복한다.

    SA 의 확률적 이동만으로는 관통부 정렬오차(TALL-EQUIP)가 0.10 m 수준에서
    남는 일이 잦다. 그 잔차는 도면상 정상으로 보이므로 반드시 없애야 한다.
    여기서는 각 관통 블록에 대해 상위층 Γ⁺/Γ⁻ 의 모든 삽입 위치를 시도하고
    목적함수가 개선될 때만 채택한다 — 결정론적이고 단조 감소한다.
    """
    best = sol
    bestF = F0 if F0 is not None else sol.evaluate()
    tall = [i for i in range(sol.n) if len(sol.sp_floors(i)) > 1]
    if not tall:
        return best, bestF
    for _ in range(max_rounds):
        improved = False
        for i in tall:
            for ff in best.floors_of(i)[1:]:
                if i not in best.gp[ff] or i not in best.gm[ff]:
                    continue
                n_p, n_m = len(best.gp[ff]), len(best.gm[ff])
                # Γ⁺ 와 Γ⁻ 를 '동시에' 옮겨야 좌표가 맞는 경우가 대부분이다.
                # 한쪽씩만 바꾸는 탐색은 그 조합을 영원히 찾지 못한다.
                for pp in range(n_p):
                    for pm in range(n_m):
                        cand = best.clone()
                        a = cand.gp[ff]; a.remove(i); a.insert(pp, i)
                        b = cand.gm[ff]; b.remove(i); b.insert(pm, i)
                        F = cand.evaluate()
                        if F < bestF - 1e-9:
                            best, bestF, improved = cand, F, True
        # 삽입 위치만으로 안 되는 경우: 상위층의 공통 블록 상대순서를 기준층과
        # 통째로 맞춘 후보를 시도한다. 개선될 때만 채택하므로 안전하다.
        for i in tall:
            f0 = best.floor[i]
            for ff in best.floors_of(i)[1:]:
                for tail_first in (False, True):
                    cand = best.clone()
                    for which in ("gp", "gm"):
                        a0 = getattr(best, which)[f0]
                        a1 = getattr(cand, which)[ff]
                        st = set(a1)
                        ordered = [b for b in a0 if b in st]
                        oset = set(ordered)
                        rest = [b for b in a1 if b not in oset]
                        getattr(cand, which)[ff] = (rest + ordered if tail_first
                                                    else ordered + rest)
                    F = cand.evaluate()
                    if F < bestF - 1e-9:
                        best, bestF, improved = cand, F, True
        if not improved:
            break
    if verbose:
        print(f"  관통부 정렬 수복 후: F = {bestF:,.1f}")
    return best, bestF


def _mutate(s: Solution, rng: random.Random):
    sp = s.spec
    for _ in range(12):
        m = rng.choice(MOVES)
        f = rng.randrange(sp.n_floors)
        g = s.gp[f]
        if m in ("swap_p", "swap_m", "swap_both", "shift"):
            if len(g) < 2:
                continue
            a, b = rng.sample(range(len(g)), 2)
            if m == "swap_p":
                s.gp[f][a], s.gp[f][b] = s.gp[f][b], s.gp[f][a]
            elif m == "swap_m":
                s.gm[f][a], s.gm[f][b] = s.gm[f][b], s.gm[f][a]
            elif m == "swap_both":
                s.gp[f][a], s.gp[f][b] = s.gp[f][b], s.gp[f][a]
                s.gm[f][a], s.gm[f][b] = s.gm[f][b], s.gm[f][a]
            else:
                v = s.gp[f].pop(a)
                s.gp[f].insert(b, v)
            return
        if m == "rotate":
            cand = [i for i in range(s.n)
                    if sp.blocks[i].rotatable and not sp.blocks[i].pinned]
            if cand:
                i = rng.choice(cand)
                s.rot[i] ^= 1
                return
        if m == "sync_pen":
            # 다층 관통 기기의 상위층 순열 위치를 기준층과 같은 상대위치로 옮긴다.
            # 관통부 정렬오차(TALL-EQUIP)를 0 으로 만들려면 이 이동이 필요하다 —
            # 일반 교환만으로는 상·하층이 같은 좌표로 수렴하기 어렵다.
            cand = [i for i in range(s.n) if len(s.sp_floors(i)) > 1]
            if cand:
                i = rng.choice(cand)
                f0 = s.floor[i]
                for ff in s.floors_of(i)[1:]:
                    for a0, a1 in ((s.gp[f0], s.gp[ff]), (s.gm[f0], s.gm[ff])):
                        if i not in a0 or i not in a1:
                            continue
                        # 두 층에 공통으로 있는 블록만 남긴 순서에서, 기준층의
                        # 바로 앞 블록 뒤에 오도록 상위층 순열을 맞춘다.
                        st = set(a1)
                        common = [b for b in a0 if b in st]
                        k = common.index(i)
                        a1.remove(i)
                        if k == 0:
                            pos = 0
                        else:
                            anchor = common[k - 1]
                            pos = a1.index(anchor) + 1 if anchor in a1 else 0
                        a1.insert(pos, i)
                return
        if m == "turbine_rotate" and s.turbine_azimuth:
            i = rng.choice(sorted(s.turbine_azimuth))
            s.turbine_azimuth[i] = float(rng.choice(s.azimuths))
            return
        if m == "move_floor" and sp.n_floors > 1:
            cand = [i for i in range(s.n)
                    if sp.blocks[i].floor is None and not sp.blocks[i].pinned]
            if cand:
                i = rng.choice(cand)
                old = s.floor[i]
                new = rng.randrange(sp.n_floors)
                if new == old:
                    continue
                s._unplace(i)
                s.floor[i] = new
                s._place(i, rng)
                return


# ---------------------------------------------------------------------------
# 기준안(baseline) — '사람이 손으로 줄 세워 배치' 한 것에 해당
# ---------------------------------------------------------------------------

def baseline(spec: Spec, seed: int = 0) -> Solution:
    """
    비교 기준안: 층별로 입력 순서대로 좌→우, 폭이 차면 다음 행으로 쌓는
    전형적 수작업 배치. 같은 Sequence-Pair 표현으로 같은 목적함수로 평가한다.

    ※ 이 기준안은 나이브하다. 여기서 나오는 개선율(프로토타입에서 −42.6%)을
      성과로 제시하면 안 된다. 실제 기대치는 −10~15% 이다. (README 참조)
    """
    rng = random.Random(seed)
    s = Solution(spec, rng)
    s.rot = [0] * s.n
    rr = 0
    for i, b in enumerate(spec.blocks):
        if b.floor is None:
            s.floor[i] = rr % spec.n_floors
            rr += 1
        else:
            s.floor[i] = b.floor

    target_w = spec.max_w or math.sqrt(sum(b.w * b.d for b in spec.blocks)) * 1.6
    s.gp, s.gm = [], []
    members: List[List[int]] = [[] for _ in range(spec.n_floors)]
    for i in range(s.n):
        for f in s.sp_floors(i):
            members[f].append(i)
    for f in range(spec.n_floors):
        ids = members[f]
        rows, cur, cw = [], [], 0.0
        for i in ids:
            w, _ = spec.blocks[i].footprint(0)
            if cur and cw + w > target_w:
                rows.append(cur); cur, cw = [], 0.0
            cur.append(i); cw += w
        if cur:
            rows.append(cur)
        gp, gm = [], []
        for r in reversed(rows):
            gp += r
        for r in rows:
            gm += r
        s.gp.append(gp); s.gm.append(gm)
    return s


# ---------------------------------------------------------------------------
# Spec 로더
# ---------------------------------------------------------------------------

_BLOCK_FIELDS = set(Block.__dataclass_fields__)


def spec_from_dict(d: dict) -> Spec:
    """
    IR(dict) -> Spec.

    blocks[i].count > 1 이면 -1, -2 … 로 복제하고, links / separations 가
    원본 id 를 참조하면 복제된 모든 인스턴스로 전개한다.
    (예: 안전주입펌프 count=2 이고 링크가 'SIP'->'RWST' 이면 SIP-1, SIP-2 모두 연결)
    """
    blocks, expand = [], {}
    for b in d["blocks"]:
        b = {k: v for k, v in b.items() if not k.startswith("_")}
        unknown = set(b) - _BLOCK_FIELDS - {"count", "instances"}
        if unknown:
            raise ValueError(f"블록 '{b.get('id')}' 에 알 수 없는 필드: "
                             f"{sorted(unknown)}")
        n = int(b.pop("count", 1))
        # 인스턴스별 속성 — 중복 계열의 train/fire_zone 처럼 복제본마다 달라야 하는
        # 값은 여기에 '명시적으로' 적는다. 엔진이 추정하지 않는다.
        inst = b.pop("instances", None)
        if inst is not None and len(inst) != n:
            raise ValueError(f"블록 '{b.get('id')}': instances 길이({len(inst)})가 "
                             f"count({n}) 와 다름")
        base = dict(b)
        ids = []
        for k in range(n):
            bb = dict(base)
            if inst:
                bb.update({kk: vv for kk, vv in inst[k].items()
                           if not kk.startswith("_")})
            if n > 1:
                bb["id"] = f"{base['id']}-{k+1}"
                bb["name"] = f"{base['name']} #{k+1}"
            if isinstance(bb.get("fixed_xy"), list):
                bb["fixed_xy"] = tuple(bb["fixed_xy"])
            unk = set(bb) - _BLOCK_FIELDS
            if unk:
                raise ValueError(f"블록 '{bb.get('id')}' 에 알 수 없는 필드: {sorted(unk)}")
            ids.append(bb["id"])
            blocks.append(Block(**bb))
        expand[base["id"]] = ids

    def ex(name):
        return expand.get(name, [name])

    links = []
    for l in d.get("links", []):
        l = {k: v for k, v in l.items() if not k.startswith("_")}
        a, b_ = l.pop("a"), l.pop("b")
        pa, pb = ex(a), ex(b_)
        # n:m 전개는 pairwise 로 순환 대응시킨다. 계열이 2개면 배관도 2련이므로
        # 단위비용을 나누지 않는다 (총 물량이 계열 수에 비례하는 것이 물리적으로 맞다).
        for k in range(max(len(pa), len(pb))):
            links.append(Link(a=pa[k % len(pa)], b=pb[k % len(pb)],
                              cost=l.get("cost", 1.0),
                              kind=l.get("kind", "pipe"),
                              vertical_factor=l.get("vertical_factor", 3.0)))

    seps = []
    for s in d.get("separations", []):
        s = {k: v for k, v in s.items() if not k.startswith("_")}
        if s.get("by_zone"):
            seps.append(SeparationRule(**s))
            continue
        a, b_ = s.pop("a"), s.pop("b")
        for ia in ex(a):
            for ib in ex(b_):
                if ia != ib:
                    seps.append(SeparationRule(a=ia, b=ib, **s))

    return Spec(
        project=d.get("project", "untitled"),
        blocks=blocks,
        links=links,
        separations=seps,
        elevations=[ElevationRule(**{k: v for k, v in e.items()
                                     if not k.startswith("_")})
                    for e in d.get("elevations", [])],
        constraints=d.get("constraints", []) or [],
        n_floors=d.get("n_floors", 1),
        floor_heights=d.get("floor_heights", []),
        max_w=d.get("max_w"), max_d=d.get("max_d"),
        aspect_max=d.get("aspect_max", 2.5),
        crane_bay=tuple(d["crane_bay"]) if d.get("crane_bay") else None,
        grid_m=float(d.get("grid_m", DEFAULT_GRID_M)),
        weights=preset_weights(d),
        source=d,
    )


# 가중치 프리셋 — IR 의 "preset" 키. 명시한 weights 가 항상 우선한다.
#   space_first : "공간을 작게" 가 1순위. 체적 외 연성 항을 크게 낮춘다.
#                 hard 항 가중치는 프리셋이 정하지 않는다 — 한계이득 기반으로
#                 IR 에 명시해야 하고, Gate 1 이 그 하한을 점검한다.
PRESETS = {
    "balanced": {},
    "space_first": dict(volume=1.0, area=0.0, link=0.05, zone=0.02, ecc=0.02,
                        sep_soft=1.0, aspect=30.0),
}


def preset_weights(d: dict) -> Dict[str, float]:
    name = d.get("preset") or "balanced"
    if name not in PRESETS:
        raise ValueError(f"알 수 없는 preset '{name}' — 사용 가능: {sorted(PRESETS)}")
    w = dict(PRESETS[name])
    w.update(d.get("weights", {}) or {})
    return w


def load_spec(path: str) -> Spec:
    with open(path, encoding="utf-8") as fp:
        return spec_from_dict(json.load(fp))
