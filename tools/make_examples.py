# -*- coding: utf-8 -*-
"""
make_examples.py — 예제 도면 + 설계요건 파일 생성기 (결정론적, 표준 라이브러리만)

  python3 tools/make_examples.py          → examples/drawings/ 에 씀

★ 이 도면들은 '실제 설계 도면' 이 아니다. 공개문헌 수준의 가정 치수로, 사람이 그렸을 법한
  관행적 배치(복도형 2열 배치, 파이프랙 양쪽 배치 등)를 규칙으로 생성한 **합성 현황 도면** 이다.
  목적은 (1) 다양한 형식의 입력 경로를 시험하고, (2) 제약이 많은 문제에서 파이프라인 전체가
  돈다는 것을 보이는 것이다. 여기서 나온 개선율을 실제 성과로 제시하지 말 것.

예제
  smr_aux_3f.dxf      원전 — SMR 보조건물 3개 층 (44개 기기, 원전 고유 제약 7종)   [목표 2]
  chem_plant.pdf      비교군 — 석유화학 공정 플랜트 배치(plot plan), 벡터 PDF 1:500   [목표 3]
  fpso_topside.svg    비교군 — FPSO 상부 모듈 2개 데크 (무게중심·위험구역 분리)        [목표 3]
  hospital_or.png     비교군 — 병원 수술부 평면 (청결/오염 동선 분리), 래스터 PNG      [목표 3]
  *_requirements.json 각 도면의 설계요건 (P&ID 연결·구역·이격·제약) — ga_update_ir 패치 형식
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import zlib
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
OUT = os.path.join(ROOT, "examples", "drawings")

import ga_core as G   # noqa: E402

Rect = Tuple[float, float, float, float]   # x, y, w, d


# ===========================================================================
# 관행적 배치 (사람이 그렸을 법한 현황) — 행 배치 + 복도 + 예약 영역 회피
# ===========================================================================

def _hit(r: Rect, others: List[Rect], eps=1e-6) -> Optional[Rect]:
    x, y, w, d = r
    for o in others:
        if x < o[0] + o[2] - eps and o[0] < x + w - eps and y < o[1] + o[3] - eps and o[1] < y + d - eps:
            return o
    return None


def row_layout(items: List[Tuple[str, float, float]], length: float, gap: float,
               corridor: float, reserved: List[Rect], y0: float = 0.0, x0: float = 0.0
               ) -> Tuple[Dict[str, Rect], List[Rect]]:
    """
    items = [(id, W, D)] (clearance 포함 외곽). 복도를 사이에 둔 두 줄씩 채운다.
    반환: (위치 {id: (x,y,w,d)}, 복도 사각형 목록)
    """
    pos: Dict[str, Rect] = {}
    corridors: List[Rect] = []
    k = 0
    base = y0
    while k < len(items):
        # 남쪽 줄(복도 아래, 위로 정렬) · 북쪽 줄(복도 위, 아래로 정렬)
        south, north = [], []
        for band in (south, north):
            x = x0
            while k < len(items):
                iid, w, d = items[k]
                if x + w > x0 + length + 1e-9 and band:
                    break
                band.append((iid, x, w, d))
                x += w + gap
                k += 1
        hs = max((d for _, _, _, d in south), default=0.0)
        hn = max((d for _, _, _, d in north), default=0.0)
        yc = base + hs + gap
        for iid, x, w, d in south:
            pos[iid] = (x, yc - gap - d, w, d)
        corridors.append((x0, yc, length, corridor))
        for iid, x, w, d in north:
            pos[iid] = (x, yc + corridor + gap, w, d)
        base = yc + corridor + gap + hn + gap
    # 예약 영역(아래층에서 올라오는 다층 기기)과 겹치면 오른쪽으로 민다
    placed: List[Rect] = list(reserved)
    for iid in [i for i, _, _ in items]:
        r = pos[iid]
        for _ in range(200):
            o = _hit(r, placed + corridors)
            if o is None:
                break
            r = (o[0] + o[2] + gap, r[1], r[2], r[3])
        pos[iid] = r
        placed.append(r)
    return pos, corridors


# ===========================================================================
# 형식별 작성기
# ===========================================================================

class DXF:
    """ASCII DXF (R2000 계열 최소 구성)."""
    def __init__(self, insunits=4):
        self.ent: List[str] = []
        self.insunits = insunits

    def g(self, c, v):
        self.ent += [str(c), str(v)]

    def poly(self, layer, pts):
        self.g(0, "LWPOLYLINE"); self.g(8, layer); self.g(100, "AcDbEntity")
        self.g(100, "AcDbPolyline"); self.g(90, len(pts)); self.g(70, 1)
        for x, y in pts:
            self.g(10, f"{x:.1f}"); self.g(20, f"{y:.1f}")

    def rect(self, layer, x, y, w, d):
        self.poly(layer, [(x, y), (x + w, y), (x + w, y + d), (x, y + d)])

    def line(self, layer, x0, y0, x1, y1):
        self.g(0, "LINE"); self.g(8, layer)
        self.g(10, f"{x0:.1f}"); self.g(20, f"{y0:.1f}"); self.g(11, f"{x1:.1f}"); self.g(21, f"{y1:.1f}")

    def text(self, layer, x, y, h, t):
        self.g(0, "TEXT"); self.g(8, layer)
        self.g(10, f"{x:.1f}"); self.g(20, f"{y:.1f}"); self.g(40, f"{h:.1f}"); self.g(1, t)

    def mtext(self, layer, x, y, h, t):
        self.g(0, "MTEXT"); self.g(8, layer)
        self.g(10, f"{x:.1f}"); self.g(20, f"{y:.1f}"); self.g(40, f"{h:.1f}"); self.g(1, t)

    def dump(self) -> str:
        head = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1015",
                "9", "$INSUNITS", "70", str(self.insunits), "0", "ENDSEC",
                "0", "SECTION", "2", "ENTITIES"]
        return "\n".join(head + self.ent + ["0", "ENDSEC", "0", "EOF"]) + "\n"


def pdf_bytes(pages: List[dict], page_w: float, page_h: float) -> bytes:
    """
    pages = [{"layers": {name: [ops...]}, "texts": [(x,y,size,str)]}]
    벡터 PDF — 레이어는 Optional Content(OCG) 로, 내용 스트림은 FlateDecode.
    """
    objs: List[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    layer_names = sorted({n for p in pages for n in p["layers"]})
    ocg_ids = {}
    for n in layer_names:
        ocg_ids[n] = add(f"<< /Type /OCG /Name ({n}) >>".encode())
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids, kids = [], []
    pages_id_placeholder = len(objs) + 1 + 2 * len(pages)   # 아래에서 계산
    content_ids = []
    for p in pages:
        ops = ["0.6 w"]
        for n in sorted(p["layers"]):
            ops.append(f"/OC /{n.replace(' ', '_')} BDC")
            ops += p["layers"][n]
            ops.append("EMC")
        ops.append("0 0 0 rg")
        for (x, y, sz, t) in p.get("texts", []):
            t = t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"BT /F1 {sz:.1f} Tf {x:.2f} {y:.2f} Td ({t}) Tj ET")
        raw = "\n".join(ops).encode("latin-1")
        comp = zlib.compress(raw, 9)
        content_ids.append(add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp)
                               + comp + b"\nendstream"))
    pages_id = len(objs) + len(pages) + 1
    props = " ".join(f"/{n.replace(' ', '_')} {ocg_ids[n]} 0 R" for n in layer_names)
    for k, p in enumerate(pages):
        page_ids.append(add((f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {page_w:.0f} {page_h:.0f}] "
                             f"/Resources << /Font << /F1 {font} 0 R >> /Properties << {props} >> >> "
                             f"/Contents {content_ids[k]} 0 R >>").encode()))
    assert len(objs) + 1 == pages_id
    add((f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(page_ids)} >>").encode())
    ocgs = " ".join(f"{ocg_ids[n]} 0 R" for n in layer_names)
    cat = add((f"<< /Type /Catalog /Pages {pages_id} 0 R /OCProperties << /OCGs [{ocgs}] "
               f"/D << /Order [{ocgs}] /ON [{ocgs}] >> >> >>").encode())
    out = bytearray(b"%PDF-1.6\n%\xe2\xe3\xcf\xd3\n")
    offs = []
    for i, b in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + b + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root {cat} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def png_gray(w: int, h: int, px: bytearray) -> bytes:
    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += px[y * w:(y + 1) * w]
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"pHYs", struct.pack(">IIB", 3937, 3937, 1))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


# ===========================================================================
# 1) 원전 — SMR 보조건물 3개 층 (DXF, mm)
# ===========================================================================

def make_smr():
    ir = json.load(open(os.path.join(ROOT, "examples", "spec_smr_aux.json"), encoding="utf-8"))
    spec = G.spec_from_dict(ir)
    opt = json.load(open(os.path.join(ROOT, "examples", "out", "GA_optimized.json"), encoding="utf-8"))
    floor_of = {b["id"]: b["floor"] for b in opt["blocks"]}      # 설계자의 층 배정(가정)
    fh = ir["floor_heights"]
    gap, corr, L = 1.5, 4.0, 66.0

    def span(b, f):
        acc, k = 0.0, 0
        while acc < b.h - 1e-9:
            acc += fh[f + k] if f + k < len(fh) else fh[-1]; k += 1
        return max(1, k)

    # 그룹 순서 = 사람이 묶는 방식: 계열 A → 계열 B → 방사선 → 제어 → 공용
    def key(b):
        order = {"TrainA": 0, "TrainB": 1}.get(b.train or "", 2)
        zo = {"R3": 0, "R2": 1, "R1": 2, "CTRL": 3, "NR": 4}.get(b.zone or "", 5)
        return (order, zo, b.id)

    cores = [("CORE-W", 0.0, 6.0, 6.0), ("CORE-E", L + 2.0, 6.0, 6.0)]   # x, w, d (y 는 복도 중심)
    positions: Dict[str, Tuple[int, Rect]] = {}
    reserved: Dict[int, List[Rect]] = {0: [], 1: [], 2: []}
    corridors: Dict[int, List[Rect]] = {}
    for f in range(3):
        bl = sorted([b for b in spec.blocks if floor_of.get(b.id, 0) == f], key=key)
        items = [(b.id, b.w + 2 * b.clearance, b.d + 2 * b.clearance) for b in bl]
        pos, cors = row_layout(items, L, gap, corr, reserved[f], y0=0.0, x0=8.0)
        corridors[f] = cors
        for b in bl:
            r = pos[b.id]
            positions[b.id] = (f, r)
            for k in range(1, span(b, f)):
                if f + k < 3:
                    reserved[f + k].append(r)
    W = max(r[0] + r[2] for _, r in positions.values()) + 8.0 + 2.0
    D = max(r[1] + r[3] for _, r in positions.values()) + 2.0
    dx = DXF(insunits=4)
    S = 1000.0   # m → mm
    for f in range(3):
        lay = f"{f+1}F"
        dx.rect(f"{lay}-OUTLINE", -2 * S, -2 * S, W * S, (D + 2) * S)
        for (x0, y0, w, d) in [(-2, -2, W, 0.4), (-2, D - 0.4, W, 0.4)]:
            dx.line(f"{lay}-WALL", x0 * S, y0 * S, (x0 + w) * S, y0 * S)
        for cx in (0.5, W - 8.5):
            yc = corridors[f][0][1] + corridors[f][0][3] / 2
            dx.rect(f"{lay}-CORE", cx * S, (yc - 3) * S, 6 * S, 6 * S)
            dx.text(f"{lay}-TEXT", (cx + 0.5) * S, (yc - 0.3) * S, 500, "CORE-W" if cx < 1 else "CORE-E")
        for k, c in enumerate(corridors[f]):
            dx.rect(f"{lay}-ROOM", c[0] * S, c[1] * S, c[2] * S, c[3] * S)
            dx.text(f"{lay}-TEXT", (c[0] + 2) * S, (c[1] + 1.5) * S, 600, f"COR-{f+1}{k+1}")
            dx.mtext(f"{lay}-TEXT", (c[0] + 12) * S, (c[1] + 1.5) * S, 500, "복도")
    for b in spec.blocks:
        f, r = positions[b.id]
        x, y = r[0] + b.clearance, r[1] + b.clearance
        layer = f"{f+1}F-HATCH" if b.opening else f"{f+1}F-EQUIP"
        dx.rect(layer, x * S, y * S, b.w * S, b.d * S)
        dx.text(f"{f+1}F-TEXT", (x + 0.4) * S, (y + b.d * 0.55) * S, 450, b.id)
        dx.mtext(f"{f+1}F-TEXT", (x + 0.4) * S, (y + b.d * 0.25) * S, 350, b.name.split("#")[0].strip())
        for k in range(1, span(b, f)):
            if f + k < 3:      # 위층: 관통부 표시 (분류 무시 레이어)
                dx.rect(f"{f+k+1}F-PENETRATION", x * S, y * S, b.w * S, b.d * S)
    with open(os.path.join(OUT, "smr_aux_3f.dxf"), "w", encoding="utf-8") as fp:
        fp.write(dx.dump())

    # ---- 설계요건 (P&ID·구역·제약) — 좌표 없음 ----
    blocks = []
    for b in spec.blocks:
        upd = dict(id=b.id, name=b.name, h=b.h, clearance=b.clearance, zone=b.zone, weight=b.weight,
                   rotatable=b.rotatable)
        for k in ("need_exterior", "need_crane", "opening"):
            if getattr(b, k):
                upd[k] = True
        for k in ("fire_zone", "train", "vital_area", "success_path", "structure"):
            if getattr(b, k):
                upd[k] = getattr(b, k)
        blocks.append(upd)
    # hard 항 가중치는 도면 크기(현황 외피)에서 자동 산정된 값을 쓴다 — 요건 파일은
    # 연성 항만 정한다. hard 제약 라이브러리 항은 한계이득 하한을 넉넉히 넘게 둔다.
    import constraints as CONS
    weights = {}
    for k, v in ir["weights"].items():
        if k in G.HARD_TERMS:
            continue
        if k.startswith("constraint.") and CONS.REGISTRY[k.split(".", 1)[1]].hard:
            v = max(v, 100000.0)
        weights[k] = v
    req = {
        "_note": "SMR 보조건물 설계요건 (가정값 — spec_smr_aux.json 에서 좌표 없이 옮김). 실제 설계값 아님.",
        "set": {"floor_heights": fh, "weights": weights, "constraints": ir["constraints"],
                "crane_bay": ir["crane_bay"]},
        "blocks": blocks,
        "remove_links": "inferred",
        "add_links": [dict(a=l.a, b=l.b, cost=l.cost, kind=l.kind, vertical_factor=l.vertical_factor)
                      for l in spec.links],
        "add_separations": [dict(a=s.a, b=s.b, min_dist=s.min_dist, hard=s.hard, by_zone=s.by_zone,
                                 basis=s.basis) for s in spec.separations],
        "add_elevations": [dict(above=e.above, below=e.below, min_dz=e.min_dz, basis=e.basis)
                           for e in spec.elevations],
        "answered_questions": ["floor_heights", "clearance", "links", "zones_separations", "names"],
    }
    return req


# ===========================================================================
# 2) 비교군 — 석유화학 공정 플랜트 (벡터 PDF 1:500)
# ===========================================================================

CHEM_UNITS = [
    # id, 이름, w, d, h, 중량t, 구역, 이격(clearance)
    ("F-101", "가열로 (Fired Heater)", 14, 10, 25, 300, "FIRED", 3.0),
    ("R-101", "반응기", 8, 8, 20, 250, "PROCESS", 2.5),
    ("R-102", "반응기 2", 8, 8, 20, 250, "PROCESS", 2.5),
    ("C-101", "증류탑 1", 6, 6, 45, 180, "PROCESS", 2.0),
    ("C-102", "증류탑 2", 6, 6, 40, 160, "PROCESS", 2.0),
    ("E-101", "열교환기 뱅크", 12, 5, 4, 60, "PROCESS", 1.5),
    ("E-102", "공랭식 냉각기", 16, 8, 10, 80, "PROCESS", 1.5),
    ("K-101", "재순환 압축기", 12, 8, 6, 120, "COMP", 3.0),
    ("K-102", "냉동 압축기", 12, 8, 6, 120, "COMP", 3.0),
    ("V-101", "분리 드럼", 5, 5, 8, 40, "PROCESS", 1.5),
    ("P-101", "공급 펌프군", 8, 4, 2, 20, "PROCESS", 1.2),
    ("P-102", "환류 펌프군", 8, 4, 2, 20, "PROCESS", 1.2),
    ("T-101", "원료 저장탱크", 18, 18, 15, 80, "STORAGE", 3.0),
    ("T-102", "제품 저장탱크", 18, 18, 15, 80, "STORAGE", 3.0),
    ("T-103", "중간 탱크", 12, 12, 12, 50, "STORAGE", 3.0),
    ("CR-1", "중앙 제어실", 20, 14, 6, 50, "OCCUPIED", 2.0),
    ("SS-1", "변전실", 14, 10, 6, 70, "ELEC", 2.0),
    ("CT-1", "냉각탑", 16, 12, 12, 90, "UTIL", 2.0),
    ("UT-1", "유틸리티 (질소·공기)", 12, 8, 5, 40, "UTIL", 1.5),
    ("LD-1", "정비 적치장 (Laydown)", 20, 12, 1, 0, "OPEN", 0.0),
    ("FL-1", "플레어 녹아웃 드럼", 6, 6, 8, 30, "FLARE", 3.0),
]


def make_chem():
    gap, L = 6.0, 110.0
    items = [(u[0], u[2] + 2 * u[7], u[3] + 2 * u[7]) for u in CHEM_UNITS]
    # 관행: 파이프랙(중앙, 고정)을 사이에 두고 양쪽 배치
    pos, cors = row_layout(items, L, gap, 8.0, [], y0=6.0, x0=6.0)
    W = max(r[0] + r[2] for r in list(pos.values()) + cors) + 6
    D = max(r[1] + r[3] for r in list(pos.values()) + cors) + 6
    k = 1 / 0.0254 * 72 / 500     # m → pt @1:500
    ox, oy = 40.0, 40.0
    page_w, page_h = W * k + 80, D * k + 120

    def R(x, y, w, d):
        return f"{ox + x*k:.2f} {oy + y*k:.2f} {w*k:.2f} {d*k:.2f} re S"
    layers = {"OUTLINE": [R(0, 0, W, D)], "EQUIP": [], "PIPERACK-FIXED": [], "ANNO": []}
    texts = []
    for u in CHEM_UNITS:
        x, y, w, d = pos[u[0]]
        cl = u[7]
        layers["EQUIP"].append(R(x + cl, y + cl, u[2], u[3]))
        texts.append((ox + (x + cl + 0.6) * k, oy + (y + cl + u[3] / 2) * k, 5.5, u[0]))
    for c in cors:      # 중앙 파이프랙 = 고정 장애물
        layers["PIPERACK-FIXED"].append(R(c[0], c[1] + 1, c[2], c[3] - 2))
        texts.append((ox + (c[0] + 2) * k, oy + (c[1] + c[3] / 2) * k, 5.5, "PR-1"))
    texts.append((ox, page_h - 30, 9, "PLOT PLAN - PETROCHEMICAL UNIT (SYNTHETIC, ASSUMED) 1:500"))
    data = pdf_bytes([{"layers": layers, "texts": texts}], page_w, page_h)
    with open(os.path.join(OUT, "chem_plant.pdf"), "wb") as fp:
        fp.write(data)

    blocks = [dict(id=u[0], name=u[1], h=u[4], weight=u[5], zone=u[6], clearance=u[7]) for u in CHEM_UNITS]
    for b in blocks:
        if b["id"] in ("T-101", "T-102", "CR-1", "LD-1"):
            b["need_exterior"] = True          # 탱크로리·접근로·비상대피
    B = "(가정값 — 실제는 CCPS GCPS / 보험사 이격표 / 정량위험평가 원문 대조)"
    seps = [("F-101", "K-101", 15), ("F-101", "K-102", 15), ("F-101", "T-101", 15), ("F-101", "T-102", 15),
            ("F-101", "T-103", 15), ("CR-1", "R-101", 15), ("CR-1", "R-102", 15), ("CR-1", "K-101", 15),
            ("CR-1", "K-102", 15), ("CR-1", "F-101", 15), ("FL-1", "CR-1", 18), ("T-101", "K-101", 10),
            ("T-102", "K-102", 10), ("SS-1", "K-101", 10)]
    links = [("T-101", "P-101", 3), ("P-101", "E-101", 3), ("E-101", "F-101", 4), ("F-101", "R-101", 5),
             ("R-101", "R-102", 4), ("R-102", "V-101", 3), ("V-101", "K-101", 4), ("K-101", "R-101", 3),
             ("V-101", "C-101", 3), ("C-101", "C-102", 3), ("C-102", "P-102", 2), ("P-102", "C-102", 1),
             ("C-102", "E-102", 2), ("E-102", "T-102", 2), ("C-101", "T-103", 2), ("K-102", "E-102", 2),
             ("CT-1", "E-101", 2), ("CT-1", "E-102", 1), ("SS-1", "K-101", 1.5), ("SS-1", "K-102", 1.5),
             ("UT-1", "R-101", 1), ("V-101", "FL-1", 2), ("C-101", "FL-1", 1), ("CR-1", "SS-1", 1)]
    req = {
        "_note": "석유화학 공정 플랜트 설계요건 (가정값). 이격 거리는 공개 문헌의 전형적 범위를 단순화한 가정이다.",
        "set": {"floor_heights": [50.0]},   # 배치도(1개 층): '층고' = 최고 기기(증류탑 45 m) 수용 높이
        "blocks": blocks,
        "remove_links": "inferred",
        "add_links": [dict(a=a, b=b, cost=c, kind="pipe") for a, b, c in links],
        "add_separations": [dict(a=a, b=b, min_dist=d, hard=True, basis=B) for a, b, d in seps],
        "answered_questions": ["floor_heights", "clearance", "links", "zones_separations"],
    }
    return req


# ===========================================================================
# 3) 비교군 — FPSO 상부 모듈 (SVG, 2개 데크)
# ===========================================================================

FPSO = [
    # id, 이름, w, d, h, t, 구역, clearance, 데크(0/1), 비고
    ("M01", "1단 분리 모듈", 20, 16, 12, 1800, "HAZ", 1.0, 0, ""),
    ("M02", "2단 분리·탈수", 18, 14, 12, 1400, "HAZ", 1.0, 0, ""),
    ("M03", "가스 압축", 22, 16, 14, 2200, "HAZ", 1.0, 0, ""),
    ("M04", "가스 처리 (탈습)", 16, 14, 12, 1300, "HAZ", 1.0, 1, ""),
    ("M05", "수 주입", 16, 12, 10, 1200, "SAFE", 1.0, 0, ""),
    ("M06", "생산수 처리", 16, 12, 10, 900, "HAZ", 1.0, 1, ""),
    ("M07", "발전 (가스터빈)", 20, 14, 12, 1600, "SAFE", 1.0, 1, ""),
    ("M08", "유틸리티·공기", 14, 10, 8, 600, "SAFE", 1.0, 1, ""),
    ("M09", "화학약품 주입", 10, 8, 6, 300, "HAZ", 1.0, 1, ""),
    ("EH-1", "전기실 (E-House)", 14, 10, 8, 500, "SAFE", 1.0, 1, ""),
    ("LQ", "거주구역 (Living Quarters)", 20, 16, 18, 2500, "LQ", 1.0, 0, "ext"),
    ("LB-1", "구명정 스테이션", 8, 5, 4, 60, "LQ", 0.5, 0, "ext"),
    ("BW-1", "방폭벽 (Blast Wall)", 30, 2, 12, 400, "SAFE", 0.0, 0, ""),
    ("LD-1", "갑판 적치장", 16, 10, 1, 0, "SAFE", 0.0, 1, ""),
]


def make_fpso():
    S = 10.0       # SVG 사용자 단위 1 = 0.1 m
    gap, L = 3.0, 90.0
    out = ['<?xml version="1.0" encoding="UTF-8"?>']
    decks: Dict[int, Dict[str, Rect]] = {}
    pinned = {0: [("TURRET", "터렛 (고정)", 0.0, 14.0, 12.0, 12.0), ("CRANE-P", "크레인 페데스탈", 50.0, 34.0, 4.0, 4.0)],
              1: [("FLARE-B", "플레어 타워 기초", 92.0, 2.0, 6.0, 6.0), ("CRANE-S", "크레인 페데스탈", 50.0, 0.0, 4.0, 4.0)]}
    for deck in (0, 1):
        items = [(m[0], m[2] + 2 * m[7], m[3] + 2 * m[7]) for m in FPSO if m[8] == deck]
        res = [(p[2], p[3], p[4], p[5]) for p in pinned[deck]]
        pos, _cors = row_layout(items, L, gap, 3.0, res, y0=2.0, x0=14.0)
        decks[deck] = pos
    W = max(max(r[0] + r[2] for r in decks[k].values()) for k in decks) + 4
    D = max(max(r[1] + r[3] for r in decks[k].values()) for k in decks) + 4
    W = max(W, 100.0)
    vw, vh = W * S, D * S * 2 + 120
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
               f'viewBox="0 0 {vw:.0f} {vh:.0f}" width="{vw/10:.0f}mm" height="{vh/10:.0f}mm" data-m-per-unit="0.1">')
    out.append('<title>FPSO TOPSIDE (SYNTHETIC, ASSUMED) — 1 unit = 0.1 m</title>')
    for deck in (0, 1):
        oy = deck * (D * S + 120)          # 위에서부터 그린다 (SVG y 하향)
        Y = lambda y, d: oy + (D - y - d) * S
        out.append(f'<g id="LEVEL_{deck+1}" inkscape:label="LEVEL_{deck+1}">')
        out.append(f'<g id="BOUNDARY_{deck+1}"><rect x="0" y="{oy:.0f}" width="{W*S:.0f}" height="{D*S:.0f}" '
                   f'fill="none" stroke="#000" stroke-width="4"/></g>')
        out.append(f'<g id="EQUIP_{deck+1}">')
        for m in FPSO:
            if m[8] != deck:
                continue
            x, y, w, d = decks[deck][m[0]]
            cl = m[7]
            out.append(f'<rect x="{(x+cl)*S:.0f}" y="{Y(y+cl, m[3]):.0f}" width="{m[2]*S:.0f}" height="{m[3]*S:.0f}" '
                       f'fill="#dbeafe" stroke="#1e3a8a" stroke-width="2"/>')
            out.append(f'<text x="{(x+cl+0.8)*S:.0f}" y="{Y(y+cl, m[3]) + m[3]*S/2:.0f}" font-size="18">{m[0]}</text>')
            out.append(f'<text x="{(x+cl+0.8)*S:.0f}" y="{Y(y+cl, m[3]) + m[3]*S/2 + 22:.0f}" font-size="14">{m[1]}</text>')
        out.append('</g>')
        out.append(f'<g id="FIXED_{deck+1}">')
        for p in pinned[deck]:
            out.append(f'<rect x="{p[2]*S:.0f}" y="{Y(p[3], p[5]):.0f}" width="{p[4]*S:.0f}" height="{p[5]*S:.0f}" '
                       f'fill="#374151" stroke="#000" stroke-width="2"/>')
            out.append(f'<text x="{(p[2]+0.5)*S:.0f}" y="{Y(p[3], p[5]) + p[5]*S/2:.0f}" font-size="16" fill="#fff">{p[0]}</text>')
        out.append('</g></g>')
    out.append('</svg>')
    with open(os.path.join(OUT, "fpso_topside.svg"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(out))

    B = "(가정값 — 실제는 화재·폭발 위험해석(FERA) / API RP 14J / DNV 원문 대조)"
    blocks = []
    for m in FPSO:
        b = dict(id=m[0], name=m[1], h=m[4], weight=m[5], zone=m[6], clearance=m[7])
        if m[9] == "ext":
            b["need_exterior"] = True
        blocks.append(b)
    hz = [m[0] for m in FPSO if m[6] == "HAZ"]
    seps = [dict(a="LQ", b=h, min_dist=12.0, hard=True, basis=B) for h in hz]
    seps += [dict(a="LB-1", b=h, min_dist=8.0, hard=True, basis=B) for h in hz]
    seps += [dict(a="EH-1", b=h, min_dist=6.0, hard=True, basis=B) for h in ("M01", "M02", "M03")]
    links = [("M01", "M02", 5), ("M02", "M03", 4), ("M03", "M04", 4), ("M02", "M06", 3), ("M06", "M05", 2),
             ("M04", "M07", 2), ("M07", "EH-1", 3), ("EH-1", "M03", 2), ("M08", "M01", 1), ("M09", "M01", 1),
             ("M09", "M06", 1), ("LQ", "LB-1", 3), ("LQ", "EH-1", 1)]
    req = {
        "_note": "FPSO 상부 설계요건 (가정값). 무게중심 편심을 강하게 본다 (Bae 2014: CoG 1 m² 이내 관행).",
        "set": {"floor_heights": [14.0, 14.0],
                "weights": {"ecc": 2000.0}},
        "blocks": blocks,
        "remove_links": "inferred",
        "add_links": [dict(a=a, b=b, cost=c, kind="pipe") for a, b, c in links],
        "add_separations": seps,
        "answered_questions": ["floor_heights", "clearance", "links", "zones_separations"],
    }
    return req


# ===========================================================================
# 4) 비교군 — 병원 수술부 (PNG 래스터, 10 px/m)
# ===========================================================================

HOSP = [
    # id, 이름, x, y, w, d (m) — 벽 중심선 기준 실 영역, 구역
    ("OR-1", "수술실 1", 0, 26, 8, 8, "STERILE"), ("OR-2", "수술실 2", 8, 26, 8, 8, "STERILE"),
    ("OR-3", "수술실 3", 16, 26, 8, 8, "STERILE"), ("OR-4", "수술실 4", 24, 26, 8, 8, "STERILE"),
    ("OR-5", "수술실 5", 40, 26, 8, 8, "STERILE"), ("OR-6", "수술실 6 (하이브리드)", 48, 26, 12, 8, "STERILE"),
    ("SCRUB-1", "스크럽", 32, 26, 8, 4, "STERILE"), ("SCRUB-2", "스크럽 2", 32, 30, 8, 4, "STERILE"),
    ("CORR-N", "북측 청결복도", 0, 22, 60, 4, "SEMI"),
    ("CORE", "멸균물품 코어", 8, 14, 24, 8, "STERILE"),
    ("CSSD", "중앙공급실", 32, 14, 12, 8, "SEMI"), ("CLEAN", "청결 창고", 44, 14, 8, 8, "SEMI"),
    ("SOIL", "오염 처리실", 52, 14, 8, 8, "DIRTY"), ("ANES", "마취 준비", 0, 14, 8, 8, "SEMI"),
    ("CORR-S", "남측 일반복도", 0, 10, 60, 4, "UNRES"),
    ("PREOP", "수술 전 대기", 0, 0, 14, 10, "UNRES"), ("PACU", "회복실", 14, 0, 18, 10, "SEMI"),
    ("LOUNGE", "직원 휴게", 32, 0, 10, 10, "UNRES"), ("LOCKER", "탈의실", 42, 0, 8, 10, "UNRES"),
    ("STORE", "장비 창고", 50, 0, 10, 10, "SEMI"),
]


def make_hospital():
    ppm = 10
    wall = 3
    W, D = 60, 34
    pw, ph = W * ppm + 2 * 20, D * ppm + 2 * 20
    px = bytearray([255]) * (pw * ph)

    def fill(x0, y0, x1, y1, v=0):
        for yy in range(max(0, y0), min(ph, y1)):
            row = yy * pw
            for xx in range(max(0, x0), min(pw, x1)):
                px[row + xx] = v

    def to_px(x, y):     # m → 이미지 픽셀 (y 하향)
        return 20 + int(round(x * ppm)), 20 + int(round((D - y) * ppm))
    for (_id, _n, x, y, w, d, _z) in HOSP:
        x0, y1 = to_px(x, y)
        x1, y0 = to_px(x + w, y + d)
        fill(x0 - 1, y0 - 1, x1 + 2, y0 + wall - 1)    # 위
        fill(x0 - 1, y1 - 1, x1 + 2, y1 + wall - 1)    # 아래
        fill(x0 - 1, y0 - 1, x0 + wall - 1, y1 + 2)    # 왼쪽
        fill(x1 - 1, y0 - 1, x1 + wall - 1, y1 + 2)    # 오른쪽
    with open(os.path.join(OUT, "hospital_or.png"), "wb") as fp:
        fp.write(png_gray(pw, ph, px))
    return None   # 요건은 import 후 인식 id 에 맞춰 만든다 (make_hospital_requirements)


def make_hospital_requirements():
    """PNG 에는 문자가 없다 → 인식된 실(RM001…)을 실제 실 목록과 기하로 대응시켜 이름·요건을 붙인다."""
    import importers
    tk = importers.import_path(os.path.join(OUT, "hospital_or.png"), {"px_per_m": 10})
    rooms = [it for it in tk["items"] if it["kind"] in ("room", "equipment")]
    mapping = {}
    for (rid, name, x, y, w, d, z) in HOSP:
        cx, cy = x + w / 2, y + d / 2
        best = min(rooms, key=lambda it: (it["x"] + it["w"] / 2 - cx) ** 2 + (it["y"] + it["d"] / 2 - cy) ** 2)
        mapping[rid] = best["id"]
    if len(set(mapping.values())) != len(HOSP):
        raise SystemExit(f"PNG 인식 실 수가 맞지 않는다: {len(rooms)} vs {len(HOSP)}")
    m = mapping
    B = "(가정값 — 실제는 의료기관 시설기준·감염관리 지침 원문 대조)"
    blocks = [dict(id=m[r[0]], name=f"{r[0]} {r[1]}", zone=r[6], clearance=0.0, h=4.5) for r in HOSP]
    blocks += [dict(all=True, kind_filter="equipment", kind="room")]
    links = [("OR-1", "CORE", 5), ("OR-2", "CORE", 5), ("OR-3", "CORE", 5), ("OR-4", "CORE", 5),
             ("OR-5", "CORE", 5), ("OR-6", "CORE", 5), ("OR-1", "CORR-N", 3), ("OR-2", "CORR-N", 3),
             ("OR-3", "CORR-N", 3), ("OR-4", "CORR-N", 3), ("OR-5", "CORR-N", 3), ("OR-6", "CORR-N", 3),
             ("SCRUB-1", "CORR-N", 3), ("SCRUB-2", "CORR-N", 3), ("CORE", "CSSD", 4), ("CSSD", "SOIL", 3),
             ("CLEAN", "CORE", 2), ("PACU", "CORR-S", 4), ("PREOP", "CORR-S", 4), ("ANES", "CORR-N", 2),
             ("CORR-N", "CORR-S", 3), ("LOCKER", "CORR-S", 2), ("LOUNGE", "CORR-S", 1), ("STORE", "CORR-N", 2),
             ("SOIL", "CORR-S", 2)]
    seps = [("SOIL", "CORE", 6.0), ("SOIL", "CLEAN", 3.0), ("SOIL", "OR-6", 6.0), ("SOIL", "OR-5", 6.0)]
    req = {
        "_note": "병원 수술부 설계요건 (가정값). 청결-오염 동선 분리와 수술실-멸균코어 인접이 핵심.",
        "set": {"floor_heights": [4.5]},
        "blocks": blocks,
        "remove_links": "inferred",
        "add_links": [dict(a=m[a], b=m[b], cost=c, kind="route") for a, b, c in links],
        "add_separations": [dict(a=m[a], b=m[b], min_dist=d, hard=True, basis=B) for a, b, d in seps],
        "answered_questions": ["floor_heights", "clearance", "links", "zones_separations", "names"],
    }
    return req


def main():
    os.makedirs(OUT, exist_ok=True)
    reqs = {"smr_aux_3f": make_smr(), "chem_plant": make_chem(), "fpso_topside": make_fpso()}
    make_hospital()
    for k, v in reqs.items():
        with open(os.path.join(OUT, f"{k}_requirements.json"), "w", encoding="utf-8") as fp:
            json.dump(v, fp, ensure_ascii=False, indent=1)
    try:
        hr = make_hospital_requirements()
        with open(os.path.join(OUT, "hospital_or_requirements.json"), "w", encoding="utf-8") as fp:
            json.dump(hr, fp, ensure_ascii=False, indent=1)
    except ImportError as ex:
        print("PNG 파서가 아직 없다 — 병원 요건 파일 생략:", ex)
    for f in sorted(os.listdir(OUT)):
        print(f"  {f:40s} {os.path.getsize(os.path.join(OUT, f)):>9,} B")


if __name__ == "__main__":
    main()
