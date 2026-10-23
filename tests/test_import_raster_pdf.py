# -*- coding: utf-8 -*-
"""
test_import_raster_pdf.py — PDF(벡터)·PNG(래스터) 가져오기의 결정론적 추출 검증.

픽스처는 tools/make_fixtures_raster_pdf.py 가 만든다 (표준 라이브러리만, 손으로 짠 PDF 객체).
기대값은 픽스처를 그린 좌표 그대로다 — PDF 는 상대 1e-6, PNG 는 벽 두께/px_per_m 이내.
"""
import json
import os
import unittest
import zlib

import _util  # noqa: F401  (engine 을 import 경로에 올린다)
from importers import import_file, import_path
from importers.common import ImportErrorUser
from importers import pdf as PDF
from importers import png as PNG

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fx(name):
    return os.path.join(FIX, name)


def read_bytes(name):
    with open(fx(name), "rb") as fp:
        return fp.read()


def by_id(tk):
    return {it["id"]: it for it in tk["items"]}


def dims(tk, kind):
    return sorted((round(it["w"], 6), round(it["d"], 6)) for it in tk["items"] if it["kind"] == kind)


def lzw_encode(data: bytes, early: int = 1) -> bytes:
    """시험용 LZW 부호기 (PDF LZWDecode, EarlyChange). 복호기는 한 코드 늦게 표를 채우므로
    폭은 '복호기의 다음 코드(= 부호기 다음 코드 - 1)' 기준으로 정한다."""
    table = {bytes([i]): i for i in range(256)}
    nxt = 258
    out_bits = []

    def width():
        w = 9
        while w < 12 and (nxt - 1) + early >= (1 << w):
            w += 1
        return w
    out_bits.append((256, 9))
    w_ = b""
    for c in data:
        wc = w_ + bytes([c])
        if wc in table:
            w_ = wc
            continue
        out_bits.append((table[w_], width()))
        if nxt < 4096:
            table[wc] = nxt
            nxt += 1
        w_ = bytes([c])
    if w_:
        out_bits.append((table[w_], width()))
    nxt += 1
    out_bits.append((257, width()))
    acc = 0
    nb = 0
    out = bytearray()
    for code, w in out_bits:
        acc = (acc << w) | code
        nb += w
        while nb >= 8:
            nb -= 8
            out.append((acc >> nb) & 255)
    if nb:
        out.append((acc << (8 - nb)) & 255)
    return bytes(out)


