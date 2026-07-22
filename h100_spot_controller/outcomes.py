"""Structured reconciliation outcomes used for logs, metrics and decisions."""

from dataclasses import dataclass
from typing import Literal

OutcomeKind = Literal[
    "disabled", "healthy", "shortfall", "rate_limited", "configuration_error", "authorization_error", "dependency_error",
    "ownership_mismatch", "invalid_fleet", "awaiting_approval", "source_terminating", "failover_complete",
]


@dataclass(frozen=True)
class ReconciliationOutcome:
    kind: OutcomeKind
    target_id: str
    active_region: str
    desired_machine_count: int
    fulfilled_machine_count: int
    error_code: str | None = None

    @property
    def advances_zone_timer(self) -> bool:
        return self.kind == "shortfall"


def classify_api_error(error: Exception) -> tuple[OutcomeKind, str]:
    """Map AWS-style errors without allowing them to advance capacity timers."""
    code = str(getattr(error, "response", {}).get("Error", {}).get("Code") or error.__class__.__name__)
    lowered = code.lower()
    if any(value in lowered for value in ("accessdenied", "unauthorized", "authfailure", "operationnotpermitted")):
        return "authorization_error", code
    if any(value in lowered for value in ("throttl", "requestlimit", "toomanyrequests", "maxconfiglimit")):
        return "rate_limited", code
    return "dependency_error", code
