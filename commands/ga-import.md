---
description: 도면 파일(DXF·SVG·PDF·PNG·IFC·CSV/XLSX)을 결정론적 파서로 읽어 현황 배치를 만들고, 그 공간을 더 작게 만드는 최적화 프로젝트를 시작한다.
argument-hint: "[도면 파일 경로 …] [축척 정보: 예 '1:200', 'mm', '20px/m']"
---

# 도면 → 현황 배치 → 최적화 프로젝트

MCP 서버 `smr-ga` 의 도구를 쓴다 (플러그인을 설치하면 자동 등록된다).
`${CLAUDE_PLUGIN_ROOT}/skills/ga-layout/SKILL.md` 의 「도면 경로」 규칙을 따른다.

## 입력

$ARGUMENTS

## 할 일

1. `ga_import_drawing(path=…, options=…)` — 축척은 **사용자가 말한 것만** 넣는다.
   DXF/IFC 는 파일 헤더 단위를 쓰므로 보통 필요 없다. PDF 는 `scale_ratio`, PNG 는 `px_per_m` 이 필수다.
   모르면 **묻는다.** 축척을 추정하지 않는다 — 틀린 축척은 모든 치수를 같은 비율로 틀리게 만든다.
2. 미리보기 SVG 경로를 사용자에게 주고 **분류(기기/실/고정/금지/해치)·개수·축척이 맞는지 확인받는다.**
   틀렸으면 `options.layer_map` 이나 축척을 바꿔 다시 import 한다. 좌표·치수를 대화에서 고치지 않는다.
3. 여러 층이 파일로 나뉘어 있으면 모두 import 한 뒤 `ga_create_project(takeoff_ids=[1층, 2층, …])`.
4. 반환된 `questions` 를 사용자에게 **그대로** 묻는다 (층고, 유지보수 이격, 연결관계, 구역·이격 요건, 고정 여부).
   답은 `ga_update_ir(patch=…, reason="사용자 답변: …")` 로만 반영한다.
5. 이어서 `/smr-ga:ga-check` 단계(검증·승인·동결) → `/smr-ga:ga-run`.
