# 벤치마크 데이터 (수동 배치)

폐쇄망 설치를 전제하므로 **자동 다운로드하지 않는다.** 아래 데이터를 손으로 넣으면
`tests/test_benchmarks.py` 가 SKIP 을 멈추고 실제 갭을 보고한다.
**데이터가 없으면 테스트는 SKIP 된다. 값을 지어내서 통과시키지 않는다.**

## 1단계 — 최적성 갭

### `qaplib/`
- 출처: `coral.ise.lehigh.edu/data-sets/qaplib/`
- 넣을 것: `<name>.dat` 과 짝이 되는 `<name>.sln`
- **최적값은 `.sln` 에서 읽는다. 코드에 하드코딩하지 않는다.**
- 권장 소규모 인스턴스부터: `nug12`, `nug15`, `had12`, `rou12`, `esc16a`
- `.sln` 이 없는 `.dat` 은 갭을 보고할 수 없으므로 건너뛴다.

외부 데이터가 없어도 `bench_qap.py --selftest` 가 n≤8 전수탐색으로
**진짜 최적** 을 만들어 갭을 보고한다. 이것이 최소한의 1단계 검증이다.

### `2dpacklib/`
- 출처: `site.unibo.it/operations-research/en/research/2dpacklib`
- 로더 미구현. 데이터를 넣으려면 로더를 먼저 붙일 것 (테스트가 그렇게 실패한다).

## 2단계 — 재현

### `wrigley2019/`
- 출처: `data.mendeley.com/datasets/w39zbzj6bk/1` (Shearon Harris, CC BY 4.0)
- 넣을 것: 변환된 `spec.json` + `expected.json`
  ```json
  {"baseline_objective": 14914, "pipe_reduction_pct_range": [33.9, 46.1]}
  ```
- ※ 위 `baseline_objective` 14,914 는 기술조사 보고서에 없는 값이다 (−33.9~46.1% 는 보고서와 일치).
  **expected.json 을 만들 때 원논문에서 직접 대조·확인 후 기입할 것.**

### `ejeh2019/`
- Ejeh 2019 부록 B 의 요소플랜트 8기기 전체 입력데이터가 공개되어 있다.
- 넣을 것: 변환된 `spec.json`
- 기대값(참고): 1층 260,942 / 2층 167,299 / 3층 149,498 / **4층 117,431 rmu**
  (수치는 보고서와 일치 확인됨. 단위 표기 "rmu" 는 보고서에 없으므로 원논문에서 확인할 것.)
- ※ 본 엔진은 MILP 가 아니라 메타휴리스틱이므로 값이 아니라
  **층수-비용 곡선의 형상**을 대조한다.

## 3단계 (미구현)

- ABWR DCD Ch.1 (벡터 PDF) — 표고별 실 폴리곤 추출
- AP1000 §12.3 방사선구역도 31장 — 선량 제약 인코딩
