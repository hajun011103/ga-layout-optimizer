# -*- coding: utf-8 -*-
"""
test_function.py — 면적을 줄여도 각 공간이 '역할' 을 하는지 확인하는 제약과 규모 계획.

  · CIRC-ACCESS : 복도에서 들어갈 수 없는 실을 잡는다 (via_rooms 개 실을 거치는 것은 허용)
  · CIRC-CONNECT: 끊긴 복도를 잡는다
  · EGRESS      : 계단까지 거리·계단 수·계단 간 이격
  · RACK-SERVE  : 배관대에서 떨어진 설비, 끊긴 배관대
  · optional    : 솔버가 복도 구간을 없앨 수 있고, 없애도 위 제약은 지켜진다
  · program     : 근거 없는 규칙 거부, 실 축소·확대, 수직 코어 묶기, 구간 나누기
"""
import copy
import random
import unittest

from _util import G, CONS, spec, ctx, B
import program as PROGRAM


def _room(bid, w, d, **kw):
    return B(bid, w=w, d=d, kind="room", rotatable=False, **kw)


def _corr(bid, w, d, **kw):
    return B(bid, w=w, d=d, kind="room", rotatable=False, circulation="corridor", **kw)


def _stair(bid, **kw):
    return B(bid, w=3.0, d=6.0, kind="room", rotatable=False, circulation="stair", **kw)


def _viols(sp, placements, cid):
    c = ctx(sp, placements)
    return [v for v in CONS.REGISTRY[cid].evaluate(c, c.params_of(cid)) if v.cid == cid]


class TestAccess(unittest.TestCase):

    def _sp(self, via=0):
        return spec([_corr("C1", 20.0, 2.0), _room("R1", 4, 4), _room("R2", 4, 4), _room("R3", 4, 4)],
                    constraints=[{"id": "CIRC-ACCESS", "params": {"door_m": 0.9, "via_rooms": via,
                                                                  "touch_tol_m": 0.2}}])[0]

    def test_room_on_corridor_ok_and_detached_room_caught(self):
        sp = self._sp()
        ok = {"C1": (0, 0, 0), "R1": (0, 2, 0), "R2": (5, 2, 0), "R3": (10, 2, 0)}
        self.assertEqual(_viols(sp, ok, "CIRC-ACCESS"), [])
        bad = dict(ok, R3=(10, 12, 0))
        v = _viols(sp, bad, "CIRC-ACCESS")
        self.assertEqual([x.blocks for x in v], [["R3"]])

    def test_via_rooms(self):
        # R3 은 R2 위에 붙어 있다 — 복도에는 직접 닿지 않는다
        pl = {"C1": (0, 0, 0), "R1": (0, 2, 0), "R2": (5, 2, 0), "R3": (5, 6, 0)}
        self.assertEqual(len(_viols(self._sp(0), pl, "CIRC-ACCESS")), 1)
        self.assertEqual(_viols(self._sp(1), pl, "CIRC-ACCESS"), [])

    def test_touch_shorter_than_door_is_not_access(self):
        sp = self._sp()
        pl = {"C1": (0, 0, 0), "R1": (0, 2, 0), "R2": (5, 2, 0), "R3": (19.5, 2, 0)}   # 0.5 m 만 겹침
        self.assertEqual([x.blocks for x in _viols(sp, pl, "CIRC-ACCESS")], [["R3"]])


