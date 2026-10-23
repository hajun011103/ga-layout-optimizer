# -*- coding: utf-8 -*-
"""
test_constraints.py — 제약 라이브러리 10종. 각각 '위반하는 배치' 와 '만족하는 배치'
두 고정 픽스처로 확인한다.

한쪽만 시험하면 조용한 실패(항상 0 을 반환하는 제약)를 못 잡는다.
LLM 파이프라인은 유창하게 실패하므로 양성/음성 양쪽이 필요하다.
"""
import unittest

from _util import CONS, G, B, ctx, spec, viol_ids


def ev(cid, c, params):
    return CONS.REGISTRY[cid].evaluate(c, params)


def amt(vs, cid=None):
    return sum(v.amount for v in vs if cid is None or v.cid == cid)


# ---------------------------------------------------------------------------

class TestFire20ft(unittest.TestCase):
    P = dict(groups=[["success_path:SP-A"], ["success_path:SP-B"]],
             min_horizontal_m=6.096, combustible_free=True,
             detection_and_suppression=True)

    def _c(self, gapx):
        sp, _ = spec([B("A", 4, 4, success_path="SP-A"),
                      B("Bb", 4, 4, success_path="SP-B")])
        return ctx(sp, {"A": (0, 0, 0), "Bb": (4 + gapx, 0, 0)})

    def test_violates_when_closer_than_20ft(self):
        vs = ev("FIRE-20FT", self._c(2.0), self.P)
        self.assertEqual(len(vs), 1)
        self.assertAlmostEqual(vs[0].amount, 6.096 - 2.0, places=6)
        self.assertTrue(vs[0].hard)

    def test_ok_when_beyond_20ft(self):
        self.assertEqual(ev("FIRE-20FT", self._c(7.0), self.P), [])

    def test_distance_exception_needs_declarations(self):
        """가연물 0 / 감지·소화를 선언하지 않으면 거리만으로 면제되지 않는다."""
        p = dict(self.P, combustible_free=False)
        vs = ev("FIRE-20FT", self._c(30.0), p)
        self.assertEqual(len(vs), 1)
        self.assertIn("거리 예외 불성립", vs[0].detail)

    def test_barrier_pair_is_exempt(self):
        p = dict(self.P, barrier_pairs=[["A", "Bb"]])
        self.assertEqual(ev("FIRE-20FT", self._c(0.5), p), [])

    def test_required_params_enforced(self):
        with self.assertRaises(CONS.MissingParam):
            ev("FIRE-20FT", self._c(2.0), dict(self.P, combustible_free=None))


