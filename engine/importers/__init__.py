# -*- coding: utf-8 -*-
"""
importers — 도면 파일 → Takeoff(현황 배치). 결정론적, 표준 라이브러리만.

  from importers import import_file
  tk = import_file(data_bytes, "plant.dxf", {"units": "mm"})

형식은 확장자가 아니라 **내용(매직 바이트)** 으로 판정한다. 확장자는 보조 단서다.

공통 옵션 (opts)
  units        : 도면 단위 덮어쓰기 — m | mm | cm | in | ft | pt
  scale_ratio  : 축척 분모. 종이 도면(PDF/이미지)이 1:100 이면 100
  m_per_unit   : 도면 1단위가 몇 m 인가 (가장 직접적인 지정. 다른 축척 옵션보다 우선)
  px_per_m     : 래스터/SVG 에서 1 m 가 몇 px 인가
  floor        : 층 정보가 없는 단일 평면도의 층 번호 (0 = 1층)
  layer_map    : {정규식: kind} — 레이어/그룹/색 이름 → equipment|room|obstacle|void|opening|boundary|ignore
  page         : PDF 페이지 번호(1부터) 또는 "all"
  min_size_m   : 이보다 작은 조각은 버린다 (기본 0.3 m)
"""
from __future__ import annotations

import os
from typing import Callable, Dict, Optional

from .common import ImportErrorUser, unit_factor, finalize, normalize_origin  # noqa: F401

SUPPORTED = [
    dict(format="dxf", ext=[".dxf"], note="ASCII DXF R12~2018. LWPOLYLINE/POLYLINE/LINE/INSERT(블록)/TEXT/MTEXT/HATCH 경계. 레이어로 층·종류 판정. $INSUNITS 로 단위 자동."),
    dict(format="svg", ext=[".svg"], note="rect/polygon/polyline/path/text/g(transform). 단위: width/height 의 mm·cm, 또는 px_per_m 지정."),
    dict(format="pdf", ext=[".pdf"], note="벡터 PDF (CAD 출력). 경로 연산자(re/m/l/h)·텍스트. 스캔 PDF 는 PNG 로 변환해 넣을 것. 축척(scale_ratio) 필요."),
    dict(format="png", ext=[".png"], note="래스터 평면도. 벽(어두운 선)으로 둘러싸인 실을 결정론적 영역분할로 인식. px_per_m 필요."),
    dict(format="ifc", ext=[".ifc"], note="IFC2x3/IFC4 STEP. IfcSpace·기기 요소의 사각/다각 압출형상 + 층(IfcBuildingStorey)."),
    dict(format="table", ext=[".csv", ".tsv", ".xlsx", ".json"], note="기기 목록표 (id,name,w,d[,h,x,y,floor,zone…]). 좌표 열이 있으면 현황 배치로, 없으면 목록만."),
]

UNSUPPORTED_HINTS = {
    "dwg": "DWG 는 비공개 바이너리 형식이라 직접 읽지 않는다. AutoCAD 'DXF로 저장' 또는 무료 ODA File Converter 로 DXF 로 변환해 넣을 것.",
    "jpeg": "JPEG 는 손실압축이라 벽 선이 번져 실 인식이 불안정하다. 원본 CAD 를 DXF/PDF 로 내보내거나, PNG 로 저장해 넣을 것.",
    "dxf-binary": "바이너리 DXF 는 지원하지 않는다. ASCII DXF 로 다시 저장할 것.",
    "gif": "GIF/BMP/TIFF 는 PNG 로 변환해 넣을 것.",
    "unknown": "형식을 판정하지 못했다. 지원 형식: DXF, SVG, PDF(벡터), PNG, IFC, CSV/XLSX/JSON.",
}


def sniff(data: bytes, filename: str = "") -> str:
    head = data[:2048]
    ext = os.path.splitext(filename.lower())[1]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a") or head.startswith(b"BM") or head[:4] in (b"II*\x00", b"MM\x00*"):
        return "gif"
    if head.startswith(b"AC10") or head.startswith(b"AC1."):
        return "dwg"
    if head.startswith(b"AutoCAD Binary DXF"):
        return "dxf-binary"
    if head.startswith(b"PK\x03\x04"):
        return "table" if ext in (".xlsx", "") or b"xl/" in data[:4096] else "unknown"
    txt = head.decode("utf-8", "ignore").lstrip("﻿").lstrip()
    low = txt.lower()
    if low.startswith("iso-10303-21"):
        return "ifc"
    if "<svg" in low:
        return "svg"
    lines = [l.strip() for l in txt.splitlines()[:6]]
    if len(lines) >= 2 and lines[0] == "0" and lines[1].upper() in ("SECTION", "EOF"):
        return "dxf"
    if low.startswith("999") and "SECTION" in txt.upper():
        return "dxf"
    if low.startswith("{") or low.startswith("["):
        return "table"
    if ext in (".csv", ".tsv", ".txt"):
        return "table"
    return "unknown"


def _reader(fmt: str) -> Callable[[bytes, str, dict], dict]:
    if fmt == "dxf":
        from . import dxf as m
    elif fmt == "svg":
        from . import svg as m
    elif fmt == "pdf":
        from . import pdf as m
    elif fmt == "png":
        from . import png as m
    elif fmt == "ifc":
        from . import ifc as m
    elif fmt == "table":
        from . import table as m
    else:
        raise ImportErrorUser(UNSUPPORTED_HINTS.get(fmt, UNSUPPORTED_HINTS["unknown"]))
    return m.read


def import_file(data: bytes, filename: str = "", opts: Optional[Dict] = None) -> dict:
    """
    바이트 → Takeoff. 실패 시 ImportErrorUser (메시지에 해결 방법 포함).
    같은 바이트 + 같은 opts → 같은 Takeoff (결정론).
    """
    opts = dict(opts or {})
    fmt = sniff(data, filename)
    if fmt in UNSUPPORTED_HINTS:
        raise ImportErrorUser(UNSUPPORTED_HINTS[fmt])
    tk = _reader(fmt)(data, filename or f"input.{fmt}", opts)
    tk["source"]["options"] = {k: v for k, v in sorted(opts.items())}
    return tk


def import_path(path: str, opts: Optional[Dict] = None) -> dict:
    with open(path, "rb") as fp:
        return import_file(fp.read(), os.path.basename(path), opts)