class TestReviewFindings(unittest.TestCase):
    """독립 검증(2026-11-01)에서 나온 문제의 회귀 시험."""

    def test_cores_stack_at_exact_half_overlap(self):
        ir = {"blocks": [{"id": "C1", "name": "STAIR", "w": 6.0, "d": 6.0, "kind": "room"},
                         {"id": "C2", "name": "STAIR", "w": 6.0, "d": 6.0, "kind": "room"}],
              "links": [], "separations": [], "floor_heights": [8.0, 8.0]}
        asis = {"blocks": {"C1": dict(x=0, y=14.5, w=6, d=6, floor=0), "C2": dict(x=0, y=11.5, w=6, d=6, floor=1)}}
        PROGRAM.apply_program(ir, {"stack_cores": True, "_asis": asis})
        self.assertEqual([b["id"] for b in ir["blocks"]], ["C1"], "정확히 절반 겹친 계단도 한 코어")

    def test_contact_measured_on_body_not_clearance(self):
        # 복도 x 0–20. 기기 본체 4×3, 이격 1.5 m (배치 좌표는 이격 포함 외곽의 좌하단).
        sp = spec([_corr("C1", 20.0, 2.0), B("P1", w=4.0, d=3.0, clearance=1.5, kind="equipment")],
                  constraints=[{"id": "CIRC-ACCESS", "params": {"door_m": 1.2, "via_rooms": 0, "touch_tol_m": 0.2,
                                                                "applies_to": ["equipment"]}}])[0]
        # 본체 x 6.5–10.5 가 복도 앞 1.5 m(이격 공간) 에 있다 → 출입 가능
        self.assertEqual(_viols(sp, {"C1": (0, 0, 0), "P1": (5, 2, 0)}, "CIRC-ACCESS"), [])
        # 본체 x 20.0–24.0 — 복도와 정면으로 겹치는 길이 0. 이격 외곽(18.5–25.5)으로 재면 1.5 m 겹쳐 통과했던 경우
        v = _viols(sp, {"C1": (0, 0, 0), "P1": (18.5, 2, 0)}, "CIRC-ACCESS")
        self.assertEqual([x.blocks for x in v], [["P1"]])

    def test_direct_access_room(self):
        sp = spec([_corr("C1", 20.0, 2.0), _room("CORE", 4, 4), _room("OR1", 4, 4, ),],
                  constraints=[{"id": "CIRC-ACCESS", "params": {"door_m": 0.9, "via_rooms": 1, "touch_tol_m": 0.2,
                                                                "direct": "^OR"}}])[0]
        pl = {"C1": (0, 0, 0), "CORE": (0, 2, 0), "OR1": (0, 6, 0)}      # OR1 은 CORE 를 거쳐야 함
        self.assertEqual([v.blocks for v in _viols(sp, pl, "CIRC-ACCESS")], [["OR1"]])

    def test_upper_stair_must_reach_ground(self):
        sp = spec([_stair("S1"), _stair("S2"), _room("R1", 4, 4)], n_floors=2, floor_heights=[4.0, 4.0],
                  constraints=[{"id": "EGRESS", "params": {"max_travel_m": 100.0, "min_exits": 1}}])[0]
        c = ctx(sp, {"S1": (0, 0, 0), "S2": (40, 0, 1), "R1": (10, 0, 1)})        # S2 는 2층에만 있다
        v = CONS.REGISTRY["EGRESS"].evaluate(c, c.params_of("EGRESS"))
        self.assertTrue(any("피난층" in x.detail for x in v))


class TestHospitalCirculation(unittest.TestCase):
    """병원 수술부에서 드러난 문제: 대기실을 복도로 치거나, 멸균 코어를 통로로 쓰거나, 청결/오염 복도가 섞임."""

    def test_waiting_room_is_not_a_corridor(self):
        self.assertIsNone(PROGRAM.circulation_of("PREOP 수술 전 대기", "room"))
        self.assertEqual(PROGRAM.circulation_of("외래 대기실", "room"), "lobby")

    def _sp(self, **params):
        base = {"door_m": 0.9, "via_rooms": 1, "touch_tol_m": 0.2}
        base.update(params)
        return spec([_corr("CN", 20.0, 2.0, name="CORR-N 청결복도"), _corr("CS", 20.0, 2.0, name="CORR-S 일반복도"),
                     _room("CORE", 4, 4, name="CORE 멸균"), _room("PACU", 4, 4, name="PACU 회복실"),
                     _room("SOIL", 4, 4, name="SOIL 오염")],
                    constraints=[{"id": "CIRC-ACCESS", "params": base}])[0]

    def test_no_passing_through_sterile_core(self):
        pl = {"CN": (0, 0, 0), "CS": (0, 30, 0), "CORE": (0, 2, 0), "PACU": (0, 6, 0), "SOIL": (10, 2, 0)}
        self.assertEqual(_viols(self._sp(), pl, "CIRC-ACCESS"), [])               # 멸균 코어를 거쳐 회복실 — 예전엔 통과
        v = _viols(self._sp(through_ok="SCRUB"), pl, "CIRC-ACCESS")
        self.assertEqual([x.blocks for x in v], [["PACU"]])

    def test_room_must_use_its_corridor(self):
        pl = {"CN": (0, 0, 0), "CS": (0, 30, 0), "CORE": (0, 2, 0), "PACU": (5, 26, 0), "SOIL": (10, 2, 0)}
        self.assertEqual(_viols(self._sp(), pl, "CIRC-ACCESS"), [])               # 오염실이 청결 복도에 — 예전엔 통과
        v = _viols(self._sp(corridor_for=[["SOIL", "CORR-S"]]), pl, "CIRC-ACCESS")
        self.assertEqual([x.blocks for x in v], [["SOIL"]])


