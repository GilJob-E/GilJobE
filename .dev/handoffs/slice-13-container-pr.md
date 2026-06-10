# Handoff — Slice 13: 컨테이너 패키징 + PR #8 (북극성 마감, 완료 — 머지 대기)

> 다음 세션은 `NEXT.md` → 이 문서 순. 북극성(`[[mvp-pr-north-star]]`) = MVP+PR — **PR까지 완료, 남은 건 머지**.

## 결론 (DONE)

1. **PR #8 OPEN**: `github.com/GilJob-E/giljob-docker` `feat/analysis-engine-use-giljobe-server` —
   "feat(analysis-engine): run GilJobE's own server (real subscriber + transcript)". 2026-06-05 개설.
   상태(06-10 확인): **OPEN · MERGEABLE · mergeStateStatus CLEAN · main(bab0bc5) 위에 정확히 리베이스(0 behind)**.
2. **PR 컨테이너 in-container 전 루프 검증 PASS** — PR이 빌드한 이미지가 스택 네트워크에서 전사를 냄
   (그쪽 wrapper가 못 내던 바로 그 환경). evidence `.dev/slice11/pr-container-live.json`.
3. **재검증(06-10, ac2551f 위)**: hoddukzoa 정렬 커밋의 "Not-tested: docker build" 갭을 실측으로 닫음.

## PR #8 내용 (커밋 2개)

- `3ea4881`(우리, 06-05): `requirements.txt` 핀 `giljobe @88a4df5`(= GilJobE main HEAD, `python -m giljobe.server`
  엔트리 포함) · `Dockerfile` CMD를 우리 서버로 교체+자작 `server.py` 삭제(362줄) · README/AGENTS/CLAUDE 계약 문서화 ·
  `test_agent_docs_contract.py` DOC_PAIRS에 analysis-engine 추가 · compose `GILJOBE_GIT_REF` bump +
  `ENABLE_SUBSCRIBER` legacy no-op 표기.
- `ac2551f`(hoddukzoa, 06-06): "Keep the GilJobE server PR aligned with main contracts" —
  `test_analysis_engine_contract.py`를 wrapper-import 검증 → GilJobE-owned 계약 검증으로 재작성(267줄 정리).
  단 **docker build 미검증 명시** → 아래 재검증으로 닫음.

## 검증 기록

### ① PR 컨테이너 in-container 전 루프 (06-05, `verify_pr_container.py` → `pr-container-live.json`)
PR 이미지(ae-pr-test)를 스택 네트워크에 띄우고(내부 livekit·공유 gemma-e4b), fake-device 헤드리스 Chrome으로
같은 룸 publish → 계약 구동:
- **발행 중 records 실시간 증가**(5s 0 → 41s 13) — 그쪽 wrapper의 "stop 후에만" 버그 부재 실증
- **window 전사 11/11 채움**, `transcriptFull` 282자 한국어 verbatim(kor.mp4 자기소개)
- `turn_end.eval` 채워짐(compact), eval 레코드 vocal/verbal/visual 정상
- "Event loop is closed" 0회

### ② 재검증 배터리 (06-10, 브랜치 tip ac2551f)
| 체크 | 결과 |
|---|---|
| 계약 스위트(tests/contract, 77) | **76 pass**; 실패 1(`test_web_static…spatialreal_vendor_assets` 404≠200)은 **origin/main에서도 동일 실패** = 기존 web-static 문제, PR 무관 격리 |
| `docker build services/analysis-engine` | **성공**(python:3.12-slim, 핀 `@88a4df5` 클린 설치) |
| 컨테이너 스모크(백엔드 env 없이) | `/healthz` **200** `{"status":"ok"}` · `nobody`로 기동 · critic warmup OK |
| `/readyz` 단독 실행 | 503 `{"ready":false}` = **정상**(`ready_check`가 vLLM 의존 게이트 — compose env에선 200) |
| 핀 freshness | `@88a4df5` = GilJobE main HEAD(이후 src 변경 없음 — b769120은 audio 문서만) → 최신 |

## 남은 일 / 결정 대기

1. **머지**: 팀 관행은 hoddukzoa12가 머지(PR #7 전례). 우리 계정도 admin이라 직접 머지 가능 — **사용자 결정 사항**.
2. **PR 코멘트(재검증 결과 공유)**: 세션 권한 정책상 차단됨(외부 발신) — 사용자가 원하면 위 ② 표를 그대로 코멘트.
3. 그쪽 스택 blocker(우리 PR 밖): ai-engine Gemini 429 · node_ip 양립(`[[webcam-path-parked]]` — 마지막 확인 시
   Tailscale IP 잔여 LIVE였으나 **현재 스택 DOWN**이라 다음 compose up 시 원복됨).
   ★ SDK 비호환(2.19.1↔서버 1.8.4)은 **upstream이 media compose 기본 이미지를 `livekit-server:v1.12.0`으로 bump**(실측
   확인)하며 그쪽이 해소하는 방향 — blocker 목록에서 강등.
4. 머지 후(post-MVP 후보): `.dev/slice11/verify_pr_container.py`의 tests/ 게이트 승격 · 실 사람-웹캠 ·
   보조 레인 통합(`[[vision-nonverbal-instability]]` MediaPipe · `[[audio-prosody-grounding]]` 프로소디 — inject-and-emit).

## 파일

```
.dev/slice11/verify_pr_container.py   PR 컨테이너 검증 하먼스 (이 커밋에 포함)
.dev/slice11/pr-container-live.json   evidence ① (이 커밋에 포함)
```

## 타임라인 (3세션 — 06-05 세션 핸드오프 누락분 복원)
- **06-05**: 패키징 + 브랜치 push + PR #8 오픈(07:02) + PR 이미지 in-container 검증 PASS(evidence ①). NEXT.md 갱신 전 세션 종료
  → 핸드오프 미작성(이 문서가 복원).
- **06-06**: hoddukzoa12가 PR 브랜치를 최신 main(bab0bc5)에 리베이스 + `ac2551f` 계약 정렬(docker build는 미검증 명시).
- **06-10**: 이 세션이 재검증 배터리(②)로 갭 닫고 마감.

## ⚠ 교훈: 동시 세션 레이스
06-10 마감 작업 중 **같은 클론에서 두 claude 세션이 동시 작업**(같은 프롬프트 중복 투입) — 한쪽 `git reset`이 다른 쪽 rebase
커밋을 대체(결과 동일해 무해), 핸드오프 중복 작성. 사용자가 한 세션을 메인 지정해 해소, 물러난 세션이 중복본 삭제.
**같은 레포 작업은 한 세션으로 모을 것**(git index는 세션 간 공유 — 경합 시 커밋 오염 위험).

## 환경 메모
- **스택 현재 DOWN**(06-10 확인, 컨테이너 0). 라이브 재검증 필요 시 hoddukzoa 스택 기동 필요(타 user 홈 — 인가 필요).
- 검증용 임시 산출물 정리됨(ae-pr8-smoke 컨테이너 rm, /tmp worktree remove). 이미지 `analysis-engine-pr8-smoke`는 로컬 잔존.
- giljob-docker 로컬 클론(`/home/kio/workspace/giljob-docker`)은 원격 PR 브랜치(ac2551f)와 동기화됨.
