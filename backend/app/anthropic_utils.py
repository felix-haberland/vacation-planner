"""Thin wrappers around the Anthropic SDK.

On a 4xx the SDK raises `APIStatusError` (usually `BadRequestError`) whose
`.response.text` / `.body` carries the real error message (missing tool_result,
invalid model, oversized context, etc.). Without logging that body, all we see
upstream is "httpx.HTTPStatusError: 400 Bad Request" which is useless.
"""

from __future__ import annotations

import json
import sys

import anthropic


def create_message(client: anthropic.Anthropic, **kwargs):
    """Call `client.messages.create(**kwargs)` and log Anthropic's response
    body to stderr if the API returns an error status. Re-raises so the
    caller / global exception handler still see the failure."""
    try:
        return client.messages.create(**kwargs)
    except anthropic.APIStatusError as exc:
        body_text = ""
        try:
            body_text = exc.response.text
        except Exception:
            pass
        payload = {
            "anthropic_status": exc.status_code,
            "anthropic_message": str(exc),
            "anthropic_body": body_text,
            "request_model": kwargs.get("model"),
            "request_tool_names": [t.get("name") for t in kwargs.get("tools") or []],
            "request_message_count": len(kwargs.get("messages") or []),
        }
        print(
            "ANTHROPIC_API_ERROR " + json.dumps(payload),
            file=sys.stderr,
            flush=True,
        )
        raise
