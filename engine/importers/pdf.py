# -*- coding: utf-8 -*-
"""
importers/pdf.py — 벡터 PDF(CAD 출력) → Takeoff. 표준 라이브러리만, 결정론적.

구조 해석
  - 고전 xref 표 + 교차참조 스트림(PDF 1.5+) + 객체 스트림(ObjStm /N /First)
  - 증분 갱신(/Prev, /XRefStm) — 새 판이 옛 판을 덮는다
  - xref 가 깨졌으면 'n g obj' 전수 탐색으로 복구
  - 필터: FlateDecode(PNG/TIFF 예측자 포함), LZWDecode, ASCIIHexDecode, ASCII85Decode, RunLengthDecode
  - 암호화 PDF 는 거부 (ImportErrorUser)

내용 스트림 해석
  - 그래픽 상태 q/Q, cm, 경로 m l c v y h re, 칠하기 S s f F f* B B* b b* n
  - Form XObject (Do, /Matrix, 깊이 제한), 이미지는 개수·면적만 센다
  - 색 RG/G/K/rg/g/k/sc/scn, 선택적 내용 /OC (BDC /OC /이름, XObject /OC) → OCG 이름 = 레이어
  - 문자 BT/ET, Tf Td TD Tm T* TL Tc Tw Tz Ts, Tj TJ ' "
    해독: 단순 글꼴 /Encoding(WinAnsi/Standard/MacRoman + /Differences), /ToUnicode CMap
    (bfchar/bfrange — 한글 CID 글꼴 Identity-H). 해독 못 한 문자는 버리고 센다 (지어내지 않는다)

기하 추출
  - 닫힌 부분경로(h / re / 첫점=끝점 / 칠하기) → 다각형. 곡선은 8분할 근사
  - 열린 직선 획 → 선분 → 축정렬 직사각형 고리(선으로 그린 실) 탐지
  - 중복 제거(칠+획 이중 출력), 용지 테두리(≥95% 면적)·도곽·표제란 제거

축척: PDF 좌표는 pt(1/72 in) 이지만 종이 크기일 뿐이다. scale_ratio(1:N 의 N) 또는
m_per_unit 필수. 단, 페이지에 /VP 뷰포트 /Measure(/X 수치형식 또는 /R 축척 문자열)가
있으면 그것을 쓰고 scale_basis 에 밝힌다.

옵션 (공통 외)
  page          : 1부터. 정수, 목록, "1,3-5", "all" (기본 1). 페이지 순서 = 층 순서
  floor         : 단일 페이지일 때 층 번호
  default_kind  : 분류 규칙에 안 걸린 닫힌 도형의 kind (기본 equipment)
  snap_pt       : 선분 끝점 맞춤 허용차 [pt] (기본 0.5)
"""
from __future__ import annotations

import base64
import binascii
import math
import re
import zlib
from bisect import bisect_right
from typing import Dict, List, Optional, Sequence, Tuple

from .common import (ImportErrorUser, assign_texts, boundary_from_items, classify, finalize,
                     make_item, new_takeoff, normalize_origin, resolve_scale, unit_factor)
from .png import unfilter

MAX_STREAM_BYTES = 256 << 20       # 스트림 하나 복호 상한 (압축 폭탄 방지)
MAX_FORM_DEPTH = 12
MAX_OPS = 30_000_000               # 페이지당 연산자 상한
MAX_PARSE_DEPTH = 64
CURVE_STEPS = 8
PT_M = 0.0254 / 72.0


class Name(str):
    __slots__ = ()


class Op(bytes):
    __slots__ = ()


class Ref:
    __slots__ = ("num", "gen")

    def __init__(self, num: int, gen: int):
        self.num = num
        self.gen = gen

    def __eq__(self, o):
        return isinstance(o, Ref) and o.num == self.num and o.gen == self.gen

    def __hash__(self):
        return hash((self.num, self.gen))

    def __repr__(self):
        return f"{self.num} {self.gen} R"


class Stream:
    __slots__ = ("dict", "raw", "num", "_dec")

    def __init__(self, d: dict, raw: bytes, num: int = -1):
        self.dict = d
        self.raw = raw
        self.num = num
        self._dec = None


class _DecodeError(Exception):
    pass


class _ImageData(Exception):
    pass


# ---------------------------------------------------------------------------
# 어휘 분석 / 객체 파서
# ---------------------------------------------------------------------------

_WSD = rb" \t\r\n\x0c\x00()<>\[\]{}/%"
_TOKEN_RX = re.compile(
    rb"(?P<ws>[ \t\r\n\x0c\x00]+|%[^\r\n]*)"
    rb"|(?P<num>[+-]?(?:\d+\.?\d*|\.\d+))(?![^" + _WSD + rb"])"
    rb"|(?P<name>/[^" + _WSD + rb"]*)"
    rb"|(?P<dopen><<)|(?P<dclose>>>)"
    rb"|(?P<hex><[0-9A-Fa-f \t\r\n\x0c\x00]*>)"
    rb"|(?P<lit>\()"
    rb"|(?P<aopen>\[)|(?P<aclose>\])"
    rb"|(?P<brace>[{}])"
    rb"|(?P<kw>[^" + _WSD + rb"]+)")
_LIT_SPECIAL = re.compile(rb"[\\()]")
_NAME_ESC = re.compile(rb"#([0-9A-Fa-f]{2})")
_ESC = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12,
        ord("("): 40, ord(")"): 41, ord("\\"): 92}
_HEX_WS = re.compile(rb"[^0-9A-Fa-f]")


def _parse_literal(data: bytes, pos: int) -> Tuple[bytes, int]:
    """'(' 다음 위치부터 → (바이트, 닫는 ')' 다음 위치). 괄호 균형·이스케이프·8진수."""
    out = bytearray()
    depth = 1
    n = len(data)
    i = pos
    while i < n:
        m = _LIT_SPECIAL.search(data, i)
        if m is None:
            out += data[i:]
            return bytes(out), n
        j = m.start()
        out += data[i:j]
        c = data[j]
        if c == 0x5C:
            j += 1
            if j >= n:
                break
            c = data[j]
            if c in _ESC:
                out.append(_ESC[c])
                j += 1
            elif 0x30 <= c <= 0x37:
                v = 0
                k = 0
                while k < 3 and j < n and 0x30 <= data[j] <= 0x37:
                    v = v * 8 + data[j] - 0x30
                    j += 1
                    k += 1
                out.append(v & 255)
            elif c == 0x0D:
                j += 1
                if j < n and data[j] == 0x0A:
                    j += 1
            elif c == 0x0A:
                j += 1
            else:
                out.append(c)
                j += 1
            i = j
        elif c == 0x28:
            depth += 1
            out.append(c)
            i = j + 1
        else:
            depth -= 1
            if depth == 0:
                return bytes(out), j + 1
            out.append(c)
            i = j + 1
    return bytes(out), n


def _hexstr(tok: bytes) -> bytes:
    h = _HEX_WS.sub(b"", tok[1:-1])
    if len(h) % 2:
        h += b"0"
    return binascii.unhexlify(h)


class Parser:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def token(self):
        """→ (종류, 값). 종류: num name str kw [ ] << >> None(EOF)"""
        data = self.data
        match = _TOKEN_RX.match
        while True:
            m = match(data, self.pos)
            if m is None:
                if self.pos >= len(data):
                    return None, None
                self.pos += 1          # 짝 없는 ')' '>' 등 — 건너뛴다
                continue
            self.pos = m.end()
            k = m.lastgroup
            if k == "ws":
                continue
            v = m.group()
            if k == "num":
                return "num", (float(v) if b"." in v else int(v))
            if k == "name":
                s = v[1:]
                if b"#" in s:
                    s = _NAME_ESC.sub(lambda mm: bytes([int(mm.group(1), 16)]), s)
                return "name", Name(s.decode("latin-1"))
            if k == "lit":
                s, self.pos = _parse_literal(data, self.pos)
                return "str", s
            if k == "hex":
                return "str", _hexstr(v)
            if k == "dopen":
                return "<<", None
            if k == "dclose":
                return ">>", None
            if k == "aopen":
                return "[", None
            if k == "aclose":
                return "]", None
            if k == "brace":
                return "kw", Op(v)
            return "kw", Op(v)

    def value(self, depth: int = 0, refs: bool = True):
        k, v = self.token()
        return self.value_from(k, v, depth, refs)

    def value_from(self, k, v, depth: int = 0, refs: bool = True):
        if depth > MAX_PARSE_DEPTH:
            raise _DecodeError("중첩이 너무 깊다")
        if k == "num":
            if refs and isinstance(v, int) and v >= 0:
                save = self.pos
                k2, v2 = self.token()
                if k2 == "num" and isinstance(v2, int):
                    k3, v3 = self.token()
                    if k3 == "kw" and v3 == b"R":
                        return Ref(v, v2)
                self.pos = save
            return v
        if k in ("name", "str"):
            return v
        if k == "[":
            arr = []
            while True:
                k2, v2 = self.token()
                if k2 == "]" or k2 is None:
                    return arr
                if k2 == "kw" and v2 in (b"endobj", b"stream", b"obj", b"endstream"):
                    self.pos -= len(v2)
                    return arr
                arr.append(self.value_from(k2, v2, depth + 1, refs))
        if k == "<<":
            d = {}
            while True:
                k2, v2 = self.token()
                if k2 == ">>" or k2 is None:
                    return d
                if k2 == "name":
                    k3, v3 = self.token()
                    if k3 == ">>":
                        d[str(v2)] = None
                        return d
                    d[str(v2)] = self.value_from(k3, v3, depth + 1, refs)
                elif k2 == "kw" and v2 in (b"endobj", b"stream", b"endstream"):
                    self.pos -= len(v2)
                    return d
                # 이름이 아닌 키는 버린다 (손상 내성)
        if k == "kw":
            if v == b"true":
                return True
            if v == b"false":
                return False
            if v == b"null":
                return None
            return v
        return None


