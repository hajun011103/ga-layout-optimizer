# 데모 영상 다시 만들기 (v2)

README 맨 위 영상과 `docs/media/cases.mp4` 를 만드는 스크립트다. 엔진과 무관한 개발 도구라
Playwright·ffmpeg 는 **녹화 전용 환경**에만 설치한다 (설치 방법은 [../README.md](../README.md)).

| 스크립트 | 만드는 것 |
|---|---|
| `cards.py <work>/cards` | 안내 화면(intro·how·ch1~5·end)과 사례 머리 카드(head01..) — 사례 이름은 `docs/data.js` 에서 읽음 |
| `record_studio.py <work> [effort] [port]` | 스튜디오 사용 장면. 화면 숫자는 녹화 중 실제 서버 응답 |
| `record_chat.py <work> chat_prompt.txt` | Claude Code 대화 장면 — `chat.html` 은 실제 `claude -p` 세션의 도구 호출 순서와 결과 수치를 옮긴 재생 화면 |
| `record_web.py <work> [page_url]` | 3D 결과 페이지에서 후보안 고르기·변형 재생 |
| `record_cases.py <work>/frames [page_url] [--only 1,4]` | 사례별 현황 → 최적화 변형 프레임 |
| `assemble.sh <work> demo\|cases [out.mp4]` | 위 조각을 이어 붙인 영상 |
| `export_media.sh <work> <cases.mp4> [gif_dir]` | README 의 `cases.gif`·`morph-fpso.gif`, 사례별 GIF·MP4 |

```bash
W=demo/out/v2; mkdir -p $W/cards
export LD_LIBRARY_PATH=$CONDA_PREFIX/lib          # smr-ga-demo 환경 (nss·nspr)
python3 server/mcp_server.py --http --port 8766 --workspace $W/ws &
python demo/video/cards.py $W/cards
python demo/video/record_studio.py $W thorough 8766
python demo/video/record_chat.py  $W demo/video/chat_prompt.txt
python demo/video/record_web.py   $W
python demo/video/record_cases.py $W/frames
bash demo/video/assemble.sh $W demo  $W/smr_ga_demo.mp4
bash demo/video/assemble.sh $W cases docs/media/cases.mp4
bash demo/video/export_media.sh $W docs/media/cases.mp4 $W/gif
```

- 사례 프레임과 3D 장면은 기본으로 공개 페이지를 찍는다. 아직 올리지 않은 `docs/` 를 찍으려면
  `python3 -m http.server 8777 -d docs` 로 띄우고 `http://127.0.0.1:8777/` 을 넘긴다.
- 스튜디오 장면의 승인자 이름은 가상 인물이다.
- 3D 장면은 GPU 없이 SwiftShader 로 그린다 (`--use-angle=swiftshader`).
