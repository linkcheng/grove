"""Closed capability and permission posture semantics for WS-1."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum
from inspect import isawaitable
from typing import Any

from app.skill_abi.runtime import (
    ClosureViolationError,
    MissingCapabilityError,
    PermissionDeniedError,
    PermissionInteractionRequiredError,
    validate_artifact,
)


class PermissionPreset(StrEnum):
    INTERACTIVE = "interactive"
    WORKSPACE_EDIT = "workspace_edit"
    READ_ONLY = "read_only"
    UNATTENDED = "unattended"


class PermissionOutcome(StrEnum):
    AUTO = "AUTO"
    ASK = "ASK"
    DENY = "DENY"


_EFFECTS = {"pure", "read", "workspace_local", "write", "external"}


def require_capabilities(required: Sequence[str], available: Sequence[str]) -> None:
    missing = tuple(sorted(set(required) - set(available)))
    if missing:
        raise MissingCapabilityError(f"missing required capabilities: {', '.join(missing)}")


def intersect_scopes(*scope_sets: Sequence[str]) -> tuple[str, ...]:
    """Return the deterministic intersection of all non-empty scope ceilings."""

    if not scope_sets:
        return ()
    result = set(scope_sets[0])
    for scopes in scope_sets[1:]:
        result.intersection_update(scopes)
    return tuple(sorted(result))


def ensure_scope_subset(requested: Sequence[str], effective: Sequence[str]) -> None:
    missing = tuple(sorted(set(requested) - set(effective)))
    if missing:
        raise PermissionDeniedError(f"requested scopes exceed effective permission: {', '.join(missing)}")


def evaluate_permission(
    preset: PermissionPreset | str,
    effect: str,
    *,
    authorized: bool,
    requires_prompt: bool | None = None,
) -> PermissionOutcome:
    try:
        posture = PermissionPreset(preset)
    except ValueError as exc:
        raise PermissionDeniedError(f"unknown permission preset: {preset}") from exc
    if effect not in _EFFECTS:
        raise PermissionDeniedError(f"unknown effect class: {effect}")
    if not authorized:
        return PermissionOutcome.DENY
    # ``requires_prompt`` is supplied by the versioned effect policy.  It is
    # intentionally evaluated before the default effect posture so a policy
    # cannot be silently bypassed by a normally auto-safe read/pure operation.
    if requires_prompt is True:
        if posture is PermissionPreset.UNATTENDED:
            return PermissionOutcome.DENY
        if posture is PermissionPreset.READ_ONLY and effect in {"workspace_local", "write", "external"}:
            return PermissionOutcome.DENY
        return PermissionOutcome.ASK
    if effect in {"pure", "read"}:
        return PermissionOutcome.AUTO
    if posture is PermissionPreset.READ_ONLY:
        return PermissionOutcome.DENY
    if posture is PermissionPreset.UNATTENDED:
        return PermissionOutcome.DENY
    if posture is PermissionPreset.WORKSPACE_EDIT and effect == "workspace_local":
        return PermissionOutcome.AUTO
    return PermissionOutcome.ASK


def check_permission(
    preset: PermissionPreset | str,
    effect: str,
    *,
    authorized: bool,
    requires_prompt: bool | None = None,
) -> PermissionOutcome:
    outcome = evaluate_permission(
        preset,
        effect,
        authorized=authorized,
        requires_prompt=requires_prompt,
    )
    if outcome is PermissionOutcome.DENY:
        raise PermissionDeniedError(f"permission denied for {effect} under {preset}")
    return outcome


class DisabledAdapter:
    """Final blocking adapter for an optional capability.

    It intentionally raises on every entry point; returning an empty or
    successful value would create a false execution fact.
    """

    def __init__(self, capability: str) -> None:
        self.capability = capability
        self.enabled = False
        self.status = "disabled"

    def invoke(self, *_args: Any, **_kwargs: Any) -> Any:
        from app.skill_abi.runtime import CapabilityUnavailableError

        raise CapabilityUnavailableError(f"capability adapter is disabled: {self.capability}")

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.invoke(*_args, **_kwargs)

    def call(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.invoke(*_args, **_kwargs)

    async def acall(self, *_args: Any, **_kwargs: Any) -> Any:
        return await self.ainvoke(*_args, **_kwargs)


def run_guarded(
    provider_or_adapter: Callable[[], Any] | DisabledAdapter,
    callback: Callable[[], Any] | None = None,
    *,
    abi_version: str | None = None,
    supported_abi_versions: Sequence[str] = ("v1", "v2"),
    artifact: tuple[bytes, str] | None = None,
    allowed_refs: Sequence[str] | None = None,
    proposal_ref: str | None = None,
    required_capabilities: Sequence[str] = (),
    available_capabilities: Sequence[str] = (),
    preset: PermissionPreset | str | None = None,
    effect: str | None = None,
    authorized: bool = True,
    requires_prompt: bool | None = None,
) -> Any:
    """Run a callback only after every pre-provider guard succeeds.

    Checks are intentionally ordered before adapter/provider invocation.  The
    function also accepts ``run_guarded(DisabledAdapter(...), callback)`` so a
    disabled optional seam remains a hard failure.
    """

    provider: Callable[[], Any] | DisabledAdapter = provider_or_adapter
    if isinstance(provider_or_adapter, DisabledAdapter):
        provider = provider_or_adapter
    elif callback is None:
        callback = provider_or_adapter
    if abi_version is not None and abi_version not in supported_abi_versions:
        from app.skill_abi.runtime import UnknownABIVersionError

        raise UnknownABIVersionError(f"unknown ABI version: {abi_version}")
    if artifact is not None:
        validate_artifact(*artifact)
    if proposal_ref is not None and (allowed_refs is None or proposal_ref not in set(allowed_refs)):
        raise ClosureViolationError(f"proposal outside closure: {proposal_ref}")
    require_capabilities(required_capabilities, available_capabilities)
    if preset is not None and effect is not None:
        # ASK is a hard pre-provider boundary, never an implicit bypass.
        outcome = check_permission(preset, effect, authorized=authorized, requires_prompt=requires_prompt)
        if outcome is PermissionOutcome.ASK:
            raise PermissionInteractionRequiredError(f"permission interaction required for {effect} under {preset}")
    if isinstance(provider, DisabledAdapter):
        return provider.invoke()
    if callback is None:
        callback = provider
    result = callback()
    if _is_python_awaitable(result):
        return _await_result(result)
    return result


bootstrap_guard = run_guarded
provider_guard = run_guarded


def _is_python_awaitable(value: Any) -> bool:
    """Use Python's complete awaitable predicate without opening frame attrs.

    ``collections.abc.Awaitable`` does not recognize generator-based
    coroutines created by ``types.coroutine``.  ``inspect.isawaitable`` does,
    while keeping the execution-frame implementation detail outside the
    contract-spine source that the dependency gate scans.
    """

    return isawaitable(value)


def _await_result(value: Any) -> Any:
    """Reject accidental async provider calls at this pure boundary.

    WS-1 only verifies the pre-provider seam; runtime drivers own the event
    loop.  Returning a coroutine here would make a guard look successful, so a
    clear error is preferable to silently scheduling work.
    """

    # Do not leave a native coroutine or generator-based coroutine pending: a
    # caller that catches the guard error must not later receive a warning for
    # an un-awaited object.  All Python awaitable implementations used by this
    # seam expose ``close``; a custom Awaitable without it still receives the
    # deterministic guard failure below.
    try:
        value.close()
    except AttributeError:
        pass
    raise RuntimeError("run_guarded received an async callback; use the runtime async guard")


__all__ = [
    "DisabledAdapter",
    "PermissionOutcome",
    "PermissionPreset",
    "PermissionInteractionRequiredError",
    "check_permission",
    "bootstrap_guard",
    "ensure_scope_subset",
    "evaluate_permission",
    "intersect_scopes",
    "require_capabilities",
    "provider_guard",
    "run_guarded",
]