class TestFireHatch(unittest.TestCase):
    """★ 해치가 화재구역 분할을 무너뜨리는지 — 프로토타입이 전혀 못 잡던 항목."""

    P = dict(openings=["H"], separation_m=6.096, combustible_free=True,
             both_levels_protected=True, merge_tolerance_m=0.0,
             penalty_m_per_conflict=25.0)
    F20 = dict(id="FIRE-20FT",
               params=dict(groups=[["success_path:SP-A"], ["success_path:SP-B"]],
                           min_horizontal_m=6.096, combustible_free=True,
                           detection_and_suppression=True))

    def _c(self, hatch_x, b_x, hatch_w=4.0):
        sp, _ = spec([B("A", 4, 4, fire_zone="FZ-A", success_path="SP-A"),
                      B("Bb", 4, 4, fire_zone="FZ-B", success_path="SP-B"),
                      B("H", hatch_w, 4, fire_zone="FZ-H", opening=True)],
                     constraints=[self.F20, dict(id="FIRE-HATCH", params=self.P)])
        return ctx(sp, {"A": (0, 0, 0), "H": (hatch_x, 0, 0), "Bb": (b_x, 0, 0)})

    def test_hatch_touching_both_zones_merges_them(self):
        # A[0..4] H[4..8] B[8..12] — 해치가 양쪽에 접한다
        vs = ev("FIRE-HATCH", self._c(4.0, 8.0), self.P)
        self.assertEqual(len(vs), 1)
        self.assertAlmostEqual(vs[0].amount, 25.0)
        self.assertIn("병합", vs[0].detail)

    def test_no_merge_when_hatch_isolated(self):
        # 해치를 양쪽에서 떼어 놓으면 병합이 일어나지 않는다
        vs = ev("FIRE-HATCH", self._c(20.0, 8.0), self.P)
        self.assertEqual(vs, [])

    def test_only_zones_adjacent_to_the_opening_merge(self):
        """개구부에 접하지 않은 구역까지 싸잡아 병합하지는 않는다."""
        # A[0..4] H[4..8] B[11..15] — B 는 해치에 접하지 않는다
        self.assertEqual(ev("FIRE-HATCH", self._c(4.0, 11.0), self.P), [])

    def test_rg1189_exception_needs_all_three_conditions(self):
        """
        해치가 양쪽에 접해 병합되더라도, 성공경로 간 6.1 m + 가연물 0 +
        양쪽 층 감지·소화를 모두 만족하면 분리로 인정한다.
        하나라도 빠지면 예외는 성립하지 않는다.
        """
        # 넓은 해치 H[4..12] 에 A[0..4] 와 B[12..16] 이 양쪽에서 접한다.
        # A-B 간극 8 m ≥ 6.096 → 세 조건을 다 만족하면 분리로 인정
        self.assertEqual(ev("FIRE-HATCH", self._c(4.0, 12.0, hatch_w=8.0), self.P), [])
        # 가연물 0 을 선언하지 않으면 예외가 사라진다
        p = dict(self.P, combustible_free=False)
        self.assertEqual(len(ev("FIRE-HATCH", self._c(4.0, 12.0, hatch_w=8.0), p)), 1)
        # 양쪽 층 감지·소화를 선언하지 않아도 마찬가지다
        p = dict(self.P, both_levels_protected=False)
        self.assertEqual(len(ev("FIRE-HATCH", self._c(4.0, 12.0, hatch_w=8.0), p)), 1)
        # 거리가 6.096 m 미만이면 세 조건 중 거리가 깨져 예외 불성립
        # A[0..4] H[4..12] B[12..16] 에서 해치를 좁히면 A-B 가 가까워진다
        self.assertEqual(len(ev("FIRE-HATCH", self._c(4.0, 8.0), self.P)), 1)


class TestTurbine25(unittest.TestCase):
    P = dict(turbine_block="T", protected=["P"], half_angle_deg=25.0)

    def _c(self, px, py, az=0.0):
        sp, _ = spec([B("T", 6, 6, turbine=True), B("P", 4, 4)])
        return ctx(sp, {"T": (0, 0, 0), "P": (px, py, 0)}, azim={"T": az})

    def test_inside_sector_violates(self):
        vs = ev("TURBINE-25", self._c(40, 0), self.P)     # 휠 평면 축 위
        self.assertEqual(len(vs), 1)
        self.assertGreater(vs[0].amount, 0.0)

    def test_outside_sector_ok(self):
        self.assertEqual(ev("TURBINE-25", self._c(0, 60), self.P), [])

    def test_azimuth_is_a_decision_variable(self):
        """방위각을 90° 돌리면 같은 배치가 위반이 되기도 하고 아니기도 하다."""
        self.assertEqual(ev("TURBINE-25", self._c(0, 60, az=0.0), self.P), [])
        self.assertEqual(len(ev("TURBINE-25", self._c(0, 60, az=90.0), self.P)), 1)

    def test_other_units_strike_zone_overlaps(self):
        """장래 호기 포함 전 호기의 strike zone 을 중첩해 본다."""
        p = dict(self.P, other_units=[dict(id="TB-U2", x=0.0, y=-50.0,
                                           azimuth_deg=90.0)])
        vs = ev("TURBINE-25", self._c(0, 60), p)
        self.assertEqual([v.blocks[1] for v in vs], ["TB-U2"])


