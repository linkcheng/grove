"""Trusted, request-scoped tenant/principal authentication context."""

from __future__ import annotations

import contextvars
import hmac
import re
from dataclasses import dataclass
from enum import StrEnum

from fastapi import HTTPException, Request, status

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+\-]{0,127}$")


class AuthenticationError(ValueError):
    """Raised for an absent, malformed, or conflicting identity context."""


class PrincipalKind(StrEnum):
    HUMAN = "human"
    WORKLOAD = "workload"


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated subject. Roles are deliberately not an authority claim."""

    principal_id: str
    kind: PrincipalKind
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.principal_id):
            raise AuthenticationError("principal identity is invalid")
        if any(not _IDENTIFIER.fullmatch(role) for role in self.roles):
            raise AuthenticationError("principal role is invalid")
        if tuple(sorted(set(self.roles))) != self.roles:
            raise AuthenticationError("principal roles must be unique and sorted")


@dataclass(frozen=True, slots=True)
class ActiveTenantContext:
    """The single tenant and principal bound to one request."""

    tenant_id: str
    principal: Principal
    auth_strength: str = "fixture"

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.tenant_id):
            raise AuthenticationError("tenant identity is invalid")
        if not _IDENTIFIER.fullmatch(self.auth_strength):
            raise AuthenticationError("authentication strength is invalid")


active_tenant_context: contextvars.ContextVar[ActiveTenantContext | None] = contextvars.ContextVar(
    "grove_active_tenant_context", default=None
)


def current_tenant_context() -> ActiveTenantContext:
    context = active_tenant_context.get()
    if context is None:
        raise AuthenticationError("active tenant context is required")
    return context


def _context_from_token(token: str) -> ActiveTenantContext:
    """Parse the explicit deterministic fixture credential."""

    if not token or len(token) > 1024 or any(char.isspace() for char in token):
        raise AuthenticationError("bearer credential is invalid")
    fields = token.split(":")
    if not fields or fields[0].lower() != "fixture":
        raise AuthenticationError("fixture bearer credential is required")
    fields = fields[1:]
    if len(fields) not in {2, 3}:
        raise AuthenticationError("bearer credential is invalid")
    tenant_id, principal_id = fields[:2]
    kind_value = fields[2] if len(fields) == 3 else PrincipalKind.HUMAN.value
    if not isinstance(tenant_id, str) or not isinstance(principal_id, str) or not isinstance(kind_value, str):
        raise AuthenticationError("bearer claims are invalid")
    try:
        kind = PrincipalKind(kind_value)
    except ValueError as exc:
        raise AuthenticationError("principal kind is invalid") from exc
    return ActiveTenantContext(tenant_id=tenant_id, principal=Principal(principal_id, kind))


def _context_from_development_headers(request: Request) -> ActiveTenantContext | None:
    """Read explicit fixture headers; they are never accepted in production."""

    if request.headers.get("x-grove-auth") != "fixture":
        return None
    tenant_id = request.headers.get("x-grove-tenant-id")
    principal_id = request.headers.get("x-grove-principal-id")
    if tenant_id is None and principal_id is None:
        return None
    if tenant_id is None or principal_id is None:
        raise AuthenticationError("tenant and principal headers must be supplied together")
    kind_value = request.headers.get("x-grove-principal-kind", PrincipalKind.HUMAN.value)
    try:
        kind = PrincipalKind(kind_value)
    except ValueError as exc:
        raise AuthenticationError("principal kind is invalid") from exc
    # No roles/scopes can arrive through a client-controlled header.
    if request.headers.get("x-grove-principal-roles") or request.headers.get("x-grove-principal-scopes"):
        raise AuthenticationError("authorization claims must come from the tenant database")
    return ActiveTenantContext(tenant_id=tenant_id, principal=Principal(principal_id, kind))


def _context_from_gateway_headers(request: Request, expected_token: str) -> ActiveTenantContext | None:
    """Parse gateway-injected identity headers behind a shared-secret proof.

    The trust anchor is the deployment-owned secret shared with the
    authenticated gateway; without it these are ordinary client-controlled
    headers and must never construct an identity.
    """

    presented = request.headers.get("x-grove-gateway-auth")
    if presented is None:
        return None
    if not hmac.compare_digest(presented, expected_token):
        raise AuthenticationError("gateway credential is invalid")
    if request.headers.get("x-grove-auth") or request.headers.get("authorization"):
        raise AuthenticationError("gateway credentials must not be combined with other credentials")
    tenant_id = request.headers.get("x-grove-tenant-id")
    principal_id = request.headers.get("x-grove-principal-id")
    if tenant_id is None or principal_id is None:
        raise AuthenticationError("gateway tenant and principal headers are required")
    kind_value = request.headers.get("x-grove-principal-kind", PrincipalKind.HUMAN.value)
    try:
        kind = PrincipalKind(kind_value)
    except ValueError as exc:
        raise AuthenticationError("principal kind is invalid") from exc
    # No roles/scopes can arrive through a client-controlled header.
    if request.headers.get("x-grove-principal-roles") or request.headers.get("x-grove-principal-scopes"):
        raise AuthenticationError("authorization claims must come from the tenant database")
    return ActiveTenantContext(
        tenant_id=tenant_id,
        principal=Principal(principal_id, kind),
        auth_strength="gateway",
    )


def authenticate_request(request: Request) -> ActiveTenantContext:
    """Authenticate one explicit fixture context and bind it to this request."""

    authorization = request.headers.get("authorization")
    bearer_context: ActiveTenantContext | None = None
    if authorization is not None:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credential or not credential.lower().startswith("fixture:"):
            raise AuthenticationError("fixture bearer credential is required")
        bearer_context = _context_from_token(credential)
    header_context = _context_from_development_headers(request)
    if bearer_context is None and header_context is None:
        raise AuthenticationError("authentication is required")
    if bearer_context is not None and header_context is not None and bearer_context != header_context:
        raise AuthenticationError("authentication contexts disagree")
    context = bearer_context or header_context
    assert context is not None
    active_tenant_context.set(context)
    request.state.active_tenant_context = context
    return context


async def require_active_tenant_context(request: Request) -> ActiveTenantContext:
    """FastAPI dependency with fail-closed fixture configuration."""

    settings = getattr(request.app.state, "settings", None)
    auth_mode = getattr(settings, "auth_mode", "disabled")
    if auth_mode == "gateway":
        token = getattr(settings, "gateway_auth_token", None)
        expected = token.get_secret_value() if token is not None else ""
        if not expected:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="authentication unavailable")
        try:
            context = _context_from_gateway_headers(request, expected)
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required") from exc
        if context is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
        active_tenant_context.set(context)
        request.state.active_tenant_context = context
        return context
    if auth_mode != "fixture":
        # An unconfigured adapter must never accept a client identity.  A
        # request without credentials remains a normal 401 for compatibility;
        # supplied credentials receive an explicit unavailable response.
        authorization = request.headers.get("authorization", "")
        if request.headers.get("x-grove-auth") or authorization.lower().startswith("bearer fixture:"):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="authentication unavailable")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    try:
        return authenticate_request(request)
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required") from exc


__all__ = [
    "ActiveTenantContext",
    "AuthenticationError",
    "Principal",
    "PrincipalKind",
    "_context_from_token",
    "active_tenant_context",
    "authenticate_request",
    "current_tenant_context",
    "require_active_tenant_context",
]
