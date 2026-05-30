# NEXT — 새 세션이 가장 먼저 읽는 파일 (고정 진입점)

> SessionStart 훅이 이 파일을 가리킨다. **매 슬라이스 종료 시 이 파일을 다음 슬라이스로 갱신**한다.
> 규칙: `CLAUDE.md` "세션 시작 프로토콜". 전체 빌드 순서: `PLAN.md §7`.

## 지금 상태 (최근 갱신: Slice 1 완료 시점)
- **완료: Slice 1** — 스캐폴드 + gje 4모듈 verbatim 복사 + 계층형 src. 상세 → `.dev/handoffs/slice-01-scaffold.md`.
  - 계층 구조 + import 컨벤션 확정 (PLAN.md §1, 메모리 `[[layered-src-structure]]`).
  - `pytest tests/test_window_assembly.py` **5 passed**; 실영상 스트리밍-흉내 검증도 PASS(`.dev/slice1-stream-*.json`).
  - venv: `.venv` (pytest + requests만 설치). **git 미커밋**.

## 다음 할 일 = Slice 2: ★ STT 필요성 스파이크 (게이팅) — PLAN.md §2 / §7-2
1. 먼저 `PLAN.md §2`(스파이크 절차) + `.dev/handoffs/slice-01-scaffold.md`(다음 슬라이스 섹션) 정독.
2. **vLLM 기동**(현재 미기동, :8000 미응답): `HF_TOKEN=… bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh` → `curl localhost:8000/v1/models`에 `google/gemma-4-E4B-it` 뜰 때까지 대기. 다운이면 테스트 `client.health()` 게이트로 skip.
3. 두 클립(`/home/kio/workspace/gje/vid_0001.mp4`, `vid_0033.mp4`) 10–16s를 16k mono PCM 추출 → `analysis/transcriber.py`의 `GemmaNativeTranscriber`(한국어 verbatim-ASR 프롬프트, temp=0.0)로 전사 → 사람 루브릭 판정 → `tests/evidence/spike-transcribe.json` 기록.
4. 분기: **PASS** → GemmaNative 기본(whisper 안 붙임). **FAIL** → FasterWhisper 기본 + `faster-whisper` deps 추가.
5. GPU 위생: 끝나면 `docker stop` + `nvidia-smi`로 VRAM 해제.

## 불변 규칙 (매 슬라이스 공통)
- src = 계층형 subpackage. import: **intra=relative / cross=absolute** (PLAN.md §1).
- gje 원본(`/home/kio/workspace/gje`)은 **복사만**, 이동/수정 금지. FS가 예상과 다르면 멈추고 보고.
- 되돌리기 어려운 작업(삭제/커밋/외부발송)은 **승인 후**. 시크릿(`HF_TOKEN`) 출력/커밋 금지.
- 완료 기준은 측정 가능하게(pytest 통과 + 실 vLLM 스파이크 PASS), 충족까지 반복.
- 슬라이스 끝: `.dev/handoffs/slice-NN-*.md` 아카이브 작성 + **이 NEXT.md 갱신**.
