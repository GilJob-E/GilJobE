# 오디오(프로소디) 그라운딩 방법론 리서치 종합 (2026-06-02)

> 4개 병렬 리서치(① F0/음질 ② 발화속도/쉼 ③ 에너지/통합셋 ④ 프로소디→감정 적대검증)의 압축 종합.
> 각 후보의 verdict와 근거. 출처는 각 절 하단. **결론만 필요하면 README.md의 결정 표를 보라.**
> 비전 세션(`.dev/vision/research-synthesis.md`)과 동일 형식 — 비전 결론과의 대응을 명시한다.

## 전제 조건 (이 프로젝트 기준)

- 입력: **16kHz mono Int16 PCM** (aiortc·LiveKit 두 경로 모두 윈도워에 이 규격으로 흐름 — `media/ingest.py`).
- cadence: 젬마 vocal read는 eval 16s 그리드 — 단 보조 레인은 이에 묶이지 않음 (협력 ≠ 벤치마크, 메모 `collaboration-not-benchmark-input-policy`). 프로소디는 **풀 16k 샘플레이트**로 분석.
- 자원: CPU 여유 + GPU 유휴 — 단, 비전과 동일하게 **검증 결과 GPU는 불필요**(아래 SER 기각).
- 평가 기준 2개(사용자 지정): **① 실시간성**(3s/16s 윈도우 CPU 처리) **② 젬마 보조 충분성**(환각·상투구·자기모순 적발 + 젬마가 못 내는 시계열 신호 제공).
- ★ **파이프라인 현실 점검 (리서치 중 발견, 설계에 영향):** 현 transcriber는 **`GemmaNativeTranscriber`(텍스트만, 타임스탬프 없음)**. faster-whisper는 **미설치**(Slice 2에서 "별도 STT 안 붙임" 결정, VRAM=0 유지). → STT-정렬 기반 발화속도는 "공짜"가 아니라 faster-whisper를 **새로 세우는** 결정. 그래서 **무전사(transcript-free)** 방법을 1순위로 둔다.

## ★ 메타 결론 1: 프로소디는 "기술(descriptive)"은 그라운딩하나 "해석(interpretive)"은 못 한다 — 비전과 동형

비전이 "긴장은 어떤 시각 모달리티로도 그라운딩 불가"로 끝났듯, 오디오도 같은 단층선에서 갈린다.
적대검증 채널(④)이 문헌 다중 교차로 확정한 결론:

```
그라운딩 가능 (측정 사실 + confidence로 emit):
  F0 평균·변동성(단조로움) · 발화속도(음절/초) · 에너지/강도 컨투어 · 쉼 횟수·길이 분포
  → 이들은 AROUSAL(각성)을 추적하는 기술적 사실.

그라운딩 불가 (해석의 비약 — Barrett 역추론 오류):
  긴장/nervousness · 자신감 유무 · 열정/진정성-as-내부상태
  → 高각성 ≠ 긴장 (열정적인 사람도 불안한 사람도 둘 다 高각성, 음향으로 구분 불가)
  → 면접=발표 레지스터가 피치·속도를 감정과 무관하게 끌어올림 (read-vs-spontaneous 교란)
  → "자신감"은 청자의 PERCEPTION이지 측정된 내부 상태가 아님; valence/열정은 음향상 각성과 분리 불가
```

→ **설계 대응(비전과 동일):** 기술적 feature를 측정·emit(+confidence) / 젬마의 감정 단정은 **차단** / 라벨이 필요하면 *"청자에게 ~하게 들릴 수 있음"*(perceived) 형태로, 면접자 내부상태 진단으로 쓰지 않는다.

> **정정(중요):** Barrett 2019("Emotional Expressions Reconsidered")는 **얼굴** 논문이다 — vocal κ를 따로 제시하지 않는다. κ≈0.27·R²≈0.03은 표정 수치. Barrett의 *프레이밍*(신뢰성/특이성/일반화·역추론 오류)은 음성에 그대로 이전되나 *수치*는 프로소디 전용 문헌에서 가져와야 한다. (얼굴 κ를 음성에 도용하지 말 것.)

## ★ 메타 결론 2: 무거운 GPU SER는 v1에서 기각 — 비전과 동일