# ---------------------------------------------------------------------------
# 필터
# ---------------------------------------------------------------------------

def _flate(b: bytes) -> bytes:
    """zlib. 손상된 꼬리는 읽은 데까지만 (청크 단위로 넣어 부분 결과를 살린다)."""
    for wbits, skip in ((15, 0), (-15, 2), (-15, 0)):
        d = zlib.decompressobj(wbits)
        out = bytearray()
        ok = True
        src = b[skip:]
        for i in range(0, len(src), 1 << 16):
            try:
                out += d.decompress(src[i:i + (1 << 16)], MAX_STREAM_BYTES - len(out))
            except zlib.error:
                ok = bool(out)
                break
            if d.unconsumed_tail or len(out) >= MAX_STREAM_BYTES:
                raise _DecodeError("압축 해제 크기가 상한을 넘는다")
            if d.eof:
                break
        if ok:
            try:
                out += d.flush()
            except zlib.error:
                pass
            return bytes(out)
    raise _DecodeError("FlateDecode 실패")


def _lzw(data: bytes, early: int = 1) -> bytes:
    out = bytearray()
    table: List[Optional[bytes]] = [bytes([i]) for i in range(256)] + [None, None]
    codelen = 9
    prev: Optional[bytes] = None
    buf = 0
    bits = 0
    i = 0
    n = len(data)
    while True:
        while bits < codelen and i < n:
            buf = (buf << 8) | data[i]
            i += 1
            bits += 8
        if bits < codelen:
            break
        bits -= codelen
        code = (buf >> bits) & ((1 << codelen) - 1)
        buf &= (1 << bits) - 1
        if code == 256:
            del table[258:]
            codelen = 9
            prev = None
            continue
        if code == 257:
            break
        if prev is None:
            if code >= len(table) or table[code] is None:
                break
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            if entry is None:
                break
            if len(table) < 4096:
                table.append(prev + entry[:1])
        elif code == len(table):
            entry = prev + prev[:1]
            if len(table) < 4096:
                table.append(entry)
        else:
            break
        out += entry
        if len(out) > MAX_STREAM_BYTES:
            raise _DecodeError("압축 해제 크기가 상한을 넘는다")
        prev = entry
        if codelen < 12 and len(table) + early >= (1 << codelen):
            codelen += 1
    return bytes(out)


def _a85(data: bytes) -> bytes:
    s = data.strip()
    if s.startswith(b"<~"):
        s = s[2:]
    e = s.find(b"~>")
    if e >= 0:
        s = s[:e]
    s = re.sub(rb"[ \t\r\n\x0c\x00]", b"", s)
    try:
        return base64.a85decode(s)
    except ValueError as ex:
        raise _DecodeError(f"ASCII85 오류: {ex}")


def _ahx(data: bytes) -> bytes:
    e = data.find(b">")
    if e >= 0:
        data = data[:e]
    h = _HEX_WS.sub(b"", data)
    if len(h) % 2:
        h += b"0"
    return binascii.unhexlify(h)


