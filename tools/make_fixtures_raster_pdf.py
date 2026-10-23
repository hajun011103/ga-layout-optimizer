# -*- coding: utf-8 -*-
"""
tools/make_fixtures_raster_pdf.py — PDF/PNG 가져오기 시험용 결정론적 픽스처 생성 (표준 라이브러리만)

  python3 tools/make_fixtures_raster_pdf.py        → tests/fixtures/*.pdf, *.png

PDF (1:100, 1 m = 1 cm 종이 = 28.3464567 pt — 내용은 cm 연산자로 미터 좌표계에서 그린다)
  plan_classic.pdf  고전 xref 표 + 증분 갱신(/Prev). 1판의 'HX-1' 이 2판에서 'HX-2' 로 바뀐다
  plan_xrefstm.pdf  같은 도면을 교차참조 스트림(PNG Up 예측자) + 객체 스트림으로
  plan_measure.pdf  /VP /Measure 로 축척을 품은 PDF (사용자 축척 없이 읽혀야 한다)
  scanned.pdf       페이지 전체를 덮는 이미지 하나 (스캔 PDF 경고)
  encrypted.pdf     /Encrypt 트레일러 (거부되어야 한다)

  1쪽: 경계 40×25 (OCG BOUNDARY), 실 3 (OCG ROOM — R3 은 낱개 선분 4개),
       기기 2 (OCG EQUIP — P-101 은 칠+획 이중 출력, HX-2 는 Form XObject),
       기둥 원 ⌀1 (OCG COLUMN, 베지에), 한글 '펌프실' (Type0/Identity-H + ToUnicode),
       용지 테두리·표제란(제거 대상)
  2쪽: /Rotate 90, 경계 20×15, 실 6×4 (회전 후 4×6)

PNG (600×400, px_per_m = 20)
  외벽 6 px, 칸막이 2 개, 칸막이 하나에 10 px 문틈, 20×20 px 꽉 찬 기둥
  plan.png              8비트 회색, 행마다 필터 0~4 순환, pHYs 72 dpi
  plan_palette.png      팔레트(색형식 3) 4비트 + tRNS, 주황 구역 #FFC864 (4×4 m)
  plan_rgb_adam7.png    RGB 8비트 Adam7 인터레이스, 주황 구역
  plan_ga16.png         회색+알파 16비트
"""
from __future__ import annotations

import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "fixtures")
S = 72.0 / 2.54          # 1:100 에서 1 m(실제) = 1 cm(종이) = 28.346… pt


# ---------------------------------------------------------------------------
# PDF 작성기
# ---------------------------------------------------------------------------

def stream_obj(dict_inner: bytes, data: bytes, filt: str = "flate") -> bytes:
    if filt == "flate":
        enc = zlib.compress(data, 9)
        head = b"/Filter /FlateDecode "
    elif filt == "a85flate":
        import base64
        enc = base64.a85encode(zlib.compress(data, 9), wrapcol=72) + b"~>"
        head = b"/Filter [/ASCII85Decode /FlateDecode] "
    else:
        enc = data
        head = b""
    return (b"<< " + head + dict_inner + b" /Length %d >>\nstream\n" % len(enc)
            + enc + b"\nendstream")


def is_stream(body: bytes) -> bool:
    return b"\nstream\n" in body


