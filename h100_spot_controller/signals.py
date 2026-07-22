"""Read-only SPS collection with an explicit rate-limited degraded mode."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from .config import CapacityTarget
from .discovery import RegionDiscovery


@dataclass(frozen=True)
class SpsObservation:
    status: str  # ok | rate_limited | failed
    region: str
    scores: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None
    observed_at: datetime | None = None
    request_fingerprint: str | None = None


@dataclass(frozen=True)
class SpotPriceObservation:
    status: str
    region: str
    prices: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class CandidateSignalSnapshot:
    """Advisory signals; these fields never authorize a Region failover."""
    sps_by_region: dict[str, SpsObservation]
    price_by_region: dict[str, SpotPriceObservation]
    local_zone_eligibility: dict[str, RegionDiscovery]
    observed_at: datetime | None = None
    request_fingerprint: str | None = None


@dataclass(frozen=True)
class SpsRequest:
    """A stable, comparable SPS request and its content fingerprint."""

    instance_types: tuple[str, ...]
    target_capacity: int
    region_names: tuple[str, ...]
    single_availability_zone: bool
    target_capacity_unit_type: str = "units"

    def as_api_kwargs(self) -> dict[str, Any]:
        return {
            "InstanceTypes": list(self.instance_types),
            "TargetCapacity": self.target_capacity,
            "TargetCapacityUnitType": self.target_capacity_unit_type,
            "RegionNames": list(self.region_names),
            "SingleAvailabilityZone": self.single_availability_zone,
        }

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(self.as_api_kwargs(), sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()


def build_sps_request(
    target: CapacityTarget,
    regions: tuple[str, ...],
    *,
    single_availability_zone: bool = False,
) -> SpsRequest:
    """Build one exact SPS configuration reused across collection and selection."""
    return SpsRequest(
        instance_types=tuple(item.name for item in target.instance_types),
        target_capacity=target.desired_instance_count,
        region_names=regions,
        single_availability_zone=single_availability_zone,
    )


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        value = response.get("Error", {}).get("Code")
        return str(value) if value else None
    return None


def collect_sps(client: Any, target: CapacityTarget, region: str, now: datetime | None = None) -> SpsObservation:
    """Collect exactly one stable configuration; never mutate AWS or retry new configs."""
    observed_at = now or datetime.now(timezone.utc)
    request = build_sps_request(target, (region,), single_availability_zone=True)
    try:
        response = client.get_spot_placement_scores(**request.as_api_kwargs())
    except Exception as error:
        code = _error_code(error)
        if code == "MaxConfigLimitExceeded":
            return SpsObservation("rate_limited", region, error_code=code, observed_at=observed_at, request_fingerprint=request.fingerprint)
        return SpsObservation("failed", region, error_code=code or type(error).__name__, observed_at=observed_at, request_fingerprint=request.fingerprint)
    return SpsObservation("ok", region, tuple(response.get("SpotPlacementScores", ())), observed_at=observed_at, request_fingerprint=request.fingerprint)


def collect_sps_regions(
    client: Any,
    target: CapacityTarget,
    regions: tuple[str, ...],
    now: datetime | None = None,
    *,
    single_availability_zone: bool = False,
) -> dict[str, SpsObservation]:
    """Use one stable SPS configuration for all candidate Regions."""
    observed_at = now or datetime.now(timezone.utc)
    request = build_sps_request(target, regions, single_availability_zone=single_availability_zone)
    try:
        response = client.get_spot_placement_scores(**request.as_api_kwargs())
    except Exception as error:
        code = _error_code(error)
        status = "rate_limited" if code == "MaxConfigLimitExceeded" else "failed"
        return {
            region: SpsObservation(
                status, region, error_code=code or type(error).__name__,
                observed_at=observed_at, request_fingerprint=request.fingerprint,
            )
            for region in regions
        }
    grouped = {region: [] for region in regions}
    for score in response.get("SpotPlacementScores", ()):
        if not isinstance(score, dict):
            continue
        region = score.get("Region")
        if region in grouped:
            grouped[region].append(score)
    return {
        region: SpsObservation(
            "ok", region, tuple(grouped[region]), observed_at=observed_at,
            request_fingerprint=request.fingerprint,
        )
        for region in regions
    }


def collect_spot_prices(client: Any, target: CapacityTarget, region: str, now: datetime | None = None) -> SpotPriceObservation:
    """Collect a single read-only price snapshot for standard and Local Zone pools.

    Local Zone prices are intentionally observations only: unlike SPS, they are
    not used as a score or an automatic Region-failover authorization.
    """
    observed_at = now or datetime.now(timezone.utc)
    try:
        response = client.describe_spot_price_history(
            InstanceTypes=[item.name for item in target.instance_types], ProductDescriptions=["Linux/UNIX"],
            StartTime=observed_at, MaxResults=100,
        )
    except Exception as error:
        return SpotPriceObservation("failed", region, error_code=_error_code(error) or type(error).__name__, observed_at=observed_at)
    return SpotPriceObservation("ok", region, tuple(response.get("SpotPriceHistory", ())), observed_at=observed_at)


def collect_candidate_signals(target: CapacityTarget, sps_clients: dict[str, Any], price_clients: dict[str, Any], discoveries: dict[str, RegionDiscovery]) -> CandidateSignalSnapshot:
    """Collect the same stable SPS configuration for every approved Region."""
    regions = [item.region for item in target.candidate_regions]
    observed_at = datetime.now(timezone.utc)
    request = build_sps_request(target, tuple(regions))
    return CandidateSignalSnapshot(
        sps_by_region=collect_sps_regions(sps_clients[regions[0]], target, tuple(regions), observed_at),
        price_by_region={region: collect_spot_prices(price_clients[region], target, region, observed_at) for region in regions},
        local_zone_eligibility=discoveries,
        observed_at=observed_at,
        request_fingerprint=request.fingerprint,
    )
