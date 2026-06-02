# 오디오(프로소디) 그라운딩 탐색 (보조 세션 "audio", 2026-06-02~)

> **진행 상태: 리서치 단계 완료 / PoC 미착수.** 작업 흐름은 `.dev/vision/`(완료된 비전 탐색 세션)을
> 템플릿으로 따른다. 지속하는 에이전트는 이 README → `research-synthesis.md` 순으로 읽으면 충분하다.
> 메인 트랙 진입점은 `.dev/handoffs/NEXT.md` — 이 폴더는 그것을 대체하지 않는다(별개 보조 탐색).

## 한 줄 요약 (리서치 + PoC 결과)

젬마 vocal 채널의 의심을 교차검증할 **객관 프로소디 레인**을 4채널 병렬 리서치 + kor.mp4 PoC로 검증했다.
**채택: parselmouth(F0+음절핵) + silero-VAD + librosa RMS, 전부 CPU·전사 비의존(16s 윈도우 26.8ms).**
무거운 GPU SER(wav2vec2/emotion2vec) 전부 탈락. PoC 실측 결과 **젬마 vocal 주장 4유형 중 3개(단조·속도모순·
볼륨하락)가 미근거 상투구/자기모순/역상관, 1개(짧은 호흡)만 그라운딩** — 비전 충돌 패턴이 오디오에서 재현됨.
**"단조로움·속도·볼륨·쉼"은 그라운딩 가능, "긴장·자신감·열정"은 어떤 음향으로도 불가**(비전 "긴장 불가"와 동형).
상세 → `conflict-analysis.md`(충돌 전수) · `research-synthesis.md`(방법론).

## 확정된 결정 (검증 근거 포함)

| 결정 | 근거 |
|------|------|
| **parselmouth 채택** (F0 → PDQ·F0std semitone) | "단조로운 톤"(젬마 3/3 상투구) 그라운딩. Praat C라 ms급 CPU. 한국어 AP/declination detrend 필요 |
| **parselmouth 음절핵 채택** (De Jong-Wempe, 전사 불요) | speech/articulation rate(음절/초) 한 패스 → "빠름↔일정" 자기모순 숫자 반증. r≈0.8 검증 |
| **silero-VAD 채택** (MIT, CPU<1ms/chunk) | 쉼 횟수·길이 분포 → "호흡 짧음" 검증. 임계 250ms juncture 대조 |
| **librosa RMS 컨투어 채택** (ISC) | "볼륨 작아짐"을 *어디서* 줄었는지 localize. ★상대 dB만(AGC 교란 플래그) |
| **jitter/shimmer/HNR→긴장 기각** | 음량·모음·마이크·내용에 교란, 생리적 각성만 추적, 각성≠긴장 → 3 출처 교차 |
| **openSMILE eGeMAPS 기각**(의존성), 어휘만 채택 | functionals=집계라 컨투어 localize 불가 + openSMILE 상용금지 라이선스 절벽 |
| **GPU SER 전부 기각(v1)** | acted→spontaneous 비전이(자발 ~40%), 해석오류 재범, 각성은 CPU로 이미 잡힘 |
| **★ "긴장/자신감/열정"은 그라운딩 대상에서 제외** | 어떤 음향으로도 객관 근거 없음. 레인 역할은 *긴장 판정*이 아니라 *근거 없는 긴장 단정 차단* |

## 핵심 발견: 수치 ↔ 젬마 vocal 충돌 (PoC 실측, 상세 `conflict-analysis.md`)

| # | 젬마 주장 | 실측 | 판정 |
|---|---|---|---|
| 🔴1 | **"단조로운 톤"** 3/3 윈도우 동일문구 | PDQ 0.26→0.30→0.31(engaging 상한 0.235 초과), F0sd 4.3~5.1 st(단조<2 st), 옥타브오류<2.3%. PDQ가 오히려 *증가* | 근거없는 상투구 (비전 "입술 떨림"판) |
| 🔴2 | **"빠르지만"(16-32s) ↔ "일정"(타)** | 조음속도 6.48/6.08/6.08 음절/초 — "빠름" 지목 윈도우가 가장 느린 축. 셋 다 한국어 정상(5.8~6.9) | 자기모순 미근거 (비전 미소판) |
| 🔴3 | **"볼륨 작아짐"/"힘 빠짐"**(16-32·21-37) | 에너지 half→half +0.36/+0.45dB(상승). 약하락(−1.27)은 "유지"라던 0-16s에 — 부호 역전 | 역상관 (비전 "제스처 못함"판) |
| 🟢4 | **"호흡이 다소 짧아"**(16-32·21-37) | 쉼≥0.25s 0개, 어절휴지 median 0.11~0.14s(<250ms norm), silero speech_ratio~1.0 | **그라운딩됨**(유일하게 맞음) |