def write_classic(objs: dict, root: int, extra: bytes = b"") -> bytes:
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = {}
    for num in sorted(objs):
        offs[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    size = max(objs) + 1
    xref = len(out)
    out += b"xref\n0 %d\n" % size
    out += b"0000000000 65535 f \n"
    for n in range(1, size):
        if n in offs:
            out += b"%010d 00000 n \n" % offs[n]
        else:
            out += b"0000000000 00000 f \n"
    out += b"trailer\n<< /Size %d /Root %d 0 R %s>>\nstartxref\n%d\n%%%%EOF\n" % (size, root, extra, xref)
    return bytes(out)


def append_update(base: bytes, objs: dict, root: int) -> bytes:
    """증분 갱신: 바뀐 객체만 덧붙이고 /Prev 로 앞 판의 xref 를 가리킨다."""
    prev = int(base[base.rfind(b"startxref") + 9:].split()[0])
    out = bytearray(base)
    offs = {}
    for num in sorted(objs):
        offs[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 1\n0000000000 65535 f \n"
    for num in sorted(offs):
        out += b"%d 1\n%010d 00000 n \n" % (num, offs[num])
    size = max(max(objs) + 1, int(base[base.rfind(b"/Size") + 5:].split()[0]))
    out += b"trailer\n<< /Size %d /Root %d 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (
        size, root, prev, xref)
    return bytes(out)


def write_xref_stream(objs: dict, root: int) -> bytes:
    """스트림이 아닌 객체는 모두 객체 스트림 하나에, xref 는 교차참조 스트림(/Predictor 12)."""
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    plain = [n for n in sorted(objs) if not is_stream(objs[n])]
    direct = [n for n in sorted(objs) if is_stream(objs[n])]
    stm_num = max(objs) + 1
    xref_num = stm_num + 1
    head = b""
    body = b""
    index = {}
    for i, n in enumerate(plain):
        head += b"%d %d " % (n, len(body))
        body += objs[n] + b"\n"
        index[n] = i
    stm = stream_obj(b"/Type /ObjStm /N %d /First %d" % (len(plain), len(head)), head + body)
    allobjs = dict((n, objs[n]) for n in direct)
    allobjs[stm_num] = stm
    offs = {}
    for num in sorted(allobjs):
        offs[num] = len(out)
        out += b"%d 0 obj\n" % num + allobjs[num] + b"\nendobj\n"
    xref_off = len(out)
    size = xref_num + 1
    rows = []
    for n in range(size):
        if n in offs:
            rows.append(struct.pack(">BIH", 1, offs[n], 0))
        elif n == xref_num:
            rows.append(struct.pack(">BIH", 1, xref_off, 0))
        elif n in index:
            rows.append(struct.pack(">BIH", 2, stm_num, index[n]))
        else:
            rows.append(struct.pack(">BIH", 0, 0, 65535 if n == 0 else 0))
    # PNG Up 예측자로 인코딩
    raw = bytearray()
    prev = bytes(7)
    for r in rows:
        raw.append(2)
        raw += bytes((r[i] - prev[i]) & 255 for i in range(7))
        prev = r
    enc = zlib.compress(bytes(raw), 9)
    out += (b"%d 0 obj\n<< /Type /XRef /Size %d /Root %d 0 R /W [1 4 2] /Index [0 %d] "
            b"/Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 7 >> /Length %d >>\nstream\n"
            % (xref_num, size, root, size, len(enc))) + enc + b"\nendstream\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref_off
    return bytes(out)


def circle(cx, cy, r) -> bytes:
    k = 0.5522847498 * r
    return (b"%g %g m %g %g %g %g %g %g c %g %g %g %g %g %g c %g %g %g %g %g %g c %g %g %g %g %g %g c h"
            % (cx + r, cy,
               cx + r, cy + k, cx + k, cy + r, cx, cy + r,
               cx - k, cy + r, cx - r, cy + k, cx - r, cy,
               cx - r, cy - k, cx - k, cy - r, cx, cy - r,
               cx + k, cy - r, cx + r, cy - k, cx + r, cy))


TOUNICODE = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
2 beginbfchar
<0001> <D38C>
<0002> <D504>
endbfchar
1 beginbfrange
<0003> <0003> <C2E4>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


def page1_content(hx: bytes) -> bytes:
    s = "%.15g" % S
    return b"\n".join([
        b"% page frame and title block (pt)",
        b"0.5 w 0 0 0 RG",
        b"5 5 1290 890 re S",
        b"1100 5 195 40 re S",
        b"BT /F1 10 Tf 1110 20 Td (SMR TEST PLAN) Tj ET",
        b"q " + (f"{s} 0 0 {s} 50 50 cm").encode(),
        b"0.02 w",
        b"/OC /oc1 BDC 0 0 40 25 re S EMC",
        b"/OC /oc2 BDC",
        b"1 1 10 8 re S",
        b"12 1 m 20 1 l 20 9 l 12 9 l h S",
        b"21 1 m 30 1 l S 30 1 m 30 7 l S 30 7 m 21 7 l S 21 7 m 21 1 l S",
        b"EMC",
        b"/OC /oc3 BDC",
        b"0.8 0.8 0.8 rg 2 12 3 2 re f 2 12 3 2 re S",
        b"/Fm1 Do",
        b"EMC",
        b"/OC /oc4 BDC 0.3 0.3 0.3 rg " + circle(35, 20, 0.5) + b" f EMC",
        b"BT /F1 0.5 Tf 2.5 12.8 Td (P-101) Tj ET",
        b"BT /F1 0.4 Tf 8.3 12.5 Td [(" + hx[:2] + b") -30 (" + hx[2:] + b")] TJ ET",
        b"BT /F2 0.6 Tf 3 5 Td <000100020003> Tj ET",
        b"Q",
    ]) + b"\n"


def page2_content() -> bytes:
    s = "%.15g" % S
    return b"\n".join([
        b"q " + (f"{s} 0 0 {s} 30 30 cm").encode(),
        b"0.02 w /OC /oc1 BDC 0 0 20 15 re S EMC",
        b"/OC /oc2 BDC 2 2 6 4 re S EMC",
        b"BT /F1 0.5 Tf 3 3.5 Td (CTRL) Tj ET",
        b"Q",
    ]) + b"\n"


def plan_objects(hx: bytes, p2_filter: str = "flate") -> dict:
    o = {}
    o[1] = b"<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [5 0 R 6 0 R 7 0 R 8 0 R] /D << /Order [5 0 R 6 0 R 7 0 R 8 0 R] >> >> >>"
    o[2] = b"<< /Type /Pages /Kids [3 0 R 13 0 R] /Count 2 /Resources 4 0 R >>"
    o[3] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1300 900] /Contents 9 0 R >>"
    o[4] = (b"<< /Font << /F1 10 0 R /F2 11 0 R >> /XObject << /Fm1 12 0 R >> "
            b"/Properties << /oc1 5 0 R /oc2 6 0 R /oc3 7 0 R /oc4 8 0 R >> >>")
    o[5] = b"<< /Type /OCG /Name (BOUNDARY) >>"
    o[6] = b"<< /Type /OCG /Name (ROOM) >>"
    o[7] = b"<< /Type /OCG /Name <FEFF00450051005500490050> >>"      # 'EQUIP' (UTF-16BE 텍스트 문자열)
    o[8] = b"<< /Type /OCG /Name (COLUMN) >>"
    o[9] = stream_obj(b"", page1_content(hx))
    o[10] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    o[11] = (b"<< /Type /Font /Subtype /Type0 /BaseFont /MalgunGothic /Encoding /Identity-H "
             b"/DescendantFonts [<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MalgunGothic "
             b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 1000 >>] "
             b"/ToUnicode 14 0 R >>")
    o[12] = stream_obj(b"/Type /XObject /Subtype /Form /BBox [0 0 3 2] /Matrix [1 0 0 1 8 12]",
                       b"0 0 2.5 1.5 re S\n")
    o[13] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 900 700] /Rotate 90 /Contents 15 0 R >>"
    o[14] = stream_obj(b"", TOUNICODE)
    o[15] = stream_obj(b"", page2_content(), p2_filter)
    return o


