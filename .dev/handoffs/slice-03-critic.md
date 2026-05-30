# Handoff — Slice 3: critic 리팩토링 + mocks (완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 → `PLAN.md §4`(windowing) 순으로 읽는다.
> 전체 빌드 순서: `PLAN.md §7`.

## 결론 (DONE)

gje `native_eval.WindowEvaluator` → giljobe `analysis/critic.WindowCritic` 포팅 완료.
채널①(`read_nonverbal`)·채널②(`evaluate_window`)·JSON 방어층 verbatim, 신규 `transcribe_window`가
주입된 `Transcriber`(기본 GemmaNative, 같은 vLLM client 공유 → VRAM 0)에 위임. 오프라인 더블
3종 + 테스트 완비. **오프라인 20 passed + 실 vLLM 라이브 PASS.** 7-에이전트 적대적 리뷰 통과.

## 무엇을 만들었나

```
src/giljobe/analysis/critic.py        (신규) WindowCritic — gje WindowEvaluator 포팅
src/giljobe/analysis/mocks.py         (신규) MockCritic / MockTranscriber / SlowMockCritic
tests/test_critic_json.py             (신규, 오프라인) _extract_json/_repair_truncated_json 회귀 8개
tests/test_mocks.py                   (신규, 오프라인) 더블 계약 + 레인 독립성 7개
tests/test_critic_live.py             (신규, 실 vLLM) read_nonverbal/transcribe_window/eval, health 게이트
tests/evidence/critic-live.json       (신규, 정본) 사람 큐레이션 판정·루브릭·리뷰 요약
tests/evidence/critic-live.raw.json   (신규, 기계) 테스트가 덮어쓰는 측정치
```

### critic.py 의도된 델타 (gje native_eval.py 대비 — 그 외는 logically identical)
1. 클래스명 `WindowEvaluator` → `WindowCritic`.
2. import: cross-subpackage=absolute(`giljobe.llm/media/models…`), sibling=relative(`.transcriber`). (gje는 전부 relative)
3. `__init__`에 `transcriber: Transcriber | None` 추가 → `self.transcriber = transcriber or GemmaNativeTranscriber(client=self.client)`. **critic과 같은 client 공유**(같은 E4B, VRAM 0).
4. 신규 `transcribe_window(pcm, *, t, window_s, sample_rate=16000) → str`: `self.transcriber.transcribe`에 위임. `t`/`window_s`는 **안 씀** — read_nonverbal과 호출부 시그니처를 맞춰 windowing이 같은 (t,window_s)로 nv 신호와 전사를 페어링하게 받기만 함(주석에 "왜" 박음).
5. 3개 SYSTEM 프롬프트·`_video_part/_audio_part/_repair_truncated_json/_extract_json/_ask_json/warmup/read_nonverbal/evaluate_window` **그대로**.

### mocks.py 설계 결정
- **상태 없는 순수 함수**로 유지(호출 카운터 배제). 이유: windowing이 스레드풀 레인에서 **동시** 호출 → 공유 카운터는 동시 증가 플레이크. 호출수·cadence 검증은 windower가 loop에서 직렬화해 모은 레코드로 한다.
- `MockCritic`은 `WindowCritic` 메서드 표면(`read_nonverbal/transcribe_window/evaluate_window/warmup`)을 **시그니처까지 미러링**, 윈도우 좌표 echo(어느 윈도우 신호인지 assert 가능). 전사는 실 critic처럼 주입 `Transcriber`(기본 `MockTranscriber`)에 위임.
- `MockTranscriber`: 고정 문자열, 빈 PCM이면 빈 문자열(실 transcriber 계약과 동일).
- `SlowMockCritic(MockCritic)`: 레인별 `nv_delay/stt_delay/eval_delay`(기본 eval만 느림). `time.sleep`만 얹음.
- **Critic 프로토콜은 안 만듦**(YAGNI — 3회 규칙). 필요하면 소비자(windowing) 있는 Slice 4에서.

## ★ 발견·수정 (적대적 리뷰)

7-에이전트 워크플로(fidelity/imports/interface_compat/tests + 발견별 독립 검증자). **fidelity·interface_compat 0건**(포팅 충실·인터페이스 미러링 OK).
- **확정 1건(minor, 수정함)**: `test_slow_mock_eval_does_not_block_nv_in_threadpool` 상한 타이밍 단언(`nv_elapsed < 0.3` vs eval sleep 0.5s)이 GIL 경합 하 플레이크(검증자가 4-busy-loader로 359ms/50회중1회 재현). → **구조적 증명**으로 교체: `eval_delay=2.0`, `nv_fut.result(timeout=1.0)` + `assert not eval_fut.done()`(nv 완료 시점에 eval 미완). 벽시계 마진 제거. **4-loader 하 60/60 재확인.**
- **드롭 2건**: imports_layering — 하나는 결함 아닌 긍정 노트, 하나는 `from .transcriber import Transcriber`가 vllm_client를 transitive import한다는 nit(비실재: import-time만, 호출 없음·계약 보존 / 범위 밖: 수정하려면 금지된 transcriber.py 손대야).

## 검증 결과 (PASS)

- 오프라인: `pytest tests/test_critic_json.py tests/test_mocks.py tests/test_window_assembly.py` → **20 passed** (vLLM/ffmpeg 불요).
- 라이브: `pytest tests/test_critic_live.py -v -s` → **PASS**. nv=engaged/0.60(유효 라벨·한국어 note·좌표 echo), 한국어 전사(0.16~0.24s), compact eval(summary+critique). 상세 `tests/evidence/critic-live.json`.
- 전체: `pytest tests/` → **22 passed**(spike+critic-live 포함, vLLM 기동 시).
- GPU 위생: 끝나고 `docker stop vllm-gemma4-e4b` + `nvidia-smi`로 VRAM 해제 확인.

## 다음 슬라이스 = Slice 4: windowing — PLAN §5 / §7-4

- `pipeline/windowing.py`: gje `SlidingWindowPipeline` 불변식 유지 재구현. `add_frame/add_audio`(lock), `poll(now)`로 nv 그리드(3s) 전진·슬라이스·`nv_exec` submit, stt 그리드(nv와 같은 3s), eval 그리드(16s, `_covered_until`은 연속 prefix 성공시만 전진), 턴 경계(`/turn/start` reset, `/turn/end` flush+tail+`turn_end` emit), worker→loop는 `loop.call_soon_threadsafe`만.
- 검증: `test_windowing`(mock + 실 스레드풀) — cadence(3s/16s)·eot tail·실패윈도우 tail 복구·중복없음·**레인독립(SlowMockCritic)**·stt 레인 1윈도우 1전사. *GPU 불요.*
- 이 슬라이스 산출물 그대로 사용: `MockCritic`(배선), `SlowMockCritic`(레인독립), `MockTranscriber`. `WindowCritic`은 nv/stt/eval 세 레인의 호출 대상.
- import: `pipeline`은 `analysis`(critic/mocks)·`media`(ingest, Slice 7)·`models` 의존. cross=absolute.

## git

이 슬라이스 커밋(메모 `[[commit-habit]]`). 제안 메시지:
`feat(slice3): port WindowCritic + offline mocks; adversarial review pass`
- 미커밋 대상: `kor.mp4`(gitignore), `.venv`, `__pycache__`, `*.raw.json`은 커밋(기계 측정치 기록).
