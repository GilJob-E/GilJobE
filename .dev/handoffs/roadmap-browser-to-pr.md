# 로드맵 — 실 브라우저 작동 테스트 → PR (Slice 10~13)

> Slice 9까지 핫패스(입력→비평+전사→JSON)는 **실 critic 라이브 + 공유 스택 백엔드**로 닫혔다. 여기서부터
> 목표 = **진짜 브라우저(getUserMedia)로 작동 → 통합 레포 PR**. 이 문서는 그 다중 슬라이스 계획(durable).
> NEXT.md가 매 슬라이스 진입점, 이 문서가 전체 그림. 상세 결정·환경은 메모리 `giljob-docker-integration-target`.

## 지금까지 vs 목표 (간극)
Slice 9 = **합성 publisher**(우리가 kor.mp4를 직접 LiveKit에 push) → 검증: 구독·ingest·실 critic·전사·emit·레이턴시.
**합성 publisher가 건너뛴 4가지 = 남은 일**:
1. 브라우저 **getUserMedia → livekit-client SDK 인코딩 → publish** (앞단 우회했음)
2. 실 **세션/토큰 흐름** (`POST /api/sessions` → 룸 `giljob-session-{id}` → candidate 토큰)
3. **출력 배선** (transcript_full → ai-engine `lastAnswer` → next-question). 현재 JSONL/SSE까지만.
4. **턴 경계 트리거** (실 인터뷰에서 "답변 끝"을 누가 알리나)

## 환경 사실 (2026-06-04 실측 — 새 세션 인지)
- **통합 스택 giljob-v2가 로컬 UP**(`docker ps`로 확인; 내려가 있으면 메모리 §재기동법으로 `docker start`).
  - **caddy `:80` = 인터뷰 웹 프론트** 도달(title "GilJob v2 · Local Interview Media") → **브라우저 테스트 즉시 가능**.
  - **livekit `:7880`**(스택). 키는 테스트 `_resolve_creds`가 컨테이너 env에서 자동 해석(env→container→dev). ★ 스택 API key 문자열이 `devkey`라 dev fallback과 *값*으론 구분 불가 → source 라벨로 구분(시크릿 값 미기재).
  - **gemma-e4b `:8000` = 우리 분석 백엔드 동일물**(동일 이미지·모델, 오디오네이티브 실측). `VLLM_BASE_URL` 기본 8000 → 그대로 공유. **우리 vLLM 따로 안 띄움.**
  - ai-engine `:8100`·api·analysis-engine `:8200`은 **caddy 뒤 내부망 전용**(호스트 직접포트 미노출).
- **통합 슬롯 예약됨**: `giljob-v2-analysis-engine-1`(8200) = 현재 placeholder = **우리 코드가 들어갈 자리**(PR 타깃).
- 우리 `server/`엔 **상시 서비스 진입점 없음**(build_subscriber=팩토리, 테스트가 구동). 로컬 데모 `web/`는 LiveKit 피벗으로 제거됨.

---