def make_pdfs():
    base = write_classic(plan_objects(b"HX-1"), 1)
    upd = plan_objects(b"HX-2")
    classic = append_update(base, {9: upd[9]}, 1)
    with open(os.path.join(OUT, "plan_classic.pdf"), "wb") as fp:
        fp.write(classic)
    with open(os.path.join(OUT, "plan_xrefstm.pdf"), "wb") as fp:
        fp.write(write_xref_stream(plan_objects(b"HX-2", "a85flate"), 1))

    # /VP /Measure — 1 pt = 0.0352777… m (1:100)
    m = {}
    m[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    m[2] = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
    m[3] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Contents 4 0 R "
            b"/VP [<< /Type /Viewport /BBox [0 0 600 400] /Measure << /Type /Measure /Subtype /RL "
            b"/R (1:100) /X [<< /Type /NumberFormat /U (m) /C %.17g /D 100 >>] >> >>] >>"
            % (0.0254 / 72 * 100))
    s = "%.15g" % S
    m[4] = stream_obj(b"", (f"q {s} 0 0 {s} 20 20 cm 0.5 g 1 1 5 3 re f Q\n").encode())
    with open(os.path.join(OUT, "plan_measure.pdf"), "wb") as fp:
        fp.write(write_classic(m, 1))

    # 스캔 PDF
    sc = {}
    sc[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    sc[2] = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
    sc[3] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
             b"/Resources << /XObject << /Im1 5 0 R >> >> >>")
    sc[4] = stream_obj(b"", b"q 600 0 0 780 6 6 cm /Im1 Do Q\n")
    img = bytes((x * 13 + y * 7) & 255 for y in range(8) for x in range(8))
    sc[5] = stream_obj(b"/Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceGray "
                       b"/BitsPerComponent 8", img)
    with open(os.path.join(OUT, "scanned.pdf"), "wb") as fp:
        fp.write(write_classic(sc, 1))

    # 암호화 PDF (트레일러 /Encrypt)
    en = dict(sc)
    en[6] = b"<< /Filter /Standard /V 2 /R 3 /Length 128 /P -4 /O <00> /U <00> >>"
    with open(os.path.join(OUT, "encrypted.pdf"), "wb") as fp:
        fp.write(write_classic(en, 1, b"/Encrypt 6 0 R /ID [<00> <00>] "))


# ---------------------------------------------------------------------------
# PNG 작성기
# ---------------------------------------------------------------------------

W, H = 600, 400


