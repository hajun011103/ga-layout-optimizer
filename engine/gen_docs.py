# -*- coding: utf-8 -*-
"""
gen_docs.py — 제약 라이브러리 문서를 코드에서 생성한다.

문서와 코드가 어긋나는 것을 막기 위해 references/constraint-library.md 는
손으로 쓰지 않고 여기서 만든다. REGISTRY 가 단일 진실 원천이다.

  python3 gen_docs.py            # 표준출력
  python3 gen_docs.py --write    # skills/ga-layout/references/constraint-library.md 갱신
  python3 gen_docs.py --check    # 최신인지 확인 (테스트에서 호출)
"""
from __future__ import annotations

import argparse
import os
import sys

import constraints as CONS
from version import ENGINE_VERSION

HEADER = """<!-- 자동 생성 파일 — 손으로 고치지 말 것.
     engine/constraints.py 의 REGISTRY 를 고친 뒤 `python3 engine/gen_docs.py --write` -->

# 제약조건 라이브러리 (사전 검증)

LLM 은 이 목록에서 제약을 **선택하고 파라미터만 채운다.** 수식을 발명하지 않는다.
FlairGPT / LLM-FuncMapper 가 채택한 "사전 정의 비용함수 라이브러리에 매핑" 구조이며,
원자력 적용에서 가장 이식 가치가 큰 설계 결정이다.

- `*` 가 붙은 파라미터는 **필수**다. 값이 `null` 이면 Gate 1 에서 ERROR 로 막힌다.
- 기본값이 없는 파라미터는 "규제 원문 대조 없이 값을 넣지 말라"는 뜻이다.
  → [regulatory-numbers.md](regulatory-numbers.md) 의 사용금지 항목을 먼저 읽을 것.
- 위반량의 **단위가 섞여 있다** (m / m² / 건). 가중치를 같은 크기로 주면 안 된다.
  `validate_spec.py` 의 한계이득 점검이 단위별 하한을 알려준다.

IR 에는 이렇게 적는다.

```json
"constraints": [
  {"id": "FIRE-20FT",
   "params": {"groups": [["success_path:SP-A"], ["success_path:SP-B"]],
              "combustible_free": true,
              "detection_and_suppression": true}}
]
```

선택자(selector)는 다음을 쓴다.

| 형태 | 뜻 |
|---|---|
| `"SIP-1"` | 블록 id |
| `"SIP"` | `count` 로 전개된 인스턴스 전부 (SIP-1, SIP-2 …) |
| `"zone:R3"` | 방사선/계통 구역 |
| `"train:TrainA"` | 다중성 계열 |
| `"fire_zone:FZ-A"` | 화재구역 |
| `"structure:AUX"` | 내진 이격 단위(건물) |
| `"vital_area:VA-1"` | 보안 중요구역 |
| `"success_path:SP-A"` | App. R 고온정지 성공경로 계열 |

---

"""


def build() -> str:
    out = [HEADER, "## 요약\n", CONS.catalog_markdown(), "\n---\n"]
    for cid in sorted(CONS.REGISTRY):
        cd = CONS.REGISTRY[cid]
        out.append(f"## `{cid}` — {cd.name}\n")
        out.append(f"**유형** {'hard' if cd.hard else 'soft'} · "
                   f"**기본 가중치** {cd.default_weight:g}\n")
        out.append(f"**근거**\n\n> {cd.basis}\n")
        out.append(f"**수식**\n\n```\n{cd.formula}\n```\n")
        out.append("**파라미터**\n")
        out.append("| 이름 | 단위 | 필수 | 기본값 | 설명 |")
        out.append("|---|---|---|---|---|")
        for k, m in cd.params.items():
            dv = "—" if m["default"] is None else f"`{m['default']}`"
            note = (m["note"] or "").replace("\n", " ").replace("|", "/")
            out.append(f"| `{k}` | {m['unit']} | {'★' if m['required'] else ''} | "
                       f"{dv} | {note} |")
        if cd.note:
            out.append(f"\n**비고** {cd.note}\n")
        out.append("\n---\n")
    out.append(f"\n<sub>engine {ENGINE_VERSION} · "
               f"engine/constraints.py 에서 자동 생성됨</sub>\n")
    return "\n".join(out)


def target() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "skills", "ga-layout", "references",
                        "constraint-library.md")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    txt = build()
    if a.write:
        with open(target(), "w", encoding="utf-8") as fp:
            fp.write(txt)
        print(f"wrote {target()}")
        return 0
    if a.check:
        try:
            with open(target(), encoding="utf-8") as fp:
                cur = fp.read()
        except FileNotFoundError:
            print("constraint-library.md 없음"); return 1
        if cur != txt:
            print("constraint-library.md 가 코드와 어긋남 — "
                  "python3 engine/gen_docs.py --write")
            return 1
        print("최신")
        return 0
    sys.stdout.write(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
