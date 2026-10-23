# -*- coding: utf-8 -*-
"""
drawing.py — 최적화 결과를 '설계 도면' 으로 출력.

출력물
  1) SVG  : 층별 평면도 (격자, 치수선, 실명/기기명, 범례, 축척, 표제란)
             + 화재구역 경계  + 터빈 미사일 금지 부채꼴
             + ★ hard 제약 위반 지점 빨간색 표시
  2) DXF  : AutoCAD R12 형식, 층별 레이어 — CAD 에서 열어 편집 가능
  3) JSON : 좌표/치수 기계판독 결과 (BIM/IFC 변환의 입력)

★ 위반 표시가 왜 필요한가: 프로토타입에서 최적해가 크레인 베이를 1.9 m 벗어났고,
  최종해에도 방사선관리구역 출입통제가 외벽에서 0.10 m 떠 있었다. 둘 다 도면상
  정상으로 보인다. 도면을 눈으로 봐서는 hard 제약 위반을 알 수 없다.

격자 기본값은 App. R III.G.2 의 20 ft = 6.096 m 다 — 이것이 GA 의 실질 그리드 모듈.
외부 의존성 없음 (문자열로 직접 생성).
"""
from __future__ import annotations
import json
import math
from typing import Dict, List, Optional, Sequence, Tuple

from geometry import sector_polygon

ZONE_COLORS = {
    "RCA": "#f4b6b6", "R3": "#e8746e", "R2": "#f2a65a", "R1": "#f6d365",
    "NR": "#cfe8cf", "TrainA": "#9ec5fe", "TrainB": "#b6e3c6",
    "BOP": "#d9d2e9", "TB": "#cfe2f3", "AUX": "#ead1dc", "CTRL": "#fff2cc",
}
DEFAULT_FILL = "#e6e6e6"
VIOLATION_RED = "#e11d48"
FIRE_LINE = "#b45309"
MISSILE_FILL = "#7c3aed"


def _fill(zone):
    return ZONE_COLORS.get(zone, DEFAULT_FILL)


