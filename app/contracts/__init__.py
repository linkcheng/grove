"""Canonical execution contract package (WS-1)."""

from app.contracts import canonical as _canonical
from app.contracts.canonical import *  # noqa: F403
from app.contracts.trust import (
    enrich_decision,
    enrich_knowledge_decision,
    knowledge_request_from_decision,
    tool_command_from_decision,
)

__all__ = [
    *_canonical.__all__,
    "enrich_decision",
    "enrich_knowledge_decision",
    "knowledge_request_from_decision",
    "tool_command_from_decision",
]
