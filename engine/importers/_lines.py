# -*- coding: utf-8 -*-
"""
importers/_lines.py — 형식별 파서가 공유하는 2D 기하 보조 (결정론적, 표준 라이브러리만)

  - 2D 아핀 변환 (SVG 행렬 규약 a b c d e f)
  - 원/타원/원호/베지에 → 결정론적 다각형 근사
  - 낱개 선분(LINE, 열린 경로)에서 축정렬 직사각형 닫힌 고리 찾기

선분 고리 찾기는 '최소 직사각형' 만 찾는다: 각 좌하단 후보점에서 오른쪽·위로
선이 이어지는 가장 가까운 꼭짓점을 골라 네 변이 모두 선으로 덮이는지 본다.
격자형으로 그린 실(벽을 공유하는 칸)도 칸 단위로 나온다. 사선·곡선 고리는 찾지 않는다.
"""
from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

Affine = Tuple[float, float, float, float, float, float]
IDENT: Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# 원·타원 근사 꼭짓점 수. 0°,90°,180°,270° 에 꼭짓점이 오도록 4 의 배수.
CIRCLE_SEGS = 16
CURVE_STEPS = 8


# ---------------------------------------------------------------------------
# 아핀 변환
# ---------------------------------------------------------------------------

def mul(m: Affine, n: Affine) -> Affine:
    """m ∘ n (n 을 먼저 적용)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B,
            a * C + c * D, b * C + d * D,
            a * E + c * F + e, b * E + d * F + f)


def apply(m: Affine, x: float, y: float) -> Tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def apply_pts(m: Affine, pts: Sequence[Sequence[float]]) -> List[Tuple[float, float]]:
    return [apply(m, p[0], p[1]) for p in pts]


def translate(tx: float, ty: float) -> Affine:
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


def scale(sx: float, sy: float) -> Affine:
    return (sx, 0.0, 0.0, sy, 0.0, 0.0)


def rotate_deg(deg: float) -> Affine:
    c, s = _cos_sin(deg)
    return (c, s, -s, c, 0.0, 0.0)


def _cos_sin(deg: float) -> Tuple[float, float]:
    """90° 배수에서 정확히 0/±1 이 나오게 (부동소수 찌꺼기 제거 → 결정론·정확도)."""
    r = deg % 360.0
    for k, cs in ((0.0, (1.0, 0.0)), (90.0, (0.0, 1.0)), (180.0, (-1.0, 0.0)),
                  (270.0, (0.0, -1.0)), (360.0, (1.0, 0.0))):
        if abs(r - k) < 1e-9:
            return cs
    t = math.radians(r)
    return math.cos(t), math.sin(t)


# ---------------------------------------------------------------------------
# 곡선 근사
# ---------------------------------------------------------------------------

def circle_pts(cx: float, cy: float, rx: float, ry: float = None,
               rot_deg: float = 0.0, n: int = CIRCLE_SEGS) -> List[Tuple[float, float]]:
    ry = rx if ry is None else ry
    cr, sr = _cos_sin(rot_deg)
    out = []
    for k in range(n):
        c, s = _cos_sin(360.0 * k / n)
        x, y = rx * c, ry * s
        out.append((cx + x * cr - y * sr, cy + x * sr + y * cr))
    return out


def arc_pts(cx: float, cy: float, r: float, a0_deg: float, a1_deg: float,
            ccw: bool = True, steps: int = CURVE_STEPS) -> List[Tuple[float, float]]:
    """원호 위 점 (양 끝 포함)."""
    if ccw:
        while a1_deg <= a0_deg:
            a1_deg += 360.0
    else:
        while a1_deg >= a0_deg:
            a1_deg -= 360.0
    out = []
    for k in range(steps + 1):
        c, s = _cos_sin(a0_deg + (a1_deg - a0_deg) * k / steps)
        out.append((cx + r * c, cy + r * s))
    return out


def bulge_pts(p0: Sequence[float], p1: Sequence[float], bulge: float,
              steps: int = CURVE_STEPS) -> List[Tuple[float, float]]:
    """DXF bulge 원호 p0→p1 의 중간 점들 (p0, p1 제외)."""
    if abs(bulge) < 1e-12:
        return []
    x0, y0 = p0[0], p0[1]
    x1, y1 = p1[0], p1[1]
    chord = math.hypot(x1 - x0, y1 - y0)
    if chord < 1e-12:
        return []
    theta = 4.0 * math.atan(bulge)                 # 중심각 (부호 = 방향)
    r = chord / (2.0 * math.sin(abs(theta) / 2.0))
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    # 중심: 현의 수직이등분선 위
    h = r * math.cos(abs(theta) / 2.0)
    ux, uy = (x1 - x0) / chord, (y1 - y0) / chord
    sgn = 1.0 if (bulge > 0) == (abs(theta) < math.pi) else -1.0
    cx, cy = mx - uy * h * sgn, my + ux * h * sgn
    a0 = math.atan2(y0 - cy, x0 - cx)
    out = []
    for k in range(1, steps):
        a = a0 + theta * k / steps
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def cubic_pts(p0, p1, p2, p3, steps: int = CURVE_STEPS) -> List[Tuple[float, float]]:
    """3차 베지에의 점들 (p0 제외, p3 포함)."""
    out = []
    for k in range(1, steps + 1):
        t = k / steps
        u = 1.0 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def quad_pts(p0, p1, p2, steps: int = CURVE_STEPS) -> List[Tuple[float, float]]:
    out = []
    for k in range(1, steps + 1):
        t = k / steps
        u = 1.0 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def svg_arc_pts(x0, y0, rx, ry, phi_deg, large, sweep, x1, y1,
                steps: int = CURVE_STEPS) -> List[Tuple[float, float]]:
    """SVG 타원호 (끝점 표기) → 점들 (시작점 제외, 끝점 포함). SVG 1.1 부록 F.6."""
    if abs(x0 - x1) < 1e-12 and abs(y0 - y1) < 1e-12:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx < 1e-12 or ry < 1e-12:
        return [(x1, y1)]
    cp, sp = _cos_sin(phi_deg)
    dx, dy = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1.0:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den > 0 else 0.0
    if bool(large) == bool(sweep):
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x0 + x1) / 2.0
    cy = sp * cxp + cp * cyp + (y0 + y1) / 2.0

    def ang(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    t1 = ang(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    out = []
    for k in range(1, steps + 1):
        t = t1 + dt * k / steps
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        out.append((cp * ex - sp * ey + cx, sp * ex + cp * ey + cy))
    out[-1] = (x1, y1)
    return out


# ---------------------------------------------------------------------------
# 닫힘 판정
# ---------------------------------------------------------------------------

def close_if_coincident(pts: List[Tuple[float, float]], tol: float) -> Tuple[List, bool]:
    """첫 점과 끝 점이 tol 안에서 같으면 끝 점을 빼고 닫힌 것으로 본다."""
    if len(pts) >= 4 and abs(pts[0][0] - pts[-1][0]) <= tol and abs(pts[0][1] - pts[-1][1]) <= tol:
        return list(pts[:-1]), True
    return list(pts), False


def dedupe_closed(pts: Sequence[Sequence[float]], tol: float = 0.0) -> List[Tuple[float, float]]:
    """연속 중복점 제거, 닫힌 다각형이면 끝=처음 중복도 제거."""
    out: List[Tuple[float, float]] = []
    for p in pts:
        q = (float(p[0]), float(p[1]))
        if out and abs(out[-1][0] - q[0]) <= tol and abs(out[-1][1] - q[1]) <= tol:
            continue
        out.append(q)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) <= tol and abs(out[0][1] - out[-1][1]) <= tol:
        out.pop()
    return out


# ---------------------------------------------------------------------------
# 선분 → 직사각형 고리
# ---------------------------------------------------------------------------

MAX_SEGMENTS = 20000


def _merge(intervals: List[Tuple[float, float]], tol: float) -> List[Tuple[float, float]]:
    intervals = sorted(intervals)
    out: List[List[float]] = []
    for a, b in intervals:
        if out and a <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _covered(ivs: List[Tuple[float, float]], a: float, b: float, tol: float) -> bool:
    for lo, hi in ivs:
        if lo - tol <= a and b <= hi + tol:
            return True
        if lo > a + tol:
            break
    return False


def rects_from_segments(segs: Sequence[Tuple[float, float, float, float]], tol: float
                        ) -> Tuple[List[Tuple[float, float, float, float]], Dict[str, int]]:
    """
    선분 [(x0,y0,x1,y1)] → 최소 축정렬 직사각형 [(x0,y0,x1,y1)] (정렬된 목록).
    끝점은 tol 격자로 스냅한다. 사선은 무시하고 개수만 센다.
    """
    info = {"segments": len(segs), "diagonal": 0, "rects": 0, "truncated": 0}
    if len(segs) > MAX_SEGMENTS:
        info["truncated"] = len(segs) - MAX_SEGMENTS
        segs = list(segs)[:MAX_SEGMENTS]
    q = tol if tol > 0 else 1e-9

    def snap(v: float) -> float:
        return round(v / q) * q

    hor: Dict[float, List[Tuple[float, float]]] = {}
    ver: Dict[float, List[Tuple[float, float]]] = {}
    nodes = set()
    for x0, y0, x1, y1 in segs:
        x0, y0, x1, y1 = snap(x0), snap(y0), snap(x1), snap(y1)
        if abs(y0 - y1) <= q / 2 and abs(x0 - x1) > q / 2:
            a, b = min(x0, x1), max(x0, x1)
            hor.setdefault(y0, []).append((a, b))
        elif abs(x0 - x1) <= q / 2 and abs(y0 - y1) > q / 2:
            a, b = min(y0, y1), max(y0, y1)
            ver.setdefault(x0, []).append((a, b))
        else:
            if not (abs(x0 - x1) <= q / 2 and abs(y0 - y1) <= q / 2):
                info["diagonal"] += 1
            continue
        nodes.add((x0, y0)); nodes.add((x1, y1))
    hor = {k: _merge(v, q / 2) for k, v in hor.items()}
    ver = {k: _merge(v, q / 2) for k, v in ver.items()}
    # T 교차점도 꼭짓점 후보로 (벽을 공유하는 격자 칸)
    for y, ivs in sorted(hor.items()):
        for x, vivs in sorted(ver.items()):
            if _covered(ivs, x, x, q / 2) and _covered(vivs, y, y, q / 2):
                nodes.add((x, y))
    xs = sorted({n[0] for n in nodes})
    ys = sorted({n[1] for n in nodes})
    found = set()
    eps = q / 2
    for (x0, y0) in sorted(nodes):
        h0 = hor.get(y0, [])
        v0 = ver.get(x0, [])
        if not h0 or not v0:
            continue
        done = False
        for x1 in xs:
            if x1 <= x0 + eps:
                continue
            if not _covered(h0, x0, x1, eps):
                break
            v1 = ver.get(x1, [])
            if not v1 or not _covered(v1, y0, y0 + 2 * eps, eps):
                continue
            for y1 in ys:
                if y1 <= y0 + eps:
                    continue
                if not _covered(v0, y0, y1, eps):
                    break
                if not _covered(v1, y0, y1, eps):
                    break
                if _covered(hor.get(y1, []), x0, x1, eps):
                    found.add((x0, y0, x1, y1))
                    done = True
                    break
            if done:
                break
    rects = sorted(found)
    info["rects"] = len(rects)
    return rects, info


def rect_pts(r: Sequence[float]) -> List[Tuple[float, float]]:
    x0, y0, x1, y1 = r
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
