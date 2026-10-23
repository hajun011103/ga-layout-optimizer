# -*- coding: utf-8 -*-
"""
importers/table.py — 기기 목록표 → Takeoff (결정론적, 표준 라이브러리만)

형식
  CSV/TSV : 구분자 자동 판정 (, \\t ; |). 인코딩 utf-8-sig → cp949
  XLSX    : zipfile 로 직접 읽는다 (첫 번째 시트, 공유 문자열·인라인 문자열·숫자)
  JSON    : 행 목록 [{...}], IR {"blocks": [...]}, 이 엔진의 결과 JSON (blocks 에 x,y,w,d,floor)

머리글 별칭 (대소문자 무시, 한글 포함)
  id/tag/태그/기기번호 · name/이름/명칭 · w/width/폭/가로 · d/depth/깊이/세로/length
  h/height/높이 · x · y (cx/cy/center → 중심 좌표) · floor/층/level · zone/구역
  kind/종류 · clearance/이격 · weight/중량
단위
  머리글의 단위 표기 "w(mm)", "폭[m]", "x_mm" 가 열마다 우선한다. 없으면 units 옵션이 필요하다.
  IR/결과 JSON 은 스키마상 m 이다.
치수(w, d)가 없는 행은 만들지 않고 stats.rows_missing_dims 에 남긴다 (추정 금지).
좌표가 없으면 item 은 x=y=0, unplaced=True 이고 원점 정규화를 하지 않는다.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from .common import (KINDS, ImportErrorUser, boundary_from_items, classify, finalize,
                     floor_from_name, make_item, new_takeoff, normalize_origin, resolve_scale,
                     unit_factor)

ALIASES = {
    "id": ["id", "tag", "tagno", "tagnumber", "태그", "태그번호", "기기번호", "equipid",
           "equipmentid", "itemid", "기기id"],
    "name": ["name", "이름", "명칭", "기기명", "품명", "description", "desc", "설명"],
    "w": ["w", "width", "폭", "가로", "너비", "가로길이"],
    "d": ["d", "depth", "깊이", "세로", "length", "길이", "l", "세로길이"],
    "h": ["h", "height", "높이"],
    "x": ["x", "x좌표", "posx", "xpos", "left", "x0"],
    "y": ["y", "y좌표", "posy", "ypos", "bottom", "y0"],
    "cx": ["cx", "centerx", "xcenter", "중심x", "x중심", "centrex"],
    "cy": ["cy", "centery", "ycenter", "중심y", "y중심", "centrey"],
    "floor": ["floor", "층", "level", "storey", "story", "층번호", "기준층"],
    "zone": ["zone", "구역", "방사선구역", "radzone"],
    "kind": ["kind", "종류", "구분", "category"],
    "clearance": ["clearance", "이격", "이격거리", "유지보수이격"],
    "weight": ["weight", "중량", "무게", "mass"],
}
LENGTH_FIELDS = ("w", "d", "h", "x", "y", "cx", "cy", "clearance")
EXTRA_FIELDS = ("zone", "clearance", "h", "weight")
_ALIAS_INDEX = {a: k for k, al in ALIASES.items() for a in al}
_UNIT_RX = re.compile(r"\s*[\(\[\{]\s*([A-Za-z㎜㎝]+)\s*[\)\]\}]\s*$|[\s_\-]+(mm|cm|m|in|ft|ton|kg|t)$",
                      re.I)
_WEIGHT_UNITS = {"t": 1.0, "ton": 1.0, "tonne": 1.0, "kg": 0.001}


def _norm(h: str) -> Tuple[str, Optional[str]]:
    """머리글 → (별칭 키 후보 문자열, 단위 표기|None)."""
    s = str(h or "").strip().replace("﻿", "")
    unit = None
    m = _UNIT_RX.search(s)
    if m:
        unit = (m.group(1) or m.group(2)).lower().replace("㎜", "mm").replace("㎝", "cm")
        s = s[:m.start()]
    key = re.sub(r"[\s_\-\.:/]+", "", s).lower()
    return key, unit


def _map_header(row: List[str]) -> Tuple[Dict[str, int], Dict[str, str]]:
    cols: Dict[str, int] = {}
    units: Dict[str, str] = {}
    for j, h in enumerate(row):
        key, unit = _norm(h)
        f = _ALIAS_INDEX.get(key)
        if f is None or f in cols:
            continue
        cols[f] = j
        if unit:
            units[f] = unit
    return cols, units


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------

def _decode(data: bytes) -> Tuple[str, str]:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise ImportErrorUser("표 파일의 문자 인코딩을 판정하지 못했다 (UTF-8 또는 CP949 로 저장할 것)")


def _read_csv(data: bytes, filename: str) -> Tuple[List[List[str]], str]:
    text, enc = _decode(data)
    sample = "\n".join(text.splitlines()[:20])
    if filename.lower().endswith(".tsv"):
        delim = "\t"
    else:
        counts = {d: sample.splitlines()[0].count(d) if sample else 0 for d in (",", "\t", ";", "|")}
        delim = max(sorted(counts), key=lambda d: (counts[d], -",\t;|".index(d)))
        if counts[delim] == 0:
            delim = ","
    rows = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
    return rows, f"{enc}, 구분자 {delim!r}"


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def _read_xlsx(data: bytes) -> Tuple[List[List], str]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportErrorUser("XLSX(zip) 파일을 열지 못했다. 엑셀에서 .xlsx 로 다시 저장할 것")
    names = set(z.namelist())

    def xml(name):
        info = z.getinfo(name)
        if info.file_size > 200 * 1024 * 1024:
            raise ImportErrorUser(f"XLSX 내부 파일 {name} 이 너무 크다")
        return ET.fromstring(z.read(name))

    shared: List[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in xml("xl/sharedStrings.xml").findall("m:si", _NS):
            # rPh(후리가나) 안의 t 는 뺀다
            txt = []
            for ch in si:
                tag = ch.tag.rsplit("}", 1)[-1]
                if tag == "t":
                    txt.append(ch.text or "")
                elif tag == "r":
                    t = ch.find("m:t", _NS)
                    txt.append((t.text or "") if t is not None else "")
            shared.append("".join(txt))
    sheet_path = None
    sheet_name = ""
    if "xl/workbook.xml" in names:
        wb = xml("xl/workbook.xml")
        sh = wb.find("m:sheets/m:sheet", _NS)
        if sh is not None:
            sheet_name = sh.get("name", "")
            rid = sh.get(f"{{{_RNS}}}id")
            if rid and "xl/_rels/workbook.xml.rels" in names:
                for rel in xml("xl/_rels/workbook.xml.rels"):
                    if rel.get("Id") == rid:
                        tgt = rel.get("Target", "")
                        sheet_path = tgt.lstrip("/") if tgt.startswith("/") else "xl/" + tgt
                        break
    if sheet_path is None or sheet_path not in names:
        cands = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not cands:
            raise ImportErrorUser("XLSX 에서 워크시트를 찾지 못했다")
        sheet_path = sorted(cands, key=lambda n: int(re.findall(r"\d+", n)[-1]))[0]
    ws = xml(sheet_path)
    rows: Dict[int, Dict[int, object]] = {}
    for rn, row in enumerate(ws.iter(f"{{{_NS['m']}}}row")):
        r = int(row.get("r", rn + 1))
        cells: Dict[int, object] = {}
        for k, c in enumerate(row.findall("m:c", _NS)):
            ref = c.get("r")
            j = _col_index(ref) if ref else k
            t = c.get("t", "n")
            v = c.find("m:v", _NS)
            val: object = None
            if t == "s" and v is not None:
                idx = int(v.text)
                val = shared[idx] if 0 <= idx < len(shared) else ""
            elif t == "inlineStr":
                is_ = c.find("m:is", _NS)
                val = "".join((x.text or "") for x in is_.iter(f"{{{_NS['m']}}}t")) if is_ is not None else ""
            elif t in ("str", "e"):
                val = v.text if v is not None else ""
            elif t == "b":
                val = (v.text == "1") if v is not None else None
            elif v is not None and v.text is not None:
                try:
                    fv = float(v.text)
                    val = int(fv) if fv.is_integer() and "." not in v.text and "E" not in v.text.upper() else fv
                except ValueError:
                    val = v.text
            cells[j] = val
        rows[r] = cells
    out: List[List] = []
    if rows:
        ncol = max((max(c) + 1 for c in rows.values() if c), default=0)
        for r in range(1, max(rows) + 1):
            c = rows.get(r, {})
            out.append([c.get(j) for j in range(ncol)])
    return out, f"XLSX 시트 '{sheet_name or sheet_path}'"


# ---------------------------------------------------------------------------
# 행 → item
# ---------------------------------------------------------------------------

def _find_header(rows: List[List]) -> int:
    best, best_n = -1, 0
    for k, r in enumerate(rows[:15]):
        cols, _u = _map_header([("" if v is None else str(v)) for v in r])
        n = len(cols) + (2 if "w" in cols and "d" in cols else 0)
        if n > best_n:
            best, best_n = k, n
    if best < 0 or best_n < 3:
        raise ImportErrorUser(
            "표의 머리글 행을 찾지 못했다. 첫 행에 id, name, w, d (또는 태그/명칭/폭/깊이) "
            "열 이름을 둘 것")
    return best


def _rows_from_grid(rows: List[List]) -> Tuple[List[Tuple[int, Dict[str, object]]], Dict[str, str], List[str]]:
    hk = _find_header(rows)
    header = [("" if v is None else str(v)) for v in rows[hk]]
    cols, units = _map_header(header)
    out = []
    for k in range(hk + 1, len(rows)):
        r = rows[k]
        if all(v is None or str(v).strip() == "" for v in r):
            continue
        rec = {f: (r[j] if j < len(r) else None) for f, j in cols.items()}
        out.append((k + 1, rec))
    unmapped = [h for j, h in enumerate(header) if h.strip() and j not in cols.values()]
    return out, units, unmapped


def _rows_from_dicts(lst: List[dict]) -> Tuple[List[Tuple[int, Dict[str, object]]], Dict[str, str], List[str]]:
    keys: List[str] = []
    for d in lst:
        if isinstance(d, dict):
            for k in d:
                if k not in keys:
                    keys.append(k)
    cols, units = _map_header(keys)
    out = []
    for n, d in enumerate(lst):
        if not isinstance(d, dict):
            continue
        out.append((n + 1, {f: d.get(keys[j]) for f, j in cols.items()}))
    unmapped = [k for j, k in enumerate(keys) if j not in cols.values()]
    return out, units, unmapped


def _floor_value(v, base: int) -> Optional[int]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    n = _num(v)
    if n is not None and float(n).is_integer():
        return max(0, int(n) - base)
    return floor_from_name(str(v))


def read(data: bytes, filename: str, opts: dict) -> dict:
    """바이트 → Takeoff. 손상된 값은 ImportErrorUser 로 바꿔 알린다 (내부 예외를 그대로 내보내지 않는다)."""
    try:
        return _read(data, filename, opts)
    except ImportErrorUser:
        raise
    except (ValueError, IndexError, KeyError, TypeError, ZeroDivisionError, RecursionError) as e:
        raise ImportErrorUser(
            f"표(CSV/XLSX/JSON) 파일의 내용을 해석하지 못했다 ({type(e).__name__}: {str(e)[:120]}). "
            f"파일이 손상되었거나 잘렸는지 확인하고, CAD/BIM 프로그램에서 다시 내보낼 것")


def _read(data: bytes, filename: str, opts: dict) -> dict:
    tk = new_takeoff(filename, "table", data)
    low = filename.lower()
    json_mode = None
    native_m = False
    envelope = None
    if data[:4] == b"PK\x03\x04":
        grid, src = _read_xlsx(data)
        recs, units, unmapped = _rows_from_grid(grid)
    else:
        txt = data.decode("utf-8-sig", "ignore").lstrip()
        if txt[:1] in "[{" and not low.endswith((".csv", ".tsv")):
            try:
                obj = json.loads(data.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError) as e:
                raise ImportErrorUser(f"JSON 을 해석하지 못했다: {e}")
            if isinstance(obj, dict) and isinstance(obj.get("blocks"), list):
                lst = obj["blocks"]
                native_m = True
                json_mode = "result" if ("envelope" in obj or any(
                    isinstance(b, dict) and "x" in b for b in lst)) else "ir"
                env = obj.get("envelope") or {}
                if json_mode == "result" and env.get("width_m") and env.get("depth_m"):
                    envelope = (float(env["width_m"]), float(env["depth_m"]))
            elif isinstance(obj, list):
                lst = obj; json_mode = "rows"
            elif isinstance(obj, dict) and isinstance(obj.get("items"), list):
                lst = obj["items"]; json_mode = "rows"
            else:
                raise ImportErrorUser("JSON 은 행 목록 [...] 이거나 {\"blocks\": [...]} 여야 한다")
            recs, units, unmapped = _rows_from_dicts(lst)
            src = f"JSON ({json_mode})"
        else:
            grid, src = _read_csv(data, filename)
            recs, units, unmapped = _rows_from_grid(grid)

    # ---- 단위 --------------------------------------------------------------
    user = any(opts.get(k) not in (None, "") for k in ("units", "m_per_unit", "px_per_m"))
    if native_m and not user:
        base_f, basis = 1.0, "엔진 IR/결과 JSON — 스키마상 m → ×1"
    else:
        used = [fl for fl in LENGTH_FIELDS if any(r.get(fl) not in (None, "") for _n, r in recs)]
        need_default = [fl for fl in used if fl not in units]
        if need_default or user:
            o = dict(opts)
            o.pop("scale_ratio", None)
            try:
                base_f, basis = resolve_scale(o, None, "table")
            except ImportErrorUser:
                raise ImportErrorUser(
                    "기기 목록표의 길이 단위를 알 수 없다 (열 " + ", ".join(need_default) +
                    "). units 옵션(m/mm/cm/in/ft)을 주거나 머리글에 단위를 적을 것 "
                    "(예: 'w(mm)', '폭[m]'). 단위는 추정하지 않는다.")
        else:
            base_f, basis = None, ""
    col_f: Dict[str, float] = {}
    parts = []
    for fl in LENGTH_FIELDS:
        if fl in units:
            uf = unit_factor(units[fl]) if units[fl] in ("m", "mm", "cm", "in", "ft") else None
            if uf is None:
                raise ImportErrorUser(f"열 '{fl}' 의 단위 표기 '{units[fl]}' 를 모른다 (m/mm/cm/in/ft)")
            col_f[fl] = uf
            parts.append(f"{fl}[{units[fl]}]")
        elif base_f is not None:
            col_f[fl] = base_f
    tk["scale_basis"] = "; ".join(([basis] if basis else []) + (["머리글 단위 " + ", ".join(parts)] if parts else []))
    if not tk["scale_basis"]:
        tk["scale_basis"] = "길이 열 없음"
    w_unit = units.get("weight")
    w_f = _WEIGHT_UNITS.get(w_unit) if w_unit else None
    if w_unit and w_f is None:
        raise ImportErrorUser(f"중량 단위 '{w_unit}' 를 모른다 (ton, t, kg)")
    if any(r.get("weight") not in (None, "") for _n, r in recs) and not w_unit and not native_m:
        tk["warnings"].append("중량 열에 단위 표기가 없어 값을 그대로 옮겼다 (IR 스키마 단위는 ton)")

    # ---- 층 번호 기준 ------------------------------------------------------
    nums = [_num(r.get("floor")) for _n, r in recs if _num(r.get("floor")) is not None]
    if "floor_base" in opts:
        base = int(opts["floor_base"])
        tk["warnings"].append(f"층 번호를 floor_base={base} 기준으로 읽었다")
    elif native_m:
        base = 0
    elif nums:
        base = 0 if min(nums) <= 0 else 1
        tk["warnings"].append(
            "층 번호 해석: " + ("0 이 있어 0 = 1층" if base == 0 else "0 이 없어 1 = 1층") +
            " 으로 읽었다. 다르면 floor_base 옵션(0 또는 1)을 줄 것")
    else:
        base = 1
    default_floor = int(opts.get("floor", 0) or 0)

    # ---- 행 → item ---------------------------------------------------------
    missing: List[dict] = []
    has_pos_all = True
    any_pos = False
    for rn, r in recs:
        rid = str(r.get("id") or "").strip()
        nm = str(r.get("name") or "").strip()
        w = _num(r.get("w")); d = _num(r.get("d"))
        if w is None or d is None or w <= 0 or d <= 0:
            missing.append({"row": rn, "id": rid, "name": nm,
                            "missing": [k for k, v in (("w", w), ("d", d)) if v is None or v <= 0]})
            continue
        w *= col_f.get("w", 1.0); d *= col_f.get("d", 1.0)
        x = _num(r.get("x")); y = _num(r.get("y"))
        cx = _num(r.get("cx")); cy = _num(r.get("cy"))
        if x is not None and y is not None:
            x *= col_f["x"]; y *= col_f["y"]
            placed = True
        elif cx is not None and cy is not None:
            x = cx * col_f["cx"] - w / 2.0; y = cy * col_f["cy"] - d / 2.0
            placed = True
        else:
            x = y = 0.0
            placed = False
        has_pos_all = has_pos_all and placed
        any_pos = any_pos or placed
        fl = _floor_value(r.get("floor"), base)
        if fl is None:
            fl = default_floor
        kind = "equipment"
        kv = str(r.get("kind") or "").strip()
        if kv:
            kind = kv.lower() if kv.lower() in KINDS + ("boundary", "ignore") else classify(kv, opts.get("layer_map"))
        if kind == "ignore":
            continue
        ref = f"row:{rn:05d}"
        if kind == "boundary":
            if placed:
                tk["boundary"].append({"floor": fl, "rect": [round(x, 6), round(y, 6),
                                                             round(x + w, 6), round(y + d, 6)],
                                       "source_ref": ref})
            else:
                tk["warnings"].append(f"{ref}: 경계 행에 좌표가 없어 무시했다")
            continue
        it = make_item(kind, fl, [(x, y), (x + w, y), (x + w, y + d), (x, y + d)], None, ref,
                       name=nm or None)
        if rid:
            it["id"] = re.sub(r"\s+", "-", rid)
            it["labels"] = [rid]
        elif nm:
            it["labels"] = [nm]
        for k in EXTRA_FIELDS:
            v = r.get(k)
            if v in (None, ""):
                continue
            if k in ("clearance", "h"):
                nv = _num(v)
                if nv is not None:
                    it[k] = round(nv * col_f.get(k, 1.0), 6)
            elif k == "weight":
                nv = _num(v)
                if nv is not None:
                    it[k] = round(nv * (w_f or 1.0), 6)
            else:
                it[k] = str(v).strip()
        if not placed:
            it["unplaced"] = True
        tk["items"].append(it)

    if json_mode == "result" and envelope:
        for fl in sorted({it["floor"] for it in tk["items"]}):
            tk["boundary"].append({"floor": fl, "rect": [0.0, 0.0, envelope[0], envelope[1]],
                                   "source_ref": "envelope"})

    tk["stats"]["has_positions"] = bool(tk["items"]) and has_pos_all
    tk["stats"]["rows_missing_dims"] = missing
    tk["stats"]["source"] = src
    if unmapped:
        tk["stats"]["unmapped_columns"] = unmapped
    if missing:
        tk["warnings"].append(
            f"치수(w/d)가 없는 행 {len(missing)}개는 만들지 않았다 — 행 "
            + ", ".join(str(m['row']) for m in missing[:20]) + (" …" if len(missing) > 20 else "")
            + ". 치수는 추정하지 않는다")
    if tk["items"] and not has_pos_all:
        tk["warnings"].append(
            ("일부 " if any_pos else "") + "행에 좌표(x,y)가 없다 — 목록으로만 쓰인다 (unplaced). "
            "현황 배치가 아니므로 원점 정규화를 하지 않았다")
    if tk["stats"]["has_positions"]:
        if not tk["boundary"]:
            boundary_from_items(tk)
        normalize_origin(tk)
    finalize(tk, float(opts.get("min_size_m", 0.0)))
    return tk