class TestSeismicGap(unittest.TestCase):
    def _c(self, gapx):
        sp, _ = spec([B("A", 10, 10, structure="AUX"),
                      B("Bb", 10, 10, structure="DG")])
        return ctx(sp, {"A": (0, 0, 0), "Bb": (10 + gapx, 0, 0)})

    P = dict(structures=[dict(id="AUX", blocks=["structure:AUX"], delta_max_m=0.06),
                         dict(id="DG", blocks=["structure:DG"], delta_max_m=0.04)],
             construction_tolerance_m=0.05)

    def test_violates_performance_formula(self):
        vs = ev("SEISMIC-GAP", self._c(0.10), self.P)     # 요구 0.15
        self.assertEqual(len(vs), 1)
        self.assertAlmostEqual(vs[0].amount, 0.05, places=6)

    def test_ok_when_gap_sufficient(self):
        self.assertEqual(ev("SEISMIC-GAP", self._c(0.20), self.P), [])

    def test_delta_must_be_supplied(self):
        p = dict(self.P, structures=[dict(id="AUX", blocks=["structure:AUX"],
                                          delta_max_m=None),
                                     dict(id="DG", blocks=["structure:DG"],
                                          delta_max_m=0.04)])
        with self.assertRaises(CONS.MissingParam):
            ev("SEISMIC-GAP", self._c(0.10), p)


class TestHelb(unittest.TestCase):
    def _c(self, gapx):
        sp, _ = spec([B("MS", 8, 4), B("MCR", 10, 6)])
        return ctx(sp, {"MS": (0, 0, 0), "MCR": (8 + gapx, 0, 0)})

    P = dict(sources=["MS"], targets=["MCR"], min_dist_m=12.0)

    def test_too_close_violates(self):
        vs = ev("HELB-DIST", self._c(3.0), self.P)
        self.assertEqual(len(vs), 1)
        self.assertAlmostEqual(vs[0].amount, 9.0, places=6)

    def test_far_enough_ok(self):
        self.assertEqual(ev("HELB-DIST", self._c(15.0), self.P), [])

    def test_jet_cone(self):
        p = dict(self.P, min_dist_m=1.0,
                 sources=[dict(block="MS", jet_azimuth_deg=0.0)],
                 jet_half_angle_deg=10.0)
        vs = ev("HELB-DIST", self._c(15.0), p)
        self.assertEqual(len(vs), 1)
        self.assertIn("제트 확산 원뿔", vs[0].detail)

    def test_min_dist_has_no_default(self):
        with self.assertRaises(CONS.MissingParam):
            ev("HELB-DIST", self._c(3.0), dict(sources=["MS"], targets=["MCR"]))


class TestTallEquip(unittest.TestCase):
    def _c(self, dx, dy):
        sp, _ = spec([B("T", 6, 6, h=9.0)], n_floors=2, floor_heights=[5.0, 5.0])
        c = ctx(sp, {"T": (10, 10, 0)}, shadows={("T", 1): (10 + dx, 10 + dy)})
        c.tall_span = {0: 2}
        return c

    def test_misalignment_violates(self):
        vs = ev("TALL-EQUIP", self._c(0.1, 0.0), dict(align_tolerance_m=0.0))
        self.assertEqual(len(vs), 1)
        self.assertAlmostEqual(vs[0].amount, 0.1, places=6)

    def test_aligned_ok(self):
        self.assertEqual(ev("TALL-EQUIP", self._c(0.0, 0.0),
                            dict(align_tolerance_m=0.0)), [])

    def test_penetrating_the_roof_is_reported(self):
        sp, _ = spec([B("T", 6, 6, h=14.0)], n_floors=2, floor_heights=[5.0, 5.0])
        c = ctx(sp, {"T": (0, 0, 0)})
        c.tall_span = {0: 3}
        vs = ev("TALL-EQUIP", c, dict())
        self.assertEqual(len(vs), 1)
        self.assertIn("최상층을 벗어난다", vs[0].detail)


