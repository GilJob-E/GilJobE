"""윈도우 단위 native AV 면접 분석자 (D8 video_url+audio_url 경로).

윈도우(1fps JPEG 프레임 + 16kHz PCM)를 video_url+audio_url로 조립해 E4B에 보내고,
per-window 구조화 신호를 낸다 — 채널① 비언어 read, 채널② verbal/vocal/visual 비평.
집계는 하지 않는다(다운스트림 몫). vLLM은 이 비평자 뒤에 숨는다.

gje `native_eval.WindowEvaluator`를 giljobe로 포팅한 것. 비평 메서드(`read_nonverbal`/
`evaluate_window`)와 JSON 헬퍼는 **그대로** 유지하고, 전사는 critic이 직접 박지 않고
주입된 `Transcriber`(기본 `GemmaNativeTranscriber`, 같은 vLLM E4B → VRAM 추가 0)에
위임하는 `transcribe_window`만 새로 얹는다.
"""
from __future__ import annotations

import json
import re

from giljobe.llm.vllm_client import VllmClient, default_vllm_client
from giljobe.media.window_assembly import assemble_audio_url, assemble_video_url
from giljobe.models.signals import NONVERBAL_STATES, EvaluationSignal, NonVerbalSignal

from .grounding import describe_visual
from .prosody import describe_vocal
from .transcriber import GemmaNativeTranscriber, Transcriber

MODEL = "google/gemma-4-E4B-it"

NONVERBAL_SYSTEM = (
    "당신은 지원자의 비언어 신호를 예리하게 읽는 면접관입니다. 지금 이 짧은 구간(몇 초)의 "
    "영상+음성에서 지원자의 상태를 *있는 그대로* 판별하세요. 답변 내용·논리는 보지 말고 "
    "비언어(표정, 시선, 자세, 목소리 톤·떨림)만. "
    "중요: 무난해 보여도 안전하게 'engaged'로 때우지 마세요. 시선 이탈·깜빡임, 경직되거나 "
    "무너지는 자세, 망설임·말 끊김, 긴장·불안, 에너지/자신감의 *변화*를 적극적으로 포착해 "
    "구분하세요. intensity는 신호의 실제 강도를 눈금에 맞게 매기세요 — 습관적으로 0.8을 찍지 "
    "말 것. 약하거나 애매하면 0.2~0.4, 중간이면 0.5, 뚜렷할 때만 0.7+. "
    "오직 SINGLE JSON object로만 답하세요: "
    '{"state": one of ' + "[" + ", ".join(NONVERBAL_STATES) + "], "
    '"intensity": number 0.0-1.0, "note": 관찰한 구체 단서(한국어 짧게)}.'
)

EVAL_SYSTEM = (
    "당신은 까다롭고 안목 높은 면접관입니다. 지원자 답변의 ~16초 구간을 멀티모달(영상+음성)로 "
    "**비평**하세요. 칭찬을 나열하는 평가자가 아니라 *비평가*로서, 각 축마다 잘한 점이 있으면 "
    "짚되 **약점·어색함·우려·개선점을 반드시 구체적으로 지적**하세요. 무난하거나 부족하거나 "
    "평범하면 솔직히 그렇게 쓰세요. '안정적', '자연스러움' 같은 일반론적 호평으로 때우지 마세요. "
    "오직 SINGLE JSON object로만 답하세요(키): "
    '"verbal": {"logic": str, "structure": str, "specificity": str}, '
    '"vocal": {"volume": str, "pace": str, "pauses": str, "intonation": str}, '
    '"visual": {"eye_contact": str, "posture": str, "expression": str, "gesture_over_time": str}, '
    '"critique": [str, ...]  // 면접관으로서 이 구간에서 가장 걸리는 약점·우려 1~3개(필수, 비워두지 말 것), '
    '"key_observations": [str, ...]. 각 leaf는 한국어 짧은 비평.'
)

