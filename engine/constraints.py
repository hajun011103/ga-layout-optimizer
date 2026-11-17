# -*- coding: utf-8 -*-
"""
constraints.py — 사전 검증된 제약조건 라이브러리 (원전 고유)

설계 원칙 (보고서 05장 불변식 3)
--------------------------------
LLM은 이 파일의 제약을 **선택하고 파라미터만 채운다.** 수식을 발명하지 않는다.
FlairGPT / LLM-FuncMapper 가 채택한 "사전 정의 비용함수 라이브러리에 매핑" 구조이며,
원자력 적용에서 가장 이식 가치가 큰 설계 결정이다.

각 제약은 다음을 갖는다.
  id        : 안정적인 식별자 (IR·도면·추적성 표에서 그대로 쓰인다)
  name      : 사람이 읽는 이름
  basis     : 근거 조항 (원문 확인된 것만)
  params    : 파라미터 정의. required=True 이고 값이 없으면 Gate 1 에서 ERROR.
  hard      : 기본 처리 층위
  formula   : 수식 요약 (문서 자동생성용)
  evaluate  : (ctx, params) -> List[Violation]

★ 규제 수치에 대한 금지사항은 references/regulatory-numbers.md 와
  이 파일 하단의 FORBIDDEN_VALUES 에 동시에 있다. 한쪽만 고치지 말 것.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from geometry import (axis_gaps, dist_to_boundary, gap, l_corridors,
                      overlap_area, rect_center, rect_corners, rect_of,
                      sector_intrusion)


# ---------------------------------------------------------------------------
# 위반 기록
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    """
    제약 위반 1건.

    amount 는 목적함수에 들어가는 스칼라이며 단위가 섞이면 안 되므로
    unit 을 함께 들고 다닌다. 가중치는 spec.weights['constraint.<ID>'] 에서 온다.
    """
    cid: str
    detail: str
    amount: float
    unit: str = "m"                       # m | m2 | count
    hard: bool = True
    blocks: List[str] = field(default_factory=list)
    marker: Optional[dict] = None         # 도면에 빨간색으로 찍을 위치 정보

    def __str__(self) -> str:
        return f"[{self.cid}] {self.detail} — 위반량 {self.amount:,.3f} {self.unit}"


@dataclass
class ConstraintDef:
    id: str
    name: str
    basis: str
    formula: str
    params: Dict[str, dict]
    evaluate: Callable[[Any, dict], List[Violation]]
    hard: bool = True
    default_weight: float = 500.0
    note: str = ""


REGISTRY: Dict[str, ConstraintDef] = {}


def register(cd: ConstraintDef) -> ConstraintDef:
    if cd.id in REGISTRY:
        raise ValueError(f"중복 제약 id: {cd.id}")
    REGISTRY[cd.id] = cd
    return cd


def P(unit: str, default=None, required: bool = False, note: str = "") -> dict:
    return dict(unit=unit, default=default, required=required, note=note)


# ---------------------------------------------------------------------------
# 평가 컨텍스트
# ---------------------------------------------------------------------------

class EvalContext:
    """
    제약 평가에 필요한 것만 담은 읽기 전용 뷰.

    geo[i]      : 블록 i 의 기준층 배치 dict(x,y,w,d,f,z,cx,cy)
    shadows[(i,f)] : 다층 관통 기기의 상위층 그림자 배치 dict(x,y,w,d,f)
    env         : (W, D, H)
    """

    def __init__(self, spec, geo: Dict[int, dict], env, shadows: Dict = None):
        self.spec = spec
        self.geo = geo
        self.env = env
        self.W, self.D, self.H = env
        self.shadows = shadows or {}
        self.idx = {b.id: i for i, b in enumerate(spec.blocks)}
        self._params_by_cid = {c.get("id"): (c.get("params") or {})
                               for c in getattr(spec, "constraints", [])}

    # -- 선택자 ------------------------------------------------------------
    def resolve(self, sel) -> List[int]:
        """
        선택자 -> 블록 인덱스 목록.

          "SIP-1"          블록 id (count 전개 전 원본 id 도 허용: 'SIP' -> SIP-1, SIP-2)
          "zone:R3"        방사선/계통 구역 라벨
          "train:TrainA"   다중성 계열
          "fire_zone:FZ-1" 화재구역
          "structure:AUX"  구조물(내진 이격 단위)
          ["...", "..."]   위의 목록
        """
        if sel is None:
            return []
        if isinstance(sel, (list, tuple)):
            out: List[int] = []
            for s in sel:
                for i in self.resolve(s):
                    if i not in out:
                        out.append(i)
            return out
        s = str(sel)
        if ":" in s:
            key, val = s.split(":", 1)
            if key in ("zone", "train", "fire_zone", "structure", "vital_area",
                       "success_path"):
                return [i for i, b in enumerate(self.spec.blocks)
                        if getattr(b, key, None) == val]
            raise ValueError(f"알 수 없는 선택자 키: {key!r}")
        if s in self.idx:
            return [self.idx[s]]
        # count 로 전개된 인스턴스 (SIP -> SIP-1, SIP-2)
        pref = s + "-"
        hits = [i for i, b in enumerate(self.spec.blocks) if b.id.startswith(pref)]
        return hits

    def bid(self, i: int) -> str:
        return self.spec.blocks[i].id

    def placed(self, i: int) -> bool:
        return i in self.geo

    def rect(self, i: int):
        return rect_of(self.geo[i])

    def floor(self, i: int) -> int:
        return self.geo[i]["f"]

    def spans(self, i: int) -> List[int]:
        """블록 i 가 점유하는 층 목록 (다층 관통 포함)."""
        if i not in self.geo:
            return []
        fs = [self.geo[i]["f"]]
        fs += [f for (j, f) in self.shadows if j == i]
        return sorted(set(fs))

    def rect_on(self, i: int, f: int):
        if i in self.geo and self.geo[i]["f"] == f:
            return rect_of(self.geo[i])
        s = self.shadows.get((i, f))
        return rect_of(s) if s else None

    def params_of(self, cid: str) -> dict:
        return self._params_by_cid.get(cid, {})


def _need(params: dict, key: str, cid: str):
    v = params.get(key, REGISTRY[cid].params[key]["default"])
    if v is None:
        raise MissingParam(cid, key)
    return v


class MissingParam(Exception):
    def __init__(self, cid, key):
        self.cid, self.key = cid, key
        super().__init__(f"[{cid}] 필수 파라미터 '{key}' 가 비어 있음(null). "
                         f"값은 인용 가능한 출처에서 사람이 입력해야 한다.")


def _get(params: dict, key: str, cid: str):
    if key in params and params[key] is not None:
        return params[key]
    return REGISTRY[cid].params[key]["default"]


# ---------------------------------------------------------------------------
# FIRE-20FT — 10 CFR 50 App. R III.G.2 화재 분리
# ---------------------------------------------------------------------------

def _eval_fire_20ft(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "FIRE-20FT"
    groups = _need(p, "groups", cid)
    dmin = _get(p, "min_horizontal_m", cid)
    metric = _get(p, "gap_metric", cid)
    comb_free = _need(p, "combustible_free", cid)
    detection = _need(p, "detection_and_suppression", cid)
    barrier_pairs = _get(p, "barrier_pairs", cid) or []

    bset = set()
    for a, b in barrier_pairs:
        bset.add(frozenset((a, b)))

    # 계열 -> 블록 인덱스
    gidx = [ctx.resolve(g) for g in groups]
    out: List[Violation] = []
    for gi in range(len(gidx)):
        for gj in range(gi + 1, len(gidx)):
            for i in gidx[gi]:
                for j in gidx[gj]:
                    if not (ctx.placed(i) and ctx.placed(j)):
                        continue
                    if frozenset((ctx.bid(i), ctx.bid(j))) in bset:
                        continue          # 3시간 내화방벽으로 III.G.2(a) 만족
                    # 서로 다른 층은 내화 바닥슬래브로 분리 — FIRE-HATCH 가 병합 여부를 본다
                    if not set(ctx.spans(i)) & set(ctx.spans(j)):
                        continue
                    f = sorted(set(ctx.spans(i)) & set(ctx.spans(j)))[0]
                    ri, rj = ctx.rect_on(i, f), ctx.rect_on(j, f)
                    if ri is None or rj is None:
                        continue
                    g = gap(ri, rj, metric)
                    if not (comb_free and detection):
                        # III.G.2(b) 의 거리 예외는 '개재 가연물 0 + 감지·자동소화'
                        # 를 함께 만족할 때만 성립한다. 선언되지 않았으면 방벽이
                        # 없는 한 거리로는 면제되지 않는다.
                        out.append(Violation(
                            cid,
                            f"{ctx.bid(i)}↔{ctx.bid(j)}: 방벽 미지정 + "
                            f"가연물0/감지소화 미선언 → III.G.2 거리 예외 불성립",
                            dmin, "m", True, [ctx.bid(i), ctx.bid(j)],
                            marker=dict(kind="pair", floor=f, a=ri, b=rj)))
                    elif g < dmin - 1e-9:
                        out.append(Violation(
                            cid,
                            f"{ctx.bid(i)}↔{ctx.bid(j)} 수평 {g:.2f} m < {dmin:.3f} m "
                            f"(층 {f+1})",
                            dmin - g, "m", True, [ctx.bid(i), ctx.bid(j)],
                            marker=dict(kind="pair", floor=f, a=ri, b=rj)))
    return out


register(ConstraintDef(
    id="FIRE-20FT",
    name="화재방호 계열 분리 (App. R III.G.2)",
    basis="10 CFR 50 Appendix R III.G.2 — 고온정지 1계열 무손상: "
          "①3시간 내화방벽 ②수평 20 ft(6.096 m) 초과 + 개재 가연물 0 + 감지·자동소화 "
          "③1시간 방벽 + 감지·자동소화 중 하나.",
    formula="Σ_{(i,j)∈성공경로 계열쌍} max(0, d_min − gap_ij)  [i,j 동일 화재구역·동일 층]",
    params=dict(
        groups=P("list[list[selector]]", None, True,
                 "성공경로 계열 그룹. 서로 다른 그룹 간에 분리를 요구한다."),
        min_horizontal_m=P("m", 6.096, False,
                           "App. R 의 20 ft. ★ 이 값이 GA 의 실질 그리드 모듈이며 "
                           "격자 해상도 기본값이다."),
        gap_metric=P("enum", "euclid", False, "euclid | chebyshev | manhattan"),
        combustible_free=P("bool", None, True,
                           "개재 가연물 0 이 확인되었는가. 선언하지 않으면 거리 예외 불성립."),
        detection_and_suppression=P("bool", None, True,
                                    "화재감지 + 자동소화 설치 여부."),
        barrier_pairs=P("list[[id,id]]", [], False,
                        "3시간 내화방벽으로 분리되어 거리 요건이 면제되는 쌍."),
    ),
    evaluate=_eval_fire_20ft,
    hard=True, default_weight=800.0,
    note="RG 1.189 §6.1.9: 중복 펌프 트레인실 상호 최소 3시간 방벽 + 받침대·연석·바닥배수. "
         "EDG 일일탱크는 4,164 L 이하만 EDG실 내 허용.",
))


# ---------------------------------------------------------------------------
# FIRE-HATCH — ★ 해치·계단에 의한 화재구역 병합 (최우선 신규 구현)
# ---------------------------------------------------------------------------

class _UF:
    def __init__(self):
        self.p: Dict[str, str] = {}

    def find(self, a: str) -> str:
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _eval_fire_hatch(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "FIRE-HATCH"
    openings = _need(p, "openings", cid)
    sep = _get(p, "separation_m", cid)
    comb_free = _need(p, "combustible_free", cid)
    both_levels = _need(p, "both_levels_protected", cid)
    tol = _get(p, "merge_tolerance_m", cid)
    pen = _get(p, "penalty_m_per_conflict", cid)

    uf = _UF()
    for b in ctx.spec.blocks:
        if b.fire_zone:
            uf.find(b.fire_zone)

    merged_by: Dict[Tuple[str, str], str] = {}
    exempt_ok = bool(comb_free and both_levels)

    # 1) 개구부에 접한(또는 merge_tolerance 이내) 블록들의 화재구역을 병합한다.
    #    개구부가 여러 층을 관통하면 그 층들의 구역이 모두 하나로 묶인다 —
    #    이것이 RG 1.189 Rev.5 가 말하는 "양쪽을 하나의 화재구역으로 간주" 다.
    op_idx = ctx.resolve(openings)
    for oi in op_idx:
        if not ctx.placed(oi):
            continue
        anchor = ctx.spec.blocks[oi].fire_zone
        touched: List[str] = []
        for f in ctx.spans(oi):
            ro = ctx.rect_on(oi, f)
            if ro is None:
                continue
            for j in range(len(ctx.spec.blocks)):
                if j == oi or not ctx.placed(j):
                    continue
                bz = ctx.spec.blocks[j].fire_zone
                if not bz:
                    continue
                rj = ctx.rect_on(j, f)
                if rj is None:
                    continue
                if gap(ro, rj, "euclid") <= tol + 1e-9:
                    touched.append(bz)
        seq = ([anchor] if anchor else []) + touched
        for k in range(1, len(seq)):
            a, b = seq[0], seq[k]
            if a and b and uf.find(a) != uf.find(b):
                uf.union(a, b)
                merged_by[(min(a, b), max(a, b))] = ctx.bid(oi)

    # 병합 결과로 무효화된 '서로 다른 화재구역' 요구를 찾는다
    req: List[Tuple[int, int]] = []
    f20 = ctx.params_of("FIRE-20FT")
    groups = f20.get("groups") or []
    gidx = [ctx.resolve(g) for g in groups]
    for a in range(len(gidx)):
        for b in range(a + 1, len(gidx)):
            for i in gidx[a]:
                for j in gidx[b]:
                    req.append((i, j))
    for pair in (_get(p, "required_distinct", cid) or []):
        ia, ib = ctx.resolve(pair[0]), ctx.resolve(pair[1])
        for i in ia:
            for j in ib:
                req.append((i, j))

    out: List[Violation] = []
    seen = set()
    for i, j in req:
        zi = ctx.spec.blocks[i].fire_zone
        zj = ctx.spec.blocks[j].fire_zone
        if not zi or not zj or zi == zj:
            continue
        if uf.find(zi) != uf.find(zj):
            continue
        # 2) RG 1.189 Rev.5 예외 — 성공경로 간 누적 수평거리 6.1 m 이상 +
        #    개재 가연물 0 + 양쪽 층 감지·소화를 모두 만족하면 병합에도 불구하고
        #    분리로 인정한다. 셋 중 하나라도 선언되지 않았으면 예외는 성립하지 않는다.
        if exempt_ok and ctx.placed(i) and ctx.placed(j):
            common = sorted(set(ctx.spans(i)) & set(ctx.spans(j)))
            if common:
                ri, rj = ctx.rect_on(i, common[0]), ctx.rect_on(j, common[0])
                if ri and rj and gap(ri, rj, "euclid") >= sep - 1e-9:
                    continue
            else:
                continue
        key = (min(zi, zj), max(zi, zj))
        if key in seen:
            continue
        seen.add(key)
        who = merged_by.get(key, "개구부")
        rr = ctx.rect_on(i, ctx.floor(i))
        out.append(Violation(
            cid,
            f"화재구역 {zi} 와 {zj} 가 '{who}'(대형 미방호 개구부)에 의해 하나의 "
            f"화재구역으로 병합됨 → {ctx.bid(i)}↔{ctx.bid(j)} 분리 요구 무효",
            pen, "m", True, [ctx.bid(i), ctx.bid(j), who],
            marker=dict(kind="merge", floor=ctx.floor(i), a=rr)))
    return out


register(ConstraintDef(
    id="FIRE-HATCH",
    name="★ 해치·계단에 의한 화재구역 병합",
    basis="RG 1.189 Rev.5 — 벽·바닥에 해치·계단 같은 대형 미방호 개구부가 있으면 "
          "양쪽을 하나의 화재구역으로 간주한다. 예외는 성공경로 간 누적 수평거리 "
          "6.1 m + 개재 가연물 0 + 양쪽 층 감지·소화를 모두 만족할 때뿐.",
    formula="zones' = union-find(zones, 개구부 인접관계);  "
            "Σ_{(i,j)∈분리요구} penalty · 1[find(z_i)=find(z_j)]",
    params=dict(
        openings=P("list[selector]", None, True,
                   "대형 미방호 개구부 블록(기기반출 해치·계단실 등)."),
        separation_m=P("m", 6.096, False, "RG 1.189 예외의 누적 수평거리."),
        combustible_free=P("bool", None, True, "개구부 주변 개재 가연물 0 여부."),
        both_levels_protected=P("bool", None, True, "양쪽 층 감지·자동소화 설치 여부."),
        merge_tolerance_m=P("m", 0.0, False, "개구부와 이 거리 이내면 접한 것으로 본다."),
        required_distinct=P("list[[selector,selector]]", [], False,
                            "FIRE-20FT groups 외에 추가로 서로 다른 화재구역이어야 하는 쌍."),
        penalty_m_per_conflict=P("m/건", 25.0, False,
                                 "무효화된 분리 요구 1건당 등가 위반량. "
                                 "물리량이 아니라 스칼라화 계수이므로 감사 시 명시할 것."),
    ),
    evaluate=_eval_fire_hatch,
    hard=True, default_weight=800.0,
    note="SMR 모듈 반출로는 화재구역 설계와 '동시에' 풀어야 한다. 해치를 독립 블록으로 "
         "두고 나중에 배치하는 방식으로는 이 충돌이 잡히지 않는다.",
))


# ---------------------------------------------------------------------------
# TURBINE-25 — 터빈 미사일 금지 부채꼴
# ---------------------------------------------------------------------------

def _eval_turbine(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "TURBINE-25"
    tb = _need(p, "turbine_block", cid)
    prot = _need(p, "protected", cid)
    ha = _get(p, "half_angle_deg", cid)
    rng = _get(p, "range_m", cid)
    others = _get(p, "other_units", cid) or []

    ti = ctx.resolve(tb)
    out: List[Violation] = []
    origins: List[Tuple[Tuple[float, float], float, str]] = []
    for i in ti:
        if not ctx.placed(i):
            continue
        az = ctx.turbine_azimuth.get(i, _get(p, "azimuth_deg", cid) or 0.0)
        origins.append((rect_center(ctx.rect(i)), az, ctx.bid(i)))
    for u in others:
        origins.append(((float(u["x"]), float(u["y"])), float(u["azimuth_deg"]),
                        str(u.get("id", "장래호기"))))

    for j in ctx.resolve(prot):
        if not ctx.placed(j) or j in ti:
            continue
        rj = ctx.rect(j)
        for (o, az, tag) in origins:
            v = sector_intrusion(rj, o, az, ha, rng, bidirectional=True)
            if v > 1e-9:
                out.append(Violation(
                    cid,
                    f"{ctx.bid(j)} 가 {tag} 의 미사일 금지 부채꼴(±{ha:g}°, "
                    f"방위 {az:g}°) 안에 있음",
                    v, "m", True, [ctx.bid(j), tag],
                    marker=dict(kind="rect", floor=ctx.floor(j), a=rj)))
    return out


register(ConstraintDef(
    id="TURBINE-25",
    name="터빈 미사일 금지 부채꼴",
    basis="RG 1.115 — 저궤도 터빈 미사일은 터빈 휠 평면에서 ±25°로 벌어지는 "
          "부채꼴에 집중된다. 안전목표 P4 < 1e-7/년/호기. 부지 내 현재 및 장래의 "
          "모든 호기 strike zone 을 중첩해 검토해야 한다.",
    formula="Σ_{j∈보호대상} Σ_{터빈} arc_deficit(j, origin, axis, ±25°)  "
            "[방위각은 이산 결정변수]",
    params=dict(
        turbine_block=P("selector", None, True, "터빈 블록."),
        protected=P("list[selector]", None, True,
                    "부채꼴에서 배제해야 하는 안전 관련 블록/구역."),
        half_angle_deg=P("deg", 25.0, False, "RG 1.115 저궤도 미사일."),
        azimuth_deg=P("deg", None, False,
                      "고정 방위각. null 이면 allowed_azimuths_deg 중에서 최적화한다."),
        allowed_azimuths_deg=P("list[deg]", None, False,
                               "이산 결정변수의 후보값. null 이면 15° 간격 0~165°."),
        range_m=P("m", None, False, "strike zone 유효 반경. null 이면 무제한."),
        other_units=P("list[{id,x,y,azimuth_deg}]", [], False,
                      "장래 호기 포함 부지 좌표계상의 다른 터빈."),
    ),
    evaluate=_eval_turbine,
    hard=True, default_weight=800.0,
    note="터빈 방위각은 GA 최상위 이산 결정변수다. ga_core.Solution.turbine_azimuth "
         "에 유전자로 들어가 있으며 SA/NSGA 의 'turbine_rotate' 이동으로 탐색된다.",
))


# ---------------------------------------------------------------------------
# SEISMIC-GAP — 건물 간 이격 (고정 수치 금지, 성능식)
# ---------------------------------------------------------------------------

def _struct_bbox(ctx: EvalContext, sel) -> Optional[Tuple[float, float, float, float]]:
    xs, ys, xe, ye = [], [], [], []
    for i in ctx.resolve(sel):
        if not ctx.placed(i):
            continue
        x, y, w, d = ctx.rect(i)
        xs.append(x); ys.append(y); xe.append(x + w); ye.append(y + d)
    if not xs:
        return None
    return (min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))


def _eval_seismic(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "SEISMIC-GAP"
    structs = _need(p, "structures", cid)
    tol = _need(p, "construction_tolerance_m", cid)
    boxes = []
    for s in structs:
        dm = s.get("delta_max_m")
        if dm is None:
            raise MissingParam(cid, f"structures[{s.get('id')}].delta_max_m")
        bb = _struct_bbox(ctx, s.get("blocks") or s.get("select"))
        if bb:
            boxes.append((s.get("id", "?"), bb, float(dm)))
    out: List[Violation] = []
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            (na, ba, da), (nb, bb_, db) = boxes[a], boxes[b]
            need = da + db + tol
            g = gap(ba, bb_, "euclid")
            if g < need - 1e-9:
                out.append(Violation(
                    cid,
                    f"구조물 {na}↔{nb}: 이격 {g:.3f} m < δ_max({da:g})+δ_max({db:g})"
                    f"+시공오차({tol:g}) = {need:.3f} m",
                    need - g, "m", True, [na, nb],
                    marker=dict(kind="pair", floor=0, a=ba, b=bb_)))
    return out


register(ConstraintDef(
    id="SEISMIC-GAP",
    name="구조물 간 내진 이격 (성능식)",
    basis="NUREG-0800 SRP 3.7.2 — 건물 간 이격은 고정 수치가 아니라 성능식이다. "
          "구조물 전 높이에 대해 gap_ij ≥ δ_max,i + δ_max,j + 시공오차. "
          "ITAAC 에 as-built 대 as-designed gap 확인이 포함된다.",
    formula="Σ_{(i,j)} max(0, (δ_i + δ_j + tol) − gap_ij)",
    params=dict(
        structures=P("list[{id,blocks,delta_max_m}]", None, True,
                     "구조물 단위와 각 구조물의 최대 지진 변위 δ_max. "
                     "δ 는 구조해석 결과에서 온다 — 기본값 없음."),
        construction_tolerance_m=P("m", None, True, "시공오차. 프로젝트 값."),
    ),
    evaluate=_eval_seismic,
    hard=True, default_weight=800.0,
    note="★ '50 mm' 류의 고정 수치는 NRC 규제문서에 존재하지 않는다. "
         "평면 bbox 간극으로 근사하며, 전 높이 검토는 구조해석에서 별도로 확인할 것.",
))


# ---------------------------------------------------------------------------
# HELB-DIST — 고에너지배관파단, 거리에 의한 분리
# ---------------------------------------------------------------------------

def _eval_helb(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "HELB-DIST"
    sources = _need(p, "sources", cid)
    targets = _need(p, "targets", cid)
    dmin = _need(p, "min_dist_m", cid)
    jha = _get(p, "jet_half_angle_deg", cid)
    metric = _get(p, "gap_metric", cid)

    tgt = ctx.resolve(targets)
    out: List[Violation] = []
    for s in sources:
        sel = s if isinstance(s, str) else s.get("block")
        jaz = None if isinstance(s, str) else s.get("jet_azimuth_deg")
        for i in ctx.resolve(sel):
            if not ctx.placed(i):
                continue
            ri = ctx.rect(i)
            for j in tgt:
                if j == i or not ctx.placed(j):
                    continue
                rj = ctx.rect(j)
                g = gap(ri, rj, metric)
                if g < dmin - 1e-9:
                    out.append(Violation(
                        cid,
                        f"고에너지 {ctx.bid(i)} ↔ {ctx.bid(j)}: {g:.2f} m < {dmin:g} m "
                        f"(BTP 3-3 — 배치에 의한 분리가 규제상 1순위)",
                        dmin - g, "m", True, [ctx.bid(i), ctx.bid(j)],
                        marker=dict(kind="pair", floor=ctx.floor(j), a=ri, b=rj)))
                if jaz is not None:
                    v = sector_intrusion(rj, rect_center(ri), float(jaz), jha,
                                         None, bidirectional=False)
                    if v > 1e-9:
                        out.append(Violation(
                            cid,
                            f"{ctx.bid(j)} 가 {ctx.bid(i)} 의 제트 확산 원뿔"
                            f"(반각 {jha:g}°) 안에 있음",
                            v, "m", True, [ctx.bid(i), ctx.bid(j)],
                            marker=dict(kind="rect", floor=ctx.floor(j), a=rj)))
    return out


register(ConstraintDef(
    id="HELB-DIST",
    name="고에너지배관파단 — 거리에 의한 분리",
    basis="NUREG-0800 BTP 3-3 원문: \"방호의 선호되는 방법은 플랜트 배치에 의한 "
          "분리와 격리에 기반한다\" — 거리에 의한 분리가 1순위, 구획벽 2순위, "
          "중복성 의존이 최후수단. 명문 하드 제약: 주증기·주급수 배관을 주제어실 "
          "주변에 배선하지 말 것. 제트 확산 반각 ≤ 10°.",
    formula="Σ max(0, d_min − gap) + Σ jet_cone_intrusion(target, source, ±10°)",
    params=dict(
        sources=P("list[selector|{block,jet_azimuth_deg}]", None, True,
                  "고에너지 배관/기기(주증기·주급수 등)."),
        targets=P("list[selector]", None, True, "주제어실 등 보호 대상."),
        min_dist_m=P("m", None, True,
                     "★ 규제 고정 수치가 아니다. 플랜트별 HELB 해석 결과에서 주입할 것."),
        jet_half_angle_deg=P("deg", 10.0, False, "BTP 3-3 제트 확산 반각 상한."),
        gap_metric=P("enum", "euclid", False, "euclid | chebyshev | manhattan"),
    ),
    evaluate=_eval_helb,
    hard=True, default_weight=800.0,
    note="거리 기반 목적함수가 규제 논리와 정합한다는 근거가 여기 있다.",
))


# ---------------------------------------------------------------------------
# TALL-EQUIP — 다층 관통 기기
# ---------------------------------------------------------------------------

def _eval_tall(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "TALL-EQUIP"
    tol = _get(p, "align_tolerance_m", cid)
    out: List[Violation] = []
    for (i, f), sh in sorted(ctx.shadows.items()):
        base = ctx.geo.get(i)
        if base is None:
            continue
        dx = abs(sh["x"] - base["x"])
        dy = abs(sh["y"] - base["y"])
        mis = dx + dy
        if mis > tol + 1e-9:
            out.append(Violation(
                cid,
                f"{ctx.bid(i)}: {f+1}층 관통부 정렬 오차 Δx={dx:.2f} Δy={dy:.2f} m "
                f"(기준층 {base['f']+1}층). 관통부는 전 층에서 동일 좌표여야 한다.",
                mis - tol, "m", True, [ctx.bid(i)],
                marker=dict(kind="rect", floor=f, a=rect_of(sh))))
    # 층 범위 초과
    for i, b in enumerate(ctx.spec.blocks):
        if i not in ctx.geo:
            continue
        need = ctx.tall_span.get(i, 1)
        if ctx.geo[i]["f"] + need > ctx.spec.n_floors:
            out.append(Violation(
                cid,
                f"{ctx.bid(i)}: 높이 {b.h:g} m 가 {need}개 층을 관통하는데 "
                f"기준층 {ctx.geo[i]['f']+1} 에서 건물 최상층을 벗어난다.",
                float(ctx.geo[i]["f"] + need - ctx.spec.n_floors), "count",
                True, [b.id]))
    return out


register(ConstraintDef(
    id="TALL-EQUIP",
    name="다층 관통 기기",
    basis="층고보다 높은 기기(재장전수탱크·사용후연료저장조·해치)는 연속된 층을 "
          "점유한다. 관통부는 전 층에서 같은 평면 좌표를 가져야 하며 상부 층의 "
          "다른 기기와 겹칠 수 없다.",
    formula="span_i = ceil(h_i / 층고);  Σ_{f∈span} (|Δx| + |Δy|)  [그림자 정렬오차]",
    params=dict(
        align_tolerance_m=P("m", 0.0, False, "관통부 정렬 허용오차."),
    ),
    evaluate=_eval_tall,
    hard=True, default_weight=800.0,
    note="상위 층 중첩은 표현 단계에서 배제된다 — 관통 기기를 상위 층 "
         "Sequence-Pair 에 '그림자'로 함께 넣기 때문이다. 남는 것은 정렬오차뿐이고 "
         "이 값이 0 이어야 실제로 실현 가능한 관통부다.",
))


# ---------------------------------------------------------------------------
# FREE-AREA — 공지 제약
# ---------------------------------------------------------------------------

def _floor_occupied(ctx: EvalContext, f: int, include_clearance: bool) -> float:
    tot = 0.0
    for i in range(len(ctx.spec.blocks)):
        r = ctx.rect_on(i, f)
        if r is None:
            continue
        if include_clearance:
            tot += r[2] * r[3]
        else:
            b = ctx.spec.blocks[i]
            tot += max(0.0, r[2] - 2 * b.clearance) * max(0.0, r[3] - 2 * b.clearance)
    return tot


def _eval_free_area(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "FREE-AREA"
    gr = _need(p, "ground_floor_free_ratio", cid)
    tr = _need(p, "top_floor_free_ratio", cid)
    inc = _get(p, "include_clearance", cid)
    plan = ctx.W * ctx.D
    out: List[Violation] = []
    for f, ratio, tag in ((0, gr, "1층 작업공간"),
                          (ctx.spec.n_floors - 1, tr, "최상층 비상공간")):
        if plan <= 0:
            continue
        occ = _floor_occupied(ctx, f, inc)
        allowed = plan * (1.0 - ratio)
        if occ > allowed + 1e-9:
            out.append(Violation(
                cid,
                f"{f+1}층 점유 {occ:,.0f} m² > 허용 {allowed:,.0f} m² "
                f"({tag} {ratio*100:.0f}% 확보 실패)",
                occ - allowed, "m2", True, []))
    return out


register(ConstraintDef(
    id="FREE-AREA",
    name="공지(free area) 제약",
    basis="Ejeh 2019 LNG 플랜트 예제: 1층 면적의 50%를 작업공간, 최상층 60%를 "
          "비상공간으로 비워둔다. 원전의 레이다운 구역·비상대응 공간에 대응. "
          "이 제약이 없으면 최적화기가 층을 가득 채운다.",
    formula="occupied(f) ≤ (1 − ratio_f) · W · D   for f ∈ {1층, 최상층}",
    params=dict(
        ground_floor_free_ratio=P("ratio", None, True,
                                  "★ 문헌 예제값 0.50 은 참고용이다. 프로젝트 "
                                  "레이다운/작업공간 계획에서 값을 정할 것."),
        top_floor_free_ratio=P("ratio", None, True,
                               "★ 문헌 예제값 0.60 은 참고용이다."),
        include_clearance=P("bool", True, False,
                            "점유면적에 유지보수 이격을 포함할지."),
    ),
    evaluate=_eval_free_area,
    hard=True, default_weight=20.0,
    note="단위가 m² 이므로 가중치를 m 단위 제약과 같은 크기로 주면 안 된다.",
))


# ---------------------------------------------------------------------------
# LOAD-PATH — 안전하중경로
# ---------------------------------------------------------------------------

def _eval_load_path(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "LOAD-PATH"
    paths = _need(p, "paths", cid)
    groups = _need(p, "redundant_groups", cid)
    clr = _get(p, "min_clearance_m", cid)
    out: List[Violation] = []

    for pd in paths:
        wsel, tsel = pd.get("from"), pd.get("to")
        width = pd.get("width_m")
        if width is None:
            raise MissingParam(cid, f"paths[{pd.get('id')}].width_m")
        eff_w = width + 2 * (clr or 0.0)
        fi = ctx.resolve(wsel)
        ti = ctx.resolve(tsel)
        if not fi or not ti:
            continue
        a, b = fi[0], ti[0]
        if not (ctx.placed(a) and ctx.placed(b)):
            continue
        f = ctx.floor(a)
        pa, pb = rect_center(ctx.rect(a)), rect_center(ctx.rect(b))
        best = None
        for corr in l_corridors(pa, pb, eff_w):
            touched: Dict[str, float] = {}
            for j in range(len(ctx.spec.blocks)):
                if j in (a, b):
                    continue
                rj = ctx.rect_on(j, f)
                if rj is None:
                    continue
                tr = ctx.spec.blocks[j].train
                if not tr:
                    continue
                ov = sum(overlap_area(seg, rj) for seg in corr)
                if ov > 1e-9:
                    touched[tr] = touched.get(tr, 0.0) + ov
            viol = 0.0
            hit: List[str] = []
            for grp in groups:
                inside = [(t, v) for t, v in touched.items() if t in grp]
                if len(inside) >= 2:
                    inside.sort(key=lambda kv: -kv[1])
                    viol += sum(v for _, v in inside[1:])
                    hit += [t for t, _ in inside]
            if best is None or viol < best[0]:
                best = (viol, hit, corr)
        if best and best[0] > 1e-9:
            out.append(Violation(
                cid,
                f"하중경로 '{pd.get('id', f'{ctx.bid(a)}→{ctx.bid(b)}')}' 가 "
                f"중복 계열 {sorted(set(best[1]))} 을 동시에 지난다 — "
                f"단일 낙하가 중복 트레인을 동시에 무력화할 수 있음",
                best[0], "m2", True, [ctx.bid(a), ctx.bid(b)],
                marker=dict(kind="corridor", floor=f, segs=best[2])))
    return out


register(ConstraintDef(
    id="LOAD-PATH",
    name="안전하중경로 (중량물 취급)",
    basis="NUREG-0612 §5.1.1 — 안전하중경로를 절차서와 기기배치도면에 명기하고, "
          "\"단일 낙하가 안전정지에 필요한 계통의 중복 트레인을 동시에 무력화해서는 "
          "안 된다\". SRP 9.1.5 심사 1항은 취급기기와 주요 형상의 설계 배치도를 "
          "명시적으로 요구한다.",
    formula="corridor(from→to, width) 와 교차하는 train 이 동일 중복군에서 2개 이상이면 "
            "2번째 이후 교차면적을 위반량으로 계상",
    params=dict(
        paths=P("list[{id,from,to,width_m,floor}]", None, True, "중량물 이동 경로."),
        redundant_groups=P("list[list[train]]", None, True,
                           "동시 무력화가 금지되는 계열 그룹. 예: [[\"TrainA\",\"TrainB\"]]"),
        min_clearance_m=P("m", None, False,
                          "경로 양측 추가 이격. APR1400 실적은 특정 기기 4.5 m 이내 "
                          "통과 금지였다 — 실적값이며 규제 수치가 아니다."),
    ),
    evaluate=_eval_load_path,
    hard=True, default_weight=20.0,
    note="경로는 직교(bridge+trolley) 2구간 L자 두 가지 중 위반이 작은 쪽을 취한다.",
))


# ---------------------------------------------------------------------------
# VITAL-AREA — 보안 (중요구역 집약 + 2중 방벽)
# ---------------------------------------------------------------------------

def _eval_vital(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "VITAL-AREA"
    vsel = _need(p, "vital_blocks", cid)
    maxa = _need(p, "max_areas", cid)
    standoff = _get(p, "barrier_standoff_m", cid)
    pen = _get(p, "penalty_m_per_extra_area", cid)

    vi = [i for i in ctx.resolve(vsel) if ctx.placed(i)]
    areas: Dict[str, List[int]] = {}
    unassigned: List[str] = []
    for i in vi:
        va = ctx.spec.blocks[i].vital_area
        if not va:
            unassigned.append(ctx.bid(i))
            continue
        areas.setdefault(va, []).append(i)
    out: List[Violation] = []
    if unassigned:
        out.append(Violation(
            cid, f"vital_area 미지정 중요기기 {len(unassigned)}개: "
                 f"{', '.join(unassigned[:6])} — §73.55(e)(9) 는 중요기기가 "
                 f"vital area '안에만' 있을 것을 요구한다",
            float(len(unassigned)), "count", True, unassigned))
    if len(areas) > maxa:
        out.append(Violation(
            cid, f"중요구역 {len(areas)}개 > 상한 {maxa}개 — 집약 실패 "
                 f"({', '.join(sorted(areas))})",
            pen * (len(areas) - maxa), "m", True, []))
    if standoff is not None:
        for va, idxs in sorted(areas.items()):
            for i in idxs:
                dd = dist_to_boundary(ctx.rect(i), ctx.W, ctx.D)
                if dd < standoff - 1e-9:
                    out.append(Violation(
                        cid,
                        f"{ctx.bid(i)}(VA {va}) 가 외벽에서 {dd:.2f} m — "
                        f"요구 {standoff:g} m (2중 방벽 통과 요건의 내측 배치)",
                        standoff - dd, "m", True, [ctx.bid(i)],
                        marker=dict(kind="rect", floor=ctx.floor(i), a=ctx.rect(i))))
    return out


register(ConstraintDef(
    id="VITAL-AREA",
    name="중요구역(vital area) 집약 및 2중 방벽",
    basis="10 CFR 73.55(e)(9) — \"중요기기는 vital area 안에만 위치해야 하며, "
          "접근에 최소 2중의 물리적 방벽 통과를 요구해야 한다\". "
          "(f) target set 분석이 VA 경계를 역으로 결정한다.",
    formula="penalty · max(0, |areas| − max_areas) + Σ max(0, standoff − d(i, ∂Ω))",
    params=dict(
        vital_blocks=P("list[selector]", None, True, "중요기기."),
        max_areas=P("count", None, True, "허용 중요구역 수 상한."),
        barrier_standoff_m=P("m", None, False,
                             "★ 외벽으로부터의 최소 이격. 차량폭탄 stand-off "
                             "distance 는 Safeguards Information 이므로 그 값을 "
                             "여기에 넣지 말 것. 파라미터 슬롯만 제공한다."),
        penalty_m_per_extra_area=P("m/개", 30.0, False, "스칼라화 계수."),
    ),
    evaluate=_eval_vital,
    hard=True, default_weight=200.0,
))


# ---------------------------------------------------------------------------
# ELEC-SEP — 전기계통 물리적 분리 (★ 기본값 없음. 원문 대조 강제)
# ---------------------------------------------------------------------------

def _eval_elec(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "ELEC-SEP"
    cit = _need(p, "citation", cid)
    if not re.search(r"IEEE\s*(Std\s*)?384", str(cit), re.I) and \
       not re.search(r"APR1400|실적|KEPIC", str(cit)):
        raise MissingParam(cid, "citation(IEEE 384 판/조항 또는 실적 근거 명시 필요)")
    groups = _need(p, "groups", cid)
    dh = _need(p, "min_horizontal_m", cid)
    barrier = _get(p, "barrier_pairs", cid) or []
    bset = {frozenset(t) for t in barrier}
    gidx = [ctx.resolve(g) for g in groups]
    out: List[Violation] = []
    for a in range(len(gidx)):
        for b in range(a + 1, len(gidx)):
            for i in gidx[a]:
                for j in gidx[b]:
                    if not (ctx.placed(i) and ctx.placed(j)):
                        continue
                    if frozenset((ctx.bid(i), ctx.bid(j))) in bset:
                        continue
                    if ctx.floor(i) != ctx.floor(j):
                        continue
                    g = gap(ctx.rect(i), ctx.rect(j), "euclid")
                    if g < dh - 1e-9:
                        out.append(Violation(
                            cid,
                            f"{ctx.bid(i)}↔{ctx.bid(j)}: {g:.2f} m < {dh:g} m "
                            f"(근거: {cit})",
                            dh - g, "m", True, [ctx.bid(i), ctx.bid(j)],
                            marker=dict(kind="pair", floor=ctx.floor(i),
                                        a=ctx.rect(i), b=ctx.rect(j))))
    return out


register(ConstraintDef(
    id="ELEC-SEP",
    name="전기계통 물리적 분리",
    basis="GDC 17 + RG 1.75 Rev.3. ★ 현행 RG 1.75 Rev.3 본문에는 수치 이격거리가 "
          "전혀 없다. IEEE Std 384-1992 채택 시 원문이 1981년판 대비 \"특정 형상에 "
          "대해 이격거리 기준이 축소되었다\"고 명시했다.",
    formula="Σ max(0, d_min − gap)  [d_min 은 IEEE 384 최신판 §5 원본 대조 후 입력]",
    params=dict(
        groups=P("list[list[selector]]", None, True, "분리 대상 계열 그룹."),
        min_horizontal_m=P("m", None, True,
                           "★★ 기본값 없음. '수평 3 ft / 수직 5 ft'(0.914/1.524 m)는 "
                           "IEEE 384-1981 관행값이므로 그대로 넣으면 안 된다. "
                           "IEEE 384 최신판 §5 원본 대조 후 입력할 것. "
                           "잠정 대용: APR1400 실적 주변압기–UAT–SAT 15 m 또는 "
                           "3시간 내화방벽."),
        citation=P("str", None, True,
                   "위 수치의 출처. IEEE 384 판·절 번호 또는 실적 근거를 적어야 한다."),
        barrier_pairs=P("list[[id,id]]", [], False, "내화방벽으로 면제되는 쌍."),
    ),
    evaluate=_eval_elec,
    hard=True, default_weight=800.0,
    note="APR1400 실적 참고: 레이스웨이 표식 간격 ≤ 4.6 m, 4계열 색상구분, "
         "중복 축전지는 별도 안전등급 구조물, 트레이 적층 상→하 "
         "13.8 kV → 4.16 kV → 480 V/DC → 제어 → 계장.",
))


# ---------------------------------------------------------------------------
# 사용 금지 수치 감시 (references/regulatory-numbers.md 와 쌍을 이룬다)
# ---------------------------------------------------------------------------

FORBIDDEN_VALUES = [
    dict(
        key="ELEC-3FT-5FT",
        title="전기계통 이격 '수평 3 ft / 수직 5 ft' 사용 금지",
        detect="ELEC-SEP.min_horizontal_m ∈ {0.914, 0.9144, 1.524} 또는 citation 누락",
        why="현행 RG 1.75 Rev.3 에는 수치 이격거리가 없다. 통상 인용값은 "
            "IEEE 384-1981 관행값이며, 1992년판이 '특정 형상에 대해 기준이 축소'되었다고 "
            "명시했다. IEEE 384 최신판 §5 원본 대조 전에는 입력 금지.",
    ),
    dict(
        key="SEISMIC-FIXED",
        title="건물 간 이격 고정 수치 금지",
        detect="SEISMIC-GAP 에 고정 min_gap 을 넣는 형태",
        why="SRP 3.7.2 는 성능식이다. gap ≥ δ_i + δ_j + 시공오차.",
    ),
    dict(
        key="ZONE-NUMBERING",
        title="방사선구역 번호 체계 하드코딩 금지",
        detect="zone 값이 'Zone I'~'Zone V' / '구역 1'~'구역 5' 형태",
        why="RG 8.8 이 '모호함을 피하기 위해 구역 번호를 일절 언급하지 않는다'고 "
            "명시한다. 국내 Zone I~V 는 사업자 사내기준이다. "
            "10 CFR 20.1003 정의(30 cm 에서 0.05 / 1 mSv/h, 1 m 에서 5 Gy/h)로 매핑할 것.",
    ),
    dict(
        key="ISOLATION-20FT",
        title="격리구역 폭 '20 ft' 금지",
        detect="isolation_zone_width_m ≈ 6.096",
        why="그것은 §73.51(ISFSI) 조항이다. 발전소 §73.55 에는 폭 수치가 없고 "
            "'관찰과 평가가 가능한 충분한 크기'라는 성능요건뿐이다.",
    ),
    dict(
        key="SGI",
        title="Safeguards Information 을 코드/IR 에 넣지 않는다",
        detect="vehicle_barrier_standoff_m, aircraft_impact_* 등에 값이 채워진 경우",
        why="차량방벽 stand-off 거리, 항공기 충돌 속도·각도·해석방법은 영구 비공개다. "
            "파라미터 슬롯만 두고 값은 보안등급 자료에서 주입할 것.",
    ),
]

_ZONE_BAD = re.compile(r"^\s*(zone|구역)\s*[\-_ ]?\s*([ivxIVX]+|[1-5])\s*$", re.I)
SGI_PARAM_NAMES = ("vehicle_barrier_standoff_m", "aircraft_impact_speed_mps",
                   "aircraft_impact_angle_deg", "target_set_details")


def scan_forbidden(ir: dict) -> Tuple[List[str], List[str]]:
    """
    IR(dict) 을 훑어 사용금지 수치를 찾는다. (errors, warnings) 반환.
    validate_spec.py 가 Gate 1 에서 호출한다.
    """
    errs: List[str] = []
    warns: List[str] = []

    for c in ir.get("constraints", []) or []:
        cid = c.get("id")
        pr = c.get("params") or {}
        if cid == "ELEC-SEP":
            v = pr.get("min_horizontal_m")
            if v is not None and abs(float(v) - 0.9144) < 0.02:
                errs.append("[금지:ELEC-3FT-5FT] ELEC-SEP.min_horizontal_m=0.914 m "
                            "는 IEEE 384-1981 관행값(3 ft)이다. RG 1.75 Rev.3 에는 "
                            "수치 이격거리가 없다. IEEE 384 최신판 §5 대조 후 입력할 것.")
            if v is not None and abs(float(v) - 1.524) < 0.02:
                errs.append("[금지:ELEC-3FT-5FT] ELEC-SEP 에 1.524 m(5 ft, 수직 관행값)"
                            "가 수평 이격으로 들어가 있다.")
            if not pr.get("citation"):
                errs.append("[금지:ELEC-3FT-5FT] ELEC-SEP 는 citation(IEEE 384 판·절 "
                            "또는 실적 근거) 없이 사용할 수 없다.")
        if cid == "SEISMIC-GAP":
            if pr.get("min_gap_m") is not None or pr.get("fixed_gap_m") is not None:
                errs.append("[금지:SEISMIC-FIXED] SEISMIC-GAP 에 고정 이격 수치를 "
                            "넣을 수 없다. δ_max,i + δ_max,j + 시공오차 성능식만 쓴다.")
        for nm in SGI_PARAM_NAMES:
            if pr.get(nm) is not None:
                errs.append(f"[금지:SGI] '{nm}' 에 값이 들어 있다. Safeguards "
                            f"Information 은 IR/코드에 넣지 않는다. 슬롯만 두고 "
                            f"보안등급 채널로 주입할 것.")
        if pr.get("isolation_zone_width_m") is not None and \
           abs(float(pr["isolation_zone_width_m"]) - 6.096) < 0.05:
            errs.append("[금지:ISOLATION-20FT] 격리구역 폭 20 ft(6.096 m)는 "
                        "§73.51(ISFSI) 조항이다. §73.55 에는 폭 수치가 없다.")

    for b in ir.get("blocks", []) or []:
        z = b.get("zone")
        if isinstance(z, str) and _ZONE_BAD.match(z):
            warns.append(f"[금지:ZONE-NUMBERING] 블록 '{b.get('id')}' 의 zone='{z}' "
                         f"— RG 8.8 은 구역 번호를 일절 언급하지 않는다. "
                         f"10 CFR 20.1003 정의로 매핑한 라벨을 쓸 것 "
                         f"(예: 'RA-0.05mSvh', 'HRA-1mSvh', 'VHRA-5Gyh').")
    return errs, warns


# ---------------------------------------------------------------------------
# 전체 평가
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 동선 기능 — 면적을 줄여도 복도·계단·배관대가 '역할' 을 하게 한다
#   (복도·계단도 움직일 수 있고, 복도·배관대 구간은 없앨 수도 있다 — Block.optional)
# ---------------------------------------------------------------------------

CIRC_DEFAULT = ("corridor", "stair", "elevator", "lobby")


def _touch_len(a, b, tol: float) -> Tuple[float, float]:
    """두 사각형 (x,y,w,d) 이 벽을 사이에 두고 맞닿은 길이와 간극. 맞닿지 않으면 (0, 간극)."""
    gx = max(b[0] - (a[0] + a[2]), a[0] - (b[0] + b[2]))
    gy = max(b[1] - (a[1] + a[3]), a[1] - (b[1] + b[3]))
    # 정확히 맞닿은 변(13.2 = 1.8 + 11.4)은 부동소수 오차로 -1e-15 겹침이 될 수 있다 — 0 으로 본다.
    # (그러지 않으면 '겹침 길이 1e-15' 로 나와 맞닿은 계단과 복도가 끊긴 것으로 채점됐다.)
    if abs(gx) < 1e-6:
        gx = 0.0
    if abs(gy) < 1e-6:
        gy = 0.0
    if gx <= tol and gy < 0:
        return -gy, max(gx, 0.0)
    if gy <= tol and gx < 0:
        return -gx, max(gy, 0.0)
    return 0.0, math.hypot(max(gx, 0.0), max(gy, 0.0))


def _on_floor(ctx: EvalContext, f: int):
    """
    층 f 에 놓인 블록 (i, 본체 rect) — 다층 블록(계단 코어)의 위층 그림자 포함.
    접촉·간극은 '본체' 로 잰다. 유지보수 이격(clearance)을 포함한 외곽으로 재면 본체가 복도에서
    1.5 m 떨어져 있어도 맞닿은 것으로 나온다 (독립 검증에서 발견). 이격은 _tol_of 로 허용 간극에 더한다.
    """
    out = []
    for i in range(len(ctx.spec.blocks)):
        r = ctx.rect_on(i, f)
        if r is not None:
            cl = ctx.spec.blocks[i].clearance
            out.append((i, (r[0] + cl, r[1] + cl, max(r[2] - 2 * cl, 0.01), max(r[3] - 2 * cl, 0.01))))
    return out


def _tol_of(ctx: EvalContext, i: int, j: int, tol: float) -> float:
    """두 블록의 허용 간극 = 벽 두께(tol) + 양쪽 유지보수 이격 (이격 공간은 접근 통로로 쓸 수 있다)."""
    return tol + ctx.spec.blocks[i].clearance + ctx.spec.blocks[j].clearance


def _components(nodes, rect, edge_ok):
    """nodes 를 edge_ok(i,j) 로 이은 연결요소 목록 (결정론적 순서)."""
    uf = {i: i for i in nodes}

    def find(x):
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x
    srt = sorted(nodes, key=lambda i: rect[i][0])
    for k, i in enumerate(srt):
        ri = rect[i]
        for j in srt[k + 1:]:
            if rect[j][0] > ri[0] + ri[2] + 1.0:
                break
            if edge_ok(i, j):
                a, b = find(i), find(j)
                if a != b:
                    uf[max(a, b)] = min(a, b)
    comp: Dict[int, List[int]] = {}
    for i in nodes:
        comp.setdefault(find(i), []).append(i)
    return sorted(comp.values(), key=lambda c: (-len(c), min(c)))


def _nm(b) -> str:
    """규칙 정규식이 보는 문자열 = 이름 + id (이름이 같은 복도 구간도 id 로 가를 수 있게)."""
    return f"{b.name or ''} {b.id}"


def _eval_circ_access(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "CIRC-ACCESS"
    door = float(_need(p, "door_m", cid))
    via = int(_get(p, "via_rooms", cid))
    tol = float(_get(p, "touch_tol_m", cid))
    kinds = set(_get(p, "applies_to", cid))
    ctypes = set(_get(p, "circulation_types", cid))
    ext_rx = _get(p, "exterior_access", cid)
    ext_rx = re.compile(ext_rx, re.I) if ext_rx else None
    direct = _get(p, "direct", cid)
    direct = re.compile(direct, re.I) if direct else None
    thr = _get(p, "through_ok", cid)
    thr = re.compile(thr, re.I) if thr else None
    cfor = [(re.compile(a, re.I), re.compile(b, re.I)) for a, b in (_get(p, "corridor_for", cid) or [])]
    nothr = _get(p, "no_through", cid)
    nothr = re.compile(nothr, re.I) if nothr else None
    noent = set(_get(p, "no_entry_types", cid) or [])
    ent_ok = _get(p, "entry_ok", cid)
    ent_ok = re.compile(ent_ok, re.I) if ent_ok else None
    sp = ctx.spec
    out: List[Violation] = []
    for f in range(sp.n_floors):
        items = _on_floor(ctx, f)
        rect = {i: r for i, r in items}
        circ = [i for i, _ in items if sp.blocks[i].circulation in ctypes]
        if items:
            fx0 = min(r[0] for _, r in items); fy0 = min(r[1] for _, r in items)
            fx1 = max(r[0] + r[2] for _, r in items); fy1 = max(r[1] + r[3] for _, r in items)
        rooms = [i for i, _ in items if sp.blocks[i].circulation is None and sp.blocks[i].kind in kinds
                 and ctx.geo.get(i, {}).get("f") == f]
        if not rooms:
            continue
        if not circ:
            lost = [i for i in rooms if not (ext_rx and ext_rx.search(_nm(sp.blocks[i])))]
            if lost:
                out.append(Violation(cid, f"{f+1}층에 동선(복도·계단)이 하나도 없어 실 {len(lost)}개에 들어갈 수 없음",
                                     10.0 * len(lost), "m", True, [sp.blocks[i].id for i in lost[:20]]))
            continue
        # 실 ↔ 동선, 실 ↔ 실 이 문 폭 이상 맞닿으면 드나들 수 있다 (via_rooms 개 실을 거쳐도 된다).
        nodes = circ + rooms
        srt = sorted(nodes, key=lambda i: rect[i][0])
        adj: Dict[int, List[int]] = {i: [] for i in nodes}
        maxcl = max((sp.blocks[i].clearance for i in nodes), default=0.0)
        for k, i in enumerate(srt):
            ri = rect[i]
            for j in srt[k + 1:]:
                if rect[j][0] > ri[0] + ri[2] + tol + 2 * maxcl:
                    break
                L, _g = _touch_len(ri, rect[j], _tol_of(ctx, i, j, tol))
                if L >= door:
                    adj[i].append(j); adj[j].append(i)
        # 실마다 '드나들 수 있는 복도' 를 정할 수 있다 (예: 수술실 → 청결 복도). 정하지 않으면 모든 동선.
        def allowed_circ(i):
            nm = _nm(sp.blocks[i])
            base = circ
            if noent and not (ent_ok and ent_ok.search(nm)):
                # 피난계단·승강기로는 실 문을 내지 않는다 (IBC 2021 §1023.4 등) — 그 동선은 출입구가 아니다
                base = [c for c in circ if sp.blocks[c].circulation not in noent]
            for rx, crx in cfor:
                if rx.search(nm):
                    return {c for c in base if crx.search(_nm(sp.blocks[c]))}
            return set(base)
        circ_set = set(circ)
        depth = {}
        # 1단계: 허용된 복도에 직접 맞닿은 실
        for i in rooms:
            if any(j in allowed_circ(i) for j in adj[i]):
                depth[i] = 1
        # 2단계 이상: 이미 들어갈 수 있는 실을 거쳐서 — 단, 통로로 써도 되는 실(through_ok)만 거친다
        q = [i for i in rooms if i in depth]
        while q:
            nq = []
            for i in q:
                nm_i = _nm(sp.blocks[i])
                if depth[i] >= via + 1 or (thr and not thr.search(nm_i)) or (nothr and nothr.search(nm_i)):
                    continue
                for j in adj[i]:
                    if j in depth or j in circ_set or j not in rooms:
                        continue
                    if (cfor or noent) and allowed_circ(j) != circ_set and not any(
                            depth.get(k) == 1 and set(adj[k]) & allowed_circ(j) for k in [i]):
                        continue                    # 지정 복도가 있는 실은 그 복도 쪽 실을 거쳐야 한다
                    depth[j] = depth[i] + 1
                    nq.append(j)
            q = nq
        for i in rooms:
            if i in depth and not (direct and direct.search(_nm(sp.blocks[i])) and depth[i] > 1):
                continue
            if ext_rx and ext_rx.search(_nm(sp.blocks[i])):
                r = rect[i]   # 바깥에서 바로 드나드는 실 (야드·쓰레기·반입) — 층 외곽에 닿으면 된다
                if min(r[0] - fx0, r[1] - fy0, fx1 - (r[0] + r[2]), fy1 - (r[1] + r[3])) <= tol:
                    continue
            g = min(_touch_len(rect[i], rect[c], _tol_of(ctx, i, c, tol))[1] for c in circ)
            how = ("복도에 직접 접해야 하는 실인데 다른 실을 거쳐야 들어감" if i in depth
                   else "복도·다른 실을 통해 들어갈 수 없음")
            out.append(Violation(
                cid, f"{sp.blocks[i].id}({sp.blocks[i].name}): {how} "
                     f"(가까운 동선까지 {g:.1f} m, 문 폭 {door:g} m 이상 맞닿아야 함)",
                g + 1.0, "m", True, [sp.blocks[i].id],
                marker=dict(kind="rect", floor=f, a=rect[i])))
    return out


register(ConstraintDef(
    id="CIRC-ACCESS",
    name="실 출입 — 모든 실은 복도에서 들어갈 수 있어야 한다",
    basis="실은 복도(또는 via_rooms 개 이하의 다른 실)를 거쳐 문으로 드나든다. 문 폭은 인용 값으로 넣는다 "
          "(예: ADA 2010 §404.2.3 문 순폭 32 in = 0.815 m, IBC 2021 §1010.1.1).",
    formula="∀ 실 r: 동선 집합에서 '문 폭 이상 맞닿음' 간선으로 depth ≤ via_rooms+1 안에 도달",
    params=dict(
        door_m=P("m", None, True, "맞닿은 벽 길이 하한 = 문 순폭. 인용 필수."),
        via_rooms=P("count", 1, False, "다른 실을 거쳐 들어가도 되는 수 (진료실 안 화장실처럼). 0 이면 모든 실이 복도에 직접 접해야 함."),
        touch_tol_m=P("m", 0.35, False, "벽 두께 — 이 간극 이하면 맞닿은 것으로 본다."),
        applies_to=P("kinds", ["room"], False, "출입을 확인할 블록 종류."),
        circulation_types=P("types", list(CIRC_DEFAULT), False, "동선으로 보는 circulation 값."),
        exterior_access=P("regex", None, False, "바깥 문으로 드나드는 실 이름 (예: YARD|TRASH|RECEIV) — 층 외곽에 닿으면 출입 가능."),
        direct=P("regex", None, False, "복도에 '직접' 접해야 하는 실 이름 (예: 수술실 — 침대가 다른 실을 거치면 안 됨)."),
        through_ok=P("regex", None, False, "다른 실이 '거쳐 지나가도' 되는 실 이름. 정하면 그 외의 실(멸균 코어 등)은 통로로 못 쓴다."),
        corridor_for=P("pairs", None, False, "[[실 이름 정규식, 복도 이름 정규식], …] — 그 실은 그 복도로만 드나든다 (청결/오염 동선 분리)."),
        no_through=P("regex", None, False, "다른 실이 '거쳐 지나가면 안 되는' 실 이름 (예: 화장실·창고·기계실·전기실 — IBC 2021 §1016.2)."),
        no_entry_types=P("types", [], False, "실 문을 낼 수 없는 동선 종류 (예: ['stair','elevator'] — 피난계단실 개구부 제한, IBC 2021 §1023.4). 동선 연결·피난에는 그대로 쓴다."),
        entry_ok=P("regex", None, False, "no_entry_types 로도 문을 낼 수 있는 실 이름 (예: 평상시 재실하는 사무실)."),
    ),
    evaluate=_eval_circ_access, hard=True, default_weight=2000.0,
))


def _eval_circ_connect(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "CIRC-CONNECT"
    wmin = float(_need(p, "min_width_m", cid))
    tol = float(_get(p, "touch_tol_m", cid))
    ctypes = set(_get(p, "circulation_types", cid))
    need = set(_get(p, "must_include", cid))
    sp = ctx.spec
    out: List[Violation] = []
    for f in range(sp.n_floors):
        items = _on_floor(ctx, f)
        rect = {i: r for i, r in items}
        circ = [i for i, _ in items if sp.blocks[i].circulation in ctypes]
        if not circ:
            continue
        comps = _components(circ, rect, lambda i, j: _touch_len(rect[i], rect[j], _tol_of(ctx, i, j, tol))[0] >= wmin)
        main = comps[0]
        for c in comps[1:]:
            g = min(_touch_len(rect[a], rect[b], _tol_of(ctx, a, b, tol))[1] for a in c for b in main)
            out.append(Violation(
                cid, f"{f+1}층 동선이 끊겼다: {', '.join(sp.blocks[i].id for i in c[:4])}"
                     f"{' …' if len(c) > 4 else ''} 이(가) 주 동선과 {g:.1f} m 떨어짐 "
                     f"(폭 {wmin:g} m 이상으로 이어져야 함)",
                g + wmin, "m", True, [sp.blocks[i].id for i in c[:20]]))
        has = {sp.blocks[i].circulation for i in main}
        for t in sorted(need):
            if any(sp.blocks[i].circulation == t for i in circ) and t not in has:
                out.append(Violation(cid, f"{f+1}층 주 동선에 {t} 가 이어져 있지 않음",
                                     wmin, "m", True, []))
    return out


register(ConstraintDef(
    id="CIRC-CONNECT",
    name="동선 연결 — 층마다 복도·계단·엘리베이터가 하나로 이어져야 한다",
    basis="복도는 실과 계단을 잇는 하나의 망이어야 피난·이동이 된다. 최소 폭은 인용 값 "
          "(예: IBC 2021 Table 1020.3 복도 44 in = 1.118 m, ASR A1.8 표 2).",
    formula="층마다 동선 블록 그래프(맞닿은 길이 ≥ min_width)가 연결요소 1개이고, must_include 종류를 포함",
    params=dict(
        min_width_m=P("m", None, True, "이어진 것으로 보는 맞닿은 길이 하한 = 복도 최소 폭. 인용 필수."),
        touch_tol_m=P("m", 0.35, False, "벽 두께."),
        circulation_types=P("types", list(CIRC_DEFAULT), False, "한 망으로 이어져야 하는 circulation 값."),
        must_include=P("types", ["stair"], False, "층마다 주 동선에 포함돼야 하는 종류."),
    ),
    evaluate=_eval_circ_connect, hard=True, default_weight=2000.0,
))


def _eval_egress(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "EGRESS"
    lmax = float(_need(p, "max_travel_m", cid))
    stypes = set(_get(p, "exit_types", cid))
    min_exits = int(_get(p, "min_exits", cid))
    frac = _get(p, "exit_separation_frac", cid)
    kinds = set(_get(p, "applies_to", cid))
    ex = _get(p, "exempt", cid)
    ex = re.compile(ex, re.I) if ex else None
    sp = ctx.spec
    out: List[Violation] = []
    for f in range(sp.n_floors):
        items = _on_floor(ctx, f)
        stairs = [r for i, r in items if sp.blocks[i].circulation in stypes]
        occ = [(i, r) for i, r in items if sp.blocks[i].circulation is None
               and sp.blocks[i].kind in kinds and ctx.geo.get(i, {}).get("f") == f
               and not (ex and ex.search(sp.blocks[i].name or ""))]
        if not occ:
            continue
        if len(stairs) < min_exits:
            out.append(Violation(cid, f"{f+1}층 피난계단 {len(stairs)}개 < 필요 {min_exits}개",
                                 10.0 * (min_exits - len(stairs)), "count", True, []))
        if not stairs:
            continue
        g0 = int(_get(p, "ground_floor", cid))
        for i, r in items:
            if sp.blocks[i].circulation in stypes and f != g0:
                fs = ctx.spans(i)
                # 위층 계단은 내려가서, 지하 계단은 올라가서 피난층에 닿아야 한다 (지하 → 1층 연결도 본다)
                if not all(k in fs for k in range(min(g0, f), max(g0, f) + 1)):
                    out.append(Violation(cid, f"{sp.blocks[i].id}: {f+1}층 계단이 피난층({g0+1}층)까지 이어지지 않음 "
                                              f"(이 계단의 층: {', '.join(str(k + 1) for k in fs)})",
                                         5.0, "m", True, [sp.blocks[i].id], marker=dict(kind="rect", floor=f, a=r)))
        sc = [(r[0] + r[2] / 2, r[1] + r[3] / 2) for r in stairs]
        for i, r in occ:
            cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
            d = min(abs(cx - x) + abs(cy - y) for x, y in sc)   # 직교 거리 = 복도 경로의 하한
            if d > lmax + 1e-9:
                out.append(Violation(cid, f"{sp.blocks[i].id}: 가장 가까운 계단까지 {d:.1f} m > 허용 {lmax:g} m",
                                     d - lmax, "m", True, [sp.blocks[i].id],
                                     marker=dict(kind="rect", floor=f, a=r)))
        if frac and len(sc) >= 2:
            xs = [r[0] for _, r in items] + [r[0] + r[2] for _, r in items]
            ys = [r[1] for _, r in items] + [r[1] + r[3] for _, r in items]
            diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
            far = max(math.dist(a, b) for a in sc for b in sc)
            if far < frac * diag - 1e-9:
                out.append(Violation(cid, f"{f+1}층 계단 사이 거리 {far:.1f} m < 대각선의 {frac:g} ({frac*diag:.1f} m) — "
                                          f"한 화재로 두 출구를 다 잃을 수 있음", frac * diag - far, "m", True, []))
    return out


register(ConstraintDef(
    id="EGRESS",
    name="피난 — 계단까지 거리·계단 수·계단 간 이격",
    basis="IBC 2021 §1017.2 출구까지 보행거리, §1006.3 층당 출구 수, §1007.1.1 출구 간 이격(스프링클러 시 대각선의 1/3); "
          "독일 MBO §35 / ASR A2.3 (계단까지 35 m). 값은 관할·용도에 맞게 인용해 넣는다.",
    formula="∀ 실: 직교거리(실 중심→가장 가까운 계단) ≤ max_travel ; 층당 계단 ≥ min_exits ; 최대 계단 간 거리 ≥ frac·대각선",
    params=dict(
        max_travel_m=P("m", None, True, "인용 필수. 직교거리는 실제 보행거리의 하한이므로 보수적이지 않다 — 여유를 둘 것."),
        exit_types=P("types", ["stair"], False, "출구로 보는 circulation 값."),
        min_exits=P("count", 2, False, "층당 출구(계단) 수."),
        exit_separation_frac=P("ratio", None, False, "계단 간 최소 거리 / 층 대각선 (IBC 1007.1.1: 1/3 스프링클러, 1/2 비스프링클러)."),
        applies_to=P("kinds", ["room", "equipment"], False, ""),
        exempt=P("regex", None, False, "상주 인원이 없는 실(옥탑 기계실 등) — 피난 거리·출구 수 검사에서 뺀다."),
        ground_floor=P("index", 0, False, "피난층(0 부터). 위층·지하층 계단은 이 층까지 끊김 없이 이어져야 한다."),
    ),
    evaluate=_eval_egress, hard=True, default_weight=2000.0,
))


def _eval_adjacent(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "ADJACENT"
    tol = float(_get(p, "touch_tol_m", cid))
    sp = ctx.spec
    out: List[Violation] = []
    for rule in _need(p, "rules", cid):
        def _sel(x):
            if x.startswith("circ:"):           # 동선 종류로 지정 (circ:corridor|lobby)
                ts = set(x[5:].split("|"))
                return lambda blk: blk.circulation in ts
            rx = re.compile(x, re.I)
            return lambda blk: bool(rx.search(_nm(blk)))
        ra, rb = _sel(rule["a"]), _sel(rule["b"])
        mode = rule.get("mode", "must")
        need = float(rule.get("min_touch_m", 0.9))
        why = rule.get("why", "")
        for f in range(sp.n_floors):
            items = _on_floor(ctx, f)
            A = [(i, r) for i, r in items if ra(sp.blocks[i]) and ctx.geo.get(i, {}).get("f") == f]
            Bs = [(j, r) for j, r in items if rb(sp.blocks[j])]
            for i, r in A:
                cands = [(j, _touch_len(r, rj, _tol_of(ctx, i, j, tol))) for j, rj in Bs if j != i]
                if mode == "must":
                    if not cands:
                        continue          # 이 층에 상대가 없다 — 다른 층 배치는 층 배정 문제로 본다
                    mg = rule.get("max_gap_m")
                    if any((L >= need) if mg is None else (_g <= mg + 1e-9) for _, (L, _g) in cands):
                        continue
                    g = min(g for _, (_L, g) in cands)
                    out.append(Violation(cid, f"{sp.blocks[i].id}({sp.blocks[i].name}): '{rule['b']}' 와 "
                                              f"{need:g} m 이상 맞닿아야 함 (가장 가까운 것 {g:.1f} m) — {why}",
                                         g + 1.0, "m", True, [sp.blocks[i].id],
                                         marker=dict(kind="rect", floor=f, a=r)))
                else:
                    for j, (L, g) in cands:
                        if L > 0.0:
                            out.append(Violation(cid, f"{sp.blocks[i].id}↔{sp.blocks[j].id}: 맞닿으면 안 됨 "
                                                      f"(맞닿은 길이 {L:.1f} m) — {why}",
                                                 L + 1.0, "m", True, [sp.blocks[i].id, sp.blocks[j].id],
                                                 marker=dict(kind="rect", floor=f, a=r)))
    return out


register(ConstraintDef(
    id="ADJACENT",
    name="인접 — 붙어 있어야 하는 실 / 붙으면 안 되는 실",
    basis="기능상 직접 이어져야 하는 실(수술실↔멸균 코어, 검체 화장실↔검사실, 엘리베이터↔복도)과 "
          "직접 이어지면 안 되는 실(오염실↔청결 구역). 규칙마다 근거를 why 에 적는다.",
    formula="mode=must: ∀ a: ∃ b 같은 층, 맞닿은 길이 ≥ min_touch ; mode=must_not: ∀ a,b: 맞닿은 길이 = 0",
    params=dict(
        rules=P("list", None, True, "[{a: 실 정규식, b: 실 정규식, mode: must|must_not, min_touch_m, max_gap_m, why}] — max_gap_m 을 주면 맞닿음 대신 간극 ≤ max_gap 이면 된다. — 정규식은 이름+id 에 맞춘다. 'circ:corridor|lobby' 처럼 동선 종류로도 지정."),
        touch_tol_m=P("m", 0.35, False, "벽 두께."),
    ),
    evaluate=_eval_adjacent, hard=True, default_weight=2000.0,
))


def _eval_dead_end(ctx: EvalContext, p: dict) -> List[Violation]:
    """
    막다른 복도: 동선망에서 한쪽 끝만 이어진 복도 사슬의 길이.
    복도 블록의 동선 이웃(맞닿은 길이 ≥ min_width)이 1개인 블록에서 출발해, 이웃이 2개인 복도를 따라가며
    길이(긴 변)를 더한다. 갈림(이웃 ≥3)·로비에 닿으면 그 길이가 막다른 구간이고, 출구(계단 등)에 닿으면
    막다른 곳이 아니다. 블록 단위 근사 — 복도 조각 안의 실제 끝 위치는 보지 않는다.
    """
    cid = "DEAD-END"
    lmax = float(_need(p, "max_m", cid))
    wmin = float(_need(p, "min_width_m", cid))
    tol = float(_get(p, "touch_tol_m", cid))
    exits = set(_get(p, "exit_types", cid))
    sp = ctx.spec
    out: List[Violation] = []
    for f in range(sp.n_floors):
        items = _on_floor(ctx, f)
        rect = {i: r for i, r in items}
        circ = [i for i, _ in items if sp.blocks[i].circulation in ("corridor", "lobby") or
                sp.blocks[i].circulation in exits]
        nb = {i: [] for i in circ}
        for k, i in enumerate(circ):
            for j in circ[k + 1:]:
                if _touch_len(rect[i], rect[j], _tol_of(ctx, i, j, tol))[0] >= wmin:
                    nb[i].append(j); nb[j].append(i)
        seen = set()
        for s0 in circ:
            if sp.blocks[s0].circulation != "corridor" or len(nb[s0]) != 1 or s0 in seen:
                continue
            chain, prev, cur, L, ok = [s0], None, s0, 0.0, False
            while True:
                seen.add(cur)
                L += max(rect[cur][2], rect[cur][3])
                nxt = [j for j in nb[cur] if j != prev]
                if not nxt:
                    break
                j = nxt[0]
                if sp.blocks[j].circulation in exits:
                    ok = True; break
                if sp.blocks[j].circulation != "corridor" or len(nb[j]) != 2:
                    break                       # 갈림·로비 — 여기까지가 막다른 구간
                prev, cur = cur, j
                chain.append(j)
            if not ok and L > lmax + 1e-9:
                out.append(Violation(cid, f"{f+1}층 막다른 복도 {L:.1f} m > 허용 {lmax:g} m "
                                          f"({', '.join(sp.blocks[i].id for i in chain[:4])})",
                                     L - lmax, "m", True, [sp.blocks[i].id for i in chain[:20]],
                                     marker=dict(kind="rect", floor=f, a=rect[s0])))
    return out


register(ConstraintDef(
    id="DEAD-END",
    name="막다른 복도 길이",
    basis="IBC 2021 §1020.5 — 막다른 복도 20 ft(6.1 m), B·E·F·I-1·M·R-1·R-2·S·U 용도 스프링클러 시 50 ft(15.24 m). "
          "복도 구간을 없애면 막다른 복도가 길어질 수 있다.",
    formula="한쪽 끝만 동선망에 이어진 복도 사슬의 길이(블록 긴 변의 합) ≤ max_m — 출구에 닿는 사슬은 제외",
    params=dict(
        max_m=P("m", None, True, "인용 필수."),
        min_width_m=P("m", None, True, "이어진 것으로 보는 맞닿은 길이 = 복도 최소 폭."),
        touch_tol_m=P("m", 0.35, False, "벽 두께."),
        exit_types=P("types", ["stair"], False, "닿으면 막다른 곳이 아닌 동선 종류."),
    ),
    evaluate=_eval_dead_end, hard=True, default_weight=2000.0,
))


def _iga(word: str) -> str:
    """받침이 있으면 '이', 없으면 '가'."""
    ch = word[-1:] if word else ""
    return "이" if "가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28 else "가"


def _eval_rack_serve(ctx: EvalContext, p: dict) -> List[Violation]:
    cid = "RACK-SERVE"
    rtype = _get(p, "rack_type", cid)
    dmax = float(_need(p, "max_gap_m", cid))
    tol = float(_get(p, "touch_tol_m", cid))
    served = ctx.resolve(_need(p, "served", cid))
    sp = ctx.spec
    out: List[Violation] = []
    # 공급 요소 종류에 맞는 말로 알린다 (크레인 위반을 '배관대에서 …' 로 쓰지 않게)
    noun, lack = {"piperack": ("배관대", "배관을 받을 수 없음"),
                  "crane": ("크레인", "크레인 작업반경 밖"),
                  "road": ("도로", "도로에서 접근할 수 없음")}.get(rtype, (str(rtype), "공급 범위 밖"))
    for f in range(sp.n_floors):
        items = _on_floor(ctx, f)
        rect = {i: r for i, r in items}
        racks = [i for i, _ in items if sp.blocks[i].circulation == rtype]
        if not racks:
            if any(i in rect for i in served):
                out.append(Violation(cid, f"{f+1}층에 {noun}{_iga(noun)} 없다", 50.0, "m", True, []))
            continue
        wmin = min(min(rect[i][2], rect[i][3]) for i in racks) * 0.5
        comps = _components(racks, rect, lambda i, j: _touch_len(rect[i], rect[j], _tol_of(ctx, i, j, tol))[0] >= wmin)
        for c in (comps[1:] if _get(p, "connected", cid) else []):
            g = min(_touch_len(rect[a], rect[b], _tol_of(ctx, a, b, tol))[1] for a in c for b in comps[0])
            out.append(Violation(cid, f"{noun}{_iga(noun)} 끊겼다 ({', '.join(sp.blocks[i].id for i in c[:3])} 가 {g:.1f} m 떨어짐)",
                                 g + wmin, "m", True, [sp.blocks[i].id for i in c]))
        for i in served:
            if i not in rect:
                continue
            g = min(_touch_len(rect[i], rect[r], tol)[1] for r in racks)
            if g > dmax + 1e-9:
                out.append(Violation(cid, f"{sp.blocks[i].id}({sp.blocks[i].name}): {noun}에서 {g:.1f} m > 허용 {dmax:g} m — "
                                          f"{lack}", g - dmax, "m", True, [sp.blocks[i].id],
                                     marker=dict(kind="rect", floor=f, a=rect[i])))
    return out


register(ConstraintDef(
    id="RACK-SERVE",
    name="배관대 기능 — 공정 설비가 배관대에 붙어 있고 배관대가 끊기지 않아야 한다",
    basis="CCPS Guidelines for Siting and Layout of Facilities (2018): 공정 설비는 주 배관대 양옆에 배치한다. "
          "배관대 구간은 없앨 수 있지만(optional), 남은 배관대는 이어져 있어야 하고 설비는 max_gap 안에 있어야 한다.",
    formula="∀ 설비 s ∈ served: gap(s, 배관대) ≤ max_gap ; 배관대 구간 그래프 연결",
    params=dict(
        served=P("selector", None, True, "배관대에서 배관을 받는 설비 선택자."),
        max_gap_m=P("m", None, True, "설비–배관대 최대 간극 (배관 지선 길이). 인용 또는 가정 명시."),
        rack_type=P("type", "piperack", False, "공급 요소의 circulation 값 (piperack, crane …)."),
        touch_tol_m=P("m", 0.5, False, ""),
        connected=P("bool", True, False, "공급 요소끼리 이어져 있어야 하는가 (배관대 true, 크레인 false)."),
    ),
    evaluate=_eval_rack_serve, hard=True, default_weight=2000.0,
))


def evaluate_all(ctx: EvalContext) -> Tuple[List[Violation], Dict[str, float]]:
    """
    spec.constraints 에 선언된 제약만 평가한다. 선언하지 않은 제약은 돌지 않는다.
    반환: (위반 목록, {constraint.<ID>: 가중 합계})
    """
    viols: List[Violation] = []
    terms: Dict[str, float] = {}
    for c in getattr(ctx.spec, "constraints", []) or []:
        cid = c.get("id")
        if c.get("enabled") is False:
            continue
        cd = REGISTRY.get(cid)
        if cd is None:
            raise KeyError(f"알 수 없는 제약 id: {cid!r}. "
                           f"사용 가능: {', '.join(sorted(REGISTRY))}")
        vs = cd.evaluate(ctx, c.get("params") or {})
        w = ctx.spec.weights.get(f"constraint.{cid}", c.get("weight", cd.default_weight))
        if vs:
            terms[f"constraint.{cid}"] = w * sum(v.amount for v in vs)
        viols.extend(vs)
    return viols, terms


def catalog_markdown() -> str:
    """references/constraint-library.md 자동 생성용."""
    lines = ["| ID | 이름 | 유형 | 근거 | 파라미터 |", "|---|---|---|---|---|"]
    for cid in sorted(REGISTRY):
        cd = REGISTRY[cid]
        ps = ", ".join(f"`{k}`{'*' if v['required'] else ''}"
                       for k, v in cd.params.items())
        basis = cd.basis.replace("\n", " ").replace("|", "/")
        lines.append(f"| `{cid}` | {cd.name} | {'hard' if cd.hard else 'soft'} | "
                     f"{basis[:160]} | {ps} |")
    lines.append("")
    lines.append("`*` 표시는 필수 파라미터 — 값이 null 이면 Gate 1 에서 ERROR.")
    return "\n".join(lines)
