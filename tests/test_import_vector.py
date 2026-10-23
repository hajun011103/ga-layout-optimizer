# -*- coding: utf-8 -*-
"""
test_import_vector.py — 벡터 도면(DXF/SVG/IFC)·기기 목록표(CSV/XLSX/JSON) 가져오기.

표본은 tools/make_fixtures_vector.py 가 만든다 (치수는 그 파일 주석과 1:1).
핵심 주장
  1) 치수는 파일 단위 근거로만 m 로 바뀐다 — 축척을 모르면 사용자에게 묻는다(ImportErrorUser).
  2) 이 엔진이 쓴 DXF 는 다시 읽으면 같은 기기·층·치수가 나온다 (왕복).
  3) 같은 바이트 + 같은 옵션 → 같은 Takeoff.
"""
import json
import os
import subprocess
import sys
import unittest

from _util import ROOT, EXAMPLES
import importers as I
from importers.common import ImportErrorUser

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
TOL = 1e-6


def _ensure_fixtures():
    need = ["plant_mm.dxf", "plant_cp949.dxf", "plant.svg", "plant.ifc",
            "equipment.csv", "equipment.xlsx"]
    if not all(os.path.exists(os.path.join(FIX, n)) for n in need):
        subprocess.check_call([sys.executable, os.path.join(ROOT, "tools", "make_fixtures_vector.py")],
                              stdout=subprocess.DEVNULL)


def load(name, opts=None):
    return I.import_path(os.path.join(FIX, name), opts or {})


def by_id(tk):
    return {it["id"]: it for it in tk["items"]}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_fixtures()

    def assertBox(self, it, x, y, w, d):
        for k, v in (("x", x), ("y", y), ("w", w), ("d", d)):
            self.assertAlmostEqual(it[k], v, delta=TOL, msg=f"{it['id']}.{k}")


class TestDxf(_Base):

    def test_fixture_items(self):
        tk = load("plant_mm.dxf")
        self.assertIn("$INSUNITS=4", tk["scale_basis"])
        self.assertEqual(tk["boundary"], [{"floor": 0, "rect": [0.0, 0.0, 30.0, 20.0],
                                           "source_ref": tk["boundary"][0]["source_ref"]}])
        self.assertEqual(tk["stats"]["by_kind"], {"room": 3, "equipment": 3, "obstacle": 1})
        m = by_id(tk)
        self.assertEqual(sorted(m), ["FX001", "HX-301", "MCR-01", "P-101", "P-102", "RM001", "RM002"])
        self.assertBox(m["RM001"], 1, 1, 10, 8)
        self.assertEqual(m["RM001"]["name"], "펌프실")          # MTEXT 서식 제거
        self.assertEqual(m["RM002"]["name"], "전기실")          # \U+XXXX
        self.assertBox(m["MCR-01"], 1, 10, 14, 9)
        self.assertEqual(m["MCR-01"]["name"], "제어실")         # MTEXT 문단 → 태그/명칭 분리
        self.assertEqual(m["MCR-01"]["kind"], "room")
        # INSERT + ATTRIB
        self.assertBox(m["P-101"], 3, 3, 2, 1)
        self.assertEqual(m["P-101"]["name"], "냉각수 펌프 A")
        self.assertEqual(m["P-101"]["block"], "PUMP")
        # 중첩 블록 + 90° 회전
        self.assertBox(m["P-102"], 15, 2, 2, 3)
        self.assertEqual(m["P-102"]["kind"], "equipment")
        # LINE 4 개 직사각형
        self.assertBox(m["HX-301"], 21, 2, 3, 2)
        self.assertTrue(m["HX-301"]["source_ref"].startswith("LINES:EQUIP"))
        self.assertBox(m["FX001"], 25, 15, 0.5, 0.5)
        self.assertEqual(m["FX001"]["kind"], "obstacle")
        self.assertTrue(all(it["floor"] == 0 for it in tk["items"]))
        # DIM 레이어 문자는 라벨이 되지 않는다
        self.assertFalse(any("3000" in l for it in tk["items"] for l in it["labels"]))

    def test_cp949_and_floor_from_layer(self):
        with self.assertRaises(ImportErrorUser) as cm:
            load("plant_cp949.dxf")
        self.assertIn("units", str(cm.exception))
        tk = load("plant_cp949.dxf", {"units": "mm"})
        self.assertEqual(tk["stats"]["encoding"], "cp949")
        (it,) = tk["items"]
        self.assertEqual(it["name"], "배터리실")
        self.assertEqual(it["floor"], 1)                        # 레이어 2F_ROOM
        self.assertBox(it, 0, 0, 4, 3)

    def test_round_trip_engine_output(self):
        """examples/out/GA_optimized.dxf → JSON 의 기기 본체(이격 제외)와 일치."""
        tk = I.import_path(os.path.join(EXAMPLES, "out", "GA_optimized.dxf"))
        self.assertIn("FLOOR_nn_", tk["scale_basis"])
        with open(os.path.join(EXAMPLES, "out", "GA_optimized.json"), encoding="utf-8") as fp:
            ref = json.load(fp)
        blocks = {b["id"]: b for b in ref["blocks"]}
        m = by_id(tk)
        self.assertEqual(sorted(m), sorted(blocks))
        self.assertTrue(all(it["kind"] == "equipment" for it in tk["items"]))
        for bid, b in blocks.items():
            it, cl = m[bid], b["clearance"]
            self.assertEqual(it["floor"], b["floor"], bid)
            for k, v in (("x", b["x"] + cl), ("y", b["y"] + cl),
                         ("w", b["w"] - 2 * cl), ("d", b["d"] - 2 * cl)):
                self.assertAlmostEqual(it[k], v, delta=1e-4, msg=f"{bid}.{k}")
        env = ref["envelope"]
        self.assertEqual(len(tk["boundary"]), len(ref["floors"]))
        for bnd in tk["boundary"]:
            self.assertEqual(bnd["rect"], [0.0, 0.0, env["width_m"], env["depth_m"]])
        self.assertEqual(tk["texts"], [])                        # FZ 라벨은 무시

    def test_layer_map_override(self):
        tk = load("plant_mm.dxf", {"layer_map": {"^COLUMN$": "void"}})
        self.assertEqual(tk["stats"]["by_kind"].get("void"), 1)
        self.assertNotIn("obstacle", tk["stats"]["by_kind"])


