"""프로덕션 엔트리 — 통합 컨테이너가 실행할 분석 서비스(`python -m giljobe.server`).

통합 타깃(`services/analysis-engine`)은 우리 src를 클론해 이 엔트리를 CMD로 돌리면 된다(그쪽 자작
wrapper를 대체). 실 WindowCritic(공유 vLLM E4B)+실 rtc.Room으로 AnalysisService를 조립하고,
env에서 LiveKit/vLLM 접속을 해석한다(server.config / llm.vllm_client).

env: ANALYSIS_ENGINE_PORT|PORT(기본 8200) · LIVEKIT_URL · LIVEKIT_TOKEN|LIVEKIT_API_KEY/SECRET ·
     LIVEKIT_SESSION_ID · VLLM_BASE_URL. 시크릿은 읽기만(로그 비노출 — rules/security.md).
객관 그라운딩 레인(선택): GILJOBE_VISION_MODELS_DIR(MediaPipe .task 2개) · GILJOBE_VISION=off ·
     GILJOBE_PROSODY=off. 의존성(`[vision]`/`[prosody]` extra)·모델이 없으면 자동 비활성(서비스 무영향).
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

from .http_app import make_app
from .service import AnalysisService

logger = logging.getLogger(__name__)


def _build_critic():
    """공유 WindowCritic 1개 — 턴마다 새로 만들지 않고 재사용(전사기 포함, 같은 vLLM E4B → VRAM 0).
    구성만 하고 health는 안 친다(vLLM 다운이어도 서버는 기동, /readyz가 상태 반영)."""
    from giljobe.analysis.critic import WindowCritic
    from giljobe.llm.vllm_client import default_vllm_client

    client = default_vllm_client()
    return WindowCritic(client=client, transcriber=_build_transcriber(client))


def _build_transcriber(client):
    """STT 선택(`GILJOBE_STT`): gemma(기본, 현행 무변경) | whisper(로컬 단독) |
    auto(gemma primary → whisper 폴백 — vLLM 장애 시에도 전사 유지).
    None 반환이면 critic이 기본 GemmaNative를 만든다. 미스컨피그는 기동 시점에 명시 실패
    (침묵 강등 금지 — 잘못된 값으로 gemma가 조용히 선택되면 폴백 의도가 증발한다)."""
    from giljobe.analysis.transcriber import (
        FallbackTranscriber,
        FasterWhisperTranscriber,
        GemmaNativeTranscriber,
    )

    mode = (os.environ.get("GILJOBE_STT") or "gemma").strip().lower()
    if mode == "gemma":
        return None
    whisper = FasterWhisperTranscriber(model_name=os.environ.get("GILJOBE_WHISPER_MODEL", "small"))
    if mode == "whisper":
        return whisper
    if mode == "auto":
        return FallbackTranscriber(GemmaNativeTranscriber(client=client), whisper)
    raise ValueError(f"GILJOBE_STT 값이 잘못됨: {mode!r} (gemma|whisper|auto)")


def _make_lanes():
    """객관 그라운딩 레인 팩토리(start마다 호출) — 의존성/모델/env에 따라 자동 켜짐/꺼짐.
    grounder는 세션 수명(윈도워가 close)이라 매번 새로, prosody는 상태 없지만 같은 경로로 생성."""
    from giljobe.analysis.grounding import maybe_vision_grounder
    from giljobe.analysis.prosody import maybe_prosody_extractor

    return maybe_vision_grounder(), maybe_prosody_extractor()


def _vllm_ready(critic) -> bool:
    health = getattr(critic.client, "health", None)
    if health is None:
        return True
    try:
        health()
        return True
    except Exception:  # noqa: BLE001 - 다운이면 not-ready(예외 전파 X)
        return False


def _port() -> int:
    return int(os.environ.get("ANALYSIS_ENGINE_PORT") or os.environ.get("PORT") or 8200)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    critic = _build_critic()
    # 공유 critic 재사용 — make_critic은 mode 라벨만 받고 같은 인스턴스 반환.
    service = AnalysisService(make_critic=lambda _mode: critic, make_lanes=_make_lanes)
    app = make_app(service, ready_check=lambda: _vllm_ready(critic))

    async def _warmup(_app: web.Application) -> None:
        # 첫 턴의 콜드 그래프 비용을 사용자 턴 밖으로(best-effort).
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, critic.warmup)
        logger.info("critic warmup 완료")

    app.on_startup.append(_warmup)
    port = _port()
    logger.info("analysis-engine 서버 기동 :%d (LiveKit=%s)", port, os.environ.get("LIVEKIT_URL"))
    web.run_app(app, host="0.0.0.0", port=port, access_log=None)


if __name__ == "__main__":
    main()