CPU 레이턴시 16s 윈도우 26.8ms(p95 31.7ms, 598x 여유) → 평가기준 ① 충족. ② 충족(충돌 3건 적발 + 시계열 신호 추가).
**"긴장/자신감/열정"은 그라운딩 대상 아님** — 레인 역할은 *판정*이 아니라 *근거 없는 단정 차단*.

> **아래는 원본 차터(2026-06-02 작성) — 리서치 전 계획. 흐름/평가기준은 유효, 후보 verdict·충돌은 위 표·synthesis·conflict-analysis가 정본.**

---

## (원본 차터) 무엇을 / 왜

젬마(E4B)의 **vocal 채널**(volume/pace/pauses/intonation 평가)도 비전 채널과 같은 불안정이 의심된다.
비전 탐색에서 시각 주장(미소/떨림/시선/제스처)이 객관 수치와 충돌 7건을 냈듯, vocal 주장도
**객관 프로소디 수치**(F0·에너지·발화속도·쉼)와 대조 검증이 필요하다.

추가 동기: 비전 리서치의 결론 중 하나가 **"각성(arousal)이 필요하면 vision이 아니라 음성 prosody가
정답"**(rPPG 기각 사유, `.dev/vision/research-synthesis.md`)이었다. 그 가설도 여기서 검증한다.

## 검증 대상 — 젬마 vocal 주장 (충돌 후보)

`.dev/kor_gemma_out.json`의 eval 3개 윈도우에서 추출. **비전과 같은 패턴이 이미 의심됨:**

| 젬마 주장 | 등장 | 의심 패턴 | 검증할 객관 수치 |
|---|---|---|---|
| **"단조로운 톤"** (열정/자신감 전달 안 됨) | **3/3 윈도우, 거의 동일 문구** | ★ 상투구 (비전의 "입술 떨림" 패턴) | F0(피치) 표준편차·범위 — 진짜 단조로운지 |
| **"전반적으로 빠르지만"** ↔ "일정한 속도 유지" | 16-32s는 "빠름", 0-16s·21-37s는 "일정" | ★ 자기모순 (비전의 미소 패턴) | 발화속도(음절/초) — STT 타임스탬프 또는 onset 기반 |
| "볼륨이 미세하게 작아지는 경향" / "힘이 빠지는" | 2/3 | 검증 가능한 구체 주장 | RMS 에너지 컨투어 — 실제로 줄어드는지·어느 구간인지 |
| "문장 사이 호흡이 다소 짧아" | 2/3 | 검증 가능한 구체 주장 | 쉼(pause) 검출 — VAD로 쉼 횟수/길이 분포 |

## 흐름 (비전 세션 템플릿 그대로)

1. **리서치** (`/researcher`, 채널 병렬 fan-out): 후보를 **신호-갭 기준으로** 열거 — 도구가 아니라 갭부터.
   - 피치/억양: F0 추정 (Praat/parselmouth, pyworld, librosa.pyin, CREPE)
   - 에너지/볼륨: RMS, loudness (librosa, openSMILE)
   - 발화속도: 음절/초 (STT 정렬, forced alignment, onset 검출)
   - 쉼/호흡: VAD (silero-VAD, webrtcvad), 쉼 길이 분포
   - 음질/긴장 후보: jitter/shimmer/HNR (Praat 계열) — ★ "음질이 긴장을 나타낸다"는 주장은
     비판적으로 검증할 것 (비전의 "긴장 그라운딩 불가" 결론이 오디오에도 적용되는지가 핵심 질문)
   - 통합 feature set: openSMILE eGeMAPS (정동 컴퓨팅 표준 88-feature)
2. **PoC 검증** (kor.mp4 오디오 → 객관 수치 → 젬마 주장과 윈도우 정렬 대조)
3. **충돌 분석** (`conflict-analysis.md` — 비전과 동일 형식)
4. **설계 제안** — 비전의 **"inject AND emit"** 설계를 오디오로 확장:
   프로소디 수치를 젬마 eval 프롬프트에 주입 + record에 raw 보존. (비전 README의 필수 조건 2개 동일 적용)

## 평가 기준 (사용자 지정, 비전과 동일)

1. **실시간성** — 16kHz mono PCM 스트림을 3s/16s cadence에 맞춰 처리 가능한가 (CPU 우선)
2. **젬마 보조 충분성** — 젬마 vocal 주장의 환각/상투구/자기모순을 잡아내고, 젬마가 못 내는 신호를 더하는가

## 입력 / 비교 정답지

- **`kor.mp4`** (repo 루트, gitignore) — 오디오 추출: `ffmpeg -i kor.mp4 -vn -ar 16000 -ac 1 -f s16le ...`
  (파이프라인 규격과 동일: 16kHz mono Int16 PCM — `src/giljobe/media/ingest.py` 참조)
- **`.dev/kor_gemma_out.json`** — 젬마 vocal 주장 (위 표의 원문)
- 파이프라인 현황: 메인 트랙 Slice 7 완료 시점 기준, 오디오는 이미 16k mono PCM으로 윈도워에 흐름
  (aiortc 경로 + LiveKit 경로 모두) → 프로소디 레인은 같은 PCM을 소비하면 됨

