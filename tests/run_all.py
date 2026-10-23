# -*- coding: utf-8 -*-
"""전체 테스트 실행 — python3 tests/run_all.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(HERE, pattern="test_*.py")
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if res.wasSuccessful() else 1)
