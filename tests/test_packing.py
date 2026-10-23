# -*- coding: utf-8 -*-
"""
test_packing.py — Sequence-Pair 표현의 불변식.

핵심 주장: 이 표현은 겹치는 배치를 '생성할 수 없다'. 중첩 페널티가 필요 없고
탐색공간 전체가 실현가능하다는 성능상의 근거가 여기에 있으므로, 이 성질이
깨지지 않았는지 기계적으로 확인한다.
"""
import random
import unittest

from _util import G, spec, B
from geometry import overlap_area


def overlaps(dims, x, y):
    n = len(dims)
    worst = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            worst = max(worst, overlap_area((x[i], y[i], dims[i][0], dims[i][1]),
                                            (x[j], y[j], dims[j][0], dims[j][1])))
    return worst


class TestSequencePair(unittest.TestCase):

    def test_no_overlap_random(self):
        """무작위 순열 500개에서 중첩 면적이 항상 0."""
        rng = random.Random(20260826)
        for trial in range(500):
            n = rng.randrange(2, 12)
            dims = [(rng.uniform(1, 9), rng.uniform(1, 9)) for _ in range(n)]
            gp = list(range(n)); rng.shuffle(gp)
            gm = list(range(n)); rng.shuffle(gm)
            x, y, W, D = G.pack_sequence_pair(dims, gp, gm)
            self.assertLess(overlaps(dims, x, y), 1e-9,
                            f"trial {trial}: 중첩 발생 — 표현 불변식 붕괴")
            self.assertGreaterEqual(W, max(d[0] for d in dims) - 1e-9)
            self.assertGreaterEqual(D, max(d[1] for d in dims) - 1e-9)

    def test_all_inside_envelope(self):
        rng = random.Random(7)
        for _ in range(100):
            n = rng.randrange(2, 10)
            dims = [(rng.uniform(1, 6), rng.uniform(1, 6)) for _ in range(n)]
            gp = list(range(n)); rng.shuffle(gp)
            gm = list(range(n)); rng.shuffle(gm)
            x, y, W, D = G.pack_sequence_pair(dims, gp, gm)
            for i in range(n):
                self.assertGreaterEqual(x[i], -1e-9)
                self.assertGreaterEqual(y[i], -1e-9)
                self.assertLessEqual(x[i] + dims[i][0], W + 1e-9)
                self.assertLessEqual(y[i] + dims[i][1], D + 1e-9)

    def test_known_two_block_relations(self):
        """Murata 1996 의 관계 규칙이 그대로 성립하는지."""
        dims = [(4.0, 2.0), (3.0, 5.0)]
        # 두 순열에서 0 이 1 보다 앞  ->  0 은 1 의 '왼쪽'
        x, y, W, D = G.pack_sequence_pair(dims, [0, 1], [0, 1])
        self.assertAlmostEqual(x[1], 4.0)
        self.assertAlmostEqual(y[1], 0.0)
        self.assertAlmostEqual(W, 7.0)
        # Γ⁺ 에서만 뒤  ->  0 은 1 의 '아래'
        x, y, W, D = G.pack_sequence_pair(dims, [1, 0], [0, 1])
        self.assertAlmostEqual(x[1], 0.0)
        self.assertAlmostEqual(y[1], 2.0)
        self.assertAlmostEqual(D, 7.0)

    def test_clearance_absorbed_into_footprint(self):
        """유지보수 이격은 블록 팽창으로 흡수된다 — 별도 페널티가 필요 없다."""
        sp, _ = spec([B("A", 4.0, 3.0, clearance=1.5)])
        self.assertEqual(sp.blocks[0].footprint(0), (7.0, 6.0))
        self.assertEqual(sp.blocks[0].footprint(1), (6.0, 7.0))

    def test_pack_is_deterministic(self):
        dims = [(3.0, 2.0), (1.0, 5.0), (4.0, 4.0), (2.0, 2.0)]
        a = G.pack_sequence_pair(dims, [2, 0, 3, 1], [1, 2, 0, 3])
        b = G.pack_sequence_pair(dims, [2, 0, 3, 1], [1, 2, 0, 3])
        self.assertEqual(a, b)


class TestTallEquipmentSpan(unittest.TestCase):

    def test_span_counts_floors_even_beyond_roof(self):
        """지붕을 뚫는 기기를 잘라내 '정상' 으로 만들면 안 된다."""
        sp, _ = spec([B("T", 5, 5, h=14.0)], n_floors=2, floor_heights=[5.0, 5.0])
        s = G.baseline(sp)
        self.assertEqual(s.span_of(0, 0), 3)          # 5+5+5 필요 → 3개 층
        self.assertEqual(s.floors_of(0), [0, 1])      # 실제 배치는 2개 층뿐

    def test_shadow_created_on_upper_floor(self):
        sp, _ = spec([B("T", 5, 5, h=8.0, floor=0), B("X", 4, 4, h=3.0, floor=1)],
                     n_floors=2, floor_heights=[5.0, 5.0])
        s = G.baseline(sp)
        geo, shadows = s.decode()
        self.assertIn((0, 1), shadows, "다층 관통 기기가 상위층에 그림자를 남겨야 한다")
        # 그림자는 상위층 패킹에 실제로 자리를 차지한다 → 중첩이 생길 수 없다
        self.assertLess(overlap_area(
            (shadows[(0, 1)]["x"], shadows[(0, 1)]["y"],
             shadows[(0, 1)]["w"], shadows[(0, 1)]["d"]),
            (geo[1]["x"], geo[1]["y"], geo[1]["w"], geo[1]["d"])), 1e-9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
