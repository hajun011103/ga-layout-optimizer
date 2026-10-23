---
description: 브라우저 스튜디오(업로드·검증·승인·실행·현황 비교 화면)와 원격 MCP 엔드포인트를 띄운다. ChatGPT·Claude 웹 연결에도 같은 서버를 쓴다.
argument-hint: "[포트 (기본 8765)]"
allowed-tools: Bash(python3 *)
---

다음을 백그라운드로 실행하고 주소를 사용자에게 알려라.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/server/mcp_server.py --http --port ${ARGUMENTS:-8765}
```

- 스튜디오: `http://127.0.0.1:<포트>/studio`
- MCP 엔드포인트: `http://127.0.0.1:<포트>/mcp`

외부(ChatGPT 웹 등)에 열어야 하면 README 「ChatGPT 웹 연결」을 따르게 하고,
**도면·결과가 외부 LLM 서비스로 전달된다는 점**을 먼저 알린다 (실제 원전 설계값 금지).
