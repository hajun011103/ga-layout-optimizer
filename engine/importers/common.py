# -*- coding: utf-8 -*-
"""
importers/common.py — 도면 → Takeoff(현황 배치) 공통 계약 (결정론적 영역. LLM 미개입)

모든 형식별 파서(dxf/svg/pdf/png/ifc/table)는 같은 모양의 Takeoff dict 를 반환한다.
좌표는 **파서만** 만든다. LLM 은 Takeoff 의 좌표를 쓰거나 고치지 않는다
(불변식 1 — LLM 의 치수 추출 정확도 48.4%).

Takeoff (dict, JSON 직렬화 가능)
--------------------------------
{
  "_schema":  "smr-ga-takeoff/1.0",
  "source":   {"filename": str, "format": "dxf"|"svg"|..., "sha256": str, "bytes": int},
  "units":    "m",                       # 항상 미터. 변환 근거는 scale_basis 에
  "scale_basis": str,                    # "DXF $INSUNITS=4 (mm) → ×0.001" 같은 문장
  "floors":   [{"index": 0, "label": "1F"}],
  "boundary": [{"floor": 0, "rect": [x0, y0, x1, y1], "source_ref": str}],
  "items": [{
      "id": str, "name": str,
      "kind": "equipment" | "room" | "obstacle" | "void" | "opening",
      "floor": int,
      "x": float, "y": float, "w": float, "d": float,   # 좌하단 + 치수 [m], y 상향
      "polygon": [[x, y], ...] | null,                   # 원형상(직사각형이 아니면)
      "rect_fill": float,                                # 폴리곤면적/외접사각형면적 (1.0=직사각형)
      "layer": str | null,
      "labels": [str],                                   # 내부에 있던 문자
      "source_ref": str                                  # 엔티티 핸들/요소 id/픽셀영역
  }],
  "texts":    [{"floor": int, "x": float, "y": float, "text": str}],   # 어느 item 에도 안 들어간 문자
  "warnings": [str],
  "stats":    {...}
}

kind 의미
  equipment : 기기 — 배치 최적화 대상
  room      : 실(室) — 배치 최적화 대상 (실 단위 평면계획)
  obstacle  : 움직일 수 없는 것 (코어·기둥·격납·샤프트) — pinned 로 고정
  void      : 배치 금지 영역 (부지 외부, L자 경계의 빈 모서리) — pinned 로 고정
  opening   : 해치·계단 — FIRE-HATCH 가 화재구역 병합을 판정

파서가 모르는 것은 **모른다고 쓴다** (warnings). 치수를 추정해 채우지 않는다.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

TAKEOFF_SCHEMA = "smr-ga-takeoff/1.0"
KINDS = ("equipment", "room", "obstacle", "void", "opening")


class ImportErrorUser(Exception):
    """사용자가 해결해야 하는 입력 문제 (형식 미지원, 축척 누락 등).
    메시지는 그대로 사용자/LLM 에게 전달된다 — 해결 방법을 포함해 쓸 것."""


# ---------------------------------------------------------------------------
# 단위
# ---------------------------------------------------------------------------

UNIT_TO_M = {
    "m": 1.0, "mm": 0.001, "cm": 0.01, "in": 0.0254, "ft": 0.3048,
    "pt": 0.0254 / 72.0,          # PDF 포인트
    "px": None,                   # 축척이 필요하다
}


def unit_factor(units: Optional[str]) -> Optional[float]:
    if units is None:
        return None
    u = str(units).strip().lower()
    aliases = {"meter": "m", "meters": "m", "millimeter": "mm", "millimeters": "mm",
               "centimeter": "cm", "inch": "in", "inches": "in", "feet": "ft",
               "foot": "ft"}
    u = aliases.get(u, u)
    if u not in UNIT_TO_M:
        raise ImportErrorUser(f"알 수 없는 단위 '{units}'. 사용 가능: m, mm, cm, in, ft")
    return UNIT_TO_M[u]


# ---------------------------------------------------------------------------
# 기하
# ---------------------------------------------------------------------------

def polygon_area(pts: Sequence[Sequence[float]]) -> float:
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for k in range(n):
        x0, y0 = pts[k][0], pts[k][1]
        x1, y1 = pts[(k + 1) % n][0], pts[(k + 1) % n][1]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def bbox(pts: Iterable[Sequence[float]]) -> Tuple[float, float, float, float]:
    xs, ys = [], []
    for p in pts:
        xs.append(p[0]); ys.append(p[1])
    return min(xs), min(ys), max(xs), max(ys)


def point_in_rect(px: float, py: float, r: Sequence[float], tol: float = 1e-9) -> bool:
    x0, y0, x1, y1 = r
    return x0 - tol <= px <= x1 + tol and y0 - tol <= py <= y1 + tol


def point_in_polygon(px: float, py: float, pts: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i][0], pts[i][1]
        xj, yj = pts[j][0], pts[j][1]
        if (yi > py) != (yj > py):
            xc = (xj - xi) * (py - yi) / ((yj - yi) or 1e-300) + xi
            if px < xc:
                inside = not inside
        j = i
    return inside


def rect_contains(outer: Sequence[float], inner: Sequence[float], tol: float = 1e-6) -> bool:
    return (outer[0] - tol <= inner[0] and outer[1] - tol <= inner[1]
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


# ---------------------------------------------------------------------------
# 분류 — 레이어/이름 키워드. 사용자가 layer_map 으로 덮어쓸 수 있다.
# ---------------------------------------------------------------------------

# 순서가 의미 있다: 먼저 맞는 것이 이긴다. 대소문자 무시.
DEFAULT_KIND_RULES: List[Tuple[str, str]] = [
    (r"bound|envelope|outline|site|외곽|외벽선|경계|부지", "boundary"),
    (r"void|keep.?out|no.?go|금지|빈공간", "void"),
    (r"hatch|stair|opening|해치|계단|개구", "opening"),
    (r"core|column|col\b|shaft|contain|obstacle|fixed|기둥|코어|샤프트|격납|고정", "obstacle"),
    (r"room|space|area|실\b|공간|구역", "room"),
    (r"equip|equipment|machine|pump|tank|hx|기기|설비|장비|탱크|펌프", "equipment"),
    (r"wall|벽|dim|치수|text|문자|grid|그리드|axis|anno|hatch_pattern|title|border|도곽|clearance|penetration|violation|fire_zone|turbine", "ignore"),
]


def classify(layer: Optional[str], layer_map: Optional[Dict[str, str]] = None,
             default: str = "equipment") -> str:
    """레이어명 → kind | 'boundary' | 'ignore'. layer_map 은 {정규식: kind}."""
    name = layer or ""
    for pat, kind in (layer_map or {}).items():
        if re.search(pat, name, re.I):
            return kind
    for pat, kind in DEFAULT_KIND_RULES:
        if re.search(pat, name, re.I):
            return kind
    return default


FLOOR_PATTERNS = [
    (re.compile(r"FLOOR[_\- ]?0*(\d+)", re.I), 0),     # 이 엔진의 DXF 출력 (FLOOR_01 → 0)
    (re.compile(r"(?<![A-Z0-9])B(\d+)F?(?![A-Z0-9])", re.I), "basement"),
    (re.compile(r"(?<![A-Z0-9])(\d+)\s*F(?![A-Z0-9])", re.I), 0),
    (re.compile(r"(\d+)\s*층"), 0),
    (re.compile(r"LEVEL[_\- ]?0*(\d+)", re.I), 0),
]


def floor_from_name(name: Optional[str]) -> Optional[int]:
    """'FLOOR_02_EQUIPMENT' → 1, '3F' → 2, '2층' → 1. 지하층은 음수 대신 None(사람 확인)."""
    if not name:
        return None
    for rx, mode in FLOOR_PATTERNS:
        m = rx.search(name)
        if m:
            if mode == "basement":
                return None
            return max(0, int(m.group(1)) - 1)
    return None


# ---------------------------------------------------------------------------
# 조립
# ---------------------------------------------------------------------------

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def new_takeoff(filename: str, fmt: str, data: bytes) -> dict:
    return {
        "_schema": TAKEOFF_SCHEMA,
        "source": {"filename": filename, "format": fmt,
                   "sha256": sha256_bytes(data), "bytes": len(data)},
        "units": "m",
        "scale_basis": "",
        "floors": [],
        "boundary": [],
        "items": [],
        "texts": [],
        "warnings": [],
        "stats": {},
    }


_ID_SAFE = re.compile(r"[^A-Za-z0-9_\-]+")


def make_item(kind: str, floor: int, pts: Sequence[Sequence[float]],
              layer: Optional[str] = None, source_ref: str = "",
              name: Optional[str] = None) -> dict:
    """폴리곤(또는 사각형 꼭짓점) → item. 좌표는 이미 미터여야 한다."""
    x0, y0, x1, y1 = bbox(pts)
    w, d = x1 - x0, y1 - y0
    area = polygon_area(pts) if len(pts) >= 3 else w * d
    fill = area / (w * d) if w * d > 0 else 0.0
    is_rect = fill > 0.995 and len(pts) <= 5
    return {
        "id": "", "name": name or "",
        "kind": kind, "floor": int(floor),
        "x": round(x0, 6), "y": round(y0, 6), "w": round(w, 6), "d": round(d, 6),
        "polygon": None if is_rect else [[round(p[0], 6), round(p[1], 6)] for p in pts],
        "rect_fill": round(fill, 4),
        "layer": layer, "labels": [], "source_ref": source_ref,
    }


def assign_texts(tk: dict, texts: List[dict]) -> None:
    """
    문자를 가장 작은 포함 item 에 붙인다. 포함 item 이 없으면 tk['texts'] 에 남긴다.
    결정론: 문자 입력 순서, 면적 오름차순·좌표 순 tie-break.
    """
    items = tk["items"]
    order = sorted(range(len(items)),
                   key=lambda k: (items[k]["w"] * items[k]["d"], items[k]["floor"],
                                  items[k]["x"], items[k]["y"]))
    for t in texts:
        hit = None
        for k in order:
            it = items[k]
            if it["floor"] != t.get("floor", 0):
                continue
            r = (it["x"], it["y"], it["x"] + it["w"], it["y"] + it["d"])
            if not point_in_rect(t["x"], t["y"], r):
                continue
            if it["polygon"] and not point_in_polygon(t["x"], t["y"], it["polygon"]):
                continue
            hit = it
            break
        s = str(t["text"]).strip()
        if not s:
            continue
        if hit is not None:
            hit["labels"].append(s)
        else:
            tk["texts"].append({"floor": t.get("floor", 0), "x": round(t["x"], 6),
                                "y": round(t["y"], 6), "text": s})


TAG_RX = re.compile(r"^[A-Z]{1,6}[\-_ ]?\d{1,4}[A-Z]?$")
IDENT_RX = re.compile(r"^[A-Z][A-Z0-9]*(?:[\-_.][A-Z0-9]+){0,4}$")


def finalize(tk: dict, min_size_m: float = 0.3) -> dict:
    """
    공통 후처리 (결정론적):
      - 너무 작은 조각 제거 (min_size_m 미만) → warnings
      - id/name 부여: 라벨 중 태그형(P-101) → id, 나머지 → name. 없으면 kind+순번
      - 중복 id 는 -2, -3 … 접미
      - floors 목록 채우기, stats 계산
      - 정렬: floor, y, x  (파일 내 엔티티 순서에 결과가 의존하지 않게)
    """
    keep, dropped = [], 0
    for it in tk["items"]:
        if it["w"] < min_size_m or it["d"] < min_size_m:
            dropped += 1
            continue
        keep.append(it)
    if dropped:
        tk["warnings"].append(f"{min_size_m} m 미만 조각 {dropped}개를 버렸다 (선·해치 조각으로 판단)")
    keep.sort(key=lambda it: (it["floor"], round(it["y"], 3), round(it["x"], 3),
                              it["kind"], it["source_ref"]))

    used: Dict[str, int] = {}
    counters: Dict[str, int] = {}
    for it in keep:
        labels = [l for l in it["labels"] if l.strip()]
        tag = next((l for l in labels if TAG_RX.match(l.strip().upper())), None)
        if tag is None:
            # 태그형(P-101)이 없으면 식별자처럼 생긴 첫 라벨(SWGR-A, MCR, SFP-HX-1)을 쓴다.
            tag = next((l for l in labels if IDENT_RX.match(l.strip())), None)
        if not it["id"]:
            if tag:
                it["id"] = _ID_SAFE.sub("-", tag.strip().upper()).strip("-")
            else:
                pref = {"equipment": "EQ", "room": "RM", "obstacle": "FX",
                        "void": "VOID", "opening": "OPN"}.get(it["kind"], "IT")
                counters[pref] = counters.get(pref, 0) + 1
                it["id"] = f"{pref}{counters[pref]:03d}"
        if not it["name"]:
            rest = [l for l in labels if l != tag]
            it["name"] = " / ".join(rest) if rest else (tag or it["id"])
        base = it["id"]
        if base in used:
            used[base] += 1
            it["id"] = f"{base}-{used[base]}"
        else:
            used[base] = 1
        if it["polygon"] is not None and it["rect_fill"] < 0.85:
            tk["warnings"].append(
                f"[{it['id']}] 직사각형이 아님 (채움률 {it['rect_fill']:.0%}) — "
                f"외접 사각형 {it['w']:.2f}×{it['d']:.2f} m 로 근사한다")
    tk["items"] = keep

    floors = sorted({it["floor"] for it in keep} | {b["floor"] for b in tk["boundary"]})
    known = {f["index"]: f for f in tk["floors"]}
    tk["floors"] = [known.get(f, {"index": f, "label": f"{f+1}F"}) for f in floors]

    by_kind: Dict[str, int] = {}
    for it in keep:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    tk["stats"].update(items=len(keep), by_kind=by_kind, floors=len(floors),
                       unassigned_texts=len(tk["texts"]))
    if not keep:
        tk["warnings"].append("인식된 기기/실이 0개 — 레이어 분류(layer_map)나 축척을 확인할 것")
    return tk


def _floor_boxes(tk: dict) -> Dict[int, Tuple[float, float, float, float]]:
    out: Dict[int, List[Tuple[float, float]]] = {}
    for b in tk["boundary"]:
        r = b["rect"]; out.setdefault(b["floor"], []).extend([(r[0], r[1]), (r[2], r[3])])
    has_b = set(out)
    for it in tk["items"]:
        if it["floor"] in has_b:
            continue
        out.setdefault(it["floor"], []).extend([(it["x"], it["y"]), (it["x"] + it["w"], it["y"] + it["d"])])
    return {f: bbox(p) for f, p in out.items() if p}


def _shift(tk: dict, f: Optional[int], ox: float, oy: float) -> None:
    for it in tk["items"]:
        if f is not None and it["floor"] != f:
            continue
        it["x"] = round(it["x"] - ox, 6); it["y"] = round(it["y"] - oy, 6)
        if it["polygon"]:
            it["polygon"] = [[round(p[0] - ox, 6), round(p[1] - oy, 6)] for p in it["polygon"]]
    for b in tk["boundary"]:
        if f is not None and b["floor"] != f:
            continue
        r = b["rect"]
        b["rect"] = [round(r[0] - ox, 6), round(r[1] - oy, 6), round(r[2] - ox, 6), round(r[3] - oy, 6)]
    for t in tk["texts"]:
        if f is not None and t.get("floor", 0) != f:
            continue
        t["x"] = round(t["x"] - ox, 6); t["y"] = round(t["y"] - oy, 6)


def normalize_origin(tk: dict) -> None:
    """
    원점을 (경계 또는 전체 item) 좌하단으로 옮긴다. 엔진 좌표계는 (0,0) 기준이다.

    여러 층을 한 장에 '나란히' 그린 도면(층별 외곽이 서로 겹치지 않음)은 층마다
    따로 원점을 맞춘다. 층이 같은 자리에 겹쳐 그려진 도면(일반적인 CAD 층별 레이어)은
    공통 원점을 쓴다 — 그래야 다층 관통 기기의 상·하층 좌표가 유지된다.
    """
    boxes = _floor_boxes(tk)
    bfloors = {b["floor"] for b in tk["boundary"]}
    # 층마다 외곽(경계) 도형이 있을 때만 '나란히 그린 도면' 으로 본다. 경계가 없는
    # 목록표·도면은 좌표가 공통 기준이라고 가정한다 (우연히 떨어져 있어도 옮기지 않는다).
    if len(boxes) > 1 and set(boxes) <= bfloors:
        fl = sorted(boxes)
        disjoint = all(not (boxes[a][0] < boxes[b][2] and boxes[b][0] < boxes[a][2]
                            and boxes[a][1] < boxes[b][3] and boxes[b][1] < boxes[a][3])
                       for i, a in enumerate(fl) for b in fl[i + 1:])
        if disjoint:
            shifts = {}
            for f in fl:
                ox, oy = boxes[f][0], boxes[f][1]
                _shift(tk, f, ox, oy)
                shifts[str(f)] = [round(ox, 6), round(oy, 6)]
            tk["stats"]["origin_shift_m"] = shifts
            tk["warnings"].append("층별 평면이 한 장에 나란히 그려져 있어 층마다 원점을 따로 맞췄다 "
                                  "— 층 간 수직 정렬(관통부)은 도면 원점 기준이 아니다")
            return
    _normalize_global(tk)


def _normalize_global(tk: dict) -> None:
    pts = []
    for b in tk["boundary"]:
        r = b["rect"]; pts += [(r[0], r[1]), (r[2], r[3])]
    if not pts:
        for it in tk["items"]:
            pts += [(it["x"], it["y"]), (it["x"] + it["w"], it["y"] + it["d"])]
    if not pts:
        return
    ox, oy, _, _ = bbox(pts)
    if abs(ox) < 1e-9 and abs(oy) < 1e-9:
        return
    for it in tk["items"]:
        it["x"] = round(it["x"] - ox, 6); it["y"] = round(it["y"] - oy, 6)
        if it["polygon"]:
            it["polygon"] = [[round(p[0] - ox, 6), round(p[1] - oy, 6)] for p in it["polygon"]]
    for b in tk["boundary"]:
        r = b["rect"]
        b["rect"] = [round(r[0] - ox, 6), round(r[1] - oy, 6),
                     round(r[2] - ox, 6), round(r[3] - oy, 6)]
    for t in tk["texts"]:
        t["x"] = round(t["x"] - ox, 6); t["y"] = round(t["y"] - oy, 6)
    tk["stats"]["origin_shift_m"] = [round(ox, 6), round(oy, 6)]


def boundary_from_items(tk: dict) -> None:
    """경계가 없으면 boundary 를 비워 두고 경고만 한다 (추정하지 않는다)."""
    if not tk["boundary"]:
        tk["warnings"].append(
            "외곽(경계) 도형을 찾지 못했다 — '현재 공간' 한계를 모르므로 현황 외피는 "
            "기기 외접 사각형으로 계산된다. 경계 레이어가 있으면 layer_map 으로 지정할 것")


def resolve_scale(opts: dict, native_units: Optional[str], fmt: str) -> Tuple[float, str]:
    """
    도면 1단위 → m 배율과 그 근거 문장. 근거 없이 축척을 추정하지 않는다.

    우선순위: m_per_unit > px_per_m > units(사용자) > native_units(파일 헤더) ; × scale_ratio
    축척을 정할 수 없으면 ImportErrorUser — 사용자에게 물어야 한다.
    """
    ratio = opts.get("scale_ratio")
    ratio = float(ratio) if ratio not in (None, "") else 1.0
    if ratio <= 0:
        raise ImportErrorUser("scale_ratio 는 양수여야 한다 (1:100 도면이면 100)")
    if opts.get("m_per_unit") not in (None, ""):
        f = float(opts["m_per_unit"])
        if f <= 0:
            raise ImportErrorUser("m_per_unit 은 양수여야 한다")
        return f, f"사용자 지정 m_per_unit={f:g}"
    if opts.get("px_per_m") not in (None, ""):
        p = float(opts["px_per_m"])
        if p <= 0:
            raise ImportErrorUser("px_per_m 은 양수여야 한다")
        return 1.0 / p, f"사용자 지정 px_per_m={p:g} → 1 px = {1.0/p:g} m"
    for src, u in (("사용자 지정 units", opts.get("units")), ("파일 헤더", native_units)):
        if u:
            f = unit_factor(u)
            if f is None:
                continue
            basis = f"{src} '{u}' → ×{f:g}"
            if ratio != 1.0:
                basis += f", 축척 1:{ratio:g} → ×{ratio:g}"
            return f * ratio, basis
    raise ImportErrorUser(
        f"{fmt.upper()} 도면의 축척을 알 수 없다. 다음 중 하나를 지정할 것: "
        f"units(m/mm/cm/in/ft), scale_ratio(종이 축척 1:N 의 N), m_per_unit, px_per_m. "
        f"축척은 추정하지 않는다 — 틀린 축척은 모든 치수를 같은 비율로 틀리게 만든다.")
