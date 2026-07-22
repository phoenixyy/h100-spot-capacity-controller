"""Read-only integration report for the two initial candidate Regions."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from .config import CapacityTarget
from .discovery import discover_region
from .gpu import GpuMetadataError, target_with_gpu_metadata
from .failover import build_failover_plan, plan_as_dict, target_configuration_version
from .fleet import find_owned_fleet, find_owned_instances, fleet_request, observe_fleet_capacity
from .launch_contract import inspect_launch_contract
from .pricing import ondemand_caps
from .signals import collect_sps_regions
from .region_selection import build_signal_snapshot, collect_regional_readiness, decision_from_selection, select_region


G_AND_VT_SPOT_VCPU_QUOTA_CODE = "L-3819A6DF"


def _standard_placement_report(client: Any, inputs: Any, offerings: dict[str, tuple[str, ...]], instance_types: set[str]) -> dict[str, Any]:
    expected = {item.subnet_id: item.zone_id for item in inputs.standard_placements}
    try:
        response = client.describe_subnets(SubnetIds=sorted(expected))
        actual = {item["SubnetId"]: item.get("AvailabilityZoneId") for item in response.get("Subnets", [])}
        return {
            "valid": all(actual.get(subnet_id) == zone_id for subnet_id, zone_id in expected.items()),
            "placements": [{
                "subnet_id": subnet_id, "configured_zone_id": zone_id,
                "discovered_zone_id": actual.get(subnet_id),
                "offers_configured_type": bool(instance_types & set(offerings.get(zone_id, ()))),
            } for subnet_id, zone_id in expected.items()],
        }
    except Exception as error:
        return {"valid": False, "error_code": getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)}


def _spot_quota_report(client: Any | None) -> dict[str, Any]:
    if client is None:
        return {"status": "not-requested"}
    try:
        response = client.get_service_quota(ServiceCode="ec2", QuotaCode=G_AND_VT_SPOT_VCPU_QUOTA_CODE)
        quota = response["Quota"]
        return {"status": "ok", "quota_code": G_AND_VT_SPOT_VCPU_QUOTA_CODE, "value_vcpus": float(quota["Value"]), "adjustable": bool(quota.get("Adjustable", False))}
    except Exception as error:
        return {"status": "error", "quota_code": G_AND_VT_SPOT_VCPU_QUOTA_CODE, "error_code": getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)}


def integration_dry_run(
    target: CapacityTarget,
    ec2_clients: dict[str, Any],
    pricing: Any,
    now: datetime,
    service_quota_clients: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe and plan only; this function contains no capacity-write calls."""
    regions: dict[str, Any] = {}
    active_owned_ids: tuple[str, ...] = ()
    source_impact_error: str | None = None
    region_names = tuple(item.region for item in target.candidate_regions)
    sps_by_region = collect_sps_regions(ec2_clients[region_names[0]], target, region_names, now)
    selection_report = None
    if target.region_selection.mode != "manual" and service_quota_clients:
        az_sps = collect_sps_regions(
            ec2_clients[region_names[0]], target, region_names, now,
            single_availability_zone=True,
        )
        readiness = {
            region: collect_regional_readiness(
                target, region, ec2_clients[region], pricing, service_quota_clients[region], now,
                az_sps=az_sps.get(region),
            )
            for region in region_names
        }
        decision_snapshot = build_signal_snapshot(
            target, target_configuration_version(target), sps_by_region, readiness, now,
        )
        selection = select_region(target, decision_snapshot, now)
        decision = decision_from_selection(
            target, target_configuration_version(target), 1, decision_snapshot, selection, now,
        )
        selection_report = {
            "mode": target.region_selection.mode,
            "pinned_region": target.active_region,
            "recommended_region": selection.selected_region,
            "ordered_candidates": [item.as_dict() for item in selection.ordered_candidates],
            "excluded_candidates": [item.as_dict() for item in selection.excluded_candidates],
            "reason": selection.reason,
            "snapshot_id": decision_snapshot.snapshot_id,
            "request_fingerprint": decision.request_fingerprint,
            "decision_version": decision.decision_version,
            "evidence_observed_at": decision_snapshot.observed_at.isoformat(),
            "decision_expires_at": decision.expires_at.isoformat(),
            "would_apply_automatically": target.region_selection.mode == "auto_initial",
        }
    for inputs in target.candidate_regions:
        client = ec2_clients[inputs.region]
        region_target = replace(target, active_region=inputs.region)
        try:
            region_target, gpu_metadata = target_with_gpu_metadata(region_target, client)
            gpu_report = {name: descriptor.as_dict() for name, descriptor in gpu_metadata.items()}
        except GpuMetadataError as error:
            gpu_report = {"status": "invalid", "error": str(error)}
        discovery = discover_region(client, inputs.region, [item.name for item in target.instance_types], inputs.local_zone_placements)
        try:
            launch_contract = asdict(inspect_launch_contract(client, target, inputs))
        except Exception as error:
            launch_contract = {"valid": False, "error_code": getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)}
        caps = ondemand_caps(pricing, inputs.region, tuple(item.name for item in target.instance_types))
        fleet = find_owned_fleet(client, region_target)
        capacity = observe_fleet_capacity(client, fleet, region_target) if fleet else None
        if inputs.region == target.active_region:
            try:
                active_owned_ids = tuple(sorted(instance["InstanceId"] for instance in find_owned_instances(client, region_target)))
            except Exception as error:
                active_owned_ids = capacity.owned_instance_ids if capacity else ()
                source_impact_error = getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)
        regions[inputs.region] = {
            "sps": asdict(sps_by_region[inputs.region]),
            "on_demand_caps_usd": {key: str(value) for key, value in caps.items()},
            "zones": [asdict(item) for item in discovery.zones],
            "instance_type_offerings_by_zone": discovery.offered_instance_types_by_zone,
            "standard_placements": _standard_placement_report(
                client, inputs, discovery.offered_instance_types_by_zone,
                {item.name for item in target.instance_types},
            ),
            "g_and_vt_spot_vcpu_quota": _spot_quota_report((service_quota_clients or {}).get(inputs.region)),
            "launch_contract": launch_contract,
            "gpu_metadata": gpu_report,
            "owned_fleet_id": fleet.get("FleetId") if fleet else None,
            "fulfilled_by_zone": capacity.by_zone if capacity else {},
            "zone_expansion_order": [item.zone_id for item in (*inputs.standard_placements, *inputs.local_zone_placements)],
            "request_preview": fleet_request(region_target, (), caps),
        }
    automatic_failover = None
    manual_failover = None
    try:
        if target.active_region is None:
            raise ValueError("no operator-selected active Region")
        active_fleet = regions[target.active_region]["owned_fleet_id"]
        version = target_configuration_version(target)
        automatic_failover = plan_as_dict(build_failover_plan(
            target, version, active_owned_ids, now, source_fleet_id=active_fleet,
            notification_topic_arn=target.notification_topic_arn,
            trigger="capacity-shortfall",
        ))
        manual_failover = plan_as_dict(build_failover_plan(
            target, version, active_owned_ids, now, source_fleet_id=active_fleet,
            notification_topic_arn=target.notification_topic_arn,
            trigger="operator-request",
        ))
    except ValueError:
        pass
    return {
        "target_id": target.target_id,
        "accelerator_profile": target.accelerator_profile,
        "configured_instance_types": [item.name for item in target.instance_types],
        "enabled": target.enabled,
        "aws_write": False,
        "active_region": target.active_region,
        "region_selection": selection_report,
        "desired_machine_count": target.desired_instance_count,
        "regions": regions,
        "failover_plan_preview": automatic_failover,
        "automatic_failover_plan_preview": automatic_failover,
        "manual_failover_plan_preview": manual_failover,
        "source_termination_impact": list(active_owned_ids),
        "source_termination_impact_error": source_impact_error,
    }
