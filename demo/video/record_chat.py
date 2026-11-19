# -*- coding: utf-8 -*-
"""Claude Code 대화 장면(chat.html 재생) 녹화. usage: python demo/video/record_chat.py <out_dir> <prompt.txt>"""
import json, os, sys, time
from playwright.sync_api import sync_playwright
OUT = sys.argv[1]; HERE = os.path.dirname(os.path.abspath(__file__))
prompt = open(sys.argv[2], encoding="utf-8").read().strip()
with sync_playwright() as pw:
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": 1600, "height": 900}, record_video_dir=os.path.join(OUT, "raw_chat"),
                         record_video_size={"width": 1600, "height": 900})
    p = ctx.new_page(); t0 = time.time()
    p.goto("file://" + os.path.join(HERE, "chat.html")); time.sleep(0.8)
    st = round(time.time() - t0, 2)
    p.evaluate("p => window.start(p, 1.0)", prompt)
    p.wait_for_function("window.done === true", timeout=120000)
    en = round(time.time() - t0, 2)
    vid = str(p.video.path()); ctx.close(); br.close()
json.dump(dict(video=vid, events=[["start", st], ["end", en]]), open(os.path.join(OUT, "chat_events.json"), "w"))
print(vid, st, en)