wav2vec2/HuBERT/emotion2vec 음성감정인식(SER)은 **REJECT(v1)**:
1. **자발 한국어 면접 발화에 미보정** — EMOVOME: acted RAVDESS 자기평가 ~73% → 자발발화 valence ~41–44%·arousal ~41–51%로 붕괴. 최고 300M 트랜스포머도 ~62%/56%.
2. **acted→spontaneous 비전이** — 교차코퍼스 −15~25pt, acted가 가장 어려운 방향(2+ 출처 교차확인). 한국어 자원(ETRI KEMDy/KESDy)도 대부분 **연기(acted)** → 동일 한계 상속.
3. **해석 오류 재범** — 우리가 "그라운딩 불가"로 결론낸 감정 라벨을 다른 블랙박스로 다시 뱉을 뿐.
4. **기술 레인과 중복** — 유일하게 실리는 차원(각성)은 이미 CPU에서 F0/강도/속도/쉼으로 싸게 잡힘.

비전의 "GPU 모델은 보정된 신호를 안 더해서 전부 탈락"과 같은 논리. → 무거운 모델은 CPU 베이스라인이 부족할 때만.

---

## 채택된 것 (CPU 신호처리 스택)

### ① F0/피치 — parselmouth (Praat 바인딩) ★ ADOPT — "단조로움" 그라운딩
- Praat C/C++을 그대로 래핑(자동상관 F0) → 초 단위 오디오를 ms급에 처리. F0 컨투어 + jitter/shimmer/HNR 한 객체.
- **"단조로운 톤"(젬마 3/3 윈도우 상투구)은 그라운딩 가능 — 핵심 산출물.** 측정 지표:
  - **PDQ = SD(F0)/mean(F0)** (Pitch Dynamism Quotient) — 발표/낭독의 "생동감" 검증용 문헌 지표(engaging speaker 0.11~0.235). **헤드라인 숫자.**
  - **F0 std (Hz 아님 semitone)** + **F0 range**. 세미톤=지각적으로 옳고(eGeMAPS 표준), Hz std는 화자 평균피치에 교란됨.
  - **임상 정의와 일치:** "monotone speech"는 *F0의 SD 감소*로 operationalize됨(실어/조현 문헌).
- **한국어 특이사항(지표에 영향):** 서울말은 비성조이나 **악센트구(AP) LH 템플릿 + 문장 declination(피치 하강)** 의무 → ⓐ 한국어는 *결코 완전 평탄하지 않음*(매 AP마다 상승) → 영어 기준 절대임계 금지, **한국어 baseline 대비 상대 변동성**으로. ⓑ declination이 16s 윈도우 F0 std를 인위 팽창 → **detrend(추세제거)** 후 측정.
- **실시간성:** Praat 내부 C라 trivial. **라이선스: GPL-3.0+** ⚠️ — 내부/서버측 분석 서비스(`services/analysis-engine`)면 무방, 배포 바이너리가 permissive 유지해야 하면 아래 fallback.
- 출처: parselmouth https://pypi.org/project/praat-parselmouth/ · 설치/라이선스 https://parselmouth.readthedocs.io/en/stable/installation.html · PDQ(Hincks) https://www.isca-archive.org/icall_2004/hincks04_icall.html · monotone=reduced SD F0 https://www.sciencedirect.com/science/article/abs/pii/S0920996418300276 · 한국어 AP/declination(Jun) https://linguistics.ucla.edu/people/jun/JUN-JKL14-FINAL.pdf

