# 비전 그라운딩 탐색 (보조 세션 "vision", 2026-05-31 ~ 06-02)

> **다른 에이전트를 위한 진입점.** 이 폴더는 메인 슬라이스 트랙과 **별개의 보조 탐색 세션** 산출물이다.
> 메인 트랙 진입점은 `.dev/handoffs/NEXT.md` — 이 폴더는 그것을 대체하지 않는다.
> 작업 지속 시 이 README → `conflict-analysis.md` → `research-synthesis.md` 순으로 읽으면 충분하다.

## 한 줄 요약

젬마(E4B) 비언어 채널의 불안정(상투구·자기모순·환각, 메모 `vision-nonverbal-instability`)을 교차검증하는
**객관 비전 레인**을 탐색·검증했다. **결론: MediaPipe Face + Pose (CPU, 2-스레드 병렬) 채택.**
무거운 GPU 모델(Py-Feat/OpenFace/rPPG/마이크로표정)은 전부 비용 대비 가치 없음으로 탈락.

## 확정된 결정 (검증 근거 포함)

| 결정 | 근거 |
|------|------|
| **MediaPipe Face Landmarker 채택** (52 블렌드셰이프 + head-pose) | CPU 70fps(2.4x), 미소 신호 시각검증 일치, 젬마 환각(떨림·긴장·시선) 정량 적발 → `vision_grounding_poc.json` |
| **MediaPipe Pose Landmarker 채택** (33 키포인트 + visibility) | CPU 합산 37.9fps, "제스처 없음" 주장이 근거 없음을 폭로(손이 프레임 밖 96%) → `pose_grounding_poc.json` |
| **레인 구성: Face·Pose 각자 스레드** | 순차(1스레드)는 p95 43.9ms로 예산(33ms) 초과, 병렬(2스레드)은 57.6fps·p95 24.8ms로 여유 → `combined_latency.json` |
| **Hand Landmarker는 옵션** (손 보이는 셋업에서만) | 이 클립은 손 가시성 2% — hand 모델은 낭비. 셋 다 순차로 돌리면 26fps로 미달 |
| **Py-Feat(FACS AU) 탈락** | GPU에서 1.65fps(42배 느림), 미소 판정은 MediaPipe와 동률, "긴장" AU 못 잡음 → `pyfeat_compare.json` |
| **rPPG(심박) 기각** | 발화가 신호 파괴 + HRV는 16~30s+ 필요 + 발화/인지/긴장 분리 불가 → `research-synthesis.md` |
| **마이크로표정 기각** | 100~200fps 필요(우리 30fps), spotting F1 0.01~0.36, 미유지보수 연구코드 |
| **★ "긴장"은 그라운딩 대상에서 제외** | AU·rPPG·각성추정 어느 모달리티로도 객관 근거 없음. 비전 레인의 역할은 긴장 *판정*이 아니라 근거 없는 긴장 *단정 차단* |

## 핵심 발견: 수치 ↔ 젬마 충돌 7건

상세 → **`conflict-analysis.md`**. 요약:

1. 🔴 ss=36: 젬마 "긴장감" ↔ 클립 최대의 진짜(Duchenne) 미소 (MP 0.52·100%, Py-Feat AU12 0.97/happy 1.00, 시각확인)
2. 🔴 "입술 떨림" 7개 윈도우: 라벨과 실측 입술모션이 **역상관** (찍힌 그룹 0.00228 < 안 찍힌 그룹 0.00262)
3. 🔴 "제스처를 활용하지 못한다": 손이 프레임에 없음(가시성 2%) — 관측 불가를 결함으로 단정
4. 🟠 시선 "안정/불안정" 라벨이 객관 수치와 역방향 (ss=9·15·18)
5. 🟠 미소 자기모순: eval "무표정" ↔ nv "미소" ↔ 진실은 "간헐적 미소"(피크 0.84~0.89)
6. 🟡 ss=3 "표정 변화 없음" ↔ 프레임 56%에서 미소
7. 🟡 nv intensity가 13개 중 11개에서 0.6 고정 = 측정이 아닌 디폴트

## 파일 지도

