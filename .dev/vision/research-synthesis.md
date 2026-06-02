# 비전 그라운딩 방법론 리서치 종합 (2026-05-31 ~ 06-02)

> 6개 병렬 리서치(표정/AU/시선/통합툴킷 4채널 + rPPG/각성·포즈·마이크로표정 확장 2채널)의 압축 종합.
> 각 후보의 verdict와 근거. 출처는 각 절 하단. **결론만 필요하면 README.md의 결정 표를 보라.**

## 전제 조건 (이 프로젝트 기준)

- 입력: WebRTC 웹캠 30fps (정면 착석, 발화 중인 면접자)
- cadence: 젬마 비언어 read는 3s 그리드 — 단 보조 레인은 이에 묶이지 않음 (협력 ≠ 벤치마크)
- 자원: CPU 여유 + RTX 4090 × 2 유휴 — 단, 검증 결과 GPU는 불필요했음
- 라이선스: 비상업이라 제약 없음 (사용자 확인)
- 평가 기준 2개: **① 실시간성 ② 젬마 보조 충분성** (사용자 지정)

## 채택된 것

### MediaPipe Face Landmarker ★ v1 채택
- 478 랜드마크 + **52 ARKit 블렌드셰이프** + head-pose 행렬, 한 모델·한 추론. Apache-2.0, `pip install mediapipe`.
- 실측: **CPU 14.2ms/frame = 70fps (2.4x)**, 얼굴 검출 96%.
- 면접 관련 블렌드셰이프: `mouthSmileL/R`(미소) `cheekSquintL/R`(진짜미소 근사) `browDown/InnerUp`(눈썹)
  `eyeBlinkL/R`(깜빡임) `eyeLook*`(시선방향) `mouthPress`(입술눌림) `jawOpen`(발화).
- 한계: 블렌드셰이프는 FACS와 "느슨한 대응"(아바타 애니메이션용) → 추세/상대값으로 사용, 절대 캘리브레이션 아님.
- 출처: https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker

### MediaPipe Pose (+Hand) Landmarker ★ v1 채택
- 33 신체 키포인트(+ per-landmark **visibility**) / 21×2 손 키포인트. Apache-2.0, CPU 실시간.
- 실측: pose+hand 26.4ms = 37.9fps. **face+pose 2-스레드 병렬 = 17.4ms = 57.6fps (p95 24.8ms < 33ms 예산)**.
- 제스처 메트릭은 키포인트에서 직접 산출(기성 라이브러리 없음): 손목 속도 피크 = 제스처 이벤트,
  어깨중점 분산 = 자세 안정성, **visibility = 관측가능성 게이팅**(이번 검증에서 가장 가치 있던 신호).
- ②각성/표정과 달리 **기하학적으로 객관**(운동학) → Barrett 문제 없음.
- 출처: https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker

## 탈락한 것 (근거 포함 — 재논의 시 이 근거를 먼저 반박할 것)

### Py-Feat (FACS AU) — 실측 후 탈락
- 기대: 캘리브레이션된 AU(AU6+12=진짜미소, AU4/14/23/24=긴장 후보)가 블렌드셰이프보다 정밀할 것.
- 실측(.dev/vision/pyfeat_compare.json): **GPU(4090)에서 607ms/frame = 1.65fps** → dense 불가.
  미소 판정은 MediaPipe와 거의 완전 상관(추가 정보 0). "긴장" AU는 발화 조음에 지배되어 무용.
  유일한 add는 AU06 Duchenne 구분 — marginal.
- 비용 메모: py-feat 0.6.2는 **Python 3.11 + torch 2.2.2/tv 0.17.2 + scipy<1.14 + 단일 GPU 고정** 필요(의존성 지옥).
- 출처: https://github.com/cosanlab/py-feat

