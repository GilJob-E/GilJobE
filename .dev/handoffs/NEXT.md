# NEXT — 새 세션이 가장 먼저 읽는 파일 (고정 진입점)

> SessionStart 훅이 이 파일을 가리킨다. **매 슬라이스 종료 시 이 파일을 다음 슬라이스로 갱신**한다.
> 규칙: `CLAUDE.md` "세션 시작 프로토콜". 전체 빌드 순서: `PLAN.md §7`.

## 지금 상태 (최근 갱신: Slice 2 완료 시점)
- **완료: Slice 1** — 스캐폴드 + gje 4모듈 verbatim 복사 + 계층형 src. 상세 → `.dev/handoffs/slice-01-scaffold.md`.
- **완료: Slice 2** — ★ STT 필요성 스파이크 **PASS**. 상세 → `.dev/handoffs/slice-02-stt-spike.md`.
  - **결정: `GemmaNativeTranscriber` 기본 STT, faster-whisper 안 붙임**(VRAM 추가 0). 정본 evidence: `tests/evidence/spike-transcribe.json`.
  - 신규: `analysis/transcriber.py`(Transcriber 프로토콜 + GemmaNative), `tests/test_spike_transcribe.py`.
  - ★ 발견·수정 버그: **한국어 user 지시문은 전사에 echo됨 → user-turn 텍스트는 영어 `"Transcribe."`** (transcriber.py 주석).
  - ★ 데이터 주의: gje 클립(vid_0001/0033)은 **영어**라 부적합. 스파이크는 **`kor.mp4`**(repo 루트, 한국어, gitignore라 미커밋)로 함. 없으면 spike skip.
  - `pytest -q` **6 passed**. GPU 위생 OK(vLLM stop, VRAM 해제 확인).

## 다음 할 일 = Slice 3: critic 리팩토링 + mocks — PLAN §3 / §7-3
1. 먼저 `PLAN.md §3` + `.dev/handoffs/slice-02-stt-spike.md`(다음 슬라이스 섹션) 정독.
2. **`analysis/critic.py`**: gje `native_eval.WindowEvaluator`(`/home/kio/workspace/gje/src/local_infer/native_eval.py`) → `WindowCritic` 리팩토링.
   - `read_nonverbal`(채널① 핵심)·`evaluate_window`(채널② 부차) + 헬퍼(`_ask_json/_extract_json/_repair_truncated_json/NONVERBAL_SYSTEM/_video_part/_audio_part/MODEL`) **그대로 포팅**.
   - **신규 `transcribe_window(pcm,*,t,window_s,sample_rate=16000)→str`**: 주입된 `Transcriber`(기본 GemmaNative)에 위임(critic은 STT를 박지 않음).
   - import: cross-subpackage=absolute(`from giljobe.models.signals…`, `…media.window_assembly`, `…llm.vllm_client`), sibling=relative(`from .transcriber import`).
3. **`analysis/mocks.py`**: `MockCritic`(고정 신호, vLLM/ffmpeg 불요)·`MockTranscriber`(고정 문자열)·`SlowMockCritic`(레인 독립 테스트용 sleep).
4. transcriber: GemmaNative는 **이미 완료**. faster-whisper는 **안 만든다**(스파이크 PASS — 인터페이스 뒤 폴백 자리만 유지).
5. 검증: 실 vLLM에서 `WindowCritic.transcribe_window` 한국어 전사 / `MockCritic` 오프라인. (vLLM 기동 시 GPU 위생 — 끝나면 `docker stop` + `nvidia-smi`.)

## 불변 규칙 (매 슬라이스 공통)
- src = 계층형 subpackage. import: **intra=relative / cross=absolute** (PLAN.md §1, 메모 `[[layered-src-structure]]`).
- gje 원본(`/home/kio/workspace/gje`)은 **복사만**, 이동/수정 금지. FS·데이터가 예상과 다르면 **멈추고 보고**(이번 슬라이스 영어클립 사건처럼).
- 되돌리기 어려운 작업(삭제/외부발송)은 **승인 후**. 시크릿(`HF_TOKEN`) 출력/커밋 금지. 커밋+푸시는 작업단위 끝 습관(메모 `[[commit-habit]]`).
- 완료 기준은 측정 가능하게(pytest 통과 + 실 vLLM 검증), 충족까지 반복.
- 슬라이스 끝: `.dev/handoffs/slice-NN-*.md` 아카이브 작성 + **이 NEXT.md 갱신**.