class TestPDF(unittest.TestCase):
    OPTS = {"scale_ratio": 100}

    def check_page1(self, tk):
        T = 1e-6
        self.assertEqual(tk["stats"]["by_kind"], {"room": 3, "equipment": 2, "obstacle": 1})
        for got, want in zip(dims(tk, "room"), sorted([(10.0, 8.0), (8.0, 8.0), (9.0, 6.0)])):
            self.assertAlmostEqual(got[0], want[0], delta=T * want[0])
            self.assertAlmostEqual(got[1], want[1], delta=T * want[1])
        it = by_id(tk)
        self.assertIn("P-101", it)
        self.assertIn("HX-2", it, "증분 갱신(/Prev)의 새 판 내용이 반영돼야 한다")
        self.assertNotIn("HX-1", it)
        p, h = it["P-101"], it["HX-2"]
        self.assertEqual(p["kind"], "equipment")
        self.assertEqual(p["layer"], "EQUIP", "UTF-16 OCG 이름을 해독해야 한다")
        self.assertAlmostEqual(p["w"], 3.0, delta=3e-6)
        self.assertAlmostEqual(p["d"], 2.0, delta=2e-6)
        self.assertAlmostEqual(p["x"], 2.0, delta=2e-6)
        self.assertAlmostEqual(p["y"], 12.0, delta=12e-6)
        self.assertAlmostEqual(h["w"], 2.5, delta=3e-6)        # Form XObject + /Matrix
        self.assertAlmostEqual(h["x"], 8.0, delta=8e-6)
        self.assertAlmostEqual(h["d"], 1.5, delta=2e-6)
        col = [i for i in tk["items"] if i["kind"] == "obstacle"][0]
        self.assertEqual(col["layer"], "COLUMN")
        self.assertAlmostEqual(col["w"], 1.0, delta=1e-6)       # 베지에 원 → 외접 1×1
        self.assertAlmostEqual(col["d"], 1.0, delta=1e-6)
        self.assertIsNotNone(col["polygon"])
        kor = [i for i in tk["items"] if "펌프실" in i["labels"]]
        self.assertEqual(len(kor), 1, "Type0/Identity-H + ToUnicode 한글 해독")
        self.assertEqual((kor[0]["w"], kor[0]["d"]), (10.0, 8.0))
        self.assertEqual(kor[0]["name"], "펌프실")
        self.assertEqual(len(tk["boundary"]), 1)
        b = tk["boundary"][0]["rect"]
        for g, w in zip(b, [0.0, 0.0, 40.0, 25.0]):
            self.assertAlmostEqual(g, w, delta=1e-6 * 40)
        st = tk["stats"]
        self.assertEqual(st["frames_removed"], 1)
        self.assertEqual(st["title_block_removed"], 1)
        self.assertGreaterEqual(st["duplicates_removed"], 1)   # 칠 + 획 이중 출력
        self.assertEqual(st["segment_rects"], 1)               # 낱개 선분 4개로 그린 R3
        self.assertEqual(st["undecodable_texts"], 0)
        self.assertEqual([t["text"] for t in tk["texts"]], ["SMR TEST PLAN"])

    def test_classic_xref_incremental(self):
        tk = import_path(fx("plan_classic.pdf"), self.OPTS)
        self.check_page1(tk)
        self.assertIn("2개 판", tk["stats"]["xref"])
        self.assertIn("1:100", tk["scale_basis"])
        self.assertEqual(tk["source"]["format"], "pdf")

    def test_xref_stream_objstm(self):
        tk = import_path(fx("plan_xrefstm.pdf"), self.OPTS)
        self.assertEqual(tk["stats"]["xref"], "xref-stream")
        self.check_page1(tk)
        a = import_path(fx("plan_classic.pdf"), self.OPTS)
        key = lambda t: [(i["id"], i["kind"], i["x"], i["y"], i["w"], i["d"], i["labels"]) for i in t["items"]]
        self.assertEqual(key(a), key(tk))

    def test_all_pages_rotate(self):
        tk = import_path(fx("plan_xrefstm.pdf"), dict(self.OPTS, page="all"))
        self.assertEqual([f["index"] for f in tk["floors"]], [0, 1])
        f1 = [i for i in tk["items"] if i["floor"] == 1]
        self.assertEqual(len(f1), 1)
        self.assertEqual(f1[0]["id"], "CTRL")
        self.assertAlmostEqual(f1[0]["w"], 4.0, delta=4e-6)     # /Rotate 90 → 6×4 가 4×6 로
        self.assertAlmostEqual(f1[0]["d"], 6.0, delta=6e-6)
        b1 = [b for b in tk["boundary"] if b["floor"] == 1][0]["rect"]
        self.assertAlmostEqual(b1[2] - b1[0], 15.0, delta=1e-5)
        self.assertAlmostEqual(b1[3] - b1[1], 20.0, delta=1e-5)

    def test_floor_option_and_page_select(self):
        tk = import_path(fx("plan_classic.pdf"), dict(self.OPTS, page=2, floor=3))
        self.assertEqual({i["floor"] for i in tk["items"]}, {3})
        tk = import_path(fx("plan_classic.pdf"), dict(self.OPTS, page="1-2"))
        self.assertEqual(tk["stats"]["floors"], 2)
        with self.assertRaises(ImportErrorUser):
            import_path(fx("plan_classic.pdf"), dict(self.OPTS, page=3))

    def test_missing_scale(self):
        with self.assertRaises(ImportErrorUser) as cm:
            import_path(fx("plan_classic.pdf"), {})
        self.assertIn("scale_ratio", str(cm.exception))
        tk = import_path(fx("plan_classic.pdf"), {"m_per_unit": 0.0254 / 72 * 100})
        self.check_page1(tk)

    def test_measure_dictionary(self):
        tk = import_path(fx("plan_measure.pdf"), {})
        self.assertIn("/Measure", tk["scale_basis"])
        self.assertEqual(len(tk["items"]), 1)
        it = tk["items"][0]
        self.assertAlmostEqual(it["w"], 5.0, delta=5e-6)
        self.assertAlmostEqual(it["d"], 3.0, delta=3e-6)
        self.assertEqual(it["kind"], "equipment")
        tk = import_path(fx("plan_measure.pdf"), {"default_kind": "room"})
        self.assertEqual(tk["items"][0]["kind"], "room")
        # 사용자 축척이 다르면 사용자 지정을 따르되 경고
        tk = import_path(fx("plan_measure.pdf"), {"scale_ratio": 50})
        self.assertAlmostEqual(tk["items"][0]["w"], 2.5, delta=1e-5)
        self.assertTrue(any("다르다" in w for w in tk["warnings"]))

    def test_layer_map(self):
        tk = import_path(fx("plan_classic.pdf"), dict(self.OPTS, layer_map={"^EQUIP$": "obstacle"}))
        self.assertEqual(tk["stats"]["by_kind"].get("equipment", 0), 0)
        self.assertEqual(tk["stats"]["by_kind"]["obstacle"], 3)

    def test_scanned_warning(self):
        tk = import_path(fx("scanned.pdf"), self.OPTS)
        self.assertTrue(any("스캔/래스터 PDF" in w and "png" in w for w in tk["warnings"]))
        self.assertEqual(tk["stats"]["images"], 1)

    def test_encrypted_rejected(self):
        with self.assertRaises(ImportErrorUser) as cm:
            import_path(fx("encrypted.pdf"), self.OPTS)
        self.assertIn("암호화", str(cm.exception))

    def test_broken_xref_recovery(self):
        data = read_bytes("plan_classic.pdf")
        i = data.rfind(b"startxref")
        bad = data[:i] + b"startxref\n12\n%%EOF\n"
        tk = import_file(bad, "broken.pdf", self.OPTS)
        self.assertTrue(any("전수 탐색" in w for w in tk["warnings"]))
        self.assertEqual(tk["stats"]["xref"], "scan")
        self.check_page1(tk)

    def test_filters(self):
        payload = (b"0 0 m 10 10 l S BT (HELLO) Tj ET " * 300) + bytes(range(256)) * 4
        self.assertEqual(PDF._lzw(lzw_encode(payload, 1), 1), payload)
        self.assertEqual(PDF._lzw(lzw_encode(payload, 0), 0), payload)
        import base64
        self.assertEqual(PDF._a85(b"<~" + base64.a85encode(payload) + b"~>"), payload)
        self.assertEqual(PDF._ahx(payload.hex().encode() + b">"), payload)
        self.assertEqual(PDF._rle(b"\x02abc\xfdz\x80"), b"abczzzz")
        # PNG 예측자 (Sub/Up/Paeth) — 열 3, 색 1
        rows = [b"\x01\x05\x01\x01", b"\x02\x01\x01\x01", b"\x04\x00\x00\x00"]
        self.assertEqual(PDF._predict(b"".join(rows), {"Predictor": 12, "Columns": 3}),
                         bytes([5, 6, 7, 6, 7, 8, 6, 7, 8]))
        # 리터럴 문자열 이스케이프·8진수·괄호 균형
        s, _ = PDF._parse_literal(b"a\\(b\\)c (nested) \\101\\0612\\\nZ)", 0)
        self.assertEqual(s, b"a(b)c (nested) A12Z")

    def test_tounicode_cmap(self):
        cm = PDF.CMap(b"1 begincodespacerange <00> <FF> endcodespacerange "
                      b"1 beginbfrange <41> <43> <0058> endbfrange "
                      b"1 beginbfrange <61> <62> [<D55C> <AE00>] endbfrange")
        self.assertEqual(cm.lookup(1, 0x42), "Y")
        self.assertEqual(cm.lookup(1, 0x62), "글")
        self.assertIsNone(cm.lookup(1, 0x7A))

    def test_determinism(self):
        a = json.dumps(import_path(fx("plan_classic.pdf"), dict(self.OPTS, page="all")),
                       sort_keys=True, ensure_ascii=False)
        b = json.dumps(import_path(fx("plan_classic.pdf"), dict(self.OPTS, page="all")),
                       sort_keys=True, ensure_ascii=False)
        self.assertEqual(a, b)


