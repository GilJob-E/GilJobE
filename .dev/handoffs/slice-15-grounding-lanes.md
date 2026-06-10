# Slice 15 — 객관 그라운딩 레인(vision MediaPipe + audio 프로소디) inject-and-emit (2026-06-10)

> 보조 탐색 세션 `.dev/vision/`·`.dev/audio/`의 확정 결정을 메인 트랙 src로 편입한 슬라이스.
> Slice 14(PR #8 머지 — 사람 결정)는 그대로 남아 있다(이 슬라이스는 번호만 다음 것을 씀).

## 무엇을 / 왜

젬마 E4B의 비언어·vocal 채널은 상투구·자기모순·환각이 측정으로 확인됐다(비전 충돌 7건,
vocal 충돌 3/4건 — `.dev/vision/conflict-analysis.md`·`.dev/audio/conflict-analysis.md`).
두 탐색 세션이 합의한 처방 **"inject AND emit"**을 구현:
- **inject**: 객관 수치(한국어 라벨 집계)를 critic 프롬프트에 주입 + 차단 규칙
  (수치 모순 금지 / 근거 없는 관찰 금지 / 관측 불가 항목 평가 금지 / 긴장·자신감·열정 단정 금지).
- **emit**: 같은 수치를 record에 raw로 보존(`objective_nonverbal` / `objective_vocal` /
  `objective_visual`) — 모델이 수치를 무시하면 그 자체가 검출 가능한 모델 실패 신호.

## 신규 / 변경

| 파일 | 내용 |
|---|---|
| `analysis/prosody.py` **신규** | PoC 포팅: parselmouth(Hirst 2-pass F0→PDQ·sd_semitone, De Jong-Wempe 음절핵) + Praat 강도 silence run(쉼) + **numpy RMS**(librosa와 동일 정의 25ms/10ms — 의존성 절감) + `describe_vocal` + `maybe_prosody_extractor`(env `GILJOBE_PROSODY`) |
| `analysis/grounding.py` **신규** | MediaPipe Face+Pose dense 레인. face·pose **각자 단일 스레드**(VIDEO 모드 ts 단조 요구 + 순차 1스레드는 33ms 예산 초과 — combined_latency.json). epoch 가드 reset(턴 간 in-flight 격리), ts 베이스 전진(검출기 재생성 없이 단조 유지), max_backlog load-shedding(+`shed_frames_total` 노출), 집계는 순수 함수(`aggregate_face/pose`) + `describe_visual`(관측불가 차단 라벨) + `maybe_vision_grounder`(env `GILJOBE_VISION_MODELS_DIR`/`GILJOBE_VISION`) |
| `models/signals.py` | `NonVerbalSignal.objective` · `EvaluationSignal.objective_vocal/objective_visual` |
| `models/records.py` | `WindowRecord.objective_nonverbal`(nv.objective에서) |
| `analysis/critic.py` | `grounding_block()`+`GROUNDING_RULES`; `read_nonverbal(vision=)`·`evaluate_window(prosody=, vision=)` — compact tail에도 주입 |
| `pipeline/windowing.py` | `grounder`/`prosody` 주입(+`_Grounder`/`_Prosody` Protocol), `add_dense_frame`(이벤트루프→submit만), `_do_nv/_do_eval/_do_tail`이 수치 계산→inject→signal에 attach. 레인 실패는 None 강등(추론 레인 무영향). reset/close가 grounder 수명 구동(★주입이어도 윈도워가 close — 세션 수명 계약) |
| `media/ingest.py` | `rgb_array()` 분리(encode_jpeg_rgb와 공유), `consume_video_lk(dense=)` — **throttle 전** 매 프레임 콜백(풀 fps) |
| `server/subscriber.py` | video track에 `windower.add_dense_frame` 연결(grounder 있을 때만) |
| `server/app.py`·`service.py`·`__main__.py` | `build_subscriber(grounder=, prosody=)` / `AnalysisService(make_lanes=)` / 프로덕션 `_make_lanes`(env 자동 켜짐/꺼짐) |
| `pyproject.toml` | optional extras `vision`(mediapipe) · `prosody`(parselmouth+scipy, ★GPL-3 주의) |

## 검증

**오프라인 137 passed**(기존 108 무회귀 + 신규 29: `test_prosody` 12 · `test_grounding` 9 ·
`test_grounding_pipeline` 8). 레인 미설치/모델 없음이면 신규 테스트는 skip(코어 무영향).

**포팅 충실도(실측 대조)**: 프로소디 — PoC와 PDQ·sd_semitone·조음속도·쉼 카운트 **자릿수까지 일치**
(에너지 delta만 ±0.2dB — librosa center-padding 차이, 추세 동일), 16s 윈도우 26~30ms.
비전 — kor.mp4 PoC와 blink/yaw_std/lip_motion 동일·smile ±0.003, 디코드 포함 53.6fps.

**★ inject 스파이크 PASS**(acceptance test "수치를 줘도 젬마가 따르는가" — 실 vLLM E4B,
`.dev/grounding/inject_spike.py` → `inject_spike.json`, 베이스라인=`.dev/kor_gemma_out.json`):

| 충돌(베이스라인) | 주입 후 |
|---|---|
| 🔴 nv "입술 떨림" 상투구 7개 윈도우 | **0개** (소멸) |
| 🔴 visual "제스처 활용 못함" 단정 3/3 | **3/3 "관측 불가"로 정정** (wrist_visible=0 차단 동작) |
| 🔴 ss=36 "긴장감" ↔ 클립 최대 미소 | **engaged/미소 유지로 정정** |
| 🟡 nv intensity 13개 중 11개 0.6 고정 | 0.3/0.6 분포로 분화 |
| 🔴 vocal "볼륨 작아짐" 역상관 2건 | **2/2 "일관된 음량"으로 정정** (0-16s는 실측 −1.05dB 방향과 일치하는 "미세 감소"로) |
| 🔴 vocal "빠르지만"↔"일정" 자기모순 | **해소** ("적절한 속도") |
| 🔴 vocal "단조로운 톤" 3/3 상투구 | **1/3 잔존**(16-32s에서 주입 수치 4.89st를 무시하고 "단조" 반복 — emit된 raw로 검출 가능 = 설계의 모델-실패 플래깅 경로) |
| 🟢 "호흡 짧음"(유일하게 그라운딩됐던 주장) | 유지(올바름 — 수치 기반 짧은 휴지 언급) |

## 발견 / 한계

1. **수치-번역 경향**: 주입 후 nv note가 "시선 이탈 평균 0.2xx" 식으로 수치를 인용하는 경향
   (비전 README의 열린 질문 그대로). 수치 위에 해석을 더하는 윈도우도 있으나, nv 채널을
   수치+템플릿으로 대체하는 과격 대안은 여전히 검토 가치 있음(비용·안정성).
2. **단조 상투구 1/3 잔존**: 프롬프트 규칙으로도 16-32s에서 수치 모순 출력. emit 덕에 소비자가
   `objective_vocal.pitch.sd_semitone`로 기계 검출 가능 — 후속에서 모순 자동 플래깅 고려.
3. **silero-VAD 미포함(의도)**: PoC의 교차검증용이었고 쉼은 Praat 강도 경로가 커버 —
   torch/onnxruntime 의존을 컨테이너에 안 싣는 선택. 필요 시 추후.
4. **aiortc 경로(consume_video) dense 미연결**: 통합 타깃은 LiveKit 경로라 거기만 배선.
5. **컨테이너(giljob-docker) 미반영**: PR #8 머지 후 별도 작업 — extras 설치
   (`pip install .[vision,prosody]`) + 모델 .task 2개 다운로드 + env 3종. parselmouth GPL-3 검토.
6. 스파이크 후 vLLM 컨테이너 stop·VRAM 해제 확인(GPU 위생). `tests/evidence/critic-live.raw.json`은
   라이브 게이트 테스트 재실행으로 재생성됨(무회귀 1 passed).

## 재현

```bash
pytest                       # 오프라인 137 (레인 테스트는 의존성/모델 게이트)
# inject 스파이크(실 vLLM): bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh 후
PYTHONPATH=src .venv/bin/python .dev/grounding/inject_spike.py
docker stop vllm-gemma4-e4b  # GPU 위생
```
