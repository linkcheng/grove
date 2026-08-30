"""Runtime structural gate over the AssetRisk model answer (WS-7).

The gateway's prompted-JSON mode is verified NOT to enforce its advertised
``json_schema`` constraint (HTTP 200 regardless of output shape), so the
model randomly answers with an empty body, a leaked prompt placeholder
(``$your_answer``) or schema-echo gibberish.  This gate is deliberately
structural: it decides only whether an answer may be shown at all, never
whether it is business-correct -- business quality stays with the
owner-run human review.  The kernel retries within the issued
schema-retry budget; when the budget is exhausted the answer fails closed
as the graph's ``inference_output_invalid`` typed failure and never
reaches the typed report.
"""

from __future__ import annotations

MIN_ANSWER_CHARS = 80

# Substrings that occur only when prompted-format instructions leaked into
# the answer (observed shapes: the pydantic-ai prompted-output placeholder
# and raw JSON-schema fragments echoed instead of a business assessment).
# Business answers are Chinese prose citing frozen policies; none of these
# ASCII fragments can appear legitimately.
LEAK_MARKERS: frozenset[str] = frozenset(
    {
        "$your_answer",
        "additionalProperties",
        '"properties"',
        '"required"',
        '{"$schema"',
    }
)


class AnswerStructureError(ValueError):
    """Raised when an answer fails the structural gate (fail closed)."""


def validate_answer_structure(answer: object) -> str:
    """Validate the raw model answer; return the stripped answer or raise."""

    if type(answer) is not str:
        raise AnswerStructureError("answer must be an exact str")
    stripped = answer.strip()
    if not stripped:
        raise AnswerStructureError("answer is empty or whitespace-only")
    # Strong signals first: leaked format text and raw JSON echoes diagnose
    # the failure mode; the length floor is the weakest signal and runs last.
    for marker in sorted(LEAK_MARKERS):
        if marker in answer:
            raise AnswerStructureError(f"answer leaks prompted-format text: {marker!r}")
    if stripped.startswith("{"):
        raise AnswerStructureError("answer is a raw JSON echo")
    if len(stripped) < MIN_ANSWER_CHARS:
        raise AnswerStructureError(f"answer is shorter than {MIN_ANSWER_CHARS} characters")
    return stripped


__all__ = [
    "LEAK_MARKERS",
    "MIN_ANSWER_CHARS",
    "AnswerStructureError",
    "validate_answer_structure",
]
