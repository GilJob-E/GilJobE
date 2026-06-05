#!/usr/bin/env python3
"""smoke-signals.jsonl 한 줄(레코드)을 받아 모니터용 1줄 요약으로 출력(라이브 가시성)."""
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    t = r.get("type")
    nvd = r.get("nonverbal") if isinstance(r.get("nonverbal"), dict) else {}
    nv = (nvd or {}).get("state", "-")
    tx = str(r.get("transcript") or r.get("transcript_full") or "")[:60]
    tt = r.get("t")
    print(f"[rec] {t} t={tt} nv={nv} tx={tx!r}", flush=True)