# end-of-turn tail용 — 답변 *마지막 구간*만 아주 짧게. 출력 토큰이 지연의 지배 비용이라
# (생성 ~90 tok/s), 다음질문 steering에 필요한 최소 2키만 내 turn 종료 지연을 묶는다.
# (5키 중첩 스키마는 160토큰에서 잘려 JSON이 깨졌음 — 실측, 2026-05-30.)
EVAL_TAIL_SYSTEM = (
    "당신은 까다로운 면접관입니다. 지원자 답변의 **마지막 구간**(영상+음성)을 빠르게 보고, "
    "다음 질문을 정하는 데 필요한 핵심만 아주 짧게 내세요. 절대 길게 쓰지 말 것. "
    "오직 SINGLE JSON object로만, 키 2개만: "
    '{"summary": "이 구간 한 줄 요약(한국어, 한 문장)", '
    '"critique": ["가장 걸리는 약점 1~2개, 한국어 짧게"]}'
)


# inject-and-emit 차단 규칙(.dev/vision/README.md 필수 조건 2) — 수치는 객관 레인이 실측한
# ground truth이고, 모델의 역할은 측정이 아니라 해석이다. 충돌 7건(비전)·3건(오디오)은 전부
# "측정 못 하는데 측정한 척"에서 왔으므로 생성 시점에서 막는다. 같은 수치가 record에도 raw로
# 보존되므로(emit), 모델 출력이 수치와 모순되면 그 자체가 모델 실패 신호다.
GROUNDING_RULES = (
    "위 측정치는 이 구간을 객관 도구로 실측한 값입니다. 반드시 따르세요: "
    "(1) 측정치와 모순되는 묘사 금지. "
    "(2) 측정치에 근거 없는 관찰(예: 측정되지 않은 떨림·미세 표정)을 지어내지 말 것. "
    "(3) '관측 불가'로 표시된 항목은 평가하지 말 것 — 없다고 단정하는 것도 금지. "
    "(4) 긴장·자신감·열정 같은 내면 상태는 어떤 측정치로도 단정할 수 없습니다 — "
    "보이는/들리는 단서만 기술하세요."
)


def grounding_block(*, vision: dict | None = None, prosody: dict | None = None) -> str:
    """객관 측정치를 프롬프트 주입 블록으로 직렬화(한국어 라벨 — raw 수치 dump가 아니라 집계).
    측정치가 없으면 빈 문자열(주입 생략). 해석 라벨은 붙이지 않는다(측정/해석 분업)."""
    lines: list[str] = []
    if vision:
        desc = describe_visual(vision)
        if desc:
            lines.append("[시각 측정치 — MediaPipe 실측]\n" + desc)
    if prosody:
        desc = describe_vocal(prosody)
        if desc:
            lines.append("[음성 측정치 — 프로소디 실측]\n" + desc)
    if not lines:
        return ""
    return "\n\n" + "\n\n".join(lines) + "\n\n" + GROUNDING_RULES


def _video_part(url: str) -> dict:
    return {"type": "video_url", "video_url": {"url": url}}


def _audio_part(url: str) -> dict:
    return {"type": "audio_url", "audio_url": {"url": url}}


def _repair_truncated_json(text: str) -> dict | None:
    """max_tokens로 잘린 JSON object 복구 — 열린 문자열/괄호를 균형 맞춰 닫고 파싱 시도.
    실패하면 None. (compact tail이 토큰 상한에 닿아도 부분 신호라도 건지기 위한 방어선.)"""
    start = text.find("{")
    if start < 0:
        return None
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text[start:]:
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    frag = text[start:]
    if in_str:
        frag += '"'                      # 열린 문자열 닫기
    frag = frag.rstrip().rstrip(",")     # 끝의 불완전 쉼표 제거
    if re.search(r":\s*$", frag):
        frag += " null"                  # "key": 만 있고 값 없이 끊긴 경우
    frag += "".join(reversed(stack))     # 남은 열린 괄호 역순으로 닫기
    try:
        return json.loads(frag)
    except json.JSONDecodeError:
        return None