class TestFreeArea(unittest.TestCase):
    def _c(self, w):
        sp, _ = spec([B("A", w, 10)], n_floors=1)
        return ctx(sp, {"A": (0, 0, 0)}, env=(20.0, 20.0, 5.0))

    P = dict(ground_floor_free_ratio=0.5, top_floor_free_ratio=0.5)

    def test_over_occupied_violates(self):
        vs = ev("FREE-AREA", self._c(18), self.P)     # 180 m² > 허용 200? -> ok
        self.assertEqual(vs, [])
        vs = ev("FREE-AREA", self._c(20.0), dict(self.P,
                                                 ground_floor_free_ratio=0.9,
                                                 top_floor_free_ratio=0.9))
        self.assertTrue(vs)
        self.assertEqual(vs[0].unit, "m2")

    def test_ratios_have_no_default(self):
        with self.assertRaises(CONS.MissingParam):
            ev("FREE-AREA", self._c(18), dict())


class TestLoadPath(unittest.TestCase):
    def _c(self, b_train):
        sp, _ = spec([B("H", 4, 4), B("SRW", 4, 4),
                      B("A", 4, 4, train="TrainA"),
                      B("Bb", 4, 4, train=b_train)])
        # H(0,0) -> SRW(40,0) 사이 복도에 A 와 B 가 놓인다
        return ctx(sp, {"H": (0, 0, 0), "SRW": (40, 0, 0),
                        "A": (12, 0, 0), "Bb": (24, 0, 0)})

    P = dict(paths=[dict(id="p1", **{"from": "H", "to": "SRW"}, width_m=4.0)],
             redundant_groups=[["TrainA", "TrainB"]])

    def test_two_redundant_trains_on_one_path_violates(self):
        vs = ev("LOAD-PATH", self._c("TrainB"), self.P)
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0].unit, "m2")

    def test_single_train_ok(self):
        self.assertEqual(ev("LOAD-PATH", self._c("TrainA"), self.P), [])

    def test_width_required(self):
        p = dict(self.P, paths=[dict(id="p1", **{"from": "H", "to": "SRW"})])
        with self.assertRaises(CONS.MissingParam):
            ev("LOAD-PATH", self._c("TrainB"), p)


class TestVitalArea(unittest.TestCase):
    def _c(self, areas):
        blocks = [B(f"V{k}", 4, 4, vital_area=a) for k, a in enumerate(areas)]
        sp, _ = spec(blocks)
        return ctx(sp, {f"V{k}": (k * 10, 0, 0) for k in range(len(areas))})

    def test_too_many_areas_violates(self):
        vs = ev("VITAL-AREA", self._c(["VA-1", "VA-2", "VA-3"]),
                dict(vital_blocks=["vital_area:VA-1", "vital_area:VA-2",
                                   "vital_area:VA-3"], max_areas=2))
        self.assertEqual(len(vs), 1)
        self.assertIn("집약 실패", vs[0].detail)

    def test_within_limit_ok(self):
        self.assertEqual(ev("VITAL-AREA", self._c(["VA-1", "VA-1"]),
                            dict(vital_blocks=["vital_area:VA-1"], max_areas=2)), [])

    def test_unassigned_vital_equipment_is_reported(self):
        sp, _ = spec([B("V", 4, 4, zone="SAFETY")])
        c = ctx(sp, {"V": (0, 0, 0)})
        vs = ev("VITAL-AREA", c, dict(vital_blocks=["zone:SAFETY"], max_areas=2))
        self.assertEqual(len(vs), 1)
        self.assertIn("vital_area 미지정", vs[0].detail)


class TestElecSep(unittest.TestCase):
    def _c(self, gapx):
        sp, _ = spec([B("A", 4, 4, train="TrainA"), B("Bb", 4, 4, train="TrainB")])
        return ctx(sp, {"A": (0, 0, 0), "Bb": (4 + gapx, 0, 0)})

    P = dict(groups=[["train:TrainA"], ["train:TrainB"]], min_horizontal_m=15.0,
             citation="APR1400 실적 — 주변압기–UAT–SAT 15 m 또는 3시간 내화방벽")

    def test_violates(self):
        vs = ev("ELEC-SEP", self._c(5.0), self.P)
        self.assertEqual(len(vs), 1)
        self.assertAlmostEqual(vs[0].amount, 10.0, places=6)

    def test_ok(self):
        self.assertEqual(ev("ELEC-SEP", self._c(20.0), self.P), [])

    def test_citation_is_mandatory(self):
        with self.assertRaises(CONS.MissingParam):
            ev("ELEC-SEP", self._c(5.0), dict(self.P, citation=None))

    def test_citation_must_reference_ieee384_or_precedent(self):
        with self.assertRaises(CONS.MissingParam):
            ev("ELEC-SEP", self._c(5.0), dict(self.P, citation="어디선가 본 값"))


