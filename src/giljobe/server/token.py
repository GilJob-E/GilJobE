"""LiveKit 액세스 토큰 발급 — hidden 구독자(우리)용 JWT.

통합 타깃에선 services/api가 토큰을 발급하는 게 권장이지만(키가 거기 있음), 우리가 직접 키를 가질
때(env LIVEKIT_API_KEY/SECRET)는 여기서 self-mint한다. grant는 **hidden subscriber**:
다른 참가자에 안 보이고(hidden), 구독만 하며(can_subscribe), publish는 안 한다(can_publish=False).

★ 시크릿 위생(rules/security.md): api_key/secret·생성된 JWT를 로그·커밋·콘솔에 남기지 않는다.
"""
from __future__ import annotations

from datetime import timedelta

# candidate(피면접자)와 충돌하지 않는 고유 identity. 룸 패턴은 giljob-session-{id}(통합 타깃 규약).
DEFAULT_IDENTITY = "giljobe-analysis"
DEFAULT_TTL_S = 3600


def subscriber_token(
    api_key: str,
    api_secret: str,
    room: str,
    *,
    identity: str = DEFAULT_IDENTITY,
    ttl_s: int = DEFAULT_TTL_S,
) -> str:
    """hidden subscriber 토큰(JWT)을 만든다. livekit-api는 호출 시 lazy import(미설치 환경 보호)."""
    from livekit import api

    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_subscribe=True,
                can_publish=False,
                can_publish_data=False,
                hidden=True,  # ★ 다른 참가자(candidate)에게 안 보이는 분석 봇
            )
        )
        .with_ttl(timedelta(seconds=ttl_s))
        .to_jwt()
    )
