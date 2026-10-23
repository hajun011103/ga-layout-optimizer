# -*- coding: utf-8 -*-
"""
validate_spec.py — Design Intent IR 검증기 (Gate 1 의 결정론적 게이트)

LLM 이 자연어에서 뽑아낸 IR 은 그럴듯하지만 틀릴 수 있다. 측정된 값으로,
같은 생성기에 조건을 사람이 직접 넣으면 IoU 85.04, LLM 이 문장에서 뽑아 넣으면
60.51 이었고 LLM 의 추출 정확도는 치수 48.4% / 위치 53.0% 였다.
→ 되묻고 검증하는 것이 비용이 아니라 성능이다.

따라서 최적화 엔진에 들어가기 전에 **LLM 을 전혀 쓰지 않는** 규칙 기반 검사를
통과해야 한다. 여기서 걸리는 항목은 사람이 판단해서 해소한다.

  python3 validate_spec.py spec.json                 # 검증만
  python3 validate_spec.py spec.json --freeze        # 통과 시 IR 동결 + 해시 출력
  python3 validate_spec.py spec.json --json          # 기계판독 결과

종료코드 0 = 통과, 1 = ERROR 존재(진행 금지). WARN 은 통과하되 사람이 판단.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Any, Dict, List, Tuple

import constraints as CONS
import irhash
from version import ENGINE_VERSION, IR_SCHEMA_VERSION


class Report:
    def __init__(self):
        self.ERR: List[str] = []
        self.WARN: List[str] = []
        self.INFO: List[str] = []

    def E(self, m): self.ERR.append(m)
    def W(self, m): self.WARN.append(m)
    def I(self, m): self.INFO.append(m)


# ---------------------------------------------------------------------------

def check_ir(d: dict, probe: bool = True) -> Report:
    r = Report()

    # ---- 0. 필수 키 -------------------------------------------------------
    for k in ("project", "blocks"):
        if k not in d:
            r.E(f"필수 키 누락: '{k}'")
    if r.ERR:
        return r
    if not isinstance(d["blocks"], list) or not d["blocks"]:
        r.E("blocks 가 비어 있음")
        return r

    ids: Dict[str, dict] = {}
    dup = set()
    for b in d["blocks"]:
        bid = b.get("id")
        if not bid:
            r.E("id 없는 블록 존재"); continue
        if bid in ids:
            dup.add(bid)
        ids[bid] = b

        # ---- 1. 치수 정합성 ------------------------------------------------
        for k in ("w", "d"):
            v = b.get(k)
            if v is None:
                r.E(f"[{bid}] 치수 '{k}' 누락 — 값을 지어내지 말고 사용자에게 물을 것")
            elif not isinstance(v, (int, float)) or v <= 0:
                r.E(f"[{bid}] 치수 {k}={v!r} 가 양수가 아님")
            elif v > 200:
                r.W(f"[{bid}] {k}={v} m — 단위 오류(mm↔m) 의심")
            elif v < 0.5:
                r.W(f"[{bid}] {k}={v} m — 지나치게 작음, 단위 확인")
        if b.get("h", 4.0) is None or b.get("h", 4.0) <= 0:
            r.E(f"[{bid}] 높이 h={b.get('h')} 가 양수가 아님")
        cl = b.get("clearance", 0.0)
        if cl is None or cl < 0:
            r.E(f"[{bid}] clearance 가 음수이거나 null")
        elif cl == 0 and b.get("kind", "equipment") == "equipment" and not b.get("pinned"):
            r.W(f"[{bid}] clearance=0 — 유지보수/접근 이격이 정말 불필요한지 확인 "
                f"(IAEA SSR-2/1 Req.29 는 정비 접근성을 안전요건으로 규정)")
        n = int(b.get("count", 1) or 1)
        if n < 1:
            r.E(f"[{bid}] count={n}")

    for x in sorted(dup):
        r.E(f"중복 id: {x}")

    # ---- 2. 참조 무결성 ---------------------------------------------------
    def known(name) -> bool:
        if name in ids:
            return True
        base = str(name).rsplit("-", 1)[0]
        return base in ids and base != name

    for l in d.get("links", []) or []:
        for side in ("a", "b"):
            if not known(l.get(side, "")):
                r.E(f"link 참조 오류: '{l.get(side)}' 는 정의되지 않은 블록")
        if (l.get("cost", 1) or 0) <= 0:
            r.W(f"link {l.get('a')}-{l.get('b')} 의 cost <= 0")

    zones = {b.get("zone") for b in d["blocks"]}
    for s in d.get("separations", []) or []:
        if s.get("by_zone"):
            for side in ("a", "b"):
                if s.get(side) not in zones:
                    r.E(f"separation zone 참조 오류: '{s.get(side)}'")
        else:
            for side in ("a", "b"):
                if not known(s.get(side, "")):
                    r.E(f"separation 참조 오류: '{s.get(side)}'")
        md = s.get("min_dist")
        if md is None or md <= 0:
            r.E(f"separation {s.get('a')}-{s.get('b')}: min_dist 부적정({md})")
        elif md > 100:
            r.W(f"separation {s.get('a')}-{s.get('b')}: min_dist={md} m — 과대 의심")
        if not s.get("basis"):
            r.W(f"separation {s.get('a')}-{s.get('b')}: basis(근거 조항) 미기재 — "
                f"인용 없는 수치는 IR 에 들어가면 안 된다")

    for e in d.get("elevations", []) or []:
        for side in ("above", "below"):
            if not known(e.get(side, "")):
                r.E(f"elevation 참조 오류: '{e.get(side)}'")

    # ---- 3. 제약 라이브러리 선언 검증 --------------------------------------
    seen_cids = set()
    for c in d.get("constraints", []) or []:
        cid = c.get("id")
        if cid not in CONS.REGISTRY:
            r.E(f"알 수 없는 제약 id: {cid!r}. 사용 가능: "
                f"{', '.join(sorted(CONS.REGISTRY))}")
            continue
        if cid in seen_cids:
            r.W(f"제약 {cid} 가 중복 선언됨 — 뒤의 것도 함께 평가된다")
        seen_cids.add(cid)
        cd = CONS.REGISTRY[cid]
        pr = c.get("params") or {}
        unknown = {k for k in pr if not k.startswith("_")} - set(cd.params)
        if unknown:
            r.W(f"[{cid}] 정의되지 않은 파라미터 {sorted(unknown)} — 무시된다")
        for k, meta in cd.params.items():
            if meta["required"] and pr.get(k, meta["default"]) is None:
                r.E(f"[{cid}] 필수 파라미터 '{k}' 가 비어 있음(null). "
                    f"{meta['note'] or '값은 인용 가능한 출처에서 사람이 입력해야 한다.'}")
        if cid == "SEISMIC-GAP":
            for st in (pr.get("structures") or []):
                if st.get("delta_max_m") is None:
                    r.E(f"[SEISMIC-GAP] 구조물 '{st.get('id')}' 의 delta_max_m 이 null. "
                        f"δ 는 구조해석 결과에서 온다 — 고정 수치를 쓰면 안 된다.")
        if cid == "LOAD-PATH":
            for p in (pr.get("paths") or []):
                if p.get("width_m") is None:
                    r.E(f"[LOAD-PATH] 경로 '{p.get('id')}' 의 width_m 이 null")

    # ---- 3b. ★ 사용금지 수치 감시 ------------------------------------------
    fe, fw = CONS.scan_forbidden(d)
    for m in fe:
        r.E(m)
    for m in fw:
        r.W(m)

    # ---- 4. 기하학적 실현 가능성 (사전 진단) --------------------------------
    nf = int(d.get("n_floors", 1) or 1)
    mw, md_ = d.get("max_w"), d.get("max_d")
    occ = void_area = 0.0
    for b in d["blocks"]:
        n = int(b.get("count", 1) or 1)
        c = b.get("clearance", 0.0) or 0.0
        a = n * ((b.get("w", 0) or 0) + 2 * c) * ((b.get("d", 0) or 0) + 2 * c)
        if b.get("kind") == "void":
            void_area += a            # 배치 금지 영역은 점유가 아니라 가용면적 차감
        else:
            occ += a
    if mw and md_:
        cap = mw * md_ * nf - void_area
        util = occ / cap if cap else 9.9
        r.I(f"점유면적 {occ:,.0f} m² / 가용면적 {cap:,.0f} m² = 이용률 {util*100:.1f}%")
        if util > 1.0:
            r.E(f"실현 불가: 필요 면적이 max_w×max_d×n_floors 를 초과 ({util*100:.0f}%)")
        elif util > 0.85:
            r.W(f"이용률 {util*100:.0f}% — 통로/계단/샤프트 여유가 없다. "
                f"해가 없거나 매우 나쁠 수 있음")
        elif util < 0.25:
            r.W(f"이용률 {util*100:.0f}% — 제약이 느슨하다. 결과가 자명해질 수 있음")
        for b in d["blocks"]:
            c = b.get("clearance", 0.0) or 0.0
            bw, bd = (b.get("w", 0) or 0) + 2 * c, (b.get("d", 0) or 0) + 2 * c
            if min(bw, bd) > min(mw, md_) or max(bw, bd) > max(mw, md_):
                r.E(f"[{b.get('id')}] 단일 블록이 외피 한계보다 큼 "
                    f"({bw:.1f}×{bd:.1f} vs {mw}×{md_})")

    # 공지 제약과 면적의 양립 가능성
    fa = next((c for c in (d.get("constraints") or []) if c.get("id") == "FREE-AREA"),
              None)
    if fa and mw and md_:
        pr = fa.get("params") or {}
        g0 = pr.get("ground_floor_free_ratio")
        if g0 is not None and nf >= 1:
            usable = mw * md_ * ((1 - g0) + max(0, nf - 1))
            if occ > usable:
                r.E(f"실현 불가: FREE-AREA 1층 공지 {g0*100:.0f}% 를 반영하면 "
                    f"가용 {usable:,.0f} m² < 점유 {occ:,.0f} m²")

    # 층고 vs 기기 높이 → 다층 관통
    fh = d.get("floor_heights", []) or []
    tall = []
    if fh:
        if len(fh) != nf:
            r.E(f"floor_heights 길이({len(fh)}) != n_floors({nf})")
        for b in d["blocks"]:
            f = b.get("floor")
            h = b.get("h", 0) or 0
            if f is not None and 0 <= f < len(fh) and h > fh[f]:
                tall.append(b.get("id"))
                acc, k = 0.0, 0
                while acc < h - 1e-9 and k < 2 * nf + 2:
                    acc += fh[f + k] if f + k < nf else fh[-1]
                    k += 1
                if f + k > nf:
                    r.E(f"[{b.get('id')}] 높이 {h} m 가 기준층 {f+1} 에서 "
                        f"{k}개 층을 관통해야 하는데 건물이 {nf}층뿐이다 "
                        f"(지붕을 뚫는 기기)")
            elif f is None and h > max(fh):
                tall.append(b.get("id"))
    if tall and "TALL-EQUIP" not in seen_cids:
        r.W(f"층고보다 높은 블록 {len(tall)}개({', '.join(sorted(set(tall))[:6])}) 가 "
            f"있는데 TALL-EQUIP 제약이 선언되지 않았다 — 관통부 정렬이 검증되지 않는다")

    # ---- 4b. ★ 빈 공간을 요구하는 이격 -------------------------------------
    # Sequence-Pair 패킹은 '빈 공간을 만들지 않는다'. 압축 배치가 항상 생성되므로,
    # 큰 이격을 실현하려면 그 사이를 채우는 실체(통로·차폐벽·공지 블록)가 IR 에
    # 있어야 한다. 없으면 최적화기는 페널티를 내고 붙여 놓는 해를 낸다.
    fills = []
    for b in d["blocks"]:
        c = b.get("clearance", 0.0) or 0.0
        fills.append(max((b.get("w", 0) or 0) + 2 * c, (b.get("d", 0) or 0) + 2 * c))
    fill_max = max(fills) if fills else 0.0
    big = []
    for s_ in (d.get("separations") or []):
        if s_.get("hard", True) and (s_.get("min_dist") or 0) > fill_max:
            big.append(f"{s_.get('a')}↔{s_.get('b')} {s_.get('min_dist')} m")
    for c in (d.get("constraints") or []):
        pr = c.get("params") or {}
        for k in ("min_dist_m", "min_horizontal_m"):
            v = pr.get(k)
            if v is not None and v > fill_max:
                big.append(f"{c.get('id')}.{k} {v} m")
    if big:
        r.W("Sequence-Pair 패킹은 빈 공간을 만들지 않는다. 다음 이격 요구는 어떤 단일 "
            "블록보다도 크므로(최대 블록 치수 {:.1f} m), 사이를 채우는 실체(통로·차폐벽·"
            "공지 블록)를 IR 에 명시하지 않으면 해를 못 찾을 수 있다: {}"
            .format(fill_max, ", ".join(big[:6])))

    # ---- 4c. 고정 장애물(pinned) · 도면 입력 --------------------------------
    if d.get("preset") not in (None, "balanced", "space_first"):
        r.E(f"알 수 없는 preset {d.get('preset')!r} — balanced | space_first")
    pins = []
    for b in d["blocks"]:
        bid = b.get("id")
        if b.get("kind") == "void" and not b.get("pinned"):
            r.E(f"[{bid}] kind=void 는 pinned=true 여야 한다 (배치 금지 영역은 움직이지 않는다)")
        if not b.get("pinned"):
            continue
        if int(b.get("count", 1) or 1) != 1:
            r.E(f"[{bid}] pinned 블록은 count=1 이어야 한다")
        fx = b.get("fixed_xy")
        if not fx or len(fx) != 2:
            r.E(f"[{bid}] pinned=true 인데 fixed_xy(중심 좌표)가 없다 — 좌표는 도면 파서가 "
                f"쓴다. LLM 이 지어내지 말 것")
            continue
        if b.get("floor") is None:
            r.E(f"[{bid}] pinned=true 인데 floor 가 없다")
            continue
        c = b.get("clearance", 0.0) or 0.0
        w, dd = (b.get("w", 0) or 0) + 2 * c, (b.get("d", 0) or 0) + 2 * c
        x0, y0 = fx[0] - w / 2, fx[1] - dd / 2
        pins.append((bid, int(b["floor"]), x0, y0, x0 + w, y0 + dd))
        if x0 < -1e-6 or y0 < -1e-6:
            r.E(f"[{bid}] 고정 장애물이 원점 (0,0) 밖으로 나가 있다 ({x0:.2f}, {y0:.2f})")
        if mw and x0 + w > mw + 1e-6:
            r.E(f"[{bid}] 고정 장애물이 max_w={mw} 를 넘는다 (x 끝 {x0 + w:.2f})")
        if md_ and y0 + dd > md_ + 1e-6:
            r.E(f"[{bid}] 고정 장애물이 max_d={md_} 를 넘는다 (y 끝 {y0 + dd:.2f})")
    for k in range(len(pins)):
        for m in range(k + 1, len(pins)):
            a, b2 = pins[k], pins[m]
            if a[1] != b2[1]:
                continue
            ox = min(a[4], b2[4]) - max(a[2], b2[2])
            oy = min(a[5], b2[5]) - max(a[3], b2[3])
            if ox > 1e-6 and oy > 1e-6:
                r.E(f"고정 장애물 {a[0]} 와 {b2[0]} 가 {a[1]+1}층에서 겹친다 "
                    f"({ox:.2f}×{oy:.2f} m) — 하나를 고정 해제하거나 도면을 확인할 것")
    if pins:
        r.I(f"고정 장애물(pinned) {len(pins)}개 — 나머지 블록은 이를 피해 배치된다")
    src = d.get("drawing_source") or []
    if src:
        r.I("원본 도면: " + ", ".join(f"{s_.get('filename')} ({s_.get('format')}, "
                                      f"{str(s_.get('sha256',''))[:12]}…)" for s_ in src))

    # ---- 5. 제약 간 모순 ---------------------------------------------------
    seps = {(s.get("a"), s.get("b")): s.get("min_dist")
            for s in (d.get("separations") or []) if not s.get("by_zone")}
    for l in (d.get("links") or []):
        k1, k2 = (l.get("a"), l.get("b")), (l.get("b"), l.get("a"))
        md2 = seps.get(k1) or seps.get(k2)
        if md2 and md2 > 10:
            r.I(f"'{l.get('a')}'-'{l.get('b')}' 는 연결(배관)이면서 {md2} m 이격 요구 "
                f"— 의도된 것인지 확인(예: 전기적 분리 계열)")
    for e in (d.get("elevations") or []):
        a_, b_ = ids.get(e.get("above")), ids.get(e.get("below"))
        if a_ and b_ and a_.get("floor") is not None and b_.get("floor") is not None:
            if a_["floor"] <= b_["floor"]:
                r.E(f"표고 제약 모순: {e['above']}(층 {a_['floor']}) 이 "
                    f"{e['below']}(층 {b_['floor']}) 보다 위여야 하는데 층 고정값이 반대")

    # 화재구역 vs 계열 — FIRE-20FT 를 선언했는데 fire_zone 이 없으면 FIRE-HATCH 가 무의미
    has_fz = any(b.get("fire_zone") for b in d["blocks"])
    if "FIRE-HATCH" in seen_cids and not has_fz:
        r.E("FIRE-HATCH 를 선언했으나 fire_zone 이 지정된 블록이 없다 — "
            "병합 판정 대상이 없어 항상 0 을 반환한다(조용한 실패)")
    if "FIRE-20FT" in seen_cids and "FIRE-HATCH" not in seen_cids:
        r.W("FIRE-20FT 만 선언되고 FIRE-HATCH 가 없다 — RG 1.189 Rev.5 는 "
            "대형 미방호 개구부(해치·계단)가 있으면 양쪽을 하나의 화재구역으로 "
            "간주한다. 반출 해치가 있는 배치에서 이 검사를 빼면 분리 요건이 "
            "충족된 것처럼 보인다")
    if any(b.get("opening") for b in d["blocks"]) and "FIRE-HATCH" not in seen_cids:
        r.W("opening=true 인 블록이 있는데 FIRE-HATCH 가 선언되지 않았다")

    # 터빈
    if any(b.get("turbine") for b in d["blocks"]) and "TURBINE-25" not in seen_cids:
        r.W("turbine=true 인 블록이 있는데 TURBINE-25 가 선언되지 않았다 — "
            "미사일 금지 부채꼴이 평가되지 않는다")

    # ---- 6. 가중치 ---------------------------------------------------------
    w = d.get("weights", {}) or {}
    if not w:
        r.W("weights 미지정 — 기본값 사용. 다목적 문제에서 가중치는 설계 판단 "
            "사항이므로 반드시 사람이 승인해야 한다")
    for k, v in w.items():
        if v is None or v < 0:
            r.E(f"weights['{k}'] 가 음수이거나 null")

    # ---- 7. 커버리지 -------------------------------------------------------
    linked = set()
    for l in (d.get("links") or []):
        linked.add(l.get("a")); linked.add(l.get("b"))
    movable = {k for k, b in ids.items() if b.get("kind") not in ("void", "obstacle")}
    orphan = [k for k in ids if k not in linked and k in movable]
    if orphan:
        r.W(f"연결이 하나도 없는 블록 {len(orphan)}개: {', '.join(sorted(orphan)[:8])}"
            f"{' …' if len(orphan) > 8 else ''} — 목적함수의 연결항이 이들을 안 본다")
    nozone = [k for k, b in ids.items() if not b.get("zone") and k in movable]
    if nozone:
        r.W(f"zone 미지정 블록 {len(nozone)}개: {', '.join(sorted(nozone)[:8])} "
            f"— 방사선구역 분산항에서 제외된다")

    # ---- 8. ★ 가중치 자동 점검 (동적) ---------------------------------------
    if probe and not r.ERR:
        try:
            _probe_weights(d, r)
        except Exception as ex:                       # noqa: BLE001
            r.W(f"가중치 동적 점검 실패(구조는 유효): {type(ex).__name__}: {ex}")

    return r


def _probe_weights(d: dict, r: Report):
    """
    ★ 가중치 자동 점검 — 'hard 제약 위반이 비용을 내고 넘어갈 수 있는 옵션' 인지 본다.

    함정(보고서 02장): hard 제약의 가중치가 목적항 대비 충분히 크지 않으면
    최적화기는 "위반 페널티를 내고 체적을 줄이는" 해를 정상적인 최적해로 반환한다.
    프로토타입에서 실제로 발생했다 — 크레인 가중치를 10배 올리기 전까지 최적해가
    크레인 베이를 1.9 m 벗어나 있었다. 도면만 봐서는 알아채기 어렵다.

    비교는 '총량' 이 아니라 '한계이득' 으로 한다. 단위가 맞아야 하기 때문이다.
      1 m 를 양보해서 얻는 이득 ref_m  = w_volume·max(D·H, W·H) + w_area·max(W,D)
                                        + w_link·max_i Σ_j c_ij
      1 m² 를 양보해서 얻는 이득 ref_m2 = w_volume·H + w_area
    hard 페널티 가중치가 ref 의 10배 미만이면 경고한다.
    """
    import ga_core as G
    sp = G.spec_from_dict(d)
    base = G.baseline(sp)
    F, terms, geo, env, viols, _ = base.evaluate(detail=True)
    W, D, H = env
    deg: Dict[str, float] = {}
    for lk in sp.links:
        deg[lk.a] = deg.get(lk.a, 0.0) + lk.cost
        deg[lk.b] = deg.get(lk.b, 0.0) + lk.cost
    ref_m = (sp.wt("volume") * max(D * H, W * H) + sp.wt("area") * max(W, D)
             + sp.wt("link") * (max(deg.values()) if deg else 0.0))
    ref_m2 = sp.wt("volume") * H + sp.wt("area")
    r.I(f"기준안 F={F:,.0f}, 외피 {W:.1f}×{D:.1f}×{H:.1f} m")
    r.I(f"한계이득: 1 m 양보 = {ref_m:,.0f}, 1 m² 양보 = {ref_m2:,.0f} "
        f"→ hard 페널티 가중치 권장 하한 {ref_m*10:,.0f} (m), {ref_m2*10:,.0f} (m²)")
    if ref_m <= 0:
        return
    for k in G.HARD_TERMS:
        wv = sp.wt(k)
        if wv < ref_m * 10:
            r.W(f"hard 항 '{k}' 가중치 {wv:,.0f} < 권장 하한 {ref_m*10:,.0f} — "
                f"1 m 위반이 '비용을 내고 넘어갈 수 있는' 옵션이 된다")
    for c in (d.get("constraints") or []):
        cid = c.get("id")
        cd = CONS.REGISTRY.get(cid)
        if not cd or not cd.hard:
            continue
        wv = sp.weights.get(f"constraint.{cid}", c.get("weight", cd.default_weight))
        area_unit = cid in ("FREE-AREA", "LOAD-PATH")
        lim = (ref_m2 if area_unit else ref_m) * 10
        if wv < lim:
            r.W(f"hard 제약 '{cid}' 가중치 {wv:,.0f} < 권장 하한 {lim:,.0f} "
                f"(위반량 단위 {'m²' if area_unit else 'm'})")


# ---------------------------------------------------------------------------

def print_report(r: Report, ir: dict = None) -> int:
    print("=" * 74)
    print("  DESIGN INTENT IR 검증 결과  (GATE 1 — 결정론적, LLM 미사용)")
    print("=" * 74)
    for m in r.INFO:
        print(f"  [INFO ] {m}")
    for m in r.WARN:
        print(f"  [WARN ] {m}")
    for m in r.ERR:
        print(f"  [ERROR] {m}")
    print("-" * 74)
    print(f"  ERROR {len(r.ERR)} · WARN {len(r.WARN)} · INFO {len(r.INFO)}")
    if r.ERR:
        print("  ▶ GATE 1 불통과 — 최적화 진행 금지. 위 ERROR 를 사람이 해소할 것.")
        return 1
    print("  ▶ GATE 1 통과 — WARN 항목은 담당 엔지니어 확인 후 진행.")
    if ir is not None:
        print(f"  ▶ IR SHA-256 = {irhash.sha256_obj(ir)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Design Intent IR 검증 (Gate 1)")
    ap.add_argument("spec")
    ap.add_argument("--freeze", action="store_true",
                    help="통과 시 IR 을 <spec>.frozen.json 으로 동결하고 해시를 기록")
    ap.add_argument("--json", action="store_true", help="기계판독 결과 출력")
    ap.add_argument("--no-probe", action="store_true", help="가중치 동적 점검 생략")
    a = ap.parse_args(argv)

    with open(a.spec, encoding="utf-8") as fp:
        ir = json.load(fp)
    r = check_ir(ir, probe=not a.no_probe)

    if a.json:
        h = irhash.sha256_obj(ir)
        print(json.dumps(dict(errors=r.ERR, warnings=r.WARN, info=r.INFO,
                              passed=not r.ERR, ir_sha256=h,
                              engine_version=ENGINE_VERSION,
                              ir_schema_version=IR_SCHEMA_VERSION),
                         ensure_ascii=False, indent=2))
        return 1 if r.ERR else 0

    code = print_report(r, ir)
    if code == 0 and a.freeze:
        h = irhash.sha256_obj(ir)
        out = os.path.splitext(a.spec)[0] + ".frozen.json"
        frozen = dict(ir)
        frozen["_frozen"] = dict(ir_sha256=h, engine_version=ENGINE_VERSION,
                                 ir_schema_version=IR_SCHEMA_VERSION)
        with open(out, "w", encoding="utf-8") as fp:
            json.dump(frozen, fp, ensure_ascii=False, indent=2)
        with open(out + ".sha256", "w", encoding="utf-8") as fp:
            fp.write(h + "\n")
        print(f"  ▶ 동결: {out}")
        print(f"  ▶ 해시: {out}.sha256")
        print("  ▶ 이후 전 과정은 이 해시로부터 LLM 호출 0회로 재현 가능해야 한다.")
    return code


if __name__ == "__main__":
    sys.exit(main())