class TestEntryRules(unittest.TestCase):
    """감사에서 드러난 문제: 장비실 문이 피난계단실로만 나거나, 화장실·창고를 거쳐 다른 실로 들어감."""

    def _sp(self, **params):
        base = {"door_m": 0.9, "via_rooms": 1, "touch_tol_m": 0.2}
        base.update(params)
        return spec([_corr("C1", 20.0, 2.0), _stair("S1"), _room("PUMP", 4, 4, name="PUMP ROOM"),
                     _room("WC", 4, 4, name="TOILET"), _room("LAB", 4, 4, name="LAB")],
                    constraints=[{"id": "CIRC-ACCESS", "params": base}])[0]

    def test_no_door_into_exit_stair(self):
        pl = {"C1": (0, 0, 0), "S1": (30, 0, 0), "PUMP": (33, 0, 0), "WC": (0, 2, 0), "LAB": (4, 2, 0)}
        self.assertEqual(_viols(self._sp(), pl, "CIRC-ACCESS"), [])        # 계단실로만 문 — 예전엔 통과
        v = _viols(self._sp(no_entry_types=["stair", "elevator"]), pl, "CIRC-ACCESS")
        self.assertEqual([x.blocks for x in v], [["PUMP"]])
        self.assertEqual(_viols(self._sp(no_entry_types=["stair"], entry_ok="PUMP"), pl, "CIRC-ACCESS"), [])

    def test_no_passing_through_toilet(self):
        pl = {"C1": (0, 0, 0), "S1": (30, 0, 0), "PUMP": (0, 2, 0), "WC": (8, 2, 0), "LAB": (8, 6, 0)}
        self.assertEqual(_viols(self._sp(), pl, "CIRC-ACCESS"), [])        # 화장실을 거쳐 실험실 — 예전엔 통과
        v = _viols(self._sp(no_through="TOILET"), pl, "CIRC-ACCESS")
        self.assertEqual([x.blocks for x in v], [["LAB"]])


