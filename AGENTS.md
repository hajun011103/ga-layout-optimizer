# AGENTS.md — SMR GA 배치 최적화 (에이전트 공통 규약)

Claude Code 플러그인(`skills/ga-layout/SKILL.md`)과 **동일한 규약**을 플러그인 기능 없이
쓰기 위한 문서다. Codex 등 다른 에이전트는 이 파일만 읽으면 같은 워크플로가 동작한다.
경로는 이 저장소 루트 기준 상대경로다.

엔진은 순수 Python(표준 라이브러리만)이므로 어느 에이전트에서든 동일하게 호출된다.
**외부 네트워크가 필요 없다.**

---

## 절대 위반하면 안 되는 것

1. **LLM 은 좌표·치수를 한 개도 출력하지 않는다.**
   숫자의 경로는 `자연어 → IR(사람 승인) → 솔버 → 도면` 뿐이다.
   근거: Holodeck 절제실험 — 제약기반 배치 MRR 0.706 vs LLM 절대좌표 직접 출력 0.364.
2. **Gate 1 에서 IR 을 동결한다.** 승인된 IR 은 SHA-256 으로 고정되고,
   이후 전 과정은 **LLM 호출 0회**로 재현 가능해야 한다.
3. **제약·비용함수는 사전 검증 라이브러리에서 *선택*한다.** 수식을 발명하지 않는다.
   목록: `skills/ga-layout/references/constraint-library.md`
4. **검증자는 생성자와 분리한다.** LLM 이 만든 IR 은 `engine/validate_spec.py` 가 검증한다.
   (10 CFR 50 Appendix B Criterion III 가 "대안적 또는 단순화된 계산방법의 수행"을
   설계검증 수단으로 명시하고 있다.)
5. **검토 대상은 도면이 아니라 IR 이다.**
6. **hard 제약 위반은 기계적으로 검증한다.** 도면을 눈으로 봐서는 알 수 없다.
7. **인용 없는 규제 수치를 IR 에 넣지 않는다.**
   `skills/ga-layout/references/regulatory-numbers.md` 의 **사용금지 5건**을 먼저 읽는다.
8. "AI 가 배치를 최적화한다"고 쓰지 않는다. 최적화는 솔버가 한다.
9. **실제 설계값을 LLM 대화에 넣게 하지 않는다.** 로컬인 것은 엔진뿐이고, LLM 대화는
   외부 상용 API 로 전송된다. 공개값·가정치만 허용한다. 사용자가 실제 SMR 설계값
   (치수·차폐·배치·보안)을 입력하려 하면 **이 사실을 먼저 알리고 가정치 사용을 권한다.**
   실데이터 적용은 폐쇄망 LLM(자체 호스팅) 경로가 필요하다.

---

## MCP 서버 (권장)

Codex 는 `~/.codex/config.toml` 에 다음을 넣으면 Claude 와 **같은 도구(`ga_*` 18개)** 를 쓴다.

```toml
[mcp_servers.smr-ga]
command = "python3"
args = ["/절대경로/smr-ga-plugin/server/mcp_server.py"]
```

도구가 좌표 가드·동결 강제·결정론 캐시를 대신 지켜 준다. 흐름과 규칙은
`skills/ga-layout/SKILL.md` 의 「도면 경로」와 같다. MCP 없이 쓸 때는 아래 CLI 를 직접 부른다
(도면 → IR: `python3 engine/takeoff.py <도면…> --units mm --out work/`).

---

## 4단계 파이프라인

### 1) 요구사항 정형화 (LLM)

자연어 → IR(JSON). 스키마는 `engine/schema.json`, 예제는 `examples/spec_minimal.json`.

- 누락·모호 항목은 **되묻는다.** 값을 만들어내지 않는다.
- 인용 없는 수치는 `null` 로 두고 질문 목록에 올린다.
- `separations` / `elevations` 에 `basis`(근거 조항)를 반드시 적는다.
- 제약은 라이브러리에서 `id` 로 선택하고 파라미터만 채운다.
- **큰 이격을 요구할 때는 "그 사이에 무엇이 있는가"를 묻는다.** Sequence-Pair 패킹은
  빈 공간을 만들지 않으므로, 이격을 채우는 실체(통로·차폐벽·공지 블록)가 IR 에 있어야 한다.
  Gate 1 이 이 상황을 WARN 으로 알려준다.