### rPPG (원격 광용적맥파 → 심박/HRV) — 리서치 단계 기각
"긴장의 자율신경 기반을 직접 잰다"는 발상은 매력적이나 **3중 벽**:
1. **발화가 신호 파괴**: 말하면 SNR 7.85→1.06dB, HR 오차 3배 (면접자는 계속 말함 = 최악 구간).
2. **시간 요구**: HR ~10s, HRV(스트레스 지표)는 16~30s+, 주파수영역은 분 단위 — 3s cadence와 양립 불가.
3. **교란 분리 불가**: HR 상승 = 발화 + 인지부하 + 각성. 말하는 것만으로 HR/BP 상승(감정 무관).
→ "노이즈를 생리학으로 둔갑"시키는 역효과. 각성이 필요하면 **음성 prosody**(오디오 레인)가 정도.
- 출처: RobustPPG https://pmc.ncbi.nlm.nih.gov/articles/PMC9664884/ · HRV 윈도우 https://pmc.ncbi.nlm.nih.gov/articles/PMC10376629/ · 발화-HR https://pubmed.ncbi.nlm.nih.gov/2463975/

### 마이크로표정 spotting — 리서치 단계 기각
- 100~200fps 필요(우리 30fps), spotting F1 0.01~0.36(대부분 누락/오탐), 레포는 미유지보수 연구코드.
- 거짓 신호를 제조해 그라운딩을 오히려 오염시킴.
- 출처: MEGC2020/2021 결과 · https://github.com/muse1998/MMNet

### OpenFace / LibreFace / InsightFace / EmoNet / 전용 gaze (L2CS 등)
- OpenFace 2.0: C++ 빌드 지옥 + 시선 벡터는 유일 강점이나, MediaPipe 거친 시선도 젬마 오판을 이미 잡음 → 우선순위 낮음.
- EmoNet(valence/arousal 최강): CC-BY-NC-ND — 비상업이라 가능하나 아래 "각성" 결론으로 무의미.
- L2CS-Net/yakhyo gaze: 시선 정밀도가 필요해지면 1순위 재고 대상 (MIT, GPU 실시간).

### 연속 valence-AROUSAL (HSEmotion VA 등) — 조건부 보류
- arousal 축이 "긴장"에 가장 가깝지만 **Barrett-2019 문제 상속**: 얼굴 기반 각성 추정 ≠ 내부 상태
  (전문가 평정 κ=0.27 ≈ 우연, self-report와 R²=0.03).
- 쓴다면 HSEmotion `enet_b0_8_va_mtl`(Apache, ONNX, 실시간) — 단 "저신뢰 활성 추정"으로 헤지하고
  윈도우 trend/분산만. **v1에서는 뺀다.**
- 출처: https://github.com/sb-ai-lab/EmotiEffLib · Barrett 2019 https://journals.sagepub.com/doi/10.1177/1529100619832930

## ★ 메타 결론 1: "긴장"은 어떤 비전 모달리티로도 그라운딩 불가

AU(MP·Py-Feat 실측) · rPPG(리서치) · 각성추정(리서치) 전부 "긴장/nervousness"에 깨끗한 객관 근거를 못 준다.
이건 모델 선택 문제가 아니라 사실이다. 올바른 설계 대응:
- (a) 객관적인 것만 그라운딩: 미소·시선·깜빡임·머리/자세 안정성·**관측가능성**
- (b) 근거 없는 "긴장" 단정을 시스템이 **차단** (프롬프트 수정 또는 emit 단계 불일치 플래그)
- (c) 각성이 정말 필요하면 vision이 아니라 **음성 prosody**로

## ★ 메타 결론 2: 협력 ≠ 벤치마크 (입력 정책)

보조 레인을 메인 모델 cadence(1fps)로 굶기면 존재 이유가 사라진다(깜빡임·입술모션 등 시계열 신호 전멸).
보조 레인은 **네이티브 fps 풀가동**이 기본. 메모 `collaboration-not-benchmark-input-policy` 참조.
같은 패턴의 두 번째 실수: 후보를 "얼굴"로 조기 협소화 → 신호-갭(자세/제스처) 기준으로 펼치자 Pose가 최고 가치였음.
**해법 공간은 갭 기준으로 먼저 열거할 것.**

## Barrett 2019 프레이밍 (emit 설계 시 필수 반영)

범주 감정 인식(FER)은 과학적으로 계쟁 중("특정 표정→특정 감정"의 신뢰 매핑은 없음).
객관 레인은 젬마보다 "더 옳은 oracle"이 아니라 **에러 특성이 다른 두 번째 계기**다.
→ emit은 "표정-움직임 feature + confidence"로, 감정 라벨 단정 없이.
→ 융합은 일치/불일치 플래그로, 덮어쓰기 없이 (다운스트림이 가중치 판단).