### ② 발화속도 — Praat 음절핵 검출 (De Jong & Wempe 2009, parselmouth) ★ ADOPT — "빠름↔일정" 그라운딩
- 강도 피크(전후 dip, 기본 mindip=2dB) + **유성성(voicing) 교차검증**으로 음절핵을 셈. **전사 불요(transcript-free).**
- 출력이 정확히 필요한 것: 음절수 · **# 쉼 · phonation time · 발화속도(speech rate, 쉼포함) · 조음속도(articulation rate, 쉼제외)** 한 패스에. → 젬마의 **"전반적으로 빠르지만" ↔ "일정한 속도 유지" 자기모순을 숫자로 반증**(인접 윈도우 음절/초 비교).
- 검증: De Jong & Wempe 2009(Behav Res Methods 41:385) — 인간↔자동 상관 高(r≈0.8). 언어독립(강도+유성성) → 한국어 안전. 파이썬 포트 `drfeinberg/PraatScripts/syllable_nuclei.py`(parselmouth) 그대로 사용.
- **한국어 norm(객관 라벨용):** 서울말 자발발화 조음속도 ~5.8–6.9 음절/초(성인), ~7–8.2(청년 남성). → <5 느림 / 6–7 보통 / >7.5 진짜 빠름.
- **실시간성:** 16k 3s/16s 강도+피치 = ms급 CPU. GPU 불요. 가장 가벼우면서 가장 완전.
- 한계: 잡음/과압축에 피크피킹 저하 — `mindip`/silence 임계 튜닝, kor.mp4로 검증 필수.
- 출처: De Jong & Wempe 2009 https://link.springer.com/article/10.3758/BRM.41.2.385 (PDF https://www.fon.hum.uva.nl/archive/2009/2009-brm-JongWempe.pdf) · 파이썬 포트 https://github.com/drfeinberg/PraatScripts/blob/master/syllable_nuclei.py · 한국어 조음속도 norm https://www.eksss.org/archive/view_article?pid=pss-10-4-19

### ③ 쉼/호흡 — silero-VAD ★ ADOPT — "호흡 짧음" 그라운딩
- **MIT**, ~2MB ONNX/JIT, **CPU <1ms/chunk**(RTF≈0.004), 16kHz 네이티브(고정 chunk=512 samples). GPU 불요.
- `get_speech_timestamps` → 발화 구간, **gap=쉼** → **쉼 횟수·평균/중앙값·길이 분포(히스토그램)** = 정확히 "문장 사이 호흡이 다소 짧아" 검증물.
- **쉼 길이 임계(문헌):** 0~250ms=조음적 무휴지(정상 juncture), >250ms=묵음휴지(250ms가 통상 운율경계 임계), 200~1000ms=주저/분절 휴지. → 절간 묵음 중앙값을 ~200–250ms 대역과 대조하면 "호흡 짧음"이 객관 검증됨.
- 보너스: ②의 Praat 스크립트가 쉼 횟수 baseline을 **공짜로** 줌 → silero는 *분포*까지 필요할 때 위에 얹음.
- 출처: silero-VAD https://github.com/snakers4/silero-vad · 쉼 임계 https://asmp-eurasipjournals.springeropen.com/articles/10.1186/s13636-016-0096-7

### ④ 에너지/볼륨 — librosa RMS 컨투어 ★ ADOPT — "볼륨 작아짐" 그라운딩
- `librosa.feature.rms` → **프레임별 RMS 컨투어**(젬마가 못 내는 시계열). → "에너지가 t=Xs에서 30%(~3dB) 하락, 최저점 t≈19s"처럼 *어디서* 줄었는지 emit. **ISC 라이선스**(permissive), 사실상 이미 의존.
- 16kHz는 hop/frame을 **명시(예: hop 10ms/frame 25–40ms)** — 2048/512 기본값은 ~22kHz 가정.
- dBFS는 `amplitude_to_db(ref=윈도우 피크 or 80th-pct)`로 **상대값만**. 추세 숫자는 baseline→trough dB 낙폭.
- ★ **교란(필수 반영):** 절대 볼륨은 무의미 — 마이크 게인·거리·**AGC(자동게인)** 가 좌우. WebRTC 기본 AGC=ON이 실제 위협(진짜 하락을 *가리거나* fast-release가 pumping 위양성). → **절대 볼륨 단정 금지, 윈도우/세션 내 상대 dynamics만**. 가능하면 트랙에서 `autoGainControl:false`, 아니면 출력 메타에 AGC 교란 플래그.
- 출처: librosa.rms https://librosa.org/doc/0.11.0/generated/librosa.feature.rms.html · AGC 교란 https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3842986/

---

## 탈락한 것 (근거 포함 — 재논의 시 이 근거를 먼저 반박할 것)

### 🔴 jitter/shimmer/HNR → "긴장(tension)" 신호 — REJECT (긴장 라벨로는 기각)
- 측정 자체는 parselmouth로 가능하나, **긴장-as-감정의 객관 근거가 못 됨** (3 출처 교차):
  - 체계적 리뷰(PLOS One/PMC12289014): 음질 feature는 37검사 중 18에서만 표적상태 검출(프로소디 52/69) — "대부분 jitter↔stress 무관, shimmer/HNR 일관추세 없음".
  - 다중패러다임 연구(Sci Rep/PMC10918109): 음성 feature는 *생리적 활성*이 동반된 MIST에선 변하나 기분만 바뀐 Cyberball에선 불변 → "음성 feature는 생리적 스트레스 반응에 관련될 뿐, 고립된 부정정서엔 아님".
