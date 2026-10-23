# -*- coding: utf-8 -*-
"""
bench_qap.py — 1단계 검증: 최적해가 알려진 벤치마크에서 최적성 갭을 정량 보고한다.

왜 QAP 인가
-----------
QAPLIB 인스턴스는 최적해(또는 최선 알려진 해)가 공개되어 있어, 우리 탐색부가
'얼마나 못 맞추는지' 를 숫자로 말할 수 있는 **유일한 단계**다. 여기서 못 맞추면
원전 문제로 갈 이유가 없다. (배치 목적함수의 정당성이 아니라 탐색부의 성능을 본다.)

주의 — 이 파일은 배치 목적함수를 검증하지 않는다. QAP 는 이산 위치 배정 문제이고
GA 패킹은 연속 좌표 문제다. 검증되는 것은 어닐링 스케줄·이동집합·다중 재시작 등
**탐색 기계장치** 뿐이다. 이 구분을 보고서에 반드시 명시할 것.

데이터 (폐쇄망: 수동 복사)
  QAPLIB    coral.ise.lehigh.edu/data-sets/qaplib/   -> tests/data/qaplib/*.dat, *.sln
  2DPackLib https://site.unibo.it/operations-research/en/research/2dpacklib
  데이터가 없으면 테스트는 SKIP 된다. 값을 지어내지 않는다.

  python3 bench_qap.py --selftest              # 외부 데이터 없이: 전수탐색 대조
  python3 bench_qap.py --dir tests/data/qaplib # QAPLIB 인스턴스 전부
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import math
import os
import random
import sys
from typing import List, Optional, Sequence, Tuple

Matrix = List[List[float]]


# ---------------------------------------------------------------------------
# QAPLIB 파일 형식
# ---------------------------------------------------------------------------

def _tokens(path: str) -> List[str]:
    with open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read().split()


def load_dat(path: str) -> Tuple[int, Matrix, Matrix]:
    """QAPLIB .dat: n, 그리고 n×n 행렬 두 개(흐름 F, 거리 D)."""
    t = _tokens(path)
    n = int(t[0])
    need = 1 + 2 * n * n
    if len(t) < need:
        raise ValueError(f"{path}: 토큰 부족 ({len(t)} < {need})")
    v = [float(x) for x in t[1:need]]
    A = [v[i * n:(i + 1) * n] for i in range(n)]
    B = [v[n * n + i * n: n * n + (i + 1) * n] for i in range(n)]
    return n, A, B


def load_sln(path: str) -> Tuple[int, float, Optional[List[int]]]:
    """
    QAPLIB .sln: 'n  objective' 다음에 순열.
    ★ 최적값을 코드에 하드코딩하지 않는다 — 사용자가 받은 .sln 에서 읽는다.
    """
    t = _tokens(path)
    n = int(t[0])
    obj = float(t[1])
    perm = None
    if len(t) >= 2 + n:
        perm = [int(x) - 1 for x in t[2:2 + n]]
        if sorted(perm) != list(range(n)):
            perm = None
    return n, obj, perm


# ---------------------------------------------------------------------------
# 목적함수 및 탐색
# ---------------------------------------------------------------------------

def qap_cost(perm: Sequence[int], A: Matrix, B: Matrix) -> float:
    """Σ_ij A[i][j] · B[perm[i]][perm[j]]  (QAPLIB 표준 정식화)."""
    n = len(perm)
    s = 0.0
    for i in range(n):
        ai = A[i]
        bi = B[perm[i]]
        for j in range(n):
            s += ai[j] * bi[perm[j]]
    return s


def _delta_swap(perm: List[int], A: Matrix, B: Matrix, u: int, v: int) -> float:
    """u,v 자리를 바꿀 때의 비용 변화 (O(n))."""
    n = len(perm)
    pu, pv = perm[u], perm[v]
    d = 0.0
    for k in range(n):
        if k in (u, v):
            continue
        pk = perm[k]
        d += (A[u][k] * (B[pv][pk] - B[pu][pk])
              + A[k][u] * (B[pk][pv] - B[pk][pu])
              + A[v][k] * (B[pu][pk] - B[pv][pk])
              + A[k][v] * (B[pk][pu] - B[pk][pv]))
    d += (A[u][v] * (B[pv][pu] - B[pu][pv])
          + A[v][u] * (B[pu][pv] - B[pv][pu]))
    d += (A[u][u] * (B[pv][pv] - B[pu][pu])
          + A[v][v] * (B[pu][pu] - B[pv][pv]))
    return d


def solve_qap(A: Matrix, B: Matrix, iters: int = 200000, restarts: int = 5,
              seed: int = 20260826) -> Tuple[List[int], float]:
    """
    SA + 다중 재시작. ga_core 와 같은 계열의 어닐링 스케줄·이동집합을 쓴다.
    seed 고정으로 완전 재현 가능.
    """
    n = len(A)
    rng = random.Random(seed)
    best, bestC = None, float("inf")
    for _ in range(restarts):
        perm = list(range(n))
        rng.shuffle(perm)
        cur = qap_cost(perm, A, B)
        lb, lbc = list(perm), cur
        T0 = max(1e-9, abs(cur) * 0.08)
        T1 = max(1e-12, T0 * 1e-5)
        for it in range(iters):
            T = T0 * (T1 / T0) ** (it / max(1, iters - 1))
            u, v = rng.randrange(n), rng.randrange(n)
            if u == v:
                continue
            d = _delta_swap(perm, A, B, u, v)
            if d < 0 or rng.random() < math.exp(-d / max(T, 1e-12)):
                perm[u], perm[v] = perm[v], perm[u]
                cur += d
                if cur < lbc - 1e-9:
                    lb, lbc = list(perm), cur
        if lbc < bestC:
            best, bestC = lb, lbc
    return best, qap_cost(best, A, B)


def brute_force(A: Matrix, B: Matrix) -> Tuple[List[int], float]:
    """n <= 8 전수탐색. 외부 데이터 없이 '진짜 최적' 을 얻는 유일한 방법."""
    n = len(A)
    if n > 8:
        raise ValueError("전수탐색은 n<=8 에서만")
    best, bestC = None, float("inf")
    for p in itertools.permutations(range(n)):
        c = qap_cost(p, A, B)
        if c < bestC:
            best, bestC = list(p), c
    return best, bestC


def random_instance(n: int, seed: int) -> Tuple[Matrix, Matrix]:
    """자체 생성 인스턴스 (seed 고정). 전수탐색 대조용."""
    rng = random.Random(seed)
    A = [[0.0] * n for _ in range(n)]
    B = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            a = float(rng.randrange(0, 20))
            b = float(rng.randrange(1, 20))
            A[i][j] = A[j][i] = a
            B[i][j] = B[j][i] = b
    return A, B


# ---------------------------------------------------------------------------

def gap_pct(found: float, opt: float) -> float:
    return (found - opt) / opt * 100.0 if opt else float("nan")


def run_dir(d: str, iters: int, restarts: int, seed: int) -> List[dict]:
    rows = []
    for dat in sorted(glob.glob(os.path.join(d, "*.dat"))):
        name = os.path.splitext(os.path.basename(dat))[0]
        sln = os.path.join(d, name + ".sln")
        if not os.path.exists(sln):
            print(f"  {name}: .sln 없음 — 최적값을 모르므로 갭을 보고할 수 없다. SKIP")
            continue
        n, A, B = load_dat(dat)
        n2, opt, _ = load_sln(sln)
        if n != n2:
            print(f"  {name}: .dat(n={n}) 와 .sln(n={n2}) 불일치. SKIP")
            continue
        _, found = solve_qap(A, B, iters=iters, restarts=restarts, seed=seed)
        g = gap_pct(found, opt)
        rows.append(dict(instance=name, n=n, known_optimum=opt, found=found,
                         gap_pct=round(g, 4)))
        print(f"  {name:<12s} n={n:<3d} 최적 {opt:>12,.0f} | 우리 {found:>12,.0f} | "
              f"갭 {g:6.2f}%")
    return rows


def selftest(seed: int = 20260826, iters: int = 40000, restarts: int = 3) -> List[dict]:
    """외부 데이터 없이 최적성 갭을 보고한다 — 전수탐색이 정답을 준다."""
    rows = []
    for n in (6, 7, 8):
        A, B = random_instance(n, seed + n)
        _, opt = brute_force(A, B)
        _, found = solve_qap(A, B, iters=iters, restarts=restarts, seed=seed)
        g = gap_pct(found, opt)
        rows.append(dict(instance=f"selftest-n{n}", n=n, known_optimum=opt,
                         found=found, gap_pct=round(g, 6)))
        print(f"  selftest-n{n}  전수탐색 최적 {opt:>10,.0f} | 우리 {found:>10,.0f} | "
              f"갭 {g:6.3f}%")
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None, help="QAPLIB .dat/.sln 디렉터리")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--iters", type=int, default=100000)
    ap.add_argument("--restarts", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260826)
    ap.add_argument("--json", default=None, help="결과를 JSON 으로 저장")
    a = ap.parse_args(argv)

    rows: List[dict] = []
    print("=" * 74)
    print("  1단계 검증 — 최적해가 알려진 인스턴스에서의 최적성 갭")
    print("  ※ 검증 대상은 탐색 기계장치(어닐링·이동집합)이지 배치 목적함수가 아니다.")
    print("=" * 74)
    if a.selftest or not a.dir:
        rows += selftest(seed=a.seed, iters=min(a.iters, 40000),
                         restarts=min(a.restarts, 3))
    if a.dir:
        if not os.path.isdir(a.dir):
            print(f"  디렉터리 없음: {a.dir}")
        else:
            rows += run_dir(a.dir, a.iters, a.restarts, a.seed)
    if rows:
        worst = max(r["gap_pct"] for r in rows)
        print("-" * 74)
        print(f"  인스턴스 {len(rows)}건 · 최대 갭 {worst:.3f}%")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fp:
            json.dump(rows, fp, ensure_ascii=False, indent=2)
        print(f"  → {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