def _rle(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        L = data[i]
        i += 1
        if L == 128:
            break
        if L < 128:
            out += data[i:i + L + 1]
            i += L + 1
        else:
            if i < n:
                out += bytes([data[i]]) * (257 - L)
            i += 1
    return bytes(out)


def _predict(data: bytes, p) -> bytes:
    if not isinstance(p, dict):
        return data
    pred = p.get("Predictor", 1)
    if not isinstance(pred, int) or pred <= 1:
        return data
    colors = int(p.get("Colors", 1) or 1)
    bpc = int(p.get("BitsPerComponent", 8) or 8)
    cols = int(p.get("Columns", 1) or 1)
    bpp = max(1, (colors * bpc + 7) // 8)
    rowlen = (colors * bpc * cols + 7) // 8
    if pred == 2:
        if bpc != 8:
            raise _DecodeError("TIFF 예측자는 8비트만 지원")
        out = bytearray(data)
        for r in range(0, len(out) - rowlen + 1, rowlen):
            for i in range(r + bpp, r + rowlen):
                out[i] = (out[i] + out[i - bpp]) & 255
        return bytes(out)
    nrows = len(data) // (rowlen + 1)
    return bytes(unfilter(data, nrows, rowlen, bpp))


_IMAGE_FILTERS = {"DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"}


# ---------------------------------------------------------------------------
# 문서
# ---------------------------------------------------------------------------

_OBJ_RX = re.compile(rb"(?<![0-9])(\d{1,10})[ \t\r\n\x0c\x00]+(\d{1,5})[ \t\r\n\x0c\x00]+obj(?![A-Za-z])")
_XREF_ROW = re.compile(rb"(\d{10})[ \t](\d{5})[ \t]([nf])")


class Doc:
    def __init__(self, data: bytes):
        self.data = data
        self.xref: Dict[int, tuple] = {}
        self.trailer: dict = {}
        self.cache: Dict[int, object] = {}
        self.objstm: Dict[int, Dict[int, object]] = {}
        self.mode = ""
        self.warnings: List[str] = []
        self._scanned: Optional[Dict[int, int]] = None
        self.hdr = max(0, data.find(b"%PDF"))
        try:
            self._load_xref()
            root = self.resolve(self.trailer.get("Root"))
            if not isinstance(root, dict):
                raise _DecodeError("Root 없음")
        except (_DecodeError, ValueError, IndexError, KeyError, TypeError, zlib.error):
            self._rebuild()

    # -- xref ---------------------------------------------------------------
    def _load_xref(self):
        data = self.data
        sx = data.rfind(b"startxref")
        if sx < 0:
            raise _DecodeError("startxref 없음")
        p = Parser(data, sx + 9)
        k, off = p.token()
        if k != "num":
            raise _DecodeError("startxref 값 없음")
        seen = set()
        modes = []
        first = True
        while isinstance(off, int) and off not in seen and len(seen) < 512:
            seen.add(off)
            trailer, mode = self._read_section(off)
            modes.append(mode)
            if first:
                self.trailer = dict(trailer)
                first = False
            else:
                for kk, vv in trailer.items():
                    self.trailer.setdefault(kk, vv)
            xs = trailer.get("XRefStm")
            if isinstance(xs, int) and xs not in seen:
                seen.add(xs)
                self._read_section(xs)
            off = trailer.get("Prev")
        self.mode = "+".join(dict.fromkeys(modes)) + (f" ({len(seen)}개 판)" if len(seen) > 1 else "")

    def _read_section(self, off: int):
        data = self.data
        for o in (off, off + self.hdr):
            p = Parser(data, o)
            save = p.pos
            k, v = p.token()
            if k == "kw" and v == b"xref":
                return self._read_table(p), "xref-table"
            p.pos = save
            if k == "num":
                obj = self._parse_indirect(o)
                if isinstance(obj, Stream) and obj.dict.get("Type") == "XRef":
                    self._read_xref_stream(obj)
                    return obj.dict, "xref-stream"
        raise _DecodeError("xref 위치가 잘못됐다")

    def _read_table(self, p: Parser) -> dict:
        data = self.data
        while True:
            save = p.pos
            k, start = p.token()
            if k == "kw" and start == b"trailer":
                return p.value()
            if k != "num":
                raise _DecodeError("xref 표 손상")
            k2, count = p.token()
            if k2 != "num":
                raise _DecodeError("xref 표 손상")
            pos = p.pos
            for i in range(count):
                m = _XREF_ROW.search(data, pos, pos + 40)
                if not m:
                    raise _DecodeError("xref 행 손상")
                pos = m.end()
                num = start + i
                if num not in self.xref:
                    if m.group(3) == b"n":
                        self.xref[num] = ("o", int(m.group(1)), int(m.group(2)))
                    else:
                        self.xref[num] = ("f",)
            p.pos = pos
            del save

    def _read_xref_stream(self, s: Stream):
        d = s.dict
        raw = self.decode(s)
        W = [int(x) for x in self.resolve(d.get("W"))]
        size = int(self.resolve(d.get("Size", 0)))
        index = self.resolve(d.get("Index")) or [0, size]
        rl = sum(W)
        if rl <= 0:
            raise _DecodeError("xref 스트림 /W 손상")
        pos = 0
        for j in range(0, len(index) - 1, 2):
            start, count = int(index[j]), int(index[j + 1])
            for i in range(count):
                if pos + rl > len(raw):
                    return
                f = []
                q = pos
                for w in W:
                    f.append(int.from_bytes(raw[q:q + w], "big") if w else None)
                    q += w
                pos += rl
                t = f[0] if W[0] else 1
                num = start + i
                if num in self.xref:
                    continue
                if t == 0:
                    self.xref[num] = ("f",)
                elif t == 1:
                    self.xref[num] = ("o", f[1] or 0, f[2] or 0)
                elif t == 2:
                    self.xref[num] = ("c", f[1] or 0, f[2] or 0)

    def _rebuild(self):
        """xref 가 깨졌다 — 'n g obj' 전수 탐색."""
        self.warnings.append("PDF 교차참조(xref)가 손상돼 객체를 전수 탐색으로 복구했다")
        self.xref = {}
        self.cache = {}
        self.objstm = {}
        table = self._scan_table()
        for num, off in table.items():
            self.xref[num] = ("o", off, 0)
        self.mode = "scan"
        trailer: dict = {}
        for m in re.finditer(rb"trailer", self.data):
            try:
                t = Parser(self.data, m.end()).value()
                if isinstance(t, dict):
                    for k, v in t.items():
                        trailer[k] = v
            except _DecodeError:
                pass
        # 객체 스트림 안의 객체 등록, XRef 스트림의 Root
        for num in sorted(table):
            try:
                o = self.obj(num)
            except _DecodeError:
                continue
            if isinstance(o, Stream):
                t = o.dict.get("Type")
                if t == "ObjStm":
                    try:
                        for cn in self._objstm(num):
                            self.xref.setdefault(cn, ("c", num, 0))
                    except _DecodeError:
                        pass
                elif t == "XRef":
                    for k in ("Root", "Info", "Encrypt"):
                        if k in o.dict:
                            trailer.setdefault(k, o.dict[k])
        if not isinstance(self.resolve(trailer.get("Root")), dict):
            for num in sorted(self.xref):
                try:
                    o = self.obj(num)
                except _DecodeError:
                    continue
                if isinstance(o, dict) and o.get("Type") == "Catalog":
                    trailer["Root"] = Ref(num, 0)
                    break
        self.trailer = trailer

    def _scan_table(self) -> Dict[int, int]:
        if self._scanned is None:
            t: Dict[int, int] = {}
            for m in _OBJ_RX.finditer(self.data):
                t[int(m.group(1))] = m.start()
            self._scanned = t
        return self._scanned

    # -- 객체 ----------------------------------------------------------------
    def _parse_indirect(self, off: int, expect: Optional[int] = None):
        data = self.data
        p = Parser(data, off)
        k1, n1 = p.token()
        k2, _g = p.token()
        k3, kw = p.token()
        if not (k1 == "num" and k2 == "num" and k3 == "kw" and kw == b"obj"):
            raise _DecodeError("객체 머리 없음")
        if expect is not None and n1 != expect:
            raise _DecodeError("객체 번호 불일치")
        val = p.value()
        save = p.pos
        k, v = p.token()
        if k == "kw" and v == b"stream" and isinstance(val, dict):
            pos = p.pos
            if data[pos:pos + 2] == b"\r\n":
                pos += 2
            elif data[pos:pos + 1] in (b"\n", b"\r"):
                pos += 1
            L = val.get("Length")
            try:
                L = self.resolve(L) if isinstance(L, Ref) else L
            except _DecodeError:
                L = None
            raw = None
            if isinstance(L, int) and L >= 0:
                tail = data[pos + L:pos + L + 32]
                if b"endstream" in tail:
                    raw = data[pos:pos + L]
            if raw is None:
                e = data.find(b"endstream", pos)
                if e < 0:
                    e = len(data)
                end = e
                if data[end - 2:end] == b"\r\n":
                    end -= 2
                elif data[end - 1:end] in (b"\n", b"\r"):
                    end -= 1
                raw = data[pos:end]
            return Stream(val, raw, n1)
        p.pos = save
        return val

    def obj(self, num: int):
        if num in self.cache:
            return self.cache[num]
        self.cache[num] = None            # 순환 참조 방지
        ent = self.xref.get(num)
        val = None
        if ent is not None and ent[0] == "o":
            try:
                val = self._parse_indirect(ent[1], num)
            except (_DecodeError, ValueError, IndexError):
                alt = self._scan_table().get(num)
                if alt is not None and alt != ent[1]:
                    try:
                        val = self._parse_indirect(alt, num)
                    except (_DecodeError, ValueError, IndexError):
                        val = None
        elif ent is not None and ent[0] == "c":
            try:
                val = self._objstm(ent[1]).get(num)
            except (_DecodeError, ValueError, IndexError, zlib.error):
                val = None
        elif ent is None and self.mode != "scan":
            alt = self._scan_table().get(num)
            if alt is not None:
                try:
                    val = self._parse_indirect(alt, num)
                except (_DecodeError, ValueError, IndexError):
                    val = None
        self.cache[num] = val
        return val

    def _objstm(self, num: int) -> Dict[int, object]:
        if num in self.objstm:
            return self.objstm[num]
        self.objstm[num] = {}
        s = self.obj(num)
        if not isinstance(s, Stream):
            raise _DecodeError("객체 스트림이 아니다")
        data = self.decode(s)
        n = int(self.resolve(s.dict.get("N", 0)))
        first = int(self.resolve(s.dict.get("First", 0)))
        p = Parser(data[:first])
        pairs = []
        for _ in range(n):
            k1, a = p.token()
            k2, b = p.token()
            if k1 != "num" or k2 != "num":
                break
            pairs.append((a, b))
        out: Dict[int, object] = {}
        for a, b in pairs:
            try:
                out[a] = Parser(data, first + b).value()
            except _DecodeError:
                pass
        self.objstm[num] = out
        return out

    def resolve(self, x, _depth: int = 0):
        while isinstance(x, Ref):
            _depth += 1
            if _depth > 32:
                return None
            x = self.obj(x.num)
        return x

    def decode(self, s: Stream) -> bytes:
        if s._dec is not None:
            return s._dec
        d = s.dict
        filters = self.resolve(d.get("Filter"))
        parms = self.resolve(d.get("DecodeParms", d.get("DP")))
        if filters is None:
            filters = []
        elif not isinstance(filters, list):
            filters = [filters]
        if not isinstance(parms, list):
            parms = [parms] * len(filters)
        data = s.raw
        for i, f in enumerate(filters):
            f = self.resolve(f)
            pm = self.resolve(parms[i]) if i < len(parms) else None
            if isinstance(pm, dict):
                pm = {k: self.resolve(v) for k, v in pm.items()}
            if f in ("FlateDecode", "Fl"):
                data = _predict(_flate(data), pm)
            elif f in ("LZWDecode", "LZW"):
                early = pm.get("EarlyChange", 1) if isinstance(pm, dict) else 1
                data = _predict(_lzw(data, int(early)), pm)
            elif f in ("ASCIIHexDecode", "AHx"):
                data = _ahx(data)
            elif f in ("ASCII85Decode", "A85"):
                data = _a85(data)
            elif f in ("RunLengthDecode", "RL"):
                data = _rle(data)
            elif f in _IMAGE_FILTERS:
                raise _ImageData(f)
            elif f == "Crypt":
                pass
            else:
                raise _DecodeError(f"지원하지 않는 필터 {f}")
        s._dec = data
        return data


# ---------------------------------------------------------------------------
# 글꼴 / 문자 해독
# ---------------------------------------------------------------------------

_GLYPH = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$",
    "percent": "%", "ampersand": "&", "quotesingle": "'", "quoteright": "’", "quoteleft": "‘",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",",
    "hyphen": "-", "minus": "−", "period": ".", "slash": "/", "colon": ":", "semicolon": ";",
    "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}",
    "asciitilde": "~", "degree": "°", "plusminus": "±", "multiply": "×", "divide": "÷",
    "endash": "–", "emdash": "—", "bullet": "•", "periodcentered": "·", "mu": "µ",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "nbspace": "\xa0",
    "twosuperior": "²", "threesuperior": "³", "section": "§", "paragraph": "¶",
    "copyright": "©", "registered": "®", "fi": "fi", "fl": "fl", "quotedblleft": "“",
    "quotedblright": "”", "ellipsis": "…", "phi": "φ", "Phi": "Φ", "diameter": "⌀",
}


def _glyph_to_uni(name: str) -> Optional[str]:
    base = name.split(".")[0].split("_")[0] if name not in _GLYPH else name
    if base in _GLYPH:
        return _GLYPH[base]
    if len(base) == 1 and base.isalpha() and base.isascii():
        return base
    m = re.fullmatch(r"uni((?:[0-9A-Fa-f]{4})+)", base)
    if m:
        h = m.group(1)
        try:
            return "".join(chr(int(h[i:i + 4], 16)) for i in range(0, len(h), 4))
        except ValueError:
            return None
    m = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", base)
    if m:
        v = int(m.group(1), 16)
        return chr(v) if v < 0x110000 else None
    return None


def _table_winansi() -> List[Optional[str]]:
    t: List[Optional[str]] = [None] * 256
    for c in range(32, 256):
        try:
            t[c] = bytes([c]).decode("cp1252")
        except UnicodeDecodeError:
            t[c] = None
    return t


def _table_standard() -> List[Optional[str]]:
    t: List[Optional[str]] = [None] * 256
    for c in range(32, 127):
        t[c] = chr(c)
    t[0x27] = "’"
    t[0x60] = "‘"
    for c, s in ((0xA1, "¡"), (0xA2, "¢"), (0xA3, "£"), (0xA5, "¥"), (0xA7, "§"),
                 (0xB1, "–"), (0xB7, "•"), (0xD0, "—"), (0xAE, "fi"), (0xAF, "fl")):
        t[c] = s
    return t


def _table_mac() -> List[Optional[str]]:
    t: List[Optional[str]] = [None] * 256
    for c in range(32, 256):
        t[c] = bytes([c]).decode("mac_roman")
    return t


_ENC_TABLES = {"WinAnsiEncoding": _table_winansi(), "StandardEncoding": _table_standard(),
               "MacRomanEncoding": _table_mac(), "PDFDocEncoding": _table_winansi()}


def _text_string(b) -> str:
    """PDF 텍스트 문자열 (OCG 이름 등): UTF-16BE BOM 이면 UTF-16, 아니면 PDFDocEncoding≈latin-1."""
    if isinstance(b, str):
        return b
    if not isinstance(b, (bytes, bytearray)):
        return ""
    if b[:2] == b"\xfe\xff":
        return b[2:].decode("utf-16-be", "replace")
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", "replace")
    return b.decode("latin-1")


class CMap:
    """ToUnicode CMap: codespace + bfchar + bfrange."""

    def __init__(self, data: bytes):
        self.spaces: List[Tuple[int, int, int]] = []   # (len, lo, hi)
        self.map: Dict[Tuple[int, int], str] = {}
        self.ranges: List[Tuple[int, int, int, object]] = []  # (len, lo, hi, dst)
        p = Parser(data)
        mode = None
        buf: list = []
        while True:
            k, v = p.token()
            if k is None:
                break
            if k == "kw":
                if v in (b"begincodespacerange", b"beginbfchar", b"beginbfrange"):
                    mode = v
                    buf = []
                elif v == b"endcodespacerange":
                    for i in range(0, len(buf) - 1, 2):
                        lo, hi = buf[i], buf[i + 1]
                        if isinstance(lo, bytes) and isinstance(hi, bytes) and lo:
                            self.spaces.append((len(lo), int.from_bytes(lo, "big"),
                                                int.from_bytes(hi, "big")))
                    mode = None
                elif v == b"endbfchar":
                    for i in range(0, len(buf) - 1, 2):
                        src, dst = buf[i], buf[i + 1]
                        if isinstance(src, bytes) and src:
                            s = self._dst(dst)
                            if s is not None:
                                self.map[(len(src), int.from_bytes(src, "big"))] = s
                    mode = None
                elif v == b"endbfrange":
                    for i in range(0, len(buf) - 2, 3):
                        lo, hi, dst = buf[i], buf[i + 1], buf[i + 2]
                        if isinstance(lo, bytes) and isinstance(hi, bytes) and lo:
                            self.ranges.append((len(lo), int.from_bytes(lo, "big"),
                                                int.from_bytes(hi, "big"), dst))
                    mode = None
                continue
            if mode is None:
                continue
            if k == "[":
                buf.append(p.value_from(k, v, 1, refs=False))
            elif k in ("str", "name", "num"):
                buf.append(v)
        self.lengths = sorted({s[0] for s in self.spaces}) or \
            sorted({k[0] for k in self.map} | {r[0] for r in self.ranges})

    @staticmethod
    def _dst(dst) -> Optional[str]:
        if isinstance(dst, bytes):
            try:
                return dst.decode("utf-16-be")
            except UnicodeDecodeError:
                return None
        if isinstance(dst, Name):
            return _glyph_to_uni(dst)
        return None

    def lookup(self, L: int, code: int) -> Optional[str]:
        s = self.map.get((L, code))
        if s is not None:
            return s
        for (rl, lo, hi, dst) in self.ranges:
            if rl == L and lo <= code <= hi:
                off = code - lo
                if isinstance(dst, list):
                    return self._dst(dst[off]) if off < len(dst) else None
                if isinstance(dst, bytes) and dst:
                    v = int.from_bytes(dst, "big") + off
                    try:
                        return v.to_bytes(len(dst), "big").decode("utf-16-be")
                    except (UnicodeDecodeError, OverflowError):
                        return None
        return None

    def split(self, s: bytes, default_len: int) -> List[Tuple[int, int]]:
        out = []
        i, n = 0, len(s)
        lens = self.lengths or [default_len]
        while i < n:
            took = False
            if self.spaces:
                for L in lens:
                    if i + L <= n:
                        c = int.from_bytes(s[i:i + L], "big")
                        for (sl, lo, hi) in self.spaces:
                            if sl == L and lo <= c <= hi:
                                out.append((L, c))
                                i += L
                                took = True
                                break
                    if took:
                        break
            if not took:
                L = lens[0] if not self.spaces else min(lens)
                L = max(1, min(L, n - i))
                out.append((L, int.from_bytes(s[i:i + L], "big")))
                i += L
        return out


_UTF16_CMAP = re.compile(r"^Uni\w+-(UCS2|UTF16)-[HV]$")
_KS_CMAP = re.compile(r"^(KSCms-UHC|KSC-EUC|KSCpc-EUC|KSC-Johab)-[HV]")


class Font:
    def __init__(self, doc: Doc, fd: dict):
        self.ok = isinstance(fd, dict)
        fd = fd if self.ok else {}
        self.subtype = fd.get("Subtype")
        self.composite = self.subtype == "Type0"
        self.cmap: Optional[CMap] = None
        self.mode = "simple"
        self.widths: Dict[int, float] = {}
        self.default_w = 500.0
        self.wscale = 1.0
        tu = doc.resolve(fd.get("ToUnicode"))
        if isinstance(tu, Stream):
            try:
                self.cmap = CMap(doc.decode(tu))
            except (_DecodeError, _ImageData, ValueError):
                self.cmap = None
        if self.composite:
            enc = doc.resolve(fd.get("Encoding"))
            self.mode = "identity"
            if isinstance(enc, str):
                if _UTF16_CMAP.match(enc):
                    self.mode = "utf16"
                elif _KS_CMAP.match(enc):
                    self.mode = "cp949"
            elif isinstance(enc, Stream):
                self.mode = "embedded"
                try:
                    ec = CMap(doc.decode(enc))
                    if ec.spaces and self.cmap is not None and not self.cmap.spaces:
                        self.cmap.spaces = ec.spaces
                        self.cmap.lengths = sorted({s[0] for s in ec.spaces})
                except (_DecodeError, _ImageData, ValueError):
                    pass
            desc = doc.resolve(fd.get("DescendantFonts"))
            df = doc.resolve(desc[0]) if isinstance(desc, list) and desc else None
            self.default_w = 1000.0
            if isinstance(df, dict):
                dw = doc.resolve(df.get("DW"))
                if isinstance(dw, (int, float)):
                    self.default_w = float(dw)
                W = doc.resolve(df.get("W"))
                if isinstance(W, list):
                    i = 0
                    while i + 1 < len(W) and len(self.widths) < 100000:
                        a = doc.resolve(W[i])
                        b = doc.resolve(W[i + 1])
                        if isinstance(b, list):
                            for j, wv in enumerate(b):
                                wv = doc.resolve(wv)
                                if isinstance(wv, (int, float)):
                                    self.widths[int(a) + j] = float(wv)
                            i += 2
                        elif i + 2 < len(W):
                            wv = doc.resolve(W[i + 2])
                            if isinstance(a, int) and isinstance(b, int) and isinstance(wv, (int, float)):
                                for c in range(a, min(b, a + 65536) + 1):
                                    self.widths[c] = float(wv)
                            i += 3
                        else:
                            break
        else:
            fc = doc.resolve(fd.get("FirstChar"))
            ws = doc.resolve(fd.get("Widths"))
            if isinstance(fc, int) and isinstance(ws, list):
                for j, wv in enumerate(ws[:256]):
                    wv = doc.resolve(wv)
                    if isinstance(wv, (int, float)):
                        self.widths[fc + j] = float(wv)
            if self.subtype == "Type3":
                fm = doc.resolve(fd.get("FontMatrix"))
                if isinstance(fm, list) and fm and isinstance(fm[0], (int, float)):
                    self.wscale = float(fm[0]) * 1000.0
            enc = doc.resolve(fd.get("Encoding"))
            base = "WinAnsiEncoding" if self.subtype == "TrueType" else "StandardEncoding"
            diffs = None
            if isinstance(enc, str):
                base = enc if enc in _ENC_TABLES else base
            elif isinstance(enc, dict):
                be = doc.resolve(enc.get("BaseEncoding"))
                if isinstance(be, str) and be in _ENC_TABLES:
                    base = be
                diffs = doc.resolve(enc.get("Differences"))
            self.table = list(_ENC_TABLES[base])
            if isinstance(diffs, list):
                code = 0
                for x in diffs:
                    x = doc.resolve(x)
                    if isinstance(x, int):
                        code = x
                    elif isinstance(x, Name):
                        if 0 <= code < 256:
                            self.table[code] = _glyph_to_uni(x)
                        code += 1

    def decode(self, s: bytes) -> Tuple[Optional[str], List[Tuple[int, int]]]:
        """→ (문자열 또는 None(해독 불가), [(코드, 코드길이)])"""
        if self.composite:
            if self.cmap is not None:
                codes = self.cmap.split(s, 2)
            else:
                codes = [(2, int.from_bytes(s[i:i + 2], "big")) for i in range(0, len(s) - 1, 2)]
            out = []
            text_ok = True
            if self.cmap is not None:
                for L, c in codes:
                    u = self.cmap.lookup(L, c)
                    if u is None:
                        text_ok = False
                        break
                    out.append(u)
            elif self.mode == "utf16":
                try:
                    out.append(s.decode("utf-16-be"))
                except UnicodeDecodeError:
                    text_ok = False
            elif self.mode == "cp949":
                try:
                    out.append(s.decode("cp949"))
                    codes = [(1, c) for c in s]
                except UnicodeDecodeError:
                    text_ok = False
            else:
                text_ok = False
            return ("".join(out) if text_ok else None), [(c, L) for L, c in codes]
        out = []
        ok = True
        codes = []
        for b in s:
            codes.append((b, 1))
            u = None
            if self.cmap is not None:
                u = self.cmap.lookup(1, b)
            if u is None:
                u = self.table[b]
            if u is None:
                ok = False
            elif ok:
                out.append(u)
        return ("".join(out) if ok else None), codes

    def width(self, code: int) -> float:
        return self.widths.get(code, self.default_w) * self.wscale


# ---------------------------------------------------------------------------
# 내용 스트림 해석
# ---------------------------------------------------------------------------

IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mmul(a, b):
    """a × b (PDF 행벡터 규약: 먼저 a, 그다음 b)."""
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a1 * b2, a0 * b1 + a1 * b3,
            a2 * b0 + a3 * b2, a2 * b1 + a3 * b3,
            a4 * b0 + a5 * b2 + b4, a4 * b1 + a5 * b3 + b5)


