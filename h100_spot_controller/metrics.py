"""CloudWatch metric payloads for reconciliation, without changing capacity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .outcomes import ReconciliationOutcome
from .signals import CandidateSignalSnapshot
from .region_selection import RegionSelectionResult, RegionSignalSnapshot

NAMESPACE = "H100SpotCapacityController"


def metric_data(
    outcome: ReconciliationOutcome,
    realized_h100_gpu_count: int,
    active_zone_count: int,
    *,
    accelerator_profile: str = "gpu-production",
    realized_accelerator_count: int | None = None,
    accelerator_counts_by_model: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    dimensions = [
        {"Name": "TargetId", "Value": outcome.target_id},
        {"Name": "Region", "Value": outcome.active_region},
        {"Name": "AcceleratorProfile", "Value": accelerator_profile},
    ]
    realized_accelerator_count = realized_h100_gpu_count if realized_accelerator_count is None else realized_accelerator_count
    data = [
        {"MetricName": "DesiredMachineCapacity", "Unit": "Count", "Value": outcome.desired_machine_count, "Dimensions": dimensions},
        {"MetricName": "FulfilledMachineCapacity", "Unit": "Count", "Value": outcome.fulfilled_machine_count, "Dimensions": dimensions},
        {"MetricName": "MachineShortfall", "Unit": "Count", "Value": max(0, outcome.desired_machine_count - outcome.fulfilled_machine_count), "Dimensions": dimensions},
        {"MetricName": "RealizedAcceleratorCount", "Unit": "Count", "Value": realized_accelerator_count, "Dimensions": dimensions},
        {"MetricName": "ActiveZoneCount", "Unit": "Count", "Value": active_zone_count, "Dimensions": dimensions},
        {"MetricName": "ReconciliationOutcome", "Unit": "Count", "Value": 1, "Dimensions": [*dimensions, {"Name": "Outcome", "Value": outcome.kind}]},
    ]
    for model, count in sorted((accelerator_counts_by_model or {}).items()):
        data.append({
            "MetricName": "RealizedAcceleratorModelCount", "Unit": "Count", "Value": count,
            "Dimensions": [*dimensions, {"Name": "AcceleratorModel", "Value": model}],
        })
    return data


def publish_metrics(
    cloudwatch: Any,
    outcome: ReconciliationOutcome,
    realized_h100_gpu_count: int,
    active_zone_count: int,
    *,
    accelerator_profile: str = "gpu-production",
    realized_accelerator_count: int | None = None,
    accelerator_counts_by_model: dict[str, int] | None = None,
) -> None:
    cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data(
        outcome, realized_h100_gpu_count, active_zone_count,
        accelerator_profile=accelerator_profile,
        realized_accelerator_count=realized_accelerator_count,
        accelerator_counts_by_model=accelerator_counts_by_model,
    ))


def signal_metric_data(target_id: str, snapshot: CandidateSignalSnapshot, accelerator_profile: str = "gpu-production") -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    for region, observation in snapshot.sps_by_region.items():
        if observation.status != "ok":
            data.append({"MetricName": "CapacitySignalError", "Unit": "Count", "Value": 1, "Dimensions": [
                {"Name": "TargetId", "Value": target_id}, {"Name": "Region", "Value": region},
                {"Name": "AcceleratorProfile", "Value": accelerator_profile},
                {"Name": "SignalType", "Value": "sps"}, {"Name": "Status", "Value": observation.status},
                {"Name": "ErrorCode", "Value": observation.error_code or "unknown"},
            ]})
        for score in observation.scores:
            dimensions = [{"Name": "TargetId", "Value": target_id}, {"Name": "Region", "Value": region}, {"Name": "AcceleratorProfile", "Value": accelerator_profile}]
            location = score.get("AvailabilityZoneId") or score.get("Region") or region
            data.append({"MetricName": "SpotPlacementScore", "Unit": "None", "Value": score.get("Score", 0), "Dimensions": [*dimensions, {"Name": "Location", "Value": location}]})
    for region, observation in snapshot.price_by_region.items():
        if observation.status != "ok":
            data.append({"MetricName": "CapacitySignalError", "Unit": "Count", "Value": 1, "Dimensions": [
                {"Name": "TargetId", "Value": target_id}, {"Name": "Region", "Value": region},
                {"Name": "AcceleratorProfile", "Value": accelerator_profile},
                {"Name": "SignalType", "Value": "spot-price"}, {"Name": "Status", "Value": observation.status},
                {"Name": "ErrorCode", "Value": observation.error_code or "unknown"},
            ]})
        for price in observation.prices:
            data.append({"MetricName": "SpotPriceUsd", "Unit": "None", "Value": float(price["SpotPrice"]), "Dimensions": [
                {"Name": "TargetId", "Value": target_id}, {"Name": "Region", "Value": region},
                {"Name": "AcceleratorProfile", "Value": accelerator_profile},
                {"Name": "InstanceType", "Value": price["InstanceType"]}, {"Name": "AvailabilityZone", "Value": price.get("AvailabilityZone", "unknown")},
            ]})
    for region, discovery in snapshot.local_zone_eligibility.items():
        for zone in discovery.zones:
            if zone.zone_type == "local-zone":
                data.append({"MetricName": "LocalZoneEligible", "Unit": "Count", "Value": 1 if zone.eligible else 0, "Dimensions": [
                    {"Name": "TargetId", "Value": target_id}, {"Name": "Region", "Value": region}, {"Name": "AcceleratorProfile", "Value": accelerator_profile}, {"Name": "ZoneId", "Value": zone.zone_id},
                ]})
    return data


def publish_signal_metrics(cloudwatch: Any, target_id: str, snapshot: CandidateSignalSnapshot, accelerator_profile: str = "gpu-production") -> None:
    data = signal_metric_data(target_id, snapshot, accelerator_profile)
    for offset in range(0, len(data), 1000):
        cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=data[offset:offset + 1000])


def region_selection_metric_data(
    target_id: str,
    mode: str,
    selection: RegionSelectionResult,
    snapshot: RegionSignalSnapshot,
    now: datetime,
    pinned_region: str | None = None,
) -> list[dict[str, Any]]:
    base = [{"Name": "TargetId", "Value": target_id}, {"Name": "SelectionMode", "Value": mode}]
    selected = selection.selected_region or "none"
    data = [
        {"MetricName": "EligibleRegionCount", "Unit": "Count", "Value": len(selection.ordered_candidates), "Dimensions": base},
        {"MetricName": "NoEligibleRegion", "Unit": "Count", "Value": 0 if selection.selected_region else 1, "Dimensions": base},
        {"MetricName": "RegionDecision", "Unit": "Count", "Value": 1, "Dimensions": [*base, {"Name": "SelectedRegion", "Value": selected}]},
        {
            "MetricName": "RecommendationDiffersFromActive", "Unit": "Count",
            "Value": 1 if pinned_region and selection.selected_region and pinned_region != selection.selected_region else 0,
            "Dimensions": base,
        },
    ]
    for region, observation in snapshot.sps_by_region.items():
        age = max(0.0, (now - (observation.observed_at or snapshot.observed_at)).total_seconds())
        data.append({
            "MetricName": "RegionSignalAgeSeconds", "Unit": "Seconds", "Value": age,
            "Dimensions": [*base, {"Name": "Region", "Value": region}],
        })
    return data


def eks_readiness_metric_data(target_id: str, region: str, registered: int, ready: int) -> list[dict[str, Any]]:
    dimensions = [{"Name": "TargetId", "Value": target_id}, {"Name": "Region", "Value": region}]
    return [
        {"MetricName": "EksRegisteredNodeCount", "Unit": "Count", "Value": registered, "Dimensions": dimensions},
        {"MetricName": "EksReadyNodeCount", "Unit": "Count", "Value": ready, "Dimensions": dimensions},
    ]


def operational_metric_data(target_id: str, region: str, failover_state: str, retries: int = 0, interruptions: int = 0, failover_trigger: str = "none") -> list[dict[str, Any]]:
    dimensions = [{"Name": "TargetId", "Value": target_id}, {"Name": "Region", "Value": region}]
    return [
        {"MetricName": "FailoverState", "Unit": "Count", "Value": 1, "Dimensions": [*dimensions, {"Name": "State", "Value": failover_state}]},
        {"MetricName": "FailoverTrigger", "Unit": "Count", "Value": 1, "Dimensions": [*dimensions, {"Name": "Trigger", "Value": failover_trigger}]},
        {"MetricName": "RetryCount", "Unit": "Count", "Value": retries, "Dimensions": dimensions},
        {"MetricName": "InterruptionCount", "Unit": "Count", "Value": interruptions, "Dimensions": dimensions},
    ]


def zone_metric_data(target_id: str, region: str, active_zone_ids: tuple[str, ...], fulfilled_by_zone: dict[str, int]) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    for zone_id in active_zone_ids:
        dimensions = [{"Name": "TargetId", "Value": target_id}, {"Name": "Region", "Value": region}, {"Name": "ZoneId", "Value": zone_id}]
        data.extend([
            {"MetricName": "ZoneActive", "Unit": "Count", "Value": 1, "Dimensions": dimensions},
            {"MetricName": "ZoneMachineCapacity", "Unit": "Count", "Value": fulfilled_by_zone.get(zone_id, 0), "Dimensions": dimensions},
        ])
    return data
