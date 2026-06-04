# Handoff — Slice 9: 라이브 e2e + 실 critic (완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 → `PLAN.md`(§7-8은 LiveKit 피벗으로
> 일부 superseded — slice-08/09 핸드오프·NEXT.md 우선) 순으로 읽는다.

## 결론 (DONE)

실 LiveKit 구독 경로에 **실 critic**(vLLM E4B `WindowCritic` + `GemmaNativeTranscriber`)을 배선해
실제 한국어 미디어(kor.mp4)로 종단 측정했다. **PASS**(2런 안정). 측정 4종 + deferred #1(크로스트랙
t0) 전부 evidence로 확보. **src 무수정**(측정은 테스트 레벨 계측 — CaptureSink + 윈도워 프록시).

- **freshness lag**(emit − poll-clock 윈도우종료 = poll지연 + critic추론): live median **0.76~0.99s**,
  max 1.07s(2런). 3s cadence 대비 충분.
- **전사**: transcript_full이 kor.mp4 자기소개와 충실 일치(verbatim, 환각·번역·거부 없음). 도메인어
  정확(SK하이닉스/공정/파라미터 테스트/차세대 메모리). ★ **Opus 왕복 경로**(Slice 2 ffmpeg 직추출과
  다름)에도 verbatim 유지. ASR 오류는 비결정적 음운 슬립(공모전→공무전, 수상→배상 등 런마다 다름).
- **nv read**: engaged/neutral/nervous 구분 + 구체 note. 단 **긴장류 그라운딩 불가**(메모리
  `vision-nonverbal-instability` 재확인) + intensity 0.3/0.6 이분(눈금 보정 프롬프트에도 습관값).
- **eot finalize**: turn_end(transcript_full + compact eval) 파일 마지막. 발화중 풀 eval(채널② 16s)
  2건도 송출 — verbal/vocal/visual 비평이 실제 *비평가*적(사탕발림 아님).
- **deferred #1 크로스트랙 t0**: **측정 완료** → audio가 video보다 **~0.3s 선행**(run1 -0.377/run2
  -0.298). LiveKit이 video 트랙을 늦게 딜리버(첫 video t0+0.39s vs 첫 audio t0+0.01s). 0.3s ≪ 3s
  윈도우라 페어링 유효 → **v1 수용 결정**(아래 §결정).

## 무엇을 만들었나

```
tests/test_e2e_live_critic.py   (신규, 게이트) 1 — 독립 livekit + kor.mp4 publisher → 실 critic → JSONL + 계측
tests/evidence/live-critic.json      (신규) 사람-큐레이션 판정·루브릭·deferred#1 결정
tests/evidence/live-critic.raw.json  (신규) 테스트가 매 런 덮어쓰는 측정치(최신 런)
README.md                       (수정) Slice 9 상태 + STT 라이브 재확인 + 실행법
```
**src 변경 0** — 측정은 테스트 안에서만:
- `_ClockSink`(emit 시각=loop.time 기록 → freshness lag) · `_InstWindower`(윈도워 위임 프록시: 첫
  add_frame/add_audio 시각 → 크로스트랙 skew; on_signal은 inner로 라우팅).
- 배선은 build_subscriber를 *수동 전개*해 계측 훅을 끼움(critic=실 WindowCritic, recorder sinks=
  [JSONLSink, ClockSink], windower를 _InstWindower로 감쌈). 실서비스 배선과 동형(=build_subscriber).

## 환경 (이 슬라이스에서 띄움 — 재현용)

- **vLLM**: `bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh`(HF_TOKEN env 필요, 값 미출력).
  컨테이너 `vllm-gemma4-e4b`, `localhost:8000`, google/gemma-4-E4B-it. ~1–2분 로드.
- **LiveKit**: ★ 통합 giljob-v2 스택은 **OOM(Exit 137)으로 전부 내려가 있었음** → 깨우지 않고 **독립
  livekit-server를 dev로** 띄움: `docker run -d --name livekit-dev --network host livekit/livekit-server:v1.8 --dev`.
  dev 키 `devkey/secret`(플레이스홀더, 통합 키 아님). `--network host`로 localhost ICE 단순화(7880/7881/
  7882). 통합 타깃과 **동일 버전(v1.8)**이라 경로 충실. publisher/subscriber 토큰 둘 다 dev 키로 self-mint.
- kor.mp4: repo 루트, 1080x1920 h264 30fps 38.2s + aac 44100 스테레오(gitignore — 없으면 skip).
- **GPU 위생**: 슬라이스 종료 시 `docker stop vllm-gemma4-e4b livekit-dev` + `nvidia-smi`로 VRAM 해제.

## publisher 설계 (candidate 모사)

이벤트루프 블로킹 방지가 핵심(publisher·subscriber·critic이 한 루프). → **사전 디코드**: kor.mp4를
PyAV로 미리 풀어 video=360x640 RGB24 @10fps 리스트 + audio=16k mono Int16 전체 버퍼로(루프 밖). 그
뒤 realtime push만 루프에서(video=프레임 pts에 sleep 페이싱+timestamp_us, audio=20ms청크를
`AudioSource.capture_frame`이 realtime 페이싱). critic은 윈도워 executor(스레드풀)에서 도니 루프 무블로킹.
360x640으로 낮춘 이유: subscriber가 어차피 640x480으로 다시 리사이즈 → 대역폭·메모리 절감(전체 ~260MB).

