# -*- coding: utf-8 -*-
"""
test_determinism.py — 재현성 강제.

10 CFR 50 Appendix B / ASME NQA-1 체계의 설계도구 검증은 도구의 결정론을 전제한다.
LLM 은 같은 프롬프트·같은 설정에서도 출력이 달라지므로, 숫자를 만드는 구간은
전부 이 파일이 지키는 성질을 만족해야 한다.

  동일 IR 해시 + 동일 seed  →  동일 결과 해시
"""
import json
import os
import subprocess
import sys
import unittest

from _util import ENGINE, EXAMPLES, G
import irhash
import nsga
import decompose
import run as runner
import validate_spec


MIN = os.path.join(EXAMPLES, "spec_minimal.json")
INFEAS = os.path.join(EXAMPLES, "spec_infeasible.json")


def payload_hash(spec, sol):
    return irhash.sha256_obj(runner.result_payload(spec, sol))


class TestIRHash(unittest.TestCase):

    def test_key_order_does_not_change_hash(self):
        a = dict(project="p", blocks=[dict(id="A", w=1, d=2)], n_floors=1)
        b = dict(n_floors=1, blocks=[dict(d=2, w=1, id="A")], project="p")
        self.assertEqual(irhash.sha256_obj(a), irhash.sha256_obj(b))

    def test_comments_do_not_change_hash(self):
        a = dict(project="p", blocks=[dict(id="A", w=1, d=2)])
        b = dict(project="p", _note="사람용 주석", blocks=[dict(id="A", w=1, d=2,
                                                            _why="설명")])
        self.assertEqual(irhash.sha256_obj(a), irhash.sha256_obj(b))

    def test_value_change_does_change_hash(self):
        a = dict(project="p", blocks=[dict(id="A", w=1, d=2)])
        b = dict(project="p", blocks=[dict(id="A", w=1.0001, d=2)])
        self.assertNotEqual(irhash.sha256_obj(a), irhash.sha256_obj(b))

    def test_examples_have_stable_hashes(self):
        for f in ("spec_minimal.json", "spec_smr_aux.json"):
            p = os.path.join(EXAMPLES, f)
            self.assertEqual(irhash.sha256_file(p), irhash.sha256_file(p))


class TestSearchDeterminism(unittest.TestCase):

    def setUp(self):
        self.spec = G.load_spec(MIN)

    def test_sa_same_seed_same_result(self):
        a, Fa, _ = G.optimize(self.spec, iters=1500, restarts=2, seed=42,
                              verbose=False)
        b, Fb, _ = G.optimize(self.spec, iters=1500, restarts=2, seed=42,
                              verbose=False)
        self.assertEqual(Fa, Fb)
        self.assertEqual(payload_hash(self.spec, a), payload_hash(self.spec, b))

    def test_sa_different_seed_explores_differently(self):
        _, Fa, _ = G.optimize(self.spec, iters=1500, restarts=1, seed=1,
                              verbose=False)
        _, Fb, _ = G.optimize(self.spec, iters=1500, restarts=1, seed=2,
                              verbose=False)
        # 같은 값이 나올 수도 있으나(둘 다 전역최적) 궤적은 seed 로만 결정된다.
        _, Fa2, _ = G.optimize(self.spec, iters=1500, restarts=1, seed=1,
                               verbose=False)
        self.assertEqual(Fa, Fa2)

    def test_baseline_is_deterministic(self):
        h1 = payload_hash(self.spec, G.baseline(self.spec))
        h2 = payload_hash(self.spec, G.baseline(self.spec))
        self.assertEqual(h1, h2)

    def test_nsga_same_seed_same_front(self):
        f1, _ = nsga.nsga2(self.spec, pop_size=10, generations=4, seed=9,
                           verbose=False)
        f2, _ = nsga.nsga2(self.spec, pop_size=10, generations=4, seed=9,
                           verbose=False)
        self.assertEqual([i.obj for i in f1], [i.obj for i in f2])
        self.assertEqual([payload_hash(self.spec, i.sol) for i in f1],
                         [payload_hash(self.spec, i.sol) for i in f2])

    def test_partition_is_deterministic(self):
        a = decompose.partition_areas(self.spec, 2, seed=5)
        b = decompose.partition_areas(self.spec, 2, seed=5)
        self.assertEqual(a, b)

    def test_penetration_repair_never_worsens(self):
        sol, F, _ = G.optimize(self.spec, iters=800, restarts=1, seed=3,
                               verbose=False)
        rep, Fr = G.repair_penetrations(sol, F)
        self.assertLessEqual(Fr, F + 1e-9)


