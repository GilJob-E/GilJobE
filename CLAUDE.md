# giljobe

> **프로젝트 지도.** AI가 작업을 시작할 때 가장 먼저 읽는 파일.
> "전체 그림 + 어디를 보면 되는지"만 둔다. 상세는 `docs/`·`.claude/rules/`·`PLAN.md`로 위임한다.
> 길어지면(>120줄) 잘게 쪼갠다.

## 무엇을 만드는가
- 면접 시뮬레이션의 **입력단**: 사용자의 WebRTC 웹캠+마이크 스트림을 받아 **gje(MMM)로 비언어 비평 + STT 전사**를 수행하고, 다운스트림 면접관 LLM이 먹을 **`.json`을 송출**한다.
- 전체 루프: USER(webRTC) → **[gje(MMM)+STT(.json)]** → 면접관 LLM → SpatialReal/Elevenlabs TTS → USER. 이 레포는 **굵게 표시한 박스 하나**만 담당(나머지는 범위 밖).
- 기술 스택: Python 3.12 · aiortc(WebRTC) · gje(MMM)=vLLM+Gemma-4-E4B(네이티브 AV) · faster-whisper(STT 폴백) · aiohttp · ffmpeg
- 상세 설계·빌드 순서 → **`PLAN.md`** / 구조도 → `system_diagram_main.png`

## 폴더 지도 (어디에 무엇이 있는가)

| 폴더 | 용도 | 관리 주체 |
|------|------|-----------|
| `src/giljobe/` | 비즈니스 로직(ingest·windowing·critic·transcriber·emit·server) | 사람 + AI |
| `tests/` | 검증(유닛 + e2e mock/live) | 사람 + AI |
| `docs/` | 비즈니스 진실·아키텍처·스펙 (작업 **전** 참고) | **사람** |
| `.dev/` | AI 작업 기록·learnings·디버깅 | AI |
| `.claude/` | AI 설정 (rules·skills·hooks) | 사람 |

→ gje 원본은 **`/home/kio/workspace/gje`(별도 git repo)** — 여기로 **import하지 않는다**. 필수 모듈만 `src/giljobe/`로 **복사(cp)**해 리팩토링.
→ 폴더 배치 상세: `.claude/rules/project-structure.md`

## 작업 규칙 (`.claude/rules/`가 상황별 자동 로드)
- 항상: 코드 스타일 → `rules/code-style.md`, 폴더 구조 → `rules/project-structure.md`
- 테스트 작업 시: `rules/testing.md`
- `.env`/시크릿(`HF_TOKEN`·`VLLM_BASE_URL`) 작업 시: `rules/security.md`

## 일하는 방식
- **"해줘"가 아니라 "같이 계획부터".** 모호하면 만들기 **전에** 묻는다.
- **완료 기준을 먼저, 측정 가능하게 합의**한다(예: "pytest 통과 + 실 vLLM 스파이크 PASS"). 충족할 때까지 반복.
- **만드는 관점 ↔ 검증하는 관점을 분리**한다. 코드뿐 아니라 동작도 직접 확인.
- 같은 작업 3번 → Skill, 같은 실수 3번 → 이 파일 또는 `rules/`에 규칙으로.

## 경계 (DON'Ts) — ★ 이 세션의 사고에서 학습됨
- **gje 원본(`/home/kio/workspace/gje`) 디렉토리를 이동/이름변경/수정 금지.** "가져오기"는 항상 **지정 파일 복사**, 전체 `mv` 금지.
- 파일시스템 구조가 예상외로 바뀌면(경로 소멸/등장) → **멈추고 보고**, 그 위에 작업 쌓지 말 것.
- 삭제·배포·외부 발송 등 되돌리기 어려운 작업은 **실행 전 승인**.
- 비밀키·`.env`(`HF_TOKEN` 등) 값 출력/커밋 금지. SQL 쓰면 parameterized만.
- vLLM 컨테이너는 유휴 시 `docker stop` + `nvidia-smi`로 VRAM 해제(GPU 위생).

## 참고 문서 (필요할 때만 — Progressive Disclosure)
- 구현 설계·빌드 순서: `PLAN.md`
- 아키텍처/컨벤션/도메인 용어: `docs/`(채워지면)

<!-- 여기에 모든 걸 적지 말 것. 상황별 상세는 docs/·rules/·PLAN.md로 위임하고, 이 파일은 지도로만 유지. -->
