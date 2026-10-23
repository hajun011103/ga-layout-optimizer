# -*- coding: utf-8 -*-
"""
importers/ifc.py — IFC2x3 / IFC4 (STEP Part 21) → Takeoff (결정론적, 표준 라이브러리만)

읽는 것
  - IfcBuildingStorey (이름·표고) → 층. 표고 오름차순으로 0,1,2…
  - 소속: IfcRelContainedInSpatialStructure, IfcRelAggregates (실 → 층, 요소 → 실 → 층)
  - 평면 외형: Body 의 IfcExtrudedAreaSolid (IfcRectangleProfileDef / IfcCircleProfileDef /
    IfcArbitraryClosedProfileDef[IfcPolyline, IfcIndexedPolyCurve]), IfcBooleanClippingResult 의
    첫 피연산자, IfcMappedItem. 그 밖(IfcFacetedBrep 등)은 Box 표현의 IfcBoundingBox,
    없으면 형상 꼭짓점의 외접 사각형 (경고). IfcLocalPlacement 사슬(Z축 회전) 적용.
  - 단위: IfcUnitAssignment 의 LENGTHUNIT (IfcSIUnit 접두어 / IfcConversionBasedUnit)
  - 종류: IfcSpace → room, IfcColumn → obstacle, IfcStair·IfcOpeningElement → opening,
    벽·슬래브·보·문·창·배관 → 무시, 기기형 요소 → equipment. layer_map 은 'IFC타입:이름' 에 적용.
  - 높이: 압출 깊이를 아는 경우 item['h'] [m]
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

from . import _lines as L
from .common import (_ID_SAFE, ImportErrorUser, assign_texts, bbox, boundary_from_items, finalize,
                     make_item, new_takeoff, normalize_origin, resolve_scale)

SI_PREFIX = {"EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9, "MEGA": 1e6, "KILO": 1e3,
             "HECTO": 1e2, "DECA": 1e1, "DECI": 1e-1, "CENTI": 1e-2, "MILLI": 1e-3,
             "MICRO": 1e-6, "NANO": 1e-9, "PICO": 1e-12, "FEMTO": 1e-15, "ATTO": 1e-18}

ROOM_TYPES = {"IFCSPACE"}
OBSTACLE_TYPES = {"IFCCOLUMN", "IFCCOLUMNSTANDARDCASE", "IFCCHIMNEY"}
OPENING_TYPES = {"IFCSTAIR", "IFCSTAIRFLIGHT", "IFCOPENINGELEMENT", "IFCOPENINGSTANDARDCASE",
                 "IFCRAMP", "IFCRAMPFLIGHT", "IFCTRANSPORTELEMENT"}
IGNORE_TYPES = {"IFCWALL", "IFCWALLSTANDARDCASE", "IFCWALLELEMENTEDCASE", "IFCCURTAINWALL",
                "IFCSLAB", "IFCSLABSTANDARDCASE", "IFCSLABELEMENTEDCASE", "IFCBEAM",
                "IFCBEAMSTANDARDCASE", "IFCROOF", "IFCDOOR", "IFCDOORSTANDARDCASE", "IFCWINDOW",
                "IFCWINDOWSTANDARDCASE", "IFCCOVERING", "IFCRAILING", "IFCPLATE",
                "IFCPLATESTANDARDCASE", "IFCMEMBER", "IFCMEMBERSTANDARDCASE", "IFCFOOTING",
                "IFCPILE", "IFCBUILDINGELEMENTPART", "IFCFLOWSEGMENT", "IFCFLOWFITTING",
                "IFCPIPESEGMENT", "IFCPIPEFITTING", "IFCDUCTSEGMENT", "IFCDUCTFITTING",
                "IFCCABLESEGMENT", "IFCCABLEFITTING", "IFCCABLECARRIERSEGMENT",
                "IFCCABLECARRIERFITTING", "IFCVIRTUALELEMENT", "IFCANNOTATION", "IFCGRID",
                "IFCREINFORCINGBAR", "IFCREINFORCINGMESH", "IFCTENDON", "IFCDISCRETEACCESSORY",
                "IFCMECHANICALFASTENER", "IFCFASTENER", "IFCSHADINGDEVICE", "IFCSPATIALZONE",
                "IFCZONE", "IFCEXTERNALSPATIALELEMENT"}
MESH_TYPES = {"IFCFACETEDBREP", "IFCFACETEDBREPWITHVOIDS", "IFCADVANCEDBREP",
              "IFCFACEBASEDSURFACEMODEL", "IFCSHELLBASEDSURFACEMODEL",
              "IFCTRIANGULATEDFACESET", "IFCPOLYGONALFACESET"}
EQUIPMENT_TYPES = {
    "IFCBUILDINGELEMENTPROXY", "IFCFLOWTERMINAL", "IFCFLOWMOVINGDEVICE", "IFCFLOWSTORAGEDEVICE",
    "IFCFLOWTREATMENTDEVICE", "IFCFLOWCONTROLLER", "IFCENERGYCONVERSIONDEVICE",
    "IFCDISTRIBUTIONELEMENT", "IFCDISTRIBUTIONFLOWELEMENT", "IFCDISTRIBUTIONCONTROLELEMENT",
    "IFCFURNISHINGELEMENT", "IFCFURNITURE", "IFCSYSTEMFURNITUREELEMENT",
    "IFCPUMP", "IFCTANK", "IFCBOILER", "IFCCHILLER", "IFCHEATEXCHANGER", "IFCCOMPRESSOR",
    "IFCFAN", "IFCCOOLINGTOWER", "IFCCONDENSER", "IFCEVAPORATOR", "IFCEVAPORATIVECOOLER",
    "IFCCOIL", "IFCAIRTOAIRHEATRECOVERY", "IFCUNITARYEQUIPMENT", "IFCELECTRICGENERATOR",
    "IFCELECTRICMOTOR", "IFCENGINE", "IFCTRANSFORMER", "IFCELECTRICDISTRIBUTIONBOARD",
    "IFCSWITCHINGDEVICE", "IFCELECTRICFLOWSTORAGEDEVICE", "IFCFILTER", "IFCINTERCEPTOR",
    "IFCMEDICALDEVICE", "IFCSOLARDEVICE", "IFCBURNER", "IFCHUMIDIFIER", "IFCSPACEHEATER",
    "IFCUNITARYCONTROLELEMENT", "IFCPROTECTIVEDEVICE", "IFCAUDIOVISUALAPPLIANCE",
    "IFCELECTRICAPPLIANCE", "IFCCOMMUNICATIONSAPPLIANCE", "IFCSANITARYTERMINAL",
    "IFCFIRESUPPRESSIONTERMINAL", "IFCAIRTERMINALBOX", "IFCVALVE", "IFCDAMPER",
    "IFCFLOWMETER", "IFCCONTROLLER", "IFCSENSOR", "IFCACTUATOR", "IFCALARM",
    "IFCLAMP", "IFCLIGHTFIXTURE", "IFCOUTLET", "IFCAIRTERMINAL", "IFCSTACKTERMINAL",
    "IFCWASTETERMINAL", "IFCTUBEBUNDLE", "IFCDUCTSILENCER",
}


# ---------------------------------------------------------------------------
# STEP Part 21 파서
# ---------------------------------------------------------------------------

class Ref(int):
    """#123 참조."""