## 비전 세션에서 가져올 교훈 (반복 금지)

1. **협력 ≠ 벤치마크** (메모 `collaboration-not-benchmark-input-policy`): 프로소디 레인을 젬마 cadence로
   굶기지 말 것 — 16kHz 풀 샘플레이트로 분석하고, 젬마가 못 내는 시계열 신호(피치 컨투어, 쉼 분포)를 뽑는다.
2. **후보는 갭 기준으로 열거** — 첫 도구(librosa 등)에 앵커링하지 말 것. 위 리서치 갭 목록에서 출발.
3. **"긴장" 비판적 검증** — jitter/shimmer→긴장 주장은 문헌이 있으나, 비전에서 모든 모달리티가 실패했다.
   발화 스타일/내용과 분리 가능한지 적대적으로 확인할 것.
4. **무거운 모델 회의주의** — 비전에서 GPU 모델 전부 탈락했다. 오디오도 CPU 신호처리(Praat/librosa)가
   충분할 가능성이 높다. GPU 모델(wav2vec 감정인식 등)은 CPU 베이스라인이 부족할 때만.

## 파일 지도

```
.dev/audio/
├── README.md                  ★ 이 파일 (진입점) — 결정 표 + 충돌 표 + 파일 지도
├── research-synthesis.md      방법론 리서치 종합 (4채널, 후보별 verdict + 출처)
├── conflict-analysis.md       수치↔젬마 vocal 충돌 전수 분석 (PoC 실측)
├── prosody_grounding_poc.py   PoC 스크립트 (parselmouth+silero+librosa, 경로 상대)
├── prosody_grounding_poc.json PoC 결과 (윈도우별 수치 + 젬마 vocal 주장 병기 + 레이턴시)
└── kor_16k_mono.pcm           (gitignore) kor.mp4 추출 오디오 — 없으면 아래 재현법
```

폴더 밖 의존물:
- **`kor.mp4`** (repo 루트, gitignore) — 테스트 클립. 한국어 자기소개 38.27s, 여성 화자. 없으면 PoC 재현 불가.
- **`.dev/kor_gemma_out.json`** — 젬마 풀 추론(eval 3윈도우 vocal). PoC의 **비교 정답지**(메인 트랙 산출물).

## 재현 방법

```bash
# 오디오 추출 (파이프라인 규격 16k mono Int16 PCM)
ffmpeg -y -i kor.mp4 -vn -ar 16000 -ac 1 -f s16le .dev/audio/kor_16k_mono.pcm
# PoC 실행 (메인 .venv — parselmouth/librosa/silero-vad 설치됨, GPU/vLLM 불요)
cd .dev/audio && ../../.venv/bin/python prosody_grounding_poc.py
```

## 환경 변경 사항 (이 세션이 남긴 것)

- 메인 `.venv`에 **praat-parselmouth 0.4.7 · librosa 0.11.0 · soundfile · silero-vad(+onnxruntime, torch)** 설치됨.
  (비전이 mediapipe 추가한 것과 동형 — CPU-only, GPU/vLLM 불요.)
- `.dev/audio/kor_16k_mono.pcm` 생성(gitignore 대상 — 아래 gitignore 갱신 필요).

## 다음 단계 (작업 지속 시)

1. **inject-and-emit 스파이크**(acceptance test): PoC 4수치를 젬마 vocal 프롬프트에 주입 + "수치 근거 없는
   vocal 묘사·긴장/자신감 단정 금지" 규칙 → 재추론, 충돌 3건이 사라지는지 측정. (GPU/vLLM 필요. `conflict-analysis.md` 末.)
2. **단일 클립 한계 보강**: 진짜 단조/빠른/긴쉼 화자 클립으로 *반대방향* 검증 + 실 WebRTC AGC 경로 에너지 재검증.
3. **PLAN.md 슬라이스 편입**: `analysis/prosody.py`(가칭) — 16k PCM 윈도우 → 4신호 집계. dense 레인 분리는
   비전 `analysis/grounding.py`와 동형 패턴(NEXT.md 갱신은 메인 트랙 소유).
4. **통합 타깃**: giljob-docker `services/analysis-engine`(LiveKit subscribe 입력) — parselmouth GPL 주의
   (배포 바이너리 permissive 요구 시 pyworld+librosa fallback), 메모 `giljob-docker-integration-target`.

## 관련 문서 / 메모

- 메모 `vision-nonverbal-instability` — 문제의 발단 + 비전 결과; 이 오디오 세션이 같은 패턴을 음성에서 확인.
- 메모 `collaboration-not-benchmark-input-policy` — 보조 레인을 메인 cadence로 굶기지 말 것(풀 16k 분석).
- `.dev/vision/README.md` — 템플릿이 된 비전 세션(설계 "inject AND emit" 포함).
