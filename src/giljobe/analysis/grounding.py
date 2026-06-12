"""비전 그라운딩 dense 레인 — MediaPipe Face+Pose를 풀 프레임레이트로 돌려 윈도우 집계를 낸다.

`.dev/vision/` 탐색 세션의 확정 결정 포팅(vision_grounding_poc.py·pose_grounding_poc.py →
프로덕션): **MediaPipe Face Landmarker(52 블렌드셰이프+head-pose) + Pose Landmarker(33 키포인트
+visibility), face·pose 각자 단일 스레드**(순차 1스레드는 p95 43.9ms로 33ms 예산 초과, 병렬
2스레드는 57.6fps — `.dev/vision/combined_latency.json`). Hand Landmarker는 옵션 모델 —
모델 파일이 있으면 셋째 레인으로 가동해 손가락 셈(펼친 손가락 수 안정 구간)을 그라운딩한다.

★ 협력 ≠ 벤치마크(메모 `collaboration-not-benchmark-input-policy`): 이 레인은 젬마 cadence(3s,
1fps)로 굶기지 않고 **네이티브 fps로 풀가동**해, 젬마가 구조적으로 못 내는 시계열 신호(깜빡임
횟수·미소 지속%·움직임 분산·손목 가시성)를 측정한다. 윈도워의 3s 그리드(1fps JPEG 버퍼)와
분리된 dense 피드(`add_frame`) — Slice 4에서 stt 레인을 분리한 것과 동형 패턴.

레인의 역할은 *판정*이 아니라 *근거 없는 단정 차단*: "긴장"은 어떤 비전 수치로도 그라운딩
불가(.dev/vision/README.md 결정 표) — 해석 라벨 없이 기하학적 사실만 집계한다. 관측 불가
(`wrist_visible≈0`)는 해당 평가의 차단 신호로 쓴다(describe_visual).

동시성: add_frame은 이벤트루프에서 불리므로 **submit만**(비차단). 검출은 레인 전용 단일 스레드
워커에서(MediaPipe VIDEO 모드는 검출기당 단조 증가 timestamp 요구 → 레인당 1스레드 필수).
백로그가 max_backlog를 넘으면 프레임을 버린다(load-shedding — 실시간성이 완전성보다 우선).
reset()은 epoch 가드로 이전 턴의 in-flight 결과가 새 턴 집계에 새는 것을 막는다.

의존성: mediapipe는 optional extra(`[vision]`) — 모듈 import는 가볍게, 무거운 import는
VisionGrounder 안에서만. 모델 .task 2개는 env `GILJOBE_VISION_MODELS_DIR`로 지정한다.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

FACE_MODEL = "face_landmarker.task"
POSE_MODEL = "pose_landmarker.task"
HAND_MODEL = "hand_landmarker.task"
BLINK_TH = 0.5      # avg(eyeBlinkL,R) 상향 교차 = 깜빡임 1회(PoC와 동일)
SMILE_TH = 0.2      # smile_pct 기준(PoC와 동일)
VIS_TH = 0.5        # pose visibility 임계(프레임 안/밖)
GESTURE_VEL = 0.15  # 어깨폭 15%/frame 이상 손목 변위 = 뚜렷한 손동작(PoC와 동일)
WRIST_OBSERVABLE = 0.2  # 이 미만이면 손이 화면 밖 — 제스처 평가 차단 신호
FINGER_RADIAL = 1.25    # 손목→끝/손목→PIP 반경비 — 펴짐 1.4대·접힘 0.6~0.7·가림 환각 1.1(벤치 실측)
THUMB_AWAY = 0.75       # 엄지끝→중지MCP/손바닥길이 — 접힘(손바닥 가로지름) 0.55~0.67·폄 0.83+(벤치 실측)
FINGER_SEG_MIN_S = 0.4  # 동일 카운트 안정 구간 하한 — 미만은 셈 전이 노이즈로 버린다
# Pose 33 랜드마크 인덱스
_NOSE, _LSH, _RSH, _LWR, _RWR = 0, 11, 12, 15, 16
# Hand 21 랜드마크 인덱스: 네 손가락 (PIP, TIP) + 엄지 끝·중지 MCP(손바닥 스케일 기준점)
_FINGERS = ((6, 8), (10, 12), (14, 16), (18, 20))
_THUMB_TIP, _MIDDLE_MCP = 4, 9


def maybe_vision_grounder(models_dir: str | Path | None = None) -> "VisionGrounder | None":
    """비전 레인 팩토리 — 의존성/모델이 없거나 env로 껐으면 None(레인 비활성, 파이프라인 무영향).
    env: GILJOBE_VISION=off 명시 비활성 / GILJOBE_VISION_MODELS_DIR=모델(.task) 디렉토리."""
    if os.environ.get("GILJOBE_VISION", "auto").lower() == "off":
        logger.info("비전 레인 비활성(GILJOBE_VISION=off)")
        return None
    d = Path(models_dir or os.environ.get("GILJOBE_VISION_MODELS_DIR", ""))
    if not models_dir and not os.environ.get("GILJOBE_VISION_MODELS_DIR"):
        logger.info("비전 레인 비활성(GILJOBE_VISION_MODELS_DIR 미설정)")
        return None
    face, pose, hand = d / FACE_MODEL, d / POSE_MODEL, d / HAND_MODEL
    if not (face.is_file() and pose.is_file()):
        logger.warning("비전 레인 비활성(모델 없음: %s, %s)", face, pose)
        return None
    if not hand.is_file():
        logger.info("손 레인 없이 가동(모델 없음: %s) — face/pose만", hand)
    try:
        return VisionGrounder(face, pose, hand_model=hand if hand.is_file() else None)
    except Exception:  # noqa: BLE001 - 레인 기동 실패가 서비스를 죽이면 안 됨(비활성으로 강등)
        logger.exception("비전 레인 비활성(VisionGrounder 생성 실패)")
        return None


class VisionGrounder:
    """dense 비전 레인 — add_frame(비차단 submit) → 워커 검출 → window_metrics(start,end) 집계.

    수명은 한 구독자 세션과 같다(윈도워가 reset/close를 함께 구동). MediaPipe VIDEO 모드의
    timestamp 단조 요구 때문에 reset은 검출기를 재생성하지 않고 ts 베이스를 전진시킨다."""

    def __init__(self, face_model: str | Path, pose_model: str | Path, *,
                 hand_model: str | Path | None = None, max_backlog: int = 30) -> None:
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        self._face = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=str(face_model)),
            running_mode=vision.RunningMode.VIDEO, num_faces=1,
            output_face_blendshapes=True, output_facial_transformation_matrixes=True,
        ))
        self._pose = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=str(pose_model)),
            running_mode=vision.RunningMode.VIDEO, num_poses=1,
        ))
        self._hand = None                    # 옵션 셋째 레인 — 모델 없으면 face/pose만(기존 동작)
        if hand_model is not None:
            self._hand = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
                base_options=python.BaseOptions(model_asset_path=str(hand_model)),
                running_mode=vision.RunningMode.VIDEO, num_hands=2,
            ))
        self.max_backlog = max_backlog
        # 레인당 워커 1 고정 — VIDEO 모드 검출기는 단조 timestamp를 요구해 동시 호출 불가.
        self._face_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gje-face")
        self._pose_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gje-pose")
        self._hand_exec = (ThreadPoolExecutor(max_workers=1, thread_name_prefix="gje-hand")
                           if self._hand else None)
        self._rows_lock = threading.Lock()   # 결과 행 + epoch 보호
        self._sub_lock = threading.Lock()    # pending 카운터(load-shedding) 보호
        self._face_rows: list[dict] = []
        self._pose_rows: list[dict] = []
        self._hand_rows: list[dict] = []
        self._epoch = 0
        self._pending_face = 0
        self._pending_pose = 0
        self._pending_hand = 0
        self._shed = 0                       # 백로그로 버린 프레임 수(집계에 노출 — silent cap 방지)
        self._ts_base_ms = 0                 # reset 시 전진 — MediaPipe ts 단조 유지
        self._face_last_ms = 0               # face 워커 전용(단일 스레드)
        self._pose_last_ms = 0               # pose 워커 전용(단일 스레드)
        self._hand_last_ms = 0               # hand 워커 전용(단일 스레드)
        self._closed = False

    # ── 입력 (이벤트루프에서 호출 — submit만, 비차단) ──
    def add_frame(self, t_s: float, frame) -> None:
        """frame: rtc.VideoFrame 덕타입(.data/.width/.height, RGB24) 또는 (H,W,3) uint8 ndarray.
        백로그 초과 시 드롭(load-shedding) — 검출이 밀리면 완성도보다 실시간성을 지킨다."""
        if self._closed:
            return
        with self._sub_lock:
            if (self._pending_face >= self.max_backlog or self._pending_pose >= self.max_backlog
                    or self._pending_hand >= self.max_backlog):
                self._shed += 1
                return
            self._pending_face += 1
            self._pending_pose += 1
            if self._hand_exec:
                self._pending_hand += 1
            epoch = self._epoch
        self._face_exec.submit(self._detect_face, epoch, t_s, frame)
        self._pose_exec.submit(self._detect_pose, epoch, t_s, frame)
        if self._hand_exec:
            self._hand_exec.submit(self._detect_hand, epoch, t_s, frame)

    # ── 워커 (레인당 단일 스레드 — 예외는 로깅하고 삼킨다: 한 프레임 실패가 레인을 죽이지 않게) ──
    def _detect_face(self, epoch: int, t_s: float, frame) -> None:
        try:
            ms = self._next_ms("_face_last_ms", t_s)
            res = self._face.detect_for_video(self._mp_image(frame), ms)
            row = _face_row(t_s, res)
        except Exception:  # noqa: BLE001
            logger.exception("face detect 실패 t=%.2f(이 프레임만 누락)", t_s)
            return
        finally:
            with self._sub_lock:
                self._pending_face -= 1
        self._append(self._face_rows, epoch, row)

    def _detect_pose(self, epoch: int, t_s: float, frame) -> None:
        try:
            ms = self._next_ms("_pose_last_ms", t_s)
            res = self._pose.detect_for_video(self._mp_image(frame), ms)
            row = _pose_row(t_s, res)
        except Exception:  # noqa: BLE001
            logger.exception("pose detect 실패 t=%.2f(이 프레임만 누락)", t_s)
            return
        finally:
            with self._sub_lock:
                self._pending_pose -= 1
        self._append(self._pose_rows, epoch, row)

    def _detect_hand(self, epoch: int, t_s: float, frame) -> None:
        try:
            ms = self._next_ms("_hand_last_ms", t_s)
            res = self._hand.detect_for_video(self._mp_image(frame), ms)
            row = _hand_row(t_s, res)
        except Exception:  # noqa: BLE001
            logger.exception("hand detect 실패 t=%.2f(이 프레임만 누락)", t_s)
            return
        finally:
            with self._sub_lock:
                self._pending_hand -= 1
        self._append(self._hand_rows, epoch, row)

    def _next_ms(self, attr: str, t_s: float) -> int:
        """검출기별 단조 증가 ms timestamp(VIDEO 모드 요구). 워커 전용 상태라 lock 불요."""
        ms = max(self._ts_base_ms + int(round(t_s * 1000.0)), getattr(self, attr) + 1)
        setattr(self, attr, ms)
        return ms

    def _mp_image(self, frame):
        import mediapipe as mp

        if isinstance(frame, np.ndarray):
            rgb = frame
        else:  # rtc.VideoFrame 덕타입 — 변환은 워커에서(이벤트루프 비용 0)
            from giljobe.media.ingest import rgb_array

            rgb = rgb_array(frame.data, frame.width, frame.height)
        return mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))

    def _append(self, rows: list[dict], epoch: int, row: dict) -> None:
        with self._rows_lock:
            if epoch == self._epoch:  # reset 후 도착한 이전 턴 in-flight 결과는 버린다
                rows.append(row)

    # ── 집계 ──
    def window_metrics(self, start_s: float, end_s: float) -> dict | None:
        """[start,end) 구간의 윈도우 집계. 행이 하나도 없으면 None(주입/emit 생략 신호).
        in-flight 1~2 프레임은 기다리지 않는다 — 90행 집계에서 꼬리 프레임 누락은 무시 가능."""
        with self._rows_lock:
            face = [r for r in self._face_rows if start_s <= r["t"] < end_s]
            pose = [r for r in self._pose_rows if start_s <= r["t"] < end_s]
            hand = [r for r in self._hand_rows if start_s <= r["t"] < end_s]
            shed = self._shed
        if not face and not pose and not hand:
            return None
        out = {**aggregate_face(face), **aggregate_pose(pose)}
        if self._hand_exec:  # 손 레인 비활성이면 키 자체를 내지 않는다(기존 shape 보존)
            out.update(aggregate_hands(hand))
        if shed:
            out["shed_frames_total"] = shed
        return out

    def wait_idle(self, timeout: float | None = None) -> None:
        """제출된 검출이 모두 끝날 때까지 대기(테스트/측정용 배리어 — 레인당 단일 스레드 FIFO)."""
        if self._closed:
            return
        barriers = [self._face_exec.submit(lambda: None), self._pose_exec.submit(lambda: None)]
        if self._hand_exec:
            barriers.append(self._hand_exec.submit(lambda: None))
        wait(barriers, timeout=timeout)

    # ── 수명 ──
    def reset(self) -> None:
        """턴 시작 — 행/카운터 초기화 + epoch 전진(이전 턴 in-flight 결과 차단) + ts 베이스 전진
        (t가 0으로 되돌아가도 MediaPipe timestamp는 단조 유지)."""
        with self._rows_lock:
            self._face_rows = []
            self._pose_rows = []
            self._hand_rows = []
            self._epoch += 1
        with self._sub_lock:
            self._shed = 0
        self._ts_base_ms = max(self._face_last_ms, self._pose_last_ms, self._hand_last_ms) + 1000

    def close(self) -> None:
        """새 입력 차단 후 레인 종료. 검출기 close는 각 레인 큐의 마지막 잡으로 제출해(단일 스레드
        FIFO) in-flight 검출과 경합하지 않게 한다. shutdown(wait=False)라 호출자는 블록되지 않는다."""
        self._closed = True
        self._face_exec.submit(self._face.close)
        self._pose_exec.submit(self._pose.close)
        self._face_exec.shutdown(wait=False)
        self._pose_exec.shutdown(wait=False)
        if self._hand_exec:
            self._hand_exec.submit(self._hand.close)
            self._hand_exec.shutdown(wait=False)


# ── per-frame 행 추출 (워커에서 호출 — 집계에 필요한 필드만 남겨 행을 작게) ──
def _face_row(t_s: float, res) -> dict:
    if not res.face_blendshapes:
        return {"t": t_s, "face": False}
    bs = {c.category_name: c.score for c in res.face_blendshapes[0]}
    yaw, pitch, _roll = _euler_deg(np.array(res.facial_transformation_matrixes[0]))
    lmk = res.face_landmarks[0]
    return {
        "t": t_s,
        "face": True,
        "smile": (bs.get("mouthSmileLeft", 0.0) + bs.get("mouthSmileRight", 0.0)) / 2,
        "brow_down": (bs.get("browDownLeft", 0.0) + bs.get("browDownRight", 0.0)) / 2,
        "press": (bs.get("mouthPressLeft", 0.0) + bs.get("mouthPressRight", 0.0)) / 2,
        "blink": (bs.get("eyeBlinkLeft", 0.0) + bs.get("eyeBlinkRight", 0.0)) / 2,
        # 시선 이탈 프록시(PoC와 동일): 눈동자 이동 블렌드셰이프 최대값
        "gaze_off": max(
            bs.get("eyeLookOutLeft", 0.0), bs.get("eyeLookOutRight", 0.0),
            bs.get("eyeLookInLeft", 0.0), bs.get("eyeLookInRight", 0.0),
            bs.get("eyeLookUpLeft", 0.0), bs.get("eyeLookDownLeft", 0.0),
        ),
        "yaw": yaw,
        "pitch": pitch,
        # 입술 모션("떨림" 상투구 검증) 프록시 랜드마크: 입꼬리(61,291)·윗입술(13)·아랫입술(14)
        "lips": [(lmk[i].x, lmk[i].y) for i in (61, 291, 13, 14)],
    }


def _pose_row(t_s: float, res) -> dict:
    if not res.pose_landmarks:
        return {"t": t_s, "pose": False}
    lm = res.pose_landmarks[0]
    return {
        "t": t_s,
        "pose": True,
        "wrist_vis": max(lm[_LWR].visibility, lm[_RWR].visibility),
        "sh_mid": ((lm[_LSH].x + lm[_RSH].x) / 2, (lm[_LSH].y + lm[_RSH].y) / 2),
        "sh_w": math.dist((lm[_LSH].x, lm[_LSH].y), (lm[_RSH].x, lm[_RSH].y)),  # 스케일 정규화용
        "nose": (lm[_NOSE].x, lm[_NOSE].y),
        "wr": {
            "l": (lm[_LWR].x, lm[_LWR].y, lm[_LWR].visibility),
            "r": (lm[_RWR].x, lm[_RWR].y, lm[_RWR].visibility),
        },
    }


def _hand_row(t_s: float, res) -> dict:
    if not res.hand_world_landmarks:
        return {"t": t_s, "hands": 0}
    # world 좌표(m) 필수 — 정규화 2D는 카메라 쪽 접힘이 단축돼 일직선으로 보인다(프로브 증거)
    fingers = sum(count_extended_fingers([(p.x, p.y, p.z) for p in lm])
                  for lm in res.hand_world_landmarks)
    return {"t": t_s, "hands": len(res.hand_world_landmarks), "fingers": fingers}


def count_extended_fingers(pts: list[tuple[float, ...]]) -> int:
    """Hand 21 랜드마크(3D world 권장) → 펴진 손가락 수.

    관절각 기준은 기각 — 가려진 손가락은 MediaPipe가 '펴짐' 기하로 환각해 각도가 통과한다
    (기준 영상 '3'의 접힌 검지: 체인 길이는 뭉개졌는데 관절각 160~180도 — hands_probe.py).
    네 손가락은 손목 반경비(접히면 끝이 PIP 안쪽으로 들어온다), 엄지는 손바닥을 가로지르는
    접힘 특성상 끝→중지MCP 거리로 판정. 임계는 기준 영상 3-2-5 실측 분리값(상수 주석)."""
    wrist = pts[0]
    n = sum(1 for pip, tip in _FINGERS
            if math.dist(wrist, pts[tip]) > math.dist(wrist, pts[pip]) * FINGER_RADIAL)
    palm = math.dist(wrist, pts[_MIDDLE_MCP])
    if palm > 1e-9 and math.dist(pts[_THUMB_TIP], pts[_MIDDLE_MCP]) > palm * THUMB_AWAY:
        n += 1
    return n


def _angle_deg(v: tuple[float, ...], a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """꼭짓점 v에서 a·b로 향하는 두 벡터의 사잇각(도) — 2D/3D 무관."""
    av = [ai - vi for ai, vi in zip(a, v)]
    bv = [bi - vi for bi, vi in zip(b, v)]
    den = math.sqrt(sum(x * x for x in av)) * math.sqrt(sum(x * x for x in bv))
    if den < 1e-9:
        return 0.0
    dot = sum(x * y for x, y in zip(av, bv))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot / den))))


def _euler_deg(mat: np.ndarray) -> tuple[float, float, float]:
    """4x4 변환행렬 회전부 → (yaw, pitch, roll) 도. 그라운딩엔 절대각보다 변동성(std)이 중요."""
    r = mat[:3, :3]
    sy = math.sqrt(r[0, 0] ** 2 + r[1, 0] ** 2)
    pitch = math.degrees(math.atan2(-r[2, 0], sy))
    yaw = math.degrees(math.atan2(r[1, 0], r[0, 0]))
    roll = math.degrees(math.atan2(r[2, 1], r[2, 2]))
    return yaw, pitch, roll


# ── 윈도우 집계 (순수 함수 — mediapipe 불요, 오프라인 테스트 직격) ──
def aggregate_face(rows: list[dict]) -> dict:
    """face 행 → 윈도우 집계. 검출 0이면 커버리지만(face_seen_ratio가 수치 신뢰 게이트 —
    .dev/vision/README.md "face_detect_rate 동반 주입으로 게이팅")."""
    if not rows:
        return {"face_frames": 0}
    seen = [r for r in rows if r["face"]]
    out = {"face_frames": len(rows), "face_seen_ratio": round(len(seen) / len(rows), 2)}
    if not seen:
        return out
    smile = np.array([r["smile"] for r in seen])
    out.update({
        "smile_mean": round(float(smile.mean()), 3),
        "smile_max": round(float(smile.max()), 3),
        "smile_ratio": round(float((smile > SMILE_TH).mean()), 2),
        "brow_down_mean": round(float(np.mean([r["brow_down"] for r in seen])), 3),
        "mouth_press_mean": round(float(np.mean([r["press"] for r in seen])), 3),
        "blink_count": _count_blinks(seen),
        "gaze_off_mean": round(float(np.mean([r["gaze_off"] for r in seen])), 3),
        "yaw_std_deg": round(float(np.std([r["yaw"] for r in seen])), 2),
        "pitch_std_deg": round(float(np.std([r["pitch"] for r in seen])), 2),
        "lip_motion": round(_lip_velocity(seen), 5),
    })
    return out


def aggregate_pose(rows: list[dict]) -> dict:
    """pose 행 → 윈도우 집계. 위치는 어깨폭으로 정규화(카메라 거리 무관). wrist_visible이
    낮으면 제스처 수치는 '관측 불가'를 뜻한다(없음으로 단정 금지 — PoC 충돌 #3)."""
    if not rows:
        return {"pose_frames": 0}
    seen = [r for r in rows if r["pose"]]
    out = {"pose_frames": len(rows), "pose_seen_ratio": round(len(seen) / len(rows), 2)}
    if len(seen) < 2:
        return out
    sw = float(np.mean([r["sh_w"] for r in seen])) or 1e-6
    sh_mid = np.array([r["sh_mid"] for r in seen])
    nose = np.array([r["nose"] for r in seen])
    wrist_motion, gesture_events = _wrist_motion(seen, sw)
    out.update({
        "posture_std": round(float(np.mean(np.std(sh_mid, axis=0))) / sw, 4),
        "head_sway": round(float(np.mean(np.std(nose, axis=0))) / sw, 4),
        "wrist_visible": round(float(np.mean([r["wrist_vis"] > VIS_TH for r in seen])), 2),
        "wrist_motion": round(wrist_motion, 4),
        "gesture_events": gesture_events,
    })
    return out


def aggregate_hands(rows: list[dict]) -> dict:
    """hand 행 → 윈도우 집계. 손가락 카운트는 안정 구간(FINGER_SEG_MIN_S 이상 동일 카운트)으로
    붕괴해 전이 프레임을 버린다 — '3→2→5' 같은 셈 제스처가 사실 시퀀스로 남는다."""
    if not rows:
        return {"hand_frames": 0}
    seen = [r for r in rows if r["hands"]]
    out = {"hand_frames": len(rows), "hand_seen_ratio": round(len(seen) / len(rows), 2)}
    if not seen:
        return out
    segs = _finger_segments(seen)
    if segs:
        out["finger_segments"] = segs
        # 시퀀스는 셈 제스처(비0)만 — 주먹/내림(0)은 segments에 사실로 남는다
        out["finger_sequence"] = [s["count"] for s in segs if s["count"]]
    return out


def _finger_segments(seen: list[dict], min_dur_s: float = FINGER_SEG_MIN_S) -> list[dict]:
    """중위수 스무딩(±2행) 후 동일 카운트 연속 구간으로 붕괴 — 짧은 전이는 버리고, 전이로
    끊겼던 같은 카운트는 재결합한다(검출 플리커가 시퀀스를 쪼개지 않게)."""
    counts = [r["fingers"] for r in seen]
    sm = [int(np.median(counts[max(0, i - 2):i + 3])) for i in range(len(counts))]
    segs: list[dict] = []
    start = 0
    for i in range(1, len(sm) + 1):
        if i < len(sm) and sm[i] == sm[start]:
            continue
        if seen[i - 1]["t"] - seen[start]["t"] >= min_dur_s:
            if segs and segs[-1]["count"] == sm[start]:
                segs[-1]["end_s"] = round(seen[i - 1]["t"], 2)
            else:
                segs.append({"count": sm[start], "start_s": round(seen[start]["t"], 2),
                             "end_s": round(seen[i - 1]["t"], 2)})
        start = i
    return segs


def _count_blinks(rows: list[dict]) -> int:
    """avg(eyeBlinkL,R)이 임계(0.5)를 상향 교차한 횟수 — 1fps 프레임으론 구조적으로 못 세는
    시계열 신호(이 레인의 존재 이유)."""
    blinks = 0
    prev = 0.0
    for r in rows:
        if prev < BLINK_TH <= r["blink"]:
            blinks += 1
        prev = r["blink"]
    return blinks


def _lip_velocity(rows: list[dict]) -> float:
    """프레임간 입술 랜드마크 평균 이동량(정규화 좌표) — "입술 떨림" 상투구의 검증 수치."""
    if len(rows) < 2:
        return 0.0
    vels = []
    for a, b in zip(rows[:-1], rows[1:]):
        d = [math.dist(pa, pb) for pa, pb in zip(a["lips"], b["lips"])]
        vels.append(sum(d) / len(d))
    return float(np.mean(vels))


def _wrist_motion(rows: list[dict], sw: float) -> tuple[float, int]:
    """손목이 보이는 프레임간 변위 평균(어깨폭 정규화) + 제스처 이벤트 수(속도 피크)."""
    vels = []
    for a, b in zip(rows[:-1], rows[1:]):
        ds = []
        for k in ("l", "r"):
            (xa, ya, va), (xb, yb, vb) = a["wr"][k], b["wr"][k]
            if va > VIS_TH and vb > VIS_TH:
                ds.append(math.dist((xa, ya), (xb, yb)))
        if ds:
            vels.append(max(ds) / sw)
    if not vels:
        return 0.0, 0
    return float(np.mean(vels)), sum(1 for v in vels if v > GESTURE_VEL)


def describe_visual(m: dict) -> str:
    """비전 집계 → 프롬프트 주입용 한국어 라벨 라인. 관측 불가는 명시해 해당 평가를 차단한다
    (해석 라벨 없음 — 측정은 레인, 해석은 모델의 분업)."""
    lines: list[str] = []
    if m.get("face_frames", 0) and m.get("face_seen_ratio", 0) > 0:
        lines.append(
            f"- 미소: 평균 {m['smile_mean']}/최대 {m['smile_max']}, "
            f"프레임 {int(m['smile_ratio'] * 100)}%에서 미소(>0.2)"
        )
        lines.append(
            f"- 깜빡임 {m['blink_count']}회, 시선 이탈 평균 {m['gaze_off_mean']}, "
            f"머리 움직임 yaw/pitch 표준편차 {m['yaw_std_deg']}/{m['pitch_std_deg']}도"
        )
        lines.append(f"- 입술 움직임(프레임간 평균 변위) {m['lip_motion']}")
        if m["face_seen_ratio"] < 0.5:
            lines.append(f"- ★얼굴 검출률 {int(m['face_seen_ratio'] * 100)}% — 위 수치 신뢰 낮음")
    elif "face_frames" in m:
        lines.append("- 얼굴 미검출 — 표정/시선 평가 근거 없음(단정 금지)")
    if m.get("pose_frames", 0) and "posture_std" in m:
        lines.append(
            f"- 자세: 어깨중점 흔들림 {m['posture_std']}(어깨폭 정규화), 머리 sway {m['head_sway']}"
        )
        if m.get("wrist_visible", 0.0) < WRIST_OBSERVABLE:
            lines.append(
                f"- ★손목이 화면에 거의 안 보임(프레임 {int(m['wrist_visible'] * 100)}%) — "
                "손동작/제스처는 관측 불가이므로 평가하지 말 것(없다고 단정 금지)"
            )
        else:
            lines.append(
                f"- 손동작: 손목 변위 평균 {m['wrist_motion']}(어깨폭/프레임), "
                f"뚜렷한 제스처 {m['gesture_events']}회"
            )
    if m.get("finger_sequence"):
        seq = "→".join(str(c) for c in m["finger_sequence"])
        spans = ", ".join(f"{s['count']}개 {s['start_s']}~{s['end_s']}초"
                          for s in m.get("finger_segments", []) if s["count"])
        lines.append(
            f"- 손가락 펼침(안정 구간): {seq}" + (f" ({spans})" if spans else "")
            + f" — 손 검출률 {int(m.get('hand_seen_ratio', 0) * 100)}%"
        )
    return "\n".join(lines)
