# -*- coding: utf-8 -*-
"""
test_drawing_pipeline.py — 도면 입력 경로와 MCP 서버의 불변식.

  · 고정 장애물(pinned)은 정확히 그 자리에 있고, 다른 블록은 절대 겹치지 않는다
  · 다층 관통부는 구조적으로 정렬된다 (TALL-EQUIP 잔차 0)
  · 도면 → IR 에서 좌표는 pinned 에만, 현황 좌표는 asis.json 에만 있다
  · LLM 경로(IR 생성·패치)로는 좌표를 넣을 수 없다
  · 동결되지 않은(또는 동결 후 바뀐) IR 로는 최적화하지 않는다
  · MCP stdio / HTTP 가 같은 도구를 같은 결과로 제공한다
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

from _util import G, ROOT, spec, B
from geometry import overlap_area

sys.path.insert(0, os.path.join(ROOT, "server"))
import asis as ASIS          # noqa: E402
import importers             # noqa: E402
import irhash                # noqa: E402
import takeoff as TAKEOFF    # noqa: E402
import tools as T            # noqa: E402

DRAW = os.path.join(ROOT, "examples", "drawings")
PY = sys.executable


def _minimal():
    with open(os.path.join(ROOT, "examples", "spec_minimal.json"), encoding="utf-8") as fp:
        return json.load(fp)


def _rects(geo):
    return [(i, (g["x"], g["y"], g["w"], g["d"]), g["f"]) for i, g in geo.items()]


class TestPinned(unittest.TestCase):

    def _spec(self):
        blocks = [B(f"E{k}", w=3 + k % 3, d=2 + k % 2, clearance=0.5) for k in range(10)]
        blocks.append(dict(id="CORE", name="core", w=4, d=4, pinned=True, kind="obstacle",
                           fixed_xy=[8.0, 6.0], floor=0, rotatable=False))
        blocks.append(dict(id="VOID", name="void", w=6, d=6, pinned=True, kind="void",
                           fixed_xy=[30.0, 30.0], floor=0, rotatable=False))
        return spec(blocks)[0]

    def test_pinned_exact_and_no_overlap(self):
        sp = self._spec()
        rng = random.Random(7)
        core = next(i for i, b in enumerate(sp.blocks) if b.id == "CORE")
        for trial in range(200):
            s = G.Solution(sp, rng)
            for _ in range(5):
                G._mutate(s, rng)
            geo, _ = s.decode()
            g = geo[core]
            self.assertAlmostEqual(g["x"] + g["w"] / 2, 8.0, 9)
            self.assertAlmostEqual(g["y"] + g["d"] / 2, 6.0, 9)
            rs = _rects(geo)
            for a in range(len(rs)):
                for b in range(a + 1, len(rs)):
                    self.assertLess(overlap_area(rs[a][1], rs[b][1]), 1e-9,
                                    f"trial {trial}: 고정 장애물과 겹침")

    def test_void_does_not_grow_envelope(self):
        sp = self._spec()
        s = G.Solution(sp, random.Random(1))
        s.decode()
        W, D, _ = s.env
        # VOID 는 (27..33) 에 있지만 외피를 거기까지 늘리지 않는다
        self.assertLess(min(W, D), 27.0 + 1e-9 + 40)   # 상한 확인용
        geo, _ = s.decode()
        others = max(g["x"] + g["w"] for i, g in geo.items() if sp.blocks[i].kind != "void")
        self.assertAlmostEqual(W, others, 9)

    def test_pinned_validated(self):
        import validate_spec
        ir = dict(project="t", blocks=[B("A"), dict(id="P", name="p", w=2, d=2, pinned=True)])
        r = validate_spec.check_ir(ir, probe=False)
        self.assertTrue(any("fixed_xy" in e for e in r.ERR))
        ir["blocks"][1].update(fixed_xy=[1, 1], floor=0)
        ir["blocks"].append(dict(id="Q", name="q", w=2, d=2, pinned=True, fixed_xy=[1.5, 1.5], floor=0))
        r = validate_spec.check_ir(ir, probe=False)
        self.assertTrue(any("겹친다" in e for e in r.ERR))


class TestThroughFloor(unittest.TestCase):

    def test_alignment_is_structural(self):
        """위층 관통부 좌표 = 기준층 좌표 (어떤 순열에서도)."""
        blocks = [B("TALL", w=5, d=5, h=12, floor=0, clearance=1.0)]
        blocks += [B(f"E{k}", w=3, d=2, clearance=0.5) for k in range(8)]
        sp = spec(blocks, n_floors=2, floor_heights=[6.0, 6.0])[0]
        rng = random.Random(3)
        for _ in range(100):
            s = G.Solution(sp, rng)
            for _ in range(8):
                G._mutate(s, rng)
            geo, sh = s.decode()
            self.assertIn((0, 1), sh)
            self.assertAlmostEqual(sh[(0, 1)]["x"], geo[0]["x"], 9)
            self.assertAlmostEqual(sh[(0, 1)]["y"], geo[0]["y"], 9)
            rs = [r for r in _rects(geo) if r[2] == 1] + [(0, (sh[(0, 1)]["x"], sh[(0, 1)]["y"],
                                                              sh[(0, 1)]["w"], sh[(0, 1)]["d"]), 1)]
            for a in range(len(rs)):
                for b in range(a + 1, len(rs)):
                    self.assertLess(overlap_area(rs[a][1], rs[b][1]), 1e-9)


class TestTakeoffToIR(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tk = importers.import_path(os.path.join(DRAW, "smr_aux_3f.dxf"), {})
        cls.ir, cls.asis, cls.qs = TAKEOFF.takeoff_to_ir([cls.tk], {"project": "t"})

    def test_counts_and_space(self):
        by = self.tk["stats"]["by_kind"]
        self.assertEqual(by["equipment"], 43)
        self.assertEqual(by["opening"], 1)
        self.assertEqual(by["obstacle"], 6)
        self.assertEqual(self.ir["n_floors"], 3)
        self.assertEqual(self.ir["preset"], "space_first")
        self.assertIsNotNone(self.ir["max_w"])
        self.assertEqual(self.ir["asis_sha256"], ASIS.asis_hash(self.asis))

    def test_coordinates_only_in_pinned(self):
        for b in self.ir["blocks"]:
            if b.get("pinned"):
                self.assertIn("fixed_xy", b)
            else:
                self.assertNotIn("fixed_xy", b)
                self.assertNotIn("x", b)
        moving = [b["id"] for b in self.ir["blocks"] if not b.get("pinned")]
        for bid in moving:
            self.assertIn(bid, self.asis["blocks"])

    def test_questions_ask_not_invent(self):
        ids = {q["id"] for q in self.qs}
        self.assertIn("floor_heights", ids)
        self.assertIn("clearance", ids)
        self.assertNotIn("floor_heights", self.ir)

    def test_corridors_split_keep_area(self):
        cor = [b for b in self.ir["blocks"] if b["id"].startswith("COR-11")]
        self.assertGreater(len(cor), 1)
        orig = next(it for it in self.tk["items"] if it["id"] == "COR-11")
        self.assertAlmostEqual(sum(b["w"] * b["d"] for b in cor), orig["w"] * orig["d"], 3)

    def test_asis_scored_same_objective(self):
        sp = G.spec_from_dict(self.ir)
        m1 = ASIS.metrics(sp, ASIS.AsIsSolution(sp, self.asis))
        m2 = ASIS.metrics(sp, ASIS.AsIsSolution(sp, self.asis))
        self.assertEqual(m1, m2)
        self.assertGreater(m1["footprint_m2"], 0)


class TestTools(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="smrga-")
        self.t = T.Tools(T.Workspace(self.dir, allow_local_paths=True, allow_url_fetch=False))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_llm_cannot_write_coordinates(self):
        ir = _minimal()
        bad = json.loads(json.dumps(ir)); bad["blocks"][0]["fixed_xy"] = [1, 2]
        with self.assertRaises(T.ToolError):
            self.t.ga_create_project_from_ir(ir=bad)
        pid = self.t.ga_create_project_from_ir(ir=ir)["project_id"]
        with self.assertRaises(T.ToolError):
            self.t.ga_update_ir(project_id=pid, reason="x",
                                patch={"blocks": [{"id": ir["blocks"][0]["id"], "x": 3}]})

    def test_freeze_required_and_invalidated_by_edit(self):
        ir = _minimal()
        pid = self.t.ga_create_project_from_ir(ir=ir)["project_id"]
        with self.assertRaises(T.ToolError):
            self.t.ga_optimize(project_id=pid)
        with self.assertRaises(T.ToolError):
            self.t.ga_freeze(project_id=pid, approved_by="")          # 승인자 필수
        self.t.ga_freeze(project_id=pid, approved_by="tester", acknowledged_warnings=True)
        self.t.ga_update_ir(project_id=pid, reason="층고 변경", patch={"set": {"aspect_max": 3.0}})
        with self.assertRaises(T.ToolError):
            self.t.ga_optimize(project_id=pid)                          # 편집 → 동결 해제

    def test_weights_merge(self):
        ir = _minimal()
        pid = self.t.ga_create_project_from_ir(ir=ir)["project_id"]
        before = dict(self.t.ga_get_ir(project_id=pid)["ir"].get("weights") or {})
        self.t.ga_update_ir(project_id=pid, reason="t", patch={"set": {"weights": {"ecc": 9.0}}})
        after = self.t.ga_get_ir(project_id=pid)["ir"]["weights"]
        for k, v in before.items():
            if k != "ecc":
                self.assertEqual(after[k], v)
        self.assertEqual(after["ecc"], 9.0)

    def test_drawing_to_result_deterministic(self):
        r = self.t.ga_load_example(name="smr_aux")
        pid = r["project"]["project_id"]
        self.t.ga_apply_example_requirements(project_id=pid, name="smr_aux")
        self.assertTrue(self.t.ga_validate(project_id=pid)["passed"])
        self.t.ga_freeze(project_id=pid, approved_by="tester", acknowledged_warnings=True)
        import pipeline
        d, ir = self.t._load_ir(pid)
        a = self.t.ws.rjson(os.path.join(d, "asis.json"))
        out1, out2 = os.path.join(self.dir, "o1"), os.path.join(self.dir, "o2")
        s1 = pipeline.optimize_ir(ir, out1, iters=300, restarts=1, asis=a)
        s2 = pipeline.optimize_ir(ir, out2, iters=300, restarts=1, asis=a)
        self.assertEqual(s1["result_sha256"], s2["result_sha256"])
        self.assertIsNotNone(s1["comparison"])
        self.assertTrue(os.path.exists(os.path.join(out1, "report.html")))
        self.assertTrue(os.path.exists(os.path.join(out1, "GA_asis_floor1.svg")))


class TestMCPTransports(unittest.TestCase):

    def test_stdio(self):
        d = tempfile.mkdtemp(prefix="smrga-")
        try:
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "0"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "ga_import_drawing",
                                "arguments": {"path": os.path.join(DRAW, "smr_aux_3f.dxf")}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "ga_import_drawing", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "nope"}]
            p = subprocess.run([PY, os.path.join(ROOT, "server", "mcp_server.py"), "--workspace", d],
                               input="\n".join(json.dumps(m) for m in msgs) + "\n",
                               capture_output=True, text=True, timeout=120)
            rs = {r["id"]: r for r in map(json.loads, p.stdout.strip().splitlines())}
            self.assertEqual(set(rs), {1, 2, 3, 4, 5})             # 알림에는 응답하지 않는다
            self.assertEqual(rs[1]["result"]["protocolVersion"], "2025-03-26")
            names = {t["name"] for t in rs[2]["result"]["tools"]}
            self.assertIn("ga_freeze", names)
            self.assertFalse(rs[3]["result"]["isError"])
            self.assertEqual(rs[3]["result"]["structuredContent"]["format"], "dxf")
            self.assertTrue(rs[4]["result"]["isError"])
            self.assertEqual(rs[5]["error"]["code"], -32601)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_http_upload_and_token(self):
        import mcp_server as M
        from http.server import ThreadingHTTPServer
        d = tempfile.mkdtemp(prefix="smrga-")
        ws = T.Workspace(d, allow_local_paths=False, allow_url_fetch=False,
                         public_url="http://127.0.0.1/t/sekret")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), M.make_handler(M.MCP(T.Tools(ws)), "sekret", True))
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
        base = f"http://127.0.0.1:{port}"

        def post(path, data, headers=None):
            req = urllib.request.Request(base + path, data=data, method="POST",
                                         headers=dict({"Content-Type": "application/json"}, **(headers or {})))
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
            self.assertEqual(post("/mcp", body)[0], 401)                    # 토큰 없음
            code, _ = post("/t/sekret/mcp", body)
            self.assertEqual(code, 200)
            with open(os.path.join(DRAW, "fpso_topside.svg"), "rb") as fp:
                code, raw = post("/t/sekret/upload", fp.read(), {"X-Filename": "fpso.svg"})
            fid = json.loads(raw)["file_id"]
            call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "ga_import_drawing", "arguments": {"file_id": fid}}}
            code, raw = post("/mcp", json.dumps(call).encode(), {"Authorization": "Bearer sekret"})
            res = json.loads(raw)["result"]
            self.assertFalse(res["isError"], res)
            self.assertEqual(res["structuredContent"]["format"], "svg")
            # 원격 모드: 로컬 경로 읽기 금지
            call["params"]["arguments"] = {"path": "/etc/passwd"}
            code, raw = post("/t/sekret/mcp", json.dumps(call).encode())
            self.assertTrue(json.loads(raw)["result"]["isError"])
        finally:
            srv.shutdown()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
