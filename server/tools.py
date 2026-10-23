# -*- coding: utf-8 -*-
"""
server/tools.py — MCP 도구 구현 + 작업공간. 표준 라이브러리만.

LLM(Claude/GPT)은 이 도구들을 '호출' 만 한다. 도구는 다음을 강제한다.
  · 좌표는 도면 파서만 쓴다 — LLM 이 보내는 IR/패치에 x·y·fixed_xy 가 있으면 거부
  · 최적화는 Gate 1 통과 + 사람 승인(동결)된 IR 로만 — 동결 후 IR 이 바뀌면 다시 승인
  · 모든 숫자는 결정론적 엔진 출력 — 도구 응답의 숫자를 LLM 이 다시 쓰지 않도록 안내

작업공간 (기본 ~/.smr-ga/workspace)
  files/<file_id>/<filename>        업로드 원본
  takeoffs/<takeoff_id>.json         도면 인식 결과
  projects/<project_id>/ir.json      IR (편집 가능) · asis.json · questions.json · history.jsonl
                       frozen.json   Gate 1 승인본 (+ approval)
  runs/<run_id>/                     산출물 (SVG/DXF/JSON/report.html)
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "engine")
if ENGINE not in sys.path:
    sys.path.insert(0, ENGINE)

import asis as ASIS            # noqa: E402
import constraints as CONS     # noqa: E402
import decompose               # noqa: E402
import ga_core as G            # noqa: E402
import importers               # noqa: E402
import irhash                  # noqa: E402
import pipeline                # noqa: E402
import takeoff as TAKEOFF      # noqa: E402
import validate_spec           # noqa: E402
from importers.preview import takeoff_svg   # noqa: E402
from version import ENGINE_VERSION          # noqa: E402


def _engine_fingerprint() -> str:
    """엔진 소스 해시 — 결과 캐시 키. 코드가 바뀌면 같은 IR 이라도 다시 계산한다."""
    h = hashlib.sha256()
    for root, _d, files in os.walk(ENGINE):
        for f in sorted(files):
            if f.endswith(".py"):
                with open(os.path.join(root, f), "rb") as fp:
                    h.update(f.encode() + fp.read())
    return h.hexdigest()[:16]


ENGINE_FP = _engine_fingerprint()

MAX_FILE_BYTES = 100 * 1024 * 1024
ID_RX = re.compile(r"^[0-9a-f]{8,64}$")
COORD_KEYS = {"fixed_xy", "x", "y", "cx", "cy"}


class ToolError(Exception):
    """사용자/LLM 이 고칠 수 있는 오류. 메시지는 그대로 전달된다."""


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Workspace:
    def __init__(self, root: str, *, allow_local_paths: bool, allow_url_fetch: bool,
                 public_url: Optional[str] = None):
        self.root = os.path.abspath(os.path.expanduser(root))
        for d in ("files", "takeoffs", "projects", "runs"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        self.allow_local_paths = allow_local_paths
        self.allow_url_fetch = allow_url_fetch
        self.public_url = public_url.rstrip("/") if public_url else None
        self.runs: Dict[str, dict] = {}
        self.lock = threading.Lock()
        self.run_slots = threading.Semaphore(2)

    # ---- 경로 ---------------------------------------------------------------
    def p(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    def url(self, rel: str) -> Optional[str]:
        return f"{self.public_url}/{rel}" if self.public_url else None

    def link(self, rel: str) -> dict:
        """도구 응답에 넣는 산출물 위치 — HTTP 모드면 URL, 로컬이면 파일 경로."""
        out = {"path": self.p(*rel.split("/"))}
        u = self.url(rel)
        if u:
            out["url"] = u
        return out

    @staticmethod
    def _check_id(x: str, what: str) -> str:
        if not isinstance(x, str) or not ID_RX.match(x):
            raise ToolError(f"{what} 형식이 잘못됐다: {x!r}")
        return x

    # ---- 파일 ---------------------------------------------------------------
    def store_file(self, data: bytes, filename: str) -> dict:
        if len(data) > MAX_FILE_BYTES:
            raise ToolError(f"파일이 너무 크다 ({len(data)/1e6:.1f} MB > {MAX_FILE_BYTES/1e6:.0f} MB)")
        if not data:
            raise ToolError("빈 파일")
        fid = _sha(data)[:16]
        safe = re.sub(r"[^\w.\-가-힣 ]+", "_", os.path.basename(filename or "drawing")) or "drawing"
        d = self.p("files", fid)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, safe)
        if not os.path.exists(path):
            with open(path, "wb") as fp:
                fp.write(data)
        return dict(file_id=fid, filename=safe, bytes=len(data),
                    format_guess=importers.sniff(data, safe))

    def load_file(self, fid: str):
        self._check_id(fid, "file_id")
        d = self.p("files", fid)
        if not os.path.isdir(d):
            raise ToolError(f"file_id {fid} 없음 — 먼저 업로드할 것")
        names = sorted(os.listdir(d))
        with open(os.path.join(d, names[0]), "rb") as fp:
            return fp.read(), names[0]

    # ---- JSON 저장 ----------------------------------------------------------
    @staticmethod
    def wjson(path: str, obj: Any):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(obj, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @staticmethod
    def rjson(path: str):
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)

    def project_dir(self, pid: str) -> str:
        self._check_id(pid, "project_id")
        d = self.p("projects", pid)
        if not os.path.isdir(d):
            raise ToolError(f"project_id {pid} 없음")
        return d

    def log(self, pid: str, event: dict):
        with open(os.path.join(self.project_dir(pid), "history.jsonl"), "a",
                  encoding="utf-8") as fp:
            fp.write(json.dumps(dict(event, at=_now()), ensure_ascii=False) + "\n")


# ===========================================================================
# 도구
# ===========================================================================

class Tools:
    def __init__(self, ws: Workspace):
        self.ws = ws

    # ---------------------------------------------------------------- 안내
    def ga_supported_formats(self, **_):
        return dict(
            formats=importers.SUPPORTED,
            not_supported={k: v for k, v in importers.UNSUPPORTED_HINTS.items()
                           if k != "unknown"},
            how_to_send=self._how_to_send(),
            scale_rule=("축척은 추정하지 않는다. DXF/IFC 는 파일 헤더 단위를 쓰고, "
                        "PDF 는 scale_ratio(1:N 의 N), PNG 는 px_per_m 이 필요하다."),
        )

    def _how_to_send(self) -> List[str]:
        out = []
        if self.ws.public_url:
            out.append(f"브라우저 업로드: {self.ws.public_url}/studio — 업로드 후 나오는 file_id 를 ga_import_drawing 에 전달")
            out.append("ChatGPT 등에서 파일 다운로드 URL 이 있으면 url 인자로 전달")
        if self.ws.allow_local_paths:
            out.append("로컬 경로: path 인자 (이 서버가 도는 컴퓨터의 파일)")
        out.append("작은 파일(CSV 등): content_base64 + filename")
        return out

    def ga_upload_link(self, **_):
        if not self.ws.public_url:
            return dict(message="이 서버는 로컬(stdio) 모드다 — path 인자로 파일 경로를 주면 된다.",
                        how_to_send=self._how_to_send())
        return dict(upload_page=f"{self.ws.public_url}/studio",
                    message="이 페이지에서 도면을 올리면 file_id 가 나온다. 그 file_id 를 대화에 붙여 넣을 것.")

    # ---------------------------------------------------------------- 입력
    def _get_bytes(self, file_id=None, path=None, url=None, content_base64=None,
                   filename=None):
        given = [x for x in (file_id, path, url, content_base64) if x]
        if len(given) != 1:
            raise ToolError("file_id, path, url, content_base64 중 정확히 하나를 줄 것")
        if file_id:
            return self.ws.load_file(file_id)
        if path:
            if not self.ws.allow_local_paths:
                raise ToolError("이 서버는 원격 모드라 로컬 경로를 읽지 않는다 — 업로드(file_id)나 url 을 쓸 것")
            path = os.path.abspath(os.path.expanduser(path))
            if not os.path.isfile(path):
                raise ToolError(f"파일 없음: {path}")
            with open(path, "rb") as fp:
                return fp.read(), os.path.basename(path)
        if url:
            if not self.ws.allow_url_fetch:
                raise ToolError("이 서버는 URL 다운로드를 허용하지 않는다 (--allow-url-fetch)")
            pu = urllib.parse.urlparse(url)
            if pu.scheme not in ("http", "https"):
                raise ToolError("http(s) URL 만 허용")
            req = urllib.request.Request(url, headers={"User-Agent": "smr-ga-mcp/" + ENGINE_VERSION})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read(MAX_FILE_BYTES + 1)
                cd = r.headers.get("Content-Disposition") or ""
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            name = filename or (urllib.parse.unquote(m.group(1)) if m else
                                os.path.basename(pu.path) or "download")
            return data, name
        try:
            data = base64.b64decode(content_base64, validate=False)
        except Exception as ex:   # noqa: BLE001
            raise ToolError(f"content_base64 디코딩 실패: {ex}")
        return data, filename or "upload.bin"

    def ga_import_drawing(self, file_id=None, path=None, url=None, content_base64=None,
                          filename=None, options=None, **_):
        data, name = self._get_bytes(file_id, path, url, content_base64, filename)
        stored = self.ws.store_file(data, name)
        opts = dict(options or {})
        try:
            tk = importers.import_file(data, stored["filename"], opts)
        except importers.ImportErrorUser as ex:
            raise ToolError(str(ex))
        tid = irhash.sha256_obj(tk)[:16]
        self.ws.wjson(self.ws.p("takeoffs", f"{tid}.json"), tk)
        previews = {}
        for f in tk["floors"]:
            rel = f"takeoffs/{tid}_floor{f['index']+1}.svg"
            with open(self.ws.p(*rel.split("/")), "w", encoding="utf-8") as fp:
                fp.write(takeoff_svg(tk, f["index"]))
            previews[f"{f['index']+1}F"] = self.ws.link(rel)
        items = [dict(id=it["id"], name=it["name"], kind=it["kind"], floor=it["floor"] + 1,
                      w=round(it["w"], 3), d=round(it["d"], 3))
                 for it in tk["items"][:60]]
        return dict(
            takeoff_id=tid, file_id=stored["file_id"], filename=stored["filename"],
            format=tk["source"]["format"], scale_basis=tk["scale_basis"],
            stats=tk["stats"], warnings=tk["warnings"][:30],
            items_preview=items, items_total=len(tk["items"]),
            preview_svg=previews,
            next=("사용자에게 미리보기로 축척·분류가 맞는지 확인받은 뒤 ga_create_project. "
                  "치수·좌표를 대화에서 고치지 말 것 — 틀렸으면 options(축척·layer_map)를 바꿔 다시 import."),
        )

    # ---------------------------------------------------------------- 프로젝트
    def _new_project(self, ir: dict, asis: Optional[dict], questions: List[dict], origin: str):
        seed = irhash.sha256_obj(dict(ir=ir, t=time.time_ns()))[:12]
        pid = seed
        d = self.ws.p("projects", pid)
        os.makedirs(d, exist_ok=True)
        self.ws.wjson(os.path.join(d, "ir.json"), ir)
        if asis is not None:
            self.ws.wjson(os.path.join(d, "asis.json"), asis)
        self.ws.wjson(os.path.join(d, "questions.json"), questions)
        self.ws.log(pid, dict(event="create", origin=origin, ir_sha256=irhash.sha256_obj(ir)))
        return pid

    def ga_create_project(self, takeoff_ids=None, project_name=None, options=None, **_):
        if not takeoff_ids:
            raise ToolError("takeoff_ids 가 비어 있다 — ga_import_drawing 결과의 takeoff_id 목록")
        tks = []
        for t in takeoff_ids:
            self.ws._check_id(t, "takeoff_id")
            pth = self.ws.p("takeoffs", f"{t}.json")
            if not os.path.exists(pth):
                raise ToolError(f"takeoff_id {t} 없음")
            tks.append(self.ws.rjson(pth))
        o = dict(options or {})
        o["project"] = project_name
        try:
            ir, asis, qs = TAKEOFF.takeoff_to_ir(tks, o)
        except ValueError as ex:
            raise ToolError(str(ex))
        pid = self._new_project(ir, asis, qs, origin=f"drawing:{','.join(takeoff_ids)}")
        return dict(project_id=pid, summary=self._ir_summary(ir), questions=qs,
                    import_warnings=ir.get("_import_warnings", [])[:20],
                    next=("questions 를 사용자에게 물어 답을 ga_update_ir 로 반영 → ga_validate → "
                          "사용자 승인 → ga_freeze → ga_optimize. 답을 모르는 값은 지어내지 말 것."))

    def ga_create_project_from_ir(self, ir=None, **_):
        """자연어 경로 — LLM 이 쓴 IR. 좌표 필드는 거부한다."""
        if not isinstance(ir, dict):
            raise ToolError("ir 는 JSON 객체여야 한다")
        bad = self._find_coords(ir)
        if bad:
            raise ToolError("IR 에 좌표 필드가 있다: " + ", ".join(bad[:8]) +
                            " — 좌표는 도면 파서와 솔버만 만든다. 고정 위치가 필요하면 도면을 import 할 것.")
        if any(b.get("pinned") for b in ir.get("blocks", []) or []):
            raise ToolError("pinned 블록은 도면에서만 만들 수 있다 (좌표가 필요하다)")
        ir = copy.deepcopy(ir)
        ir.setdefault("_schema", "smr-ga-ir/1.0")
        pid = self._new_project(ir, None, [], origin="llm-ir")
        rep = validate_spec.check_ir(ir, probe=False)
        return dict(project_id=pid, summary=self._ir_summary(ir),
                    gate1_preview=dict(errors=rep.ERR, warnings=rep.WARN[:20]))

    def ga_list_examples(self, **_):
        import examples_catalog as EX
        return dict(examples=EX.catalog())

    def ga_load_example(self, name=None, **_):
        import examples_catalog as EX
        ex = EX.get(name)
        if ex is None:
            raise ToolError(f"예제 {name!r} 없음 — ga_list_examples 참조")
        if ex["kind"] == "ir":
            ir = self.ws.rjson(ex["path"])
            ir = {k: v for k, v in ir.items() if k != "_frozen"}
            pid = self._new_project(ir, None, [], origin=f"example:{name}")
            return dict(project_id=pid, example=EX.public(ex), summary=self._ir_summary(ir))
        # 도면 예제: 실제 import 경로를 그대로 탄다
        r = self.ga_import_drawing(path=ex["path"], options=ex.get("import_options")) \
            if self.ws.allow_local_paths else self._import_trusted(ex)
        cp = self.ga_create_project(takeoff_ids=[r["takeoff_id"]], project_name=ex["title"],
                                    options=ex.get("project_options"))
        out = dict(example=EX.public(ex), import_result=r, project=cp)
        if ex.get("requirements"):
            out["requirements_file"] = ex["requirements"]
            out["hint"] = ("이 예제에는 설계요건 파일(P&ID 연결·구역·이격 — 가정값)이 함께 있다. "
                           "ga_apply_example_requirements 로 적용하면 '사용자가 질문에 답한 상태' 가 된다.")
        return out

    def _import_trusted(self, ex):
        """원격 모드에서도 번들 예제 파일은 읽을 수 있게 한다 (사용자 파일이 아님)."""
        with open(ex["path"], "rb") as fp:
            data = fp.read()
        st = self.ws.store_file(data, os.path.basename(ex["path"]))
        return self.ga_import_drawing(file_id=st["file_id"], options=ex.get("import_options"))

    def ga_apply_example_requirements(self, project_id=None, name=None, **_):
        import examples_catalog as EX
        ex = EX.get(name)
        if ex is None or not ex.get("requirements"):
            raise ToolError(f"예제 {name!r} 에 요건 파일이 없다")
        patch = self.ws.rjson(ex["requirements"])
        return self.ga_update_ir(project_id=project_id, patch=patch,
                                 reason=f"예제 요건 파일 적용: {os.path.basename(ex['requirements'])}")

    # ---------------------------------------------------------------- IR 조회·편집
    @staticmethod
    def _find_coords(obj, path="") -> List[str]:
        out = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.startswith("_") or k == "params":
                    # 제약 파라미터 안의 좌표(타 호기 터빈 위치 등)는 부지 자료에서 오는 입력이다.
                    # 배치 좌표가 아니므로 막지 않되, 변경 로그에 근거(reason)가 남는다.
                    continue
                if k in COORD_KEYS:
                    out.append(f"{path}.{k}".lstrip("."))
                out += Tools._find_coords(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                out += Tools._find_coords(v, f"{path}[{i}]")
        return out

    @staticmethod
    def _ir_summary(ir: dict) -> dict:
        bl = ir.get("blocks", [])
        kinds: Dict[str, int] = {}
        for b in bl:
            k = b.get("kind", "equipment")
            kinds[k] = kinds.get(k, 0) + int(b.get("count", 1) or 1)
        return dict(project=ir.get("project"), n_floors=ir.get("n_floors", 1),
                    floor_heights=ir.get("floor_heights"), preset=ir.get("preset", "balanced"),
                    max_w=ir.get("max_w"), max_d=ir.get("max_d"),
                    blocks=len(bl), by_kind=kinds, links=len(ir.get("links", []) or []),
                    separations=len(ir.get("separations", []) or []),
                    elevations=len(ir.get("elevations", []) or []),
                    constraints=[c.get("id") for c in ir.get("constraints", []) or []],
                    envelope_basis=ir.get("_envelope_basis"),
                    ir_sha256=irhash.sha256_obj(ir))

    def _load_ir(self, pid):
        d = self.ws.project_dir(pid)
        return d, self.ws.rjson(os.path.join(d, "ir.json"))

    def ga_get_ir(self, project_id=None, include_blocks=True, **_):
        d, ir = self._load_ir(project_id)
        out = dict(project_id=project_id, summary=self._ir_summary(ir),
                   frozen=self._frozen_state(project_id, ir))
        if include_blocks:
            out["ir"] = ir
        qp = os.path.join(d, "questions.json")
        if os.path.exists(qp):
            out["open_questions"] = self.ws.rjson(qp)
        return out

    BLOCK_EDITABLE = {"name", "zone", "clearance", "h", "weight", "need_exterior", "need_crane",
                      "rotatable", "floor", "fire_zone", "train", "structure", "vital_area",
                      "success_path", "opening", "turbine", "pinned", "kind", "w", "d", "count",
                      "instances"}
    TOP_EDITABLE = {"project", "n_floors", "floor_heights", "preset", "weights", "max_w", "max_d",
                    "aspect_max", "grid_m", "crane_bay", "constraints"}

    def ga_update_ir(self, project_id=None, patch=None, reason=None, **_):
        if not isinstance(patch, dict) or not patch:
            raise ToolError("patch 는 비어 있지 않은 객체여야 한다")
        if not reason:
            raise ToolError("reason(변경 근거 — 사용자 답변·문서 출처)을 적을 것")
        bad = self._find_coords(patch)
        if bad:
            raise ToolError("좌표는 대화로 바꿀 수 없다: " + ", ".join(bad[:8]) +
                            " — 좌표는 도면 파서와 솔버만 만든다.")
        d, ir = self._load_ir(project_id)
        asis_p = os.path.join(d, "asis.json")
        asis = self.ws.rjson(asis_p) if os.path.exists(asis_p) else None
        ir = copy.deepcopy(ir)
        changes: List[str] = []
        unknown = {k for k in patch if not k.startswith("_")} - {"set", "blocks", "add_blocks", "remove_blocks", "add_links",
                                "remove_links", "add_separations", "remove_separations",
                                "add_elevations", "remove_elevations", "answered_questions"}
        if unknown:
            raise ToolError(f"알 수 없는 patch 키 {sorted(unknown)}")

        for k, v in (patch.get("set") or {}).items():
            if k not in self.TOP_EDITABLE:
                raise ToolError(f"최상위 '{k}' 는 편집할 수 없다. 가능: {sorted(self.TOP_EDITABLE)}")
            if k == "weights" and isinstance(v, dict):
                # 가중치는 병합한다 — 일부만 주었다고 hard 항 가중치가 기본값으로 떨어지면
                # '위반이 비용을 내고 넘어갈 수 있는 옵션' 이 된다 (보고서 02장의 함정).
                w = dict(ir.get("weights") or {})
                w.update(v)
                ir[k] = w
                changes.append("merge weights " + ", ".join(sorted(v)))
                continue
            ir[k] = v
            changes.append(f"set {k}")

        idx = {b["id"]: b for b in ir["blocks"]}
        for upd in patch.get("blocks") or []:
            sel = upd.get("id") or upd.get("ids")
            ids = [sel] if isinstance(sel, str) else list(sel or [])
            if upd.get("match_name"):
                rx = re.compile(upd["match_name"], re.I)
                ids += [b["id"] for b in ir["blocks"] if rx.search(b.get("name", "") + " " + b["id"])]
            if upd.get("all"):
                ids = [b["id"] for b in ir["blocks"]
                       if not upd.get("kind_filter") or b.get("kind", "equipment") == upd["kind_filter"]]
            fields = {k: v for k, v in upd.items()
                      if k not in ("id", "ids", "match_name", "all", "kind_filter")}
            for k in fields:
                if k not in self.BLOCK_EDITABLE:
                    raise ToolError(f"블록 필드 '{k}' 는 편집할 수 없다. 가능: {sorted(self.BLOCK_EDITABLE)}")
            if not ids:
                if upd.get("all"):
                    changes.append(f"(일괄 선택 {upd.get('kind_filter')} — 해당 블록 없음)")
                    continue
                raise ToolError(f"블록 선택이 비었다: {upd}")
            for bid in ids:
                b = idx.get(bid)
                if b is None:
                    raise ToolError(f"블록 '{bid}' 없음")
                for k, v in fields.items():
                    if k == "pinned":
                        if v:
                            a = (asis or {}).get("blocks", {}).get(bid)
                            if a is None:
                                raise ToolError(f"'{bid}' 는 현황 좌표가 없어 고정할 수 없다")
                            b["pinned"] = True
                            b["rotatable"] = False
                            b["floor"] = a["floor"]
                            b["fixed_xy"] = [round(a["x"] + a["w"] / 2, 4),
                                             round(a["y"] + a["d"] / 2, 4)]
                            b["clearance"] = 0.0
                        else:
                            b.pop("pinned", None)
                            b.pop("fixed_xy", None)
                            b["rotatable"] = True
                        changes.append(f"{bid}.pinned={bool(v)}")
                        continue
                    if k in ("w", "d") and b.get(k) != v:
                        changes.append(f"★ {bid}.{k} {b.get(k)} → {v} (치수 변경 — 도면값과 다름)")
                    else:
                        changes.append(f"{bid}.{k}")
                    b[k] = v
        for nb in patch.get("add_blocks") or []:
            if nb.get("id") in idx:
                raise ToolError(f"블록 id 중복: {nb.get('id')}")
            if nb.get("pinned"):
                raise ToolError("새 블록은 pinned 일 수 없다 (좌표가 없다)")
            ir["blocks"].append(nb); idx[nb["id"]] = nb
            changes.append(f"+block {nb.get('id')}")
        for rid in patch.get("remove_blocks") or []:
            if rid not in idx:
                raise ToolError(f"블록 '{rid}' 없음")
            ir["blocks"] = [b for b in ir["blocks"] if b["id"] != rid]
            for key in ("links", "separations"):
                ir[key] = [x for x in ir.get(key, []) or [] if rid not in (x.get("a"), x.get("b"))]
            ir["elevations"] = [x for x in ir.get("elevations", []) or []
                                if rid not in (x.get("above"), x.get("below"))]
            changes.append(f"-block {rid}")

        def pair(x):
            return tuple(sorted((x.get("a"), x.get("b"))))
        for key, add, rem in (("links", "add_links", "remove_links"),
                              ("separations", "add_separations", "remove_separations")):
            ir.setdefault(key, [])
            if patch.get(rem) is not None:
                if patch[rem] == "inferred":
                    n0 = len(ir[key])
                    ir[key] = [x for x in ir[key] if not x.get("_inferred")]
                    changes.append(f"-{key} 추정분 {n0 - len(ir[key])}개")
                else:
                    gone = {pair(x) for x in patch[rem]}
                    ir[key] = [x for x in ir[key] if pair(x) not in gone]
                    changes.append(f"-{key} {len(gone)}")
            for x in patch.get(add) or []:
                ir[key].append(x)
            if patch.get(add):
                changes.append(f"+{key} {len(patch[add])}")
        ir.setdefault("elevations", [])
        if patch.get("remove_elevations"):
            gone = {(x.get("above"), x.get("below")) for x in patch["remove_elevations"]}
            ir["elevations"] = [e for e in ir["elevations"] if (e.get("above"), e.get("below")) not in gone]
        for e in patch.get("add_elevations") or []:
            ir["elevations"].append(e)

        # 층 수 정합: floor_heights 길이가 n_floors 와 다르면 한 값이면 확장
        fh = ir.get("floor_heights")
        if isinstance(fh, (int, float)):
            ir["floor_heights"] = [float(fh)] * int(ir.get("n_floors", 1))
        elif isinstance(fh, list) and len(fh) == 1 and int(ir.get("n_floors", 1)) > 1:
            ir["floor_heights"] = fh * int(ir["n_floors"])

        try:
            G.spec_from_dict(ir)
            if ir.get("_weights_auto"):
                old_w = dict(ir.get("weights") or {})
                TAKEOFF.refresh_hard_weights(ir)
                if ir["weights"] != old_w:
                    changes.append("hard 가중치 재산정 (층고·외곽·연결 변경 반영)")
        except Exception as ex:   # noqa: BLE001
            raise ToolError(f"패치 적용 후 IR 이 엔진에 로드되지 않는다: {ex}")
        self.ws.wjson(os.path.join(d, "ir.json"), ir)
        qp = os.path.join(d, "questions.json")
        if patch.get("answered_questions") and os.path.exists(qp):
            qs = [q for q in self.ws.rjson(qp) if q["id"] not in set(patch["answered_questions"])]
            self.ws.wjson(qp, qs)
        h = irhash.sha256_obj(ir)
        self.ws.log(project_id, dict(event="update", reason=reason, changes=changes, ir_sha256=h))
        fr = self._frozen_state(project_id, ir)
        return dict(project_id=project_id, changes=changes, ir_sha256=h,
                    summary=self._ir_summary(ir), frozen=fr,
                    note=("IR 이 바뀌었다 — 동결이 해제됐다. ga_validate → 사용자 승인 → ga_freeze 를 다시 할 것."
                          if fr.get("stale") else None))

    # ---------------------------------------------------------------- Gate 1
    def ga_validate(self, project_id=None, **_):
        d, ir = self._load_ir(project_id)
        rep = validate_spec.check_ir(ir, probe=True)
        h = irhash.sha256_obj(ir)
        self.ws.log(project_id, dict(event="validate", passed=not rep.ERR, ir_sha256=h,
                                     errors=len(rep.ERR), warnings=len(rep.WARN)))
        return dict(project_id=project_id, passed=not rep.ERR, ir_sha256=h,
                    errors=rep.ERR, warnings=rep.WARN, info=rep.INFO,
                    next=("ERROR 를 사람이 해소할 것 — 값을 지어내지 말 것" if rep.ERR else
                          "WARN 을 사용자에게 보여주고 승인을 받은 뒤 ga_freeze(approved_by=승인자)"))

    def _frozen_state(self, pid, ir) -> dict:
        fp = os.path.join(self.ws.project_dir(pid), "frozen.json")
        if not os.path.exists(fp):
            return dict(frozen=False)
        fr = self.ws.rjson(fp)
        cur = irhash.sha256_obj(ir)
        return dict(frozen=fr["ir_sha256"] == cur, frozen_sha256=fr["ir_sha256"],
                    approved_by=fr.get("approved_by"), approved_at=fr.get("approved_at"),
                    stale=fr["ir_sha256"] != cur)

    def ga_freeze(self, project_id=None, approved_by=None, acknowledged_warnings=False, **_):
        if not approved_by or not str(approved_by).strip():
            raise ToolError("approved_by(승인한 사람)가 필요하다. 사용자가 대화에서 명시적으로 승인한 뒤에만 호출할 것.")
        d, ir = self._load_ir(project_id)
        rep = validate_spec.check_ir(ir, probe=True)
        if rep.ERR:
            raise ToolError("GATE 1 불통과 — 동결할 수 없다:\n" + "\n".join(rep.ERR[:20]))
        if rep.WARN and not acknowledged_warnings:
            raise ToolError(f"WARN {len(rep.WARN)}건을 사용자가 확인했는지 acknowledged_warnings=true 로 표시할 것")
        h = irhash.sha256_obj(ir)
        rec = dict(ir_sha256=h, approved_by=str(approved_by), approved_at=_now(),
                   warnings_acknowledged=rep.WARN, engine_version=ENGINE_VERSION)
        self.ws.wjson(os.path.join(d, "frozen.json"), rec)
        self.ws.wjson(os.path.join(d, f"ir.frozen.{h[:12]}.json"), ir)
        self.ws.log(project_id, dict(event="freeze", **rec))
        return dict(project_id=project_id, frozen=True, ir_sha256=h, approved_by=rec["approved_by"],
                    message="IR 동결 완료. 이후 최적화는 이 해시로부터 LLM 호출 0회로 재현된다.")

    # ---------------------------------------------------------------- 최적화
    def ga_optimize(self, project_id=None, mode="sa", effort="normal", seed=20260826,
                    wait_seconds=45, pick=None, **_):
        d, ir = self._load_ir(project_id)
        fr = self._frozen_state(project_id, ir)
        if not fr.get("frozen"):
            raise ToolError("동결되지 않은 IR 이다 (또는 동결 후 바뀌었다). "
                            "ga_validate → 사용자 승인 → ga_freeze 먼저.")
        if mode not in ("sa", "nsga"):
            raise ToolError("mode 는 sa | nsga")
        if effort not in pipeline.EFFORT:
            raise ToolError(f"effort 는 {sorted(pipeline.EFFORT)}")
        asis_p = os.path.join(d, "asis.json")
        asis = self.ws.rjson(asis_p) if os.path.exists(asis_p) else None
        run_id = irhash.sha256_obj(dict(ir=fr["frozen_sha256"], mode=mode, effort=effort,
                                        seed=int(seed), pick=pick, engine=ENGINE_VERSION, fp=ENGINE_FP,
                                        asis=ASIS.asis_hash(asis) if asis else None))[:16]
        out = self.ws.p("runs", run_id)
        st = dict(run_id=run_id, project_id=project_id, status="queued", progress=0.0,
                  message="대기", started_at=_now())
        done_p = os.path.join(out, "summary.json")
        if os.path.exists(done_p):
            # 같은 IR 해시 + 같은 설정 = 같은 결과 (결정론). 다시 돌리지 않는다.
            return self._run_result(run_id, cached=True)
        with self.ws.lock:
            if run_id in self.ws.runs and self.ws.runs[run_id]["status"] in ("queued", "running"):
                return self.ga_run_status(run_id)
            self.ws.runs[run_id] = st

        def work():
            with self.ws.run_slots:
                st["status"] = "running"

                def prog(msg, frac):
                    st["message"], st["progress"] = msg, round(frac, 3)
                try:
                    pipeline.optimize_ir(ir, out, mode=mode, effort=effort, seed=int(seed),
                                         asis=asis, pick=pick, progress=prog)
                    st["status"] = "done"
                    self.ws.log(project_id, dict(event="optimize", run_id=run_id, mode=mode,
                                                 effort=effort, seed=int(seed)))
                except pipeline.GateError as ex:
                    st.update(status="error", message="GATE 1 불통과: " + "; ".join(ex.errors[:5]))
                except Exception as ex:   # noqa: BLE001
                    st.update(status="error", message=f"{type(ex).__name__}: {ex}",
                              trace=traceback.format_exc()[-2000:])
        threading.Thread(target=work, daemon=True).start()
        t0 = time.time()
        while time.time() - t0 < float(wait_seconds or 0):
            if st["status"] in ("done", "error"):
                break
            time.sleep(0.25)
        return self.ga_run_status(run_id)

    def ga_run_status(self, run_id=None, **_):
        self.ws._check_id(run_id, "run_id")
        st = self.ws.runs.get(run_id)
        if st is None or st["status"] == "done":
            if os.path.exists(self.ws.p("runs", run_id, "summary.json")):
                return self._run_result(run_id)
            if st is None:
                raise ToolError(f"run_id {run_id} 없음")
        out = {k: v for k, v in st.items() if k != "trace"}
        if st["status"] in ("queued", "running"):
            out["next"] = "몇 초 뒤 ga_run_status 로 다시 확인"
        return out

    def _run_result(self, run_id, cached=False):
        s = self.ws.rjson(self.ws.p("runs", run_id, "summary.json"))
        files = {}
        for name in s["outputs"]:
            if os.path.exists(self.ws.p("runs", run_id, name)):
                files[name] = self.ws.link(f"runs/{run_id}/{name}")
        o, a, c = s["optimized"], s.get("asis"), s.get("comparison")
        brief = dict(
            envelope_m=[o["width_m"], o["depth_m"], o["height_m"]],
            footprint_m2=o["footprint_m2"], volume_m3=o["volume_m3"],
            hard_violations=o["hard_violation_count"],
        )
        if a:
            brief.update(verdict=c.get("verdict"), asis_better=c.get("asis_better"))
            brief.update(asis_footprint_m2=a["footprint_m2"],
                         footprint_reduction_pct=c["footprint_reduction_pct"],
                         volume_reduction_pct=c["volume_reduction_pct"],
                         asis_hard_violations=a["hard_violation_count"])
        else:
            brief.update(naive_baseline_footprint_m2=s["naive_baseline"]["footprint_m2"],
                         caution="현황 도면이 없어 나이브 기준안과만 비교 — 이 개선율은 성과로 쓰지 말 것")
        warn = None
        if o["hard_violation_count"]:
            warn = (f"★ hard 제약 위반 {o['hard_violation_count']}건 — 이 해는 채택할 수 없다. "
                    f"개선율을 주장하지 말 것.")
        return dict(run_id=run_id, status="done", cached=cached, warning=warn, brief=brief,
                    report=files.get("report.html"), files=files,
                    reproducibility=dict(ir_sha256=s["ir_sha256"], seed=s["seed"],
                                         result_sha256=s["result_sha256"],
                                         engine_version=s["engine_version"]),
                    summary=s,
                    rule="숫자는 이 응답의 값을 그대로 인용할 것. 다시 계산하거나 반올림을 바꾸지 말 것.")

    def ga_get_drawing(self, run_id=None, name=None, takeoff_id=None, include_svg=False, **_):
        if takeoff_id:
            self.ws._check_id(takeoff_id, "takeoff_id")
            base, prefix = self.ws.p("takeoffs"), f"takeoffs/"
            name = name or f"{takeoff_id}_floor1.svg"
            if not name.startswith(takeoff_id):
                raise ToolError("takeoff 미리보기 이름이 아니다")
        else:
            self.ws._check_id(run_id, "run_id")
            base, prefix = self.ws.p("runs", run_id), f"runs/{run_id}/"
            name = name or "GA_optimized_floor1.svg"
        if "/" in name or "\\" in name or name.startswith("."):
            raise ToolError("파일 이름만 줄 것")
        pth = os.path.join(base, name)
        if not os.path.exists(pth):
            avail = sorted(x for x in os.listdir(base) if x.endswith(".svg"))
            raise ToolError(f"{name} 없음. 있는 도면: {avail[:20]}")
        out = dict(name=name, link=self.ws.link(prefix + name), bytes=os.path.getsize(pth))
        if include_svg:
            with open(pth, encoding="utf-8") as fp:
                out["svg"] = fp.read()
        return out

    def ga_floor_sweep(self, project_id=None, floors="1-4", effort="quick", seed=20260826, **_):
        d, ir = self._load_ir(project_id)
        fr = self._frozen_state(project_id, ir)
        if not fr.get("frozen"):
            raise ToolError("동결된 IR 로만 스윕한다 — ga_freeze 먼저")
        rng = []
        for part in str(floors).split(","):
            if "-" in part:
                a, b = part.split("-", 1); rng += list(range(int(a), int(b) + 1))
            elif part.strip():
                rng.append(int(part))
        if not rng or max(rng) > 12 or min(rng) < 1:
            raise ToolError("floors 는 1~12 범위 (예: '1-4')")
        if any(b.get("pinned") for b in ir["blocks"]):
            raise ToolError("고정 장애물(pinned)이 있는 IR 은 층수를 바꿔 볼 수 없다 — 고정 해제 후 사본으로 검토")
        spec = G.spec_from_dict(ir)
        p = pipeline.effort_params(effort, len(spec.blocks))
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            rows = decompose.floor_sweep(spec, rng, iters=p["iters"], restarts=p["restarts"],
                                         seed=int(seed))
        return dict(project_id=project_id, floors=rng, rows=rows,
                    note="층수를 결정변수로 두면 면적-체적-수직배관 트레이드오프가 드러난다 (Ejeh 2019).")

    def ga_explain(self, run_id=None, **_):
        """해설용 사실 묶음 — 읽기 전용. LLM 은 이것을 '설명' 할 뿐 숫자를 고치지 않는다."""
        r = self._run_result(run_id)
        s = r["summary"]
        res = self.ws.rjson(self.ws.p("runs", run_id, "result.json"))
        terms = sorted(res["terms"].items(), key=lambda kv: -abs(kv[1]))
        top = [dict(term=k, value=round(v, 2)) for k, v in terms if abs(v) > 1e-9][:10]
        # 외피에 닿는 블록 = 외피 크기를 결정하는 블록 (binding)
        W, D, _H = res["envelope"]
        touching = sorted({b["id"] for b in res["blocks"]
                           if abs(b["x"] + b["w"] - W) < 1e-6 or abs(b["y"] + b["d"] - D) < 1e-6})
        return dict(run_id=run_id, objective_terms=top,
                    envelope_binding_blocks=touching[:40],
                    hard_violations=res["hard_violations"],
                    comparison=s.get("comparison"), gate1_warnings=s.get("gate1_warnings", [])[:20],
                    reproducibility=r["reproducibility"],
                    guidance=("최적화는 솔버가 했다 — 'AI 가 배치했다' 고 쓰지 말 것. "
                              "envelope_binding_blocks 는 외피 크기를 정하는 블록이다 — 공간을 더 줄이려면 "
                              "이 블록들의 치수·층·고정 여부를 검토하라고 안내할 것."))

    def ga_constraint_catalog(self, **_):
        return dict(markdown=CONS.catalog_markdown(),
                    regulatory_warnings_file=os.path.join(ROOT, "skills", "ga-layout",
                                                          "references", "regulatory-numbers.md"))


# ===========================================================================
# 도구 스키마 (MCP tools/list)
# ===========================================================================

_FILE_ARGS = {
    "file_id": {"type": "string", "description": "업로드(/upload, 스튜디오)로 받은 file_id"},
    "path": {"type": "string", "description": "로컬 파일 경로 (stdio/로컬 모드에서만)"},
    "url": {"type": "string", "description": "http(s) 다운로드 URL (허용된 서버에서만)"},
    "content_base64": {"type": "string", "description": "작은 파일 내용 (base64). filename 도 줄 것"},
    "filename": {"type": "string"},
}
_IMPORT_OPTS = {
    "type": "object",
    "description": "도면 해석 옵션. 축척은 추정하지 않는다 — 모르면 사용자에게 물을 것.",
    "properties": {
        "units": {"type": "string", "enum": ["m", "mm", "cm", "in", "ft", "pt"]},
        "scale_ratio": {"type": "number", "description": "종이 축척 1:N 의 N (PDF·스캔)"},
        "m_per_unit": {"type": "number"},
        "px_per_m": {"type": "number", "description": "PNG/SVG: 1 m 당 픽셀"},
        "page": {"description": "PDF 페이지(1부터) 또는 'all'"},
        "floor": {"type": "integer", "description": "단일 평면도의 층(0=1층)"},
        "layer_map": {"type": "object", "description": "{정규식: equipment|room|obstacle|void|opening|boundary|ignore}"},
        "min_size_m": {"type": "number"},
        "close_gaps_px": {"type": "integer", "description": "PNG: 문 틈 메우기(px)"},
        "threshold": {"type": "integer", "description": "PNG: 흑백 임계값 0-255"},
        "default_kind": {"type": "string"},
    },
}

TOOL_DEFS: List[dict] = [
    dict(name="ga_supported_formats", ro=True,
         description="지원하는 도면 형식(DXF·SVG·PDF·PNG·IFC·CSV/XLSX)과 파일 전달 방법, 축척 규칙을 알려준다. 사용자가 도면을 주기 전 처음 한 번 호출.",
         schema={"type": "object", "properties": {}}),
    dict(name="ga_upload_link", ro=True,
         description="(원격/웹 모드) 사용자가 도면 파일을 올릴 브라우저 업로드 페이지 링크를 준다. ChatGPT·Claude 웹처럼 로컬 파일을 직접 넘길 수 없을 때 사용.",
         schema={"type": "object", "properties": {}}),
    dict(name="ga_import_drawing", ro=False,
         description=("도면 파일을 결정론적 파서로 읽어 기기·실·장애물·외곽을 인식한다 (LLM 이 좌표를 읽지 않는다). "
                      "결과의 미리보기 SVG 를 사용자에게 보여주고 축척·분류가 맞는지 확인받을 것. "
                      "틀리면 options 를 바꿔 다시 호출. DWG/JPEG 는 변환 안내가 반환된다."),
         schema={"type": "object", "properties": dict(_FILE_ARGS, options=_IMPORT_OPTS)}),
    dict(name="ga_create_project", ro=False,
         description=("인식된 도면(takeoff_id 여러 개 = 여러 층 가능)으로 최적화 프로젝트(IR 초안 + 현황 배치)를 만든다. "
                      "기본은 '공간을 작게' 우선(space_first), 현재 공간 외곽 안에서, 고정 장애물은 그대로. "
                      "반환된 questions 를 사용자에게 물어볼 것."),
         schema={"type": "object", "required": ["takeoff_ids"], "properties": {
             "takeoff_ids": {"type": "array", "items": {"type": "string"}},
             "project_name": {"type": "string"},
             "options": {"type": "object", "properties": {
                 "floor_heights": {"type": "array", "items": {"type": "number"}},
                 "default_clearance": {"type": "number", "description": "기기 유지보수 이격 [m] — 사용자 답"},
                 "preset": {"type": "string", "enum": ["space_first", "balanced"]},
                 "keep_floors": {"type": "boolean"}, "pin_obstacles": {"type": "boolean"},
                 "pin_openings": {"type": "boolean"}, "infer_adjacency": {"type": "boolean"},
                 "fit_within_existing": {"type": "boolean"}, "stack_files": {"type": "boolean"}}}}}),
    dict(name="ga_create_project_from_ir", ro=False,
         description=("도면 없이 자연어 요구사항으로 만든 IR(JSON)로 프로젝트를 만든다. 좌표(x,y,fixed_xy)는 넣을 수 없다. "
                      "치수를 모르면 지어내지 말고 사용자에게 물을 것. 스키마는 ga_constraint_catalog/engine/schema.json."),
         schema={"type": "object", "required": ["ir"], "properties": {"ir": {"type": "object"}}}),
    dict(name="ga_list_examples", ro=True,
         description="내장 예제 목록: 원전(SMR 보조건물) 도면과, 원전 도면이 없을 때 쓰는 제약이 많은 비교군(화학 공정플랜트, FPSO 상부, 병원 수술부 등).",
         schema={"type": "object", "properties": {}}),
    dict(name="ga_load_example", ro=False,
         description="내장 예제를 실제 도면 import 경로 그대로 불러와 프로젝트를 만든다.",
         schema={"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}),
    dict(name="ga_apply_example_requirements", ro=False,
         description="예제에 딸린 설계요건 파일(연결·구역·이격 — 가정값)을 IR 에 적용한다. 데모용.",
         schema={"type": "object", "required": ["project_id", "name"], "properties": {
             "project_id": {"type": "string"}, "name": {"type": "string"}}}),
    dict(name="ga_get_ir", ro=True,
         description="프로젝트의 IR·요약·동결 상태·남은 질문을 본다.",
         schema={"type": "object", "required": ["project_id"], "properties": {
             "project_id": {"type": "string"}, "include_blocks": {"type": "boolean"}}}),
    dict(name="ga_update_ir", ro=False,
         description=("사용자 답변을 IR 의 비좌표 필드에 반영한다 (층고·이격·구역·연결·이격요건·제약·고정 여부·이름). "
                      "좌표는 바꿀 수 없다. reason 에 근거(사용자 답변/문서)를 적을 것. 바꾸면 동결이 풀린다.\n"
                      "patch 예: {\"set\":{\"floor_heights\":[6,5]}, \"blocks\":[{\"all\":true,\"kind_filter\":\"equipment\",\"clearance\":1.0},"
                      "{\"id\":\"P-101\",\"zone\":\"RCA\"}], \"add_links\":[{\"a\":\"P-101\",\"b\":\"HX-1\",\"cost\":3,\"kind\":\"pipe\"}], "
                      "\"add_separations\":[{\"a\":\"A\",\"b\":\"B\",\"min_dist\":6.1,\"hard\":true,\"basis\":\"근거\"}], "
                      "\"remove_links\":\"inferred\", \"answered_questions\":[\"clearance\"]}"),
         schema={"type": "object", "required": ["project_id", "patch", "reason"], "properties": {
             "project_id": {"type": "string"}, "patch": {"type": "object"}, "reason": {"type": "string"}}}),
    dict(name="ga_validate", ro=True,
         description="Gate 1 — IR 을 결정론적 규칙으로 검증한다 (LLM 미사용). ERROR 가 있으면 진행 금지, WARN 은 사용자에게 보여줄 것.",
         schema={"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}}}),
    dict(name="ga_freeze", ro=False,
         description=("Gate 1 사람 승인 — 사용자가 대화에서 IR 과 WARN 을 보고 명시적으로 승인했을 때만 호출한다. "
                      "IR 을 SHA-256 으로 동결한다. 사용자 승인 없이 호출하지 말 것."),
         schema={"type": "object", "required": ["project_id", "approved_by"], "properties": {
             "project_id": {"type": "string"},
             "approved_by": {"type": "string", "description": "승인한 사람 (사용자가 밝힌 이름/직함)"},
             "acknowledged_warnings": {"type": "boolean"}}}),
    dict(name="ga_optimize", ro=False,
         description=("동결된 IR 로 결정론적 솔버를 돌려 배치를 최적화하고 도면(SVG/DXF)·보고서를 만든다. "
                      "현황 도면이 있으면 같은 목적함수로 현황 vs 최적화를 비교한다. 오래 걸리면 run_id 를 주고 끝난다 — ga_run_status 로 확인. "
                      "hard 위반이 0 이 아니면 반드시 경고할 것."),
         schema={"type": "object", "required": ["project_id"], "properties": {
             "project_id": {"type": "string"},
             "mode": {"type": "string", "enum": ["sa", "nsga"], "description": "sa=단일 목적(기본), nsga=Pareto 3목적"},
             "effort": {"type": "string", "enum": ["quick", "normal", "thorough"]},
             "seed": {"type": "integer"}, "wait_seconds": {"type": "number"},
             "pick": {"type": "integer", "description": "nsga 에서 도면으로 낼 Pareto 해 번호"}}}),
    dict(name="ga_run_status", ro=True,
         description="최적화 진행 상태/결과.",
         schema={"type": "object", "required": ["run_id"], "properties": {"run_id": {"type": "string"}}}),
    dict(name="ga_get_drawing", ro=True,
         description="결과 도면(SVG) 내용과 링크. name 예: GA_optimized_floor1.svg, GA_asis_floor1.svg. takeoff_id 를 주면 인식 미리보기.",
         schema={"type": "object", "properties": {
             "run_id": {"type": "string"}, "takeoff_id": {"type": "string"}, "name": {"type": "string"},
             "include_svg": {"type": "boolean", "description": "SVG 원문 포함 (크다 — 필요할 때만)"}}}),
    dict(name="ga_floor_sweep", ro=True,
         description="층수 민감도 스윕 — 층수를 바꿔 가며 면적·체적·목적함수를 표로 낸다 (고정 장애물 없는 IR).",
         schema={"type": "object", "required": ["project_id"], "properties": {
             "project_id": {"type": "string"}, "floors": {"type": "string"},
             "effort": {"type": "string", "enum": ["quick", "normal", "thorough"]}, "seed": {"type": "integer"}}}),
    dict(name="ga_explain", ro=True,
         description="결과 해설용 사실 묶음 (목적함수 항, 외피를 결정하는 블록, 위반, 비교). 읽기 전용.",
         schema={"type": "object", "required": ["run_id"], "properties": {"run_id": {"type": "string"}}}),
    dict(name="ga_constraint_catalog", ro=True,
         description="사전 검증된 제약 라이브러리(원전 고유 10종) 카탈로그. 제약은 여기서 '선택' 만 한다 — 수식을 발명하지 않는다.",
         schema={"type": "object", "properties": {}}),
]