class TestAdjacentDeadEnd(unittest.TestCase):

    def test_adjacent_must_and_must_not(self):
        rules = [{"a": "OR", "b": "CORE", "mode": "must", "min_touch_m": 1.0},
                 {"a": "SOIL", "b": "CORE", "mode": "must_not"}]
        sp = spec([_room("OR1", 6, 6, name="OR 1"), _room("CORE", 4, 6, name="CORE"), _room("SOIL", 3, 3, name="SOIL")],
                  constraints=[{"id": "ADJACENT", "params": {"rules": rules, "touch_tol_m": 0.2}}])[0]
        ok = {"OR1": (0, 0, 0), "CORE": (6, 0, 0), "SOIL": (20, 0, 0)}
        self.assertEqual(_viols(sp, ok, "ADJACENT"), [])
        bad = {"OR1": (0, 0, 0), "CORE": (12, 0, 0), "SOIL": (16, 0, 0)}
        self.assertEqual(sorted(tuple(v.blocks) for v in _viols(sp, bad, "ADJACENT")), [("OR1",), ("SOIL", "CORE")])

    def test_dead_end_corridor(self):
        sp = spec([_corr("C1", 10.0, 2.0), _corr("C2", 10.0, 2.0), _stair("S1")],
                  constraints=[{"id": "DEAD-END", "params": {"max_m": 15.0, "min_width_m": 1.1, "touch_tol_m": 0.2}}])[0]
        # 계단 - C1 - C2 일렬: 출구로 이어지므로 막다른 곳 아님
        self.assertEqual(_viols(sp, {"S1": (0, 0, 0), "C1": (3, 0, 0), "C2": (13, 0, 0)}, "DEAD-END"), [])
        # 계단이 C1 가운데 옆에 붙고(갈림), C2 가 끝에 20 m 로 이어짐 → C2 쪽 막다른 10 m(허용), 계단 반대편 없음
        sp2 = spec([_corr("C1", 10.0, 2.0), _corr("C2", 10.0, 2.0), _corr("C3", 10.0, 2.0), _stair("S1")],
                   constraints=[{"id": "DEAD-END", "params": {"max_m": 15.0, "min_width_m": 1.1, "touch_tol_m": 0.2}}])[0]
        v = _viols(sp2, {"S1": (0, 0, 0), "C1": (3, 0, 0), "C2": (13, 0, 0), "C3": (23, 0, 0)}, "DEAD-END")
        self.assertEqual(v, [])                                # 한 줄로 계단까지 — 막다른 곳 아님
        # 계단이 가운데 C2 옆에 붙음(갈림): 양쪽 C1·C3 가 각각 10 m 막다른 복도 — 허용 8 m 이면 둘 다 위반
        sp3 = spec([_corr("C1", 10.0, 2.0), _corr("C2", 10.0, 2.0), _corr("C3", 10.0, 2.0), _stair("S1")],
                   constraints=[{"id": "DEAD-END", "params": {"max_m": 8.0, "min_width_m": 1.1, "touch_tol_m": 0.2}}])[0]
        v = _viols(sp3, {"C1": (0, 0, 0), "C2": (10, 0, 0), "C3": (20, 0, 0), "S1": (13, 2, 0)}, "DEAD-END")
        self.assertEqual(sorted(x.blocks for x in v), [["C1"], ["C3"]])


class TestConnectEgress(unittest.TestCase):

    def test_broken_corridor(self):
        sp = spec([_corr("C1", 10, 2), _corr("C2", 10, 2), _stair("S1")],
                  constraints=[{"id": "CIRC-CONNECT", "params": {"min_width_m": 1.1, "touch_tol_m": 0.2}}])[0]
        ok = {"C1": (0, 0, 0), "C2": (10, 0, 0), "S1": (20, 0, 0)}
        self.assertEqual(_viols(sp, ok, "CIRC-CONNECT"), [])
        bad = {"C1": (0, 0, 0), "C2": (13, 0, 0), "S1": (23, 0, 0)}
        self.assertTrue(_viols(sp, bad, "CIRC-CONNECT"))

    def test_egress_distance_and_count(self):
        sp = spec([_stair("S1"), _stair("S2"), _room("R1", 4, 4)],
                  constraints=[{"id": "EGRESS", "params": {"max_travel_m": 30.0, "min_exits": 2,
                                                           "exit_separation_frac": 0.33}}])[0]
        self.assertEqual(_viols(sp, {"S1": (0, 0, 0), "S2": (40, 0, 0), "R1": (10, 0, 0)}, "EGRESS"), [])
        far = _viols(sp, {"S1": (0, 0, 0), "S2": (3, 0, 0), "R1": (60, 0, 0)}, "EGRESS")
        self.assertTrue(any("가장 가까운 계단" in v.detail for v in far))
        self.assertTrue(any("계단 사이 거리" in v.detail for v in far))


