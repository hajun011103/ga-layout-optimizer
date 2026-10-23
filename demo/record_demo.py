# -*- coding: utf-8 -*-
"""
record_demo.py — 시연 영상 자동 녹화 (Playwright + ffmpeg). 엔진과 무관한 개발 도구.

  # 1) 서버 (새 작업공간)
  python3 server/mcp_server.py --http --port 8765 --workspace /tmp/ga-demo-ws
  # 2) 녹화 (playwright·ffmpeg 가 있는 별도 환경에서)
  python demo/record_demo.py --out demo/out

화면에 나오는 모든 숫자는 녹화 중 실제로 MCP 도구를 호출해 얻은 것이다.
Claude Code 장면은 실제 실행 기록(demo/claude_session_steps.json)을 속도만 조절해 재생한다.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DRAW = os.path.join(ROOT, "examples", "drawings")
STUDIO = "http://127.0.0.1:8765/studio"
W, H = 1280, 720

CLAUDE_PROMPT = ("원전 보조건물 현황 도면 examples/drawings/smr_aux_3f.dxf 를 smr-ga 도구로 불러와서, "
                 "지금 공간보다 더 작게 배치를 최적화해줘. 설계요건(층고·연결·구역·이격)은 내장 예제 smr_aux 의 "
                 "요건 파일을 적용하면 돼. 검증 결과를 보고 내가 승인할게 — 승인자는 '홍길동 책임'이고, "
                 "WARN 확인했고 승인한다. 탐색량은 quick. 끝나면 현황 대비 결과와 판정을 한국어로 짧게 정리해줘.")

CAP_JS = """
(text) => {
  let d = document.getElementById('__cap');
  if (!d) {
    d = document.createElement('div'); d.id = '__cap';
    d.style.cssText = 'position:fixed;left:50%;bottom:22px;transform:translateX(-50%);max-width:86%;' +
      'background:rgba(17,17,20,.88);color:#fff;font:600 19px/1.45 "Malgun Gothic",sans-serif;' +
      'padding:10px 20px;border-radius:10px;z-index:99999;text-align:center;box-shadow:0 6px 24px rgba(0,0,0,.25)';
    document.body.appendChild(d);
  }
  d.style.display = text ? '' : 'none';
  d.textContent = text;
}
"""


class Rec:
    def __init__(self, page):
        self.p = page

    def cap(self, text, hold=0.0):
        self.p.evaluate(CAP_JS, text)
        if hold:
            time.sleep(hold)

    def scroll_to(self, sel, offset=70, hold=0.0):
        self.p.evaluate("""([s, o]) => { const e = document.querySelector(s);
            if (e) window.scrollTo({top: e.getBoundingClientRect().top + window.scrollY - o, behavior: 'smooth'}); }""",
                        [sel, offset])
        time.sleep(0.9 + hold)

    def glide(self, px, seconds):
        """천천히 스크롤 (도면을 훑어보는 장면)."""
        steps = max(1, int(seconds * 25))
        for _ in range(steps):
            self.p.evaluate(f"window.scrollBy(0, {px/steps})")
            time.sleep(seconds / steps)

    def slide(self, name, text, hold):
        self.p.goto(f"file://{HERE}/slides.html#{name}")
        time.sleep(0.6)
        self.cap(text, hold)


def studio_flow(r: Rec, *, upload=None, example=None, opts=None, req=None, effort="quick",
                caps: dict = None, glide=True):
    p = r.p
    caps = caps or {}
    p.goto(STUDIO)
    p.wait_for_selector("#ex option[value=smr_aux]", state="attached")
    r.cap(caps.get("open", ""), 2.0)
    if upload:
        for sel, val in (opts or {}).items():
            p.fill(sel, str(val))
            time.sleep(0.4)
        p.set_input_files("#file", upload)
        p.wait_for_selector("#imp:not(.hide)", timeout=60000)
        time.sleep(0.8)
    else:
        p.select_option("#ex", example)
        time.sleep(0.8)
        p.click("#exbtn")
        p.wait_for_selector("#proj:not(.hide)", timeout=60000)
    r.scroll_to("#imp", hold=0.3)
    r.cap(caps.get("imp", ""), 4.5)
    if glide:
        r.glide(420, 3.0)
    r.scroll_to("#c2")
    if upload:
        p.click("#mkproj")
        p.wait_for_selector("#proj:not(.hide)")
        time.sleep(0.6)
    r.cap(caps.get("questions", ""), 4.5)
    if req:
        p.set_input_files("#reqfile", req)
    else:
        p.click("#applyreq")
    time.sleep(1.5)
    r.cap(caps.get("req", ""), 3.5)
    r.scroll_to("#c3")
    p.click("#val")
    p.wait_for_selector("#gate:not(.hide)")
    r.cap(caps.get("gate", "Gate 1 — 결정론적 규칙 검증 (LLM 미사용). ERROR 가 있으면 진행 불가, WARN 은 사람이 확인"), 3.5)
    p.click("#appr")
    p.keyboard.type("홍길동 책임", delay=90)
    p.check("#ack")
    time.sleep(0.4)
    p.click("#frz")
    p.wait_for_selector("#run:not([disabled])")
    r.cap(caps.get("freeze", "사람이 승인해야 IR 이 SHA-256 으로 동결된다 — 이후 계산은 LLM 호출 0회로 재현"), 3.0)
    r.scroll_to("#c4")
    p.select_option("#eff", effort)
    time.sleep(0.4)
    p.click("#run")
    r.cap(caps.get("run", "결정론적 솔버가 계산한다 — 현황 배치에서 출발 + 다중 재시작"), 0)
    p.wait_for_selector("#res:not(.hide)", timeout=600000)
    time.sleep(0.8)
    r.scroll_to("#c4", offset=10)
    r.cap(caps.get("result", ""), 5.5)
    if glide:
        r.scroll_to("#resimgs", offset=60)
        r.cap(caps.get("drawings", "왼쪽: 현황(도면에서 읽은 그대로) · 오른쪽: 최적화 — 빨간 표시 = hard 제약 위반"), 0)
        r.glide(900, 7.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--nuclear-effort", default="normal")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    raw = os.path.join(a.out, "raw")
    shutil.rmtree(raw, ignore_errors=True)
    with open(os.path.join(HERE, "claude_session_steps.json"), encoding="utf-8") as fp:
        steps = json.load(fp)

    with sync_playwright() as pw:
        br = pw.chromium.launch()
        ctx = br.new_context(viewport={"width": W, "height": H}, record_video_dir=raw,
                             record_video_size={"width": W, "height": H}, device_scale_factor=1)
        page = ctx.new_page()
        page.on("dialog", lambda d: d.dismiss())
        r = Rec(page)

        # ---- 0. 제목 · 목표 ----
        r.slide("title", "", 4.5)
        r.slide("goals", "세 가지 목표와 해법 — LLM 은 묻고 호출하고 설명할 뿐, 좌표는 파서와 솔버만 만든다", 9.0)

        # ---- 1. 실제 Claude Code 세션 ----
        page.goto(f"file://{HERE}/claude_replay.html")
        time.sleep(0.5)
        r.cap("Claude Code 에서 — 자연어로 요청하면 Claude 가 MCP 도구를 순서대로 호출한다 (실제 실행 기록 재생)")
        page.evaluate("([p, s]) => window.start(p, s, 1.6)", [CLAUDE_PROMPT, steps])
        page.wait_for_function("window.done === true", timeout=180000)
        r.cap("Claude 는 도구 응답의 숫자를 그대로 인용하고, 합성 도면이라 성과로 쓰면 안 된다는 점까지 밝힌다", 5.0)

        # ---- 2. 원전 — 업로드부터 (목표 1·2) ----
        studio_flow(r, upload=os.path.join(DRAW, "smr_aux_3f.dxf"),
                    req=os.path.join(DRAW, "smr_aux_3f_requirements.json"),
                    effort=a.nuclear_effort,
                    caps=dict(
                        open="스튜디오 — 사람이 직접 쓰는 화면. Claude·ChatGPT 와 같은 MCP 도구를 호출한다 (오른쪽 기록)",
                        imp="원전 SMR 보조건물 현황 도면(DXF, 3개 층) — 파서가 기기 43 · 고정 코어 6 · 복도 6 · 해치 1 을 읽었다. 축척은 DXF 헤더(mm)",
                        questions="도면이 모르는 것은 지어내지 않고 묻는다 — 층고, 유지보수 이격, 연결관계, 구역·이격 요건, 코어가 정말 고정인지",
                        req="답: 설계요건 파일 — P&ID 연결, 계열 분리, App.R 화재분리·해치 병합·관통·터빈 미사일 등 원전 제약 (가정값)",
                        run="결정론적 솔버 계산 중 — 현황 배치에서 출발 + 다중 재시작 (원전 44기기, 3개 층)",
                        result="현황 도면은 hard 위반 15건 → 최적화 0건, 건축면적 −28.8% — 같은 목적함수로 채점한 비교",
                    ))

        # 보고서
        link = page.get_attribute("#links a", "href")
        if link:
            page.goto(link)
            time.sleep(0.6)
            r.cap("보고서 한 파일 — 판정, 비교표, 층별 현황/최적화 도면, 재현 해시. DXF 도 함께 나온다", 3.5)
            r.glide(1400, 6.0)

        # ---- 3. 비교군 (목표 3) ----
        r.slide("goals", "원전 도면이 없을 때 — 제약이 많은 비교군 세 가지", 3.0)
        studio_flow(r, example="chem_plant", effort="quick", glide=False, caps=dict(
            open="비교군 ① 석유화학 공정 플랜트 — 벡터 PDF 1:500",
            imp="PDF 의 레이어(OCG)와 문자를 읽었다 — 장치 21 · 고정 파이프랙 3",
            questions="같은 질문 → 요건 파일: 가열로·압축기·탱크·제어실 화재/폭발 이격, 공정 배관 (가정값)",
            req="원전의 App.R 화재분리와 같은 구조의 제약",
            result="현황 hard 위반 11 → 0, 건축면적 −9.0%",
        ))
        studio_flow(r, example="fpso_topside", effort="quick", glide=False, caps=dict(
            open="비교군 ② FPSO 상부 모듈 2개 데크 — SVG",
            imp="데크 2개를 한 장에 그린 SVG — 층마다 원점을 맞춰 읽었다. 고정: 터렛·크레인·플레어 기초",
            questions="요건: 위험구역 ↔ 거주구역 분리, 무게중심 편심 (원전의 내진 비틀림 항과 같은 항)",
            req="",
            result="현황 hard 위반 4 → 0 — 이 수치는 합성 현황의 여유 공간 때문이다 (성과로 쓰지 말 것)",
        ))
        studio_flow(r, example="hospital_or", effort="quick", glide=False, caps=dict(
            open="비교군 ③ 병원 수술부 — PNG 래스터 평면도 (문자 없음)",
            imp="벽으로 둘러싸인 실 20개를 영역 분할로 인식 — 이름은 OCR 하지 않고 요건 파일·사용자 답변으로",
            questions="요건: 청결/오염 동선 분리, 수술실 ↔ 멸균 코어 인접",
            req="",
            result="빈틈없이 꽉 찬 평면 — 솔버가 더 작은 실현가능 배치를 못 찾으면 '현황 유지'로 판정한다. 개선을 지어내지 않는다",
        ))

        # ---- 4. ChatGPT 웹 ----
        r.slide("connect", "ChatGPT 웹 · Claude.ai 웹 — 같은 서버를 HTTPS 로 열어 원격 MCP 커넥터로 등록한다", 9.0)

        # ---- 5. 요약 ----
        r.slide("summary", "같은 IR · 같은 목적함수로 현황과 비교한 결과 — 엔진은 표준 라이브러리만, 폐쇄망 설치 가능", 10.0)
        r.slide("title", "", 2.0)

        vid = page.video.path()
        ctx.close()
        br.close()

    mp4 = os.path.join(a.out, "smr_ga_demo.mp4")
    ff = shutil.which("ffmpeg") or "ffmpeg"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", vid, "-c:v", "libx264", "-preset", "slow",
                    "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4], check=True)
    print(mp4)


if __name__ == "__main__":
    main()
