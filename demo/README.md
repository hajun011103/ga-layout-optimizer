# 시연 영상 다시 녹화하기

녹화는 엔진과 무관한 개발 도구다 (Playwright·ffmpeg 필요 — 엔진 환경에 설치하지 말 것).

```bash
# 녹화 전용 환경 (한 번만)
conda create -y -n smr-ga-demo -c conda-forge python=3.11 ffmpeg nspr nss alsa-lib
conda activate smr-ga-demo && pip install playwright && python -m playwright install chromium

# 1) 새 작업공간으로 서버
python3 server/mcp_server.py --http --port 8765 --workspace /tmp/ga-demo-ws
# 2) 녹화 → demo/out/smr_ga_demo.mp4
LD_LIBRARY_PATH=$CONDA_PREFIX/lib python demo/record_demo.py --out demo/out
```

- 화면의 숫자는 녹화 중 실제 MCP 호출 결과다.
- Claude Code 장면은 `claude -p … --mcp-config` 실제 실행에서 도구 호출 단계만 뽑은
  `claude_session_steps.json` 을 속도만 조절해 재생한다 (원본 기록은 환경 정보가 섞여 있어 저장하지 않았다).
- ChatGPT 웹 화면은 사용자 계정이 필요해 녹화하지 않았다. 대신 같은 HTTP 엔드포인트에
  실제로 `initialize`·`tools/list` 를 보내 응답을 보여준다.
- 한글 글꼴이 없으면 글자가 깨진다 (WSL: `cp /mnt/c/Windows/Fonts/malgun*.ttf ~/.local/share/fonts/ && fc-cache -f`).
- 공개 저장소용으로 재생 기록의 로컬 경로와 노형 명칭은 일반화했다 (도구 호출 순서와 결과 수치는 그대로).