def _esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _elev(spec, floor):
    fh = spec.floor_heights or [5.0] * spec.n_floors
    return sum(fh[:floor])


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def to_svg(spec, geo, env, floor: int, path: str,
           px_per_m: float = None, margin: int = 120, grid: float = None,
           title_extra: str = "", shadows: Dict = None,
           violations: Sequence = (), turbines: Sequence[dict] = ()):
    """
    한 층의 평면도를 SVG 로 쓴다.

      shadows    : {(block_idx, floor): rec}  다층 관통 기기의 상위층 점유
      violations : constraints.Violation 목록 — marker 가 있는 것만 표시
      turbines   : [{"x":, "y":, "azimuth_deg":, "half_angle_deg":, "label":}]
    """
    W, D, H = env
    if px_per_m is None:
        # 작은 평면도(10 m)도 큰 플랜트(150 m)도 읽을 수 있는 크기로
        px_per_m = max(3.0, min(40.0, 900.0 / max(W, D, 1e-6)))
    grid = grid if grid is not None else getattr(spec, "grid_m", 6.096)
    shadows = shadows or {}
    ids = [i for i in geo if geo[i]["f"] == floor]
    sw, sh = W * px_per_m, D * px_per_m
    legend_w = 330
    total_w = sw + 2 * margin + legend_w
    total_h = sh + 2 * margin + 138

    def X(x): return margin + x * px_per_m
    def Y(y): return margin + (D - y) * px_per_m          # y축 상향 반전

    s: List[str] = []
    s.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" '
             f'height="{total_h:.0f}" viewBox="0 0 {total_w:.0f} {total_h:.0f}" '
             f'font-family="IBM Plex Sans KR, Malgun Gothic, Noto Sans KR, sans-serif">')
    s.append('<defs><marker id="ar" markerWidth="8" markerHeight="8" refX="4" refY="4" '
             'orient="auto"><path d="M0,4 L8,1.5 L8,6.5 z" fill="#111827"/></marker>'
             '<pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
             'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" '
             'stroke="#6b7280" stroke-width="1.4"/></pattern>'
             '<pattern id="xhatch" width="8" height="8" patternUnits="userSpaceOnUse">'
             '<path d="M0,0 L8,8 M8,0 L0,8" stroke="#d1d5db" stroke-width="1"/></pattern></defs>')
    s.append('<rect width="100%" height="100%" fill="#ffffff"/>')

    # ---- 격자 (기본 모듈 = App. R 6.096 m) ----
    s.append('<g stroke="#e9edf2" stroke-width="1">')
    n = 0
    while n * grid <= W + 1e-9:
        s.append(f'<line x1="{X(n*grid):.1f}" y1="{Y(0):.1f}" '
                 f'x2="{X(n*grid):.1f}" y2="{Y(D):.1f}"/>')
        n += 1
    n = 0
    while n * grid <= D + 1e-9:
        s.append(f'<line x1="{X(0):.1f}" y1="{Y(n*grid):.1f}" '
                 f'x2="{X(W):.1f}" y2="{Y(n*grid):.1f}"/>')
        n += 1
    s.append('</g>')

    # ---- 통심선 라벨 ----
    s.append('<g font-size="10" fill="#9aa5b1" text-anchor="middle">')
    n = 0
    while n * grid <= W + 1e-9:
        s.append(f'<text x="{X(n*grid):.1f}" y="{Y(D)-8:.1f}">{chr(65+n%26)}</text>')
        n += 1
    n = 0
    while n * grid <= D + 1e-9:
        s.append(f'<text x="{X(0)-16:.1f}" y="{Y(n*grid)+4:.1f}">{n+1}</text>')
        n += 1
    s.append('</g>')

    # ---- 외피 ----
    s.append(f'<rect x="{X(0):.1f}" y="{Y(D):.1f}" width="{sw:.1f}" height="{sh:.1f}" '
             f'fill="none" stroke="#111827" stroke-width="3"/>')

    # ---- 화재구역 경계 ----
    fz = _fire_zone_boxes(spec, geo, shadows, floor)
    if fz:
        s.append(f'<g fill="none" stroke="{FIRE_LINE}" stroke-width="2" '
                 f'stroke-dasharray="10 4 2 4">')
        for label, (x, y, w, d) in sorted(fz.items()):
            s.append(f'<rect x="{X(x-0.6):.1f}" y="{Y(y+d+0.6):.1f}" '
                     f'width="{(w+1.2)*px_per_m:.1f}" height="{(d+1.2)*px_per_m:.1f}"/>')
        s.append('</g>')
        s.append(f'<g font-size="11" font-weight="700" fill="{FIRE_LINE}">')
        for label, (x, y, w, d) in sorted(fz.items()):
            s.append(f'<text x="{X(x-0.4):.1f}" y="{Y(y+d+0.9):.1f}">'
                     f'FZ {_esc(label)}</text>')
        s.append('</g>')

    # ---- 상위층 관통 그림자 (이 층을 지나가는 다층 기기) ----
    for (i, f), g in sorted(shadows.items()):
        if f != floor:
            continue
        s.append(f'<rect x="{X(g["x"]):.1f}" y="{Y(g["y"]+g["d"]):.1f}" '
                 f'width="{g["w"]*px_per_m:.1f}" height="{g["d"]*px_per_m:.1f}" '
                 f'fill="url(#hatch)" fill-opacity="0.35" stroke="#6b7280" '
                 f'stroke-width="1.2" stroke-dasharray="6 3"/>')
        s.append(f'<text x="{X(g["x"]+g["w"]/2):.1f}" y="{Y(g["y"]+g["d"]/2):.1f}" '
                 f'font-size="9.5" text-anchor="middle" fill="#4b5563">'
                 f'{_esc(spec.blocks[i].id)} (관통)</text>')

    # ---- 블록 ----
    for i in ids:
        g = geo[i]
        b = spec.blocks[i]
        cl = b.clearance
        x, y, w, d = g["x"], g["y"], g["w"], g["d"]
        if getattr(b, "kind", "") == "void":
            s.append(f'<rect x="{X(x):.1f}" y="{Y(y+d):.1f}" width="{w*px_per_m:.1f}" '
                     f'height="{d*px_per_m:.1f}" fill="url(#xhatch)" stroke="#9ca3af" '
                     f'stroke-width="1" stroke-dasharray="3 3"/>')
            continue
        if getattr(b, "pinned", False):
            s.append(f'<rect x="{X(x):.1f}" y="{Y(y+d):.1f}" width="{w*px_per_m:.1f}" '
                     f'height="{d*px_per_m:.1f}" fill="#4b5563" fill-opacity="0.85" '
                     f'stroke="#111827" stroke-width="1.6"/>')
            if min(w, d) * px_per_m > 18:
                fs = max(7.0, min(11, min(w, d) * px_per_m / 5))
                s.append(f'<text x="{X(x+w/2):.1f}" y="{Y(y+d/2)+fs/3:.1f}" '
                         f'font-size="{fs:.1f}" text-anchor="middle" fill="#ffffff">'
                         f'{_esc(b.id)}</text>')
            continue
        if cl > 0:
            s.append(f'<rect x="{X(x):.1f}" y="{Y(y+d):.1f}" width="{w*px_per_m:.1f}" '
                     f'height="{d*px_per_m:.1f}" fill="{_fill(b.zone)}" '
                     f'fill-opacity="0.35" stroke="#8a94a6" stroke-width="1" '
                     f'stroke-dasharray="4 3"/>')
        s.append(f'<rect x="{X(x+cl):.1f}" y="{Y(y+d-cl):.1f}" '
                 f'width="{(w-2*cl)*px_per_m:.1f}" height="{(d-2*cl)*px_per_m:.1f}" '
                 f'fill="{_fill(b.zone)}" stroke="#1f2937" stroke-width="1.6"/>')
        cx, cy = X(x + w / 2), Y(y + d / 2)
        fs = max(7.5, min(13, min(w, d) * px_per_m / 6.5))
        s.append(f'<text x="{cx:.1f}" y="{cy-2:.1f}" font-size="{fs:.1f}" '
                 f'font-weight="600" text-anchor="middle" fill="#111827">'
                 f'{_esc(b.id)}</text>')
        if min(w, d) * px_per_m > 42:
            s.append(f'<text x="{cx:.1f}" y="{cy+fs:.1f}" font-size="{fs*0.82:.1f}" '
                     f'text-anchor="middle" fill="#4b5563">{_esc(b.name)}</text>')
            s.append(f'<text x="{cx:.1f}" y="{cy+2.05*fs:.1f}" font-size="{fs*0.72:.1f}" '
                     f'text-anchor="middle" fill="#6b7280">{b.w:.1f}×{b.d:.1f}m</text>')

    # ---- 연결선 (동일 층만) ----
    idx = {b.id: k for k, b in enumerate(spec.blocks)}
    s.append('<g stroke="#2563eb" stroke-opacity="0.35" stroke-width="1.2" fill="none">')
    for lk in spec.links:
        i, j = idx.get(lk.a), idx.get(lk.b)
        if i is None or j is None or i not in geo or j not in geo:
            continue
        if geo[i]["f"] != floor or geo[j]["f"] != floor:
            continue
        a, b_ = geo[i], geo[j]
        s.append(f'<path d="M {X(a["cx"]):.1f} {Y(a["cy"]):.1f} '
                 f'L {X(b_["cx"]):.1f} {Y(a["cy"]):.1f} '
                 f'L {X(b_["cx"]):.1f} {Y(b_["cy"]):.1f}"/>')
    s.append('</g>')

    # ---- 터빈 미사일 금지 부채꼴 ----
    for t in turbines or ():
        R = math.hypot(W, D) * 1.2
        polys = sector_polygon((t["x"], t["y"]), t.get("azimuth_deg", 0.0),
                               t.get("half_angle_deg", 25.0), R)
        for poly in polys:
            pts = " ".join(f"{X(px):.1f},{Y(py):.1f}" for px, py in poly)
            s.append(f'<polygon points="{pts}" fill="{MISSILE_FILL}" '
                     f'fill-opacity="0.10" stroke="{MISSILE_FILL}" '
                     f'stroke-width="1.2" stroke-dasharray="7 4"/>')
        s.append(f'<text x="{X(t["x"]):.1f}" y="{Y(t["y"])-6:.1f}" font-size="10.5" '
                 f'text-anchor="middle" fill="{MISSILE_FILL}" font-weight="700">'
                 f'{_esc(t.get("label","TURBINE"))} ±{t.get("half_angle_deg",25):g}° '
                 f'@ {t.get("azimuth_deg",0):g}°</text>')

    # ---- ★ hard 제약 위반 표시 ----
    nv = _draw_violations(s, violations, floor, X, Y, px_per_m)

    # ---- 치수선 ----
    s.append('<g stroke="#111827" stroke-width="1" fill="#111827" font-size="12">')
    yy = Y(0) + 34
    s.append(f'<line x1="{X(0):.1f}" y1="{yy:.1f}" x2="{X(W):.1f}" y2="{yy:.1f}" '
             f'marker-start="url(#ar)" marker-end="url(#ar)"/>')
    s.append(f'<text x="{X(W/2):.1f}" y="{yy-6:.1f}" text-anchor="middle">{W:,.1f} m</text>')
    xx = X(W) + 26
    s.append(f'<line x1="{xx:.1f}" y1="{Y(0):.1f}" x2="{xx:.1f}" y2="{Y(D):.1f}" '
             f'marker-start="url(#ar)" marker-end="url(#ar)"/>')
    s.append(f'<text x="{xx+6:.1f}" y="{Y(D/2):.1f}" writing-mode="tb">{D:,.1f} m</text>')
    s.append('</g>')

    # ---- 범례 ----
    lx, ly = margin + sw + 62, margin
    s.append(f'<text x="{lx}" y="{ly-24}" font-size="14" font-weight="700">'
             f'범례 / LEGEND</text>')
    zones = sorted({spec.blocks[i].zone for i in ids if spec.blocks[i].zone})
    k = 0
    for z in zones:
        yb = ly + k * 22
        s.append(f'<rect x="{lx}" y="{yb}" width="18" height="14" fill="{_fill(z)}" '
                 f'stroke="#1f2937"/>')
        s.append(f'<text x="{lx+26}" y="{yb+12}" font-size="12">{_esc(z)}</text>')
        k += 1
    yb = ly + k * 22 + 12
    for label, style in (
        ("유지보수 이격 (clearance)",
         f'fill="none" stroke="#8a94a6" stroke-dasharray="4 3"'),
        ("다층 관통 기기 (이 층 통과)",
         f'fill="url(#hatch)" stroke="#6b7280" stroke-dasharray="6 3"'),
        ("화재구역 경계 (FZ)",
         f'fill="none" stroke="{FIRE_LINE}" stroke-dasharray="10 4 2 4"'),
        ("터빈 미사일 금지 부채꼴",
         f'fill="{MISSILE_FILL}" fill-opacity="0.12" stroke="{MISSILE_FILL}" '
         f'stroke-dasharray="7 4"'),
        ("고정 장애물 (pinned)", 'fill="#4b5563" stroke="#111827"'),
        ("배치 금지 영역 (void)", 'fill="url(#xhatch)" stroke="#9ca3af" stroke-dasharray="3 3"'),
        ("★ hard 제약 위반",
         f'fill="none" stroke="{VIOLATION_RED}" stroke-width="2.5"'),
    ):
        s.append(f'<rect x="{lx}" y="{yb}" width="18" height="14" {style}/>')
        s.append(f'<text x="{lx+26}" y="{yb+12}" font-size="12">{_esc(label)}</text>')
        yb += 22
    s.append(f'<line x1="{lx}" y1="{yb+8}" x2="{lx+18}" y2="{yb+8}" stroke="#2563eb" '
             f'stroke-opacity="0.6" stroke-width="1.4"/>')
    s.append(f'<text x="{lx+26}" y="{yb+12}" font-size="12">배관·케이블 연결</text>')

    # ---- 표제란 ----
    ty = margin + sh + 74
    s.append(f'<line x1="{margin}" y1="{ty-26}" x2="{total_w-40}" y2="{ty-26}" '
             f'stroke="#111827" stroke-width="1.5"/>')
    proj = str(spec.project)
    proj = proj.split("(")[0].strip() if len(proj) > 46 else proj
    s.append(f'<text x="{margin}" y="{ty-4}" font-size="16" font-weight="700">'
             f'{_esc(proj)} — GENERAL ARRANGEMENT</text>')
    s.append(f'<text x="{margin}" y="{ty+14}" font-size="12.5" fill="#374151">'
             f'PLAN AT EL. {_elev(spec, floor):+.2f} m  '
             f'(FLOOR {floor+1} OF {spec.n_floors}) · GRID {grid:g} m</text>')
    s.append(f'<text x="{margin}" y="{ty+34}" font-size="11.5" fill="#4b5563">'
             f'ENVELOPE {W:,.1f} × {D:,.1f} m · FOOTPRINT {W*D:,.0f} m² · '
             f'BLDG VOLUME {W*D*H:,.0f} m³ · {px_per_m:g} px/m · '
             f'{_esc(title_extra)}</text>')
    if nv:
        s.append(f'<text x="{margin}" y="{ty+54}" font-size="12.5" '
                 f'font-weight="700" fill="{VIOLATION_RED}">'
                 f'★ HARD CONSTRAINT VIOLATIONS ON THIS PLAN: {nv} — '
                 f'NOT AN ACCEPTABLE SOLUTION</text>')
    else:
        s.append(f'<text x="{margin}" y="{ty+54}" font-size="10.5" fill="#9aa5b1">'
                 f'AUTO-GENERATED BY SEQUENCE-PAIR + METAHEURISTIC SEARCH · '
                 f'NOT FOR CONSTRUCTION — CONCEPT STUDY ONLY</text>')
    s.append('</svg>')

    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(s))
    return path


