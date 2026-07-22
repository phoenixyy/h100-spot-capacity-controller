"""Deterministic, auditable SPS-driven initial Region selection.

This module contains no AWS capacity writes.  Collection normalizes evidence,
selection ranks it, and reconciliation separately decides whether a persisted
decision is safe to consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from hashlib import sha256
from typing import Any

from .config import CapacityTarget
from .discovery import discover_region
from .gpu import describe_gpu_instance_types
from .launch_contract import inspect_launch_contract
from .pricing import ondemand_caps
from .signals import SpsObservation, build_sps_request, collect_spot_prices


NO_ELIGIBLE_REGION = "NO_ELIGIBLE_REGION"
P_SPOT_VCPU_QUOTA_CODE = "L-7212CCBC"
G_AND_VT_SPOT_VCPU_QUOTA_CODE = "L-3819A6DF"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class RegionalReadiness:
    region: str
    launch_contract_ready: bool
    offered_instance_types: tuple[str, ...]
    quota_sufficient: bool
    price_caps_resolved: bool
    price_ratio: Decimal | None = None
    best_standard_az_score: int | None = None
    best_standard_az_count: int = 0
    error_codes: tuple[str, ...] = ()
    observed_at: datetime | None = None
    gpu_metadata: dict[str, dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        gpu_metadata = self.gpu_metadata or {}
        gpu_fingerprint = sha256(json.dumps(gpu_metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() if gpu_metadata else None
        return {
            "region": self.region,
            "launch_contract_ready": self.launch_contract_ready,
            "offered_instance_types": list(self.offered_instance_types),
            "gpu_metadata": gpu_metadata,
            "gpu_metadata_fingerprint": gpu_fingerprint,
            "quota_sufficient": self.quota_sufficient,
            "price_caps_resolved": self.price_caps_resolved,
            "price_ratio": str(self.price_ratio) if self.price_ratio is not None else None,
            "best_standard_az_score": self.best_standard_az_score,
            "best_standard_az_count": self.best_standard_az_count,
            "error_codes": list(self.error_codes),
            "observed_at": _iso(self.observed_at),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RegionalReadiness":
        ratio = raw.get("price_ratio")
        return cls(
            region=raw["region"],
            launch_contract_ready=bool(raw["launch_contract_ready"]),
            offered_instance_types=tuple(raw.get("offered_instance_types", ())),
            quota_sufficient=bool(raw["quota_sufficient"]),
            price_caps_resolved=bool(raw["price_caps_resolved"]),
            price_ratio=Decimal(str(ratio)) if ratio is not None else None,
            best_standard_az_score=int(raw["best_standard_az_score"]) if raw.get("best_standard_az_score") is not None else None,
            best_standard_az_count=int(raw.get("best_standard_az_count", 0)),
            error_codes=tuple(raw.get("error_codes", ())),
            observed_at=_parse_time(raw.get("observed_at")),
            gpu_metadata=dict(raw.get("gpu_metadata", {})),
        )


def _aws_error_code(error: Exception) -> str:
    return str(
        getattr(error, "response", {}).get("Error", {}).get("Code")
        or type(error).__name__
    )


def collect_regional_readiness(
    target: CapacityTarget,
    region: str,
    ec2: Any,
    pricing: Any,
    quota_client: Any,
    now: datetime,
    *,
    az_sps: SpsObservation | None = None,
) -> RegionalReadiness:
    """Normalize read-only prerequisites used as selector hard filters."""
    inputs = target.region_inputs(region)
    errors: list[str] = []

    try:
        launch_ready = inspect_launch_contract(ec2, target, inputs).valid
    except Exception as error:
        launch_ready = False
        errors.append(f"LAUNCH_CONTRACT_{_aws_error_code(error)}")

    offered: set[str] = set()
    try:
        discovery = discover_region(
            ec2, region, [item.name for item in target.instance_types], inputs.local_zone_placements,
        )
        standard_zone_ids = {item.zone_id for item in inputs.standard_placements}
        for zone_id in standard_zone_ids:
            offered.update(discovery.offered_instance_types_by_zone.get(zone_id, ()))
    except Exception as error:
        errors.append(f"OFFERINGS_{_aws_error_code(error)}")

    gpu_metadata: dict[str, dict[str, Any]] = {}
    try:
        descriptors = describe_gpu_instance_types(ec2, tuple(item.name for item in target.instance_types))
        gpu_metadata = {name: descriptor.as_dict() for name, descriptor in descriptors.items()}
    except Exception as error:
        errors.append(f"GPU_METADATA_{_aws_error_code(error)}")

    price_caps_resolved = False
    caps: dict[str, Decimal] = {}
    try:
        caps = ondemand_caps(pricing, region, tuple(item.name for item in target.instance_types))
        price_caps_resolved = bool(caps) and all(value > 0 for value in caps.values())
    except Exception as error:
        errors.append(f"PRICE_CAP_{_aws_error_code(error)}")

    price_ratio: Decimal | None = None
    if price_caps_resolved:
        prices = collect_spot_prices(ec2, target, region, now)
        ratios = []
        if prices.status == "ok":
            for item in prices.prices:
                instance_type = item.get("InstanceType")
                if instance_type in caps:
                    try:
                        ratios.append(Decimal(str(item["SpotPrice"])) / caps[instance_type])
                    except (KeyError, ArithmeticError):
                        continue
        if ratios:
            price_ratio = min(ratios)

    quota_sufficient = False
    try:
        type_response = ec2.describe_instance_types(
            InstanceTypes=[item.name for item in target.instance_types]
        )
        vcpus = [
            int(item["VCpuInfo"]["DefaultVCpus"])
            for item in type_response.get("InstanceTypes", ())
            if item.get("VCpuInfo", {}).get("DefaultVCpus")
        ]
        required_vcpus = min(vcpus) * target.desired_instance_count if vcpus else 0
        quota_code = (
            G_AND_VT_SPOT_VCPU_QUOTA_CODE
            if target.accelerator_profile == "functional-validation"
            else P_SPOT_VCPU_QUOTA_CODE
        )
        quota = quota_client.get_service_quota(ServiceCode="ec2", QuotaCode=quota_code)
        quota_sufficient = required_vcpus > 0 and Decimal(str(quota["Quota"]["Value"])) >= required_vcpus
    except Exception as error:
        errors.append(f"QUOTA_{_aws_error_code(error)}")

    az_values: list[int] = []
    if az_sps and az_sps.status == "ok":
        approved_zones = {item.zone_id for item in inputs.standard_placements}
        az_values = [
            int(item["Score"])
            for item in az_sps.scores
            if item.get("AvailabilityZoneId") in approved_zones
            and isinstance(item.get("Score"), (int, float))
            and not isinstance(item.get("Score"), bool)
        ]
    best_az = max(az_values) if az_values else None
    best_count = sum(value == best_az for value in az_values) if best_az is not None else 0
    return RegionalReadiness(
        region=region,
        launch_contract_ready=launch_ready,
        offered_instance_types=tuple(sorted(offered)),
        quota_sufficient=quota_sufficient,
        price_caps_resolved=price_caps_resolved,
        price_ratio=price_ratio,
        best_standard_az_score=best_az,
        best_standard_az_count=best_count,
        error_codes=tuple(errors),
        observed_at=now,
        gpu_metadata=gpu_metadata,
    )


@dataclass(frozen=True)
class RegionSignalSnapshot:
    snapshot_id: str
    target_id: str
    configuration_version: int
    request_fingerprint: str
    observed_at: datetime
    expires_at: datetime
    sps_by_region: dict[str, SpsObservation]
    readiness_by_region: dict[str, RegionalReadiness]

    def as_item(self) -> dict[str, Any]:
        return {
            "pk": f"TARGET#{self.target_id}",
            "sk": f"REGION_SIGNAL#{self.snapshot_id}",
            "entity_type": "region-signal-snapshot",
            "snapshot_id": self.snapshot_id,
            "target_id": self.target_id,
            "configuration_version": self.configuration_version,
            "request_fingerprint": self.request_fingerprint,
            "observed_at": self.observed_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "ttl": int(self.expires_at.timestamp()),
            "sps_by_region": {
                region: {
                    "status": item.status,
                    "region": item.region,
                    "scores": list(item.scores),
                    "error_code": item.error_code,
                    "observed_at": _iso(item.observed_at),
                    "request_fingerprint": item.request_fingerprint,
                }
                for region, item in self.sps_by_region.items()
            },
            "readiness_by_region": {
                region: item.as_dict() for region, item in self.readiness_by_region.items()
            },
        }

    @classmethod
    def from_item(cls, raw: dict[str, Any]) -> "RegionSignalSnapshot":
        return cls(
            snapshot_id=raw["snapshot_id"],
            target_id=raw["target_id"],
            configuration_version=int(raw["configuration_version"]),
            request_fingerprint=raw["request_fingerprint"],
            observed_at=datetime.fromisoformat(raw["observed_at"]),
            expires_at=datetime.fromisoformat(raw["expires_at"]),
            sps_by_region={
                region: SpsObservation(
                    status=item["status"], region=item["region"], scores=tuple(item.get("scores", ())),
                    error_code=item.get("error_code"), observed_at=_parse_time(item.get("observed_at")),
                    request_fingerprint=item.get("request_fingerprint"),
                )
                for region, item in raw.get("sps_by_region", {}).items()
            },
            readiness_by_region={
                region: RegionalReadiness.from_dict(item)
                for region, item in raw.get("readiness_by_region", {}).items()
            },
        )


def build_signal_snapshot(
    target: CapacityTarget,
    configuration_version: int,
    sps_by_region: dict[str, SpsObservation],
    readiness_by_region: dict[str, RegionalReadiness],
    now: datetime,
    *,
    retention_hours: int = 24,
) -> RegionSignalSnapshot:
    regions = tuple(item.region for item in target.candidate_regions)
    fingerprint = build_sps_request(target, regions).fingerprint
    identity = f"{target.target_id}|{configuration_version}|{fingerprint}|{now.isoformat()}"
    return RegionSignalSnapshot(
        snapshot_id=sha256(identity.encode("utf-8")).hexdigest()[:32],
        target_id=target.target_id,
        configuration_version=configuration_version,
        request_fingerprint=fingerprint,
        observed_at=now,
        expires_at=now + timedelta(hours=retention_hours),
        sps_by_region=sps_by_region,
        readiness_by_region=readiness_by_region,
    )


@dataclass(frozen=True)
class RankedRegion:
    region: str
    eligible: bool
    sps_score: int | None
    best_standard_az_score: int | None
    best_standard_az_count: int
    price_ratio: Decimal | None
    configured_order: int
    exclusion_reasons: tuple[str, ...] = ()

    @property
    def comparison_key(self) -> tuple[Any, ...]:
        return (
            -(self.sps_score if self.sps_score is not None else -1),
            -(self.best_standard_az_score if self.best_standard_az_score is not None else -1),
            -self.best_standard_az_count,
            self.price_ratio if self.price_ratio is not None else Decimal("Infinity"),
            self.configured_order,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "eligible": self.eligible,
            "sps_score": self.sps_score,
            "best_standard_az_score": self.best_standard_az_score,
            "best_standard_az_count": self.best_standard_az_count,
            "price_ratio": str(self.price_ratio) if self.price_ratio is not None else None,
            "configured_order": self.configured_order,
            "exclusion_reasons": list(self.exclusion_reasons),
        }


@dataclass(frozen=True)
class RegionSelectionResult:
    selected_region: str | None
    ordered_candidates: tuple[RankedRegion, ...]
    excluded_candidates: tuple[RankedRegion, ...]
    reason: str


def _score(observation: SpsObservation | None) -> int | None:
    values = [item.get("Score") for item in observation.scores] if observation else []
    numeric = [int(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return max(numeric) if numeric else None


def select_region(target: CapacityTarget, snapshot: RegionSignalSnapshot, now: datetime) -> RegionSelectionResult:
    """Apply hard filters, then the documented lexicographic ranking."""
    expected_fingerprint = build_sps_request(
        target, tuple(item.region for item in target.candidate_regions)
    ).fingerprint
    ranked: list[RankedRegion] = []
    excluded: list[RankedRegion] = []
    max_age = timedelta(minutes=target.region_selection.signal_max_age_minutes)
    approved_types = {item.name for item in target.instance_types}

    for order, inputs in enumerate(target.candidate_regions):
        observation = snapshot.sps_by_region.get(inputs.region)
        readiness = snapshot.readiness_by_region.get(inputs.region)
        reasons: list[str] = []
        score = _score(observation)
        if snapshot.configuration_version < 1:
            reasons.append("INVALID_CONFIGURATION_VERSION")
        if snapshot.request_fingerprint != expected_fingerprint:
            reasons.append("SNAPSHOT_FINGERPRINT_MISMATCH")
        if observation is None:
            reasons.append("SPS_MISSING")
        else:
            if observation.status != "ok":
                reasons.append(f"SPS_{observation.status.upper()}")
            if observation.request_fingerprint != expected_fingerprint:
                reasons.append("SPS_FINGERPRINT_MISMATCH")
            if observation.observed_at is None or now - observation.observed_at > max_age or observation.observed_at > now + timedelta(minutes=1):
                reasons.append("SPS_STALE")
            if score is None:
                reasons.append("SPS_SCORE_MISSING")
        if readiness is None:
            reasons.append("READINESS_MISSING")
        else:
            if not readiness.launch_contract_ready:
                reasons.append("LAUNCH_CONTRACT_INVALID")
            # ``None`` denotes a legacy persisted/read-only snapshot that predates
            # this field. New collector output uses {} for a failed verification.
            if readiness.gpu_metadata is not None and not readiness.gpu_metadata:
                reasons.append("GPU_METADATA_INVALID")
            if not (approved_types & set(readiness.offered_instance_types)):
                reasons.append("INSTANCE_TYPE_NOT_OFFERED")
            if not readiness.quota_sufficient:
                reasons.append("QUOTA_INSUFFICIENT")
            if not readiness.price_caps_resolved:
                reasons.append("PRICE_CAP_UNRESOLVED")
            reasons.extend(readiness.error_codes)
        candidate = RankedRegion(
            region=inputs.region,
            eligible=not reasons,
            sps_score=score,
            best_standard_az_score=readiness.best_standard_az_score if readiness else None,
            best_standard_az_count=readiness.best_standard_az_count if readiness else 0,
            price_ratio=readiness.price_ratio if readiness else None,
            configured_order=order,
            exclusion_reasons=tuple(dict.fromkeys(reasons)),
        )
        (ranked if candidate.eligible else excluded).append(candidate)

    ranked.sort(key=lambda item: item.comparison_key)
    if not ranked:
        return RegionSelectionResult(None, (), tuple(excluded), NO_ELIGIBLE_REGION)
    selected = ranked[0]
    return RegionSelectionResult(
        selected.region, tuple(ranked), tuple(excluded),
        f"selected {selected.region} by SPS/AZ/price/configured-order ranking",
    )


@dataclass(frozen=True)
class RegionDecision:
    target_id: str
    configuration_version: int
    request_fingerprint: str
    decision_version: int
    mode: str
    selected_region: str | None
    ordered_regions: tuple[str, ...]
    exclusions: dict[str, tuple[str, ...]]
    snapshot_id: str
    reason: str
    created_at: datetime
    expires_at: datetime
    applied_at: datetime | None = None

    def is_consumable(self, target: CapacityTarget, now: datetime) -> bool:
        expected = build_sps_request(
            target, tuple(item.region for item in target.candidate_regions)
        ).fingerprint
        return (
            target.enabled
            and target.region_selection.mode == "auto_initial"
            and self.mode == "auto_initial"
            and self.selected_region is not None
            and self.selected_region in {item.region for item in target.candidate_regions}
            and self.request_fingerprint == expected
            and self.expires_at > now
        )

    def as_item(self) -> dict[str, Any]:
        item = {
            "pk": f"TARGET#{self.target_id}", "sk": "REGION_DECISION",
            "entity_type": "region-decision", "target_id": self.target_id,
            "configuration_version": self.configuration_version,
            "request_fingerprint": self.request_fingerprint,
            "decision_version": self.decision_version, "mode": self.mode,
            "selected_region": self.selected_region, "ordered_regions": list(self.ordered_regions),
            "exclusions": {key: list(value) for key, value in self.exclusions.items()},
            "snapshot_id": self.snapshot_id, "reason": self.reason,
            "created_at": self.created_at.isoformat(), "expires_at": self.expires_at.isoformat(),
            "ttl": int(self.expires_at.timestamp()),
        }
        # DynamoDB condition expressions distinguish a missing attribute from a
        # present NULL attribute.  Omit this key until the atomic apply claim.
        if self.applied_at is not None:
            item["applied_at"] = self.applied_at.isoformat()
        return item

    @classmethod
    def from_item(cls, raw: dict[str, Any]) -> "RegionDecision":
        return cls(
            target_id=raw["target_id"], configuration_version=int(raw["configuration_version"]),
            request_fingerprint=raw["request_fingerprint"], decision_version=int(raw["decision_version"]),
            mode=raw["mode"], selected_region=raw.get("selected_region"),
            ordered_regions=tuple(raw.get("ordered_regions", ())),
            exclusions={key: tuple(value) for key, value in raw.get("exclusions", {}).items()},
            snapshot_id=raw["snapshot_id"], reason=raw["reason"],
            created_at=datetime.fromisoformat(raw["created_at"]),
            expires_at=datetime.fromisoformat(raw["expires_at"]),
            applied_at=_parse_time(raw.get("applied_at")),
        )


def decision_from_selection(
    target: CapacityTarget,
    configuration_version: int,
    decision_version: int,
    snapshot: RegionSignalSnapshot,
    selection: RegionSelectionResult,
    now: datetime,
) -> RegionDecision:
    return RegionDecision(
        target_id=target.target_id,
        configuration_version=configuration_version,
        request_fingerprint=snapshot.request_fingerprint,
        decision_version=decision_version,
        mode=target.region_selection.mode,
        selected_region=selection.selected_region,
        ordered_regions=tuple(item.region for item in selection.ordered_candidates),
        exclusions={item.region: item.exclusion_reasons for item in selection.excluded_candidates},
        snapshot_id=snapshot.snapshot_id,
        reason=selection.reason,
        created_at=now,
        expires_at=now + timedelta(minutes=target.region_selection.decision_ttl_minutes),
    )


@dataclass(frozen=True)
class InitialRegionResolution:
    status: str
    region: str | None
    apply_decision: bool = False
    detail: str | None = None


def resolve_initial_region(
    target: CapacityTarget,
    state: Any,
    inventory: list[dict[str, Any]],
    decision: RegionDecision | None,
    configuration_version: int,
    now: datetime,
) -> InitialRegionResolution:
    """Resolve a safe execution Region without performing any AWS write."""
    occupied = tuple(
        item["region"] for item in inventory
        if item.get("owned_fleets") or item.get("owned_instances")
    )
    if len(occupied) > 1:
        return InitialRegionResolution(
            "ownership_mismatch", None, detail=f"owned capacity exists in multiple Regions: {', '.join(occupied)}",
        )
    if state is not None:
        if occupied and occupied[0] != state.active_region:
            return InitialRegionResolution(
                "ownership_mismatch", None,
                detail=f"runtime Region {state.active_region} conflicts with occupied Region {occupied[0]}",
            )
        return InitialRegionResolution("pinned", state.active_region)
    if occupied:
        return InitialRegionResolution("discovered_occupied", occupied[0])
    if target.region_selection.mode == "manual":
        return InitialRegionResolution("manual", target.active_region)
    if target.region_selection.mode == "recommend":
        return InitialRegionResolution(
            "recommendation_only", target.active_region,
            detail="recommend mode never applies selector output",
        )
    if decision is None:
        return InitialRegionResolution("awaiting_region_decision", None)
    if decision.configuration_version != configuration_version:
        return InitialRegionResolution("awaiting_region_decision", None, detail="decision configuration version mismatch")
    if not decision.is_consumable(target, now):
        return InitialRegionResolution("awaiting_region_decision", None, detail="decision is stale or invalid")
    return InitialRegionResolution("selected", decision.selected_region, apply_decision=True)


def snapshot_canonical_json(snapshot: RegionSignalSnapshot) -> str:
    """Stable diagnostic representation useful in dry-run and tests."""
    payload = snapshot.as_item()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
