"""Expected business errors crossing the HTTP boundary."""

from __future__ import annotations


class AppError(Exception):
    """A safe, typed business failure with an HTTP transport status."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        error_code: str,
        status_code: int = 200,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.retry_after = retry_after


class PermissionDeniedError(AppError):
    def __init__(self) -> None:
        super().__init__(40301, "permission denied", error_code="PermissionDenied")


# Compatibility name for callers that imported the early WS-2 draft class.
PrincipalDeniedError = PermissionDeniedError


class RunNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(40401, "run not found", error_code="RunNotFound")


class CommandNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(40402, "command not found", error_code="CommandNotFound")


class SubmissionConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(40901, "submission conflict", error_code="SubmissionConflict")


class CommandConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(40902, "command conflict", error_code="CommandConflict")


class RunStateConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(40903, "run state conflict", error_code="RunStateConflict")


class PlanChangedError(AppError):
    def __init__(self) -> None:
        super().__init__(40904, "execution plan changed", error_code="PlanChanged")


class EvaluationGateFailedError(AppError):
    def __init__(self) -> None:
        super().__init__(41201, "evaluation gate failed", error_code="EvaluationGateFailed")


class DependencyUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            50302,
            "dependency unavailable",
            error_code="DependencyUnavailable",
            status_code=503,
            retry_after=1,
        )