- **교란 스택(라이브 면접=최악):** jitter/shimmer/HNR은 모두 **음량(SPL)에 따라 변함**(감정 무관) + 모음/내용 의존 + WebRTC 16k 마이크/코덱 perturbation 직접 유입. 통제 불가한 running speech라 *측정값=발화스타일+마이크+음량*이 지배. → "음질=긴장"은 객관성으로 포장한 새 환각.
- = 비전의 "긴장 그라운딩 불가"와 같은 결론. 최대치는 F0/강도를 *느슨한 각성*으로, **각성 ≠ 감정상태** 명시 caveat와 함께, 절대 "긴장"으로 라벨 안 함.
- 출처: AGAINST https://pmc.ncbi.nlm.nih.gov/articles/PMC12289014/ · https://pmc.ncbi.nlm.nih.gov/articles/PMC10918109/ · FOR(각성, 균형) https://ieeexplore.ieee.org/document/4218292/

### CREPE (딥 피치) — REJECT
- 절대 F0 정확도는 우위이나, 우리 지표는 **F0의 SD/range(2차 통계)** 라 프레임별 소오차에 강건 → GPU 모델 가치 0. parselmouth로 충분. (비전 "GPU 가치 없음" 패턴.)
- 출처: https://arxiv.org/abs/1802.06182

### openSMILE eGeMAPS (88 feature) — REJECT (의존성으로는 기각, *어휘*는 채택)
- **Functionals = 윈도우 전체 집계 통계**(평균/백분위/slope), **프레임별 시계열 아님** → "볼륨이 t=Xs에서 하락"을 localize 못 함(falling-slope·20/80 백분위로 *추세*는 corroborate하나 *최저점*은 못 짚음). 우리 핵심 필요와 어긋남.
- LLD 레벨로 컨투어를 뽑을 순 있으나 그 "Loudness"는 사실상 *무거운 RMS* → librosa로 공짜로 되는 걸 중복.
- **라이선스 절벽:** opensmile-python 래퍼는 permissive지만 번들된 openSMILE 바이너리는 audEERING "private/research/educational만, 상용제품 금지". 비상업인 지금은 OK지만 **제품화 순간 부채**. librosa(ISC)+pyloudnorm(MIT)+parselmouth(GPL)엔 이 절벽 없음.
- → **eGeMAPS는 라이브러리가 아니라 *사전*으로 채택:** feature 이름·정의(Loudness, F0, jitter, HNR, alpha ratio, Hammarberg, spectral slope…)를 우리 어휘로 재사용(librosa+parselmouth로 직접 계산), 컨투어를 emit. 정동컴퓨팅 표준과 정렬 + 라이선스 노출 0.
- 출처: opensmile-python https://audeering.github.io/opensmile-python/usage.html · 라이선스 https://www.audeering.com/research/opensmile/ · eGeMAPS(Eyben 2016) https://sail.usc.edu/publications/files/eyben-preprinttaffc-2015.pdf

### GPU SER (wav2vec2 AVD / emotion2vec / IEMOCAP) — REJECT (v1) — 메타결론 2 참조
- 출처: audeering valence-gap https://arxiv.org/abs/2203.07378 · EMOVOME(acted→spontaneous 정량) https://arxiv.org/html/2403.02167v3 · 교차코퍼스 https://arxiv.org/pdf/2207.02104

### Forced alignment (MFA/WhisperX/wav2vec2-CTC) — REJECT (오버킬)
- MFA는 한국어 지원하나 배치/오프라인·무거운 설치(Kaldi/conda), 실시간 3s 윈도우 도구 아님. WhisperX는 **한국어 기본 정렬기 없음**. 음절/초 스칼라엔 음소정렬 불요. (코퍼스 음성학용, 실시간 critic 신호용 아님.)
- 출처: WhisperX https://github.com/m-bain/whisperX · MFA(ko) https://montreal-forced-aligner.readthedocs.io/