class TestRackServe(unittest.TestCase):

    def test_equipment_far_from_rack(self):
        sp = spec([B("P1", w=4, d=4, zone="PROCESS"),
                   B("RK1", w=12, d=6, kind="room", circulation="piperack"),
                   B("RK2", w=12, d=6, kind="room", circulation="piperack")],
                  constraints=[{"id": "RACK-SERVE", "params": {"served": "zone:PROCESS", "max_gap_m": 3.0}}])[0]
        self.assertEqual(_viols(sp, {"P1": (0, 6, 0), "RK1": (0, 0, 0), "RK2": (12, 0, 0)}, "RACK-SERVE"), [])
        v = _viols(sp, {"P1": (0, 20, 0), "RK1": (0, 0, 0), "RK2": (30, 0, 0)}, "RACK-SERVE")
        self.assertTrue(any("배관대에서" in x.detail for x in v))
        self.assertTrue(any("끊겼다" in x.detail for x in v))


class TestOptionalBlocks(unittest.TestCase):

    def test_solver_removes_spare_corridor_but_keeps_access(self):
        blocks = [_corr(f"C{k}", 8.0, 2.0, optional=True) for k in range(6)]
        blocks += [_room(f"R{k}", 4.0, 4.0) for k in range(4)]
        blocks += [_stair("S1")]
        cons = [{"id": "CIRC-ACCESS", "params": {"door_m": 0.9, "touch_tol_m": 0.2, "via_rooms": 0}},
                {"id": "CIRC-CONNECT", "params": {"min_width_m": 1.0, "touch_tol_m": 0.2}}]
        sp = spec(blocks, constraints=cons, preset="space_first",
                  weights={"constraint.CIRC-ACCESS": 5000.0, "constraint.CIRC-CONNECT": 5000.0})[0]
        best, F, _ = G.optimize(sp, iters=4000, restarts=2, seed=7, verbose=False)
        F2, t, geo, env, v, sh = best.evaluate(detail=True)
        hard = [x for x in v if x.hard and x.amount > 1e-6]
        self.assertEqual(hard, [], [x.detail for x in hard])
        self.assertGreater(len(best.off), 0, "남는 복도 구간을 하나도 없애지 않았다")
        self.assertTrue(all(sp.blocks[i].optional for i in best.off))

    def test_no_optional_keeps_move_set(self):
        sp = spec([_room("R1", 4, 4), _room("R2", 3, 3)])[0]
        a = G.Solution(sp, random.Random(1)); b = a.clone()
        G._mutate(a, random.Random(5)); G._mutate(b, random.Random(5))
        self.assertEqual((a.gp, a.gm, a.rot), (b.gp, b.gm, b.rot))
        self.assertEqual(sp._moves, G.MOVES)


