# -*- coding: utf-8 -*-
"""
geometry.py — 제약 평가에 쓰이는 순수 기하 함수.

모든 함수는 결정론적이며 부작용이 없다. 난수·전역상태를 쓰지 않는다.

주의: 이격은 항상 '외곽 간 간극(gap)' 으로 잰다. 중심 간 거리는 큰 기기에서
실제 이격을 크게 과대평가하므로 안전 이격 판정에 쓰면 안 된다.
(보고서 02장 w4 항 주석)
"""
from __future__ import annotations
import math
from typing import Dict, Iterable, List, Sequence, Tuple

Rect = Tuple[float, float, float, float]      # (x, y, w, d)  좌하단 기준


# ---------------------------------------------------------------------------
# 사각형 기본 연산
# ---------------------------------------------------------------------------

def rect_of(g: Dict) -> Rect:
    """decode() 가 만든 geo 항목 -> (x, y, w, d)."""
    return (g["x"], g["y"], g["w"], g["d"])


def axis_gaps(a: Rect, b: Rect) -> Tuple[float, float]:
    """축별 외곽 간극. 겹치면 해당 축은 0."""
    ax, ay, aw, ad = a
    bx, by, bw, bd = b
    dx = max(0.0, max(ax - (bx + bw), bx - (ax + aw)))
    dy = max(0.0, max(ay - (by + bd), by - (ay + ad)))
    return dx, dy


def gap(a: Rect, b: Rect, metric: str = "euclid") -> float:
    """
    두 사각형 외곽 간 거리.

      euclid    : sqrt(dx^2+dy^2)   — 물리적 최단거리. 기본값.
      chebyshev : max(dx, dy)       — Ejeh 2021 이 안전거리에 쓴 척도
                                       (TD_ij = max(XD, YD, VD)). 선형화 가능하고
                                       유클리드 대비 위험지수가 동일함이 검증됨.
      manhattan : dx + dy           — 배관 라우팅 대리모형에 대응
    """
    dx, dy = axis_gaps(a, b)
    if metric == "chebyshev":
        return max(dx, dy)
    if metric == "manhattan":
        return dx + dy
    if metric == "euclid":
        return math.hypot(dx, dy)
    raise ValueError(f"unknown gap metric: {metric!r}")


def overlap_area(a: Rect, b: Rect) -> float:
    """평면 중첩 면적 [m^2]. 접하기만 하면 0."""
    ax, ay, aw, ad = a
    bx, by, bw, bd = b
    ox = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    oy = max(0.0, min(ay + ad, by + bd) - max(ay, by))
    return ox * oy


def rect_corners(r: Rect) -> List[Tuple[float, float]]:
    x, y, w, d = r
    return [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]


def rect_center(r: Rect) -> Tuple[float, float]:
    x, y, w, d = r
    return (x + w / 2.0, y + d / 2.0)


def dist_to_boundary(r: Rect, W: float, D: float) -> float:
    """외피(0,0,W,D) 경계까지의 최단 거리. 0이면 외벽에 접함."""
    x, y, w, d = r
    return min(x, y, max(0.0, W - (x + w)), max(0.0, D - (y + d)))


# ---------------------------------------------------------------------------
# 부채꼴(sector) — 터빈 미사일 금지영역, HELB 제트 원뿔
# ---------------------------------------------------------------------------

def _ang_dev(px: float, py: float, ox: float, oy: float, axis_rad: float) -> Tuple[float, float]:
    """
    원점 o 에서 점 p 까지의 (거리, 축으로부터의 각편차).
    축은 양방향 대칭으로 본다(터빈 휠 평면은 양쪽으로 미사일을 낸다).
    각편차는 [0, pi/2] 범위.
    """
    vx, vy = px - ox, py - oy
    r = math.hypot(vx, vy)
    if r < 1e-12:
        return 0.0, 0.0
    th = math.atan2(vy, vx) - axis_rad
    # 양방향 대칭: pi 주기로 접어 [-pi/2, pi/2]
    th = (th + math.pi / 2.0) % math.pi - math.pi / 2.0
    return r, abs(th)


