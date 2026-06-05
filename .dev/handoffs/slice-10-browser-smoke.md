# Handoff — Slice 10: 실 브라우저 e2e 스모크 (완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 → `roadmap-browser-to-pr.md` 순으로 읽는다.

## 결론 (DONE, PASS — 2회 재현)

진짜 **헤드리스 Chrome**(가짜장치=kor.y4m/kor.wav) → `getUserMedia` → **livekit-client SDK 인코딩** →
LiveKit publish → 우리 hidden `LiveKitSubscriber`(실 `WindowCritic`+`GemmaNativeTranscriber`) → JSONL.
Slice 9 합성 publisher가 우회한 **#1 갭(브라우저 getUserMedia→livekit-client→publish)을 실 미디어로 닫음.**
**src 변경 0**(계측·하먼스는 `.dev/slice10/`). 오프라인 **93 무회귀**.

- **전사**: 자기소개 verbatim(이나래/SK하이닉스/공정 단축/파라미터 테스트/차세대 메모리), 319자. ASR
  비결정 음운 슬립(자만/자면, 46%/6%)은 3s 스트리밍 STT 지문(Slice 9 동일) — 라이브 파이프라인 증거.
- **nv**: engaged/neutral/nervous 구분, intensity 0.3/0.6(눈금 이분 습관 = `vision-nonverbal-instability` 재확인).
- **eval 채널**(발화중 풀평가)·**turn_end.eval**(compact): verbal/vocal/visual을 실 내용 참조해 비평(사탕발림 X).
- **freshness lag**: live median **1.124s**(n=11). **크로스트랙 skew −0.282s**.
- **트랙**: candidate가 audio(1)+video(2) 둘 다 publish, 우리 subscriber가 둘 다 디코딩 수신.

## ★★ 가장 중요한 발견 — 통합 스택이 우리 지난 세션 이후 **전진**했다

NEXT.md/roadmap이 쓰인 뒤 다른 팀(hoddukzoa)이 통합을 크게 진척시켰다. **다음 슬라이스 설계의 정본 갱신 필요**:

1. **`giljob-v2-analysis-engine-1`(8200, healthy)이 우리 레포를 클론해 실행 중** — env `GILJOBE_GIT_URL=
   github.com/GilJob-E/GilJobE.git`, `GILJOBE_GIT_REF=b769120`(= 우리 커밋, `git diff b769120 HEAD -- src/`
   = **빈 diff** → 핀 코드 = 우리 현재 src와 동일). `CMD=python /app/server.py`(그쪽이 만든 HTTP 래퍼).
   `VLLM_BASE_URL=http://giljob-v2-gemma-e4b:8000`, `LIVEKIT_URL=ws://livekit:7880`. ⚠ **`ANALYSIS_ENGINE_
   ENABLE_SUBSCRIBER=false`**(실 subscriber 경로 꺼둠 — 미검증이라서로 추정).
