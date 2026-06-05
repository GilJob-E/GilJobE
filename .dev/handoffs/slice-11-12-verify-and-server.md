# Handoff — Slice 11(통합 계약 실검증) + Slice 12(우리 HTTP 서버 = PR 본체) (완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 순으로 읽는다.
> 이번 세션은 "슬라이스 11 진행"으로 시작 → 검증이 그쪽 wrapper 버그를 드러내 **우리 서버 빌드(Slice 12)**까지 이어짐.

## 결론 (DONE)

1. **Slice 11 — 통합 계약 실검증**: 그쪽 `analysis-engine`(우리 레포 클론 + 자작 wrapper)을 **무플립**으로 라이브 구동
   → 계약은 구조적으로 동작하나 **wrapper가 전사를 못 냄**(+버그 4종). 우리 코드는 정상임을 **대조군으로 격리**.
2. **Slice 12 — 우리 HTTP 서버**: 통합 계약(`/subscriber/start|stop`·`/signals`·`/healthz`·`/readyz`)을 우리가 직접
   구현(`src/giljobe/server/`). 단일 영속 aiohttp 루프 위에서 구독자를 돌려 wrapper 버그 4종을 **구조적으로 제거**.
   **오프라인 108 passed + 라이브 전 루프 PASS(전사 281자) + 적대리뷰 6확정 수정.**

## Slice 11 — 검증 결과 (그쪽 wrapper 무플립 라이브)

- **환경 발견**: `ANALYSIS_ENGINE_ENABLE_SUBSCRIBER=false`인데도 `POST /subscriber/start`가 실제 subscriber를
  띄움(202, threadAlive) → 플래그는 **부팅 auto-start만 게이트** → **플립 없이 검증 가능**(그쪽 스택 무변경).
- **livekit `node_ip=100.65.10.126`(Tailscale) 잔여 LIVE** — webcam 파킹 실험 잔여(재시작에도 원복 안 됨).
  ★ **in-container subscriber media를 막지 않음 확인**(영상 nv 12윈도우 동작, ICE udp `100.65.10.126↔172.19.0.10`).
- **부분 성공 + 버그 4종**(그쪽 in-container subscriber):
  - ✅ 영상/nv 동작(실 WindowCritic 한국어 note) · ✅ 계약 스키마 래핑 정상.
  - ❌ **전사 전부 빈 문자열**(`transcript:""` ×12, `turn_end.transcript_full:""`).
  - ❌ **발행 중 /signals records=0** (stop flush 후에만 12개 노출 — 라이브 폴링 무효).
  - ❌ **turn_end.eval null** · ❌ **`error putting to queue: Event loop is closed` 11회 폭주**(livekit-rtc FFI가
    닫힌 루프로 배달 = wrapper의 subscriber 루프 lifecycle 오류).
- **★ 결정적 격리(대조군)**: `critic.py:152`는 `WindowCritic(client)` 시 `GemmaNativeTranscriber`를 **자동 기본생성**.
  현재 똑같은 스택(Tailscale node_ip·공유 gemma-e4b 8000)에서 **우리 host subscriber**(`.dev/slice11/control_host_subscriber.py`,
  slice10 하먼스)로 같은 브라우저 publish를 돌리니 → **321자 한국어 verbatim 전사 + 12/12 윈도우 채움 + turn_end.eval 정상**.
  → **우리 코드(b769120)는 정상. 빈 전사·루프 버그는 그쪽 wrapper(`/app/server.py`)의 통합 glue 귀속.**
  이것이 `ENABLE_SUBSCRIBER=false`로 꺼둔 이유를 실증.
- evidence: `.dev/slice11/contract-loop-raw.json`(그쪽 wrapper 런), `.dev/slice11/control-host-subscriber-evidence.json`(대조군).

## Slice 12 — 우리 HTTP 서버 (PR 본체)