def _extract_json(text: str) -> dict:
    """모델 출력에서 첫 JSON object를 파싱. 잘린 출력은 복구 시도. 실패 시 ValueError."""
    text = text.strip()
    # ```json ... ``` 펜스 제거
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    repaired = _repair_truncated_json(text)  # max_tokens로 잘린 JSON 복구 시도
    if repaired is not None:
        return repaired
    raise ValueError(f"no JSON object in output: {text[:200]!r}")


class WindowCritic:
    """vLLM을 숨긴 윈도우 단위 비평자. 윈도우(1fps JPEG 프레임 + 16kHz PCM)를 받아
    video_url+audio_url로 조립해 E4B에 보내고, per-window 구조화 신호를 반환한다.
    집계는 하지 않는다(다운스트림 몫) — 비평 메서드는 모두 *한 윈도우*의 신호만 낸다.

    전사(STT)는 직접 박지 않고 주입된 `Transcriber`에 위임한다(`transcribe_window`).
    기본 transcriber는 `GemmaNativeTranscriber` — critic과 **같은** vLLM client를 공유해
    같은 E4B로 전사하므로 VRAM 추가가 0이다.
    """

    def __init__(
        self,
        *,
        client: VllmClient | None = None,
        transcriber: Transcriber | None = None,
        model: str = MODEL,
        eval_max_tokens: int = 640,
        tail_max_tokens: int = 160,
        nonverbal_max_tokens: int = 96,
    ) -> None:
        self.client = client or default_vllm_client()
        # 전사기는 critic과 같은 client를 공유 — 같은 vLLM E4B로 전사(VRAM 추가 0).
        self.transcriber = transcriber or GemmaNativeTranscriber(client=self.client)
        self.model = model
        self.eval_max_tokens = eval_max_tokens      # 발화중 풀 비평(채널②)
        self.tail_max_tokens = tail_max_tokens      # end-of-turn compact tail (≤2.5s 목표 레버)
        self.nonverbal_max_tokens = nonverbal_max_tokens  # 채널① 비언어 read

    def _ask_json(self, *, system: str, content: list[dict], max_tokens: int) -> dict:
        resp = self.client.chat(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.0,
            }
        )
        return _extract_json(resp["choices"][0]["message"]["content"])

    def warmup(self) -> None:
        """기동 1회 더미 요청으로 모델/CUDA 그래프 선warm (best-effort, 실패 무시).
        첫 턴의 부팅 비용(CUDA 그래프 컴파일 ~1.2s, D6)을 사용자 턴 밖으로 뺀다.
        주: 텍스트 경로만 데움 — 멀티모달 그래프 완전 선warm엔 합성 클립이 필요(미포함)."""
        try:
            self.client.chat({
                "model": self.model,
                "messages": [{"role": "user", "content": "warmup"}],
                "max_tokens": 1,
                "temperature": 0.0,
            })
        except Exception:  # noqa: BLE001 - 선warm 실패는 치명적이지 않음
            pass

    def read_nonverbal(
        self,
        jpeg_frames: list[bytes],
        pcm: bytes,
        *,
        t: float,
        window_s: float,
        src_fps: float = 1.0,
        sample_rate: int = 16000,
        vision: dict | None = None,
    ) -> NonVerbalSignal:
        """채널 ① — 짧은 윈도우에서 지원자 비언어 상태 read.

        vision: 비전 dense 레인의 윈도우 집계(analysis/grounding.py). 있으면 프롬프트에 주입
        (inject) — 1fps 프레임 3장이 못 담는 시계열 정보(깜빡임 횟수·미소 지속%·움직임 분산)를
        보강하고, 근거 없는 관찰/긴장 단정을 생성 시점에서 차단한다(GROUNDING_RULES)."""
        content = [_video_part(assemble_video_url(jpeg_frames, src_fps=src_fps))]
        if pcm:
            content.append(_audio_part(assemble_audio_url(pcm, sample_rate=sample_rate)))
        prompt = "Read the candidate's non-verbal state now." + grounding_block(vision=vision)
        content.append({"type": "text", "text": prompt})
        data = self._ask_json(system=NONVERBAL_SYSTEM, content=content, max_tokens=self.nonverbal_max_tokens)
        state = str(data.get("state", "neutral")).strip().lower()
        if state not in NONVERBAL_STATES:
            state = "neutral"
        try:
            intensity = max(0.0, min(1.0, float(data.get("intensity", 0.0))))
        except (TypeError, ValueError):
            intensity = 0.0
        return NonVerbalSignal(
            t=t, window_s=window_s, state=state, intensity=intensity,
            note=str(data.get("note", ""))[:120],
        )

    def transcribe_window(
        self,
        pcm: bytes,
        *,
        t: float,
        window_s: float,
        sample_rate: int = 16000,
    ) -> str:
        """채널 ① 페어 — 같은 윈도우의 PCM을 전사한다. critic은 STT를 직접 박지 않고
        주입된 `Transcriber`에 위임한다(기본 GemmaNative, 같은 vLLM → VRAM 0).

        주: `t`/`window_s`는 쓰지 않는다(transcriber는 윈도우-비종속). read_nonverbal과
        호출부 시그니처를 맞춰, windowing(Slice 4)이 같은 (t, window_s)로 nv 신호와 전사를
        윈도우별로 페어링할 수 있게 받기만 한다.
        """
        return self.transcriber.transcribe(pcm, sample_rate=sample_rate)

    def evaluate_window(
        self,
        jpeg_frames: list[bytes],
        pcm: bytes,
        *,
        window_start_s: float,
        window_dur_s: float,
        src_fps: float = 1.0,
        sample_rate: int = 16000,
        compact: bool = False,
        prosody: dict | None = None,
        vision: dict | None = None,
    ) -> EvaluationSignal:
        """채널 ② — verbal/vocal/visual 비평. compact=True면 답변 *마지막 구간*을 짧은 출력으로
        비평(터너 토큰을 줄여 end-of-turn 지연을 ≤목표로 — 생성 토큰이 지배 비용).

        prosody/vision: 객관 레인의 윈도우 측정치(analysis/prosody.py·grounding.py). 있으면
        프롬프트에 주입 — vocal(단조로움·속도·볼륨·쉼)·visual(미소·자세·제스처) 비평을 수치에
        묶고, 근거 없는 상투구/자기모순/긴장 단정을 생성 시점에서 차단한다(GROUNDING_RULES)."""
        system = EVAL_TAIL_SYSTEM if compact else EVAL_SYSTEM
        max_tokens = self.tail_max_tokens if compact else self.eval_max_tokens
        prompt = "Critique the final part of this answer." if compact else "Evaluate this answer window."
        prompt += grounding_block(vision=vision, prosody=prosody)
        content = [
            _video_part(assemble_video_url(jpeg_frames, src_fps=src_fps)),
            _audio_part(assemble_audio_url(pcm, sample_rate=sample_rate)),
            {"type": "text", "text": prompt},
        ]
        data = self._ask_json(system=system, content=content, max_tokens=max_tokens)
        if compact:  # 2키(summary/critique) 최소 스키마 → key_observations[0]=summary + critique
            summary = str(data.get("summary", "")).strip()
            return EvaluationSignal(
                window_start_s=window_start_s,
                window_dur_s=window_dur_s,
                critique=[str(x) for x in data.get("critique", [])][:2],
                key_observations=[summary] if summary else [],
                compact=True,
            )
        return EvaluationSignal(
            window_start_s=window_start_s,
            window_dur_s=window_dur_s,
            verbal=data.get("verbal", {}) if isinstance(data.get("verbal"), dict) else {},
            vocal=data.get("vocal", {}) if isinstance(data.get("vocal"), dict) else {},
            visual=data.get("visual", {}) if isinstance(data.get("visual"), dict) else {},
            critique=[str(x) for x in data.get("critique", [])][:5],
            key_observations=[str(x) for x in data.get("key_observations", [])][:8],
        )