def _cmyk(c, m, y, k):
    return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))


def _color(ops) -> Optional[Tuple[float, float, float]]:
    nums = [x for x in ops if isinstance(x, (int, float))]
    if len(nums) != len(ops):
        return None             # 패턴 이름 등
    if len(nums) == 1:
        return (nums[0], nums[0], nums[0])
    if len(nums) == 3:
        return (nums[0], nums[1], nums[2])
    if len(nums) == 4:
        return _cmyk(*nums)
    return None


class GS:
    __slots__ = ("ctm", "stroke", "fill", "font", "fs", "tc", "tw", "th", "tl", "ts")

    def __init__(self):
        self.ctm = IDENT
        self.stroke = (0.0, 0.0, 0.0)
        self.fill = (0.0, 0.0, 0.0)
        self.font = None
        self.fs = 0.0
        self.tc = 0.0
        self.tw = 0.0
        self.th = 1.0
        self.tl = 0.0
        self.ts = 0.0

    def copy(self):
        g = GS.__new__(GS)
        for k in GS.__slots__:
            setattr(g, k, getattr(self, k))
        return g


class Interp:
    def __init__(self, doc: Doc, off_layers: set):
        self.doc = doc
        self.off = off_layers
        self.polys: List[tuple] = []       # (pts, layer, ref)
        self.segs: List[tuple] = []        # (x0, y0, x1, y1, layer)
        self.texts: List[dict] = []
        self.fonts: Dict[int, Font] = {}
        self.ocg_names: Dict[int, Optional[str]] = {}
        self.layers_seen: Dict[str, int] = {}
        self.n_paint = 0
        self.n_images = 0
        self.image_area = 0.0
        self.n_ops = 0
        self.bad_ops = 0
        self.undecoded = 0
        self.hidden = 0
        self.curved_open = 0
        self.form_depth_hit = 0
        self._cur_text = None
        self._sub = 0

    # -- 레이어(OCG) ---------------------------------------------------------
    def ocg_name(self, o) -> Optional[str]:
        key = o.num if isinstance(o, Ref) else id(o)
        if key in self.ocg_names:
            return self.ocg_names[key]
        d = self.doc.resolve(o)
        name = None
        if isinstance(d, dict):
            if d.get("Type") == "OCMD" or "OCGs" in d:
                g = self.doc.resolve(d.get("OCGs"))
                if isinstance(g, list) and g:
                    name = self.ocg_name(g[0])
                elif isinstance(g, dict):
                    name = _text_string(self.doc.resolve(g.get("Name")))
            else:
                name = _text_string(self.doc.resolve(d.get("Name")))
        self.ocg_names[key] = name or None
        return name or None

    # -- 실행 ----------------------------------------------------------------
    def run(self, data: bytes, res: dict, gs: GS, depth: int, mc: list, active: set):
        doc = self.doc
        p = Parser(data)
        token = p.token
        ops: list = []
        stack: List[GS] = []
        path: List[list] = []        # [pts, closed]
        tm = tlm = IDENT
        res = res if isinstance(res, dict) else {}
        mc_base = len(mc)

        def pt(x, y):
            m = gs.ctm
            return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])

        while True:
            k, v = token()
            if k is None:
                break
            if k != "kw":
                if k == "[" or k == "<<":
                    try:
                        ops.append(p.value_from(k, v, 1, refs=False))
                    except _DecodeError:
                        ops.append(None)
                else:
                    ops.append(v)
                continue
            self.n_ops += 1
            if self.n_ops > MAX_OPS:
                raise _DecodeError("연산자 수 상한 초과")
            o = v
            try:
                if o == b"l":
                    q = pt(ops[-2], ops[-1])
                    if path and not path[-1][1]:
                        path[-1][0].append(q)
                    elif path:
                        path.append([[path[-1][0][0], q], False])
                    else:
                        path.append([[q], False])
                elif o == b"m":
                    path.append([[pt(ops[-2], ops[-1])], False])
                elif o == b"re":
                    x, y, w, h = ops[-4:]
                    path.append([[pt(x, y), pt(x + w, y), pt(x + w, y + h), pt(x, y + h)], True])
                elif o in (b"c", b"v", b"y"):
                    if not path:
                        ops = []
                        continue
                    sp = path[-1]
                    p0 = sp[0][-1]
                    if o == b"c":
                        p1, p2, p3 = pt(ops[-6], ops[-5]), pt(ops[-4], ops[-3]), pt(ops[-2], ops[-1])
                    elif o == b"v":
                        p1, p2, p3 = p0, pt(ops[-4], ops[-3]), pt(ops[-2], ops[-1])
                    else:
                        p1, p2, p3 = pt(ops[-4], ops[-3]), pt(ops[-2], ops[-1]), pt(ops[-2], ops[-1])
                    if sp[1]:
                        sp = [[sp[0][0]], False]
                        path.append(sp)
                        p0 = sp[0][0]
                    for i in range(1, CURVE_STEPS + 1):
                        t = i / CURVE_STEPS
                        u = 1 - t
                        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
                        sp[0].append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                                      a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
                    if len(sp) == 2:
                        sp.append(True)          # 곡선 포함 표시
                elif o == b"h":
                    if path:
                        path[-1][1] = True
                elif o in (b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"):
                    stroke = o in (b"S", b"s", b"B", b"B*", b"b", b"b*")
                    fill = o not in (b"S", b"s")
                    close = o in (b"s", b"b", b"b*")
                    self._paint(path, stroke, fill, close, gs, mc)
                    path = []
                elif o == b"n":
                    path = []
                elif o == b"W" or o == b"W*":
                    pass
                elif o == b"q":
                    stack.append(gs.copy())
                elif o == b"Q":
                    if stack:
                        gs = stack.pop()
                elif o == b"cm":
                    gs.ctm = mmul(tuple(float(x) for x in ops[-6:]), gs.ctm)
                elif o in (b"RG", b"G", b"K", b"SC", b"SCN"):
                    c = _color(ops)
                    gs.stroke = c
                elif o in (b"rg", b"g", b"k", b"sc", b"scn"):
                    gs.fill = _color(ops)
                elif o == b"CS":
                    gs.stroke = (0.0, 0.0, 0.0)
                elif o == b"cs":
                    gs.fill = (0.0, 0.0, 0.0)
                elif o == b"BT":
                    tm = tlm = IDENT
                elif o == b"ET":
                    pass
                elif o == b"Tf":
                    fr = (doc.resolve(res.get("Font")) or {})
                    fref = fr.get(ops[-2]) if isinstance(fr, dict) else None
                    gs.font = self._font(fref)
                    gs.fs = float(ops[-1])
                elif o == b"Td":
                    tlm = mmul((1.0, 0.0, 0.0, 1.0, float(ops[-2]), float(ops[-1])), tlm)
                    tm = tlm
                elif o == b"TD":
                    gs.tl = -float(ops[-1])
                    tlm = mmul((1.0, 0.0, 0.0, 1.0, float(ops[-2]), float(ops[-1])), tlm)
                    tm = tlm
                elif o == b"Tm":
                    tlm = tm = tuple(float(x) for x in ops[-6:])
                elif o == b"T*":
                    tlm = mmul((1.0, 0.0, 0.0, 1.0, 0.0, -gs.tl), tlm)
                    tm = tlm
                elif o == b"TL":
                    gs.tl = float(ops[-1])
                elif o == b"Tc":
                    gs.tc = float(ops[-1])
                elif o == b"Tw":
                    gs.tw = float(ops[-1])
                elif o == b"Tz":
                    gs.th = float(ops[-1]) / 100.0
                elif o == b"Ts":
                    gs.ts = float(ops[-1])
                elif o == b"Tr":
                    pass
                elif o == b"Tj":
                    tm = self._show(gs, tm, [ops[-1]])
                elif o == b"TJ":
                    tm = self._show(gs, tm, ops[-1])
                elif o == b"'":
                    tlm = mmul((1.0, 0.0, 0.0, 1.0, 0.0, -gs.tl), tlm)
                    tm = self._show(gs, tlm, [ops[-1]])
                elif o == b'"':
                    gs.tw = float(ops[-3])
                    gs.tc = float(ops[-2])
                    tlm = mmul((1.0, 0.0, 0.0, 1.0, 0.0, -gs.tl), tlm)
                    tm = self._show(gs, tlm, [ops[-1]])
                elif o == b"BDC":
                    tag, prop = ops[-2], ops[-1]
                    name = None
                    if tag == "OC":
                        if isinstance(prop, Name):
                            pr = doc.resolve(res.get("Properties")) or {}
                            ref = pr.get(prop) if isinstance(pr, dict) else None
                            name = self.ocg_name(ref) if ref is not None else None
                        elif isinstance(prop, dict):
                            name = self.ocg_name(prop)
                    mc.append(name)
                elif o == b"BMC":
                    mc.append(None)
                elif o == b"EMC":
                    if len(mc) > mc_base:
                        mc.pop()
                elif o == b"Do":
                    self._do(ops[-1], res, gs, depth, mc, active)
                elif o == b"BI":
                    self._inline_image(p, gs)
                # 그 밖의 연산자(w J j M d ri i gs sh d0 d1 MP DP BX EX …)는 기하에 무관
            except (IndexError, TypeError, ValueError, AttributeError, KeyError):
                self.bad_ops += 1
            ops = []
        del mc[mc_base:]

    # -- 칠하기 → 다각형 / 선분 -------------------------------------------------
    def _layer(self, mc, color, painted) -> Tuple[Optional[str], bool]:
        for n in reversed(mc):
            if n:
                return n, n in self.off
        if not painted:
            return "path", False
        if color is None:
            return "path", False
        return "color:%.2f,%.2f,%.2f" % tuple(min(1.0, max(0.0, c)) for c in color), False

    def _paint(self, path, stroke, fill, close, gs, mc):
        for sp in path:
            pts = sp[0]
            closed = sp[1] or close
            curved = len(sp) > 2
            self.n_paint += 1
            # 연속 중복점 제거
            q = [pts[0]]
            for p_ in pts[1:]:
                if abs(p_[0] - q[-1][0]) > 1e-9 or abs(p_[1] - q[-1][1]) > 1e-9:
                    q.append(p_)
            if len(q) >= 3 and abs(q[0][0] - q[-1][0]) <= 1e-6 and abs(q[0][1] - q[-1][1]) <= 1e-6:
                q.pop()
                closed = True
            color = gs.fill if (fill and not stroke) else gs.stroke
            layer, hidden = self._layer(mc, color, True)
            if hidden:
                self.hidden += 1
                continue
            self.layers_seen[layer] = self.layers_seen.get(layer, 0) + 1
            if (closed or fill) and len(q) >= 3:
                self._sub += 1
                self.polys.append((q, layer, f"path{self._sub}"))
            elif stroke and len(q) >= 2:
                if curved:
                    self.curved_open += 1
                    continue
                for i in range(len(q) - 1):
                    self.segs.append((q[i][0], q[i][1], q[i + 1][0], q[i + 1][1], layer))

    # -- XObject ------------------------------------------------------------
    def _do(self, name, res, gs, depth, mc, active):
        xo = self.doc.resolve(res.get("XObject")) or {}
        ref = xo.get(name) if isinstance(xo, dict) else None
        s = self.doc.resolve(ref)
        if not isinstance(s, Stream):
            return
        st = s.dict.get("Subtype")
        if st == "Image":
            self.n_images += 1
            self.image_area += _unit_square_area(gs.ctm)
            return
        if st != "Form":
            return
        if depth >= MAX_FORM_DEPTH or s.num in active:
            self.form_depth_hit += 1
            return
        try:
            data = self.doc.decode(s)
        except (_DecodeError, _ImageData):
            self.bad_ops += 1
            return
        g2 = gs.copy()
        mat = self.doc.resolve(s.dict.get("Matrix"))
        if isinstance(mat, list) and len(mat) == 6:
            g2.ctm = mmul(tuple(float(self.doc.resolve(x)) for x in mat), gs.ctm)
        r2 = self.doc.resolve(s.dict.get("Resources"))
        if not isinstance(r2, dict):
            r2 = res
        oc = s.dict.get("OC")
        pushed = False
        if oc is not None:
            mc.append(self.ocg_name(oc))
            pushed = True
        active.add(s.num)
        try:
            self.run(data, r2, g2, depth + 1, mc, active)
        finally:
            active.discard(s.num)
            if pushed and mc:
                mc.pop()

    def _inline_image(self, p: Parser, gs):
        d = {}
        while True:
            k, v = p.token()
            if k is None:
                return
            if k == "kw" and v == b"ID":
                break
            if k == "name":
                d[str(v)] = p.value(1, refs=False)
        data = p.data
        pos = p.pos + 1
        L = d.get("L", d.get("Length"))
        if isinstance(L, int) and L > 0 and data[pos + L:pos + L + 8].strip().startswith(b"EI"):
            p.pos = pos + L
        m = re.compile(rb"[ \t\r\n\x0c\x00]EI(?=[ \t\r\n\x0c\x00]|$)").search(data, p.pos)
        p.pos = m.end() if m else len(data)
        self.n_images += 1
        self.image_area += _unit_square_area(gs.ctm)

    # -- 문자 ----------------------------------------------------------------
    def _font(self, fref) -> Optional[Font]:
        if fref is None:
            return None
        key = fref.num if isinstance(fref, Ref) else id(fref)
        f = self.fonts.get(key)
        if f is None:
            f = Font(self.doc, self.doc.resolve(fref))
            self.fonts[key] = f
        return f

    def _show(self, gs: GS, tm, items):
        font = gs.font
        fs = gs.fs
        th = gs.th
        for it in items:
            if isinstance(it, (int, float)):
                tm = mmul((1.0, 0.0, 0.0, 1.0, -float(it) / 1000.0 * fs * th, 0.0), tm)
                continue
            if not isinstance(it, bytes) or font is None:
                if isinstance(it, bytes) and it:
                    self.undecoded += 1
                    self._flush_text()
                continue
            text, codes = font.decode(it)
            tx = 0.0
            for code, L in codes:
                w = font.width(code) / 1000.0 * fs + gs.tc
                if L == 1 and code == 32:
                    w += gs.tw
                tx += w * th
            m = mmul(tm, gs.ctm)
            sx, sy = m[0] * 0 + m[2] * gs.ts + m[4], m[1] * 0 + m[3] * gs.ts + m[5]
            ex, ey = m[0] * tx + m[2] * gs.ts + m[4], m[1] * tx + m[3] * gs.ts + m[5]
            size = math.hypot(m[2], m[3]) * fs
            tm = mmul((1.0, 0.0, 0.0, 1.0, tx, 0.0), tm)
            if text is None:
                self.undecoded += 1
                self._flush_text()
                continue
            text = "".join(ch for ch in text if ch >= " " or ch == "\t")
            self._add_piece(text, sx, sy, ex, ey, size, m)
        return tm

    def _add_piece(self, text, sx, sy, ex, ey, size, m):
        ux, uy = m[0], m[1]
        nrm = math.hypot(ux, uy) or 1.0
        ux, uy = ux / nrm, uy / nrm
        cur = self._cur_text
        if cur is not None and size > 0:
            dx, dy = sx - cur["ex"], sy - cur["ey"]
            along = dx * cur["ux"] + dy * cur["uy"]
            perp = -dx * cur["uy"] + dy * cur["ux"]
            same_dir = ux * cur["ux"] + uy * cur["uy"] > 0.99
            if (same_dir and abs(perp) <= 0.35 * size and -0.6 * size <= along <= 1.2 * size
                    and abs(size - cur["size"]) <= 0.25 * max(size, cur["size"])):
                if along > 0.25 * size and not cur["text"].endswith(" ") and not text.startswith(" "):
                    cur["text"] += " "
                cur["text"] += text
                cur["ex"], cur["ey"] = ex, ey
                return
        self._flush_text()
        if not text.strip():
            return
        self._cur_text = dict(text=text, sx=sx, sy=sy, ex=ex, ey=ey, size=size, ux=ux, uy=uy)

    def _flush_text(self):
        c = self._cur_text
        if c is not None:
            s = re.sub(r"\s+", " ", c["text"]).strip()
            if s:
                self.texts.append(dict(text=s, x=(c["sx"] + c["ex"]) / 2.0,
                                       y=(c["sy"] + c["ey"]) / 2.0, size=c["size"]))
        self._cur_text = None


def _unit_square_area(m) -> float:
    return abs(m[0] * m[3] - m[1] * m[2])


# ---------------------------------------------------------------------------
# 선분 → 축정렬 직사각형
# ---------------------------------------------------------------------------

def _cluster(vals: List[float], tol: float) -> Dict[float, float]:
    """값 → 대표값(군집 최솟값). 결정론."""
    out: Dict[float, float] = {}
    rep = None
    last = None
    for v in sorted(set(vals)):
        if last is None or v - last > tol:
            rep = v
        out[v] = rep
        last = v
    return out


def _merge_ivs(ivs: List[tuple], tol: float) -> List[list]:
    ivs.sort(key=lambda t: (t[0], t[1], t[2]))
    out: List[list] = []
    for a, b, lay in ivs:
        if out and a <= out[-1][1] + tol:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b, lay])
    return out