### librosa onset / webrtcvad / RMS-임계 VAD / pyloudnorm — 조건부 (FALLBACK 또는 보조)
- **librosa onset**(발화속도): 음악 노트용, 자음 버스트에 오발화·유성성 게이트 없음 → 과다계수. De Jong–Wempe(유성성 필터)가 정확히 이걸 고친 버전 → ② 사용. (REJECT as primary, crude 교차검증만.)
- **webrtcvad**(쉼): 초경량 BSD지만 위양성 多 → silero가 약간 무겁고 훨씬 정확. (FALLBACK — onnx 의존 회피 필요할 때만.)
- **pyloudnorm LUFS**(에너지): MIT, NumPy/SciPy만. 단 **윈도우당 단일 스칼라**(시계열 아님) + BS.1770 gating이 조용한(=하락!) 블록을 버릴 수 있음 → localize 불가. (FALLBACK — 사람이 읽는 절대-ish 라벨 옵션만, 레인은 아님.)
- **STT-정렬 발화속도**(whisper 타임스탬프 + 한글 음절수): 한글 1블록=1음절이라 *영어보다 깨끗*하나, 현 파이프라인에 타임스탬프 없음(전제조건 참조) → faster-whisper를 *추가*할 때만 의미. 추가하면 거의 공짜이나 발화속도만 위해 추가는 오버킬. (FALLBACK, conditional — STT 강건성으로 faster-whisper를 이미 도입하면 그때 채택.)

---

## 권장 v1 스택 (PoC 착수 지점)

| 신호 | 도구 | 라이선스 | 젬마 주장 검증 |
|------|------|----------|----------------|
| 피치 변동성("단조로움") | **parselmouth** → PDQ·F0std(semitone)·range, declination detrend, 한국어 baseline | GPL-3.0 | "단조로운 톤" 3/3 상투구 |
| 발화속도("빠름↔일정") | **parselmouth 음절핵**(De Jong-Wempe) → speech/articulation rate(음절/초) | GPL-3.0 | "빠르지만"↔"일정" 자기모순 |
| 쉼/호흡("짧음") | **silero-VAD** → 쉼 횟수·길이 분포 (Praat 스크립트가 baseline 공짜) | MIT | "호흡이 다소 짧아" |
| 에너지/볼륨("작아짐") | **librosa RMS 컨투어** → 상대 dB 낙폭·최저점 ts (AGC 플래그) | ISC | "볼륨 작아지는 경향"/"힘 빠짐" |
| 음질→긴장 | **(없음 — 기각)** F0/강도를 느슨한 각성으로만, "긴장" 라벨 금지 | — | "긴장감"/"자신감 부족"/"열정" 차단 |

- 전부 **CPU-only, GPU/vLLM 비결합, 전사 비의존**(silero·librosa·parselmouth) → VRAM=0·단일모델 정책과 일치.
- GPL 우려 시(배포 바이너리 permissive 요구) F0 fallback: **pyworld(BSD) + librosa(ISC)** — jitter/shimmer는 어차피 기각이라 손실 없음.
- 설치: `pip install praat-parselmouth librosa silero-vad`(또는 silero는 torch.hub). 메인 `.venv`는 numpy만 있음 → PoC용 추가 설치 필요(비전이 mediapipe 추가한 것과 동형).

## 다음 단계 (PoC — 차터 흐름 2~4단계)

1. **PoC 검증** (`prosody_grounding_poc.py`): kor.mp4 오디오 추출(`ffmpeg -i kor.mp4 -vn -ar 16000 -ac 1 -f s16le`) → 위 4 신호를 젬마 윈도우(0-16s, 16-32s, 21-37s 및 nv 3s 그리드)에 정렬 → `kor_gemma_out.json` vocal 주장과 병기.
2. **충돌 분석** (`conflict-analysis.md`, 비전 형식): 특히 "단조로운 톤" 3/3가 PDQ 실측과 충돌하는지, "빠름↔일정"이 음절/초로 반증되는지.
3. **설계 제안 — "inject AND emit"** (비전 README §설계): 프로소디 수치를 젬마 vocal 프롬프트에 주입 + record에 raw 보존 + "수치 근거 없는 vocal 묘사 금지 / 긴장·자신감 단정 차단" 프롬프트 규칙.
4. 단일 클립 한계 보강(다른 화자/즉흥/실 WebRTC 압축 경로).