class TestProgram(unittest.TestCase):

    def _ir(self):
        return {"blocks": [
            {"id": "E1", "name": "EXAM 1", "w": 5.0, "d": 4.0, "kind": "room"},
            {"id": "E2", "name": "EXAM 2", "w": 2.0, "d": 2.0, "kind": "room"},
            {"id": "O1", "name": "OPEN OFFICE", "w": 10.0, "d": 8.0, "kind": "room"},
            {"id": "C1", "name": "CORRIDOR", "w": 20.0, "d": 3.0, "kind": "room"},
            {"id": "S1", "name": "STAIR", "w": 3.0, "d": 6.0, "kind": "room"},
            {"id": "S2", "name": "STAIR", "w": 3.0, "d": 6.0, "kind": "room"},
            {"id": "PR", "name": "PIPE RACK", "w": 30.0, "d": 6.0, "kind": "obstacle", "pinned": True,
             "fixed_xy": [15.0, 23.0], "floor": 0},
        ], "links": [{"a": "S2", "b": "E1"}], "separations": [], "floor_heights": [4.0, 4.0]}

    def test_resize_stack_segment(self):
        ir = self._ir()
        asis = {"blocks": {"S1": dict(x=0, y=0, w=3, d=6, floor=0), "S2": dict(x=0.2, y=0, w=3, d=6, floor=1),
                           "PR": dict(x=0, y=20, w=30, d=6, floor=0)}}
        rep = PROGRAM.apply_program(ir, {
            "rooms": [{"type": "exam", "match": "EXAM", "target_area_m2": 11.0, "min_area_m2": 7.4,
                       "min_clear_dim_m": 2.4, "basis": "test"}],
            "corridor": {"width_m": 1.8, "basis": "test"},
            "stack_cores": True, "optional_corridors": True,
            "segment": [{"match": "^PR$", "length_m": 12.0}],
            "_asis": asis})
        b = {x["id"]: x for x in ir["blocks"]}
        self.assertAlmostEqual(b["E1"]["w"] * b["E1"]["d"], 11.0, places=2)      # 20 → 11 m²
        self.assertAlmostEqual(b["E2"]["w"] * b["E2"]["d"], 7.4, places=1)       # 4 → 7.4 m² (미달 → 키움)
        self.assertGreaterEqual(min(b["E2"]["w"], b["E2"]["d"]), 2.4 - 1e-9)
        self.assertEqual(b["O1"]["w"] * b["O1"]["d"], 80.0, "규칙 없는 실은 그대로")
        self.assertEqual(b["C1"]["d"], 1.8)
        self.assertTrue(b["C1"]["optional"])
        self.assertNotIn("S2", b, "위층 계단은 아래층 계단과 한 코어로 묶인다")
        self.assertEqual(ir["links"], [], "묶여 사라진 블록의 링크도 사라진다")
        self.assertGreater(b["S1"]["h"], 4.0)
        segs = [x for x in ir["blocks"] if x["id"].startswith("PR-S")]
        self.assertEqual(len(segs), 3)
        self.assertTrue(all(s["optional"] and s["circulation"] == "piperack" and not s.get("pinned") for s in segs))
        self.assertEqual(sorted(k for k in asis["blocks"] if k.startswith("PR")), ["PR-S1", "PR-S2", "PR-S3"])
        self.assertTrue(rep)

    def test_tools_reject_rule_without_basis(self):
        import os, sys, tempfile
        from _util import ROOT
        sys.path.insert(0, os.path.join(ROOT, "server"))
        import tools as T
        t = T.Tools(T.Workspace(tempfile.mkdtemp(prefix="smrga-"), allow_local_paths=True, allow_url_fetch=False))
        pid = t.ga_load_example(name="hospital_or")["project"]["project_id"]
        with self.assertRaises(T.ToolError):
            t.ga_update_ir(project_id=pid, reason="x", patch={"program": {"rooms": [
                {"type": "or", "match": "OR", "target_area_m2": 37.2}]}})

    def test_function_patch_keeps_earlier_constraints(self):
        """기능 요건 패치(set.constraints)가 앞서 넣은 원전 제약을 지우지 않는다 — id 기준 병합."""
        import json, os, sys, tempfile
        from _util import ROOT
        sys.path.insert(0, os.path.join(ROOT, "server"))
        import tools as T
        t = T.Tools(T.Workspace(tempfile.mkdtemp(prefix="smrga-"), allow_local_paths=True, allow_url_fetch=False))
        pid = t.ga_load_example(name="smr_aux")["project"]["project_id"]
        t.ga_apply_example_requirements(project_id=pid, name="smr_aux")
        ids = lambda: [c["id"] for c in t._load_ir(pid)[1].get("constraints") or []]
        before = ids()
        self.assertIn("FIRE-20FT", before)
        with open(os.path.join(ROOT, "examples", "function", "smr_aux.json"), encoding="utf-8") as fp:
            req = json.load(fp)
        t.ga_update_ir(project_id=pid, reason="x", patch={k: v for k, v in req.items() if not k.startswith("_")})
        after = ids()
        for cid in before:
            self.assertIn(cid, after)
        self.assertIn("CIRC-ACCESS", after)
        self.assertEqual(len(after), len(set(after)))
        # 같은 id 는 교체, 빼기는 remove_constraints 로만
        t.ga_update_ir(project_id=pid, reason="x", patch={"remove_constraints": ["FIRE-20FT"]})
        self.assertNotIn("FIRE-20FT", ids())
        with self.assertRaises(T.ToolError):
            t.ga_update_ir(project_id=pid, reason="x", patch={"remove_constraints": ["NO-SUCH"]})


if __name__ == "__main__":
    unittest.main()
