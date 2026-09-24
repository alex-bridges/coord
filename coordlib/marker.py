"""The machine marker that makes a GitHub comment a coord record.

Every agent posts as the same login (alex-bridges), so the comment author says
nothing about which agent wrote it. The marker does:

    <!-- coord:v1 {"agent":"claude@Eidolon:agent-coord","issue":980,"kind":"claim",...} -->

Rules:
- It is the LAST non-blank line of the comment. A quote-reply copies an earlier
  marker into the middle of a new comment; only the last line counts, so a quote
  never impersonates its source. (Kept from PR #989's design.)
- The payload is JSON, so file lists and free text need no escaping rules of
  our own. "--" is written as "-\\u002d" so the payload can never close the
  HTML comment early; in valid JSON text "--" can only occur inside a string,
  where the escape decodes back to the same characters.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

PREFIX = "<!-- coord:v1 "
SUFFIX = " -->"

KINDS = ("claim", "release", "msg", "train-add", "train-remove", "train", "review-substitute", "alex-approve")


def encode(payload: Dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    while "--" in text:
        text = text.replace("--", "-\\u002d")
    return f"{PREFIX}{text}{SUFFIX}"


def decode(body: Optional[str]) -> Optional[Dict[str, Any]]:
    """The payload of a comment whose last non-blank line is a marker, else None."""
    if not body:
        return None
    lines = [line.strip() for line in body.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        return None
    last = lines[-1]
    if not (last.startswith(PREFIX) and last.endswith(SUFFIX)):
        return None
    raw = last[len(PREFIX): -len(SUFFIX)]
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("kind") not in KINDS:
        return None
    return payload


def compose(text: str, payload: Dict[str, Any]) -> str:
    """A comment body: the human-readable text, a blank line, then the marker."""
    return f"{text.rstrip()}\n\n{encode(payload)}\n"


def strip(body: str) -> str:
    """The human-readable part of a coord comment (the marker line removed)."""
    lines = body.replace("\r\n", "\n").rstrip().split("\n")
    if lines and decode(lines[-1]) is not None:
        lines = lines[:-1]
    return "\n".join(lines).strip()
