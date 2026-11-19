# -*- coding: utf-8 -*-
"""3D 페이지의 사례별 '현황 → 최적화' 변형을 프레임(PNG)으로 찍는다 (사례 GIF·MP4 와 영상 마지막 장면의 원본).
usage: python demo/video/record_cases.py <out_dir> [page_url] [--only 1,4] [--frames 60]
docs/ 를 로컬로 띄워 찍으려면: python3 -m http.server 8777 -d docs  →  page_url=http://127.0.0.1:8777/"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

args = [a for a in sys.argv[1:]]
ONLY = set(int(x) for x in args[args.index("--only") + 1].split(",")) if "--only" in args else None
N = int(args[args.index("--frames") + 1]) if "--frames" in args else 60
pos = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
OUT = pos[0]
URL = pos[1] if len(pos) > 1 else "https://hajun011103.github.io/ga-layout-optimizer/"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        pg = await b.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1.5, color_scheme="light")
        await pg.goto(URL, wait_until="networkidle")
        await pg.add_style_tag(content=".cases{position:static!important}")   # 위에 붙는 탭 줄이 화면을 가리지 않게
        await pg.wait_for_timeout(2500)
        tabs = await pg.locator(".tab[data-i]").all()
        for i, t in enumerate(tabs):
            if ONLY and i + 1 not in ONLY:
                continue
            await t.click()
            await pg.wait_for_timeout(2000)
            views = pg.locator(".views")
            await views.scroll_into_view_if_needed()
            await pg.wait_for_timeout(500)
            d = os.path.join(OUT, f"case{i + 1:02d}")
            os.makedirs(d, exist_ok=True)
            for k in range(N + 1):
                v = round(1000 * k / N)
                await pg.evaluate("v => { const s = document.getElementById('c-morph'); s.value = v; s.dispatchEvent(new Event('input')); }", v)
                await pg.wait_for_timeout(60)
                await views.screenshot(path=os.path.join(d, f"f{k:03d}.png"))
            print(i + 1, (await t.inner_text()).replace("\n", " "), "done", flush=True)
        await b.close()

asyncio.run(main())
