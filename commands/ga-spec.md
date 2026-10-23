---
description: 자연어 요구사항에서 Design Intent IR(JSON) 초안을 만든다. 좌표는 생성하지 않는다.
argument-hint: "[요구사항 문장 또는 요구사항이 담긴 파일 경로]"
allowed-tools: Read, Write, Glob, Grep, Bash(python3 *)
---

# 단계 1 — 요구사항 정형화

`${CLAUDE_PLUGIN_ROOT}/skills/ga-layout/SKILL.md` 의 **단계 1** 규칙을 따른다.
읽어야 할 참조:

- `${CLAUDE_PLUGIN_ROOT}/engine/schema.json` — IR 스키마
- `${CLAUDE_PLUGIN_ROOT}/skills/ga-layout/references/constraint-library.md` — 제약은 여기서 **선택**만 한다
- `${CLAUDE_PLUGIN_ROOT}/skills/ga-layout/references/regulatory-numbers.md` — **★ 사용금지 수치 5건. 먼저 읽을 것**
- `${CLAUDE_PLUGIN_ROOT}/examples/spec_minimal.json` — 최소 예제

## 입력

$ARGUMENTS

## 할 일

1. 위 요구사항에서 기기 목록·수량·치수·이격·인접·층 요구를 뽑아 IR 초안을 쓴다.
2. **좌표(x, y)는 한 개도 쓰지 않는다.** 좌표는 솔버만 만든다.
3. 제시되지 않았고 인용도 없는 수치는 **`null` 로 두고 질문 목록에 올린다.**
   값을 지어내면 안 된다. 이 단계의 추출 정확도는 측정된 바 치수 48.4% / 위치 53.0% 다.
4. 모든 `separations` / `elevations` 에 `basis`(근거 조항)를 적는다. 가정값이면 "(가정값)"으로 표기한다.
5. 중복 계열의 `train` / `fire_zone` 처럼 복제본마다 달라야 하는 값은 `instances` 에 명시한다.
6. 파일로 저장한 뒤 **반드시** 다음을 출력한다.
   - 저장 경로
   - **되물어야 할 항목 목록** (null 로 둔 필드 전부 + 근거가 약한 가정)
   - 다음 명령: `/smr-ga:ga-check <경로>`

IR 을 쓴 다음 스스로 "괜찮아 보인다"고 판정하지 않는다. 검증은 Gate 1 이 한다.
