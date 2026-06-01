"""DataResult — typed wrapper returned by every scraper method."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass
class DataResult:
    """Typed result from any scraper or data-fetch operation.

    Every scraper method returns a DataResult instead of a raw dict so that
    agents can track provenance, confidence, and fallback history.
    """
    value: Any = None
    source: str = ""
    source_url: str | None = None
    confidence: str = "inferred"   # "verified" | "inferred" | "unavailable"
    error: str | None = None
    fallback_used: bool = False
    fallback_method: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    manual_check_url: str | None = None

    @property
    def ok(self) -> bool:
        """True when value is present and no error occurred."""
        return self.error is None and self.value is not None

    def to_dict(self) -> dict:
        """Serialise to plain dict (safe for JSON / DB storage)."""
        return asdict(self)
