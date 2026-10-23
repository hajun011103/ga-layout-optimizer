# -*- coding: utf-8 -*-
"""
takeoff.py — Takeoff(도면 파서 출력) → Design Intent IR 초안 + 현황 배치(asis.json)

결정론적 영역. LLM 미개입. 이 단계가 하는 일:
  1. 도면의 기기/실 사각형 → IR blocks (치수는 도면에서, 좌표는 asis.json 으로 분리)
  2. 움직일 수 없는 것(코어·기둥·격납) / 배치 금지 영역 → pinned 블록 (좌표 = 도면)
  3. '현재 공간' 외곽 → max_w / max_d (최적화는 기존 공간 안에서, 더 작게)
  4. 현황 인접관계 → 약한 route 링크 (기능적 인접성을 잃지 않게 — 사람이 확인)
  5. 도면이 알려주지 않는 것 → questions (층고, 유지보수 이격, 연결관계, 구역 …)

IR 에 들어가는 좌표는 pinned 블록의 fixed_xy 뿐이며 그것도 파서가 쓴다.
움직이는 블록의 현황 좌표는 IR 이 아니라 asis.json 에 있다 — 솔버는 그것을 보지 않는다.

  python3 takeoff.py plan.dxf --units mm --project "보조건물 현황" --out work/
  python3 takeoff.py 1F.png 2F.png --px-per-m 20 --out work/
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import asis as ASIS
import irhash
from version import ENGINE_VERSION, IR_SCHEMA_VERSION

DEFAULTS = dict(
    project=None,
    preset="space_first",
    fit_within_existing=True,     # 현재 공간 외곽을 hard 한계(max_w/max_d)로
    keep_floors=True,             # 기기의 층을 현황대로 고정 (층 이동은 큰 설계변경)
    pin_obstacles=True,           # 코어·기둥·격납 = 고정
    pin_openings=False,           # 해치·계단 = 기본은 이동 가능 (FIRE-HATCH 대상)
    infer_adjacency=True,         # 현황 인접 → 약한 route 링크
    adjacency_gap_m=1.0,
    adjacency_cost=0.2,
    default_clearance=None,       # None → 0 으로 두고 질문 목록에 올린다
    floor_heights=None,           # None → 질문 목록에 올린다 (엔진 기본값 사용)
    stack_files=True,             # 단일층 파일 여러 개 → 파일 순서대로 1층, 2층 …
    aspect_margin=0.05,
)


def _nice_ceil(x: float) -> float:
    """가중치를 사람이 읽기 좋은 수로 올림 (1, 2, 5 × 10^k)."""
    if x <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 5, 10):
        if m * e >= x:
            return float(m * e)
    return float(10 * e)


def refresh_hard_weights(ir: dict) -> None:
    """
    hard 항 가중치를 한계이득의 20배로 (다시) 잡는다 — Gate 1 점검식과 같은 식.
    층고·외곽·연결이 바뀌면 한계이득이 바뀌므로 IR 편집 후 다시 부른다.
    기존 값보다 '낮추지는' 않는다 (사람이 올린 값을 존중).
    """
    import ga_core as G
    sp = G.spec_from_dict(ir)
    nf = sp.n_floors
    fh = sp.floor_heights or [G._default_fh(sp)] * nf
    H = sum(fh[:nf])
    W0 = ir.get("max_w") or max((b.w for b in sp.blocks), default=1.0)
    D0 = ir.get("max_d") or max((b.d for b in sp.blocks), default=1.0)
    deg: Dict[str, float] = {}
    for l in sp.links:
        deg[l.a] = deg.get(l.a, 0.0) + l.cost
        deg[l.b] = deg.get(l.b, 0.0) + l.cost
    ref_m = (sp.wt("volume") * max(D0 * H, W0 * H) + sp.wt("area") * max(W0, D0)
             + sp.wt("link") * (max(deg.values()) if deg else 0.0))
    hw = _nice_ceil(20 * ref_m)
    w = dict(ir.get("weights") or {})
    for k in G.HARD_TERMS:
        w[k] = max(float(w.get(k, 0.0)), hw)
    ir["weights"] = w
    ir["_weights_basis"] = (f"hard 항 ≥ 20 × 한계이득(1 m 양보 ≈ {ref_m:,.0f}) = {hw:,.0f}. "
                            f"preset={ir.get('preset', 'balanced')}")


def merge_takeoffs(takeoffs: Sequence[dict], stack_files: bool = True
                   ) -> Tuple[List[dict], Dict[int, List[float]], List[str], List[dict]]:
    """여러 Takeoff → (items, boundary{floor:rect}, warnings, sources). 층 번호를 정리한다."""
    items, bnd, warns, srcs = [], {}, [], []
    single_floor_each = all(len({it["floor"] for it in tk["items"]} |
                                {b["floor"] for b in tk["boundary"]}) <= 1
                            for tk in takeoffs)
    used_ids = set()
    for k, tk in enumerate(takeoffs):
        offset = k if (stack_files and len(takeoffs) > 1 and single_floor_each) else 0
        srcs.append(dict(filename=tk["source"]["filename"], format=tk["source"]["format"],
                         sha256=tk["source"]["sha256"],
                         takeoff_sha256=irhash.sha256_obj(tk)))
        for w in tk.get("warnings", []):
            warns.append(f"[{tk['source']['filename']}] {w}")
        for b in tk["boundary"]:
            f = b["floor"] + offset if offset else b["floor"]
            r = b["rect"]
            if f in bnd:
                o = bnd[f]
                bnd[f] = [min(o[0], r[0]), min(o[1], r[1]), max(o[2], r[2]), max(o[3], r[3])]
            else:
                bnd[f] = list(r)
        for it in tk["items"]:
            it = dict(it)
            if offset:
                it["floor"] = it["floor"] + offset
            bid = it["id"]
            if bid in used_ids:
                n = 2
                while f"{bid}-{n}" in used_ids:
                    n += 1
                warns.append(f"id 중복 '{bid}' → '{bid}-{n}' 로 바꿨다 ({tk['source']['filename']})")
                bid = f"{bid}-{n}"
            used_ids.add(bid)
            it["id"] = bid
            items.append(it)
    return items, bnd, warns, srcs


def _split_corridors(items: List[dict], aspect: float, seg: float) -> Tuple[List[dict], int]:
    """
    형상비가 aspect 이상인 실(room) = 복도·통로로 보고 길이 seg 안팎의 구간으로 나눈다.
    66 m 짜리 복도 하나를 강체 블록으로 두면 솔버가 그것을 통째로 옮길 수밖에 없다.
    나눠도 총면적은 같다. 현황 좌표는 구간별로 그대로 유지된다 (현황 채점 불변).
    """
    out, n = [], 0
    for it in items:
        L, S = max(it["w"], it["d"]), min(it["w"], it["d"])
        if it["kind"] != "room" or S <= 0 or L / S < aspect or it.get("unplaced"):
            out.append(it)
            continue
        k = max(2, int(round(L / max(seg, S))))
        n += 1
        for j in range(k):
            c = dict(it)
            c["id"] = f"{it['id']}-S{j+1}"
            c["name"] = f"{it.get('name') or it['id']} (구간 {j+1}/{k})"
            c["polygon"] = None
            c["labels"] = list(it.get("labels", []))
            if it["w"] >= it["d"]:
                c["w"] = it["w"] / k; c["x"] = it["x"] + j * c["w"]
            else:
                c["d"] = it["d"] / k; c["y"] = it["y"] + j * c["d"]
            c["_corridor_of"] = it["id"]
            out.append(c)
    return out, n


def _gap(a: dict, b: dict) -> float:
    dx = max(0.0, max(a["x"], b["x"]) - min(a["x"] + a["w"], b["x"] + b["w"]))
    dy = max(0.0, max(a["y"], b["y"]) - min(a["y"] + a["d"], b["y"] + b["d"]))
    return math.hypot(dx, dy)


def takeoff_to_ir(takeoffs: Sequence[dict], options: Optional[dict] = None
                  ) -> Tuple[dict, dict, List[dict]]:
    """
    반환: (ir, asis, questions)
      questions = [{"id", "question", "why", "field"}] — LLM 은 이것을 사용자에게 묻는다.
                  답은 IR 의 비좌표 필드로만 들어간다.
    """
    o = dict(DEFAULTS)
    o.update({k: v for k, v in (options or {}).items() if v is not None})
    items, bnd, warns, srcs = merge_takeoffs(takeoffs, o["stack_files"])
    if not items:
        raise ValueError("도면에서 인식된 기기/실이 없다 — 레이어 분류(layer_map)나 축척을 확인할 것")

    if o.get("split_corridors", True):
        items, n_split = _split_corridors(items, o.get("corridor_aspect", 5.0),
                                          o.get("corridor_segment_m", 12.0))
        if n_split:
            warns.append(f"가늘고 긴 실(복도로 판단) {n_split}개를 구간으로 나눴다 — 면적은 그대로, "
                         f"최적화가 동선 공간을 재배치할 수 있게. 복도를 그대로 두려면 해당 블록을 고정(pinned)할 것")

    nf = max([it["floor"] for it in items] + list(bnd.keys()) + [0]) + 1
    project = o["project"] or ("현황 도면 최적화: " +
                               ", ".join(s["filename"] for s in srcs))
    questions: List[dict] = []

    def ask(qid, q, why, field):
        questions.append(dict(id=qid, question=q, why=why, field=field))

    # ---- 현황 통계 (질문에 근거로 붙인다) -----------------------------------
    movable_items = [it for it in items if it["kind"] in ("equipment", "room", "opening")]
    gaps = []
    for a in range(len(movable_items)):
        best = None
        for b in range(len(movable_items)):
            if a == b or movable_items[a]["floor"] != movable_items[b]["floor"]:
                continue
            g = _gap(movable_items[a], movable_items[b])
            if g > 1e-6 and (best is None or g < best):
                best = g
        if best is not None:
            gaps.append(best)
    med_gap = statistics.median(gaps) if gaps else None

    # ---- blocks ------------------------------------------------------------
    blocks, asis_blocks, unplaced = [], {}, []
    for it in items:
        kind = it["kind"]
        b = dict(id=it["id"], name=it.get("name") or it["id"], w=round(it["w"], 4),
                 d=round(it["d"], 4), kind=kind)
        if it.get("h"):
            b["h"] = round(float(it["h"]), 4)
        for k in ("zone", "weight", "fire_zone", "train"):
            if it.get(k) not in (None, ""):
                b[k] = it[k]
        cl = it.get("clearance")
        if cl in (None, ""):
            cl = o["default_clearance"] if kind in ("equipment",) else 0.0
        b["clearance"] = float(cl or 0.0)
        pin = ((kind == "obstacle" and o["pin_obstacles"]) or kind == "void"
               or (kind == "opening" and o["pin_openings"]))
        if kind == "opening":
            b["opening"] = True
        if it.get("unplaced"):
            unplaced.append(it["id"])
            if pin:
                pin = False
                warns.append(f"[{it['id']}] 좌표가 없어 고정할 수 없다 — 이동 가능 블록으로 둔다")
        else:
            asis_blocks[it["id"]] = dict(x=it["x"], y=it["y"], w=it["w"], d=it["d"],
                                         floor=it["floor"])
        if pin:
            b["pinned"] = True
            b["rotatable"] = False
            b["fixed_xy"] = [round(it["x"] + it["w"] / 2, 4), round(it["y"] + it["d"] / 2, 4)]
            b["floor"] = it["floor"]
            b["clearance"] = 0.0
        else:
            if o["keep_floors"] and not it.get("unplaced"):
                b["floor"] = it["floor"]
            if it.get("polygon") is not None and it.get("rect_fill", 1.0) < 0.85:
                b["_shape_note"] = (f"원형상은 직사각형이 아님(채움률 {it['rect_fill']:.0%}) "
                                    f"— 외접 사각형으로 근사")
        if it.get("labels"):
            b["_labels"] = it["labels"]
        b["_source_ref"] = it.get("source_ref", "")
        blocks.append(b)

    # ---- 현재 공간 → 외피 한계 ------------------------------------------------
    ir: dict = {
        "_schema": "smr-ga-ir/1.0",
        "_note": ("도면 파서가 만든 IR 초안. 치수는 도면에서 왔고, 좌표는 고정 장애물에만 있다. "
                  "questions 를 사용자에게 확인한 뒤 Gate 1 으로 넘길 것."),
        "project": project,
        "preset": o["preset"],
        "n_floors": nf,
        "drawing_source": srcs,
        "blocks": blocks,
        "links": [],
        "separations": [],
        "elevations": [],
        "constraints": [],
    }
    if o["floor_heights"]:
        fh = [float(x) for x in o["floor_heights"]]
        if len(fh) == 1 and nf > 1:
            fh = fh * nf
        ir["floor_heights"] = fh
    else:
        ask("floor_heights",
            f"층고(바닥~바닥, m)는 얼마입니까? 도면 평면에는 높이 정보가 없습니다 ({nf}개 층).",
            "체적 = 면적 × 높이. 층수를 바꾸는 검토(층수 스윕)에 필요하다. "
            "현황 vs 최적화 비교(같은 층수)는 층고와 무관하게 성립한다.",
            "floor_heights")

    if bnd:
        W0 = max(r[2] for r in bnd.values()); D0 = max(r[3] for r in bnd.values())
        basis = "도면 외곽(경계) 레이어"
    else:
        W0 = max(a["x"] + a["w"] for a in asis_blocks.values()) if asis_blocks else None
        D0 = max(a["y"] + a["d"] for a in asis_blocks.values()) if asis_blocks else None
        basis = "기기 외접 사각형 (경계 레이어 없음)"
    if o["fit_within_existing"] and W0 and D0:
        ir["max_w"], ir["max_d"] = round(W0, 4), round(D0, 4)
        ir["_envelope_basis"] = f"max_w/max_d = 현재 공간 {W0:.2f}×{D0:.2f} m ({basis})"
        asp = max(W0, D0) / max(min(W0, D0), 1e-9)
        ir["aspect_max"] = round(max(2.5, asp + o["aspect_margin"]), 3)

    # ---- 인접관계 → 약한 링크 ------------------------------------------------
    n_adj = 0
    if o["infer_adjacency"]:
        mv = [it for it in items if it["kind"] in ("equipment", "room", "opening")
              and not it.get("unplaced")]
        for a in range(len(mv)):
            for b in range(a + 1, len(mv)):
                if mv[a]["floor"] != mv[b]["floor"]:
                    continue
                if _gap(mv[a], mv[b]) <= o["adjacency_gap_m"] + 1e-9:
                    ir["links"].append(dict(a=mv[a]["id"], b=mv[b]["id"],
                                            cost=o["adjacency_cost"], kind="route",
                                            _inferred="현황 도면 인접 (사람 확인 필요)"))
                    n_adj += 1
    ask("links",
        "기기·실 사이의 실제 연결관계(배관·케이블·덕트·동선)와 상대 비용을 알려주시겠습니까? "
        "P&ID·계통도·동선 요구가 있으면 그것이 기준입니다."
        + (f" 지금은 도면에서 서로 {o['adjacency_gap_m']:g} m 이내로 붙어 있는 {n_adj}쌍을 "
           f"'약한 인접 링크'로 추정해 두었습니다." if n_adj else ""),
        "도면은 연결을 모른다. 연결이 없으면 최적화는 '면적만' 줄이고 기능적 동선을 무시한다.",
        "links")

    n_eq = sum(1 for it in items if it["kind"] == "equipment")
    if n_eq and o["default_clearance"] is None:
        ask("clearance",
            "기기 주위 유지보수·접근 이격(m)을 얼마로 둘까요?"
            + (f" 현황 도면에서 기기 간 최소 간극의 중앙값은 {med_gap:.2f} m 입니다." if med_gap else ""),
            "이격 0 이면 솔버가 기기를 맞붙인다 — 개선율이 과장된다. "
            "현황 간극을 이격으로 쓰면 현황과 같은 정비성 수준에서 비교된다.",
            "blocks[].clearance")
    if any(it["kind"] == "obstacle" for it in items):
        ask("pinned",
            "고정 장애물로 인식한 항목(코어·기둥·샤프트 등)이 정말 움직일 수 없습니까? "
            "옮겨도 되는 것이 있으면 알려주세요.",
            "고정 장애물은 공간 축소의 하한을 정한다.",
            "blocks[].pinned")
    unlabeled = [it["id"] for it in items
                 if not it.get("labels") and it["kind"] in ("equipment", "room")]
    if unlabeled:
        ask("names",
            f"이름 없는 항목 {len(unlabeled)}개({', '.join(unlabeled[:6])}{' …' if len(unlabeled) > 6 else ''})"
            "의 이름·용도를 알려주시겠습니까? (좌표·치수는 필요 없습니다)",
            "이름은 구역·이격·연결 요구를 붙이는 데 쓰인다.", "blocks[].name")
    ask("zones_separations",
        "구역(방사선·위험·청정 등)과 반드시 떨어뜨려야 하는 쌍, 외벽에 붙어야 하는 항목이 있습니까? "
        "근거(규정 조항)도 함께 알려주세요.",
        "도면에는 설계 요건이 없다. 요건 없이 줄이면 현황보다 '나쁜' 배치가 작게 나올 수 있다.",
        "zone / separations / need_exterior / constraints")
    if unplaced:
        ask("unplaced",
            f"좌표가 없는 항목 {len(unplaced)}개는 현황 비교에서 빠집니다. 배치도(도면)가 있습니까?",
            "목록표만으로는 현황 대비 개선량을 계산할 수 없다.", "asis")

    # ---- hard 가중치 — 한계이득 기반 (Gate 1 의 점검식과 같은 식) ----------------
    ir["weights"] = {}
    ir["_weights_auto"] = True
    refresh_hard_weights(ir)

    asis = {
        "_schema": ASIS.ASIS_SCHEMA,
        "project": project,
        "boundary": {str(f): [round(v, 6) for v in r] for f, r in sorted(bnd.items())},
        "blocks": {k: {kk: (round(vv, 6) if isinstance(vv, float) else vv)
                       for kk, vv in v.items()} for k, v in sorted(asis_blocks.items())},
        "unplaced": sorted(unplaced),
        "source": srcs,
    }
    ir["asis_sha256"] = ASIS.asis_hash(asis) if asis_blocks else None
    ir["_import_warnings"] = warns
    ir["_import_stats"] = dict(
        items=len(items), floors=nf,
        by_kind={k: sum(1 for it in items if it["kind"] == k)
                 for k in ("equipment", "room", "obstacle", "void", "opening")},
        inferred_adjacency_links=n_adj,
        median_gap_m=round(med_gap, 3) if med_gap is not None else None,
        existing_space_m=[round(W0, 3), round(D0, 3)] if W0 else None)
    return ir, asis, questions


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="도면 → Takeoff → IR 초안 + asis.json")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--units"); ap.add_argument("--scale-ratio", type=float)
    ap.add_argument("--m-per-unit", type=float); ap.add_argument("--px-per-m", type=float)
    ap.add_argument("--page"); ap.add_argument("--floor", type=int)
    ap.add_argument("--layer-map", help='JSON, 예: {"EQ_.*":"equipment"}')
    ap.add_argument("--project")
    ap.add_argument("--floor-heights", help="예: 6.0 또는 6,5,5")
    ap.add_argument("--clearance", type=float)
    ap.add_argument("--preset", default="space_first")
    ap.add_argument("--out", default="work")
    a = ap.parse_args(argv)

    import importers
    opts = {k: v for k, v in dict(units=a.units, scale_ratio=a.scale_ratio,
                                  m_per_unit=a.m_per_unit, px_per_m=a.px_per_m,
                                  page=a.page, floor=a.floor).items() if v is not None}
    if a.layer_map:
        opts["layer_map"] = json.loads(a.layer_map)
    tks = []
    for p in a.files:
        try:
            tk = importers.import_path(p, opts)
        except importers.ImportErrorUser as ex:
            print(f"[입력 오류] {p}: {ex}")
            return 1
        tks.append(tk)
        print(f"  {p}: {tk['source']['format']} · item {tk['stats'].get('items')} "
              f"{tk['stats'].get('by_kind')} · 축척 {tk['scale_basis']}")
    fh = [float(x) for x in a.floor_heights.split(",")] if a.floor_heights else None
    ir, asis, qs = takeoff_to_ir(tks, dict(project=a.project, floor_heights=fh,
                                           default_clearance=a.clearance, preset=a.preset))
    os.makedirs(a.out, exist_ok=True)
    for name, obj in (("takeoff.json", tks if len(tks) > 1 else tks[0]),
                      ("spec.json", ir), ("asis.json", asis), ("questions.json", qs)):
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as fp:
            json.dump(obj, fp, ensure_ascii=False, indent=2)
    print(f"\n  IR 초안 → {os.path.join(a.out, 'spec.json')}  (블록 {len(ir['blocks'])}개)")
    print(f"  현황 배치 → {os.path.join(a.out, 'asis.json')}")
    print(f"  확인 질문 {len(qs)}건 → questions.json")
    for q in qs:
        print(f"   - [{q['id']}] {q['question']}")
    for w in ir["_import_warnings"]:
        print(f"  [WARN] {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
