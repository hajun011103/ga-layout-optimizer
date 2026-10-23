# -*- coding: utf-8 -*-
"""
importers/svg.py — SVG → Takeoff (결정론적, 표준 라이브러리만)

읽는 것
  rect, polygon, polyline(끝점 일치 시 닫힘), path(M L H V Z C S Q T A, 상대/절대,
  암묵 반복 명령, 지수 표기), circle/ellipse(16각형), line(4개 직사각형 찾기),
  text/tspan, g(transform 중첩: matrix/translate/scale/rotate/skewX/skewY)

이름(종류·층 판정): 요소 자신의 id·class·inkscape:label·data-layer 를 먼저 보고,
  그다음 가까운 조상 g 부터 차례로 본다. 처음 분류가 되는 이름이 이긴다.
좌표: SVG 는 y 가 아래로 증가하므로 y' = (viewBox 아래끝) - y 로 뒤집어 y 상향으로 만든다.
단위: 루트 width/height 가 mm·cm·in·pt 이고 viewBox 가 있으면 사용자 단위 → m 를 계산한다.
  루트에 data-m-per-unit 이 있으면 그것을 쓴다. 둘 다 없으면 px_per_m / m_per_unit / units 필요.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from . import _lines as L
from .common import (ImportErrorUser, assign_texts, bbox, boundary_from_items, classify,
                     finalize, floor_from_name, make_item, new_takeoff, normalize_origin,
                     polygon_area, resolve_scale, unit_factor)
from .dxf import _area_warning, TEXT_LAYER_RX

SKIP_TAGS = {"defs", "clipPath", "mask", "symbol", "marker", "pattern", "metadata", "title",
             "desc", "style", "script", "linearGradient", "radialGradient", "filter",
             "namedview", "foreignObject"}
SHAPE_TAGS = {"rect", "polygon", "polyline", "path", "circle", "ellipse", "line"}
LEN_UNITS = {"mm": 0.001, "cm": 0.01, "in": 0.0254, "pt": 0.0254 / 72.0,
             "pc": 0.0254 / 6.0, "m": 1.0}
_NONE = "\x00none"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attrs(el) -> Dict[str, str]:
    """네임스페이스를 떼어낸 속성. inkscape:label 은 'label' 로."""
    out = {}
    for k in sorted(el.attrib):
        out[_local(k)] = el.attrib[k]
    return out


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _nums(s: Optional[str]) -> List[float]:
    return [float(x) for x in _NUM.findall(s or "")]


def _len(s: Optional[str], default: float = 0.0) -> float:
    if s is None:
        return default
    m = _NUM.match(s.strip())
    return float(m.group(0)) if m else default


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------

_TF = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")


def parse_transform(s: Optional[str]) -> L.Affine:
    m = L.IDENT
    if not s:
        return m
    for name, args in _TF.findall(s):
        a = _nums(args)
        if name == "matrix" and len(a) == 6:
            t = tuple(a)
        elif name == "translate" and a:
            t = L.translate(a[0], a[1] if len(a) > 1 else 0.0)
        elif name == "scale" and a:
            t = L.scale(a[0], a[1] if len(a) > 1 else a[0])
        elif name == "rotate" and a:
            t = L.rotate_deg(a[0])
            if len(a) >= 3:
                t = L.mul(L.translate(a[1], a[2]), L.mul(t, L.translate(-a[1], -a[2])))
        elif name == "skewX" and a:
            t = (1.0, 0.0, math.tan(math.radians(a[0])), 1.0, 0.0, 0.0)
        elif name == "skewY" and a:
            t = (1.0, math.tan(math.radians(a[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        m = L.mul(m, t)
    return m


# ---------------------------------------------------------------------------
# path
# ---------------------------------------------------------------------------

_CMD = set("MmLlHhVvZzCcSsQqTtAa")
_NARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "Z": 0, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}


class _Scan:
    def __init__(self, s: str):
        self.s = s; self.i = 0; self.n = len(s)

    def ws(self):
        while self.i < self.n and self.s[self.i] in " \t\r\n,":
            self.i += 1

    def peek_cmd(self) -> Optional[str]:
        self.ws()
        if self.i < self.n and self.s[self.i] in _CMD:
            return self.s[self.i]
        return None

    def num(self) -> Optional[float]:
        self.ws()
        m = _NUM.match(self.s, self.i)
        if not m:
            return None
        self.i = m.end()
        return float(m.group(0))

    def flag(self) -> Optional[float]:
        self.ws()
        if self.i < self.n and self.s[self.i] in "01":
            self.i += 1
            return float(self.s[self.i - 1])
        return None


def parse_path(d: str) -> List[Tuple[List[Tuple[float, float]], bool]]:
    """path d → [(점들, 닫힘)] 하위경로 목록. 곡선은 결정론적 표본점으로 근사."""
    sc = _Scan(d or "")
    subs: List[Tuple[List[Tuple[float, float]], bool]] = []
    cur: List[Tuple[float, float]] = []
    x = y = 0.0
    sx = sy = 0.0
    last_ctrl = None   # (kind, x, y) for S/T reflection
    cmd = None

    def flush(closed):
        nonlocal cur
        if len(cur) >= 2:
            subs.append((cur, closed))
        cur = []

    guard = 0
    while True:
        guard += 1
        if guard > 1_000_000:
            break
        c = sc.peek_cmd()
        if c is not None:
            sc.i += 1
            cmd = c
            if c in "Zz":
                if cur:
                    flush(True)
                x, y = sx, sy
                last_ctrl = None
                cmd = None
                continue
        elif cmd is None:
            break
        U = cmd.upper()
        rel = cmd.islower()
        if U == "A":
            vals = []
            for k in range(7):
                v = sc.flag() if k in (3, 4) else sc.num()
                if v is None:
                    break
                vals.append(v)
        else:
            vals = []
            for _k in range(_NARGS[U]):
                v = sc.num()
                if v is None:
                    break
                vals.append(v)
        if len(vals) < _NARGS[U]:
            if c is None:
                break       # 명령 없이 숫자도 없음 → 끝
            continue
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if U == "M":
            flush(False)
            x, y = vals[0] + ox, vals[1] + oy
            sx, sy = x, y
            cur = [(x, y)]
            cmd = "l" if rel else "L"    # 암묵 반복은 L
            last_ctrl = None
            continue
        if not cur:
            cur = [(x, y)]
        if U == "L":
            x, y = vals[0] + ox, vals[1] + oy
            cur.append((x, y)); last_ctrl = None
        elif U == "H":
            x = vals[0] + (x if rel else 0.0)
            cur.append((x, y)); last_ctrl = None
        elif U == "V":
            y = vals[0] + (y if rel else 0.0)
            cur.append((x, y)); last_ctrl = None
        elif U == "C":
            p1 = (vals[0] + ox, vals[1] + oy); p2 = (vals[2] + ox, vals[3] + oy)
            p3 = (vals[4] + ox, vals[5] + oy)
            cur += L.cubic_pts((x, y), p1, p2, p3)
            x, y = p3; last_ctrl = ("C", p2)
        elif U == "S":
            p1 = (2 * x - last_ctrl[1][0], 2 * y - last_ctrl[1][1]) if last_ctrl and last_ctrl[0] == "C" else (x, y)
            p2 = (vals[0] + ox, vals[1] + oy); p3 = (vals[2] + ox, vals[3] + oy)
            cur += L.cubic_pts((x, y), p1, p2, p3)
            x, y = p3; last_ctrl = ("C", p2)
        elif U == "Q":
            p1 = (vals[0] + ox, vals[1] + oy); p2 = (vals[2] + ox, vals[3] + oy)
            cur += L.quad_pts((x, y), p1, p2)
            x, y = p2; last_ctrl = ("Q", p1)
        elif U == "T":
            p1 = (2 * x - last_ctrl[1][0], 2 * y - last_ctrl[1][1]) if last_ctrl and last_ctrl[0] == "Q" else (x, y)
            p2 = (vals[0] + ox, vals[1] + oy)
            cur += L.quad_pts((x, y), p1, p2)
            x, y = p2; last_ctrl = ("Q", p1)
        elif U == "A":
            ex, ey = vals[5] + ox, vals[6] + oy
            cur += L.svg_arc_pts(x, y, vals[0], vals[1], vals[2], vals[3], vals[4], ex, ey)
            x, y = ex, ey; last_ctrl = None
    flush(False)
    return subs


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def _names(el) -> List[str]:
    a = _attrs(el)
    out = []
    for k in ("id", "label", "class", "data-layer", "data-kind"):
        v = (a.get(k) or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _resolve_kind(chain: List[List[str]], layer_map) -> str:
    """가장 가까운 이름부터 분류. 아무것도 안 맞으면 equipment."""
    for names in chain:
        for nm in names:
            k = classify(nm, layer_map, default=_NONE)
            if k != _NONE:
                return k
    return "equipment"


def _resolve_floor(chain: List[List[str]], default: int) -> int:
    for names in chain:
        for nm in names:
            f = floor_from_name(nm)
            if f is not None:
                return f
    return default


def _scale(root_attrs, opts) -> Tuple[float, str, float]:
    """(m_per_user_unit, 근거, 뒤집기 기준 y)."""
    vb = _nums(root_attrs.get("viewBox"))
    flip_y = (vb[1] + vb[3]) if len(vb) == 4 else _len(root_attrs.get("height"), 0.0)
    user = any(opts.get(k) not in (None, "") for k in ("units", "m_per_unit", "px_per_m"))
    if user:
        f, basis = resolve_scale(opts, None, "svg")
        return f, basis, flip_y
    ratio = opts.get("scale_ratio")
    ratio = float(ratio) if ratio not in (None, "") else 1.0
    if ratio <= 0:
        raise ImportErrorUser("scale_ratio 는 양수여야 한다 (1:100 도면이면 100)")
    tail = f", 축척 1:{ratio:g} → ×{ratio:g}" if ratio != 1.0 else ""
    dm = root_attrs.get("data-m-per-unit")
    if dm not in (None, ""):
        f = float(dm)
        if f <= 0:
            raise ImportErrorUser("data-m-per-unit 은 양수여야 한다")
        return f * ratio, f"SVG 루트 data-m-per-unit={f:g}" + tail, flip_y
    wattr = (root_attrs.get("width") or "").strip()
    m = re.match(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z%]*)\s*$", wattr)
    if m and m.group(2).lower() in LEN_UNITS and len(vb) == 4 and vb[2] > 0:
        u = m.group(2).lower()
        wm = float(m.group(1)) * LEN_UNITS[u]
        f = wm / vb[2]
        hattr = (root_attrs.get("height") or "").strip()
        mh = re.match(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z%]*)\s*$", hattr)
        if mh and mh.group(2).lower() in LEN_UNITS and vb[3] > 0:
            fh = float(mh.group(1)) * LEN_UNITS[mh.group(2).lower()] / vb[3]
            if abs(fh - f) > 1e-9 * max(f, 1e-12):
                raise ImportErrorUser(
                    "SVG 의 width/viewBox 와 height/viewBox 축척이 다르다 (가로세로 비율 왜곡). "
                    "m_per_unit 을 직접 지정할 것")
        return (f * ratio,
                f"SVG width={wattr} / viewBox 폭 {vb[2]:g} → 사용자단위 1 = {f:g} m" + tail,
                flip_y)
    f, basis = resolve_scale(opts, None, "svg")   # → ImportErrorUser (단위 모름)
    return f, basis, flip_y


def read(data: bytes, filename: str, opts: dict) -> dict:
    """바이트 → Takeoff. 손상된 값은 ImportErrorUser 로 바꿔 알린다 (내부 예외를 그대로 내보내지 않는다)."""
    try:
        return _read(data, filename, opts)
    except ImportErrorUser:
        raise
    except (ValueError, IndexError, KeyError, TypeError, ZeroDivisionError, RecursionError) as e:
        raise ImportErrorUser(
            f"SVG 파일의 내용을 해석하지 못했다 ({type(e).__name__}: {str(e)[:120]}). "
            f"파일이 손상되었거나 잘렸는지 확인하고, CAD/BIM 프로그램에서 다시 내보낼 것")


def _read(data: bytes, filename: str, opts: dict) -> dict:
    tk = new_takeoff(filename, "svg", data)
    if b"<!ENTITY" in data[:65536]:
        raise ImportErrorUser("SVG 에 DTD 엔티티 선언이 있어 읽지 않는다 (보안). 엔티티 없이 다시 저장할 것")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ImportErrorUser(f"SVG XML 을 해석하지 못했다: {e}")
    if _local(root.tag) != "svg":
        raise ImportErrorUser("루트 요소가 <svg> 가 아니다")
    ra = _attrs(root)
    f, basis, flip_y = _scale(ra, opts)
    tk["scale_basis"] = basis
    layer_map = opts.get("layer_map")
    default_floor = int(opts.get("floor", 0) or 0)
    tol_u = float(opts.get("snap_tol_m", 0.001)) / f

    def to_m(pts):
        return [(p[0] * f, (flip_y - p[1]) * f) for p in pts]

    shapes = []   # (chain, layer_str, pts_user, ref, circle)
    segs: Dict[Tuple[str, str, int], List] = {}
    texts = []
    counts = {"shapes": 0, "skipped": {}}

    def skip(tag):
        counts["skipped"][tag] = counts["skipped"].get(tag, 0) + 1

    def walk(el, m, chain, path_ids, idx):
        tag = _local(el.tag)
        a = _attrs(el)
        if tag in SKIP_TAGS or a.get("display") == "none" or "display:none" in (a.get("style") or "").replace(" ", ""):
            return
        m = L.mul(m, parse_transform(a.get("transform")))
        names = _names(el)
        my_chain = [names] + chain
        ref = "/".join(path_ids + [names[0] if names else f"{tag}[{idx}]"])
        layer_str = "/".join(n[0] for n in reversed(my_chain) if n)
        if tag in ("svg", "g", "a", "switch") or el is root:
            k = 0
            for ch in list(el):
                walk(ch, m, my_chain, path_ids + ([names[0]] if names else [f"{tag}[{idx}]"]) if el is not root else [], k)
                k += 1
            return
        if tag == "text":
            s = " ".join(t.strip() for t in el.itertext() if t.strip())
            xs, ys = _nums(a.get("x")), _nums(a.get("y"))
            if not xs or not ys:
                for ch in el:
                    if _local(ch.tag) == "tspan":
                        ca = _attrs(ch)
                        xs = xs or _nums(ca.get("x")); ys = ys or _nums(ca.get("y"))
                        break
            px, py = L.apply(m, xs[0] if xs else 0.0, ys[0] if ys else 0.0)
            k = _resolve_kind(my_chain, layer_map)
            if s and (k != "ignore" or TEXT_LAYER_RX.search(layer_str)):
                texts.append((my_chain, px, py, s))
            return
        if tag == "use":
            skip("use"); return
        if tag not in SHAPE_TAGS:
            if tag not in ("tspan",):
                skip(tag)
            return
        pts = None
        circle = False
        if tag == "rect":
            x, y = _len(a.get("x")), _len(a.get("y"))
            w, h = _len(a.get("width")), _len(a.get("height"))
            if w > 0 and h > 0:
                pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        elif tag in ("polygon", "polyline"):
            v = _nums(a.get("points"))
            p = [(v[k], v[k + 1]) for k in range(0, len(v) - 1, 2)]
            if tag == "polygon":
                pts = L.dedupe_closed(p, 0.0)
            else:
                p2, closed = L.close_if_coincident(p, tol_u)
                if closed:
                    pts = L.dedupe_closed(p2, 0.0)
                else:
                    for k in range(len(p) - 1):
                        segs.setdefault((layer_str, ref, 0), []).append(
                            L.apply(m, *p[k]) + L.apply(m, *p[k + 1]))
                    _chains[layer_str] = my_chain
        elif tag in ("circle", "ellipse"):
            cx, cy = _len(a.get("cx")), _len(a.get("cy"))
            if tag == "circle":
                rx = ry = _len(a.get("r"))
            else:
                rx, ry = _len(a.get("rx")), _len(a.get("ry"))
            if rx > 0 and ry > 0:
                pts = L.circle_pts(cx, cy, rx, ry); circle = True
        elif tag == "line":
            p0 = L.apply(m, _len(a.get("x1")), _len(a.get("y1")))
            p1 = L.apply(m, _len(a.get("x2")), _len(a.get("y2")))
            segs.setdefault((layer_str, "", 0), []).append(p0 + p1)
            _chains[layer_str] = my_chain
        elif tag == "path":
            subs = parse_path(a.get("d", ""))
            closed_polys = []
            for sp, closed in subs:
                if not closed:
                    sp, closed = L.close_if_coincident(sp, tol_u)
                if closed:
                    q = L.dedupe_closed(sp, 0.0)
                    if len(q) >= 3:
                        closed_polys.append(q)
                else:
                    for k in range(len(sp) - 1):
                        segs.setdefault((layer_str, "", 0), []).append(
                            L.apply(m, *sp[k]) + L.apply(m, *sp[k + 1]))
                    _chains[layer_str] = my_chain
            # 같은 path 안에서 가장 큰 하위경로 안에 든 하위경로는 구멍으로 보고 버린다
            if closed_polys:
                order = sorted(range(len(closed_polys)),
                               key=lambda k: (-polygon_area(closed_polys[k]), k))
                kept = []
                for k in order:
                    bb = bbox(closed_polys[k])
                    if any(_bb_in(bb, bbox(closed_polys[j])) for j in kept):
                        continue
                    kept.append(k)
                for n_, k in enumerate(sorted(kept)):
                    shapes.append((my_chain, layer_str, L.apply_pts(m, closed_polys[k]),
                                   ref + (f"#{n_}" if len(kept) > 1 else ""), False))
                    counts["shapes"] += 1
            return
        if pts is not None and len(pts) >= 3:
            shapes.append((my_chain, layer_str, L.apply_pts(m, pts), ref, circle))
            counts["shapes"] += 1

    _chains: Dict[str, List[List[str]]] = {}
    walk(root, L.IDENT, [], [], 0)

    def emit(chain, layer_str, pts_m, ref, circle):
        kind = _resolve_kind(chain, layer_map)
        fl = _resolve_floor(chain, default_floor)
        if kind == "ignore":
            return
        if kind == "boundary":
            x0, y0, x1, y1 = bbox(pts_m)
            tk["boundary"].append({"floor": fl, "rect": [round(x0, 6), round(y0, 6),
                                                         round(x1, 6), round(y1, 6)],
                                   "source_ref": ref})
            return
        it = make_item(kind, fl, pts_m, layer_str or None, ref)
        if circle:
            it["rect_fill"] = round(math.pi / 4.0, 4)
        tk["items"].append(it)

    for chain, layer_str, pts, ref, circle in shapes:
        emit(chain, layer_str, to_m(pts), ref, circle)

    by_layer: Dict[str, List] = {}
    for (layer_str, _r, _z), sg in segs.items():
        by_layer.setdefault(layer_str, []).extend(sg)
    line_rects = 0
    for layer_str in sorted(by_layer):
        chain = _chains.get(layer_str, [])
        if _resolve_kind(chain, layer_map) == "ignore":
            continue
        rects, info = L.rects_from_segments(by_layer[layer_str], tol_u)
        for r in rects:
            line_rects += 1
            emit(chain, layer_str, to_m(L.rect_pts(r)),
                 f"LINES:{layer_str}@{r[0]*f:.3f},{(flip_y - r[3])*f:.3f}", False)

    tlist = []
    for chain, px, py, s in texts:
        (X, Y), = to_m([(px, py)])
        tlist.append({"floor": _resolve_floor(chain, default_floor), "x": X, "y": Y, "text": s})
    assign_texts(tk, tlist)

    _area_warning(tk)
    boundary_from_items(tk)
    normalize_origin(tk)
    finalize(tk, float(opts.get("min_size_m", 0.3)))
    if counts["skipped"]:
        tk["warnings"].append("읽지 않은 SVG 요소: " + ", ".join(
            f"{k} {counts['skipped'][k]}" for k in sorted(counts["skipped"])))
    tk["stats"].update(svg_shapes=counts["shapes"], line_rects=line_rects,
                       svg_texts=len(texts))
    return tk


def _bb_in(inner, outer, tol=1e-9) -> bool:
    return (outer[0] - tol <= inner[0] and outer[1] - tol <= inner[1]
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)
