"""critic의 JSON 방어층 오프라인 회귀 — gje에서 verbatim 포팅한 `_extract_json`/
`_repair_truncated_json`이 포팅 후에도 그대로 동작하는지 가드한다(GPU/vLLM 불요).

채널① 신뢰성의 바닥선: E4B 출력이 펜스로 감싸이거나, 텍스트에 섞여 나오거나,
max_tokens로 잘려도 부분 신호라도 건져야 한다. happy path만이 아니라 잘림·실패도 본다.
"""
from __future__ import annotations

import pytest

from giljobe.analysis.critic import _extract_json, _repair_truncated_json


def test_extract_plain_json():
    assert _extract_json('{"state": "nervous", "intensity": 0.4}') == {
        "state": "nervous",
        "intensity": 0.4,
    }


def test_extract_strips_code_fence():
    # E4B가 ```json ... ``` 펜스로 감싸 내는 경우
    text = '```json\n{"state": "engaged", "intensity": 0.7}\n```'
    assert _extract_json(text) == {"state": "engaged", "intensity": 0.7}


def test_extract_embedded_in_prose():
    # 설명 문장에 JSON object가 섞여 나오는 경우 → 첫 object 추출
    text = '관찰 결과는 다음과 같습니다: {"state": "hesitant", "intensity": 0.3} 입니다.'
    assert _extract_json(text) == {"state": "hesitant", "intensity": 0.3}


def test_repair_truncated_object():
    # max_tokens로 문자열 중간에서 잘린 출력 → 균형 복구로 부분 신호 건짐
    truncated = '{"state": "confident", "intensity": 0.6, "note": "시선이 안정적이고 목소'
    repaired = _repair_truncated_json(truncated)
    assert repaired is not None
    assert repaired["state"] == "confident"
    assert repaired["intensity"] == 0.6


def test_repair_truncated_after_colon():
    # "key": 까지만 나오고 값 없이 끊긴 경우 → null 채워 파싱
    repaired = _repair_truncated_json('{"state": "neutral", "intensity":')
    assert repaired is not None
    assert repaired["state"] == "neutral"
    assert repaired["intensity"] is None


def test_extract_recovers_truncated_via_repair():
    # _extract_json은 직파싱/정규식 실패 시 _repair_truncated_json으로 폴백
    truncated = '{"verbal": {"logic": "논리가 다소 비약'
    out = _extract_json(truncated)
    assert isinstance(out, dict)
    assert "verbal" in out


def test_extract_raises_on_no_json():
    with pytest.raises(ValueError):
        _extract_json("여기엔 JSON object가 전혀 없습니다.")


def test_repair_returns_none_on_no_brace():
    assert _repair_truncated_json("브레이스 없음") is None