# ---------------------------------------------------------------------------

class TestForbiddenValues(unittest.TestCase):
    """references/regulatory-numbers.md 의 사용금지 5건이 코드에서도 막히는가."""

    def test_3ft_electrical_separation_blocked(self):
        e, w = CONS.scan_forbidden(dict(constraints=[
            dict(id="ELEC-SEP", params=dict(min_horizontal_m=0.9144,
                                            citation="IEEE 384-1981"))]))
        self.assertTrue(any("ELEC-3FT-5FT" in m for m in e))

    def test_5ft_value_blocked(self):
        e, _ = CONS.scan_forbidden(dict(constraints=[
            dict(id="ELEC-SEP", params=dict(min_horizontal_m=1.524,
                                            citation="IEEE 384-1981"))]))
        self.assertTrue(any("1.524" in m for m in e))

    def test_missing_citation_blocked(self):
        e, _ = CONS.scan_forbidden(dict(constraints=[
            dict(id="ELEC-SEP", params=dict(min_horizontal_m=15.0))]))
        self.assertTrue(any("citation" in m for m in e))

    def test_fixed_seismic_gap_blocked(self):
        e, _ = CONS.scan_forbidden(dict(constraints=[
            dict(id="SEISMIC-GAP", params=dict(min_gap_m=0.05))]))
        self.assertTrue(any("SEISMIC-FIXED" in m for m in e))

    def test_sgi_values_blocked(self):
        e, _ = CONS.scan_forbidden(dict(constraints=[
            dict(id="VITAL-AREA", params=dict(vehicle_barrier_standoff_m=30.0))]))
        self.assertTrue(any("SGI" in m for m in e))

    def test_isolation_zone_20ft_blocked(self):
        e, _ = CONS.scan_forbidden(dict(constraints=[
            dict(id="VITAL-AREA", params=dict(isolation_zone_width_m=6.096))]))
        self.assertTrue(any("ISOLATION-20FT" in m for m in e))

    def test_radiation_zone_numbering_warned(self):
        _, w = CONS.scan_forbidden(dict(blocks=[dict(id="X", zone="Zone III")]))
        self.assertTrue(any("ZONE-NUMBERING" in m for m in w))

    def test_clean_ir_is_silent(self):
        e, w = CONS.scan_forbidden(dict(
            blocks=[dict(id="X", zone="HRA-1mSvh")],
            constraints=[dict(id="ELEC-SEP",
                              params=dict(min_horizontal_m=15.0,
                                          citation="IEEE Std 384-2008 §5.6"))]))
        self.assertEqual(e, [])
        self.assertEqual(w, [])


class TestRegistryIntegrity(unittest.TestCase):
    def test_all_nine_new_nuclear_constraints_exist(self):
        for cid in ("FIRE-20FT", "FIRE-HATCH", "TURBINE-25", "SEISMIC-GAP",
                    "HELB-DIST", "TALL-EQUIP", "FREE-AREA", "LOAD-PATH",
                    "VITAL-AREA", "ELEC-SEP"):
            self.assertIn(cid, CONS.REGISTRY)

    def test_every_constraint_documents_its_basis(self):
        for cid, cd in CONS.REGISTRY.items():
            self.assertTrue(cd.basis and len(cd.basis) > 30,
                            f"{cid}: 근거 조항이 비어 있다")
            self.assertTrue(cd.formula, f"{cid}: 수식 요약이 비어 있다")
            self.assertTrue(cd.params, f"{cid}: 파라미터 정의가 비어 있다")

    def test_unknown_constraint_id_is_rejected(self):
        sp, _ = spec([B("A")], constraints=[dict(id="MADE-UP")])
        c = ctx(sp, {"A": (0, 0, 0)})
        with self.assertRaises(KeyError):
            CONS.evaluate_all(c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