def sector_intrusion(target: Rect, origin: Tuple[float, float],
                     axis_deg: float, half_angle_deg: float,
                     max_range_m: float = None,
                     bidirectional: bool = True) -> float:
    """
    금지 부채꼴에 대한 침입량 [m] — '호(arc) 길이 결손' 척도.

    target 의 4개 모서리 + 중심 5점 각각에 대해, 축으로부터의 각편차가
    half_angle 안쪽이면 r * (half_angle - |dtheta|) [rad*m = m] 를 누적하고
    5로 나눈다. 값이 0이면 부채꼴 밖, 클수록 깊이 들어와 있다.

    연속이고 단조라 SA/NSGA 가 밖으로 밀어내는 방향을 찾을 수 있다.
    이산 판정(안/밖)만 쓰면 탐색이 평평해져 빠져나오지 못한다.

    bidirectional=False 이면 축의 +방향 반쪽만 금지영역으로 본다
    (제트 충돌 원뿔처럼 방향성이 있는 경우).
    """
    ha = math.radians(half_angle_deg)
    ax = math.radians(axis_deg)
    ox, oy = origin
    tot = 0.0
    pts = rect_corners(target) + [rect_center(target)]
    for px, py in pts:
        if bidirectional:
            r, dth = _ang_dev(px, py, ox, oy, ax)
        else:
            vx, vy = px - ox, py - oy
            r = math.hypot(vx, vy)
            th = math.atan2(vy, vx) - ax
            th = (th + math.pi) % (2 * math.pi) - math.pi
            dth = abs(th)
        if r < 1e-12:
            tot += ha * 1.0
            continue
        if max_range_m is not None and r > max_range_m:
            continue
        if dth < ha:
            tot += r * (ha - dth)
    return tot / len(pts)


def sector_polygon(origin: Tuple[float, float], axis_deg: float,
                   half_angle_deg: float, radius: float,
                   bidirectional: bool = True, steps: int = 12
                   ) -> List[List[Tuple[float, float]]]:
    """도면 표시용 부채꼴 폴리곤(들). 평가에는 쓰지 않는다."""
    ox, oy = origin
    ha = math.radians(half_angle_deg)
    out = []
    axes = [math.radians(axis_deg)]
    if bidirectional:
        axes.append(math.radians(axis_deg) + math.pi)
    for ax in axes:
        pts = [(ox, oy)]
        for k in range(steps + 1):
            th = ax - ha + 2 * ha * k / steps
            pts.append((ox + radius * math.cos(th), oy + radius * math.sin(th)))
        out.append(pts)
    return out


# ---------------------------------------------------------------------------
# 직교 이동 경로(corridor) — 중량물 하중경로
# ---------------------------------------------------------------------------

def l_corridors(p: Tuple[float, float], q: Tuple[float, float],
                width: float) -> List[List[Rect]]:
    """
    p -> q 의 L자 직교 경로 두 가지(먼저 x, 먼저 y)를 폭 width 의
    사각형 2개짜리 통로로 반환. 크레인은 교대(bridge)+트롤리 2축이므로
    직교 경로가 실제 하중 이동 경로에 대응한다.
    """
    (px, py), (qx, qy) = p, q
    h = width / 2.0
    def hseg(x0, x1, y):
        lo, hi = (x0, x1) if x0 <= x1 else (x1, x0)
        return (lo - h, y - h, (hi - lo) + width, width)
    def vseg(y0, y1, x):
        lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)
        return (x - h, lo - h, width, (hi - lo) + width)
    return [
        [hseg(px, qx, py), vseg(py, qy, qx)],   # x 먼저
        [vseg(py, qy, px), hseg(px, qx, qy)],   # y 먼저
    ]