class TestPNG(unittest.TestCase):
    PPM = 20.0
    TOL = 6 / 20.0          # 벽 두께 / px_per_m

    def rooms(self, tk):
        return sorted((it["w"], it["d"]) for it in tk["items"] if it["kind"] == "room")

    def assert_dims(self, got, want):
        self.assertEqual(len(got), len(want), f"{got} vs {want}")
        for g, w in zip(sorted(got), sorted(want)):
            self.assertAlmostEqual(g[0], w[0], delta=self.TOL)
            self.assertAlmostEqual(g[1], w[1], delta=self.TOL)

    def test_door_gap_merges_without_closing(self):
        tk = import_path(fx("plan.png"), {"px_per_m": self.PPM})
        self.assert_dims(self.rooms(tk), [(9.7, 17.4), (17.4, 17.4)])

    def test_close_gaps_separates(self):
        tk = import_path(fx("plan.png"), {"px_per_m": self.PPM, "close_gaps_px": 6})
        self.assert_dims(self.rooms(tk), [(9.7, 17.4), (8.7, 17.4), (8.4, 17.4)])
        self.assertTrue(any("close_gaps_px" in w for w in tk["warnings"]))

    def test_obstacle_boundary_wall(self):
        tk = import_path(fx("plan.png"), {"px_per_m": self.PPM})
        obs = [it for it in tk["items"] if it["kind"] == "obstacle"]
        self.assertEqual(len(obs), 1)
        self.assertAlmostEqual(obs[0]["w"], 1.0, delta=self.TOL)
        self.assertAlmostEqual(obs[0]["d"], 1.0, delta=self.TOL)
        # y 뒤집기: 기둥 화소 y 100~120 (위에서) → 경계 하단 기준 y = (380-120)/20 = 13
        self.assertAlmostEqual(obs[0]["x"], 4.0, delta=self.TOL)
        self.assertAlmostEqual(obs[0]["y"], 13.0, delta=self.TOL)
        b = tk["boundary"][0]["rect"]
        self.assertAlmostEqual(b[2] - b[0], 28.0, delta=self.TOL)
        self.assertAlmostEqual(b[3] - b[1], 18.0, delta=self.TOL)
        self.assertEqual(tk["stats"]["wall_thickness_px"], 6)
        self.assertEqual([it["id"] for it in tk["items"] if it["kind"] == "room"], ["RM001", "RM002"])
        self.assertTrue(any("OCR" in w for w in tk["warnings"]))

    def test_variants_same_plan(self):
        """팔레트 4비트+tRNS / RGB Adam7 / 회색+알파 16비트 가 같은 결과."""
        base = import_path(fx("plan.png"), {"px_per_m": self.PPM, "close_gaps_px": 6})
        key = lambda t: [(i["kind"], i["x"], i["y"], i["w"], i["d"]) for i in t["items"]]
        for name in ("plan_palette.png", "plan_rgb_adam7.png", "plan_ga16.png"):
            tk = import_path(fx(name), {"px_per_m": self.PPM, "close_gaps_px": 6})
            self.assertEqual(key(tk), key(base), name)

    def test_color_map(self):
        for name in ("plan_palette.png", "plan_rgb_adam7.png"):
            tk = import_path(fx(name), {"px_per_m": self.PPM, "color_map": {"#FFC864": "equipment"}})
            eq = [it for it in tk["items"] if it["kind"] == "equipment"]
            self.assertEqual(len(eq), 1, name)
            self.assertAlmostEqual(eq[0]["w"], 4.0, delta=self.TOL)
            self.assertAlmostEqual(eq[0]["d"], 4.0, delta=self.TOL)
            self.assertEqual(eq[0]["layer"], "color:#FFC864")
            self.assertEqual(len(self.rooms(tk)), 2)
        with self.assertRaises(ImportErrorUser):
            import_path(fx("plan_palette.png"), {"px_per_m": 20, "color_map": {"orange": "equipment"}})

    def test_missing_scale(self):
        with self.assertRaises(ImportErrorUser) as cm:
            import_path(fx("plan.png"), {})
        self.assertIn("px_per_m", str(cm.exception))
        self.assertIn("dpi", str(cm.exception))      # pHYs 를 정보로 안내
        tk = import_path(fx("plan.png"), {"m_per_unit": 0.05})
        self.assert_dims(self.rooms(tk), [(9.7, 17.4), (17.4, 17.4)])

    def test_downsample(self):
        tk = import_path(fx("plan.png"), {"px_per_m": self.PPM, "downsample": 2, "close_gaps_px": 6})
        self.assertEqual(tk["stats"]["downsample"], 2)
        self.assertIn("축소", tk["scale_basis"])
        self.assert_dims(self.rooms(tk), [(9.7, 17.4), (8.7, 17.4), (8.4, 17.4)])

    def test_threshold_option(self):
        # 임계값 20 → 벽(30)이 밝게 → 닫힌 실 없음
        tk = import_path(fx("plan.png"), {"px_per_m": self.PPM, "threshold": 20})
        self.assertEqual(self.rooms(tk), [])

    def test_corrupt(self):
        data = read_bytes("plan.png")
        with self.assertRaises(ImportErrorUser):
            PNG.decode_png(b"\x89PNG\r\n\x1a\n" + data[8:40])
        with self.assertRaises(ImportErrorUser):
            PNG.decode_png(b"not a png")

    def test_unfilter_all_types(self):
        # 2 바이트/화소, 폭 3 — 직접 계산한 기대값과 비교
        raw = (b"\x00" + bytes([1, 2, 3, 4, 5, 6]) + b"\x01" + bytes([1, 1, 1, 1, 1, 1])
               + b"\x02" + bytes([1, 1, 1, 1, 1, 1]) + b"\x03" + bytes([2, 2, 2, 2, 2, 2])
               + b"\x04" + bytes([0, 0, 0, 0, 0, 0]))
        out = PNG.unfilter(raw, 5, 6, 2)
        r1 = [1, 2, 3, 4, 5, 6]
        r2 = [1, 1, 2, 2, 3, 3]
        r3 = [2, 2, 3, 3, 4, 4]
        prev = r3                               # Average: raw + (왼쪽 + 위) // 2
        cur = [0] * 6
        for i in range(6):
            a = cur[i - 2] if i >= 2 else 0
            cur[i] = (2 + ((a + prev[i]) >> 1)) & 255
        r4 = cur
        r5 = []
        for i in range(6):
            a = r5[i - 2] if i >= 2 else 0
            b = r4[i]
            c = r4[i - 2] if i >= 2 else 0
            pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
            r5.append(a if (pa <= pb and pa <= pc) else (b if pb <= pc else c))
        self.assertEqual(list(out), r1 + r2 + r3 + r4 + r5)

    def test_determinism(self):
        o = {"px_per_m": self.PPM, "close_gaps_px": 6, "color_map": {"#FFC864": "void"}}
        a = json.dumps(import_path(fx("plan_rgb_adam7.png"), o), sort_keys=True, ensure_ascii=False)
        b = json.dumps(import_path(fx("plan_rgb_adam7.png"), o), sort_keys=True, ensure_ascii=False)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
