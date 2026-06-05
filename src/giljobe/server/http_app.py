"""HTTP 표면 — 통합 타깃(`services/analysis-engine`) 계약을 aiohttp로 서빙.

내부 경로(caddy가 `/analysis` prefix를 외부에서 부착 → 내부는 prefix 없음, Slice 11 로그로 실측):
  GET  /healthz            라이브니스(항상 200)
  GET  /readyz             레디니스(200; vLLM 등 의존 상태는 ready_check 주입)
  POST /subscriber/start   {sessionId, criticMode} → 202, 턴 시작(룸 join)
  POST /subscriber/stop    {} → 200, 턴 flush(end_turn)
  GET  /signals?sessionId= → 200, SignalRecorder 레코드 래퍼(transcriptFull=ai-engine lastAnswer)

★ 모든 핸들러는 AnalysisService의 **같은 영속 이벤트루프**(이 aiohttp 앱 루프)에서 돈다 — 구독자
드레인/poll/브리지가 이 루프에 살아있어 그쪽 wrapper의 루프-lifecycle 버그가 구조적으로 없다.
시크릿(rules/security.md): 토큰/키는 응답·로그에 싣지 않는다(에러 메시지에 시크릿 금지).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable

from aiohttp import web

from .service import SERVICE_NAME, AnalysisService

logger = logging.getLogger(__name__)


def _json(payload: dict, status: int = 200) -> web.Response:
    # ensure_ascii=False — 한국어 전사를 읽기 좋게(JSONL/SSE 관례와 일치).
    return web.json_response(payload, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def _healthz(_req: web.Request) -> web.Response:
    return _json({"status": "ok", "service": SERVICE_NAME})


async def _readyz(req: web.Request) -> web.Response:
    check: Callable[[], bool] | None = req.app.get("ready_check")
    ready = True if check is None else bool(_safe(check))
    return _json({"ready": ready, "service": SERVICE_NAME}, status=200 if ready else 503)


async def _start(req: web.Request) -> web.Response:
    body = await _read_json(req)
    if body is None:
        return _json({"error": "invalid_json"}, status=400)
    session_id = body.get("sessionId")
    if not session_id:
        return _json({"error": "sessionId_required"}, status=400)
    critic_mode = body.get("criticMode", "window")
    service: AnalysisService = req.app["service"]
    try:
        result = await service.start(str(session_id), str(critic_mode))
    except Exception as exc:  # noqa: BLE001 - 시작 실패(토큰/접속)는 503으로(시크릿 비노출 메시지)
        logger.exception("subscriber start 실패 session=%s", session_id)
        return _json({"error": "start_failed", "detail": type(exc).__name__}, status=503)
    return _json(result, status=202)


async def _stop(req: web.Request) -> web.Response:
    service: AnalysisService = req.app["service"]
    result = await service.stop()
    return _json(result, status=200)


async def _signals(req: web.Request) -> web.Response:
    session_id = req.query.get("sessionId")
    service: AnalysisService = req.app["service"]
    return _json(service.signals(session_id), status=200)


def make_app(service: AnalysisService, *, ready_check: Callable[[], bool] | None = None) -> web.Application:
    """라우트를 service에 배선한 aiohttp 앱. 종료 시 service.aclose로 활성 세션 정리."""
    app = web.Application()
    app["service"] = service
    app["ready_check"] = ready_check
    app.add_routes([
        web.get("/healthz", _healthz),
        web.get("/readyz", _readyz),
        web.post("/subscriber/start", _start),
        web.post("/subscriber/stop", _stop),
        web.get("/signals", _signals),
    ])
    app.on_cleanup.append(_on_cleanup)
    return app


async def _on_cleanup(app: web.Application) -> None:
    await app["service"].aclose()


async def _read_json(req: web.Request) -> dict | None:
    try:
        if not req.can_read_body:
            return {}
        return await req.json()
    except (json.JSONDecodeError, ValueError):
        return None


def _safe(check: Callable[[], bool]) -> bool:
    try:
        return check()
    except Exception:  # noqa: BLE001 - 레디 체크 실패는 not-ready로(예외 전파 X)
        return False
