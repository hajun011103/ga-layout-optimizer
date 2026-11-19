# -*- coding: utf-8 -*-
"""3D 결과 페이지에서 후보안 고르기·변형 재생·근거 보기 장면 녹화.
usage: python demo/video/record_web.py <out_dir> [page_url]"""
import json, os, sys, time
from playwright.sync_api import sync_playwright

OUT = sys.argv[1]
URL = sys.argv[2] if len(sys.argv) > 2 else "https://hajun011103.github.io/ga-layout-optimizer/"
W, H = 1600, 900
CAPCSS = """#__cap{position:fixed;left:50%;bottom:30px;transform:translateX(-50%);z-index:99999;
  background:#15161a;color:#fff;font:500 23px/1.5 'Malgun Gothic',sans-serif;letter-spacing:-.01em;
  padding:13px 28px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.18);max-width:80%;text-align:center;transition:opacity .25s}
#__cap.off{opacity:0}"""

with sync_playwright() as pw:
    br = pw.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
    ctx = br.new_context(viewport={"width": W, "height": H}, color_scheme="light",
                         record_video_dir=os.path.join(OUT, "raw_web"), record_video_size={"width": W, "height": H})
    p = ctx.new_page()
    t0 = time.time(); ev = []
    mark = lambda n: ev.append([n, round(time.time() - t0, 2)])
    p.goto(URL, wait_until="networkidle")
    p.add_style_tag(content=CAPCSS)
    p.evaluate("() => { const d = document.createElement('div'); d.id='__cap'; d.className='off'; document.body.appendChild(d); }")

    def cap(t, hold=0):
        p.evaluate("t => { const d = document.getElementById('__cap'); if (!t) { d.className='off'; return; } d.textContent = t; d.className=''; }", t)
        time.sleep(hold)

    def to(sel, off=10, hold=0.0, how='smooth'):
        p.evaluate("""([q, o, b]) => { const e = document.querySelector(q);
            window.scrollTo({top: e.getBoundingClientRect().top + window.scrollY - o, behavior: b}); }""", [sel, off, how])
        time.sleep((1.1 if how == 'smooth' else 0.05) + hold)

    p.locator(".tab[data-i='2']").click()
    time.sleep(2.5)
    to("#title", 70, how="instant")
    time.sleep(1.0)
    mark("start")
    cap("후보안을 여러 개 계산합니다. 모두 같은 요건을 지킨 안이고, 고르는 것은 담당자입니다", 4.0)
    picks = p.locator("#picks .pick")
    for k in (1, 2, 0):
        p.evaluate("k => document.querySelectorAll('#picks .pick')[k].click()", k)
        to("#title", 70, how="instant")
        time.sleep(2.0)
    cap("", 0)
    to("#stage", 0)
    cap("3D로 지금 배치와 새 배치를 나란히 봅니다", 2.0)
    mark("play1"); p.evaluate("document.getElementById('c-play').click()")
    time.sleep(0.5)
    cap("지금 배치에서 새 배치로 옮겨 가는 모습", 0)
    time.sleep(4.0)
    p.evaluate("document.getElementById('c-play').click()")
    time.sleep(4.4)
    cap("돌려 보고, 층을 띄워 볼 수도 있습니다", 0)
    p.evaluate("() => { const c = document.getElementById('c-spin'); c.checked = true; c.dispatchEvent(new Event('change')); }")
    time.sleep(3.0)
    for v in range(0, 21, 2):
        p.evaluate("v => { const s = document.getElementById('c-explode'); s.value = v; s.dispatchEvent(new Event('input')); }", v)
        time.sleep(0.12)
    time.sleep(3.0)
    p.evaluate("() => { const c = document.getElementById('c-spin'); c.checked = false; c.dispatchEvent(new Event('change')); }")
    p.evaluate("() => { const s = document.getElementById('c-explode'); s.value = 0; s.dispatchEvent(new Event('input')); }")
    cap("", 0)
    to("#rs", 150)
    cap("요건마다 출처를 붙여 둡니다. 확인한 기준과 가정한 값을 나눠 표시합니다", 0)
    for _ in range(100):
        p.evaluate("window.scrollBy(0, 5)"); time.sleep(1 / 30)
    time.sleep(2.5)
    mark("end")
    vid = str(p.video.path())
    ctx.close(); br.close()
json.dump(dict(video=vid, events=ev), open(os.path.join(OUT, "web_events.json"), "w"), ensure_ascii=False, indent=1)
print(vid, ev)
