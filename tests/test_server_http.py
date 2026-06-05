#!/usr/bin/env python3
"""server HTTP 계약 테스트 — 통합 타깃(`services/analysis-engine`) 계약을 GPU·LiveKit 없이 검증.

AnalysisService(코어)와 aiohttp 라우트를 MockCritic+FakeRoom 주입으로 결정적으로 돌린다. Slice 11에서
그쪽 자작 wrapper가 낸 버그(① 전사 빔 ② 발행 중 /signals records=0=스톱 후에만 노출 ③ turn_end.eval
null ④ Event loop is closed)를 우리 서버가 구조적으로 피함을 회귀가드로 잠근다. 특히 ②는
**발행 중(stop 전) /signals에 window 레코드가 보인다**로 직접 막는다(단일 영속 루프 드레인의 핵심 효과).

실행: pytest tests/test_server_http.py
"""
from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from giljobe.analysis.mocks import MockCritic, MockTranscriber
from giljobe.server.config import SubscriberConfig, room_name
from giljobe.server.http_app import make_app
from giljobe.server.service import AnalysisService, MemorySink, signals_payload


# ─────────────────────────── fake LiveKit 표면(룸만) ───────────────────────────
class FakeRoom:
    """rtc.Room 대역 — connect/disconnect/on/remote_participants. 트랙은 안 흘리고(테스트는
    윈도워에 직접 add_*), connect는 no-op."""

    def __init__(self) -> None:
        self.handlers: dict = {}
        self.remote_participants: dict = {}
        self.connected = False

    def on(self, event, callback=None):  # noqa: ANN001
        def reg(cb):
            self.handlers.setdefault(event, []).append(cb)
            return cb
        return reg(callback) if callback is not None else reg

    async def connect(self, url, token, options):  # noqa: ANN001
        self.connected = True

    def disconnect(self):
        pass


def _make_service(*, token: str | None = "tok") -> AnalysisService:
    """MockCritic(전사 '조각') + FakeRoom 주입 서비스. token=None이면 시작 실패 경로 검증용."""
    def make_critic(_mode: str):
        return MockCritic(transcriber=MockTranscriber(text="조각"))

    def make_config(sid: str) -> SubscriberConfig:
        return SubscriberConfig(url="ws://x", room=room_name(sid), session_id=sid, token=token)

    return AnalysisService(
        make_critic=make_critic, make_config=make_config,
        room_factory=FakeRoom, clock=lambda: 100.0,
    )


def _drive_media(service: AnalysisService) -> None:
    """활성 구독자의 윈도워에 합성 9s 입력을 직접 투입(트랙 수신 모사) — window 3개 유발."""
    w = service._active.subscriber.windower  # noqa: SLF001 - 테스트가 윈도워에 직접 주입(트랙 대역)
    t = 0.0
    while t < 9.0:
        w.add_frame(t, b"jpeg"); t += 1.0
    t = 0.0
    while t < 9.0:
        w.add_audio(t, b"pcm"); t += 0.5
    w.poll(9.0)


# ─────────────────────────── 1. health / ready ───────────────────────────
def test_healthz_ok():
    async def scenario():
        async with TestClient(TestServer(make_app(_make_service()))) as c:
            r = await c.get("/healthz")
            return r.status, await r.json()
    status, body = asyncio.run(scenario())
    assert status == 200 and body["status"] == "ok" and body["service"] == "analysis-engine"
    print("  ✓ /healthz 200 ok")


def test_readyz_reflects_check():
    async def scenario():
        ok_app = make_app(_make_service(), ready_check=lambda: True)
        down_app = make_app(_make_service(), ready_check=lambda: False)
        async with TestClient(TestServer(ok_app)) as c1, TestClient(TestServer(down_app)) as c2:
            r1 = await c1.get("/readyz")
            r2 = await c2.get("/readyz")
            return (r1.status, await r1.json()), (r2.status, await r2.json())
    ok, down = asyncio.run(scenario())
    assert ok == (200, {"ready": True, "service": "analysis-engine"})
    assert down[0] == 503 and down[1]["ready"] is False
    print("  ✓ /readyz: ready_check True→200 / False→503")


