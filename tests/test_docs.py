# -*- coding: utf-8 -*-
"""
test_docs.py — 문서와 코드가 어긋나지 않는지.

규제 수치의 사용금지 항목은 문서와 코드 양쪽에 있어야 한다(합격 기준).
한쪽만 고치면 이 테스트가 잡는다.
"""
import os
import unittest

from _util import CONS, ROOT
import gen_docs

REF = os.path.join(ROOT, "skills", "ga-layout", "references")


def read(name):
    with open(os.path.join(REF, name), encoding="utf-8") as fp:
        return fp.read()


class TestConstraintCatalogIsGenerated(unittest.TestCase):

    def test_catalog_matches_registry(self):
        self.assertEqual(gen_docs.main(["--check"]), 0,
                         "constraint-library.md 가 코드와 어긋난다 — "
                         "python3 engine/gen_docs.py --write")

    def test_every_constraint_appears_in_catalog(self):
        txt = read("constraint-library.md")
        for cid in CONS.REGISTRY:
            self.assertIn(f"`{cid}`", txt)


class TestForbiddenValuesDocumentedBothSides(unittest.TestCase):
    """사용금지 항목이 문서와 코드 주석 양쪽에 있는가."""

    NEEDLES = {
        "ELEC-3FT-5FT": ["3 ft", "IEEE 384", "RG 1.75"],
        "SEISMIC-FIXED": ["성능식", "시공오차"],
        "ZONE-NUMBERING": ["RG 8.8", "20.1003"],
        "ISOLATION-20FT": ["73.51", "73.55"],
        "SGI": ["Safeguards Information", "stand-off"],
    }

    def test_all_five_are_in_the_document(self):
        txt = read("regulatory-numbers.md")
        for key, needles in self.NEEDLES.items():
            for n in needles:
                self.assertIn(n, txt, f"{key}: 문서에 '{n}' 이 없다")

    def test_all_five_are_in_the_code(self):
        keys = {f["key"] for f in CONS.FORBIDDEN_VALUES}
        self.assertEqual(keys, set(self.NEEDLES))

    def test_code_and_doc_agree_on_count(self):
        txt = read("regulatory-numbers.md")
        self.assertIn("사용금지 5건", txt)
        self.assertEqual(len(CONS.FORBIDDEN_VALUES), 5)


class TestReadmeDisclaimers(unittest.TestCase):

    def test_not_for_construction_is_stated(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fp:
            txt = fp.read()
        self.assertIn("NOT FOR CONSTRUCTION", txt)
        self.assertIn("한계", txt)

    def test_agents_md_exists_for_codex(self):
        p = os.path.join(ROOT, "AGENTS.md")
        self.assertTrue(os.path.exists(p))
        with open(p, encoding="utf-8") as fp:
            txt = fp.read()
        self.assertIn("validate_spec.py", txt)
        self.assertIn("run.py", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