def _covers(lines: Dict[float, List[list]], starts: Dict[float, List[float]], key, a, b, tol):
    ivs = lines.get(key)
    if not ivs:
        return None
    i = bisect_right(starts[key], a + tol) - 1
    if i >= 0 and ivs[i][1] >= b - tol:
        return ivs[i]
    return None


def rects_from_segments(segs: Sequence[tuple], tol: float, max_try: int = 24):
    """
    축정렬 선분 → 최소 직사각형 고리 [(x0, y0, x1, y1, layer)].
    각 좌하단 꼭짓점에서 오른쪽으로 가장 가까운 세로선부터, 네 변이 모두 선으로 덮이는
    가장 낮은 윗변을 찾는다 (벽을 공유하는 칸도 칸 단위로 나온다).
    """
    hs, vs = [], []
    other = 0
    for x0, y0, x1, y1, lay in segs:
        if abs(y1 - y0) <= tol and abs(x1 - x0) > tol:
            hs.append(((y0 + y1) / 2.0, min(x0, x1), max(x0, x1), lay))
        elif abs(x1 - x0) <= tol and abs(y1 - y0) > tol:
            vs.append(((x0 + x1) / 2.0, min(y0, y1), max(y0, y1), lay))
        else:
            other += 1
    if not hs or not vs:
        return [], other
    cy = _cluster([h[0] for h in hs] + [v[1] for v in vs] + [v[2] for v in vs], tol)
    cx = _cluster([v[0] for v in vs] + [h[1] for h in hs] + [h[2] for h in hs], tol)
    H: Dict[float, list] = {}
    V: Dict[float, list] = {}
    for y, a, b, lay in hs:
        H.setdefault(cy[y], []).append((cx[a], cx[b], lay))
    for x, a, b, lay in vs:
        V.setdefault(cx[x], []).append((cy[a], cy[b], lay))
    H = {k: _merge_ivs(v, tol) for k, v in H.items()}
    V = {k: _merge_ivs(v, tol) for k, v in V.items()}
    Hs = {k: [iv[0] for iv in v] for k, v in H.items()}
    Vs = {k: [iv[0] for iv in v] for k, v in V.items()}
    xs = sorted(V)
    ys = sorted(H)
    out = []
    seen = set()
    for ya in ys:
        for (ha, hb, _lay) in H[ya]:
            i0 = bisect_right(xs, ha - tol - 1e-12)
            cand = []
            for xi in range(i0, len(xs)):
                x = xs[xi]
                if x > hb + tol:
                    break
                ivv = _covers(V, Vs, x, ya, ya + tol, tol)
                if ivv is not None and ivv[1] > ya + tol:
                    cand.append(x)
            for ai, xa in enumerate(cand):
                found = False
                for xb in cand[ai + 1:ai + 1 + max_try]:
                    yi = bisect_right(ys, ya + tol)
                    for yj in range(yi, len(ys)):
                        yb = ys[yj]
                        lv = _covers(V, Vs, xa, ya, yb, tol)
                        rv = _covers(V, Vs, xb, ya, yb, tol)
                        if lv is None or rv is None:
                            break
                        top = _covers(H, Hs, yb, xa, xb, tol)
                        if top is not None:
                            key = (xa, ya, xb, yb)
                            if key not in seen:
                                seen.add(key)
                                bot = _covers(H, Hs, ya, xa, xb, tol)
                                lays = [bot[2] if bot else None, top[2], lv[2], rv[2]]
                                cnt: Dict[str, int] = {}
                                for L in lays:
                                    if L:
                                        cnt[L] = cnt.get(L, 0) + 1
                                lay = max(lays, key=lambda L: (cnt.get(L, 0), -lays.index(L))) if cnt else None
                                out.append((xa, ya, xb, yb, lay))
                            found = True
                            break
                    if found:
                        break
    return out, other


