# -*- coding: utf-8 -*-
"""스튜디오 사용 장면 녹화. 화면 숫자는 녹화 중 실제 서버 응답 그대로.
usage: python demo/video/record_studio.py <out_dir> [effort] [port]
events.json 에 장면 시각(초)을 남겨 편집 때 대기 구간을 잘라낸다."""
import json, os, sys, time
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = sys.argv[1]
EFFORT = sys.argv[2] if len(sys.argv) > 2 else "thorough"
URL = f"http://127.0.0.1:{sys.argv[3] if len(sys.argv) > 3 else 8766}/studio"
W, H = 1600, 900
DRAW = os.path.join(ROOT, "examples", "drawings")
FN = os.path.join(ROOT, "examples", "function")

STYLE = """
aside{display:none!important}
.wrap{grid-template-columns:1fr!important;max-width:1140px!important;margin:0 auto!important}
html{zoom:1.35}
#__cap{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:99999;
  background:#15161a;color:#fff;font:500 18px/1.5 'Malgun Gothic',sans-serif;letter-spacing:-.01em;
  padding:10px 22px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.18);max-width:80%;text-align:center;
  transition:opacity .25s}
#__cap.off{opacity:0}
"""


class R:
    def __init__(s, page):
        s.p, s.t0, s.ev = page, time.time(), []

    def mark(s, name):
        s.ev.append([name, round(time.time() - s.t0, 2)])

    def prep(s):
        s.p.add_style_tag(content=STYLE)
        s.p.evaluate("() => { const d = document.createElement('div'); d.id='__cap'; d.className='off'; document.body.appendChild(d); }")

    def cap(s, text, hold=0.0):
        s.p.evaluate("t => { const d = document.getElementById('__cap'); if (!t) { d.className='off'; return; } d.textContent = t; d.className=''; }", text)
        time.sleep(hold)

    def to(s, sel, offset=80, hold=0.0):
        s.p.evaluate("""([q, o]) => { const e = document.querySelector(q);
            if (e) window.scrollTo({top: e.getBoundingClientRect().top + window.scrollY - o, behavior: 'smooth'}); }""", [sel, offset])
        time.sleep(1.0 + hold)

    def glide(s, px, sec):
        n = max(1, int(sec * 30))
        for _ in range(n):
            s.p.evaluate(f"window.scrollBy(0, {px / n})")
            time.sleep(sec / n)


with sync_playwright() as pw:
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": W, "height": H}, record_video_dir=os.path.join(OUT, "raw_studio"),
                         record_video_size={"width": W, "height": H})
    page = ctx.new_page()
    page.on("dialog", lambda d: (print("DIALOG", d.message), d.dismiss()))
    r = R(page)
    page.goto(URL)
    page.wait_for_selector("#ex option[value=smr_aux]", state="attached")
    r.prep()
    r.mark("start")
    time.sleep(1.5)
    r.cap("도면 파일을 올리면 지금 배치를 그대로 읽어 옵니다", 2.5)
    page.set_input_files("#file", os.path.join(DRAW, "smr_aux_3f.dxf"))
    page.wait_for_selector("#imp:not(.hide)", timeout=60000)
    time.sleep(0.8)
    r.to("#imp", 90)
    r.cap("원전 보조건물 3개 층. 기기·계단·복도·해치를 층별로 인식했습니다", 3.5)
    r.glide(700, 4.0)
    r.to("#c2", 80)
    page.click("#mkproj")
    page.wait_for_selector("#proj:not(.hide)")
    time.sleep(0.6)
    r.cap("도면에 없는 정보는 짐작하지 않고 질문으로 남깁니다", 4.5)
    page.set_input_files("#reqfile", os.path.join(DRAW, "smr_aux_3f_requirements.json"))
    time.sleep(1.8)
    r.cap("설계 요건 파일로 답합니다. 층고, 기기 연결, 화재구역, 이격 거리", 3.5)
    page.set_input_files("#reqfile", os.path.join(FN, "smr_aux.json"))
    time.sleep(1.8)
    r.cap("공개 기준에서 찾은 피난·출입 요건을 더합니다. 출처는 파일에 함께 적혀 있습니다", 3.5)
    r.to("#c3", 80)
    page.click("#val")
    page.wait_for_selector("#gate:not(.hide)")
    time.sleep(0.5)
    r.cap("먼저 요건을 검사합니다. 오류가 있으면 다음 단계로 넘어가지 않습니다", 3.5)
    page.click("#appr")
    page.keyboard.type("김민수 책임", delay=110)
    page.check("#ack")
    time.sleep(0.4)
    page.click("#frz")
    page.wait_for_selector("#run:not([disabled])")
    r.cap("담당자가 확인하고 승인해야 계산을 시작합니다", 3.0)
    r.to("#c4", 80)
    page.select_option("#eff", EFFORT)
    time.sleep(0.6)
    page.click("#run")
    r.cap("지금 배치에서 출발해 여러 번 다시 시도하며 더 작은 배치를 찾습니다", 0)
    r.mark("run_start")
    page.wait_for_selector("#res:not(.hide)", timeout=7200000)   # deep 은 한 시간 넘게 걸릴 수 있다
    r.mark("run_done")
    time.sleep(0.8)
    r.to("#c4", 12)
    r.cap("지금 도면과 같은 기준으로 채점합니다. 필수 요건 위반이 남으면 채택하지 않고 그대로 알려 줍니다", 6.5)
    r.to("#cands", 70)
    if page.locator("#cands table").count():
        r.cap("후보안을 나란히 비교합니다. 고르는 것은 담당자입니다", 5.0)
    elif "같은 안" in (page.text_content("#cands") or ""):
        r.cap("후보안 기준이 모두 같은 안을 고르면, 그 안 하나만 보여 줍니다", 5.0)
    else:
        r.cap("요건을 모두 지키는 안이 없으면 후보안을 내지 않고, 위반이 가장 적은 안만 보여 줍니다", 5.0)
    r.to("#resimgs", 70)
    r.cap("층별로 지금 배치와 새 배치를 비교합니다. 빨간 표시는 요건을 어긴 곳", 0)
    r.glide(2000, 8.0)
    link = page.get_attribute("#links a", "href")
    r.mark("report")
    if link:
        page.goto(link)
        page.add_style_tag(content=STYLE)
        page.evaluate("() => { const d = document.createElement('div'); d.id='__cap'; document.body.appendChild(d); }")
        time.sleep(0.6)
        r.cap("판정, 비교표, 층별 도면을 담은 보고서와 CAD(DXF) 파일이 함께 나옵니다", 3.0)
        r.glide(2400, 7.0)
    r.mark("end")
    vid = str(page.video.path())
    ctx.close(); br.close()
json.dump(dict(video=vid, events=r.ev), open(os.path.join(OUT, "studio_events.json"), "w"), ensure_ascii=False, indent=1)
print(vid, r.ev)
