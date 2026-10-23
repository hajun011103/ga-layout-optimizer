# -*- coding: utf-8 -*-
"""
importers/dxf.py — ASCII DXF → Takeoff (결정론적, 표준 라이브러리만)

읽는 것
  HEADER   $INSUNITS (1=in, 2=ft, 4=mm, 5=cm, 6=m; 0/없음 = 모름 → units 옵션 필요)
  ENTITIES LWPOLYLINE, POLYLINE(+VERTEX/SEQEND), LINE(4개로 이룬 직사각형),
           SOLID, HATCH 경계(폴리선/직선·원호 모서리), CIRCLE, ELLIPSE(전체),
           INSERT(블록 — 삽입점·축척·회전, 중첩 8단계), TEXT, MTEXT, ATTRIB
  BLOCKS   블록 정의 (INSERT 하나 = item 하나, 블록 도형 전체의 외접 사각형)

레이어명으로 층(floor_from_name)과 종류(classify)를 정한다.
이 엔진이 쓴 DXF(FLOOR_nn_* 레이어)는 단위가 m 이고, *_TEXT 문자가 기기 id 다.

모르는 것은 warnings 에 남긴다. 치수·축척은 추정하지 않는다.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

from . import _lines as L
from .common import (ImportErrorUser, assign_texts, bbox, boundary_from_items, classify,
                     finalize, floor_from_name, make_item, new_takeoff, normalize_origin,
                     polygon_area, resolve_scale)

INSUNITS = {1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
INSUNITS_OTHER = {3: "mile", 7: "km", 8: "microinch", 9: "mil", 10: "yard",
                  11: "angstrom", 12: "nm", 13: "micron", 14: "dm"}
MAX_INSERT_DEPTH = 8

OWN_LAYER_RX = re.compile(r"^FLOOR_\d\d_[A-Z_]+$")
OWN_OTHER_LAYERS = {"0", "TURBINE_MISSILE_ZONE"}
TEXT_LAYER_RX = re.compile(r"text|label|tag|anno|문자|명칭|기기번호", re.I)
ID_LIKE_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.#/]*$")
ID_ATTR_TAGS = {"TAG", "ID", "TAGNO", "TAG_NO", "NO", "EQUIP_ID", "EQ_ID", "기기번호", "태그"}
NAME_ATTR_TAGS = {"NAME", "DESC", "DESCRIPTION", "TITLE", "명칭", "이름", "기기명"}


# ---------------------------------------------------------------------------
# 디코딩·그룹코드
# ---------------------------------------------------------------------------

def _decode(data: bytes) -> Tuple[str, str]:
    for enc in ("utf-8", "cp949", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace"), "latin-1"


_UESC = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def _unescape(s: str) -> str:
    s = _UESC.sub(lambda m: chr(int(m.group(1), 16)), s)
    return (s.replace("%%c", "⌀").replace("%%C", "⌀").replace("%%d", "°")
             .replace("%%D", "°").replace("%%p", "±").replace("%%P", "±")
             .replace("%%%", "%"))


_MT_FONT = re.compile(r"\\[fF][^;]*;")
_MT_PARAM = re.compile(r"\\[ACHQTWacdhqtwp][^;\\{}]*;")
_MT_STACK = re.compile(r"\\S([^;]*);")


def strip_mtext(s: str) -> str:
    """MTEXT 서식 코드 제거 (문단은 줄바꿈으로 남긴다): \\P, {\\f...;}, \\A1;, \\H2.5x;, \\S1^2; 등."""
    s = s.replace("\\P", "\n").replace("\\~", " ")
    s = _MT_FONT.sub("", s)
    s = _MT_STACK.sub(lambda m: re.sub(r"[\^#/]", "/", m.group(1)), s)
    s = _MT_PARAM.sub("", s)
    s = re.sub(r"\\[LlOoKkNX]", "", s)
    s = s.replace("\\{", "\x00").replace("\\}", "\x01").replace("\\\\", "\x02")
    s = s.replace("{", "").replace("}", "")
    s = s.replace("\x00", "{").replace("\x01", "}").replace("\x02", "\\")
    lines = [ln.strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _pairs(text: str) -> List[Tuple[int, str]]:
    lines = text.splitlines()
    n = len(lines) - (len(lines) % 2)
    out = []
    for k in range(0, n, 2):
        c = lines[k].strip()
        try:
            code = int(c)
        except ValueError:
            raise ImportErrorUser(
                f"DXF 그룹코드를 읽지 못했다 ({k+1}번째 줄 '{c[:20]}'). ASCII DXF 가 맞는지, "
                f"파일이 잘리지 않았는지 확인할 것")
        out.append((code, lines[k + 1].rstrip("\r")))
    return out


def _records(pairs: Sequence[Tuple[int, str]], i: int, end: int) -> List[List[Tuple[int, str]]]:
    """0 코드로 시작하는 엔티티 레코드들."""
    recs: List[List[Tuple[int, str]]] = []
    cur: Optional[List[Tuple[int, str]]] = None
    for k in range(i, end):
        c, v = pairs[k]
        if c == 0:
            if cur is not None:
                recs.append(cur)
            cur = [(0, v.strip().upper())]
        elif cur is not None:
            cur.append((c, v))
    if cur is not None:
        recs.append(cur)
    return recs


def _f(rec, code, default=None):
    for c, v in rec:
        if c == code:
            try:
                return float(v)
            except ValueError:
                return default
    return default


def _s(rec, code, default=""):
    for c, v in rec:
        if c == code:
            return v
    return default


def _all(rec, code):
    return [v for c, v in rec if c == code]


def _xy_list(rec) -> List[Tuple[float, float, float]]:
    """LWPOLYLINE 꼭짓점 (x, y, bulge)."""
    out: List[List[float]] = []
    for c, v in rec:
        if c == 10:
            out.append([float(v), 0.0, 0.0])
        elif c == 20 and out:
            out[-1][1] = float(v)
        elif c == 42 and out:
            out[-1][2] = float(v)
    return [tuple(p) for p in out]


def _with_bulges(verts: Sequence[Tuple[float, float, float]], closed: bool) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    n = len(verts)
    for k in range(n):
        x, y, b = verts[k]
        pts.append((x, y))
        if b and (k + 1 < n or closed):
            nx, ny, _ = verts[(k + 1) % n]
            pts += L.bulge_pts((x, y), (nx, ny), b)
    return pts


# ---------------------------------------------------------------------------
# 섹션 분해
# ---------------------------------------------------------------------------

def _sections(pairs):
    secs: Dict[str, Tuple[int, int]] = {}
    k, n = 0, len(pairs)
    while k < n:
        c, v = pairs[k]
        if c == 0 and v.strip().upper() == "SECTION" and k + 1 < n and pairs[k + 1][0] == 2:
            name = pairs[k + 1][1].strip().upper()
            j = k + 2
            while j < n and not (pairs[j][0] == 0 and pairs[j][1].strip().upper() == "ENDSEC"):
                j += 1
            secs.setdefault(name, (k + 2, j))
            k = j + 1
            continue
        k += 1
    return secs


def _header_vars(pairs, rng) -> Dict[str, List[Tuple[int, str]]]:
    out: Dict[str, List[Tuple[int, str]]] = {}
    if not rng:
        return out
    cur = None
    for k in range(rng[0], rng[1]):
        c, v = pairs[k]
        if c == 9:
            cur = v.strip().upper()
            out[cur] = []
        elif cur is not None:
            out[cur].append((c, v))
    return out


def _group_entities(recs):
    """POLYLINE+VERTEX+SEQEND, INSERT+ATTRIB+SEQEND 를 묶는다."""
    out = []
    k, n = 0, len(recs)
    while k < n:
        r = recs[k]
        t = r[0][1]
        if t == "POLYLINE" or (t == "INSERT" and _s(r, 66, "0").strip() == "1"):
            sub = []
            j = k + 1
            while j < n and recs[j][0][1] in ("VERTEX", "ATTRIB"):
                sub.append(recs[j]); j += 1
            if j < n and recs[j][0][1] == "SEQEND":
                j += 1
            out.append((r, sub))
            k = j
            continue
        out.append((r, []))
        k += 1
    return out


def _parse_blocks(pairs, rng) -> Dict[str, dict]:
    blocks: Dict[str, dict] = {}
    if not rng:
        return blocks
    recs = _records(pairs, rng[0], rng[1])
    cur = None
    body = []
    for r in recs:
        t = r[0][1]
        if t == "BLOCK":
            cur = r; body = []
        elif t == "ENDBLK":
            if cur is not None:
                name = _s(cur, 2).strip() or _s(cur, 3).strip()
                blocks[name.upper()] = dict(name=name, base=(_f(cur, 10, 0.0), _f(cur, 20, 0.0)),
                                            ents=_group_entities(body),
                                            flags=int(_f(cur, 70, 0) or 0))
            cur = None
        elif cur is not None:
            body.append(r)
    return blocks


# ---------------------------------------------------------------------------
# 엔티티 → 도형
# ---------------------------------------------------------------------------

class _Ctx:
    def __init__(self, tol):
        self.tol = tol
        self.shapes: List[dict] = []      # closed polygons {layer, pts, ref, circle}
        self.segs: Dict[str, List] = {}   # layer → [(x0,y0,x1,y1)]
        self.texts: List[dict] = []       # {layer, x, y, text}
        self.warn: Dict[str, int] = {}
        self.skipped: Dict[str, int] = {}

    def w(self, msg):
        self.warn[msg] = self.warn.get(msg, 0) + 1


def _ocs_flip(rec) -> bool:
    z = _f(rec, 230, 1.0)
    return z is not None and z < 0


def _hatch_paths(rec, ctx: _Ctx, ref: str) -> List[List[Tuple[float, float]]]:
    """HATCH 경계 경로 → 닫힌 다각형들. 스플라인 모서리 경로는 건너뛴다."""
    k = 0
    n = len(rec)
    while k < n and rec[k][0] != 91:
        k += 1
    if k >= n:
        return []
    npath = int(float(rec[k][1])); k += 1
    paths = []

    def need(code):
        nonlocal k
        while k < n and rec[k][0] != code:
            k += 1
        if k >= n:
            raise ValueError(code)
        v = rec[k][1]; k += 1
        return v

    try:
        for _ in range(npath):
            flag = int(float(need(92)))
            pts: List[Tuple[float, float]] = []
            ok = True
            if flag & 2:
                has_b = int(float(need(72)))
                need(73)
                nv = int(float(need(93)))
                verts = []
                for _v in range(nv):
                    x = float(need(10)); y = float(need(20))
                    b = 0.0
                    if has_b and k < n and rec[k][0] == 42:
                        b = float(rec[k][1]); k += 1
                    verts.append((x, y, b))
                pts = _with_bulges(verts, True)
            else:
                ne = int(float(need(93)))
                for _e in range(ne):
                    et = int(float(need(72)))
                    if et == 1:
                        x0 = float(need(10)); y0 = float(need(20))
                        x1 = float(need(11)); y1 = float(need(21))
                        pts += [(x0, y0), (x1, y1)]
                    elif et == 2:
                        cx = float(need(10)); cy = float(need(20)); r = float(need(40))
                        a0 = float(need(50)); a1 = float(need(51)); ccw = int(float(need(73)))
                        if ccw:
                            pts += L.arc_pts(cx, cy, r, a0, a1, True)
                        else:
                            pts += L.arc_pts(cx, cy, r, -a0, -a1, False)
                    elif et == 3:
                        cx = float(need(10)); cy = float(need(20))
                        mx = float(need(11)); my = float(need(21)); ratio = float(need(40))
                        a0 = float(need(50)); a1 = float(need(51)); need(73)
                        rx = math.hypot(mx, my)
                        rot = math.degrees(math.atan2(my, mx))
                        full = L.circle_pts(cx, cy, rx, rx * ratio, rot)
                        pts += full
                        ctx.w("HATCH 타원호 경계는 전체 타원의 외접 사각형으로 근사했다")
                    elif et == 4:
                        ok = False
                        # 스플라인 데이터 소비: 94 73 74 95 96, 40×, 10/20(42)×, [97 11/21×, 12/22 13/23]
                        need(94); need(73); need(74)
                        nk = int(float(need(95))); nc = int(float(need(96)))
                        for _q in range(nk):
                            need(40)
                        for _q in range(nc):
                            need(10); need(20)
                            if k < n and rec[k][0] == 42:
                                k += 1
                        if k + 1 < n and rec[k][0] == 97 and rec[k + 1][0] in (11, 97):
                            nf = int(float(rec[k][1])); k += 1
                            for _q in range(nf):
                                need(11); need(21)
                            while k < n and rec[k][0] in (12, 22, 13, 23):
                                k += 1
                    else:
                        raise ValueError(et)
            # 경계 객체 참조 (97 개수, 330×) 는 건너뛴다
            if k < n and rec[k][0] == 97:
                cnt = int(float(rec[k][1])); k += 1
                for _q in range(cnt):
                    if k < n and rec[k][0] == 330:
                        k += 1
            if not ok:
                ctx.w("HATCH 경계에 스플라인 모서리가 있어 그 경로를 건너뛰었다 (폴리선으로 다시 그릴 것)")
                continue
            pts = L.dedupe_closed(pts, 1e-12)
            if len(pts) >= 3:
                paths.append((flag, pts))
    except (ValueError, IndexError):
        ctx.w("HATCH 경계 데이터를 해석하지 못해 건너뛰었다")
        return []
    outer = [p for f, p in paths if f & (1 | 16)]
    return outer if outer else [p for f, p in paths]


def _entity_geom(rec, sub, ctx: _Ctx, m: L.Affine, layer_of, ref: str,
                 blocks: Dict[str, dict], depth: int, sink: dict):
    """
    엔티티 하나를 변환 m 으로 sink 에 넣는다.
    sink = {"polys": [(layer, pts, circle)], "segs": [(layer, seg)], "texts": [(layer, x, y, t)],
            "pts": [...]}  — 블록 안에서는 pts 로 외접 사각형만 쓴다.
    """
    t = rec[0][1]
    layer = layer_of(_s(rec, 8, "0").strip())
    flip = L.scale(-1.0, 1.0) if _ocs_flip(rec) else L.IDENT
    mm = L.mul(m, flip)

    def poly(pts, circle=False):
        wp = L.apply_pts(mm, pts)
        sink["polys"].append((layer, wp, circle))

    def seg(p0, p1):
        a = L.apply(mm, p0[0], p0[1]); b = L.apply(mm, p1[0], p1[1])
        sink["segs"].append((layer, (a[0], a[1], b[0], b[1])))

    if t == "LWPOLYLINE":
        verts = _xy_list(rec)
        closed = bool(int(_f(rec, 70, 0) or 0) & 1)
        _add_polyline(verts, closed, poly, seg, ctx.tol)
    elif t == "POLYLINE":
        fl = int(_f(rec, 70, 0) or 0)
        if fl & (16 | 64):
            ctx.skipped["POLYLINE(mesh)"] = ctx.skipped.get("POLYLINE(mesh)", 0) + 1
            return
        verts = []
        for v in sub:
            if v[0][1] != "VERTEX":
                continue
            vf = int(_f(v, 70, 0) or 0)
            if vf & 16:          # 스플라인 조정점
                continue
            verts.append((_f(v, 10, 0.0), _f(v, 20, 0.0), _f(v, 42, 0.0) or 0.0))
        _add_polyline(verts, bool(fl & 1), poly, seg, ctx.tol)
    elif t == "LINE":
        seg((_f(rec, 10, 0.0), _f(rec, 20, 0.0)), (_f(rec, 11, 0.0), _f(rec, 21, 0.0)))
    elif t in ("SOLID", "TRACE", "3DFACE"):
        p = [(_f(rec, 10 + i, 0.0), _f(rec, 20 + i, 0.0)) for i in range(4)]
        pts = [p[0], p[1], p[3], p[2]] if t != "3DFACE" else p
        pts = L.dedupe_closed(pts, 1e-12)
        if len(pts) >= 3:
            poly(pts)
    elif t == "HATCH":
        for pts in _hatch_paths(rec, ctx, ref):
            poly(pts)
    elif t == "CIRCLE":
        r = _f(rec, 40, 0.0) or 0.0
        if r > 0:
            poly(L.circle_pts(_f(rec, 10, 0.0), _f(rec, 20, 0.0), r), circle=True)
    elif t == "ELLIPSE":
        mx, my = _f(rec, 11, 0.0), _f(rec, 21, 0.0)
        ratio = _f(rec, 40, 1.0)
        p0, p1 = _f(rec, 41, 0.0), _f(rec, 42, 2 * math.pi)
        rx = math.hypot(mx, my)
        if rx > 0 and abs((p1 - p0) - 2 * math.pi) < 1e-6:
            poly(L.circle_pts(_f(rec, 10, 0.0), _f(rec, 20, 0.0), rx, rx * ratio,
                              math.degrees(math.atan2(my, mx))))
        else:
            ctx.skipped["ELLIPSE(arc)"] = ctx.skipped.get("ELLIPSE(arc)", 0) + 1
    elif t in ("TEXT", "MTEXT", "ATTRIB"):
        x, y = _f(rec, 10, 0.0), _f(rec, 20, 0.0)
        if t == "TEXT" and (int(_f(rec, 72, 0) or 0) or int(_f(rec, 73, 0) or 0)):
            if _f(rec, 11) is not None:
                x, y = _f(rec, 11, x), _f(rec, 21, y)
        if t == "MTEXT":
            s = strip_mtext(_unescape("".join(_all(rec, 3)) + _s(rec, 1)))
        else:
            s = _unescape(_s(rec, 1))
        X, Y = L.apply(mm, x, y)
        # MTEXT 의 문단(\\P)은 각각 별도 라벨 (태그와 명칭을 한 MTEXT 에 쓰는 경우가 많다)
        for part in s.split("\n"):
            if part.strip():
                sink["texts"].append((layer, X, Y, part.strip(), _s(rec, 2).strip().upper()))
    elif t == "INSERT":
        _insert(rec, sub, ctx, mm, layer, ref, blocks, depth, sink)
    else:
        ctx.skipped[t] = ctx.skipped.get(t, 0) + 1


def _add_polyline(verts, closed, poly, seg, tol):
    if len(verts) < 2:
        return
    if not closed:
        a, b = verts[0], verts[-1]
        if len(verts) >= 4 and abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol:
            verts = list(verts[:-1]); closed = True
    if closed:
        pts = L.dedupe_closed(_with_bulges(verts, True), 1e-12)
        if len(pts) >= 3:
            poly(pts)
        return
    pts = _with_bulges(verts, False)
    for k in range(len(pts) - 1):
        seg(pts[k], pts[k + 1])


def _insert(rec, sub, ctx, m, layer, ref, blocks, depth, sink):
    name = _s(rec, 2).strip()
    blk = blocks.get(name.upper())
    if blk is None:
        ctx.w(f"정의가 없는 블록 '{name}' 의 INSERT 를 건너뛰었다")
        return
    if depth >= MAX_INSERT_DEPTH:
        ctx.w(f"블록 중첩이 {MAX_INSERT_DEPTH}단계를 넘어 '{name}' 를 건너뛰었다")
        return
    if int(_f(rec, 70, 1) or 1) > 1 or int(_f(rec, 71, 1) or 1) > 1:
        ctx.w(f"MINSERT(배열 삽입) '{name}' 는 첫 칸만 읽었다")
    ix, iy = _f(rec, 10, 0.0), _f(rec, 20, 0.0)
    sx, sy = _f(rec, 41, 1.0), _f(rec, 42, 1.0)
    rot = _f(rec, 50, 0.0)
    bx, by = blk["base"]
    local = L.mul(L.translate(ix, iy),
                  L.mul(L.rotate_deg(rot), L.mul(L.scale(sx, sy), L.translate(-bx, -by))))
    mm = L.mul(m, local)
    inner = {"polys": [], "segs": [], "texts": [], "inserts": []}
    # 블록 안 '0' 레이어 도형은 INSERT 레이어를 물려받는다
    lay_of = (lambda l: layer if l == "0" else l)
    for k, (r, s) in enumerate(blk["ents"]):
        t = r[0][1]
        if t in ("ATTDEF",):
            continue
        _entity_geom(r, s, ctx, mm, lay_of, f"{ref}/{k}", blocks, depth + 1, inner)
    pts = []
    for _l, p, _c in inner["polys"]:
        pts += p
    for _l, sg in inner["segs"]:
        pts += [(sg[0], sg[1]), (sg[2], sg[3])]
    for ins in inner["inserts"]:
        pts += ins["pts"]
    attribs = []
    for a in sub:
        if a[0][1] == "ATTRIB":
            v = _unescape(_s(a, 1)).strip()
            if v:
                attribs.append((_s(a, 2).strip().upper(), v))
    texts = [t[3] for t in inner["texts"] if t[3]]
    content_layers = sorted({l for l, _p, _c in inner["polys"] if l != layer})
    sink["inserts"].append(dict(block=name, layer=layer, pts=pts, attribs=attribs,
                                texts=texts, content_layers=content_layers, ref=ref,
                                circle=(len(inner["polys"]) == 1 and inner["polys"][0][2]
                                        and not inner["segs"])))


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def _text_layer_ok(layer: str, layer_map) -> bool:
    """치수·화재구역·위반표시 같은 주석 레이어의 문자는 라벨로 쓰지 않는다."""
    if classify(layer, layer_map) != "ignore":
        return True
    return bool(TEXT_LAYER_RX.search(layer or ""))


def read(data: bytes, filename: str, opts: dict) -> dict:
    """바이트 → Takeoff. 손상된 값은 ImportErrorUser 로 바꿔 알린다 (내부 예외를 그대로 내보내지 않는다)."""
    try:
        return _read(data, filename, opts)
    except ImportErrorUser:
        raise
    except (ValueError, IndexError, KeyError, TypeError, ZeroDivisionError, RecursionError) as e:
        raise ImportErrorUser(
            f"DXF 파일의 내용을 해석하지 못했다 ({type(e).__name__}: {str(e)[:120]}). "
            f"파일이 손상되었거나 잘렸는지 확인하고, CAD/BIM 프로그램에서 다시 내보낼 것")


def _read(data: bytes, filename: str, opts: dict) -> dict:
    tk = new_takeoff(filename, "dxf", data)
    text, enc = _decode(data)
    pairs = _pairs(text)
    secs = _sections(pairs)
    if "ENTITIES" not in secs:
        raise ImportErrorUser("DXF 에 ENTITIES 섹션이 없다. ASCII DXF 로 다시 저장할 것")
    hdr = _header_vars(pairs, secs.get("HEADER"))
    layer_map = opts.get("layer_map")
    default_floor = int(opts.get("floor", 0) or 0)

    # ---- 단위 --------------------------------------------------------------
    ins = None
    if "$INSUNITS" in hdr:
        v = _f(hdr["$INSUNITS"], 70)
        ins = int(v) if v is not None else None
    native = INSUNITS.get(ins) if ins else None
    if ins in INSUNITS_OTHER and not any(opts.get(k) not in (None, "") for k in
                                         ("units", "m_per_unit", "px_per_m")):
        raise ImportErrorUser(
            f"DXF $INSUNITS={ins} ({INSUNITS_OTHER[ins]}) 는 지원하지 않는 단위다. "
            f"units(m/mm/cm/in/ft) 또는 m_per_unit 을 지정할 것")

    blocks = _parse_blocks(pairs, secs.get("BLOCKS"))
    ents = _group_entities(_records(pairs, *secs["ENTITIES"]))
    layers_used = sorted({_s(r, 8, "0").strip() for r, _s2 in ents if r[0][1] not in ("ENDSEC",)})
    own = bool(layers_used) and all(OWN_LAYER_RX.match(l) or l in OWN_OTHER_LAYERS
                                    for l in layers_used) and any(OWN_LAYER_RX.match(l)
                                                                  for l in layers_used)
    user_scale = any(opts.get(k) not in (None, "") for k in ("units", "m_per_unit", "px_per_m"))
    if native is None and own and not user_scale:
        f, basis = resolve_scale(opts, "m", "dxf")
        basis = ("$INSUNITS 없음 — 이 엔진의 DXF 출력(FLOOR_nn_ 레이어)으로 판단, "
                 "엔진은 m 단위로 쓴다 → ×1") + (basis[basis.index(","):] if "," in basis else "")
    else:
        f, basis = resolve_scale(opts, native, "dxf")
        if native and basis.startswith("파일 헤더"):
            basis = f"DXF $INSUNITS={ins} ({native}) → " + basis.split("→", 1)[1].strip()
    tk["scale_basis"] = basis
    tol_units = float(opts.get("snap_tol_m", 0.001)) / f
    ctx = _Ctx(tol_units)
    if enc != "utf-8":
        tk["warnings"].append(f"문자 인코딩을 {enc} 로 읽었다")

    # ---- 엔티티 ------------------------------------------------------------
    sink = {"polys": [], "segs": [], "texts": [], "inserts": []}
    for k, (r, sub) in enumerate(ents):
        h = _s(r, 5).strip()
        ref = f"{r[0][1]}#{h}" if h else f"{r[0][1]}@{k}"
        n0 = (len(sink["polys"]), len(sink["segs"]))
        _entity_geom(r, sub, ctx, L.IDENT, lambda l: l, ref, blocks, 0, sink)
        # ref 부착
        for j in range(n0[0], len(sink["polys"])):
            if len(sink["polys"][j]) == 3:
                sink["polys"][j] = sink["polys"][j] + (ref,)
        for j in range(n0[1], len(sink["segs"])):
            if len(sink["segs"][j]) == 2:
                sink["segs"][j] = sink["segs"][j] + (ref,)

    def to_m(pts):
        return [(p[0] * f, p[1] * f) for p in pts]

    def floor_of(layer):
        fl = floor_from_name(layer)
        return default_floor if fl is None else fl

    counts = {"closed": 0, "line_rects": 0, "inserts": 0, "ignored": 0, "boundary": 0}

    def emit(kind_layer, pts_m, ref, circle=False, extra=None, name=None, labels=None,
             class_name=None):
        kind = classify(class_name if class_name is not None else kind_layer, layer_map)
        fl = floor_of(kind_layer)
        if kind == "ignore":
            counts["ignored"] += 1
            return
        if kind == "boundary":
            x0, y0, x1, y1 = bbox(pts_m)
            tk["boundary"].append({"floor": fl, "rect": [round(x0, 6), round(y0, 6),
                                                         round(x1, 6), round(y1, 6)],
                                   "source_ref": ref})
            counts["boundary"] += 1
            if polygon_area(pts_m) < 0.98 * (x1 - x0) * (y1 - y0):
                tk["warnings"].append(
                    f"경계 {ref} 가 직사각형이 아니다 — 외접 사각형으로 근사했다. "
                    f"빈 모서리는 void 레이어로 그려 줄 것")
            return
        it = make_item(kind, fl, pts_m, kind_layer, ref, name=name)
        if circle:
            it["rect_fill"] = round(math.pi / 4.0, 4)
        if labels:
            it["labels"] += labels
        if extra:
            it.update(extra)
        tk["items"].append(it)

    for layer, pts, circle, ref in sink["polys"]:
        counts["closed"] += 1
        emit(layer, to_m(pts), ref, circle)

    # LINE / 열린 폴리선 → 레이어별 직사각형
    by_layer: Dict[str, List] = {}
    for layer, sg, ref in sink["segs"]:
        by_layer.setdefault(layer, []).append(sg)
    seg_total = 0
    for layer in sorted(by_layer):
        segs = by_layer[layer]
        seg_total += len(segs)
        if classify(layer, layer_map) == "ignore":
            continue
        rects, info = L.rects_from_segments(segs, tol_units)
        if info["truncated"]:
            tk["warnings"].append(f"레이어 {layer}: 선분이 너무 많아 {info['truncated']}개는 "
                                  f"직사각형 찾기에서 뺐다")
        for r in rects:
            counts["line_rects"] += 1
            emit(layer, to_m(L.rect_pts(r)),
                 f"LINES:{layer}@{r[0]*f:.3f},{r[1]*f:.3f}")

    # INSERT → 한 개의 item
    for ins_ in sink["inserts"]:
        if not ins_["pts"]:
            ctx.w(f"블록 '{ins_['block']}' 에 도형이 없어 건너뛰었다")
            continue
        counts["inserts"] += 1
        x0, y0, x1, y1 = bbox(to_m(ins_["pts"]))
        layer = ins_["layer"]
        cls = layer
        if layer == "0" or not classify(layer, layer_map, default=""):
            # 레이어로 판단이 안 되면 블록명 → 블록 내용 레이어 순으로 본다
            for cand in [ins_["block"]] + ins_["content_layers"]:
                if classify(cand, layer_map, default=""):
                    cls = cand
                    break
        tag = next((v for k_, v in ins_["attribs"] if k_ in ID_ATTR_TAGS), None)
        nm = next((v for k_, v in ins_["attribs"] if k_ in NAME_ATTR_TAGS), None)
        labels = [v for _k, v in ins_["attribs"]] + ins_["texts"]
        extra = {"block": ins_["block"]}
        before = len(tk["items"])
        emit(layer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], ins_["ref"],
             circle=bool(ins_["circle"]), extra=extra, name=nm or ins_["block"],
             labels=labels, class_name=cls)
        if tag and len(tk["items"]) > before:
            tk["items"][-1]["id"] = re.sub(r"[^A-Za-z0-9_\-]+", "-", tag.strip()).strip("-")

    # 문자
    texts = []
    for layer, x, y, s, _tag in sink["texts"]:
        if not s or not _text_layer_ok(layer, layer_map):
            continue
        texts.append({"floor": floor_of(layer), "x": x * f, "y": y * f, "text": s})
    assign_texts(tk, texts)

    # 엔진 자체 출력: *_TEXT 문자가 곧 기기 id
    if own:
        for it in tk["items"]:
            if not it["id"] and it["labels"] and ID_LIKE_RX.match(it["labels"][0]):
                it["id"] = it["labels"][0]
                if not it["name"]:
                    it["name"] = it["labels"][0]

    _area_warning(tk)
    boundary_from_items(tk)
    normalize_origin(tk)
    finalize(tk, float(opts.get("min_size_m", 0.3)))

    for msg in sorted(ctx.warn):
        c = ctx.warn[msg]
        tk["warnings"].append(msg + (f" (×{c})" if c > 1 else ""))
    if ctx.skipped:
        tk["warnings"].append("읽지 않은 엔티티: " + ", ".join(
            f"{k} {ctx.skipped[k]}" for k in sorted(ctx.skipped)))
    tk["stats"].update(dxf_insunits=ins, encoding=enc, own_output=own,
                       closed_shapes=counts["closed"], line_segments=seg_total,
                       line_rects=counts["line_rects"], inserts=counts["inserts"],
                       ignored_shapes=counts["ignored"], layers=layers_used)
    return tk


def _area_warning(tk: dict) -> None:
    """한 item 이 전체 외접 사각형의 80% 넘게 덮으면 외곽선일 가능성을 알린다 (분류는 바꾸지 않는다)."""
    its = tk["items"]
    if len(its) < 2:
        return
    for fl in sorted({it["floor"] for it in its}):
        group = [it for it in its if it["floor"] == fl]
        if len(group) < 2:
            continue
        x0 = min(it["x"] for it in group); y0 = min(it["y"] for it in group)
        x1 = max(it["x"] + it["w"] for it in group); y1 = max(it["y"] + it["d"] for it in group)
        tot = (x1 - x0) * (y1 - y0)
        if tot <= 0:
            continue
        for it in group:
            if it["w"] * it["d"] > 0.8 * tot:
                tk["warnings"].append(
                    f"{it['source_ref']} (레이어 {it['layer']}) 가 {fl+1}층 전체 범위의 "
                    f"{it['w']*it['d']/tot:.0%} 를 덮는다 — 외곽선이면 "
                    f"layer_map 으로 boundary 를 지정할 것 (예: {{\"^{re.escape(it['layer'] or '')}$\": \"boundary\"}})")
