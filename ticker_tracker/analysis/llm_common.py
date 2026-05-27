"""Shared LLM JSON parsing and coercion for analysis providers."""

from __future__ import annotations

import json
from typing import Any

_VALID_SIGNALS = frozenset({"BUY", "HOLD", "SELL", "INSUFFICIENT DATA"})
_VALID_CONFIDENCE = frozenset({"High", "Medium", "Low"})

SIMPLIFIED_JSON_INSTRUCTION = """
Respond with ONLY this JSON (no other text):
{"signal": "HOLD", "confidence": "Medium", "strengths": ["a","b","c"], \
"risks": ["x","y","z"], "summary": "Brief two-sentence summary."}
"""


def normalize_triple(items: Any, *, filler: str) -> list[str]:
    if not isinstance(items, list):
        return [filler, filler, filler]
    out = [str(x).strip() for x in items if str(x).strip()][:3]
    while len(out) < 3:
        out.append(filler)
    return out[:3]


def parse_llm_json(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def coerce_signal(value: Any) -> str:
    s = str(value or "").strip().upper()
    if s in _VALID_SIGNALS:
        return s
    return "INSUFFICIENT DATA"


def coerce_confidence(value: Any) -> str:
    s = str(value or "").strip()
    if s in _VALID_CONFIDENCE:
        return s
    title = s.title()
    if title in _VALID_CONFIDENCE:
        return title
    return "Low"