# ─────────────────────────── 2. start 계약 + 단일 세션 ───────────────────────────
def test_start_returns_running_contract():
    async def scenario():
        service = _make_service()
        async with TestClient(TestServer(make_app(service))) as c:
            r = await c.post("/subscriber/start", json={"sessionId": "iv1", "criticMode": "window"})
            body = await r.json()
            return r.status, body, service.active_session_id
    status, body, active = asyncio.run(scenario())
    assert status == 202
    assert body["sessionId"] == "iv1" and body["criticMode"] == "window"
    assert body["state"] == "running" and body["status"] == "running" and body["threadAlive"] is True
    assert active == "iv1"
    print(f"  ✓ start 202 계약: {body['sessionId']}/{body['state']}")


def test_start_missing_session_is_400():
    async def scenario():
        async with TestClient(TestServer(make_app(_make_service()))) as c:
            r = await c.post("/subscriber/start", json={"criticMode": "window"})
            return r.status, await r.json()
    status, body = asyncio.run(scenario())
    assert status == 400 and body["error"] == "sessionId_required"
    print("  ✓ start sessionId 누락 → 400")


def test_start_without_token_is_503():
    """LiveKit 토큰 미해석(키 없음)이면 503 start_failed(시크릿 비노출 detail=타입명)."""
    async def scenario():
        async with TestClient(TestServer(make_app(_make_service(token=None)))) as c:
            r = await c.post("/subscriber/start", json={"sessionId": "iv1"})
            return r.status, await r.json()
    status, body = asyncio.run(scenario())
    assert status == 503 and body["error"] == "start_failed"
    print("  ✓ 토큰 미해석 → 503 start_failed")


def test_second_start_replaces_first():
    """단일 세션 — 활성 중 start는 이전을 stop하고 새 세션을 연다."""
    async def scenario():
        service = _make_service()
        async with TestClient(TestServer(make_app(service))) as c:
            await c.post("/subscriber/start", json={"sessionId": "iv1"})
            await c.post("/subscriber/start", json={"sessionId": "iv2"})
            return service.active_session_id
    assert asyncio.run(scenario()) == "iv2"
    print("  ✓ 단일 세션: 두 번째 start가 첫 세션 대체")


# ─────────────────────────── 3. ★ 발행 중 records 가시(그쪽 버그 회귀가드) ───────────────────────────
def test_signals_visible_during_streaming_then_turn_end_on_stop():
    """★ load-bearing: 발행 중(stop 전) /signals에 window 레코드가 보이고 전사가 채워진다 —
    그쪽 wrapper는 stop flush 후에만 노출 + 전사 빔이었다(Slice 11). stop 후 turn_end+transcriptFull."""
    async def scenario():
        service = _make_service()
        async with TestClient(TestServer(make_app(service))) as c:
            await c.post("/subscriber/start", json={"sessionId": "iv1", "criticMode": "window"})
            _drive_media(service)
            await asyncio.sleep(0.4)  # 워커(MockCritic) + 드레인이 window 레코드를 MemorySink로

            mid = await (await c.get("/signals?sessionId=iv1")).json()  # ★ stop 전
            r = await c.post("/subscriber/stop", json={})
            stop_body = await r.json()
            final = await (await c.get("/signals?sessionId=iv1")).json()  # stop 후
            return mid, stop_body, final
    mid, stop_body, final = asyncio.run(scenario())

    # 발행 중: window 3개가 이미 보이고 전사가 비지 않음(스트리밍 가시성 + 전사 동작).
    mid_windows = [r for r in mid["records"] if r["type"] == "window"]
    assert len(mid_windows) == 3, [r["t"] for r in mid_windows]
    assert sorted(r["t"] for r in mid_windows) == [1.5, 4.5, 7.5]
    assert all(r["transcript"] == "조각" for r in mid_windows), "발행 중 전사 채워짐"
    assert mid["windowTranscripts"] == ["조각", "조각", "조각"]
    assert mid["rawMediaExposed"] is False and mid["rawSecretsExposed"] is False
    assert mid["latestTurnEnd"] is None  # 아직 stop 전 → turn_end 없음

    # stop: turn_end 1개 + transcriptFull(=lastAnswer) 채워짐.
    assert stop_body["status"] == "stopped"
    turn_ends = [r for r in final["records"] if r["type"] == "turn_end"]
    assert len(turn_ends) == 1
    assert final["transcriptFull"] == "조각 조각 조각"
    assert final["latestTurnEnd"]["type"] == "turn_end"
    print(f"  ✓ ★발행중 window {len(mid_windows)} 가시+전사 / stop후 turn_end+transcriptFull='{final['transcriptFull']}'")


