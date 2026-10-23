# -*- coding: utf-8 -*-
"""
mcp_server.py — SMR GA 배치최적화 MCP 서버. Python 표준 라이브러리만.

두 가지 전송을 하나의 서버가 제공한다.

  stdio  (기본)  Claude Code · Claude Desktop · Codex CLI · Cursor 등 로컬 클라이언트
      python3 server/mcp_server.py

  http           ChatGPT(웹, 개발자 모드 커넥터) · Claude.ai 커스텀 커넥터 · MCP Inspector
      python3 server/mcp_server.py --http --port 8765
      → MCP 엔드포인트  http://127.0.0.1:8765/mcp      (Streamable HTTP, JSON 응답)
        브라우저 스튜디오 http://127.0.0.1:8765/studio  (업로드·검증·승인·실행·비교)
      ChatGPT 웹은 공개 HTTPS 주소가 필요하다 → README 「ChatGPT 웹 연결」(터널) 참조.

★ 데이터 경계: 엔진은 이 컴퓨터에서 돈다. 그러나 MCP 를 부르는 LLM(Claude/ChatGPT)은
  도구 응답(기기 이름·치수·결과 수치)을 자기 서버로 가져간다. 실제 원전 설계값은
  공개값·가정치로 바꿔 쓸 것 (README 한계 13항).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tools as T          # noqa: E402
from version import ENGINE_VERSION   # noqa: E402  (engine/ 는 tools 가 sys.path 에 넣는다)

SERVER_NAME = "smr-ga"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "SMR/플랜트 General Arrangement 배치 최적화 도구. 흐름: "
    "ga_supported_formats → (도면) ga_import_drawing → 미리보기를 사용자에게 확인 → ga_create_project "
    "→ questions 를 사용자에게 묻고 ga_update_ir → ga_validate → 사용자 명시 승인 후 ga_freeze → "
    "ga_optimize → ga_run_status → ga_explain. 도면이 없으면 ga_create_project_from_ir, 시험은 ga_list_examples. "
    "규칙: (1) 좌표·치수를 지어내지 않는다 — 좌표는 파서와 솔버만 만든다. (2) 모르는 값은 사용자에게 묻는다. "
    "(3) 사용자 승인 없이 ga_freeze 를 부르지 않는다. (4) 결과 숫자는 도구 응답 값을 그대로 인용한다. "
    "(5) hard 위반이 0 이 아니면 해를 채택할 수 없다고 경고한다. (6) 'AI 가 배치했다' 가 아니라 '솔버가 계산했다'. "
    "(7) 산출물은 NOT FOR CONSTRUCTION 개념검토용이다."
)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ===========================================================================
# JSON-RPC / MCP 디스패치 (전송과 무관)
# ===========================================================================

class MCP:
    def __init__(self, tools: T.Tools):
        self.tools = tools
        self.defs = {d["name"]: d for d in T.TOOL_DEFS}

    def tool_list(self):
        out = []
        for d in T.TOOL_DEFS:
            out.append(dict(
                name=d["name"], title=d["name"].replace("ga_", "GA ").replace("_", " "),
                description=d["description"], inputSchema=d["schema"],
                annotations=dict(readOnlyHint=bool(d["ro"]), destructiveHint=False,
                                 idempotentHint=bool(d["ro"]), openWorldHint=False),
            ))
        return out

    def call_tool(self, name: str, args: Dict[str, Any]) -> dict:
        if name not in self.defs:
            return dict(isError=True, content=[dict(type="text", text=f"알 수 없는 도구: {name}")])
        fn = getattr(self.tools, name)
        t0 = time.time()
        try:
            res = fn(**(args or {}))
        except T.ToolError as ex:
            return dict(isError=True, content=[dict(type="text", text=f"[{name}] {ex}")])
        except TypeError as ex:
            return dict(isError=True, content=[dict(type="text", text=f"[{name}] 인자 오류: {ex}")])
        except Exception as ex:   # noqa: BLE001
            log(traceback.format_exc())
            return dict(isError=True, content=[dict(type="text",
                                                    text=f"[{name}] 내부 오류 {type(ex).__name__}: {ex}")])
        txt = json.dumps(res, ensure_ascii=False, indent=1, default=str)
        if len(txt) > 60000:
            txt = txt[:60000] + "\n… (잘림 — structuredContent 또는 링크를 볼 것)"
        log(f"tool {name} {time.time()-t0:.2f}s")
        return dict(content=[dict(type="text", text=txt)], structuredContent=res, isError=False)

    def handle(self, msg: dict) -> Optional[dict]:
        """요청 1건 → 응답 dict (알림이면 None)."""
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method is None:
            return None                         # 클라이언트의 응답 메시지 — 무시
        is_note = "id" not in msg

        def ok(result):
            return None if is_note else {"jsonrpc": "2.0", "id": mid, "result": result}

        def err(code, text):
            return None if is_note else {"jsonrpc": "2.0", "id": mid,
                                         "error": {"code": code, "message": text}}
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                ver = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                return ok(dict(protocolVersion=ver,
                               capabilities=dict(tools=dict(listChanged=False)),
                               serverInfo=dict(name=SERVER_NAME, title="SMR GA Layout Optimizer",
                                               version=ENGINE_VERSION),
                               instructions=INSTRUCTIONS))
            if method in ("notifications/initialized", "notifications/cancelled", "initialized"):
                return None
            if method == "ping":
                return ok({})
            if method == "tools/list":
                return ok(dict(tools=self.tool_list()))
            if method == "tools/call":
                return ok(self.call_tool(params.get("name"), params.get("arguments") or {}))
            if method in ("resources/list",):
                return ok(dict(resources=[]))
            if method in ("prompts/list",):
                return ok(dict(prompts=[]))
            return err(-32601, f"method not found: {method}")
        except Exception as ex:   # noqa: BLE001
            log(traceback.format_exc())
            return err(-32603, f"internal error: {ex}")

    def handle_payload(self, payload):
        if isinstance(payload, list):
            out = [r for r in (self.handle(m) for m in payload) if r is not None]
            return out or None
        return self.handle(payload)


# ===========================================================================
# stdio
# ===========================================================================

def serve_stdio(mcp: MCP):
    out = sys.stdout
    sys.stdout = sys.stderr          # 엔진의 print 가 프로토콜 스트림을 오염시키지 않게
    wlock = threading.Lock()
    log(f"[smr-ga] MCP stdio 서버 시작 (엔진 {ENGINE_VERSION}, 작업공간 {mcp.tools.ws.root})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = mcp.handle_payload(msg)
        if resp is not None:
            with wlock:
                out.write(json.dumps(resp, ensure_ascii=False) + "\n")
                out.flush()


# ===========================================================================
# HTTP (Streamable HTTP + 스튜디오 + 업로드 + 산출물)
# ===========================================================================

CTYPES = {".svg": "image/svg+xml", ".html": "text/html; charset=utf-8", ".json": "application/json",
          ".dxf": "application/dxf", ".png": "image/png", ".txt": "text/plain; charset=utf-8",
          ".css": "text/css", ".js": "text/javascript"}


def make_handler(mcp: MCP, token: Optional[str], cors: bool):
    ws = mcp.tools.ws
    sessions: Dict[str, float] = {}

    class H(BaseHTTPRequestHandler):
        server_version = f"smr-ga-mcp/{ENGINE_VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log("[http] " + (fmt % args))

        # ---- 공통 ----
        def _route(self):
            """토큰 경로 접두사(/t/<token>/...) 처리 → (경로, 인증 여부)."""
            path = urllib.parse.urlparse(self.path).path
            authed = token is None
            m = re.match(r"^/t/([^/]+)(/.*)?$", path)
            if m:
                if token and secrets.compare_digest(m.group(1), token):
                    authed = True
                path = m.group(2) or "/"
            hdr = self.headers.get("Authorization", "")
            if token and hdr.startswith("Bearer ") and secrets.compare_digest(hdr[7:].strip(), token):
                authed = True
            return path, authed

        def _send(self, code, body: bytes, ctype="application/json", extra: Dict[str, str] = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type, Authorization, Mcp-Session-Id, MCP-Protocol-Version, X-Filename")
                self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, obj, extra=None):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json", extra)

        def _body(self, limit=T.MAX_FILE_BYTES) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise ValueError("too large")
            return self.rfile.read(n) if n else b""

        def do_OPTIONS(self):
            self._send(204, b"", "text/plain")

        # ---- GET ----
        def do_GET(self):
            path, authed = self._route()
            if path in ("/", "/studio", "/studio/"):
                if not authed:
                    return self._send(401, "토큰이 필요하다".encode(), "text/plain; charset=utf-8")
                with open(os.path.join(HERE, "studio.html"), encoding="utf-8") as fp:
                    html = fp.read()
                base = "/t/" + token if token else ""
                html = html.replace("__BASE__", base)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/healthz":
                return self._json(200, dict(ok=True, engine=ENGINE_VERSION))
            if path == "/mcp":
                # 서버→클라이언트 SSE 스트림은 제공하지 않는다 (스펙상 허용: 405)
                return self._send(405, b"", "text/plain", {"Allow": "POST"})
            m = re.match(r"^/(runs|takeoffs)/([0-9a-f]{8,64})(?:/|_)([\w.\-]+)$", path)
            if m:
                if not authed:
                    return self._send(401, b"unauthorized", "text/plain")
                kind, rid, name = m.groups()
                if kind == "runs":
                    fp = ws.p("runs", rid, name)
                else:
                    fp = ws.p("takeoffs", f"{rid}_{name}")
                if ".." in name or not os.path.isfile(fp):
                    return self._send(404, b"not found", "text/plain")
                with open(fp, "rb") as f:
                    data = f.read()
                ct = CTYPES.get(os.path.splitext(fp)[1], "application/octet-stream")
                extra = {}
                if fp.endswith(".dxf"):
                    extra["Content-Disposition"] = f'attachment; filename="{name}"'
                return self._send(200, data, ct, extra)
            return self._send(404, b"not found", "text/plain")

        do_HEAD = do_GET

        # ---- POST ----
        def do_POST(self):
            path, authed = self._route()
            if not authed:
                return self._json(401, {"error": "unauthorized"})
            if path == "/upload":
                try:
                    data = self._body()
                except ValueError:
                    return self._json(413, {"error": "파일이 너무 크다"})
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                name = urllib.parse.unquote(self.headers.get("X-Filename") or
                                            (q.get("filename") or ["drawing"])[0])
                try:
                    return self._json(200, ws.store_file(data, name))
                except T.ToolError as ex:
                    return self._json(400, {"error": str(ex)})
            if path != "/mcp":
                return self._json(404, {"error": "not found"})
            try:
                payload = json.loads(self._body(limit=20 * 1024 * 1024) or b"null")
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "parse error"}})
            extra = {}
            first = payload[0] if isinstance(payload, list) and payload else payload
            if isinstance(first, dict) and first.get("method") == "initialize":
                sid = secrets.token_hex(16)
                sessions[sid] = time.time()
                extra["Mcp-Session-Id"] = sid
            resp = mcp.handle_payload(payload)
            if resp is None:
                return self._send(202, b"", "text/plain", extra)
            return self._json(200, resp, extra)

        def do_DELETE(self):
            path, authed = self._route()
            sid = self.headers.get("Mcp-Session-Id")
            if path == "/mcp" and sid:
                sessions.pop(sid, None)
            return self._send(204, b"", "text/plain")

    return H


def serve_http(mcp: MCP, host: str, port: int, token: Optional[str], cors: bool):
    httpd = ThreadingHTTPServer((host, port), make_handler(mcp, token, cors))
    httpd.daemon_threads = True
    pre = f"/t/{token}" if token else ""
    base = mcp.tools.ws.public_url or f"http://{host}:{port}{pre}"
    log(f"[smr-ga] MCP HTTP 서버: {base}/mcp")
    log(f"[smr-ga] 스튜디오     : {base}/studio")
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        log("[smr-ga] ★ 경고: 외부 인터페이스에 토큰 없이 열려 있다. --token 을 쓸 것.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SMR GA 배치최적화 MCP 서버")
    ap.add_argument("--http", action="store_true", help="HTTP 모드 (기본은 stdio)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workspace", default=os.environ.get("SMR_GA_WORKSPACE", "~/.smr-ga/workspace"))
    ap.add_argument("--public-url", help="외부에서 보이는 주소 (터널 https 주소). 도구 응답의 링크에 쓰인다")
    ap.add_argument("--token", help="접근 토큰. /t/<token>/mcp 경로 또는 Authorization: Bearer")
    ap.add_argument("--allow-local-paths", action="store_true",
                    help="HTTP 모드에서도 path 인자로 서버 로컬 파일을 읽게 한다 (기본: stdio 만 허용)")
    ap.add_argument("--allow-url-fetch", action="store_true",
                    help="url 인자로 외부 파일 다운로드 허용 (HTTP 모드 기본 허용, stdio 기본 금지)")
    ap.add_argument("--no-cors", action="store_true")
    a = ap.parse_args(argv)

    if a.http:
        pre = f"/t/{a.token}" if a.token else ""
        pub = a.public_url.rstrip("/") + pre if a.public_url else f"http://{a.host}:{a.port}{pre}"
        ws = T.Workspace(a.workspace, allow_local_paths=a.allow_local_paths,
                         allow_url_fetch=True, public_url=pub)
        serve_http(MCP(T.Tools(ws)), a.host, a.port, a.token, cors=not a.no_cors)
    else:
        ws = T.Workspace(a.workspace, allow_local_paths=True,
                         allow_url_fetch=a.allow_url_fetch, public_url=None)
        serve_stdio(MCP(T.Tools(ws)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
