# -*- coding: utf-8 -*-
"""
test_benchmarks.py — 3단계 검증 계획.

  1단계 (즉시)  QAPLIB + 2DPackLib — 최적성 갭을 정량 보고할 수 있는 유일한 단계.
                여기서 못 맞추면 원전 문제로 갈 이유가 없다.
  2단계 (핵심)  Wrigley 2019 Shearon Harris 데이터셋 (CC BY 4.0) 재현,
                Ejeh 2019 부록 B 요소플랜트 8기기 MILP 정식화 대조.
  3단계        ABWR DCD Ch.1 표고별 실 폴리곤, AP1000 §12.3 방사선구역도.

  + 음성 대조군: 실현 불가능한 요구사항 집합에 '불가능' 이라고 답하는지.

★ 외부 데이터는 폐쇄망 특성상 수동으로 넣는다. 없으면 SKIP 하고 안내를 출력한다.
  값을 지어내서 '통과' 시키지 않는다 — 그것이 이 분야에서 가장 흔한 실패다.
"""
import glob
import json
import os
import subprocess
import sys
import unittest

from _util import DATA, ENGINE, EXAMPLES, G
import bench_qap
import validate_spec

QAPLIB = os.path.join(DATA, "qaplib")
PACK2D = os.path.join(DATA, "2dpacklib")
WRIGLEY = os.path.join(DATA, "wrigley2019")
EJEH = os.path.join(DATA, "ejeh2019")


def _has(d, pat="*"):
    return os.path.isdir(d) and bool(glob.glob(os.path.join(d, pat)))


# ---------------------------------------------------------------------------
# 1단계
# ---------------------------------------------------------------------------

class TestStage1Optimality(unittest.TestCase):

    def test_selftest_gap_is_zero_on_brute_forced_instances(self):
        """
        외부 데이터 없이도 최적성 갭을 보고할 수 있는 경로.
        n<=8 전수탐색이 '진짜 최적' 을 주므로 탐색부의 갭이 정확히 측정된다.
        """
        rows = bench_qap.selftest(seed=20260826, iters=20000, restarts=3)
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertLessEqual(r["gap_pct"], 0.0 + 1e-9,
                                 f"{r['instance']}: 갭 {r['gap_pct']}% — "
                                 f"탐색부가 소규모 인스턴스조차 못 맞춘다")

    def test_qaplib_instances(self):
        if not _has(QAPLIB, "*.dat"):
            self.skipTest(
                f"QAPLIB 데이터 없음. {QAPLIB} 에 .dat/.sln 을 넣을 것.\n"
                f"  출처: coral.ise.lehigh.edu/data-sets/qaplib/\n"
                f"  ※ 최적값은 .sln 파일에서 읽는다. 코드에 하드코딩하지 않는다.")
        rows = bench_qap.run_dir(QAPLIB, iters=50000, restarts=3, seed=20260826)
        self.assertTrue(rows, "「.dat 은 있는데 짝이 되는 .sln 이 없다」")
        worst = max(r["gap_pct"] for r in rows)
        print(f"\n  QAPLIB {len(rows)}건 · 최대 갭 {worst:.2f}%")
        # 회귀 감시용 상한. 넉넉히 잡되, 갭은 반드시 '보고' 된다.
        self.assertLess(worst, 15.0,
                        "QAPLIB 갭이 15% 를 넘는다 — 탐색부 회귀를 의심할 것")

    def test_2dpacklib_instances(self):
        if not _has(PACK2D):
            self.skipTest(
                f"2DPackLib 데이터 없음. {PACK2D} 에 인스턴스를 넣을 것.\n"
                f"  출처: site.unibo.it/operations-research/en/research/2dpacklib")
        self.fail("2DPackLib 로더 미구현 — 데이터를 넣었다면 로더를 먼저 붙일 것")


# ---------------------------------------------------------------------------
# 2단계
# ---------------------------------------------------------------------------