2. **프론트(app.js)가 HTTP 계약에 완전 배선** — 로드맵 오픈질문 다수가 **그쪽 선택으로 닫힘**:
   - `POST /analysis/subscriber/start {sessionId, criticMode:"window"}` → 룸 `giljob-session-{sessionId}`에 우리 subscriber.
   - `POST /analysis/subscriber/stop {}` → 턴 flush(end_turn).
   - `GET /analysis/signals?sessionId=` → `{records:[window/eval/turn_end], transcriptFull, windowTranscripts, ...}`
     (= 우리 SignalRecorder 레코드 그대로 래핑).
   - 턴 경계(오픈질문#3): **UI "답변 시작"=turn baseline, "답변 종료"=`/subscriber/stop`(flush)**.
   - 출력 흐름(오픈질문#2): 프론트가 `/analysis/signals` 폴링 → transcript를 `/ai/interview/next-question`에
     먹임(**api/프론트 중계**, 직접 WebhookSink 아님).
   - `POST /api/sessions`는 **요청 sessionId 존중** → 룸 id를 우리가 선택 가능(코디네이션 오픈질문#1 해소).
3. **함의**: Slice 11~13의 큰 오픈질문이 그쪽 구현으로 상당수 답이 정해짐. 우리 다음 일은 "설계"보다
   **그쪽 계약 충족 + `ENABLE_SUBSCRIBER=true` 실 검증**에 가깝다(roadmap 갱신 권장).

## ★ 스모크 중 발견한 통합 스택 blocker 3종 (전부 그쪽 인프라 — 우리 src 무관, 적대검증 holds)

- **(a) SDK↔서버 버전 비호환**: 그쪽 번들 `livekit-client 2.19.1`(protocol 17)이 서버 `LiveKit 1.8.4`
  (protocol 15)에 `/rtc/v1` 404 → 레거시 폴백 churn으로 **publish 불안정**. 우리는 호환 `2.7.5`(protocol
  15, `/rtc/v1` 없음)로 교체해 해결. **그쪽 프론트도 동일 SDK라 실 publish 시 같은 문제 예상** → 그쪽에 보고 가치.
- **(b) ai-engine Gemini 크레딧 소진**: `POST /ai/interview/next-question` → 502(`Gemini 429 "prepayment
  credits depleted"`). 프론트는 질문 생성돼야 "답변 시작"(마이크) 활성화 → **UI 경유 오디오 publish 차단**.
  그래서 그쪽 UI 게이팅을 우회하는 최소 `pub.html`(그쪽 SDK·세션/토큰은 유지) 사용.
- **(c) LiveKit ICE가 원격 브라우저 거부**: config `node_ip=127.0.0.1`·`use_external_ip=false`·`turn.enabled=
  false` → 원격 사용자 브라우저(Windows/Tailscale)는 127.0.0.1 후보만 받아 **ICE 8회 0응답 실패**. 호스트
  내부 헤드리스 Chrome은 통과(127.0.0.1 유효). **사람 직접 웹캠(길 B)은 이게 막아서 보류** — 살리려면
  사용자 터널에 **ICE-TCP 7881 포워딩** 추가 또는 스택 `node_ip=Tailscale IP` 설정 필요.

## 무엇을 만들었나 (`.dev/slice10/`)

```
smoke_browser_live.py   구독자 하먼스 — 실 WindowCritic + _InstWindower/_ClockSink 계측(Slice 9 재사용),
                        룸 attach → sentinel/MAX_S까지 JSONL 캡처 → evidence+요약. check/live 모드.
drive_browser_pub.py    publisher 구동 — POST /api/sessions(candidate token) → 가짜장치 헤드리스 Chrome
                        으로 pub.html 로드 → Playwright CDP로 publish 확인 → hold 후 sentinel touch.
pub.html                최소 publisher — livekit-client(2.7.5) getUserMedia(가짜장치)→setCamera/MicEnabled(동시).
tail_records.py         JSONL→1줄 요약(라이브 모니터용).
kor.y4m / kor.wav       Chrome 가짜장치 입력(ffmpeg 변환, gitignore — 대용량).
lk-sdk.mjs(=2.7.5)      서버 호환 SDK(jsdelivr, gitignore). livekit-client.esm.mjs=그쪽 2.19.1 사본(비교용).
subscriber-clean-run.log  무크래시 사후검증 로그.
```
**src 무수정.** 배선은 build_subscriber와 동형(계측 훅만). evidence: `tests/evidence/browser-live.json`(판정) +
`…raw.json`(최신 런 수치).

## 적대 검증 (ultracode 워크플로 4 검증자, 273k 토큰)

- **browser-path-legit = HOLDS**: 우리 subscriber는 LiveKit AudioStream/VideoStream 디코딩으로만 미디어
  소비(src에 kor 파일 read 0); kor.wav 독립 16k 직전사 = 동일 화자·스크립트 확인; 라이브 전사의 단편화·
  열화 = 파이프라인 지문(원본 복사 아님). 브라우저-외 누수 배제.
- **stack-findings = HOLDS**: (a)(b)(c) 전부 docker inspect/curl/grep 실명령 확인. "인프라 문제, 우리 코드 아님" 성립.
- **measurements = NUANCED → 클린 재실행으로 해소**: 1차 skew −0.342s는 `setMic→setCamera` 순차 await의
  **publish 순서 confound**. 동시 enable(Promise.all) 재실행 **−0.282s = LiveKit이 video를 ~0.28s 늦게
  딜리버하는 순수값**(Slice 9 파일 publisher −0.3s와 정합). ★ deferred #1 결론은 confounder 무관하게 강건.
- **completeness = NUANCED → 클린 재실행으로 해소**: 1차 turn_end.eval=None은 **버그 아님** — windowing.py
  가드(`if frames and pcm`)의 정상 동작(늦은 flush가 `_eval_covered_until`을 실영상 너머로 오버슈트).
  **즉시 flush 재실행에서 turn_end.eval 채워짐 + 빈 트레일링 윈도우 0** → 타이밍 아티팩트 확정.

## ★ deferred #1 (크로스트랙 t0) — 실 브라우저로 닫음

**v1 수용 재확인**: 실 브라우저 skew **−0.282s**(<1s 트리거 미발동). 동시 enable이라 publish 순서 아닌
LiveKit 딜리버리 순수값. 0.28s ≪ 3s 윈도우 → 페어링 유효. Slice 8/9 deferred #1 라인 **종결**.

## 검증 결과 (PASS)

- 오프라인 `pytest`(live/spike 제외) → **93 passed**(무회귀; src 미변경).
- 라이브 스모크 2회 재현(클린 런: window 12·eval 1·turn_end 1, turn_end.eval 채워짐, 빈 tail 0).
- 게이트: vLLM(스택 gemma-e4b 8000) + LiveKit(스택 7880) + Chrome + kor.y4m/wav + httpd:8899.

## 다음 슬라이스 후보 (roadmap 갱신 반영)

핫패스(입력→비평+전사→JSON)는 **합성·실 critic·라이브·실 브라우저까지 전부 닫힘.** 남은 일은 통합 마감:
1. **그쪽 `ANALYSIS_ENGINE_ENABLE_SUBSCRIBER=true` 실 검증**(Slice 11 후보) — 그쪽 analysis-engine이 실제로
   우리 subscriber를 띄워 `/analysis/signals`로 transcript를 내고, 프론트가 그걸 next-question에 먹이는 전
   루프. 그쪽 env/이유 조율 필요(왜 꺼뒀는지).
2. **그쪽에 blocker 보고**: (a) SDK 2.19.1↔서버 1.8.4 비호환, (b) Gemini 크레딧, (c) LiveKit node_ip(원격
   브라우저용 external IP/TURN).
3. **사람 웹캠(길 B)**: 터널 7881(ICE-TCP) 추가 또는 스택 node_ip 수정 후 재시도.
4. (선택) 이 스모크 하먼스를 tests/ 게이트 e2e로 승격(현재 .dev/ 수동 하먼스).

## git
이 슬라이스 커밋(메모 `[[commit-habit]]`). 제안:
`feat(slice10): 실 브라우저 e2e 스모크 PASS — 헤드리스 Chrome 가짜장치 getUserMedia→livekit-client→publish
→ 실 critic 종단; 통합 스택 전진 발견(analysis-engine=우리 레포 래퍼+프론트 계약) + blocker 3종; deferred#1 닫음`.
- 커밋: `.dev/slice10/*.py`·`pub.html`·`tests/evidence/browser-live.json`(+raw)·이 핸드오프·`NEXT.md`·`.gitignore`.
- **제외(.gitignore)**: 대용량 파생물 `kor.y4m`·`kor.wav`·`lk-*.mjs`·`livekit-client.esm.mjs`·`smoke-signals.jsonl`·로그.
