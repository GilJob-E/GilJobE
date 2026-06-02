# giljobe

면접 시뮬레이션 시스템의 **입력단** — 사용자의 WebRTC 웹캠+마이크 스트림을 받아 **gje(MMM) 모듈로 비언어 비평 + STT 전사**를 수행하고, 다운스트림 면접관 LLM이 먹을 **`.json`을 송출**한다.

- 전체 루프: `USER(webRTC) → [gje(MMM)+STT(.json)] → 면접관 LLM → SpatialReal/Elevenlabs TTS → USER`
- 이 레포 스코프: 위 루프의 **첫 박스 하나**(입력 수신 + 비언어 비평 + 전사 + JSON 송출). 면접관 LLM·TTS·아바타·최종평가는 범위 밖.
- 구조도: `system_diagram_main.png`

## 기술 스택
Python 3.12 · aiortc(WebRTC) · gje(MMM)=vLLM+Gemma-4-E4B(네이티브 AV) · faster-whisper(STT 폴백) · aiohttp · ffmpeg

## 상태
슬라이스 단위 진행(핸드오프+`/clear`). 진입점 → `.dev/handoffs/NEXT.md`.
- **Slice 1 완료**: 계층형 src 스캐폴드 + gje 4모듈 verbatim 복사.
- **Slice 2 완료**: ★ STT 필요성 스파이크 — **PASS**.
- **Slice 3 완료**: critic 포팅(`WindowCritic`) + 오프라인 더블(`MockCritic` 등). 적대적 리뷰 통과, 라이브 PASS(`tests/evidence/critic-live.json`).
- **Slice 4–6 완료**: windowing(`TurnWindower` 4레인)·emit(`SignalRecorder`+SSE/JSONL Sink)·e2e mock 배선.
- **Slice 7 완료**: ingest(aiortc 트랙 소비자, video→1fps JPEG / audio→16k mono PCM) + resample-list gotcha 잠금.
- **Slice 8 완료**: ★ LiveKit 구독자 입력 어댑터(통합 타깃 피벗) — hidden participant로 룸 join → candidate track 구독 → 파이프라인 → JSONL. **실 LiveKit 라이브 e2e PASS**. (상세 → `.dev/handoffs/`)
- 구현 설계·빌드 순서 → **`PLAN.md`** (단 §7-8은 LiveKit 피벗으로 일부 superseded — `.dev/handoffs/NEXT.md` 우선)
- 프로젝트 지도·작업 규칙 → **`CLAUDE.md`**, `.claude/rules/`

## STT 결정 (Slice 2 스파이크, 2026-05-30)
**`GemmaNativeTranscriber`를 기본 STT로 채택. faster-whisper 안 붙임.**
같은 vLLM E4B(네이티브 AV)로 한국어를 verbatim 전사 — VRAM 추가 0, 저지연(warm 0.16~0.39s/window).
실측: `tests/evidence/spike-transcribe.json`(판정·루브릭·발견버그), `…raw.json`(측정치).
- 주의: 한국어 user 지시문은 전사에 echo되므로 user-turn 텍스트는 영어 `"Transcribe."`로 둔다(transcriber.py 주석).
- faster-whisper는 인터페이스(`Transcriber`) 뒤 폴백으로 유지 — 노이즈/즉흥 한국어에서 품질 미달이 확인되면 config 한 줄로 교체.
- 검증 한계: 단일 클립(스크립트성 자기소개). 실 WebRTC·노이즈·다화자는 Slice 9 라이브 e2e에서 재확인.

## gje 원본
gje(MMM)는 `/home/kio/workspace/gje`의 별도 repo다. **import하지 않고**, 필수 모듈만 `src/giljobe/`로 **복사**해 리팩토링한다. (원본 디렉토리는 이동/수정하지 않는다.)

## 실행
- 오프라인 테스트: `pytest`(GPU/네트워크 불요 — 93 passed).
- LiveKit 구독자(통합 입력): env `LIVEKIT_URL` + (`LIVEKIT_TOKEN` 또는 `LIVEKIT_API_KEY/SECRET`) 설정 후
  `server.app.build_subscriber(critic, [JSONLSink(...)], SubscriberConfig.from_env(session_id=...))` → `await sub.run(url, token)`. (hidden subscriber로 `giljob-session-{id}` 룸에 붙어 candidate track 구독)
- 실 LiveKit 라이브 e2e: `pytest tests/test_e2e_live_lk.py`(서버 7880 down/키 없으면 skip).
- 실 critic 라이브(Slice 9 예정): vLLM 기동 후 종단 레이턴시·전사 품질 측정 — `PLAN.md` §6/§9.