def plan_classes() -> bytearray:
    """0 바탕, 1 벽, 2 기둥, 3 주황 구역."""
    g = bytearray(W * H)

    def fill(x0, y0, x1, y1, v):
        for y in range(y0, y1):
            g[y * W + x0:y * W + x1] = bytes([v]) * (x1 - x0)
    fill(20, 20, 580, 380, 1)
    fill(26, 26, 574, 374, 0)
    fill(220, 26, 226, 374, 1)
    fill(400, 26, 406, 374, 1)
    fill(400, 200, 406, 210, 0)      # 문틈 10 px
    fill(100, 100, 120, 120, 2)      # 기둥 20×20 px = 1×1 m
    fill(260, 60, 340, 140, 3)       # 구역 80×80 px = 4×4 m
    return g


def _filter_row(ft: int, row: bytes, prev: bytes, bpp: int) -> bytes:
    out = bytearray(len(row))
    for i in range(len(row)):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        if ft == 0:
            p = 0
        elif ft == 1:
            p = a
        elif ft == 2:
            p = b
        elif ft == 3:
            p = (a + b) >> 1
        else:
            pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
            p = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
        out[i] = (row[i] - p) & 255
    return bytes([ft]) + bytes(out)


def _pack(samples: bytes, bd: int) -> bytes:
    if bd == 8:
        return bytes(samples)
    if bd == 16:
        return b"".join(bytes([v, v]) for v in samples)
    per = 8 // bd
    out = bytearray()
    for i in range(0, len(samples), per):
        v = 0
        chunk = samples[i:i + per]
        for k in range(per):
            v = (v << bd) | (chunk[k] if k < len(chunk) else 0)
        out.append(v)
    return bytes(out)


def _filtered(pixels: list, w: int, h: int, ch: int, bd: int, row0: int = 0) -> bytes:
    """pixels: 행별 표본 목록(표본당 1 값, 16비트는 상위 바이트를 복제)."""
    bpp = max(1, ch * bd // 8)
    out = bytearray()
    prev = None
    for r in range(h):
        row = _pack(bytes(pixels[r]), bd)
        if prev is None:
            prev = bytes(len(row))
        out += _filter_row((r + row0) % 5, row, prev, bpp)
        prev = row
    return bytes(out)


def _chunk(t: bytes, d: bytes) -> bytes:
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)


def write_png(path, w, h, ct, bd, sample_rows, plte=None, trns=None, interlace=False, phys=None):
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    if not interlace:
        raw = _filtered(sample_rows, w, h, ch, bd)
    else:
        raw = bytearray()
        for (x0, y0, dx, dy) in ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                                 (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)):
            xs = list(range(x0, w, dx))
            ys = list(range(y0, h, dy))
            if not xs or not ys:
                continue
            sub = []
            for y in ys:
                row = sample_rows[y]
                sub.append([row[x * ch + c] for x in xs for c in range(ch)])
            raw += _filtered(sub, len(xs), len(ys), ch, bd)
    data = b"\x89PNG\r\n\x1a\n"
    data += _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bd, ct, 0, 0, 1 if interlace else 0))
    if phys:
        data += _chunk(b"pHYs", struct.pack(">IIB", phys, phys, 1))
    if plte:
        data += _chunk(b"PLTE", plte)
    if trns:
        data += _chunk(b"tRNS", trns)
    data += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    data += _chunk(b"IEND", b"")
    with open(path, "wb") as fp:
        fp.write(data)


def make_pngs():
    g = plan_classes()
    rows = [g[y * W:(y + 1) * W] for y in range(H)]
    gray = {0: 255, 1: 30, 2: 30, 3: 255}
    write_png(os.path.join(OUT, "plan.png"), W, H, 0, 8,
              [[gray[v] for v in r] for r in rows], phys=2835)
    pal = bytes([255, 255, 255, 20, 30, 90, 20, 30, 90, 255, 200, 100])
    write_png(os.path.join(OUT, "plan_palette.png"), W, H, 3, 4,
              [[v for v in r] for r in rows], plte=pal, trns=bytes([255, 255, 255, 255]))
    rgb = {0: (255, 255, 255), 1: (40, 40, 40), 2: (40, 40, 40), 3: (255, 200, 100)}
    write_png(os.path.join(OUT, "plan_rgb_adam7.png"), W, H, 2, 8,
              [[c for v in r for c in rgb[v]] for r in rows], interlace=True)
    write_png(os.path.join(OUT, "plan_ga16.png"), W, H, 4, 16,
              [[c for v in r for c in (gray[v], 255)] for r in rows])


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    make_pdfs()
    make_pngs()
    for f in sorted(os.listdir(OUT)):
        if f.endswith((".pdf", ".png")):
            print(f, os.path.getsize(os.path.join(OUT, f)))
