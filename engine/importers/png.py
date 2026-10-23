# -*- coding: utf-8 -*-
"""
importers/png.py — 래스터 평면도(PNG) → Takeoff. 표준 라이브러리만, 결정론적.

처리 순서
  1. PNG 복호 (zlib + 5종 필터 + Adam7 인터레이스, 비트깊이 1/2/4/8/16, 색형식 0/2/3/4/6)
  2. 휘도(그레이) 변환 → 이진화 (opts['threshold'] 또는 Otsu). 어두운 화소 = 벽/잉크
  3. 밝은 화소 4-연결 영역분할 (런 길이 기반 union-find — 화소 단위 파이썬 객체를 만들지 않는다)
     · 이미지 가장자리에 닿는 영역 = 건물 바깥 → 버림
     · 나머지 닫힌 영역 = 실(room). 외접 사각형만 쓴다 (윤곽 추적 안 함)
  4. 어두운 화소 8-연결 영역 중 꽉 찬(채움률>0.9) 벽보다 두꺼운 덩어리 = 기둥 등 obstacle
  5. opts['color_map'] = {"#RRGGBB": kind} 이면 그 색 영역을 해당 kind 로

축척: 화소에는 실제 크기가 없다. px_per_m(1 m 가 몇 px) 또는 m_per_unit(1 px 가 몇 m) 필수.
pHYs(DPI)는 종이 해상도일 뿐이라 정보로만 기록한다.
문자: OCR 은 하지 않는다 (표준 라이브러리로 결정론적 OCR 불가) → id 는 RM001… 순번.

옵션
  px_per_m / m_per_unit : 축척 (필수)
  threshold     : 0~255. 이 값 미만 = 어두움. 생략 시 Otsu
  close_gaps_px : 문틈 닫기 반경(원본 px). 어두운 화소를 k 만큼 팽창시킨 뒤 영역분할,
                  실 외접 사각형을 k 만큼 되돌린다(팽창으로 줄어든 만큼 복원). 기본 0(끔)
  downsample    : 정수 축소 배율. 기본 auto(긴 변 ≤ 2000 px). 축소는 '어두움 우선'(OR 풀링)
  color_map     : {"#RRGGBB": kind} ,  color_tol : 채널별 허용차 (기본 24)
  default_kind  : 닫힌 밝은 영역의 kind (기본 room)
  floor         : 층 번호 (기본 0)
  min_size_m    : 이보다 작은 조각은 버린다 (기본 0.3)
"""
from __future__ import annotations

import math
import re
import struct
import zlib
from collections import Counter
from itertools import accumulate
from typing import Dict, List, Optional, Tuple

from .common import (KINDS, ImportErrorUser, boundary_from_items, finalize, make_item,
                     new_takeoff, normalize_origin, resolve_scale)

PNG_SIG = b"\x89PNG\r\n\x1a\n"
MAX_PIXELS = 120_000_000          # 이보다 크면 메모리 보호를 위해 거부
AUTO_MAX_SIDE = 2000

_AND255 = (255).__and__


# ---------------------------------------------------------------------------
# 필터 해제 (PDF 의 PNG 예측자도 이 함수를 쓴다)
# ---------------------------------------------------------------------------

_mask_cache: Dict[int, Tuple[int, int]] = {}


def _masks(n: int) -> Tuple[int, int]:
    m = _mask_cache.get(n)
    if m is None:
        m = (int.from_bytes(b"\x7f" * n, "little"), int.from_bytes(b"\x80" * n, "little"))
        if len(_mask_cache) < 64:
            _mask_cache[n] = m
    return m


def _add_bytes(a, b) -> bytearray:
    """바이트별 (a+b) & 255 — 큰 정수 SWAR 로 한 번에 (자리올림이 옆 바이트로 안 넘어가게)."""
    n = len(a)
    m7, m8 = _masks(n)
    x = int.from_bytes(a, "little")
    y = int.from_bytes(b, "little")
    s = ((x & m7) + (y & m7)) ^ ((x ^ y) & m8)
    return bytearray(s.to_bytes(n, "little"))