```
.dev/vision/
├── README.md                    ★ 이 파일 (진입점)
├── conflict-analysis.md         수치↔젬마 충돌 7건 전수 분석
├── research-synthesis.md        방법론 리서치 종합 (6채널, 후보별 verdict + 출처)
├── vision_grounding_poc.py      ① Face PoC (CPU) — 미소/눈썹/깜빡임/시선/머리/입술모션
├── vision_grounding_poc.json    ① 결과 (윈도우별 수치 + 젬마 note 병기)
├── pose_grounding_poc.py        ② Pose+Hand PoC (CPU) — 자세안정/손목가시성/제스처
├── pose_grounding_poc.json      ② 결과
├── pyfeat_compare.py            ③ Py-Feat 비교 (GPU) — FACS AU vs MediaPipe
├── pyfeat_compare.json          ③ 결과
├── combined_latency.py          ④ Face+Pose 동시 실행 레이턴시 (3구성)
├── combined_latency.json        ④ 결과
├── frames/                      (gitignore) 시각검증 스틸 3장 + Py-Feat 입력 38장
└── models/                      (gitignore) MediaPipe .task 3개 (face/pose/hand)
```

폴더 밖 의존물:
- **`kor.mp4`** (repo 루트, gitignore) — 테스트 클립. 한국어 자기소개 38.2s/30fps/1080x1920. 없으면 PoC 재현 불가.
- **`.dev/kor_gemma_out.json`** — 젬마 풀 추론(13윈도우 nv + 3 eval). 모든 PoC의 **비교 정답지** (메인 트랙 산출물).
- **`.dev/.venv-vision/`** (gitignore) — Py-Feat 전용 venv. **이동 금지**(절대경로 박힘).

## 재현 방법

```bash
# ①②④ MediaPipe 계열 (메인 .venv — mediapipe 0.10.35 설치됨, GPU/vLLM 불요)
.venv/bin/python .dev/vision/vision_grounding_poc.py
.venv/bin/python .dev/vision/pose_grounding_poc.py
.venv/bin/python .dev/vision/combined_latency.py

# ③ Py-Feat (전용 venv, GPU 1장 고정 필수)
CUDA_VISIBLE_DEVICES=0 .dev/.venv-vision/bin/python .dev/vision/pyfeat_compare.py
```

모델 번들이 없으면(클린 체크아웃):
```bash
mkdir -p .dev/vision/models && cd .dev/vision/models
curl -sSLO https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
curl -sSL -o pose_landmarker.task https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task
curl -sSLO https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task
```

`.venv-vision` 재구성이 필요하면(파괴됐을 때만): **Python 3.11** + `pip install setuptools wheel`
→ `pip install py-feat` → `pip install torch==2.2.2 torchvision==0.17.2 scipy==1.11.4` (버전 고정 필수 — 0.6.2 호환).

## 환경 변경 사항 (이 세션이 남긴 것)

- 메인 `.venv`에 **mediapipe 0.10.35** 추가 설치됨 (+ opencv-contrib, numpy 2.4.6 유지).
- `.dev/.venv-vision/` 생성 (Python 3.11 + py-feat 0.6.2 + torch 2.2.2 cu121). gitignore 됨.
- `.gitignore`: `/.dev/models/` → `/.dev/vision/models/` 경로 갱신, `/.dev/vision/frames/` 추가.

## ★ 설계 제안: "inject AND emit" (2026-06-02 사용자·클로드 합의)

**핵심 아이디어(사용자 제안):** 3초 비전 윈도우에서 젬마에게 **1fps 프레임 3장 + MediaPipe 윈도우 집계
수치를 함께** 넘겨, 이미지와 수치를 같이 보고 윈도우 추론을 시킨다.

**왜 옳은가:**
- 분업이 정확함 — 측정(레인) vs 해석(젬마). 젬마의 실패는 "측정 못 하는데 측정한 척"에서 왔음.
- 시간적 실명 보완 — 깜빡임 횟수·미소 min/max/지속%·움직임 분산은 1fps 프레임이 못 담는 정보.
- 충돌 7건을 생성 시점에서 공격 — `wrist_visible=0.02`가 프롬프트에 있으면 "제스처 활용 못함" 평가가 나오기 어려움.
- 보너스: **세션 baseline 대비 delta**도 주입 가능 → stateless 한계(메모)까지 해결.

