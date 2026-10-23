# -*- coding: utf-8 -*-
"""
importers/preview.py — Takeoff 미리보기 SVG. 파서가 '무엇을 무엇으로 읽었는지' 사람이 확인하는 용도.

Gate 1 이전에 사람이 봐야 하는 것: 축척이 맞는가, 기기/실/장애물 분류가 맞는가,
빠진 것·잘못 합쳐진 것은 없는가. 이 그림이 그 확인 수단이다.
"""
from __future__ import annotations

from typing import List

KIND_STYLE = {
    "equipment": ("#93c5fd", "#1e3a8a"),
    "room": ("#fde68a", "#78350f"),
    "obstacle": ("#4b5563", "#111827"),
    "void": ("#f3f4f6", "#9ca3af"),
    "opening": ("#fca5a5", "#7f1d1d"),
}
KIND_KO = {"equipment": "기기", "room": "실", "obstacle": "고정 장애물",
           "void": "배치 금지", "opening": "해치·계단"}


def _esc(t) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def takeoff_svg(tk: dict, floor: int = 0, width_px: int = 900) -> str:
    items = [it for it in tk["items"] if it["floor"] == floor]
    bnd = [b["rect"] for b in tk["boundary"] if b["floor"] == floor]
    xs, ys = [0.0], [0.0]
    for it in items:
        xs += [it["x"], it["x"] + it["w"]]; ys += [it["y"], it["y"] + it["d"]]
    for r in bnd:
        xs += [r[0], r[2]]; ys += [r[1], r[3]]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    W, D = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
    m = 40
    k = (width_px - 2 * m) / W
    Hpx = D * k + 2 * m + 70

    def X(x): return m + (x - x0) * k
    def Y(y): return m + (y1 - y) * k

    s: List[str] = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width_px} {Hpx:.0f}" '
                    f'font-family="Noto Sans KR, Malgun Gothic, sans-serif">',
                    '<rect width="100%" height="100%" fill="#ffffff"/>']
    for r in bnd:
        s.append(f'<rect x="{X(r[0]):.1f}" y="{Y(r[3]):.1f}" width="{(r[2]-r[0])*k:.1f}" '
                 f'height="{(r[3]-r[1])*k:.1f}" fill="none" stroke="#111827" stroke-width="2.5"/>')
    order = sorted(items, key=lambda it: -(it["w"] * it["d"]))     # 큰 것부터 (작은 것이 위에)
    for it in order:
        fill, stroke = KIND_STYLE.get(it["kind"], ("#e5e7eb", "#374151"))
        if it.get("polygon"):
            pts = " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in it["polygon"])
            s.append(f'<polygon points="{pts}" fill="{fill}" fill-opacity="0.75" '
                     f'stroke="{stroke}" stroke-width="1.2"/>')
            s.append(f'<rect x="{X(it["x"]):.1f}" y="{Y(it["y"]+it["d"]):.1f}" '
                     f'width="{it["w"]*k:.1f}" height="{it["d"]*k:.1f}" fill="none" '
                     f'stroke="{stroke}" stroke-width="0.8" stroke-dasharray="3 3"/>')
        else:
            s.append(f'<rect x="{X(it["x"]):.1f}" y="{Y(it["y"]+it["d"]):.1f}" '
                     f'width="{it["w"]*k:.1f}" height="{it["d"]*k:.1f}" fill="{fill}" '
                     f'fill-opacity="0.75" stroke="{stroke}" stroke-width="1.2"/>')
        if min(it["w"], it["d"]) * k > 16:
            fs = max(7, min(12, min(it["w"], it["d"]) * k / 4))
            col = "#ffffff" if it["kind"] == "obstacle" else "#111827"
            s.append(f'<text x="{X(it["x"]+it["w"]/2):.1f}" y="{Y(it["y"]+it["d"]/2)+fs/3:.1f}" '
                     f'font-size="{fs:.1f}" text-anchor="middle" fill="{col}">{_esc(it["id"])}</text>')
    for t in tk.get("texts", []):
        if t["floor"] == floor:
            s.append(f'<text x="{X(t["x"]):.1f}" y="{Y(t["y"]):.1f}" font-size="8" '
                     f'fill="#9ca3af">{_esc(t["text"][:24])}</text>')
    ly = D * k + 2 * m + 20
    lx = m
    for kind, (fill, stroke) in KIND_STYLE.items():
        n = sum(1 for it in items if it["kind"] == kind)
        s.append(f'<rect x="{lx}" y="{ly:.0f}" width="14" height="12" fill="{fill}" stroke="{stroke}"/>')
        s.append(f'<text x="{lx+20}" y="{ly+11:.0f}" font-size="12">{KIND_KO[kind]} {n}</text>')
        lx += 130
    s.append(f'<text x="{m}" y="{ly+34:.0f}" font-size="11" fill="#6b7280">'
             f'{_esc(tk["source"]["filename"])} · {floor+1}층 · {W:.1f} × {D:.1f} m · '
             f'축척: {_esc(tk.get("scale_basis", ""))[:90]}</text>')
    s.append("</svg>")
    return "\n".join(s)
