"""프로덕션 엔트리 — 통합 컨테이너가 실행할 분석 서비스(`python -m giljobe.server`).

통합 타깃(`services/analysis-engine`)은 우리 src를 클론해 이 엔트리를 CMD로 돌리면 된다(그쪽 자작
wrapper를 대체). 실 WindowCritic(공유 vLLM E4B)+실 rtc.Room으로 AnalysisService를 조립하고,
env에서 LiveKit/vLLM 접속을 해석한다(server.config / llm.vllm_client).

env: ANALYSIS_ENGINE_PORT|PORT(기본 8200) · LIVEKIT_URL · LIVEKIT_TOKEN|LIVEKIT_API_KEY/SECRET ·
     LIVEKIT_SESSION_ID · VLLM_BASE_URL. 시크릿은 읽기만(로그 비노출 — rules/security.md).
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

    return WindowCritic(client=default_vllm_client())


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
    service = AnalysisService(make_critic=lambda _mode: critic)
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