신규 `src/giljobe/server/`:
- **`service.py`** — `AnalysisService`(턴 lifecycle: start/stop/signals, 단일 세션, 주입가능=테스트) + `MemorySink`
  (레코드 누적, /signals가 읽음) + `signals_payload`(통합 프론트 래퍼 모양: records/transcriptFull/windowTranscripts/
  latestTurnEnd/rawMediaExposed:false/...). **★ 설계 핵심**: 구독자·드레인·poll·worker→loop 브리지를 전부 **한 영속
  이벤트루프**(HTTP 서버 루프) 위에서 → wrapper의 "Event loop is closed"·전사빔·스트리밍 무가시가 **구조적으로 없음**.
- **`http_app.py`** — aiohttp 라우트(내부 경로, caddy가 `/analysis` prefix 외부 부착 — 로그 실측). 한국어 ensure_ascii=False.
- **`__main__.py`** — 프로덕션 엔트리(`python -m giljobe.server`, 포트 ANALYSIS_ENGINE_PORT|PORT|8200). 공유 critic 1개
  재사용 + on_startup warmup. **통합 컨테이너 CMD를 이걸로 바꾸면 그쪽 자작 wrapper 대체.**
- `pyproject.toml`: `aiohttp` 명시 의존 승격.

**검증(완료기준 둘 다 충족)**:
- **오프라인 `pytest` 108 passed**(기존 93 무회귀 + `tests/test_server_http.py` 15: 계약·lifecycle·★발행중 records가시·
  적대리뷰 회귀가드 4). *GPU/LiveKit 불요(MockCritic+FakeRoom).*
- **라이브 전 루프 PASS**(`.dev/slice11/verify_our_server.py`): 우리 서버를 host 8201로 스택(livekit 7880·공유 gemma
  8000)에 붙여 브라우저 publish → start→/signals→stop. **발행 중 records 실시간 증가(2→13)·window 전사 11/11 채움·
  transcriptFull 281~284자 한국어 verbatim·turn_end.eval 채워짐·"Event loop is closed" 0회.** evidence `our-server-live.json`.

| 항목 | 그쪽 wrapper (Slice 11) | **우리 서버 (Slice 12)** |
|---|---|---|
| 전사 | ❌ 빈 문자열 | ✅ window 11/11, transcriptFull 281자 |
| 스트리밍 가시성 | ❌ stop 후에만(records=0) | ✅ 발행 중 실시간(2→13) |
| turn_end.eval | ❌ null | ✅ 채워짐 |
| 이벤트 루프 | ❌ "Event loop is closed" ×11 | ✅ 0회(단일 영속 루프) |

## 적대리뷰 (ultracode 워크플로, 14에이전트 / 713k 토큰)

4차원(정확성/동시성·계약·lifecycle·시크릿) 발견→적대검증→종합. **6확정 / 4기각(not-a-bug, 토큰-로그누수 의혹 반박)**.
확정 전부 **실패/엣지 경로 리소스 누수**(happy-path는 라이브 PASS) — PR 품질로 5건 수정 + 회귀가드 4:
- **major** ① `start()`서 `connect()` 실패 시 drain/poll 태스크+윈도워 누수 → `service.py` try/except→`aclose()`.
  ② 동시 `start` TOCTOU 경합 → 좀비 구독자 → `asyncio.Lock` 직렬화(`_start_locked`/`_stop_locked`).
  ③ `aclose()` 예외/취소 시 소유 executor(7스레드풀) 누수 → `subscriber.py` `windower.close()`를 **finally 첫 줄** 보장.
- **minor** ④ `_active`가 `_last` 마스킹 → 직전 턴 /signals 소실 → 둘 다 sid 룩업. ⑤ `stop()` aclose 예외 전파 →
  미처리 500 → try/except로 `stopped` 보장.
- 회귀가드(`test_server_http.py`): connect실패 누수 0·동시start 직렬화(좀비 0)·stop계약 유지·_last 회수.

## 통합 타깃 상태 갱신 (메모 `[[giljob-docker-integration-target]]`)

- 그쪽 `analysis-engine`(8200)은 우리 레포 클론 + **자작 wrapper(`/app/server.py`)** 로 실행 — 그 wrapper가 subscriber
  루프 lifecycle을 잘못 다뤄 전사빔/스트리밍무가시(=`ENABLE_SUBSCRIBER=false` 이유 추정 실증).
