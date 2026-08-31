from __future__ import annotations


class AppError(Exception):
    def __init__(self, message: str, *, status: int, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.is_operational = True


class SessionNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__("Sessão não encontrada.", status=404, code="SESSION_NOT_FOUND")


class SessionConflictError(AppError):
    def __init__(self) -> None:
        super().__init__("A sessão já foi encerrada.", status=409, code="SESSION_CLOSED")


class SessionBusyError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Outra mensagem desta sessão ainda está em processamento.",
            status=409,
            code="SESSION_BUSY",
        )


class CoordinationUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "A coordenação da sessão está temporariamente indisponível.",
            status=503,
            code="COORDINATION_UNAVAILABLE",
        )


class IdempotencyConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "A chave de idempotência já foi usada com outra requisição.",
            status=409,
            code="IDEMPOTENCY_CONFLICT",
        )
