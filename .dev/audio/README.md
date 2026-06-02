# 오디오(프로소디) 그라운딩 탐색 — 차터 (미착수, 2026-06-02 작성)

> **이 폴더는 아직 차터(시작점)만 있다.** 작업 흐름은 `.dev/vision/`(완료된 비전 탐색 세션)을
> 템플릿으로 따른다. 착수하는 에이전트는 **`.dev/vision/README.md`를 먼저 읽고** 같은 구조로 진행할 것.

## 무엇을 / 왜

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

## 산출물 규약 (비전과 동일)

```
.dev/audio/
├── README.md                  ★ 이 파일 — 작업 진행하며 결정 표/파일 지도로 갱신
├── research-synthesis.md      리서치 종합 (후보별 verdict + 출처)
├── prosody_grounding_poc.py   PoC 스크립트 (재현 가능하게, 경로는 상대)
├── prosody_grounding_poc.json PoC 결과 (윈도우별 수치 + 젬마 주장 병기)
├── conflict-analysis.md       수치↔젬마 충돌 전수 분석
└── (필요 시) models/ 등       무거운 바이너리는 gitignore + README에 재다운로드 명령
```

완료 시: 이 README를 비전 README처럼 **결정 표 + 파일 지도 + 재현법 + 다음 단계** 구조로 갱신하고,
메모 갱신(`vision-nonverbal-instability` 또는 신규 audio 메모) + 커밋·푸시.