class TestSvg(_Base):

    def test_fixture_items(self):
        tk = load("plant.svg", {"scale_ratio": 100})
        self.assertIn("300mm", tk["scale_basis"])
        self.assertEqual(tk["boundary"][0]["rect"], [0.0, 0.0, 30.0, 20.0])
        m = by_id(tk)
        self.assertEqual(sorted(m), ["FX001", "P-201", "P-202", "RM001", "RM002", "RM003", "TK-101"])
        self.assertBox(m["RM001"], 1, 11, 10, 8)                 # y 뒤집기
        self.assertEqual(m["RM001"]["name"], "펌프실")
        self.assertBox(m["RM002"], 12, 11, 8, 8)                 # 상대 l + 암묵 반복
        self.assertEqual(m["RM002"]["kind"], "room")
        self.assertBox(m["P-201"], 2, 17, 2, 1)
        self.assertEqual(m["P-201"]["name"], "급수펌프")
        self.assertBox(m["P-202"], 16, 13, 1, 3)                 # 중첩 translate + rotate(90)
        self.assertBox(m["TK-101"], 3, 6, 3, 2)                  # <line> 4 개
        self.assertBox(m["FX001"], 10.25, 4.75, 0.5, 0.5)        # circle → 16각형
        self.assertEqual(m["FX001"]["kind"], "obstacle")
        self.assertAlmostEqual(m["FX001"]["rect_fill"], 0.7854, places=4)
        self.assertEqual(m["RM003"]["floor"], 1)                 # g inkscape:label="2F"
        self.assertEqual(m["RM003"]["name"], "제어실")
        self.assertBox(m["RM003"], 2, 4, 5, 4)                   # C 곡선·지수 표기
        self.assertEqual([it["floor"] for it in tk["items"]].count(0), 6)
        self.assertEqual(tk["texts"], [])

    def test_px_svg_needs_scale(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect id="room" x="0" y="0" width="100" height="50"/></svg>'
        with self.assertRaises(ImportErrorUser) as cm:
            I.import_file(svg, "a.svg", {})
        self.assertIn("px_per_m", str(cm.exception))
        tk = I.import_file(svg, "a.svg", {"px_per_m": 10})
        self.assertBox(tk["items"][0], 0, 0, 10, 5)
        svg2 = svg.replace(b'height="300"', b'height="300" data-m-per-unit="0.05"')
        self.assertBox(I.import_file(svg2, "a.svg", {})["items"][0], 0, 0, 5, 2.5)

    def test_path_parser(self):
        from importers.svg import parse_path
        subs = parse_path("M0,0L10-5e-1 10.5.5zm1 1h2v2h-2z")
        self.assertEqual(len(subs), 2)
        self.assertEqual(subs[0][0], [(0.0, 0.0), (10.0, -0.5), (10.5, 0.5)])
        self.assertTrue(subs[0][1])
        self.assertEqual(subs[1][0], [(1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0)])
        arc = parse_path("M0 0 a1 1 0 0110 0")          # 압축된 원호 플래그
        self.assertEqual(arc[0][0][-1], (10.0, 0.0))


class TestIfc(_Base):

    def test_fixture_items(self):
        tk = load("plant.ifc")
        self.assertIn("MILLI", tk["scale_basis"])
        self.assertEqual([(f["index"], f["label"]) for f in tk["floors"]],
                         [(0, "Level 1"), (1, "Level 2")])
        m = by_id(tk)
        self.assertEqual(sorted(m), ["101", "201", "P-301"])
        self.assertEqual(m["101"]["kind"], "room")
        self.assertEqual(m["101"]["name"], "펌프실")             # \X2\ 유니코드
        self.assertEqual(m["101"]["floor"], 0)
        self.assertBox(m["101"], 5, 4, 8, 6)
        self.assertAlmostEqual(m["101"]["h"], 4.5, delta=TOL)
        self.assertEqual(m["201"]["floor"], 1)
        self.assertEqual(m["201"]["name"], "제어실")
        self.assertBox(m["201"], 0, 0, 10, 5)
        self.assertEqual(m["P-301"]["kind"], "equipment")
        self.assertEqual(m["P-301"]["floor"], 0)
        self.assertBox(m["P-301"], 6.5, 5.5, 1, 3)               # RefDirection (0,1,0) = 90°
        self.assertAlmostEqual(m["P-301"]["h"], 2.5, delta=TOL)
        self.assertEqual(tk["stats"]["ifc_ignored"], {"IFCWALL": 1})

    def test_step_strings(self):
        from importers.ifc import _parse_args, _decode_str
        self.assertEqual(_decode_str("\\X2\\D38CD504C2E4\\X0\\"), "펌프실")
        a = _parse_args("('it''s',$,*,.T.,(1,2.5,#3),IFCLENGTHMEASURE(2.))")
        self.assertEqual(a[0], "it's")
        self.assertIsNone(a[1]); self.assertIsNone(a[2])
        self.assertEqual(a[3], "T")
        self.assertEqual(a[4], [1, 2.5, 3])
        self.assertEqual(a[5][0], "IFCLENGTHMEASURE")


class TestTable(_Base):

    def test_csv(self):
        tk = load("equipment.csv")
        m = by_id(tk)
        self.assertEqual(sorted(m), ["P-101", "P-102", "TK-201"])
        self.assertBox(m["P-101"], 0, 0, 2, 1)
        self.assertEqual(m["P-101"]["name"], "냉각수 펌프 A")
        self.assertAlmostEqual(m["P-101"]["h"], 1.5, delta=TOL)
        self.assertAlmostEqual(m["P-101"]["clearance"], 0.6, delta=TOL)
        self.assertEqual(m["P-101"]["zone"], "RCA")
        self.assertEqual(m["P-101"]["weight"], 3.5)
        self.assertTrue(m["P-101"]["unplaced"])
        self.assertEqual(m["TK-201"]["floor"], 1)
        self.assertFalse(tk["stats"]["has_positions"])
        miss = tk["stats"]["rows_missing_dims"]
        self.assertEqual([(r["id"], r["missing"]) for r in miss], [("HX-301", ["w"])])

    def test_csv_needs_units_without_hints(self):
        csv_ = "id,name,w,d\nA-1,펌프,2,1\n".encode("utf-8")
        with self.assertRaises(ImportErrorUser) as cm:
            I.import_file(csv_, "list.csv", {})
        self.assertIn("units", str(cm.exception))
        tk = I.import_file(csv_, "list.csv", {"units": "m"})
        self.assertBox(tk["items"][0], 0, 0, 2, 1)

    def test_xlsx(self):
        tk = load("equipment.xlsx", {"units": "m"})
        m = by_id(tk)
        self.assertEqual(sorted(m), ["E-1", "E-2", "E-3"])
        self.assertTrue(tk["stats"]["has_positions"])
        self.assertBox(m["E-1"], 0, 0, 2.5, 1.5)                 # 원점 (10,5) 로 정규화
        self.assertBox(m["E-2"], 10, 5, 3, 3)
        self.assertBox(m["E-3"], 2, 10, 2, 1.25)
        self.assertEqual(m["E-1"]["name"], "펌프")               # 서식 있는 공유 문자열
        self.assertEqual(m["E-2"]["zone"], "NR")                 # inlineStr
        self.assertEqual(m["E-3"]["floor"], 1)
        self.assertNotIn("unplaced", m["E-1"])

    def test_engine_result_json(self):
        tk = I.import_path(os.path.join(EXAMPLES, "out", "GA_optimized.json"))
        with open(os.path.join(EXAMPLES, "out", "GA_optimized.json"), encoding="utf-8") as fp:
            ref = json.load(fp)
        m = by_id(tk)
        self.assertEqual(len(m), len(ref["blocks"]))
        for b in ref["blocks"]:
            it = m[b["id"]]
            self.assertBox(it, b["x"], b["y"], b["w"], b["d"])
            self.assertEqual(it["floor"], b["floor"])
            self.assertEqual(it["clearance"], b["clearance"])
            self.assertEqual(it["name"], b["name"])
        self.assertEqual(len(tk["boundary"]), 3)

    def test_ir_json(self):
        tk = I.import_path(os.path.join(EXAMPLES, "spec_smr_aux.json"))
        self.assertFalse(tk["stats"]["has_positions"])
        self.assertTrue(all(it.get("unplaced") for it in tk["items"]))


class TestCommon(_Base):

    def test_determinism(self):
        cases = [("plant_mm.dxf", {}), ("plant.svg", {"scale_ratio": 100}), ("plant.ifc", {}),
                 ("equipment.csv", {}), ("equipment.xlsx", {"units": "m"})]
        for name, o in cases:
            a = json.dumps(load(name, o), sort_keys=True, ensure_ascii=False)
            b = json.dumps(load(name, dict(o)), sort_keys=True, ensure_ascii=False)
            self.assertEqual(a, b, name)

    def test_missing_scale(self):
        with open(os.path.join(FIX, "plant_mm.dxf"), "rb") as fp:
            dxf = fp.read().replace(b"$INSUNITS\n70\n4", b"$INSUNITS\n70\n0")
        with self.assertRaises(ImportErrorUser) as cm:
            I.import_file(dxf, "x.dxf", {})
        self.assertIn("축척", str(cm.exception))
        tk = I.import_file(dxf, "x.dxf", {"units": "cm"})
        self.assertBox(by_id(tk)["P-101"], 30, 30, 20, 10)

    def test_rejections(self):
        with self.assertRaises(ImportErrorUser) as cm:
            I.import_file(b"AC1032\x00\x00binary", "plant.dwg")
        self.assertIn("DXF", str(cm.exception))
        with self.assertRaises(ImportErrorUser) as cm:
            I.import_file(b"\xff\xd8\xff\xe0JFIF", "plan.jpg")
        self.assertIn("PNG", str(cm.exception))

    def test_large_unclassified_warns(self):
        dxf = (b"0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n6\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n"
               b"0\nLWPOLYLINE\n8\nA\n90\n4\n70\n1\n10\n0\n20\n0\n10\n50\n20\n0\n10\n50\n20\n40\n10\n0\n20\n40\n"
               b"0\nLWPOLYLINE\n8\nB\n90\n4\n70\n1\n10\n1\n20\n1\n10\n3\n20\n1\n10\n3\n20\n2\n10\n1\n20\n2\n"
               b"0\nENDSEC\n0\nEOF\n")
        tk = I.import_file(dxf, "a.dxf", {})
        self.assertEqual(len(tk["items"]), 2)                    # 분류는 바꾸지 않는다
        self.assertTrue(any("layer_map" in w and "boundary" in w for w in tk["warnings"]))


if __name__ == "__main__":
    unittest.main()