class Enum(str):
    """.T. / .MILLI. 같은 열거값."""


class Typed(tuple):
    """IFCLENGTHMEASURE(1.0) 같은 형식 지정 값 → (TYPE, [args])."""


def _decode_str(s: str) -> str:
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            if s.startswith("\\X2\\", i):
                j = s.find("\\X0\\", i + 4)
                if j < 0:
                    out.append(s[i:]); break
                hx = s[i + 4:j]
                for k in range(0, len(hx) - 3, 4):
                    out.append(chr(int(hx[k:k + 4], 16)))
                i = j + 4; continue
            if s.startswith("\\X4\\", i):
                j = s.find("\\X0\\", i + 4)
                if j < 0:
                    out.append(s[i:]); break
                hx = s[i + 4:j]
                for k in range(0, len(hx) - 7, 8):
                    out.append(chr(int(hx[k:k + 8], 16)))
                i = j + 4; continue
            if s.startswith("\\X\\", i) and i + 5 <= n:
                out.append(bytes([int(s[i + 3:i + 5], 16)]).decode("latin-1"))
                i += 5; continue
            if s.startswith("\\S\\", i) and i + 4 <= n:
                out.append(chr(ord(s[i + 3]) + 128)); i += 4; continue
            if s.startswith("\\P", i) and i + 3 < n and s[i + 3] == "\\":
                i += 4; continue
            if s[i + 1] == "\\":
                out.append("\\"); i += 2; continue
        out.append(ch); i += 1
    return "".join(out)