class TestStage2Reproduction(unittest.TestCase):

    def test_wrigley2019_shearon_harris(self):
        """
        발표된 결과(배관길이 −33.9~46.1%)를 재현.
        기대값은 데이터셋과 함께 넣는 expected.json 에서 읽는다 — 코드에 박지 않는다.
        ※ 통용되는 "베이스라인 목적함수 14,914"는 기술조사 보고서에 없는 값이다.
          expected.json 을 만들 때 원논문(Wrigley 2019)에서 직접 대조·확인할 것.
        """
        if not _has(WRIGLEY):
            self.skipTest(
                f"Wrigley 2019 데이터 없음. {WRIGLEY} 에 넣을 것 (CC BY 4.0).\n"
                f"  출처: data.mendeley.com/datasets/w39zbzj6bk/1\n"
                f"  넣을 것: 변환된 IR(spec.json) + expected.json"
                f"{{baseline_objective, pipe_reduction_pct_range}}")
        spec_path = os.path.join(WRIGLEY, "spec.json")
        exp_path = os.path.join(WRIGLEY, "expected.json")
        self.assertTrue(os.path.exists(spec_path), "spec.json 이 필요하다")
        self.assertTrue(os.path.exists(exp_path), "expected.json 이 필요하다")
        with open(exp_path, encoding="utf-8") as fp:
            exp = json.load(fp)
        sp = G.load_spec(spec_path)
        base = G.baseline(sp)
        Fb = base.evaluate()
        best, Fo, _ = G.optimize(sp, iters=60000, restarts=4, seed=20260826,
                                 verbose=False)
        red = (Fb - Fo) / Fb * 100.0
        lo, hi = exp["pipe_reduction_pct_range"]
        print(f"\n  Wrigley 2019: 기준 {Fb:,.0f} → 최적 {Fo:,.0f} ({red:.1f}%) "
              f"· 발표 범위 {lo}~{hi}%")
        self.assertGreaterEqual(red, lo * 0.5,
                                "발표 개선폭의 절반에도 못 미친다")

    def test_ejeh2019_appendix_b(self):
        """
        Ejeh 2019 부록 B 의 요소플랜트 8기기 전체 입력데이터가 공개되어 있다.
        MILP 정식화 검증에 쓴다. 기대값: 4층 117,431 rmu.
        """
        if not _has(EJEH):
            self.skipTest(
                f"Ejeh 2019 부록 B 데이터 없음. {EJEH} 에 넣을 것.\n"
                f"  기대값: 1층 260,942 / 2층 167,299 / 3층 149,498 / 4층 117,431 rmu\n"
                f"  ※ 이 엔진은 MILP 가 아니라 메타휴리스틱이므로 값이 아니라 "
                f"'층수-비용 곡선의 형상' 을 대조한다.")
        spec_path = os.path.join(EJEH, "spec.json")
        sp = G.load_spec(spec_path)
        import decompose
        rows = decompose.floor_sweep(sp, [1, 2, 3, 4], iters=40000, restarts=3,
                                     seed=20260826, verbose=True)
        best = min(rows, key=lambda r: r["F"])
        print(f"\n  Ejeh 2019 층수 스윕 최적 = {best['n_floors']}층")
        self.assertLess(rows[-1]["F"], rows[0]["F"],
                        "다층이 단층보다 나빠졌다 — 수직 비용 모형을 의심할 것")


# ---------------------------------------------------------------------------
# 음성 대조군
# ---------------------------------------------------------------------------

class TestNegativeControl(unittest.TestCase):
    """
    LLM 파이프라인은 유창하게 실패한다. 실현 불가능한 요구사항 집합을 넣어
    '불가능' 이라고 답하는지 정례적으로 확인한다.
    """

    def setUp(self):
        self.path = os.path.join(EXAMPLES, "spec_infeasible.json")
        with open(self.path, encoding="utf-8") as fp:
            self.ir = json.load(fp)
        self.rep = validate_spec.check_ir(self.ir, probe=False)
        self.msgs = "\n".join(self.rep.ERR)

    def test_every_planted_defect_is_detected(self):
        expect = {
            "면적 초과": "실현 불가",
            "표고 모순": "표고 제약 모순",
            "치수 누락": "치수 'w' 누락",
            "전기이격 3ft": "ELEC-3FT-5FT",
            "SGI 값": "금지:SGI",
        }
        for label, needle in expect.items():
            self.assertIn(needle, self.msgs, f"음성 대조군의 '{label}' 이 안 잡혔다")

    def test_zone_numbering_is_warned(self):
        self.assertTrue(any("ZONE-NUMBERING" in m for m in self.rep.WARN))

    def test_pipeline_refuses_to_produce_a_drawing(self):
        out = os.path.join(os.path.dirname(__file__), "_tmp_neg")
        r = subprocess.run([sys.executable, os.path.join(ENGINE, "run.py"),
                            self.path, "--iters", "50", "--out", out],
                           capture_output=True, text=True, cwd=ENGINE)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(out))

    def test_impossible_geometry_is_not_silently_shrunk(self):
        """단일 블록이 외피보다 크면 '알아서 줄이지' 않고 ERROR 로 멈춘다."""
        self.assertTrue(any("단일 블록이 외피 한계보다 큼" in m
                            for m in self.rep.ERR))


if __name__ == "__main__":
    unittest.main(verbosity=2)
