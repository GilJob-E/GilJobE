# Handoff — Slice 1: 스캐폴드 + verbatim 복사 (완료)

> 슬라이스마다 clear하므로 다음 세션은 이 문서부터 읽는다. 플랜: `~/.claude/plans/glistening-growing-kernighan.md`, 전체 빌드 순서: `PLAN.md §7`.

## 무엇을 만들었나 (DONE)

gje의 안정 모듈 4개를 giljobe로 **byte-identical verbatim 복사**하고, src를 **계층형 subpackage**로 구조화. 첫 오프라인 테스트로 빌드 배선 증명.

```
src/giljobe/
  __init__.py            (빈 패키지 마커)
  models/signals.py      ← gje signals.py (무수정)  NonVerbalSignal/EvaluationSignal/NONVERBAL_STATES
  media/window_assembly.py ← gje window_assembly.py (무수정)  assemble_video_url/assemble_audio_url
  llm/vllm_stream.py     ← gje vllm_stream.py (무수정)  iter_sse_delta_text
  llm/vllm_client.py     ← gje vllm_client.py (무수정)  VllmClient/default_vllm_client
pyproject.toml           (신규)  requests dep + test/whisper extras + pytest pythonpath=src
tests/test_window_assembly.py  (gje 테스트 포팅: sys.path 핵 제거, import→giljobe.media)
.venv/                   (신규)  pytest 9.0.1 + requests 2.32.5 만 설치
```

## 계층 구조 + import 컨벤션 (★ 후속 슬라이스 필독)

target 트리(YAGNI로 아직 안 만든 것 포함) — 의존 방향 단방향 `server → pipeline → analysis → {models, llm, media}`, `emit → models`:
- `models/` 도메인 계약(순수데이터): signals[S1], records[S5: WindowRecord]
- `llm/` vLLM 전송: vllm_client[S1], vllm_stream[S1]
- `media/` 인코딩·수신: window_assembly[S1], ingest[S7]
- `analysis/` 추론(모델호출): critic[S3], transcriber[S3], mocks[S3]
- `pipeline/` 오케스트레이션: windowing[S4]
- `emit/` 송출: sink[S5]
- `server/` 앱: app[S8]
- `config.py` 루트[S3+]

**Import 규칙**: 같은 subpackage sibling → **relative**(`from .vllm_stream import`); 다른 subpackage → **absolute**(`from giljobe.models.signals import`). (메모: `[[layered-src-structure]]`)
**YAGNI**: 빈 subpackage 미리 만들지 말 것 — 파일 생길 때 함께 생성.

## 검증 결과 (PASS)

- `pytest tests/test_window_assembly.py` → **5 passed** (video basic/cap16, audio basic/cap30s, empty-raises). configfile=pyproject.toml, pythonpath=src 정상.
- import smoke: `from giljobe.models import signals; from giljobe.media import window_assembly; from giljobe.llm import vllm_client, vllm_stream` → **OK**, `default_vllm_client().base_url == http://localhost:8000`.
- 4개 모듈 md5 = gje 원본과 **IDENTICAL**.
- 재현: `cd /home/kio/workspace/giljobe && .venv/bin/python -m pytest tests/test_window_assembly.py -v`
- **실데이터 스트리밍-흉내 검증(추가)**: `.dev/verify_slice1_stream.py` — vid_0001/vid_0033을 ffmpeg로 1fps JPEG+16k PCM 추출(ingest 대체) 후 3초 슬라이딩 윈도우(windowing 대체)로 `window_assembly` 통과. vid_0001=15윈도우, vid_0033=17윈도우 **둘 다 ALL_OK**, 캡(16프레임/30s)도 실데이터로 확인. 증거: `.dev/slice1-stream-vid000{1,33}.json`. (정식 pytest 아님 — gje 클립 경로 결합이라 `.dev` 스크래치. 재현: `PYTHONPATH=src .venv/bin/python .dev/verify_slice1_stream.py <clip> <out.json>`)
  - ⚠️ 이건 Slice 1의 `media/window_assembly`만 실데이터로 증명한 것. **진짜 WebRTC 스트리밍(ingest=S7), TurnWindower(S4), 추론/전사(S3·vLLM)는 여전히 미검증** — 각 슬라이스에서.

> ⚠️ 이 세션 하니스가 **터미널 출력을 중복/뒤섞어 표시**하는 버그가 있었음(실제 FS·실행은 정상). 출력이 이상하면 `sort -u`/파일로 빼서 `Read`로 확인. 실제 디렉토리는 `python3 -c "import os;print(os.listdir(...))"`가 authoritative.

## 다음 슬라이스 = Slice 2: ★ STT 필요성 스파이크 (게이팅) — PLAN §2 / §7-2

목적: gemma-4-E4B가 충분히 정확히 한국어 전사하는가 → 별도 STT 엔진(faster-whisper) 생략 여부 결정. **이게 transcriber 기본값을 정하므로 critic/transcriber 슬라이스보다 먼저.**

선행 조건:
- **vLLM 기동 필요**(현재 미기동, 8000 미응답): `HF_TOKEN=… bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh` → `curl localhost:8000/v1/models`에 `google/gemma-4-E4B-it` 뜰 때까지 대기(~1–2분). 다운이면 테스트 health 게이트로 skip.
- 실측 클립: `/home/kio/workspace/gje/vid_0001.mp4`(43.5s), `vid_0033.mp4`(50.3s) — h264+mp3 44.1k 스테레오.
- 절차: 두 클립 10–16s 구간을 16k mono Int16 PCM 추출(`ffmpeg -ss S -t D -i clip -vn -ar 16000 -ac 1 -f s16le pipe:1`) → GemmaNative 전사(한국어 verbatim-ASR 프롬프트, temp=0.0) → 사람 루브릭 판정(verbatim/길이일치/환각없음/지연수용). 결과를 `tests/evidence/spike-transcribe.json`에 기록.
- 분기: PASS→GemmaNativeTranscriber 기본(whisper 안 붙임). FAIL→FasterWhisperTranscriber 기본 + faster-whisper deps 추가.
- GPU 위생: 끝나면 `docker stop` + `nvidia-smi`로 VRAM 해제.

주의: Slice 2는 `analysis/transcriber.py`의 GemmaNative 일부를 먼저 만들어야 스파이크 가능(또는 스파이크용 최소 호출 스크립트). PLAN §2/§3 참고해 범위 정할 것.

## 미설치 deps (후속 슬라이스에서)
`aiortc~=1.14`, `av`(PyAV), `aiohttp`, `numpy`, `Pillow` → ingest[S7]/server[S8]에서 추가. `faster-whisper` → S2 FAIL 시.

## git
미커밋. 커밋은 **사용자 승인 후에만**. (제안 메시지: `feat(slice1): scaffold layered src + verbatim copy gje 4 modules + window_assembly test`)