def _fire_zone_boxes(spec, geo, shadows, floor) -> Dict[str, Tuple[float, float, float, float]]:
    box: Dict[str, List[float]] = {}
    def add(z, x, y, w, d):
        if not z:
            return
        if z not in box:
            box[z] = [x, y, x + w, y + d]
        else:
            b = box[z]
            b[0], b[1] = min(b[0], x), min(b[1], y)
            b[2], b[3] = max(b[2], x + w), max(b[3], y + d)
    for i, g in geo.items():
        if g["f"] == floor:
            add(spec.blocks[i].fire_zone, g["x"], g["y"], g["w"], g["d"])
    for (i, f), g in shadows.items():
        if f == floor:
            add(spec.blocks[i].fire_zone, g["x"], g["y"], g["w"], g["d"])
    return {z: (b[0], b[1], b[2] - b[0], b[3] - b[1]) for z, b in box.items()}


def _draw_violations(s: List[str], violations, floor, X, Y, px_per_m) -> int:
    n = 0
    s.append(f'<g stroke="{VIOLATION_RED}" fill="none" stroke-width="2.5">')
    labels = []
    for v in violations or ():
        m = getattr(v, "marker", None)
        if not m or not getattr(v, "hard", True) or v.amount <= 1e-6:
            continue
        if m.get("floor") != floor:
            continue
        n += 1
        kind = m.get("kind")
        rects = []
        if kind in ("pair",):
            rects = [m["a"], m["b"]]
        elif kind in ("rect", "merge"):
            rects = [m["a"]]
        elif kind == "corridor":
            rects = list(m.get("segs", []))
        for r in rects:
            if not r:
                continue
            x, y, w, d = r
            s.append(f'<rect x="{X(x):.1f}" y="{Y(y+d):.1f}" '
                     f'width="{w*px_per_m:.1f}" height="{d*px_per_m:.1f}" '
                     f'stroke-dasharray="5 3"/>')
        if kind == "pair" and m.get("a") and m.get("b"):
            ax, ay, aw, ad = m["a"]
            bx, by, bw, bd = m["b"]
            s.append(f'<line x1="{X(ax+aw/2):.1f}" y1="{Y(ay+ad/2):.1f}" '
                     f'x2="{X(bx+bw/2):.1f}" y2="{Y(by+bd/2):.1f}" '
                     f'stroke-dasharray="3 3"/>')
            labels.append(((ax + aw / 2 + bx + bw / 2) / 2,
                           (ay + ad / 2 + by + bd / 2) / 2,
                           f"{v.cid} {v.amount:.2f}{v.unit}"))
        elif rects:
            x, y, w, d = rects[0]
            labels.append((x + w / 2, y + d, f"{v.cid} {v.amount:.2f}{v.unit}"))
    s.append('</g>')
    s.append(f'<g font-size="10.5" font-weight="700" fill="{VIOLATION_RED}" '
             f'text-anchor="middle">')
    for x, y, txt in labels:
        s.append(f'<text x="{X(x):.1f}" y="{Y(y)-4:.1f}">{_esc(txt)}</text>')
    s.append('</g>')
    return n


