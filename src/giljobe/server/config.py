"""LiveKit 구독자 설정 — 환경변수에서 접속 정보·토큰을 해석한다.

토큰 경로 두 가지를 모두 지원(NEXT.md Slice 8 §2 — api 팀 합의 전까지 양쪽 열어둠):
  1. LIVEKIT_TOKEN 이 있으면 그대로 사용(services/api가 발급한 hidden subscriber 토큰).
  2. 없고 LIVEKIT_API_KEY/SECRET 이 있으면 token.subscriber_token으로 self-mint.
둘 다 없으면 token=None(라이브 접속 불가 — 호출자가 skip).

룸 이름은 통합 타깃 규약 `giljob-session-{id}`. ★ 시크릿(rules/security.md): 이 모듈은 값을
**읽기만** 하고 로그·repr에 노출하지 않는다(dataclass repr에 토큰/시크릿을 넣지 않음).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .token import DEFAULT_IDENTITY, subscriber_token

ROOM_PREFIX = "giljob-session-"


def room_name(session_id: str) -> str:
    """통합 타깃 프론트가 publish하는 룸 이름 규약(`giljob-session-{id}`)."""
    return f"{ROOM_PREFIX}{session_id}"


@dataclass
class SubscriberConfig:
    """구독자 접속 설정. token은 repr에서 마스킹(시크릿 위생)."""

    url: str
    room: str
    session_id: str
    identity: str = DEFAULT_IDENTITY
    poll_interval: float = 0.5
    # 토큰은 민감 → repr 제외. resolve_token()으로 채운다.
    token: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, *, session_id: str | None = None) -> "SubscriberConfig":
        """env에서 설정을 읽는다. session_id 미지정 시 LIVEKIT_SESSION_ID 또는 'local'.
        토큰은 env LIVEKIT_TOKEN 우선, 없으면 LIVEKIT_API_KEY/SECRET로 self-mint."""
        sid = session_id or os.environ.get("LIVEKIT_SESSION_ID", "local")
        url = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
        room = room_name(sid)
        identity = os.environ.get("LIVEKIT_IDENTITY", DEFAULT_IDENTITY)
        poll = float(os.environ.get("GILJOBE_POLL_INTERVAL", "0.5"))
        token = resolve_token(room, identity=identity)
        return cls(
            url=url, room=room, session_id=sid, identity=identity,
            poll_interval=poll, token=token,
        )


def resolve_token(room: str, *, identity: str = DEFAULT_IDENTITY) -> str | None:
    """env에서 토큰을 해석: LIVEKIT_TOKEN 그대로 사용, 없으면 키로 self-mint, 둘 다 없으면 None."""
    pre_issued = os.environ.get("LIVEKIT_TOKEN")
    if pre_issued:
        return pre_issued
    key, secret = os.environ.get("LIVEKIT_API_KEY"), os.environ.get("LIVEKIT_API_SECRET")
    if key and secret:
        return subscriber_token(key, secret, room, identity=identity)
    return None