# ---------------------------------------------------------------------------
# 페이지 / 축척
# ---------------------------------------------------------------------------

def _pages(doc: Doc) -> List[dict]:
    root = doc.resolve(doc.trailer.get("Root"))
    if not isinstance(root, dict):
        raise ImportErrorUser("PDF 문서 카탈로그(Root)를 찾지 못했다 — 파일이 손상됐다")
    out: List[dict] = []
    seen = set()

    def walk(node_ref, inh, depth):
        if depth > 64 or len(out) > 100000:
            return
        key = node_ref.num if isinstance(node_ref, Ref) else id(node_ref)
        if key in seen:
            return
        seen.add(key)
        node = doc.resolve(node_ref)
        if not isinstance(node, dict):
            return
        inh = dict(inh)
        for k in ("MediaBox", "CropBox", "Resources", "Rotate"):
            if k in node:
                inh[k] = node[k]
        kids = doc.resolve(node.get("Kids"))
        if node.get("Type") == "Pages" or (isinstance(kids, list) and node.get("Type") != "Page"):
            for kid in kids or []:
                walk(kid, inh, depth + 1)
        else:
            pg = dict(node)
            pg.update({k: v for k, v in inh.items()})
            pg["_ref"] = key
            out.append(pg)

    walk(root.get("Pages"), {}, 0)
    return out