# ---------------------------------------------------------------------------
# DXF (R12 ASCII)
# ---------------------------------------------------------------------------

def _ascii(t):
    return "".join(ch if ord(ch) < 128 else "?" for ch in str(t))


def to_dxf(spec, geo, env, path: str, shadows: Dict = None, violations=(),
           turbines: Sequence[dict] = ()):
    W, D, H = env
    shadows = shadows or {}
    out: List[str] = []

    def g(code, val):
        out.append(str(code)); out.append(str(val))

    g(0, "SECTION"); g(2, "ENTITIES")

    def poly(x, y, w, d, layer):
        g(0, "LWPOLYLINE"); g(8, layer); g(100, "AcDbEntity")
        g(90, 4); g(70, 1)
        for px, py in [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]:
            g(10, f"{px:.4f}"); g(20, f"{py:.4f}")

    def text(x, y, h, t, layer):
        g(0, "TEXT"); g(8, layer)
        g(10, f"{x:.4f}"); g(20, f"{y:.4f}"); g(40, f"{h:.3f}"); g(1, t)

    for f in range(spec.n_floors):
        lay = f"FLOOR_{f+1:02d}"
        poly(0, 0, W, D, f"{lay}_ENVELOPE")
        for i, gg in geo.items():
            if gg["f"] != f:
                continue
            b = spec.blocks[i]
            cl = b.clearance
            if cl > 0:
                poly(gg["x"], gg["y"], gg["w"], gg["d"], f"{lay}_CLEARANCE")
            poly(gg["x"] + cl, gg["y"] + cl, gg["w"] - 2 * cl, gg["d"] - 2 * cl,
                 f"{lay}_EQUIPMENT")
            text(gg["x"] + cl + 0.3, gg["y"] + cl + 0.3, 1.0, _ascii(b.id),
                 f"{lay}_TEXT")
        for (i, ff), gg in shadows.items():
            if ff != f:
                continue
            poly(gg["x"], gg["y"], gg["w"], gg["d"], f"{lay}_PENETRATION")
        for z, bb in _fire_zone_boxes(spec, geo, shadows, f).items():
            poly(bb[0] - 0.6, bb[1] - 0.6, bb[2] + 1.2, bb[3] + 1.2,
                 f"{lay}_FIRE_ZONE")
            text(bb[0], bb[1] + bb[3] + 1.0, 1.2, _ascii(f"FZ {z}"),
                 f"{lay}_FIRE_ZONE")
        for v in violations or ():
            m = getattr(v, "marker", None)
            if not m or m.get("floor") != f or not getattr(v, "hard", True):
                continue
            for r in ([m.get("a"), m.get("b")] if m.get("kind") == "pair"
                      else ([m.get("a")] if m.get("kind") in ("rect", "merge")
                            else list(m.get("segs", [])))):
                if r:
                    poly(r[0], r[1], r[2], r[3], f"{lay}_VIOLATION")

    for t in turbines or ():
        R = math.hypot(W, D) * 1.2
        for poly_pts in sector_polygon((t["x"], t["y"]), t.get("azimuth_deg", 0.0),
                                       t.get("half_angle_deg", 25.0), R):
            g(0, "LWPOLYLINE"); g(8, "TURBINE_MISSILE_ZONE"); g(100, "AcDbEntity")
            g(90, len(poly_pts)); g(70, 1)
            for px, py in poly_pts:
                g(10, f"{px:.4f}"); g(20, f"{py:.4f}")

    g(0, "ENDSEC"); g(0, "EOF")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(out))
    return path


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def to_json(spec, geo, env, terms, F, path: str, shadows: Dict = None,
            violations=(), extra: dict = None):
    W, D, H = env
    shadows = shadows or {}
    data = dict(
        project=spec.project,
        not_for_construction=True,
        objective=dict(total=F, terms={k: v for k, v in sorted(terms.items())}),
        envelope=dict(width_m=W, depth_m=D, height_m=H,
                      footprint_m2=W * D, volume_m3=W * D * H),
        floors=[dict(index=f, elevation_m=_elev(spec, f),
                     height_m=(spec.floor_heights or [5.0] * spec.n_floors)[f])
                for f in range(spec.n_floors)],
        blocks=[dict(id=spec.blocks[i].id, name=spec.blocks[i].name,
                     zone=spec.blocks[i].zone, fire_zone=spec.blocks[i].fire_zone,
                     train=spec.blocks[i].train, floor=g["f"],
                     x=round(g["x"], 3), y=round(g["y"], 3), z=round(g["z"], 3),
                     w=round(g["w"], 3), d=round(g["d"], 3), h=spec.blocks[i].h,
                     clearance=spec.blocks[i].clearance,
                     penetrates_floors=sorted([f for (j, f) in shadows if j == i]))
                for i, g in sorted(geo.items(), key=lambda kv: (kv[1]["f"], kv[1]["x"]))],
        hard_violations=[dict(id=v.cid, detail=v.detail, amount=v.amount,
                              unit=v.unit, blocks=v.blocks)
                         for v in (violations or ()) if v.hard and v.amount > 1e-6],
    )
    if extra:
        data.update(extra)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    return path