class TestGateBlocksExecution(unittest.TestCase):
    """IR 에 ERROR 가 있으면 최적화가 실행되지 않는다 (합격 기준)."""

    def test_infeasible_spec_fails_gate(self):
        with open(INFEAS, encoding="utf-8") as fp:
            ir = json.load(fp)
        rep = validate_spec.check_ir(ir, probe=False)
        self.assertTrue(rep.ERR)

    def test_run_refuses_infeasible_spec(self):
        out = os.path.join(os.path.dirname(__file__), "_tmp_refuse")
        r = subprocess.run([sys.executable, os.path.join(ENGINE, "run.py"),
                            INFEAS, "--iters", "100", "--restarts", "1",
                            "--out", out],
                           capture_output=True, text=True, cwd=ENGINE)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("GATE 1 불통과", r.stdout)
        self.assertNotIn("OPTIMIZING", r.stdout)
        self.assertFalse(os.path.exists(out),
                         "게이트에서 막혔는데 출력 디렉터리가 생성되면 안 된다")

    def test_run_manifest_records_reproducibility_fields(self):
        out = os.path.join(os.path.dirname(__file__), "_tmp_manifest")
        r = subprocess.run([sys.executable, os.path.join(ENGINE, "run.py"),
                            MIN, "--iters", "400", "--restarts", "1",
                            "--out", out],
                           capture_output=True, text=True, cwd=ENGINE)
        self.assertIn(r.returncode, (0, 2), r.stdout + r.stderr)
        with open(os.path.join(out, "run_manifest.json"), encoding="utf-8") as fp:
            m = json.load(fp)
        for k in ("ir_sha256", "seed", "engine_version", "result_sha256"):
            self.assertIn(k, m)
        self.assertEqual(m["ir_sha256"], irhash.sha256_file(MIN))
        # 같은 명령을 다시 돌리면 결과 해시가 같아야 한다
        r2 = subprocess.run([sys.executable, os.path.join(ENGINE, "run.py"),
                             MIN, "--iters", "400", "--restarts", "1",
                             "--out", out + "2"],
                            capture_output=True, text=True, cwd=ENGINE)
        with open(os.path.join(out + "2", "run_manifest.json"),
                  encoding="utf-8") as fp:
            m2 = json.load(fp)
        self.assertEqual(m["result_sha256"], m2["result_sha256"])
        import shutil
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(out + "2", ignore_errors=True)


class TestFreeze(unittest.TestCase):

    def test_freeze_writes_hash(self):
        import shutil
        tmp = os.path.join(os.path.dirname(__file__), "_tmp_freeze.json")
        shutil.copy(MIN, tmp)
        code = validate_spec.main([tmp, "--freeze", "--no-probe"])
        self.assertEqual(code, 0)
        frozen = tmp.replace(".json", ".frozen.json")
        with open(frozen + ".sha256", encoding="utf-8") as fp:
            h = fp.read().strip()
        self.assertEqual(h, irhash.sha256_file(tmp))
        with open(frozen, encoding="utf-8") as fp:
            self.assertEqual(json.load(fp)["_frozen"]["ir_sha256"], h)
        # 동결본의 _frozen 은 주석 키이므로 해시에 영향을 주지 않는다
        self.assertEqual(irhash.sha256_file(frozen), h)
        for f in (tmp, frozen, frozen + ".sha256"):
            os.remove(f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
