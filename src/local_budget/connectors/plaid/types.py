"""Internal normalized facts. Never serialize these DTOs to public output."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TxnFact:
    transaction_id: str = field(repr=False)
    account_id: str = field(repr=False)
    posted_date: str
    amount_cents: int
    merchant_norm: str
    payee: str
    pending: bool
    currency: str = 'USD'


@dataclass(frozen=True)
class SyncBatch:
    original_cursor: str | None = field(repr=False)
    next_cursor: str = field(repr=False)
    events: tuple[tuple[str, TxnFact | str], ...] = field(repr=False)
    readiness: str
    request_id: str | None


@dataclass(frozen=True)
class SyncResult:
    outcome: str
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    review_count: int = 0


LIFECYCLE_STATES = frozenset({
    'disabled', 'active', 'reconnect_required', 'recovery_required',
    'disconnect_pending', 'disconnected', 'replaced', 'cleanup_needed',
})