def unfilter(raw: bytes, nrows: int, rowlen: int, bpp: int) -> bytearray:
    """
    PNG 필터(0 None, 1 Sub, 2 Up, 3 Average, 4 Paeth) 해제.
    raw = 각 행 [필터 바이트 + rowlen 바이트]. 결과 = 필터 바이트를 뺀 nrows*rowlen 바이트.
    """
    out = bytearray(nrows * rowlen)
    prev = bytearray(rowlen)
    prev_zero = True
    pos = 0
    n = len(raw)
    for r in range(nrows):
        if pos >= n:
            break
        ft = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + rowlen])
        if len(line) < rowlen:
            line.extend(b"\x00" * (rowlen - len(line)))
        pos += 1 + rowlen
        if ft == 0:
            pass
        elif ft == 1 or (ft == 4 and prev_zero):
            # Sub (첫 행의 Paeth 도 Sub 와 같다): 채널별 누적합 mod 256 — C 수준 반복
            for c in range(min(bpp, rowlen)):
                line[c::bpp] = bytes(map(_AND255, accumulate(line[c::bpp])))
        elif ft == 2:
            if not prev_zero:
                line = _add_bytes(line, prev)
        elif ft == 3:
            if prev_zero:
                for i in range(bpp, rowlen):
                    line[i] = (line[i] + (line[i - bpp] >> 1)) & 255
            else:
                for i in range(min(bpp, rowlen)):
                    line[i] = (line[i] + (prev[i] >> 1)) & 255
                for i in range(bpp, rowlen):
                    line[i] = (line[i] + ((line[i - bpp] + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(min(bpp, rowlen)):
                line[i] = (line[i] + prev[i]) & 255
            for i in range(bpp, rowlen):
                a = line[i - bpp]
                b = prev[i]
                c = prev[i - bpp]
                pa = b - c
                pb = a - c
                pc = pa + pb
                if pa < 0:
                    pa = -pa
                if pb < 0:
                    pb = -pb
                if pc < 0:
                    pc = -pc
                if pa <= pb and pa <= pc:
                    line[i] = (line[i] + a) & 255
                elif pb <= pc:
                    line[i] = (line[i] + b) & 255
                else:
                    line[i] = (line[i] + c) & 255
        else:
            raise ImportErrorUser(f"PNG 행 {r} 의 필터 형식 {ft} 가 잘못됐다 — 파일이 손상됐다")
        out[r * rowlen:(r + 1) * rowlen] = line
        prev = line
        prev_zero = False
    return out


# ---------------------------------------------------------------------------
# PNG 복호
# ---------------------------------------------------------------------------

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_VALID_DEPTH = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
          (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))

_unpack_cache: Dict[int, List[bytes]] = {}


def _unpack_table(bd: int) -> List[bytes]:
    t = _unpack_cache.get(bd)
    if t is None:
        per = 8 // bd
        mask = (1 << bd) - 1
        t = [bytes(((v >> (8 - bd * (k + 1))) & mask) for k in range(per)) for v in range(256)]
        _unpack_cache[bd] = t
    return t


def _samples_from_rows(rows: bytearray, w: int, h: int, ch: int, bd: int) -> bytearray:
    """필터 해제된 행들 → 표본당 1 바이트 (16비트는 상위 바이트, 1/2/4비트는 원래 값)."""
    if bd == 8:
        return rows
    if bd == 16:
        return bytearray(rows[0::2])
    rowlen = (w * ch * bd + 7) // 8
    tab = _unpack_table(bd)
    out = bytearray()
    need = w * ch
    for r in range(h):
        seg = rows[r * rowlen:(r + 1) * rowlen]
        out += b"".join([tab[v] for v in seg])[:need]
    return out


def decode_png(data: bytes) -> dict:
    """PNG 바이트 → dict(w, h, ct, bd, ch, samples(bytearray, w*h*ch), palette, trns, phys, warnings)."""
    if not data.startswith(PNG_SIG):
        raise ImportErrorUser("PNG 서명이 아니다")
    pos = 8
    n = len(data)
    ihdr = None
    plte = None
    trns = None
    phys = None
    idat = []
    warns: List[str] = []
    while pos + 8 <= n:
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        crc_b = data[pos + 8 + length:pos + 12 + length]
        if len(body) < length:
            warns.append(f"PNG 청크 {ctype!r} 가 잘려 있다 — 읽은 데까지만 쓴다")
        if len(crc_b) == 4:
            crc = struct.unpack(">I", crc_b)[0]
            if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
                warns.append(f"PNG 청크 {ctype.decode('latin-1')} CRC 불일치 — 손상 가능성")
        pos += 12 + length
        if ctype == b"IHDR":
            if len(body) < 13:
                raise ImportErrorUser("PNG IHDR 가 손상됐다")
            ihdr = struct.unpack(">IIBBBBB", body[:13])
        elif ctype == b"PLTE":
            plte = body
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"pHYs" and len(body) >= 9:
            px, py, unit = struct.unpack(">IIB", body[:9])
            phys = (px, py, unit)
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
        elif ctype[:1].isupper() and ctype not in (b"IHDR", b"PLTE", b"IDAT", b"IEND"):
            warns.append(f"알 수 없는 필수 청크 {ctype.decode('latin-1')} — 무시했다")
    if ihdr is None:
        raise ImportErrorUser("PNG 에 IHDR 가 없다 — 파일이 손상됐다")
    w, h, bd, ct, comp, filt, inter = ihdr
    if ct not in _CHANNELS or bd not in _VALID_DEPTH[ct]:
        raise ImportErrorUser(f"PNG 색형식 {ct}/비트깊이 {bd} 조합은 PNG 규격에 없다 — 파일이 손상됐다")
    if comp != 0 or filt != 0 or inter not in (0, 1):
        raise ImportErrorUser("PNG 압축/필터/인터레이스 방식 값이 규격 밖이다")
    if w <= 0 or h <= 0:
        raise ImportErrorUser("PNG 크기가 0 이다")
    if w * h > MAX_PIXELS:
        raise ImportErrorUser(
            f"PNG 가 너무 크다 ({w}×{h}). {MAX_PIXELS // 1_000_000} Mpx 이하로 줄여서 넣을 것")
    if ct == 3 and plte is None:
        raise ImportErrorUser("팔레트 PNG(색형식 3)에 PLTE 가 없다")
    if not idat:
        raise ImportErrorUser("PNG 에 이미지 데이터(IDAT)가 없다 — 파일이 잘렸거나 손상됐다")
    ch = _CHANNELS[ct]
    try:
        d = zlib.decompressobj()
        raw = d.decompress(b"".join(idat))
        raw += d.flush()
    except zlib.error as e:
        raise ImportErrorUser(f"PNG 이미지 데이터(zlib) 복호 실패: {e} — 파일이 손상됐다")
    bpp = max(1, ch * bd // 8)

    if inter == 0:
        rowlen = (w * ch * bd + 7) // 8
        need = h * (rowlen + 1)
        if len(raw) < need:
            warns.append("PNG 이미지 데이터가 짧다 — 모자란 행은 0 으로 채웠다")
        rows = unfilter(raw, h, rowlen, bpp)
        samples = _samples_from_rows(rows, w, h, ch, bd)
    else:
        samples = bytearray(w * h * ch)
        off = 0
        for (x0, y0, dx, dy) in _ADAM7:
            pw = (w - x0 + dx - 1) // dx if w > x0 else 0
            ph = (h - y0 + dy - 1) // dy if h > y0 else 0
            if pw == 0 or ph == 0:
                continue
            rowlen = (pw * ch * bd + 7) // 8
            seg = raw[off:off + ph * (rowlen + 1)]
            off += ph * (rowlen + 1)
            prow = unfilter(seg, ph, rowlen, bpp)
            ps = _samples_from_rows(prow, pw, ph, ch, bd)
            step = dx * ch
            for r in range(ph):
                y = y0 + r * dy
                src = ps[r * pw * ch:(r + 1) * pw * ch]
                base = (y * w + x0) * ch
                for c in range(ch):
                    samples[base + c:base + c + (pw - 1) * step + 1:step] = src[c::ch]
    return dict(w=w, h=h, ct=ct, bd=bd, ch=ch, samples=samples, palette=plte,
                trns=trns, phys=phys, interlaced=bool(inter), warnings=warns)


# ---------------------------------------------------------------------------
# 그레이·색 평면
# ---------------------------------------------------------------------------

def _lut_sum(r: bytes, g: bytes, b: bytes) -> bytearray:
    """휘도 ≈ 0.299R + 0.587G + 0.114B (각 항 내림 → 합 ≤ 255 이므로 큰 정수 덧셈에 자리올림 없음)."""
    lr = bytes((299 * v) // 1000 for v in range(256))
    lg = bytes((587 * v) // 1000 for v in range(256))
    lb = bytes((114 * v) // 1000 for v in range(256))
    n = len(r)
    s = (int.from_bytes(r.translate(lr), "little") + int.from_bytes(g.translate(lg), "little")
         + int.from_bytes(b.translate(lb), "little"))
    return bytearray(s.to_bytes(n, "little"))


def _or_bytes(a: bytes, b: bytes) -> bytes:
    n = len(a)
    return (int.from_bytes(a, "little") | int.from_bytes(b, "little")).to_bytes(n, "little")


def _and_bytes(a: bytes, b: bytes) -> bytes:
    n = len(a)
    return (int.from_bytes(a, "little") & int.from_bytes(b, "little")).to_bytes(n, "little")


def planes(img: dict, want_rgb: bool) -> Tuple[bytes, Optional[Tuple[bytes, bytes, bytes]], List[str]]:
    """→ (gray w*h, (R,G,B) 또는 None, 경고). 투명 화소(알파<128)는 흰 배경으로 본다."""
    ct, bd, ch, s = img["ct"], img["bd"], img["ch"], img["samples"]
    warns: List[str] = []
    rgb = None
    alpha = None
    if ct == 0:
        maxv = (1 << min(bd, 8)) - 1
        scale = bytes(min(255, (v * 255) // maxv) if v <= maxv else 255 for v in range(256))
        if img["trns"] and len(img["trns"]) >= 2:
            key = struct.unpack(">H", img["trns"][:2])[0]
            if bd == 16:
                key >>= 8
            scale = bytearray(scale)
            if key < 256:
                scale[key] = 255
            scale = bytes(scale)
        gray = bytes(s).translate(scale) if bd < 8 or img["trns"] else bytes(s)
        if want_rgb:
            rgb = (gray, gray, gray)
    elif ct == 3:
        pl = img["palette"]
        npal = len(pl) // 3
        al = img["trns"] or b""
        lr, lg, lb, lgray = bytearray(256), bytearray(256), bytearray(256), bytearray(256)
        for i in range(256):
            if i < npal:
                r, g, b = pl[3 * i], pl[3 * i + 1], pl[3 * i + 2]
            else:
                r = g = b = 0
            a = al[i] if i < len(al) else 255
            # 흰 배경 위 합성 (정확)
            r = (r * a + 255 * (255 - a)) // 255
            g = (g * a + 255 * (255 - a)) // 255
            b = (b * a + 255 * (255 - a)) // 255
            lr[i], lg[i], lb[i] = r, g, b
            lgray[i] = (299 * r) // 1000 + (587 * g) // 1000 + (114 * b) // 1000
        idx = bytes(s)
        gray = idx.translate(bytes(lgray))
        if want_rgb:
            rgb = (idx.translate(bytes(lr)), idx.translate(bytes(lg)), idx.translate(bytes(lb)))
    elif ct == 4:
        gray = bytes(s[0::2])
        alpha = bytes(s[1::2])
        if want_rgb:
            rgb = (gray, gray, gray)
    else:  # 2, 6
        r, g, b = bytes(s[0::ch]), bytes(s[1::ch]), bytes(s[2::ch])
        if ct == 2 and img["trns"] and len(img["trns"]) >= 6:
            warns.append("RGB 색키 투명(tRNS)은 무시했다 — 투명색이 어두우면 벽으로 보일 수 있다")
        if ct == 6:
            alpha = bytes(s[3::4])
        gray = bytes(_lut_sum(r, g, b))
        if want_rgb:
            rgb = (r, g, b)
    if alpha is not None and alpha.count(255) != len(alpha):
        # 알파 < 128 → 흰색. OR 마스크 (0xFF 또는 0x00)
        amask = alpha.translate(bytes(255 if v < 128 else 0 for v in range(256)))
        gray = _or_bytes(gray, amask)
        if rgb is not None:
            rgb = tuple(_or_bytes(p, amask) for p in rgb)  # type: ignore
        warns.append("알파 채널: 반투명 이하(알파<128) 화소를 흰 배경으로 보았다 (합성 근사)")
    return bytes(gray), rgb, warns


def otsu(gray: bytes) -> int:
    """Otsu 임계값 t (0..254): gray ≤ t 가 어두움. 동점이면 작은 t (결정론)."""
    cnt = Counter(gray)
    hist = [cnt.get(v, 0) for v in range(256)]
    total = len(gray)
    sum_all = sum(v * hist[v] for v in range(256))
    w0 = 0
    s0 = 0
    best_t, best = 127, -1.0
    for t in range(255):
        w0 += hist[t]
        s0 += t * hist[t]
        w1 = total - w0
        if w0 == 0 or w1 == 0:
            continue
        m0 = s0 / w0
        m1 = (sum_all - s0) / w1
        between = w0 * w1 * (m0 - m1) ** 2
        if between > best + 1e-9 * max(1.0, best):
            best, best_t = between, t
    return best_t


# ---------------------------------------------------------------------------
# 영역분할 — 런 길이 union-find
# ---------------------------------------------------------------------------

_RUN1 = re.compile(rb"\x01+")
_RUN0 = re.compile(rb"\x00+")


def components(mask: bytes, w: int, h: int, value: int, eight: bool = False):
    """
    mask(0/1 바이트) 에서 value 화소의 연결요소.
    → (comps, run_y, run_a, run_b, run_comp)
      comps: [dict(count, x0, y0, x1, y1, border)] (x1,y1 배타), 첫 화소 주사 순서
    """
    rx = _RUN1 if value else _RUN0
    parent: List[int] = []
    ry: List[int] = []
    ra: List[int] = []
    rb: List[int] = []
    prev: List[Tuple[int, int, int]] = []
    adj = 1 if eight else 0

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for y in range(h):
        base = y * w
        cur = []
        for m in rx.finditer(mask, base, base + w):
            a, b = m.span()
            a -= base
            b -= base
            rid = len(parent)
            parent.append(rid)
            ry.append(y)
            ra.append(a)
            rb.append(b)
            cur.append((a, b, rid))
        if prev and cur:
            i = j = 0
            lp, lc = len(prev), len(cur)
            while i < lp and j < lc:
                pa, pb, pid = prev[i]
                ca, cb, cid = cur[j]
                if pa < cb + adj and ca < pb + adj:
                    x, z = find(pid), find(cid)
                    if x != z:
                        if x < z:
                            parent[z] = x
                        else:
                            parent[x] = z
                if pb < cb:
                    i += 1
                else:
                    j += 1
        prev = cur
    comp_of: Dict[int, int] = {}
    comps: List[dict] = []
    run_comp = [0] * len(parent)
    for rid in range(len(parent)):
        root = find(rid)
        k = comp_of.get(root)
        y, a, b = ry[rid], ra[rid], rb[rid]
        if k is None:
            k = len(comps)
            comp_of[root] = k
            comps.append(dict(count=0, x0=a, y0=y, x1=b, y1=y + 1, border=False))
        c = comps[k]
        c["count"] += b - a
        if a < c["x0"]:
            c["x0"] = a
        if b > c["x1"]:
            c["x1"] = b
        if y + 1 > c["y1"]:
            c["y1"] = y + 1
        if a == 0 or b == w or y == 0 or y == h - 1:
            c["border"] = True
        run_comp[rid] = k
    return comps, ry, ra, rb, run_comp


def _downsample_mask(mask: bytes, w: int, h: int, k: int) -> Tuple[bytes, int, int]:
    """k×k 블록 중 하나라도 어두우면 어두움 (얇은 벽 보존). 나머지 행·열은 버린다."""
    wo, ho = w // k, h // k
    out = bytearray()
    for r in range(ho):
        acc = 0
        for j in range(k):
            y = r * k + j
            acc |= int.from_bytes(mask[y * w:y * w + wo * k], "little")
        row = acc.to_bytes(wo * k, "little")
        racc = 0
        for j in range(k):
            racc |= int.from_bytes(row[j::k][:wo], "little")
        out += racc.to_bytes(wo, "little")
    return bytes(out), wo, ho


def _downsample_plane(p: bytes, w: int, h: int, k: int) -> bytes:
    wo, ho = w // k, h // k
    out = bytearray()
    for r in range(ho):
        y = r * k
        out += p[y * w:y * w + wo * k:k]
    return bytes(out)


def _dilate(mask: bytes, w: int, h: int, k: int) -> bytes:
    """정사각 구조요소 (2k+1)² 팽창. 가로(정수 시프트) → 세로(행 OR) 분리 수행."""
    full = (1 << (8 * w)) - 1
    rows = []
    for y in range(h):
        v = int.from_bytes(mask[y * w:(y + 1) * w], "little")
        acc = v
        for s in range(1, k + 1):
            acc |= (v << (8 * s)) | (v >> (8 * s))
        rows.append(acc & full)
    out = bytearray()
    for y in range(h):
        acc = 0
        for yy in range(max(0, y - k), min(h, y + k + 1)):
            acc |= rows[yy]
        out += acc.to_bytes(w, "little")
    return bytes(out)


def _run_mode(mask: bytes, w: int, h: int) -> Tuple[Optional[int], dict]:
    """어두운 런 길이(가로+세로)의 최빈값 = 벽 두께 추정. 길이 1(잡음)과 긴 변/10 초과(벽 길이 방향) 제외."""
    hist: Counter = Counter()
    lim = max(2, max(w, h) // 10)
    for y in range(h):
        for m in _RUN1.finditer(mask, y * w, (y + 1) * w):
            L = m.end() - m.start()
            if 2 <= L <= lim:
                hist[L] += 1
    for x in range(w):
        col = mask[x::w]
        for m in _RUN1.finditer(col):
            L = m.end() - m.start()
            if 2 <= L <= lim:
                hist[L] += 1
    if not hist:
        return None, {}
    best = min(hist.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    top = dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))[:5])
    return best, {str(k): v for k, v in top.items()}


def _parse_color(key: str) -> Tuple[int, int, int]:
    s = str(key).strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", s):
        raise ImportErrorUser(f"color_map 의 색 '{key}' 는 #RRGGBB 형식이어야 한다")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def read(data: bytes, filename: str, opts: dict) -> dict:
    tk = new_takeoff(filename, "png", data)
    img = decode_png(data)
    tk["warnings"] += img["warnings"]
    W, H = img["w"], img["h"]

    phys_note = ""
    if img["phys"] and img["phys"][2] == 1 and img["phys"][0] > 0:
        dpi = img["phys"][0] * 0.0254
        phys_note = (f" (참고: pHYs 종이 해상도 {dpi:.0f} dpi — 종이 축척 1:N 인쇄본을 스캔한 것이면 "
                     f"px_per_m = {img['phys'][0]}/N)")
    if opts.get("px_per_m") in (None, "") and opts.get("m_per_unit") in (None, ""):
        raise ImportErrorUser(
            "PNG 래스터 도면의 축척을 알 수 없다. px_per_m(실제 1 m 가 몇 px 인가) 또는 "
            "m_per_unit(1 px 가 몇 m 인가)을 지정할 것. 도면의 치수선 하나를 재서 "
            "px 길이 ÷ 실제 m 로 구하면 된다. 축척은 추정하지 않는다." + phys_note)
    m_per_px, basis = resolve_scale(opts, None, "png")

    color_map = opts.get("color_map") or {}
    cmap: List[Tuple[Tuple[int, int, int], str, str]] = []
    for key in sorted(color_map):
        kind = str(color_map[key])
        if kind not in KINDS and kind != "boundary":
            raise ImportErrorUser(f"color_map['{key}'] = '{kind}' — kind 는 {', '.join(KINDS)} 또는 boundary")
        rgbk = _parse_color(key)
        cmap.append((rgbk, kind, "#%02X%02X%02X" % rgbk))

    gray, rgb, w2 = planes(img, bool(cmap))
    tk["warnings"] += w2
    img = None  # 메모리 해제

    # 이진화
    if opts.get("threshold") not in (None, ""):
        thr = int(opts["threshold"])
        if not 0 <= thr <= 256:
            raise ImportErrorUser("threshold 는 0~256 정수 (이 값 미만 = 어두움)")
        lut = bytes(1 if v < thr else 0 for v in range(256))
        thr_note = f"사용자 임계값 {thr} 미만"
    else:
        t = otsu(gray)
        lut = bytes(1 if v <= t else 0 for v in range(256))
        thr_note = f"Otsu 임계값 ≤{t}"
    mask = gray.translate(lut)
    gray = None

    # 축소
    ds = opts.get("downsample", "auto")
    if ds in (None, "", "auto"):
        ds = max(1, math.ceil(max(W, H) / AUTO_MAX_SIDE))
    else:
        ds = int(ds)
        if ds < 1:
            raise ImportErrorUser("downsample 은 1 이상의 정수")
    w, h = W, H
    if ds > 1:
        mask, w, h = _downsample_mask(mask, W, H, ds)
        if rgb is not None:
            rgb = tuple(_downsample_plane(p, W, H, ds) for p in rgb)  # type: ignore
        basis += f", {ds}배 축소(어두움 우선 풀링) → 작업 1 px = {m_per_px * ds:g} m"
    mpp = m_per_px * ds
    tk["scale_basis"] = basis
    floor = int(opts.get("floor", 0) or 0)
    min_size = float(opts.get("min_size_m", 0.3))
    min_px = max(1, int(math.floor(min_size / mpp)))

    def rect_m(x0, y0, x1, y1):
        """픽셀 [x0,x1)×[y0,y1) (y 아래로) → 미터 꼭짓점 (y 위로)."""
        return [(x0 * mpp, (h - y1) * mpp), (x1 * mpp, (h - y1) * mpp),
                (x1 * mpp, (h - y0) * mpp), (x0 * mpp, (h - y0) * mpp)]

    stats = tk["stats"]
    stats.update(image_px=[W, H], work_px=[w, h], downsample=ds, threshold=thr_note,
                 dark_fraction=round(mask.count(1) / max(1, len(mask)), 4))

    wall_t, top_runs = _run_mode(mask, w, h)
    stats["wall_thickness_px"] = wall_t
    stats["wall_thickness_m"] = round(wall_t * mpp, 6) if wall_t else None
    stats["dark_run_hist_top"] = top_runs

    # 색 영역
    cmask_all = None
    color_items = 0
    if cmap and rgb is not None:
        tol = int(opts.get("color_tol", 24))
        R, G, B = rgb
        for (cr, cg, cb), kind, hexs in cmap:
            def near(v0):
                return bytes(1 if abs(v - v0) <= tol else 0 for v in range(256))
            m = _and_bytes(_and_bytes(R.translate(near(cr)), G.translate(near(cg))),
                           B.translate(near(cb)))
            cmask_all = m if cmask_all is None else _or_bytes(cmask_all, m)
            comps, *_ = components(m, w, h, 1)
            for k, c in enumerate(comps):
                if c["x1"] - c["x0"] < min_px or c["y1"] - c["y0"] < min_px:
                    continue
                pts = rect_m(c["x0"], c["y0"], c["x1"], c["y1"])
                ref = f"px:color{hexs}:{c['x0']},{c['y0']},{c['x1']},{c['y1']}"
                if kind == "boundary":
                    x0, y0 = pts[0]
                    x1, y1 = pts[2]
                    tk["boundary"].append({"floor": floor, "rect": [round(x0, 6), round(y0, 6),
                                                                     round(x1, 6), round(y1, 6)],
                                           "source_ref": ref})
                    continue
                it = _px_item(kind, floor, pts, c, f"color:{hexs}", ref)
                tk["items"].append(it)
                color_items += 1
        stats["color_items"] = color_items
    elif cmap:
        tk["warnings"].append("color_map 을 줬지만 색 평면을 만들지 못했다")

    # 밝은 영역 = 실
    gap = int(opts.get("close_gaps_px", 0) or 0)
    if gap < 0:
        raise ImportErrorUser("close_gaps_px 는 0 이상의 정수")
    kg = int(math.ceil(gap / ds)) if gap else 0
    work = _dilate(mask, w, h, kg) if kg else mask
    comps, ry, ra, rb, rcomp = components(work, w, h, 0, eight=False)
    room_kind = str(opts.get("default_kind") or "room")
    outside = small = dup_color = 0
    rooms = []
    for k, c in enumerate(comps):
        if c["border"]:
            outside += 1
            continue
        bw, bh = c["x1"] - c["x0"] + 2 * kg, c["y1"] - c["y0"] + 2 * kg
        if bw < min_px or bh < min_px:
            small += 1
            continue
        rooms.append(k)
    if cmask_all is not None and rooms:
        colored = Counter()
        want = set(rooms)
        for rid in range(len(ry)):
            k = rcomp[rid]
            if k in want:
                base = ry[rid] * w
                colored[k] += cmask_all.count(1, base + ra[rid], base + rb[rid])
        keep = []
        for k in rooms:
            if colored[k] * 2 > comps[k]["count"]:
                dup_color += 1
            else:
                keep.append(k)
        rooms = keep
    for k in rooms:
        c = comps[k]
        x0, y0 = max(0, c["x0"] - kg), max(0, c["y0"] - kg)
        x1, y1 = min(w, c["x1"] + kg), min(h, c["y1"] + kg)
        fill = c["count"] / float((c["x1"] - c["x0"]) * (c["y1"] - c["y0"]))
        ref = f"px:region:{x0 * ds},{y0 * ds},{x1 * ds},{y1 * ds}"
        tk["items"].append(_px_item(room_kind, floor, rect_m(x0, y0, x1, y1),
                                    dict(c, fill=fill), "raster:region", ref))
    stats.update(light_regions=len(comps), outside_regions=outside, small_regions_ignored=small,
                 rooms=len(rooms), rooms_dropped_as_color=dup_color, close_gaps_px=gap)

    # 어두운 덩어리 = 기둥 등
    dcomps, *_ = components(mask, w, h, 1, eight=True)
    obst = 0
    wall_net = None
    tmin = (wall_t or 0) * 1.5
    for c in dcomps:
        bw, bh = c["x1"] - c["x0"], c["y1"] - c["y0"]
        if wall_net is None or c["count"] > wall_net["count"]:
            wall_net = c
        if c["border"] or bw < min_px or bh < min_px:
            continue
        fill = c["count"] / float(bw * bh)
        if fill > 0.9 and min(bw, bh) > tmin:
            ref = f"px:solid:{c['x0'] * ds},{c['y0'] * ds},{c['x1'] * ds},{c['y1'] * ds}"
            tk["items"].append(_px_item("obstacle", floor, rect_m(c["x0"], c["y0"], c["x1"], c["y1"]),
                                        dict(c, fill=fill), "raster:solid", ref))
            obst += 1
    stats.update(dark_regions=len(dcomps), obstacles=obst)

    # 경계: 가장 큰 어두운 연결요소(벽체망)의 외접 사각형이 모든 실을 품으면 그것
    if wall_net is not None and not tk["boundary"]:
        x0, y0, x1, y1 = wall_net["x0"], wall_net["y0"], wall_net["x1"], wall_net["y1"]
        ok = bool(rooms) and all(x0 <= comps[k]["x0"] and comps[k]["x1"] <= x1 and
                                 y0 <= comps[k]["y0"] and comps[k]["y1"] <= y1 for k in rooms)
        if ok:
            pts = rect_m(x0, y0, x1, y1)
            tk["boundary"].append({"floor": floor,
                                   "rect": [round(pts[0][0], 6), round(pts[0][1], 6),
                                            round(pts[2][0], 6), round(pts[2][1], 6)],
                                   "source_ref": f"px:wall-network:{x0 * ds},{y0 * ds},{x1 * ds},{y1 * ds}"})
            stats["boundary_basis"] = "가장 큰 벽체 연결요소의 외접 사각형"

    tk["floors"] = [{"index": floor, "label": f"{floor + 1}F"}]
    tk["warnings"].append(
        "PNG 는 문자 인식(OCR)을 하지 않는다 — id 는 RM001… 순번이다. 실·기기 이름은 사용자가 "
        "IR 에서 이름으로 붙일 것 (좌표가 아니라 이름만)")
    tk["warnings"].append(
        "래스터 실 윤곽은 추적하지 않는다 — 외접 사각형과 채움률(rect_fill)만 쓴다. "
        f"치수 오차는 벽 두께·화소 크기 수준(1 px = {mpp:g} m)")
    if gap:
        tk["warnings"].append(
            f"close_gaps_px={gap}: 어두운 화소를 {gap} px 팽창시켜 문틈을 닫고 영역분할했다 "
            f"(실 외접 사각형은 {gap} px 되돌림). 그보다 넓은 개구부는 실을 합친다")
    boundary_from_items(tk)
    normalize_origin(tk)
    finalize(tk, min_size)
    return tk


def _px_item(kind: str, floor: int, pts, comp: dict, layer: str, ref: str) -> dict:
    it = make_item(kind, floor, pts, layer=layer, source_ref=ref)
    bw = comp["x1"] - comp["x0"]
    bh = comp["y1"] - comp["y0"]
    fill = comp.get("fill", comp["count"] / float(max(1, bw * bh)))
    it["rect_fill"] = round(fill, 4)
    it["polygon"] = None if fill > 0.95 else [[round(p[0], 6), round(p[1], 6)] for p in pts]
    return it