**필수 조건 2개 (이거 없으면 하지 말 것):**
1. **inject AND emit** — 수치를 젬마 안으로만 넣으면 검증 가능성이 사라짐(젬마가 수치를 무시해도 알 길 없음).
   수치는 record에 **raw로도 보존**: 젬마 read = 수치 보고 생성된 것 / raw 수치 = 여전히 ground truth.
   젬마가 주입된 수치와 모순되면 → 그 자체를 모델 실패 신호로 플래깅.
2. **프롬프트 차단 규칙** — "수치에 근거 없는 관찰 기술 금지 / 수치와 모순되는 묘사 금지" 명시.
   수치는 raw 블렌드셰이프가 아니라 **집계 + 한국어 라벨**("미소: 강함, 프레임 72%") 형태로.
   단 "긴장" 같은 해석 라벨은 미리 붙이지 않는다.

**열린 질문 → 착수 시 첫 스파이크:** "수치를 줘도 젬마가 따르는가?"
- **Acceptance test가 이미 준비돼 있음**: kor.mp4 + `conflict-analysis.md`의 충돌 7건.
  수치 주입 버전으로 재추론 → 충돌이 몇 건 사라지는지 센다.
- 사라짐 → 설계 검증 완료. 안 사라짐(수치 무시) → 더 과격한 대안 검토:
  **per-window 시각 read에서 젬마 제외**, 수치만 emit (젬마는 verbal/심층 eval 전담).
- 같은 스파이크에서 확인: 젬마 read가 수치의 한국어 번역에 불과한가, 수치 위에 뭔가를 더하는가?
  전자면 nv 채널은 수치+템플릿이 더 싸고 안정적.

**자잘한 리스크 (해결책 있음):** 토큰 비용 수백 토큰(무시 가능) · 타이밍은 MediaPipe 17ms/frame이라
윈도우 닫히면 즉시 집계 가능 · 수치 자체가 틀리는 경우는 `face_detect_rate` 동반 주입으로 게이팅.

## 다음 단계 (작업 지속 시)

1. **↑ 수치 주입 스파이크 먼저** (위 acceptance test — GPU/vLLM 필요, 충돌 7건 재측정).
2. **PLAN.md에 슬라이스로 편입** (착수 조건은 메인 트랙과 조율 — NEXT.md 갱신은 메인 트랙 소유):
   - `analysis/grounding.py`(가칭): MediaPipe Face+Pose 래퍼. **30fps dense 레인을 3s 그리드에서 분리**
     (Slice 4에서 stt 레인을 분리한 것과 동형 패턴). face·pose 각자 스레드(executor).
   - 윈도우별 집계 → ① 젬마 nv 프롬프트에 주입(inject) + ② `objective_nonverbal` 필드로 emit.
   - 관측 불가(`wrist_visible≈0`)는 해당 평가 차단 신호로.
3. **단일 클립 한계 보강**: 다른 조명/즉흥발화/실 WebRTC 압축 경로 클립으로 재검증. 손 보이는 클립으로 제스처 정량화 실검증.
4. **통합 타겟 고려**: 최종 배치는 giljob-docker `services/analysis-engine` (LiveKit subscribe 입력) — 메모
   `giljob-docker-integration-target` 참조. MediaPipe는 CPU-only라 컨테이너 추가 부담 적음(alpine 호환은 확인 필요).

## 관련 문서 / 메모

- 메모 `vision-nonverbal-instability` — 문제의 발단(Slice 3) + 이 세션 결과 반영됨
- 메모 `collaboration-not-benchmark-input-policy` — 이 세션의 방법론 교훈(보조 레인을 메인 cadence로 굶기지 말 것)
- `PLAN.md` §5(윈도잉/동시성) — dense 레인 분리가 따라야 할 기존 패턴
- `.dev/handoffs/NEXT.md` — 메인 트랙 현황(이 세션 시점 Slice 7 완료: emit/sink·ingest 존재)