## Slice 10 — 실 브라우저 e2e 스모크 ⭐ (다음)
**목적**: 진짜 브라우저가 publish, 우리 subscriber(기존 코드)가 같은 룸에 붙어 실 발화를 전사·분석.
**두 방식(둘 다 가치 — 사람 먼저, 자동화로 고정)**:
- **(A) 사람 스모크** — 사용자가 브라우저(caddy:80 인터뷰룸)에서 웹캠으로 말함. 가장 진짜(즉흥 한국어·노이즈·landscape).
- **(B) 자동화** — Chrome `--use-fake-device-for-media-stream --use-file-for-fake-video-capture=kor.y4m --use-file-for-fake-audio-capture=kor.wav`. **진짜 getUserMedia→livekit-client→publish 경로**를 거치되 재현 가능. Claude-in-Chrome/Playwright 구동. → 커밋 가능한 e2e. (kor.mp4 → y4m/wav 변환 ffmpeg 필요.)
**핵심 선결 = 룸 id 코디네이션**(오픈 질문 #1): 브라우저가 `POST /api/sessions`로 만든 룸 id를 **우리 subscriber가 알아야** 같은 룸에 붙는다. 스모크 최단경로: 세션 하나 생성→룸 id 확보→우리 subscriber를 그 룸으로 띄움→말함→JSON 관찰. (수동 조율로 시작, 자동화는 api 흐름 파싱.)
**완료기준**: 브라우저 publish → 우리 JSON에 발화 일치 transcript + nv 신호, 풀 답변 동안 무크래시.
**이때 닫는 것**:
- ★ **deferred #1 크로스트랙 t0 = 실 브라우저 측정**(파일 publisher는 하한 ~0.3s만 줬음; 카메라 기동 skew 실측 → **>1s면 공유 wall-clock t0 도입** 결정). `test_e2e_live_critic`의 crosstrack 계측 재사용.
- 즉흥 한국어/노이즈 전사 품질, **landscape 웹캠**(세로 squish 해소) 재확인.
**리스크**: fake-device 미디어 포맷(y4m/wav), 웹 세션 생성 흐름, 룸 id 전달.

## Slice 11 — 출력 배선 + 턴 오케스트레이션 (가장 큰 오픈, 팀 합의)
**목적**: transcript_full이 ai-engine에 도달해 next-question 구동.
**산출물**: `emit/sink.py`에 **`WebhookSink`**(turn_end → ai-engine, PLAN §4 (c)) + 턴 경계 트리거 연결. emit는 **models만 의존** 유지.
**완료기준**: 답변 종료 → ai-engine 호출(직접 or api 경유) → 다음 질문이 우리 전사 기반.
**미정의(메모리 "호출 배선")**: ① 누가 next-question 호출(우리 직접 vs api 중계) ② 턴 끝 신호 소스(UI 버튼/VAD 무음/api). 현재 connect=턴시작, 수동 aclose=턴끝. → **api/ai-engine 팀 합의 필요**.

## Slice 12 — 서비스화 + 컨테이너 패키징
**목적**: placeholder `analysis-engine`(8200)를 실 구현으로 교체, 상시 서비스.
**산출물**:
- **서비스 진입점**(`server/__main__`/main): 룸 join → `LiveKitSubscriber` 수명관리 → `/healthz`·`/readyz`. (build_subscriber는 이미 조립 seam.)
- **세션 라우팅**: 어느 룸에 붙을지(api 알림/세션 레지스트리). v1=단일세션부터, 멀티세션 후속.
- **Dockerfile**(slim base + ffmpeg + 우리 deps) · **requirements.txt**(pyproject 추출) · **AGENTS.md+CLAUDE.md 쌍**(그쪽 contract test 강제) · compose 등록(8200, `depends_on: gemma-e4b/livekit`, env `VLLM_BASE_URL=http://gemma-e4b:8000`·`LIVEKIT_*`).
- ★ **자체 vLLM/GPU 불필요** — 스택 gemma-e4b 공유(실증).
**완료기준**: `docker compose up`로 analysis-engine healthy → 세션 join → 처리 → ai-engine 송출.
**흡수 견고성**: slice-08 #4(멀티턴 executor `wait=True`/future cancel), #5(JSONLSink fd), ICE/재접속 실패 핸들링.

## Slice 13 — PR
**목적**: `services/analysis-engine`를 통합 레포(GilJob_v2)에 랜딩.
**산출물**: giljobe 코드를 `services/analysis-engine/`로 재배치 + 컨테이너 contract + AGENTS/CLAUDE.
**완료기준**: PR 오픈, healthcheck 통과, 문서 쌍 통과. **레포=hoddukzoa/GilJob_v2라 PR 메커닉·권한 협의 필요**.

---

## 가로지르는 이슈 (슬라이스에 흡수)
| 이슈 | 어디서 | 비고 |
|---|---|---|
| 크로스트랙 t0(deferred #1) | S10 실 브라우저 측정 | >1s면 공유 wall-clock t0 도입 |
| 턴 경계/VAD | S11 | 실 인터뷰 턴 끝 트리거 — 설계 결정 |
| 출력 호출 배선 | S11 | 우리 직접 vs api 중계 |
| hidden 토큰 발급 | S11/12 | self-mint(공유 키)→api `issue_analysis_token`(hidden)이 정석 |
| 멀티세션 | S12 | v1 단일세션 명시 / per-session 윈도워 |
| 노이즈·다화자 전사 | S10 실 발화 | 단일 화자 클립만 검증됨 |
| 세로 squish(640x480) | S10 | 실 웹캠 landscape라 완화 |
| 견고성(executor·fd·재접속) | S12 | slice-08 deferred #3~5 |

## 팀 합의 오픈 질문 (코드보다 먼저)
1. **룸/세션 id를 우리 subscriber가 어떻게 아나** — api 알림 vs 룸 watch? (S10 스모크는 수동 조율로 우회 가능)
2. **출력 흐름** — analysis-engine이 ai-engine 직접 호출 vs api 중계?
3. **턴 끝 신호 소스** — UI / VAD / api?
4. **토큰** — api가 hidden subscriber 토큰 발급 vs self-mint 유지?