- **PR 방향 확정**: 우리 레포가 **올바른 서버(`python -m giljobe.server`)** 를 제공 → 통합 CMD를 이걸로 바꾸면 전사 동작.
  계약(start/stop/signals 스키마·내부경로·룸 `giljob-session-{id}`)은 그쪽 자작본과 호환되게 맞춤.

## ★ 다음 슬라이스 후보 (Slice 13 — PR 마감)

핫패스(입력→비평+전사→JSON)는 **합성·실critic·라이브·실브라우저·in-container 계약·우리 서버**까지 전부 닫힘.
북극성(`[[mvp-pr-north-star]]`) = MVP+PR. 남은 일:
1. **컨테이너 패키징**(메모 컨테이너 규약): `Dockerfile`(python:3.12-slim + ffmpeg/livekit FFI — alpine 불가, ADR감) ·
   `requirements.txt`(pyproject→pip 변환) · `/healthz`·`/readyz`(이미 구현) · **모든 디렉터리 `AGENTS.md`+`CLAUDE.md` 쌍**
   (그쪽 `test_agent_docs_contract.py` DOC_PAIRS 강제, 영어, "Read the local AGENTS.md first" 포함).
2. **PR**(`github.com/GilJob-E/GilJobE` 코드 → `GilJob_v2`/`services/analysis-engine`): 우리 서버 엔트리 채택 + compose
   `depends_on: gemma-e4b` + `VLLM_BASE_URL` 주입. PR 메커닉은 레포가 hoddukzoa 홈이라 조율.
3. **그쪽 조율/blocker 보고**: (a) 우리 서버 엔트리로 CMD 교체(자작 wrapper 대체) · (b) 스택 blocker — 번들 SDK
   2.19.1↔서버 1.8.4 비호환, ai-engine Gemini 크레딧 소진(429), node_ip(컨테이너-내부 vs 원격 양립).
4. (부수) `.dev/slice11/verify_our_server.py`를 tests/ 게이트 e2e로 승격 · 실 사람-웹캠(`[[webcam-path-parked]]`).

## 무엇을 만들었나
```
src/giljobe/server/service.py     AnalysisService + MemorySink + signals_payload (신규)
src/giljobe/server/http_app.py    aiohttp 라우트 (신규)
src/giljobe/server/__main__.py    프로덕션 엔트리 python -m giljobe.server (신규)
src/giljobe/server/subscriber.py  aclose() finally 보장 수정(executor 누수 fix)
tests/test_server_http.py         15 계약+lifecycle+회귀가드 (신규)
pyproject.toml                    aiohttp 명시 의존
.dev/slice11/                     검증 하먼스 + evidence(드라이버·대조군·우리서버 라이브)
```
**제외(.gitignore 후보)**: `.dev/slice11/our-server.log`, kor.* 파생물은 slice10 .gitignore에 이미.

## 환경 메모
- 스택 UP(giljob-v2). **우리 vLLM/livekit 띄우지 말 것**(공유 gemma-e4b 8000·livekit 7880).
- ⚠ livekit `node_ip=Tailscale(100.65.10.126)` 아직 LIVE(webcam 파킹 잔여). in-container엔 무해 확인했으나, hoddukzoa
  다음 compose up시 127.0.0.1로 원복됨. 통합 전 정리 필요(`[[webcam-path-parked]]`).
- ★ `ENABLE_SUBSCRIBER`는 그쪽 컨테이너 소관 — 이번엔 **플립 안 함**(불필요 발견). 그쪽 인프라 무변경 유지.

## git
제안 커밋: `feat(slice12): 통합 계약 HTTP 서버(server/) — 단일 영속 루프로 그쪽 wrapper 버그 4종 구조 제거; 오프라인 108 + 라이브 전 루프 전사 PASS + 적대리뷰 6확정 수정`.
- 커밋: `src/giljobe/server/{service,http_app,__main__}.py`·`subscriber.py`·`tests/test_server_http.py`·`pyproject.toml`·
  `.dev/slice11/*`·이 핸드오프·`NEXT.md`.