# ─────────────────────────── 4. signals 미스매치/유휴 ───────────────────────────
def test_signals_unknown_session_is_empty():
    async def scenario():
        async with TestClient(TestServer(make_app(_make_service()))) as c:
            return await (await c.get("/signals?sessionId=nope")).json()
    body = asyncio.run(scenario())
    assert body["recordCount"] == 0 and body["records"] == [] and body["transcriptFull"] == ""
    assert body["service"] == "analysis-engine"
    print("  ✓ 모르는 session → 빈 페이로드")


def test_stop_when_idle_is_safe():
    async def scenario():
        async with TestClient(TestServer(make_app(_make_service()))) as c:
            r = await c.post("/subscriber/stop", json={})
            return r.status, await r.json()
    status, body = asyncio.run(scenario())
    assert status == 200 and body["status"] == "idle"
    print("  ✓ 유휴 stop 안전(idle)")


# ─────────────────────────── 5. signals_payload 순수 단위 ───────────────────────────
def test_signals_payload_shape():
    recs = [
        {"type": "window", "t": 1.5, "transcript": "안녕", "nonverbal": {"state": "x"}},
        {"type": "window", "t": 4.5, "transcript": "", "nonverbal": {"state": "y"}},
        {"type": "eval", "t": 5.0},
        {"type": "turn_end", "t": 9.0, "transcript_full": "안녕하세요", "eval": {"summary": "s"}},
    ]
    p = signals_payload("iv1", recs)
    assert p["recordCount"] == 4 and p["transcriptFull"] == "안녕하세요"
    assert p["windowTranscripts"] == ["안녕"]  # 빈 전사는 제외
    assert p["latestTurnEnd"]["eval"]["summary"] == "s"
    assert p["rawMediaExposed"] is False and p["rawSecretsExposed"] is False
    assert signals_payload("iv1", [])["recordCount"] == 0
    print("  ✓ signals_payload 모양(transcriptFull/windowTranscripts/raw flags)")


def test_memory_sink_accumulates():
    s = MemorySink()
    s.emit({"type": "window", "t": 1.5})
    s.emit({"type": "turn_end", "t": 9.0})
    assert [r["type"] for r in s.records()] == ["window", "turn_end"]
    print("  ✓ MemorySink 누적")


# ─────────────────────── 6. 적대리뷰 회귀가드(누수·경합·계약) ───────────────────────
class _RaisingRoom(FakeRoom):
    async def connect(self, url, token, options):  # noqa: ANN001 - connect 실패 모사(LiveKit 다운)
        raise RuntimeError("livekit down")


class _SlowRoom(FakeRoom):
    async def connect(self, url, token, options):  # noqa: ANN001 - 실 LiveKit I/O 양보 모사(경합 창)
        await asyncio.sleep(0.02)
        self.connected = True


def _orphan_loop_tasks() -> list[str]:
    """살아있는 subscriber drain/poll 영속 태스크(누수 감지) — 정상이면 활성 세션 것만 남는다."""
    out = []
    for t in asyncio.all_tasks():
        if t.done():
            continue
        q = getattr(t.get_coro(), "__qualname__", "")
        if "_drain_loop" in q or "_poll_loop" in q:
            out.append(q)
    return out


