# -*- coding: utf-8 -*-
"""데모 영상용 안내 화면(1920x1080 PNG)과 사례 머리 카드(head01..).
usage: python demo/video/cards.py <out_dir>   — 사례 이름은 docs/data.js 에서 읽는다."""
import base64, html, json, os, sys, asyncio
from playwright.async_api import async_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = sys.argv[1]
LOGO_P = os.path.join(ROOT, "docs", "readme", "meic-logo-black.png")
_s = open(os.path.join(ROOT, "docs", "data.js"), encoding="utf-8").read()
_j = json.loads(_s[_s.index("=") + 1:].strip().rstrip(";"))
CASES = _j["cases"] if isinstance(_j, dict) else _j
LOGO = "data:image/png;base64," + base64.b64encode(open(LOGO_P, "rb").read()).decode()
HEAD = """<!doctype html><meta charset=utf-8><link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;800&display=swap" rel=stylesheet>
<style>*{margin:0;box-sizing:border-box}html,body{width:1920px;height:1080px;background:#f5f3ed;color:#15161a;font-family:'Noto Sans KR','Malgun Gothic',sans-serif;word-break:keep-all}
.logo{position:fixed;top:32px;right:48px;height:87px;mix-blend-mode:multiply}
.k{font-size:30px;color:#1f4fd1;font-weight:700}
.wrap{height:100%;display:flex;flex-direction:column;justify-content:center;padding:0 160px}
.t{font-size:80px;font-weight:800;line-height:1.25;margin-top:20px;letter-spacing:-.01em}
.s{font-size:34px;color:#6d6c66;margin-top:40px;line-height:1.55}
.grid{display:grid;gap:28px;margin-top:56px}
.card{background:#fbfaf6;border:1px solid #dedbd2;border-radius:20px;padding:36px 34px}
.card .n{font-size:28px;font-weight:800;color:#1f4fd1}
.card h3{font-size:36px;font-weight:800;margin:10px 0 14px}
.card p{font-size:25px;line-height:1.6;color:#3d3c38}
.foot{font-size:24px;color:#6d6c66;margin-top:40px}
</style><img class=logo src=\"""" + LOGO + """\">"""

def chapter(n, title, sub):
    return HEAD + f"""<div class=wrap><div class=k>{n}</div><div class=t>{title}</div><div class=s>{sub}</div></div>"""

P = {}
P["intro"] = HEAD + """<div class=wrap><div class=k>GA 배치 최적화</div>
<div class=t>도면을 넣으면, 각 공간의 역할을 지키면서<br>더 작은 배치를 찾습니다</div>
<div class=s>사용 방법과 적용 사례</div></div>"""
P["how"] = HEAD + """<div class=wrap style="padding:0 120px"><div class=k>이렇게 동작합니다</div>
<div class=grid style="grid-template-columns:repeat(4,1fr)">
<div class=card><div class=n>1</div><h3>도면 넣기</h3><p>DXF, PDF, SVG, PNG, IFC 도면에서 기기와 실의 위치와 크기를 읽어 지금 배치를 그대로 옮깁니다.</p></div>
<div class=card><div class=n>2</div><h3>기준 정하기</h3><p>이격, 피난, 출입 같은 요건을 공개 법령과 설계 지침에서 찾아 출처와 함께 넣습니다. 확인하지 못한 값은 가정으로 표시합니다.</p></div>
<div class=card><div class=n>3</div><h3>검사와 승인</h3><p>요건에 빠진 값이나 모순이 없는지 검사하고, 담당자가 승인해야 계산을 시작합니다.</p></div>
<div class=card><div class=n>4</div><h3>계산과 선택</h3><p>지금 배치와 같은 기준으로 채점하며 더 작은 배치를 찾고, 성격이 다른 후보안 가운데 담당자가 고릅니다.</p></div>
</div>
<div class=foot>배치 좌표는 도면 판독과 계산 엔진만 만듭니다. 같은 입력이면 언제 돌려도 같은 결과가 나옵니다.</div></div>"""
P["ch1"] = chapter("사용 방법 1", "브라우저 화면에서", "설치 없이 도면을 올리고, 화면의 단계를 따라 진행합니다")
P["ch2"] = chapter("사용 방법 2", "대화로 요청", "Claude Code 같은 AI 도구에 말로 요청하면 같은 단계를 순서대로 진행합니다")
P["ch3"] = HEAD + """<div class=wrap style="padding:0 120px"><div class=k>사용 방법 3</div><div class=t style="font-size:64px">쓰는 환경에 맞게 연결</div>
<div class=grid style="grid-template-columns:repeat(3,1fr)">
<div class=card><h3>내부망 PC · 폐쇄망</h3><p>Python만 있으면 설치됩니다. 추가 라이브러리도, 인터넷 연결도 필요 없습니다. 도면이 밖으로 나가지 않습니다.</p></div>
<div class=card><h3>AI 코딩 도구</h3><p>Claude Code, Claude Desktop, Codex에 플러그인으로 연결합니다. 같은 도구를 대화로 씁니다.</p></div>
<div class=card><h3>웹 AI 서비스</h3><p>ChatGPT, Claude 웹에 커넥터로 등록할 수 있습니다. 이때는 도면이 해당 서비스로 전달되므로 실제 설계 자료에는 쓰지 않습니다.</p></div>
</div></div>"""
P["ch4"] = chapter("결과 확인", "3D로 비교하고 후보안 고르기", "계산 결과를 웹 페이지에서 돌려 보며 지금 배치와 비교합니다")
P["ch5"] = chapter("적용 사례", "여섯 가지 도면에 적용한 결과", "왼쪽은 지금 배치, 오른쪽은 AI가 제안한 배치입니다")
P["end"] = HEAD.replace('<img class=logo', '<img style="display:none"') + f"""<div class=wrap style="align-items:center;text-align:center">
<img src="{LOGO}" style="height:200px;mix-blend-mode:multiply">
<div class=k style="margin-top:48px;color:#15161a;font-size:40px;font-weight:800">GA 배치 최적화</div>
<div class=s style="margin-top:18px;font-size:26px">개념 검토용 · NOT FOR CONSTRUCTION</div></div>"""

E = html.escape
for i, c in enumerate(CASES):       # 사례 변형 장면 위에 얹는 머리 카드 (높이 150)
    P[f"head{i+1:02d}"] = HEAD + f"""<div style="height:150px;display:flex;align-items:center;gap:36px;padding:0 64px;border-bottom:2px solid #dedbd2">
<div style="font-size:72px;font-weight:800;color:#1f4fd1;font-variant-numeric:tabular-nums">{i+1:02d}</div>
<div><div style="font-size:48px;font-weight:800;line-height:1.2">{E(c['title'])}</div>
<div style="font-size:26px;color:#6d6c66;margin-top:4px">{E(c['desc'])}</div></div></div>"""


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1920, "height": 1080})
        for k, h in P.items():
            await pg.set_content(h, wait_until="networkidle"); await pg.wait_for_timeout(300)
            await pg.screenshot(path=os.path.join(OUT, k + ".png"))
        await b.close()
asyncio.run(main())
print("ok", list(P))
