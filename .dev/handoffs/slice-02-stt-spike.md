# Handoff — Slice 2: ★ STT 필요성 스파이크 (게이팅, 완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 → `PLAN.md §3` 순으로 읽는다.
> 전체 빌드 순서: `PLAN.md §7`.

## 결론 (DONE)

**스파이크 PASS → `GemmaNativeTranscriber`가 기본 STT. faster-whisper 안 붙임.**
근거: 같은 vLLM E4B(네이티브 AV)가 한국어를 verbatim 전사 — VRAM 추가 0, 저지연(warm 0.16~0.39s/window). PLAN §2 분기 PASS 조건 충족.

## 무엇을 만들었나

```
src/giljobe/analysis/__init__.py        (신규, 빈 패키지 마커 — analysis subpackage 시작)
src/giljobe/analysis/transcriber.py     (신규) Transcriber 프로토콜 + GemmaNativeTranscriber
tests/test_spike_transcribe.py          (신규) 실 vLLM, health 게이트 skip, kor.mp4 5윈도우, raw 측정치 기록
tests/evidence/spike-transcribe.json    (신규, 정본) 판정·루브릭·발견버그·caveat — 사람 큐레이션
tests/evidence/spike-transcribe.raw.json(신규, 기계) 테스트가 매 실행 덮어쓰는 측정치
README.md                               (수정) "STT 결정" 절 추가
.gitignore                              (수정) *.mp4 등 미디어 픽스처 제외 + scheduled_tasks.lock
.dev/probe_clip_language.py             (.dev 스크래치) 클립 언어 규명용 1회성 프로브
```

`transcriber.py` 요지: `assemble_audio_url`(media)·`VllmClient`(llm) 재사용, plain text 반환(JSON 파싱 안 함), `temperature=0.0`, `max_tokens=256`. system 프롬프트는 한국어 ASR, **user-turn 텍스트는 영어 `"Transcribe."`**(아래 버그 참고). import 컨벤션: cross-subpackage=absolute(`from giljobe.media…`, `from giljobe.llm…`).

## ★ 발견·수정한 버그 (다음 슬라이스가 transcriber 만질 때 깨먹지 말 것)

**instruction echo**: user 지시문을 한국어(`"이 오디오를 한국어로 전사하세요."`)로 주면, 음성과 같은 언어라 모델이 그 문장을 **발화 내용으로 착각해 전사 끝에 붙인다**. 실측: `"…저는 대화 오디오를 한국어로 전사하세요."`.
**수정**: user 텍스트를 영어 `"Transcribe."`로(system은 한국어 유지). 3변형 모두 누수 제거 확인 → 최소변경 채택. `transcriber.py`에 코드+주석(왜)으로 박아둠. → **한국어를 user-turn에 다시 넣지 말 것.**

## ★ 전제 깨짐 사건 (기록)

- gje의 실측 클립(`vid_0001.mp4`/`vid_0033.mp4`)은 PLAN 가정과 달리 **전부 영어**(인도 학생 영어 자기소개)였다. → 한국어 스파이크 전제 불충족, 일시 BLOCKED. (멈추고 보고함 — 경계 준수.)
- 부수 신호: 영어에서도 Gemma 전사 메커니즘은 verbatim·저지연으로 잘 작동.
- 사용자가 **한국어 오디오 클립 `kor.mp4`**(repo 루트, SK하이닉스 지원 자기소개, ~38s, aac 44.1k 스테레오) 제공 → 본 PASS 판정 성립. (먼저 준 `korean-vid.mp4`는 **오디오 없음** → 폐기.)
- **재현엔 `kor.mp4`가 repo 루트에 있어야 함**(gitignore라 미커밋, 바이너리). 없으면 spike 테스트는 skip.

## 검증 결과 (PASS)

- `pytest -q` → **6 passed** (window_assembly 5 + spike 1). spike는 health 게이트로 vLLM 다운 시 skip.
- 루브릭 4기준(verbatim/길이·내용/환각없음/지연) 모두 PASS — 상세 `tests/evidence/spike-transcribe.json`.
- GPU 위생 OK: `docker stop vllm-gemma4-e4b` 후 `nvidia-smi` VRAM 9/1 MiB(유휴) 확인.
- 재현: vLLM 기동(`HF_TOKEN=… bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh`, ~40s) → `.venv/bin/python -m pytest tests/test_spike_transcribe.py -v -s`.

## 검증 한계 (caveat)

- 단일 클립(n=1, 스크립트성 자기소개, 비교적 깨끗한 음향). **노이즈·즉흥발화·다화자·실 WebRTC Opus 경로 미검증** → Slice 9 라이브 e2e에서 추가 한국어 클립으로 재확인.
- GT 없음 → WER 자동 불가(사람 루브릭). 소수 어절 오인식(난/알, 특기는 걸/그 걔는)은 3s 경계 절단·단일클립 음향 혼동 수준.

## 다음 슬라이스 = Slice 3: critic 리팩토링 + mocks — PLAN §3 / §7-3

- `analysis/critic.py`: gje `native_eval.WindowEvaluator`(`/home/kio/workspace/gje/src/local_infer/native_eval.py`) → `WindowCritic`로 리팩토링. `read_nonverbal`(채널① 핵심)·`evaluate_window`(채널② 부차) **그대로**, `_ask_json/_extract_json/_repair_truncated_json/NONVERBAL_SYSTEM/_video_part/_audio_part/MODEL` 그대로. **신규 `transcribe_window(pcm,*,t,window_s,sample_rate=16000)→str`**: 주입된 `Transcriber`(기본 GemmaNative)에 위임.
  - import: critic은 `signals`(models)·`window_assembly`(media)·`vllm_client`(llm)·`transcriber`(같은 analysis → relative `.transcriber`) 의존. cross=absolute, sibling=relative.
- `analysis/mocks.py`: `MockCritic`(고정 NonVerbalSignal/EvaluationSignal, vLLM/ffmpeg 불요), `MockTranscriber`(고정 문자열), `SlowMockCritic`(레인 독립성 테스트용 sleep).
- `transcriber.py`: **GemmaNative는 이미 완료(이 슬라이스)**. faster-whisper는 스파이크 PASS라 **안 만든다**(인터페이스 뒤 폴백 자리만 유지; pyproject `whisper` extra도 그대로 둠).
- 검증: 실 vLLM에서 `WindowCritic.transcribe_window`로 한국어 전사 / `MockCritic`로 오프라인. (windowing/emit는 Slice 4·5.)

## git

이 슬라이스 커밋 예정(메모 `[[commit-habit]]`: 작업단위 끝 습관적 커밋+푸시). 제안 메시지:
`feat(slice2): STT necessity spike PASS — GemmaNativeTranscriber default (no faster-whisper)`
- 미커밋 대상: `kor.mp4`(gitignore), `.venv`, `__pycache__`.