_TOK = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>'(?:[^']|'')*')
  | (?P<ref>\#\d+)
  | (?P<enum>\.[A-Za-z0-9_]+\.)
  | (?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
  | (?P<kw>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<p>[(),$*=;])
  | (?P<bin>"[0-9A-Fa-f]*")
""", re.X)


def _parse_args(s: str):
    """'(a,b,(c,d))' → 파이썬 리스트."""
    toks = []
    for m in _TOK.finditer(s):
        k = m.lastgroup
        if k == "ws":
            continue
        toks.append((k, m.group(0)))
    pos = 0

    def val():
        nonlocal pos
        k, t = toks[pos]
        if k == "p" and t == "(":
            pos += 1
            lst = []
            while not (toks[pos][0] == "p" and toks[pos][1] == ")"):
                lst.append(val())
                if toks[pos][0] == "p" and toks[pos][1] == ",":
                    pos += 1
            pos += 1
            return lst
        pos += 1
        if k == "str":
            return _decode_str(t[1:-1].replace("''", "'"))
        if k == "ref":
            return Ref(int(t[1:]))
        if k == "enum":
            return Enum(t[1:-1].upper())
        if k == "num":
            return float(t) if any(c in t for c in ".eE") else int(t)
        if k == "p" and t in "$*":
            return None
        if k == "kw":
            if pos < len(toks) and toks[pos] == ("p", "("):
                inner = val()
                return Typed((t.upper(), inner))
            return Enum(t.upper())
        if k == "bin":
            return t
        raise ValueError(t)
    return val()


def parse_step(text: str) -> Dict[int, Tuple[str, list]]:
    i = text.upper().find("DATA;")
    if i < 0:
        raise ImportErrorUser("IFC 파일에 DATA 섹션이 없다. IFC STEP(.ifc) 파일인지 확인할 것")
    body = text[i + 5:]
    ents: Dict[int, Tuple[str, list]] = {}
    # 문자열 밖의 ';' 로 문장을 나눈다
    stmts = []
    buf = []
    inq = False
    for ch in body:
        if ch == "'":
            inq = not inq
        if ch == ";" and not inq:
            stmts.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    rx = re.compile(r"^\s*#(\d+)\s*=\s*([A-Za-z0-9_]+)\s*(\(.*\))\s*$", re.S)
    for st in stmts:
        m = rx.match(st)
        if not m:
            continue           # ENDSEC, 복합 엔티티 (#1=(A()B());) 등
        try:
            args = _parse_args(m.group(3))
        except (ValueError, IndexError):
            continue
        ents[int(m.group(1))] = (m.group(2).upper(), args)
    return ents


# ---------------------------------------------------------------------------
# 기하
# ---------------------------------------------------------------------------

class _Model:
    def __init__(self, ents):
        self.e = ents
        self.warn: Dict[str, int] = {}

    def get(self, r) -> Tuple[str, list]:
        if isinstance(r, Ref) and int(r) in self.e:
            return self.e[int(r)]
        return ("", [])

    def w(self, msg):
        self.warn[msg] = self.warn.get(msg, 0) + 1

    def point(self, r) -> Tuple[float, float, float]:
        t, a = self.get(r)
        if t == "IFCCARTESIANPOINT" and a and isinstance(a[0], list):
            c = [float(v) for v in a[0]] + [0.0, 0.0, 0.0]
            return c[0], c[1], c[2]
        return 0.0, 0.0, 0.0

    def direction(self, r, default):
        t, a = self.get(r)
        if t == "IFCDIRECTION" and a and isinstance(a[0], list):
            c = [float(v) for v in a[0]] + [0.0, 0.0, 0.0]
            return c[0], c[1], c[2]
        return default

    def axis3d(self, r) -> Tuple[L.Affine, float, bool]:
        """IfcAxis2Placement3D → (평면 변환, z 이동, z축이 연직인가)."""
        t, a = self.get(r)
        if t not in ("IFCAXIS2PLACEMENT3D", "IFCAXIS2PLACEMENT2D"):
            return L.IDENT, 0.0, True
        x, y, z = self.point(a[0] if a else None)
        if t == "IFCAXIS2PLACEMENT2D":
            rx, ry, _ = self.direction(a[1] if len(a) > 1 else None, (1.0, 0.0, 0.0))
            ax_ok = True
        else:
            az = self.direction(a[1] if len(a) > 1 else None, (0.0, 0.0, 1.0))
            ax_ok = abs(abs(az[2]) - 1.0) < 1e-6
            if az[2] < 0 and ax_ok:
                self.w("z축이 아래를 향하는 배치(IfcAxis2Placement3D)는 평면 좌우가 뒤집힌 채로 읽었다")
            rx, ry, _ = self.direction(a[2] if len(a) > 2 else None, (1.0, 0.0, 0.0))
        n = math.hypot(rx, ry)
        if n < 1e-12:
            rx, ry, n = 1.0, 0.0, 1.0
        c, s = _snap(rx / n), _snap(ry / n)
        return (c, s, -s, c, x, y), z, ax_ok

    def placement(self, r, depth=0) -> Tuple[L.Affine, float, bool]:
        t, a = self.get(r)
        if t != "IFCLOCALPLACEMENT" or depth > 64:
            if t and t != "IFCLOCALPLACEMENT":
                self.w(f"{t} 배치는 지원하지 않아 원점으로 두었다")
            return L.IDENT, 0.0, True
        parent = a[0] if a else None
        m, z, ok = self.axis3d(a[1] if len(a) > 1 else None)
        if parent is not None:
            pm, pz, pok = self.placement(parent, depth + 1)
            return L.mul(pm, m), pz + z, ok and pok
        return m, z, ok


def _snap(v: float) -> float:
    for t in (0.0, 1.0, -1.0):
        if abs(v - t) < 1e-12:
            return t
    return v


def _profile_pts(md: _Model, r) -> Optional[List[Tuple[float, float]]]:
    t, a = md.get(r)
    if t in ("IFCRECTANGLEPROFILEDEF", "IFCROUNDEDRECTANGLEPROFILEDEF",
             "IFCRECTANGLEHOLLOWPROFILEDEF"):
        pos, xd, yd = a[2], float(a[3]), float(a[4])
        m, _z, _ok = md.axis3d(pos)
        pts = [(-xd / 2, -yd / 2), (xd / 2, -yd / 2), (xd / 2, yd / 2), (-xd / 2, yd / 2)]
        return L.apply_pts(m, pts)
    if t == "IFCCIRCLEPROFILEDEF" or t == "IFCCIRCLEHOLLOWPROFILEDEF":
        m, _z, _ok = md.axis3d(a[2])
        return L.apply_pts(m, L.circle_pts(0.0, 0.0, float(a[3])))
    if t in ("IFCARBITRARYCLOSEDPROFILEDEF", "IFCARBITRARYPROFILEDEFWITHVOIDS"):
        return _curve_pts(md, a[2])
    if t:
        md.w(f"단면 {t} 는 지원하지 않는다")
    return None


def _curve_pts(md: _Model, r) -> Optional[List[Tuple[float, float]]]:
    t, a = md.get(r)
    if t == "IFCPOLYLINE":
        pts = [md.point(p)[:2] for p in a[0]]
        return L.dedupe_closed(pts, 1e-12)
    if t == "IFCINDEXEDPOLYCURVE":
        lt, la = md.get(a[0])
        if lt in ("IFCCARTESIANPOINTLIST2D", "IFCCARTESIANPOINTLIST3D"):
            coords = [(float(c[0]), float(c[1])) for c in la[0]]
            segs = a[1] if len(a) > 1 else None
            if segs:
                idx = []
                for sg in segs:
                    if isinstance(sg, Typed):
                        idx += [int(v) for v in sg[1]]
                        if sg[0] == "IFCARCINDEX":
                            md.w("IfcIndexedPolyCurve 의 원호는 세 점으로 근사했다")
                pts = [coords[i - 1] for i in idx]
            else:
                pts = coords
            return L.dedupe_closed(pts, 1e-12)
    if t == "IFCCOMPOSITECURVE":
        md.w("IfcCompositeCurve 단면은 지원하지 않는다")
        return None
    if t:
        md.w(f"곡선 {t} 는 지원하지 않는다")
    return None


def _all_points(md: _Model, r, seen=None, limit=200000) -> List[Tuple[float, float, float]]:
    """형상 항목에서 닿는 모든 IfcCartesianPoint (Brep 등의 외접 사각형용)."""
    seen = set() if seen is None else seen
    out = []
    stack = [r]
    while stack and len(seen) < limit:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(reversed(x)); continue
        if isinstance(x, Typed):
            stack.extend(reversed(x[1]) if isinstance(x[1], list) else []); continue
        if not isinstance(x, Ref) or int(x) in seen:
            continue
        seen.add(int(x))
        t, a = md.get(x)
        if t == "IFCCARTESIANPOINT":
            out.append(md.point(x))
        elif t == "IFCCARTESIANPOINTLIST3D" or t == "IFCCARTESIANPOINTLIST2D":
            for c in a[0]:
                c = [float(v) for v in c] + [0.0]
                out.append((c[0], c[1], c[2]))
        else:
            stack.extend(reversed(a))
    return out


def _item_footprint(md: _Model, r, m: L.Affine, depth=0):
    """
    형상 항목 → (평면 점들[원 좌표 단위], 높이|None, 정확도 'exact'|'bbox'|None, 원형 여부).
    m 은 항목 좌표 → 요소 좌표 변환.
    """
    t, a = md.get(r)
    if depth > 16:
        return None, None, None, False
    if t == "IFCEXTRUDEDAREASOLID":
        pm, _pz, ok = md.axis3d(a[1])
        dx, dy, dz = md.direction(a[2], (0.0, 0.0, 1.0))
        if not ok or abs(dx) > 1e-6 or abs(dy) > 1e-6:
            return None, None, None, False      # 수평 압출 → 평면 외형 아님
        prof = _profile_pts(md, a[0])
        if not prof:
            return None, None, None, False
        circle = md.get(a[0])[0].startswith("IFCCIRCLE")
        depth_ = float(a[3]) * abs(dz) / (math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0)
        return L.apply_pts(L.mul(m, pm), prof), depth_, "exact", circle
    if t in ("IFCBOOLEANCLIPPINGRESULT", "IFCBOOLEANRESULT"):
        return _item_footprint(md, a[1], m, depth + 1)
    if t == "IFCMAPPEDITEM":
        st, sa = md.get(a[0])           # IfcRepresentationMap(MappingOrigin, MappedRepresentation)
        tt, ta = md.get(a[1])           # IfcCartesianTransformationOperator3D
        om, _oz, _ok = md.axis3d(sa[0]) if st else (L.IDENT, 0.0, True)
        tm = L.IDENT
        if tt.startswith("IFCCARTESIANTRANSFORMATIONOPERATOR"):
            ax1 = md.direction(ta[0], (1.0, 0.0, 0.0)) if ta else (1.0, 0.0, 0.0)
            o = md.point(ta[2]) if len(ta) > 2 else (0.0, 0.0, 0.0)
            sc = float(ta[3]) if len(ta) > 3 and ta[3] is not None else 1.0
            n = math.hypot(ax1[0], ax1[1]) or 1.0
            c, s = _snap(ax1[0] / n), _snap(ax1[1] / n)
            tm = (c * sc, s * sc, -s * sc, c * sc, o[0], o[1])
            if len(ta) > 5 and ta[5] is not None and tt.endswith("NONUNIFORM3D"):
                md.w("비균등 축척 매핑은 균등 축척으로 근사했다")
        mm = L.mul(m, L.mul(tm, om))
        rt, ra = md.get(sa[1]) if st else ("", [])
        pts, hmax, acc, circ = [], None, "exact", False
        items = ra[3] if rt == "IFCSHAPEREPRESENTATION" and len(ra) > 3 else []
        for it in items:
            p, h, ac, c_ = _item_footprint(md, it, mm, depth + 1)
            if p is None:
                return None, None, None, False
            pts += p
            hmax = h if hmax is None else (max(hmax, h) if h is not None else hmax)
            if ac == "bbox":
                acc = "bbox"
            circ = c_ if len(items) == 1 else False
        if not pts:
            return None, None, None, False
        if len(items) > 1:
            x0, y0, x1, y1 = bbox(pts); pts = L.rect_pts((x0, y0, x1, y1))
        return pts, hmax, acc, circ
    if t == "IFCBOUNDINGBOX":
        x, y, z = md.point(a[0])
        pts = L.rect_pts((x, y, x + float(a[1]), y + float(a[2])))
        return L.apply_pts(m, pts), float(a[3]), "bbox", False
    return None, None, None, False


def _product_footprint(md: _Model, rep_ref, m: L.Affine):
    t, a = md.get(rep_ref)
    if t != "IFCPRODUCTDEFINITIONSHAPE":
        return None, None, None, False, "형상 없음"
    reps = []
    for r in a[2] or []:
        rt, ra = md.get(r)
        if rt == "IFCSHAPEREPRESENTATION":
            ident = (ra[1] or "") if len(ra) > 1 else ""
            reps.append((ident, ra[3] if len(ra) > 3 else [], r))
    order = {"Body": 0, "FootPrint": 1, "Box": 2}
    reps.sort(key=lambda x: (order.get(x[0], 3), int(x[2])))
    for ident, items, _r in reps:
        if ident == "FootPrint":
            pts = []
            for it in items:
                cp = _curve_pts(md, it)
                if cp:
                    pts += L.apply_pts(m, cp)
            if pts:
                return pts, None, "exact", False, ""
            continue
        allp, hmax, acc, circ, fail = [], None, "exact", False, False
        for it in items:
            p, h, ac, c_ = _item_footprint(md, it, m)
            if p is None:
                fail = True
                break
            allp += p
            if h is not None:
                hmax = h if hmax is None else max(hmax, h)
            if ac == "bbox":
                acc = "bbox"
            circ = c_ if len(items) == 1 else False
        if not fail and allp:
            if len(items) > 1:
                x0, y0, x1, y1 = bbox(allp); allp = L.rect_pts((x0, y0, x1, y1))
            return allp, hmax, acc, circ, ""
    # 최후: 면 모델(Brep·삼각망)의 꼭짓점 외접 사각형
    for ident, items, _r in reps:
        if ident not in ("Body", "Facetation", "Reference", ""):
            continue
        mesh = [it for it in items if md.get(it)[0] in MESH_TYPES]
        if not mesh or len(mesh) != len(items):
            continue
        p3 = _all_points(md, mesh)
        if p3:
            xy = L.apply_pts(m, [(p[0], p[1]) for p in p3])
            zs = [p[2] for p in p3]
            x0, y0, x1, y1 = bbox(xy)
            return L.rect_pts((x0, y0, x1, y1)), max(zs) - min(zs), "bbox", False, ""
    return None, None, None, False, "지원하지 않는 형상"


# ---------------------------------------------------------------------------
# 단위
# ---------------------------------------------------------------------------

def _length_unit(md: _Model) -> Tuple[Optional[float], str]:
    for eid in sorted(md.e):
        t, a = md.e[eid]
        if t != "IFCUNITASSIGNMENT":
            continue
        for u in a[0] or []:
            f, desc = _unit_factor(md, u)
            if f is not None:
                return f, desc
    return None, ""


def _unit_factor(md, u) -> Tuple[Optional[float], str]:
    t, a = md.get(u)
    if t == "IFCSIUNIT" and len(a) >= 4 and a[1] == "LENGTHUNIT":
        if a[3] != "METRE":
            return None, ""
        p = a[2]
        f = SI_PREFIX.get(p, 1.0) if p else 1.0
        return f, f"IfcSIUnit LENGTHUNIT {(p + ' ') if p else ''}METRE"
    if t == "IFCCONVERSIONBASEDUNIT" and len(a) >= 4 and a[1] == "LENGTHUNIT":
        mt, ma = md.get(a[3])
        if mt == "IFCMEASUREWITHUNIT":
            v = ma[0][1][0] if isinstance(ma[0], Typed) else ma[0]
            bf, _ = _unit_factor(md, ma[1])
            if bf is not None:
                return float(v) * bf, f"IfcConversionBasedUnit '{a[2]}' = {float(v):g} × SI"
    return None, ""


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def _str(v) -> str:
    if isinstance(v, Typed):
        v = v[1][0] if v[1] else ""
    return v.strip() if isinstance(v, str) and not isinstance(v, Enum) else ""


def _kind_of(t: str) -> Optional[str]:
    if t in ROOM_TYPES:
        return "room"
    if t in OBSTACLE_TYPES:
        return "obstacle"
    if t in OPENING_TYPES:
        return "opening"
    if t in IGNORE_TYPES:
        return "ignore"
    if t in EQUIPMENT_TYPES:
        return "equipment"
    return None


def read(data: bytes, filename: str, opts: dict) -> dict:
    """바이트 → Takeoff. 손상된 값은 ImportErrorUser 로 바꿔 알린다 (내부 예외를 그대로 내보내지 않는다)."""
    try:
        return _read(data, filename, opts)
    except ImportErrorUser:
        raise
    except (ValueError, IndexError, KeyError, TypeError, ZeroDivisionError, RecursionError) as e:
        raise ImportErrorUser(
            f"IFC 파일의 내용을 해석하지 못했다 ({type(e).__name__}: {str(e)[:120]}). "
            f"파일이 손상되었거나 잘렸는지 확인하고, CAD/BIM 프로그램에서 다시 내보낼 것")


def _read(data: bytes, filename: str, opts: dict) -> dict:
    tk = new_takeoff(filename, "ifc", data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    ents = parse_step(text)
    if not ents:
        raise ImportErrorUser("IFC 엔티티를 하나도 읽지 못했다. IFC STEP(.ifc) 파일인지 확인할 것 (ifcXML/ifcZIP 은 미지원)")
    md = _Model(ents)
    layer_map = opts.get("layer_map") or {}
    default_floor = int(opts.get("floor", 0) or 0)

    # ---- 단위 --------------------------------------------------------------
    nf, ndesc = _length_unit(md)
    user = any(opts.get(k) not in (None, "") for k in ("units", "m_per_unit", "px_per_m"))
    if user or nf is None:
        f, basis = resolve_scale(opts, None, "ifc")
    else:
        f, basis = nf, f"IFC {ndesc} → ×{nf:g}"
        if opts.get("scale_ratio") not in (None, "", 1, 1.0):
            tk["warnings"].append("IFC 는 실치수 모델이라 scale_ratio 를 무시했다")
    tk["scale_basis"] = basis

    # ---- 층 ----------------------------------------------------------------
    storeys = []
    for eid in sorted(ents):
        t, a = ents[eid]
        if t == "IFCBUILDINGSTOREY":
            elev = a[9] if len(a) > 9 and isinstance(a[9], (int, float)) else None
            pm, pz, _ok = md.placement(a[5] if len(a) > 5 else None)
            storeys.append((float(elev) if elev is not None else pz, eid, _str(a[2]) or _str(a[7] if len(a) > 7 else None)))
    storeys.sort(key=lambda s: (s[0], s[1]))
    storey_floor = {eid: k for k, (_e, eid, _n) in enumerate(storeys)}
    tk["floors"] = [{"index": k, "label": nm or f"{k+1}F", "elevation_m": round(el * f, 6)}
                    for k, (el, _eid, nm) in enumerate(storeys)]

    parent: Dict[int, int] = {}
    for eid in sorted(ents):
        t, a = ents[eid]
        if t == "IFCRELCONTAINEDINSPATIALSTRUCTURE" and len(a) >= 6:
            for ch in a[4] or []:
                if isinstance(ch, Ref):
                    parent.setdefault(int(ch), int(a[5]))
        elif t == "IFCRELAGGREGATES" and len(a) >= 6:
            for ch in a[5] or []:
                if isinstance(ch, Ref):
                    parent.setdefault(int(ch), int(a[4]))

    def floor_of(eid) -> Optional[int]:
        seen = set()
        cur = eid
        while cur in parent and cur not in seen:
            seen.add(cur)
            cur = parent[cur]
            if cur in storey_floor:
                return storey_floor[cur]
        return None

    # ---- 요소 --------------------------------------------------------------
    skipped: Dict[str, int] = {}
    unknown: Dict[str, int] = {}
    no_floor = 0
    for eid in sorted(ents):
        t, a = ents[eid]
        kind = _kind_of(t)
        name = _str(a[2]) if len(a) > 2 else ""
        layer = f"{t}:{name}" if name else t
        for pat, k in sorted(layer_map.items()):
            if re.search(pat, layer, re.I):
                kind = k
                break
        if kind is None:
            if (t.startswith("IFC") and len(a) > 6 and isinstance(a[6], Ref)
                    and md.get(a[6])[0] == "IFCPRODUCTDEFINITIONSHAPE"
                    and t not in ("IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY", "IFCPROJECT")):
                unknown[t] = unknown.get(t, 0) + 1
            continue
        if kind == "ignore":
            skipped[t] = skipped.get(t, 0) + 1
            continue
        if len(a) < 7:
            continue
        m, z0, ok = md.placement(a[5])
        if not ok:
            md.w("연직이 아닌 배치 축은 평면 투영으로 근사했다")
        pts, h, acc, circle, why = _product_footprint(md, a[6], m)
        guid = _str(a[0])
        if pts is None or len(pts) < 3:
            tk["warnings"].append(f"#{eid} {t} '{name}' — 평면 형상을 얻지 못해 건너뛰었다 ({why})")
            continue
        fl = floor_of(eid)
        if fl is None:
            fl = default_floor
            no_floor += 1
        pm = [(p[0] * f, p[1] * f) for p in pts]
        if kind == "boundary":
            x0, y0, x1, y1 = bbox(pm)
            tk["boundary"].append({"floor": fl, "rect": [round(x0, 6), round(y0, 6),
                                                         round(x1, 6), round(y1, 6)],
                                   "source_ref": f"#{eid}"})
            continue
        it = make_item(kind, fl, pm, t, f"#{eid}")
        if circle:
            it["rect_fill"] = round(math.pi / 4.0, 4)
        tag = _str(a[7]) if t != "IFCSPACE" and len(a) > 7 else ""
        long_name = _str(a[7]) if t == "IFCSPACE" and len(a) > 7 else ""
        obj_type = _str(a[4]) if len(a) > 4 else ""
        labels = []
        for v in (tag, name):
            if v and v not in labels:
                labels.append(v)
        it["labels"] = labels
        it["name"] = long_name or obj_type or ""
        if t == "IFCSPACE" and name:
            it["id"] = _ID_SAFE.sub("-", name).strip("-")
        it["ifc_type"] = t
        it["ifc_guid"] = guid
        if h is not None and h > 0:
            it["h"] = round(h * f, 6)
        if acc == "bbox":
            tk["warnings"].append(f"#{eid} {t} '{name}' — 압출 형상이 아니어서 외접 사각형으로 근사했다")
        tk["items"].append(it)

    if no_floor:
        tk["warnings"].append(f"층 소속(IfcRelContainedInSpatialStructure)이 없는 요소 {no_floor}개를 "
                              f"{default_floor+1}층으로 두었다")
    if skipped:
        tk["stats"]["ifc_ignored"] = {k: skipped[k] for k in sorted(skipped)}
    if unknown:
        tk["warnings"].append("분류하지 않은 IFC 요소 (layer_map 으로 지정 가능, 예 {\"IFCPIPESEGMENT\": \"obstacle\"}): "
                              + ", ".join(f"{k} {unknown[k]}" for k in sorted(unknown)))
    for msg in sorted(md.warn):
        c = md.warn[msg]
        tk["warnings"].append(msg + (f" (×{c})" if c > 1 else ""))

    assign_texts(tk, [])
    boundary_from_items(tk)
    normalize_origin(tk)
    finalize(tk, float(opts.get("min_size_m", 0.3)))
    tk["stats"].update(ifc_entities=len(ents), storeys=len(storeys))
    return tk