## 측정 방법론 (적대적 자기검증 — 신뢰성 핵심)

- **freshness lag = emit_loop_time − (t0 + (record.t + window_s/2))**. t0=sub._t0(첫 track 구독 시각,
  loop.time). record.t=윈도우 중심 → +window_s/2=윈도우 종료(poll-clock). = **poll지연(≤poll_interval) +
  critic추론**. ★ 주의: t0가 첫 *track*(여기선 audio) 앵커라 video 미디어시계와 ~0.3s 오프셋이 있으나,
  이는 "윈도우가 due된 시점(poll-clock)→emit" 해석에선 정확하며 크로스트랙 skew로 **별도 측정**(이중계산
  아님). 드레인 브리지(call_soon_threadsafe→queue→drain) 지연 <1ms 무시 가능.
- **크로스트랙 skew = first_audio_t − first_video_t**(둘 다 첫 add_*  호출 loop.time). video는 encode
  ~2ms 편향이나 300ms skew 대비 무시. 파일 publisher(동시 송출)라 이 값은 **LiveKit 구독/네고 시차의
  하한** — 실 브라우저 getUserMedia는 카메라 기동 skew가 더해져 더 클 수 있음.

## ★ deferred #1 (크로스트랙 t0) — 측정 후 결정

**결정: v1 수용**(공유 wall-clock t0 도입 안 함). 근거: 2런 모두 skew<0.4s, 3s 그리드에서 nv·stt
페어링 오버랩 >87%, 윈도우 인덱스 어긋남 없음. **재검토 트리거**: (a) stt/nv 윈도우를 <1.5s로 줄이거나
(b) 실 브라우저에서 skew>1s 측정 시 → 서버가 공유 wall-clock t0를 양 소비자(consume_*_lk)에 주입
(slice-08 deferred #1 근본해결안; server→media 결합/복잡도↑라 YAGNI로 보류). slice-07/08 핸드오프
"크로스트랙 t0"와 동일 라인 — 이제 **측정으로 닫음**.

## 검증 결과 (PASS)

- 오프라인 `pytest`(live/spike 4개 제외) → **93 passed**(기존 무회귀; 새 게이트 테스트는 경로 제외).
- 라이브 e2e `tests/test_e2e_live_critic.py` → **PASS 2회**(window 14·eval 2·turn_end 1 동일). 게이트:
  vLLM health + LiveKit 7880 + kor.mp4, 미충족 시 skip(gje 관례).
- 판정·측정치 → `tests/evidence/live-critic.json`(루브릭·deferred#1 결정) + `…raw.json`(최신 런 수치).

## ★ 적대적 리뷰 (인라인 — 워크플로 미사용; 사용자 미옵트인)

측정 방법론을 직접 적대 검증(신뢰성 의존처): freshness lag 공식의 t0-오프셋 이중계산 우려 → **거짓**(별도
측정, 해석 정합). 드레인 지연·encode 편향 → 무시 가능 실측. 플레이크: 2런 동일 counts, lag/skew 동일
오더, 어설션 마진 큼(window≥5 vs 14, hangul≥3 vs 12). **정정 버그 0**. 잔여 관찰(차기): 끝자락 빈
partial 윈도우 1건(nv=null/tx="", EOS sliver) — turn_end sweep의 정상 동작이나 소비자에 빈 레코드 노출
(코스메틱, 무해).

## ★ 다음 슬라이스 후보 = Slice 10: 통합 패키징 (services/analysis-engine)

핫패스(입력→비평+전사→JSON)는 **라이브 실 critic까지 닫힘**. 남은 건 **통합 타깃 컨테이너화**(메모리
`giljob-docker-integration-target`): `services/analysis-engine/` — slim base(alpine→네이티브 협의)·
requirements.txt·/healthz·AGENTS+CLAUDE 쌍·내부포트. 입력=LiveKit subscribe(완성), 출력 소비자=
`services/ai-engine`(Gemini, HTTP+JSON)이라 `WebhookSink`(PLAN §4 (c)) 추가가 자연 후속. 그 전에
api 팀과 **hidden subscriber 토큰 발급 경로**(self-mint vs api 발급) 합의(slice-08 미합의 잔여).
선택 견고성(deferred): slice-08 #4(멀티턴 executor wait), #5(JSONLSink fd), 노이즈/다화자 전사 재확인.

## git
이 슬라이스 커밋(메모 `[[commit-habit]]`). 제안:
`feat(slice9): 라이브 e2e + 실 critic — 독립 livekit + kor.mp4 publisher → 실 WindowCritic/GemmaNative 종단 측정 PASS; freshness lag ~1s·전사 verbatim 유지(Opus)·크로스트랙 skew ~0.3s 측정→v1 수용`.
- 커밋 대상: `tests/test_e2e_live_critic.py`·`tests/evidence/live-critic.json`·`tests/evidence/live-critic.raw.json`·
  `README.md`·이 핸드오프·`NEXT.md`.
- **제외**: `.dev/`의 파킹된 vision/audio 탐색 스크래치(slice-06~08 관례 유지). kor.mp4(gitignore).
