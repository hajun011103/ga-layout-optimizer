# -*- coding: utf-8 -*-
"""테스트 공용 유틸 — 엔진을 import 경로에 올리고, 손으로 좌표를 놓은 컨텍스트를 만든다."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine")
if ENGINE not in sys.path:
    sys.path.insert(0, ENGINE)

EXAMPLES = os.path.join(ROOT, "examples")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

import constraints as CONS          # noqa: E402
import gen_docs                     # noqa: E402,F401
import ga_core as G                 # noqa: E402


def spec(blocks, constraints=(), n_floors=1, floor_heights=None, **kw):
    ir = dict(project="unit-test", blocks=[dict(b) for b in blocks],
              constraints=[dict(c) for c in constraints], n_floors=n_floors)
    if floor_heights:
        ir["floor_heights"] = floor_heights
    ir.update(kw)
    return G.spec_from_dict(ir), ir


def ctx(sp, placements, env=(100.0, 100.0, 10.0), shadows=None, azim=None):
    """
    placements: {block_id: (x, y, floor)}  — 치수는 Block.footprint(0) 을 쓴다.
    손으로 좌표를 놓아 제약 평가만 시험한다 (패킹은 관여하지 않는다).
    """
    idx = {b.id: i for i, b in enumerate(sp.blocks)}
    geo = {}
    for bid, (x, y, f) in placements.items():
        i = idx[bid]
        w, d = sp.blocks[i].footprint(0)
        geo[i] = dict(x=float(x), y=float(y), w=w, d=d, f=int(f),
                      z=float(f) * 5.0, cx=x + w / 2, cy=y + d / 2)
    sh = {}
    for (bid, f), (x, y) in (shadows or {}).items():
        i = idx[bid]
        w, d = sp.blocks[i].footprint(0)
        sh[(i, f)] = dict(x=float(x), y=float(y), w=w, d=d, f=int(f),
                          z=float(f) * 5.0, cx=x + w / 2, cy=y + d / 2)
    c = CONS.EvalContext(sp, geo, env, sh)
    c.turbine_azimuth = {idx[k]: v for k, v in (azim or {}).items()}
    c.tall_span = {i: 1 for i in range(len(sp.blocks))}
    return c


def B(bid, w=4.0, d=3.0, **kw):
    kw.setdefault("name", bid)
    return dict(id=bid, w=w, d=d, **kw)


def viol_ids(vs):
    return sorted({v.cid for v in vs})