def _select(opts: dict, n: int) -> List[int]:
    sel = opts.get("page", 1)
    if sel in (None, ""):
        sel = 1
    if isinstance(sel, str):
        s = sel.strip().lower()
        if s == "all":
            return list(range(1, n + 1))
        out = []
        for part in s.split(","):
            part = part.strip()
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
            if m:
                out += list(range(int(m.group(1)), int(m.group(2)) + 1))
            elif part.isdigit():
                out.append(int(part))
            elif part:
                raise ImportErrorUser(f"page='{sel}' 를 해석하지 못했다. 예: 1, [1,3], \"2-4\", \"all\"")
        sel = out
    if isinstance(sel, int):
        sel = [sel]
    sel = [int(x) for x in sel]
    for x in sel:
        if not 1 <= x <= n:
            raise ImportErrorUser(f"page={x} 는 범위 밖이다 — 이 PDF 는 {n} 쪽 (1~{n})")
    if not sel:
        raise ImportErrorUser("선택된 페이지가 없다")
    return list(dict.fromkeys(sel))


def _box(doc: Doc, pg: dict) -> Tuple[float, float, float, float]:
    mb = doc.resolve(pg.get("MediaBox"))
    cb = doc.resolve(pg.get("CropBox"))

    def norm(b):
        if isinstance(b, list) and len(b) == 4:
            v = [float(doc.resolve(x)) for x in b]
            return (min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3]))
        return None
    m = norm(mb) or (0.0, 0.0, 612.0, 792.0)
    c = norm(cb)
    if c:
        x0, y0 = max(m[0], c[0]), max(m[1], c[1])
        x1, y1 = min(m[2], c[2]), min(m[3], c[3])
        if x1 > x0 and y1 > y0:
            return (x0, y0, x1, y1)
    return m


def _page_matrix(box, rot: int):
    """사용자 공간 → 표시 공간(좌하단 원점, y 위, /Rotate 반영)."""
    x0, y0, x1, y1 = box
    W, H = x1 - x0, y1 - y0
    t = (1.0, 0.0, 0.0, 1.0, -x0, -y0)
    if rot == 90:
        r = (0.0, -1.0, 1.0, 0.0, 0.0, W)
        return mmul(t, r), (H, W)
    if rot == 180:
        r = (-1.0, 0.0, 0.0, -1.0, W, H)
        return mmul(t, r), (W, H)
    if rot == 270:
        r = (0.0, 1.0, -1.0, 0.0, H, 0.0)
        return mmul(t, r), (H, W)
    return t, (W, H)


_RATIO_RX = re.compile(r"^\s*1\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$")
_EQ_RX = re.compile(r"^\s*([0-9.]+)\s*([A-Za-z\"']+)\s*=\s*([0-9.]+)\s*([A-Za-z\"']+)\s*$")
_U_ALIAS = {'"': "in", "'": "ft", "inch": "in", "feet": "ft", "foot": "ft", "meter": "m"}


def _unit(u: str) -> Optional[float]:
    u = _U_ALIAS.get(u.strip().lower(), u.strip().lower())
    try:
        return unit_factor(u)
    except ImportErrorUser:
        return None


def _measure(doc: Doc, pg: dict) -> Optional[Tuple[float, str]]:
    vps = doc.resolve(pg.get("VP"))
    if not isinstance(vps, list):
        return None
    for vp in vps:
        vp = doc.resolve(vp)
        if not isinstance(vp, dict):
            continue
        ms = doc.resolve(vp.get("Measure"))
        if not isinstance(ms, dict) or ms.get("Subtype", "RL") != "RL":
            continue
        X = doc.resolve(ms.get("X"))
        if isinstance(X, list) and X:
            nf = doc.resolve(X[0])
            if isinstance(nf, dict):
                C = doc.resolve(nf.get("C"))
                U = _text_string(doc.resolve(nf.get("U")))
                f = _unit(U) if U else None
                if isinstance(C, (int, float)) and C > 0 and f:
                    mpp = float(C) * f
                    conv = "" if f == 1.0 else f" = {mpp:g} m"
                    return mpp, (f"PDF 뷰포트 /Measure /X: 1 pt = {float(C):g} {U}{conv}"
                                 f" (종이 축척 약 1:{mpp / PT_M:.6g})")
        R = _text_string(doc.resolve(ms.get("R")))
        if R:
            m = _RATIO_RX.match(R)
            if m:
                ratio = float(m.group(1))
                return PT_M * ratio, f"PDF 뷰포트 /Measure /R '{R}' → pt × 1:{ratio:g}"
            m = _EQ_RX.match(R)
            if m:
                pf, rf = _unit(m.group(2)), _unit(m.group(4))
                if pf and rf and float(m.group(1)) > 0:
                    ratio = float(m.group(3)) * rf / (float(m.group(1)) * pf)
                    return PT_M * ratio, f"PDF 뷰포트 /Measure /R '{R}' → 1:{ratio:g}"
    return None


def _off_layers(doc: Doc) -> set:
    root = doc.resolve(doc.trailer.get("Root")) or {}
    ocp = doc.resolve(root.get("OCProperties")) if isinstance(root, dict) else None
    if not isinstance(ocp, dict):
        return set()
    d = doc.resolve(ocp.get("D"))
    if not isinstance(d, dict):
        return set()
    it = Interp(doc, set())
    names = set()
    if d.get("BaseState") == "OFF":
        on = {id(doc.resolve(x)) for x in (doc.resolve(d.get("ON")) or [])}
        for g in doc.resolve(ocp.get("OCGs")) or []:
            if id(doc.resolve(g)) not in on:
                n = it.ocg_name(g)
                if n:
                    names.add(n)
    for g in doc.resolve(d.get("OFF")) or []:
        n = it.ocg_name(g)
        if n:
            names.add(n)
    return names


