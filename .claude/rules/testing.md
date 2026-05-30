---
description: 테스트 작성 규칙
globs: ["tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"]
---

# 테스트

- 커버리지 숫자보다 **핵심 경로부터** — windowing(cadence/eot/race)·ingest 슬라이싱·emit 스키마·e2e.
- **mock 최소화, 통합테스트 우선** — 단, GPU/모델 불요 경로는 `MockCritic`/`MockTranscriber`로, 실측은 **실 vLLM e2e**로 분리한다.
- 실패/엣지 케이스를 먼저 작성한다(resample-list gotcha, 빈 윈도우, 완료순 emit 등 — happy path만 X).
- 실 vLLM 의존 테스트는 `client.health()` 게이트로 **다운이면 skip**(gje `test_e2e_pipeline` 관례).
- 완료 기준은 측정 가능하게: **"pytest 통과 (+ 실 vLLM 스파이크 PASS)"**. 미달이면 통과할 때까지 반복.