def test_start_connect_failure_cleans_up():
    """★ connect 실패(503 경로) 시 이미 띄운 drain/poll 태스크+윈도워가 누수되지 않고, 단일세션
    가드도 막히지 않아 다음 start가 정상 진행(적대리뷰 major #1·#5)."""
    async def scenario():
        service = _make_service()
        service._room_factory = _RaisingRoom  # connect raise
        raised = False
        try:
            await service.start("iv1")
        except RuntimeError:
            raised = True
        await asyncio.sleep(0.05)  # 취소 정착
        orphans = _orphan_loop_tasks()
        active_after_fail = service.active_session_id
        service._room_factory = FakeRoom       # 복구 → 다음 start
        await service.start("iv2")
        active = service.active_session_id
        await service.stop()
        return raised, orphans, active_after_fail, active
    raised, orphans, active_after_fail, active = asyncio.run(scenario())
    assert raised and active_after_fail is None
    assert orphans == [], f"connect 실패 후 orphan drain/poll 태스크 누수: {orphans}"
    assert active == "iv2", "실패 후에도 단일세션 가드가 안 막혀 다음 start 정상"
    print("  ✓ ★connect 실패 시 누수 0 + 다음 start 정상")


def test_concurrent_start_is_serialized():
    """★ 동시 start 둘이 lifecycle lock으로 직렬화 — 활성 1개만, 좀비 구독자 없음(적대리뷰 major #2)."""
    async def scenario():
        service = _make_service()
        service._room_factory = _SlowRoom
        await asyncio.gather(service.start("ivA"), service.start("ivB"))
        await asyncio.sleep(0.05)
        active, orphans = service.active_session_id, _orphan_loop_tasks()
        await service.stop()
        await asyncio.sleep(0.05)
        return active, orphans, _orphan_loop_tasks()
    active, orphans, after_stop = asyncio.run(scenario())
    assert active in ("ivA", "ivB")
    assert len(orphans) == 2, f"활성 1개의 drain+poll만(좀비 없음): {orphans}"
    assert after_stop == [], f"stop 후 태스크 0: {after_stop}"
    print(f"  ✓ ★동시 start 직렬화: active={active}, 좀비 0")


def test_stop_survives_aclose_failure():
    """★ aclose가 던져도 stop은 계약상 200 stopped 유지(미처리 500 방지 — 적대리뷰 minor #6)."""
    async def scenario():
        service = _make_service()
        await service.start("iv1")

        async def _boom(*_a, **_k):
            raise RuntimeError("aclose boom")
        service._active.subscriber.aclose = _boom  # aclose 강제 실패
        r = await service.stop()
        return r, service.active_session_id, service._last is not None
    r, active, has_last = asyncio.run(scenario())
    assert r == {"status": "stopped"} and active is None and has_last
    print("  ✓ ★aclose 실패에도 stop=stopped(계약 유지)")


def test_signals_retrieves_last_after_new_start():
    """★ 새 턴(iv2) 시작 후에도 직전 답변(iv1)을 /signals로 회수 가능(_last 룩업 — 적대리뷰 minor #4)."""
    async def scenario():
        service = _make_service()
        await service.start("iv1")
        _drive_media(service)
        await asyncio.sleep(0.4)
        await service.stop()           # iv1 → _last
        await service.start("iv2")     # iv2 활성
        s1, s2 = service.signals("iv1"), service.signals("iv2")
        await service.stop()
        return s1, s2
    s1, s2 = asyncio.run(scenario())
    assert s1["sessionId"] == "iv1" and s1["transcriptFull"] == "조각 조각 조각", "직전 턴 _last서 회수"
    assert s2["sessionId"] == "iv2"
    print("  ✓ ★새 턴 시작 후에도 직전 답변 _last 회수")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"=== server HTTP tests ({len(tests)}) ===")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {t.__name__}: {e}")
    print(f"=== {'ALL PASS' if not failed else f'{failed} FAILED'} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