def _poly_key(pts, nd=3):
    q = [(round(p[0], nd), round(p[1], nd)) for p in pts]
    n = len(q)
    i = min(range(n), key=lambda k: q[k])
    fwd = q[i:] + q[:i]
    rev = [fwd[0]] + fwd[1:][::-1]
    return tuple(min(fwd, rev))


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def read(data: bytes, filename: str, opts: dict) -> dict:
    tk = new_takeoff(filename, "pdf", data)
    try:
        doc = Doc(data)
    except (_DecodeError, ValueError, IndexError, RecursionError) as e:
        raise ImportErrorUser(f"PDF 구조를 해석하지 못했다 ({e}). 다른 프로그램에서 'PDF로 다시 저장' 후 넣을 것")
    if doc.trailer.get("Encrypt") is not None:
        raise ImportErrorUser(
            "암호화된 PDF 는 읽지 않는다. 원본 프로그램에서 보안(암호화) 없이 다시 내보내거나, "
            "문서 열기 암호가 없다면 PDF 뷰어의 '인쇄 → PDF로 저장'으로 암호화를 푼 사본을 넣을 것")
    tk["warnings"] += doc.warnings
    try:
        pages = _pages(doc)
    except RecursionError:
        raise ImportErrorUser("PDF 페이지 트리가 손상됐다")
    if not pages:
        raise ImportErrorUser("PDF 에 페이지가 없다")
    sel = _select(opts, len(pages))

    user_scale = opts.get("scale_ratio") not in (None, "") or opts.get("m_per_unit") not in (None, "")
    scales = {}
    for pno in sel:
        meas = _measure(doc, pages[pno - 1])
        if user_scale:
            f, basis = resolve_scale(opts, "pt", "pdf")
            basis = "PDF 좌표 단위 pt(1/72 in) — " + basis
            if meas and abs(meas[0] - f) > 0.01 * f:
                tk["warnings"].append(
                    f"p.{pno}: 사용자 축척(1 pt = {f:g} m)이 PDF 내 축척 정보와 다르다 — {meas[1]}. "
                    f"사용자 지정을 따랐다")
        elif meas:
            f, basis = meas
        else:
            raise ImportErrorUser(
                "PDF 도면의 축척을 알 수 없다. PDF 좌표(pt)는 종이 크기일 뿐이다 — "
                "scale_ratio(종이 축척 1:N 의 N, 예: 1:100 이면 100) 또는 m_per_unit(1 pt 가 몇 m)을 "
                "지정할 것. 축척은 추정하지 않는다 (1:1 로 읽으면 모든 치수가 N 배 틀린다).")
        scales[pno] = (f, basis)
    uniq = list(dict.fromkeys(scales[p][1] for p in sel))
    tk["scale_basis"] = uniq[0] if len(uniq) == 1 else "; ".join(f"p.{p}: {scales[p][1]}" for p in sel)

    off = _off_layers(doc)
    layer_map = opts.get("layer_map")
    default_kind = str(opts.get("default_kind") or "equipment")
    min_size = float(opts.get("min_size_m", 0.3))
    snap = float(opts.get("snap_pt", 0.5))
    single_floor = opts.get("floor")
    texts_all: List[dict] = []
    st = tk["stats"]
    page_stats = []
    agg = dict(paths_painted=0, polygons=0, duplicates_removed=0, segments=0,
               segment_rects=0, frames_removed=0, title_block_removed=0, tiny_dropped=0,
               ignored_by_layer=0, hidden_layer_paths=0, images=0, texts=0,
               undecodable_texts=0, bad_operators=0, wall_strips_skipped=0)
    layer_count: Dict[str, int] = {}

    for fi, pno in enumerate(sel):
        pg = pages[pno - 1]
        floor = int(single_floor) if (single_floor not in (None, "") and len(sel) == 1) else fi
        tk["floors"].append({"index": floor, "label": f"{floor + 1}F", "page": pno})
        f, _b = scales[pno]
        box = _box(doc, pg)
        rot = doc.resolve(pg.get("Rotate")) or 0
        rot = int(rot) % 360 if isinstance(rot, (int, float)) else 0
        if rot % 90:
            rot = 0
        pm, (PW, PH) = _page_matrix(box, rot)
        res = doc.resolve(pg.get("Resources"))
        it = Interp(doc, off)
        contents = doc.resolve(pg.get("Contents"))
        streams = contents if isinstance(contents, list) else [contents]
        chunks = []
        for s in streams:
            s = doc.resolve(s)
            if isinstance(s, Stream):
                try:
                    chunks.append(doc.decode(s))
                except (_DecodeError, _ImageData) as e:
                    tk["warnings"].append(f"p.{pno}: 내용 스트림 복호 실패 ({e}) — 건너뜀")
        gs = GS()
        gs.ctm = pm
        try:
            it.run(b"\n".join(chunks), res if isinstance(res, dict) else {}, gs, 0, [], set())
        except _DecodeError as e:
            tk["warnings"].append(f"p.{pno}: 내용 해석 중단 ({e}) — 그때까지 읽은 것만 쓴다")
        except RecursionError:
            tk["warnings"].append(f"p.{pno}: 내용 중첩이 너무 깊어 중단했다")
        it._flush_text()

        page_area = PW * PH
        # --- 후보 도형 (pt, 표시 공간) ------------------------------------
        cands = []          # (pts, layer, ref, how)
        seen_keys = set()
        dups = 0
        for pts, layer, ref in it.polys:
            key = _poly_key(pts)
            if key in seen_keys:
                dups += 1
                continue
            seen_keys.add(key)
            cands.append((pts, layer, f"p{pno}:{ref}", "path"))
        min_pt = min_size / f
        segs = []
        tiny_seg = 0
        for s in it.segs:
            if math.hypot(s[2] - s[0], s[3] - s[1]) < min_pt:
                tiny_seg += 1
            else:
                segs.append(s)
        seg_rects = []
        other_segs = 0
        if len(segs) <= 60000:
            seg_rects, other_segs = rects_from_segments(segs, snap)
        else:
            tk["warnings"].append(f"p.{pno}: 직선 선분이 {len(segs)}개로 너무 많아 선분 고리 탐지를 건너뛰었다")
        n_seg_rects = 0
        for (x0, y0, x1, y1, lay) in seg_rects:
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            key = _poly_key(pts)
            if key in seen_keys:
                dups += 1
                continue
            seen_keys.add(key)
            n_seg_rects += 1
            cands.append((pts, lay or "path", f"p{pno}:lines@{x0:.2f},{y0:.2f}", "lines"))

        # --- 테두리·표제란 --------------------------------------------------
        def ruled(layer):
            return classify(layer, layer_map, default="\x00") != "\x00"

        frames = []
        frames_n = title_n = 0
        keep = []
        for c in cands:
            bx0, by0, bx1, by1 = _bb(c[0])
            a = (bx1 - bx0) * (by1 - by0)
            if a >= 0.95 * page_area or (a >= 0.80 * page_area and not ruled(c[1])):
                frames.append((bx0, by0, bx1, by1))
                frames_n += 1
                continue
            keep.append(c)
        margin = 0.03 * min(PW, PH)
        borders = [(0.0, 0.0, PW, PH)] + frames
        title_rects = []
        keep2 = []
        for c in keep:
            bx0, by0, bx1, by1 = _bb(c[0])
            if not ruled(c[1]):
                touch = False
                for (fx0, fy0, fx1, fy1) in borders:
                    near_x = abs(bx0 - fx0) <= margin or abs(bx1 - fx1) <= margin
                    near_y = abs(by0 - fy0) <= margin or abs(by1 - fy1) <= margin
                    if near_x and near_y:
                        touch = True
                        break
                if touch:
                    title_rects.append((bx0, by0, bx1, by1))
                    title_n += 1
                    continue
            keep2.append(c)
        keep3 = []
        for c in keep2:
            if title_rects and not ruled(c[1]):
                bx0, by0, bx1, by1 = _bb(c[0])
                if any(tx0 - 1e-6 <= bx0 and ty0 - 1e-6 <= by0 and bx1 <= tx1 + 1e-6 and by1 <= ty1 + 1e-6
                       for (tx0, ty0, tx1, ty1) in title_rects):
                    title_n += 1
                    continue
            keep3.append(c)

        # --- 미터 변환·분류 -------------------------------------------------
        tiny = ignored = strips = 0
        for pts, layer, ref, how in keep3:
            ptsm = [(p[0] * f, p[1] * f) for p in pts]
            x0, y0, x1, y1 = _bb(ptsm)
            if x1 - x0 < min_size or y1 - y0 < min_size:
                tiny += 1
                continue
            kind = classify(layer, layer_map, default=default_kind)
            if how == "lines" and kind == "ignore" and re.search(r"wall|벽", layer or "", re.I):
                w_, d_ = x1 - x0, y1 - y0
                if max(w_, d_) / min(w_, d_) >= 6.0:
                    strips += 1
                    continue
                kind = "room"
            if kind == "ignore":
                ignored += 1
                continue
            layer_count[layer] = layer_count.get(layer, 0) + 1
            if kind == "boundary":
                tk["boundary"].append({"floor": floor, "rect": [round(x0, 6), round(y0, 6),
                                                                 round(x1, 6), round(y1, 6)],
                                       "source_ref": ref})
                continue
            if kind not in ("equipment", "room", "obstacle", "void", "opening"):
                kind = default_kind
            tk["items"].append(make_item(kind, floor, ptsm, layer=layer, source_ref=ref))
        for t in it.texts:
            texts_all.append({"floor": floor, "x": t["x"] * f, "y": t["y"] * f, "text": t["text"]})

        if it.image_area >= 0.5 * page_area and (len(it.polys) + len(it.segs)) < 20:
            tk["warnings"].append(
                f"p.{pno}: 스캔/래스터 PDF로 보인다 (이미지가 페이지의 {min(1.0, it.image_area / page_area):.0%} "
                f"를 덮고 벡터 경로는 {len(it.polys) + len(it.segs)}개뿐) — PNG로 변환해 png 경로로 넣을 것 "
                f"(px_per_m 지정)")
        if len(it.segs) >= 20000 and tiny_seg >= 0.9 * len(it.segs):
            tk["warnings"].append(
                f"p.{pno}: 선분 {len(it.segs)}개 중 {tiny_seg}개가 {min_size} m 미만의 짧은 획이다 — "
                f"래스터를 짧은 주사선 획으로 '벡터화'한 그림이거나 해치가 대부분으로 보인다. "
                f"선분 고리(실) 인식이 불완전할 수 있으니 PNG로 변환해 png 경로로 넣는 것을 권한다")
        if it.undecoded:
            tk["warnings"].append(f"p.{pno}: 해독하지 못한 문자열 {it.undecoded}개를 버렸다 "
                                  f"(ToUnicode 없는 CID 글꼴 등 — 문자를 지어내지 않는다)")
        if tiny:
            tk["warnings"].append(f"p.{pno}: {min_size} m 미만 도형 {tiny}개를 버렸다 (해치·문자 윤곽·기호로 판단)")
        if title_n:
            tk["warnings"].append(f"p.{pno}: 용지 가장자리에 붙은 표제란/도곽 도형 {title_n}개를 버렸다 "
                                  f"(분류 규칙에 걸리는 레이어면 남는다 — layer_map 으로 지정 가능)")
        ps = dict(page=pno, floor=floor, size_pt=[round(PW, 3), round(PH, 3)], rotate=rot,
                  paths_painted=it.n_paint, polygons=len(it.polys), duplicates_removed=dups,
                  segments=len(it.segs), tiny_segments=tiny_seg, non_axis_segments=other_segs,
                  segment_rects=n_seg_rects, frames_removed=frames_n, title_block_removed=title_n,
                  tiny_dropped=tiny, ignored_by_layer=ignored, hidden_layer_paths=it.hidden,
                  images=it.n_images, image_cover=round(min(1.0, it.image_area / page_area), 4) if page_area else 0,
                  texts=len(it.texts), undecodable_texts=it.undecoded, bad_operators=it.bad_ops,
                  wall_strips_skipped=strips, operators=it.n_ops)
        page_stats.append(ps)
        for k in agg:
            agg[k] += ps.get(k, 0)

    assign_texts(tk, texts_all)
    st.update(agg)
    st["pages"] = page_stats
    st["xref"] = doc.mode
    st["layers"] = dict(sorted(layer_count.items()))
    if off:
        st["hidden_layers"] = sorted(off)
    boundary_from_items(tk)
    normalize_origin(tk)
    finalize(tk, min_size)
    return tk


def _bb(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)