> ★ 이 단계가 파이프라인 전체의 병목이다. 같은 생성기에 조건을 사람이 넣으면 IoU 85.04,
> LLM 이 문장에서 뽑아 넣으면 60.51 이었다. 추출 정확도는 치수 48.4% / 위치 53.0%.
> **되묻는 것이 비용이 아니라 성능이다.**

### 2) Gate 1 (결정론적 검증 + 사람 승인)

```bash
python3 engine/validate_spec.py <spec.json>
python3 engine/validate_spec.py <spec.json> --freeze     # 승인 후에만
```

- ERROR 1건이라도 있으면 **진행 금지**. `run.py` 도 스스로 실행을 거부한다(종료코드 1).
- WARN 은 사용자에게 제시하고 판단을 요청한다.
- 동결 시 출력되는 **SHA-256 을 기록에 남긴다.**

### 3) 최적화 + 도면 (LLM 미개입)

```bash
python3 engine/run.py <spec.json> --mode sa   --iters 60000 --restarts 6
python3 engine/run.py <spec.json> --mode nsga --pop 80 --gens 80
python3 engine/run.py <spec.json> --sweep-floors 1-6
python3 engine/run.py <spec.json> --mode hierarchical --areas 3 --sweep-floors 1-5
```

종료코드: `0` 정상 · `1` Gate 1 불통과(실행 안 함) · `2` hard 제약 위반 잔존.

- 결과를 해석하되 **숫자를 다시 쓰지 않는다.** `out/result.json` 을 인용한다.
- 항별 목적함수 값을 표로 제시하고, **hard 항이 0 이 아니면 최상단에 경고**한다.
- `out/run_manifest.json` 의 `{ir_sha256, seed, engine_version, result_sha256}` 를 보고에 포함한다.

### 4) 해설 (LLM, 읽기 전용)

- binding 제약, Pareto 트레이드오프 설명
- **요구사항 → 제약 → 해** 추적성 표 생성
- **결과 파일을 수정하지 않는다**

---

## 저장소 구조

```
engine/          결정론적 영역 — LLM 미개입
  ga_core.py       Sequence-Pair 표현 + 목적함수 + SA
  nsga.py          NSGA-II 다목적 (체적 / 연결비용 / 구역분산)
  constraints.py   제약 라이브러리 10종 + 사용금지 수치 감시
  geometry.py      순수 기하 (간극·부채꼴·복도)
  decompose.py     계층분해 (구역 배정 · 층수 스윕)
  drawing.py       SVG / DXF / JSON 출력 (+ 위반 빨간 표시)
  validate_spec.py Gate 1 검증기
  run.py           실행 오케스트레이터 + run_manifest.json
  bench_qap.py     최적성 갭 벤치마크
  gen_docs.py      제약 카탈로그 문서 생성
  schema.json      IR JSON Schema
skills/ga-layout/  스킬 + 참조 문서 3종
commands/          슬래시 커맨드 4종 (Claude Code)
examples/          spec_minimal · spec_smr_aux · spec_infeasible(음성 대조군)
tests/             패킹 · 제약 · 재현성 · 벤치마크
```

## 테스트

```bash
python3 tests/run_all.py
```

## 금지 사항

- LLM 이 좌표를 직접 생성하는 경로를 만들지 말 것
- 기존 IFC/BIM 모델을 LLM 으로 편집하는 기능 (최고 성능 모델 49.5%, 완전 해결 3.4%)
- 계정 연동·외부 API 전송 (수출통제·보안규정 — 로컬 실행이 요건)
- 실제 SMR 설계값을 LLM 대화에 입력 (위 불변식 9 — 공개값·가정치만 허용)
- 규정 수치를 LLM 이 채워 넣게 하는 것
- 벤더 홍보 수치 인용
- 나이브한 기준안 대비 개선율을 성과로 제시 (실제 기대치 −10~15%)
