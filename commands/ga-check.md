---
description: Gate 1 — IR 을 결정론적으로 검증하고, 통과 시 해시로 동결한다. ERROR 가 있으면 진행 금지.
argument-hint: "[spec.json 경로] [--freeze]"
allowed-tools: Read, Bash(python3 *)
---

# 단계 2 — Gate 1 (결정론적 검증 + 사람 승인)

## 실행

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/engine/validate_spec.py" $ARGUMENTS
```

## 해석 규칙

- **ERROR ≥ 1 → 진행 금지.** 각 ERROR 를 사용자에게 그대로 제시하고,
  해소 방법을 제안하되 **당신이 값을 채워 넣지 않는다.** 사용자에게 묻는다.
- **WARN → 그대로 제시하고 판단을 요청한다.** 임의로 무시하거나 해소하지 않는다.
  특히 다음 WARN 은 무시하면 안 된다.
  - 가중치 한계이득 하한 미달 → 최적화기가 "위반 페널티를 내고 체적을 줄이는" 해를 반환한다
  - `FIRE-20FT` 만 있고 `FIRE-HATCH` 가 없음 → 반출 해치가 화재구역 분할을 무너뜨리는 것을 못 잡는다
  - `basis` 미기재 → 인용 없는 수치가 IR 에 들어가 있다
- **통과 + 사용자 승인 → 동결한다.**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/engine/validate_spec.py" <spec.json> --freeze
```

  `<spec>.frozen.json` 과 `.sha256` 이 생긴다.
  **IR SHA-256 을 대화에 남긴다.** 이후 전 과정은 이 해시로부터
  LLM 호출 0회로 재현 가능해야 한다.

승인 없이 동결하지 않는다. 승인은 사람이 한다.

다음 명령: `/smr-ga:ga-run <경로>`
