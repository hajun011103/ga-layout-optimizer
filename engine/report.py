# -*- coding: utf-8 -*-
"""
report.py — 실행 결과 HTML 보고서 (한 파일, 외부 의존 없음, 오프라인 열람).

숫자는 전부 summary.json(솔버 출력)에서 온다. 이 모듈은 숫자를 계산하지 않고 옮겨 적기만 한다.
"""
from __future__ import annotations

import html
import os
from typing import List, Optional


def _fmt(v, nd=0):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:,.{nd}f}"
    return html.escape(str(v))


def _pct(v):
    if v is None:
        return "—"
    # v = 감소율(양수 = 줄어듦). 표기는 변화량 부호로: 줄면 −, 늘면 +
    cls = "good" if v > 0.05 else ("bad" if v < -0.05 else "")
    return f'<span class="{cls}">{-v:+.1f}%</span>'


def _svg(out: str, name: str) -> Optional[str]:
    p = os.path.join(out, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fp:
        s = fp.read()
    # 반응형: 고정 width/height 제거 (viewBox 유지)
    head, sep, rest = s.partition(">")
    import re
    head = re.sub(r'\s(width|height)="[^"]*"', "", head, count=2)
    return head + sep + rest


CSS = """
:root{--bg:#f7f7f5;--fg:#1c1c1a;--mut:#6b6b66;--line:#e2e1dc;--card:#fff;--good:#0f766e;--bad:#b91c1c;--acc:#1d4ed8}
@media (prefers-color-scheme: dark){:root:not([data-theme=light]){--bg:#161615;--fg:#ecebe6;--mut:#a3a29c;--line:#34332f;--card:#1f1f1d;--good:#5eead4;--bad:#fca5a5;--acc:#93c5fd}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 "IBM Plex Sans KR","Noto Sans KR","Malgun Gothic",system-ui,sans-serif}
main{max-width:1240px;margin:0 auto;padding:28px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.sub{color:var(--mut);font-size:13px}.nfc{display:inline-block;border:1.5px solid var(--bad);color:var(--bad);font-weight:700;font-size:12px;padding:2px 8px;border-radius:4px;margin:10px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi b{display:block;font-size:26px;font-variant-numeric:tabular-nums}.kpi span{color:var(--mut);font-size:13px}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-variant-numeric:tabular-nums}
th,td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:right}th:first-child,td:first-child{text-align:left}
th{font-size:12.5px;color:var(--mut);font-weight:600}
.good{color:var(--good);font-weight:700}.bad{color:var(--bad);font-weight:700}
.warn{background:#fff1f2;color:#9f1239;border:1.5px solid #e11d48;border-radius:10px;padding:12px 16px;margin:16px 0;font-weight:600}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:820px){.pair{grid-template-columns:1fr}}
figure{margin:0;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px}
figure svg{width:100%;height:auto;display:block}figcaption{font-size:13px;color:#555;padding:4px 6px}
code{font-size:12px;word-break:break-all}ul{padding-left:20px}
"""


def write_html(summary: dict, out: str, path: str) -> str:
    s = summary
    o, a, b, c = s["optimized"], s.get("asis"), s["naive_baseline"], s.get("comparison")
    h: List[str] = []
    h.append(f"<!doctype html><html lang=ko><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>GA 최적화 보고서</title><style>{CSS}</style></head><body><main>")
    h.append(f"<h1>{html.escape(s['project'])}</h1>")
    h.append(f"<div class=sub>블록 {s['blocks']}개 · {s['floors']}개 층 · 모드 {s['mode']} · "
             f"seed {s['seed']} · 엔진 {s['engine_version']} · {s['wall_time_s']} s</div>")
    h.append("<div class=nfc>NOT FOR CONSTRUCTION — 개념검토용</div>")

    if s["hard_violation_count"]:
        h.append(f"<div class=warn>★ hard 제약 위반 {s['hard_violation_count']}건 — 이 해는 채택할 수 없다. "
                 f"개선율을 주장하지 말 것.<ul>" +
                 "".join(f"<li>{html.escape(v['detail'])}</li>" for v in o["hard_violations"][:20]) +
                 "</ul></div>")

    if c and c.get("verdict"):
        cls = "warn" if (c.get("asis_better") or o["hard_violation_count"]) else "kpi"
        h.append(f"<div class={cls} style='margin:14px 0'>판정: {html.escape(c['verdict'])}</div>")
    ref = a or b
    ref_name = "현황 도면" if a else "나이브 기준안"
    red = (c or {}).get("footprint_reduction_pct") if a else None
    h.append("<div class=kpis>")
    h.append(f"<div class=kpi><span>건축면적 (최적화)</span><b>{_fmt(o['footprint_m2'])} m²</b>"
             f"<span>{ref_name} {_fmt(ref['footprint_m2'])} m²</span></div>")
    if a:
        h.append(f"<div class=kpi><span>현황 대비 면적</span><b>{_pct(red)}</b>"
                 f"<span>− = 줄어듦 (같은 목적함수로 채점)</span></div>")
    h.append(f"<div class=kpi><span>외피 W × D × H</span><b>{o['width_m']:.1f} × {o['depth_m']:.1f}</b>"
             f"<span>× {o['height_m']:.1f} m</span></div>")
    h.append(f"<div class=kpi><span>hard 제약 위반</span><b class={'bad' if o['hard_violation_count'] else 'good'}>"
             f"{o['hard_violation_count']}건</b><span>0 이어야 채택 가능</span></div>")
    h.append("</div>")

    h.append("<h2>현황 · 기준안 · 최적화 비교 (같은 IR, 같은 목적함수)</h2><table>")
    h.append("<tr><th>지표</th>" + ("<th>현황(도면)</th>" if a else "") +
             "<th>나이브 기준안</th><th>최적화</th>" + ("<th>현황 대비</th>" if a else "") + "</tr>")
    rows = [("외피 폭 W [m]", "width_m", 1), ("외피 깊이 D [m]", "depth_m", 1),
            ("건축면적 [m²]", "footprint_m2", 0), ("건물체적 [m³]", "volume_m3", 0),
            ("이용률 (기기 본체/바닥)", "utilization", 3), ("연결비용 (가중 거리)", "link_cost", 0),
            ("hard 위반 [건]", "hard_violation_count", 0)]
    for label, k, nd in rows:
        imp = ""
        if a:
            av, ov = a.get(k), o.get(k)
            if k in ("footprint_m2", "volume_m3", "link_cost", "width_m", "depth_m") and av:
                imp = f"<td>{_pct((av - ov) / av * 100)}</td>"
            else:
                imp = "<td></td>"
        h.append(f"<tr><td>{label}</td>" + (f"<td>{_fmt(a.get(k), nd)}</td>" if a else "") +
                 f"<td>{_fmt(b.get(k), nd)}</td><td><b>{_fmt(o.get(k), nd)}</b></td>{imp}</tr>")
    h.append("</table>")
    if a:
        h.append("<p class=sub>현황 배치는 도면 파서가 읽은 좌표 그대로를 같은 목적함수로 채점한 것이다. "
                 "나이브 기준안(입력 순서 줄 세우기)은 참고용이며 그 대비 개선율은 성과로 쓰지 않는다.</p>")
        if s.get("asis_clearance_overlaps"):
            h.append(f"<p class=sub>참고: 현황 도면에서 유지보수 이격(clearance)을 포함하면 겹치는 쌍 "
                     f"{len(s['asis_clearance_overlaps'])}개 — 예: "
                     f"{html.escape(', '.join(s['asis_clearance_overlaps'][:4]))}</p>")
    else:
        h.append("<p class=sub>현황 도면이 없어 나이브 기준안과만 비교했다. 이 개선율은 상한이며 "
                 "성과로 제시하지 말 것 (실제 기대치 −10~15%).</p>")

    if s.get("pareto"):
        h.append("<h2>Pareto 프론트 (설계자가 고를 것)</h2><table><tr><th>#</th><th>실현</th>"
                 "<th>체적 [m³]</th><th>연결비용</th><th>구역분산</th></tr>")
        for r in s["pareto"][:30]:
            h.append(f"<tr><td>{r['index']}</td><td>{'OK' if r['feasible'] else 'X'}</td>"
                     f"<td>{_fmt(r['volume_m3'])}</td><td>{_fmt(r['link_cost'])}</td>"
                     f"<td>{_fmt(r['zone_dispersion'], 2)}</td></tr>")
        h.append("</table>")

    h.append("<h2>층별 평면도</h2>")
    for f in range(s["floors"]):
        left = _svg(out, f"GA_asis_floor{f+1}.svg") if a else (
            _svg(out, "GA_baseline_floor1.svg") if f == 0 else None)
        right = _svg(out, f"GA_optimized_floor{f+1}.svg")
        h.append("<div class=pair>")
        if left:
            h.append(f"<figure>{left}<figcaption>{f+1}층 — {'현황(도면)' if a else '나이브 기준안'}</figcaption></figure>")
        if right:
            h.append(f"<figure>{right}<figcaption>{f+1}층 — 최적화</figcaption></figure>")
        h.append("</div><br>")

    if s.get("gate1_warnings"):
        h.append("<h2>Gate 1 WARN (담당 엔지니어 확인 사항)</h2><ul>" +
                 "".join(f"<li>{html.escape(w)}</li>" for w in s["gate1_warnings"][:40]) + "</ul>")
    h.append("<h2>재현성</h2><table>")
    for k, v in (("IR SHA-256", s["ir_sha256"]), ("결과 SHA-256", s["result_sha256"]),
                 ("seed", s["seed"]), ("파라미터", s["params"])):
        h.append(f"<tr><td>{k}</td><td><code>{html.escape(str(v))}</code></td></tr>")
    h.append("</table><p class=sub>같은 IR 해시 + 같은 seed → 같은 결과 해시. 이 보고서의 모든 숫자는 "
             "결정론적 솔버의 출력이며 LLM 이 쓰지 않았다.</p></main></body></html>")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(h))
    return path
