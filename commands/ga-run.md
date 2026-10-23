---
description: 결정론적 솔버로 배치를 최적화하고 SVG/DXF 도면을 만든다. LLM 은 호출만 하고 숫자를 다시 쓰지 않는다.
argument-hint: "[spec.json 경로] [--mode sa|nsga|hierarchical] [--sweep-floors 1-6]"
allowed-tools: Read, Bash(python3 *)
---

# 단계 3 — 최적화 + 도면 (LLM 미개입 구간)

## 실행

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/engine/run.py" $ARGUMENTS
```

인자를 사용자가 지정하지 않았다면 기본은 `--mode sa --iters 60000 --restarts 6` 이다.
다음 중 필요한 것을 제안한다.

| 목적 | 명령 |
|---|---|
| 가중치가 이미 합의됨, 빠른 안 1개 | `--mode sa --iters 60000 --restarts 6` |
| **가중치를 미리 정하지 않고 설계자가 고름 (권장)** | `--mode nsga --pop 80 --gens 80` |
| 층수-비용 트레이드오프 곡선 | `--sweep-floors 1-6` |
| 규모가 큰 문제 (구역 배정 → 층 배정 → 상세배치) | `--mode hierarchical --areas 3 --sweep-floors 1-5` |

## 보고 규칙

1. **종료코드 1 → Gate 1 불통과.** 최적화가 실행되지 않았다. `/smr-ga:ga-check` 로 돌아간다.
2. **종료코드 2 → hard 제약 위반이 남아 있다.**
   보고의 **최상단**에 경고를 쓴다. "받아들일 수 없는 해" 라고 명시한다.
   도면에 빨간색으로 표시되어 있다. 위반량을 그대로 인용한다.
3. **항별 목적함수 값을 표로 제시한다.** 값은 `out/result.json` 에서 인용하고
   당신이 다시 계산하지 않는다.
4. `out/run_manifest.json` 의 `{ir_sha256, seed, engine_version, result_sha256}` 를
   보고에 포함한다 — 재현성 기록이다.
5. `--mode nsga` 이면 **Pareto 표를 그대로 보여주고 사용자에게 고르게 한다.**
   knee 해는 정렬 편의를 위한 기본값이지 추천이 아니다.
   고른 뒤 `--pick <N>` 으로 다시 실행해 그 해의 도면을 낸다.
6. 개선율을 성과로 제시하지 않는다. 기준안이 나이브하다 — **실제 기대치는 −10~15%.**

다음 명령: `/smr-ga:ga-explain <out 디렉터리>`
