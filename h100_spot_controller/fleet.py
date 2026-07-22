"""Owned EC2 Fleet reconciliation with conservative write boundaries."""

from __future__ import annotations

from hashlib import sha256
from dataclasses import dataclass, replace
import json
from typing import Any, Iterable

from .config import CapacityTarget, ConfigurationError
from .outcomes import ReconciliationOutcome


INACTIVE_FLEET_STATES = {"deleted", "deleted_running", "deleted_terminating"}


def _fleet_state(fleet: dict[str, Any]) -> str | None:
    """Return the EC2 DescribeFleets state, retaining legacy fixture support."""
    return fleet.get("FleetState", fleet.get("State"))


def _tag_map(tags: Iterable[dict[str, str]]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in tags}


def is_owned_fleet(fleet: dict[str, Any], target: CapacityTarget) -> bool:
    """Ownership is tag-based; never adopt an untagged or foreign fleet."""
    return _tag_map(fleet.get("Tags", [])) == {**_tag_map(fleet.get("Tags", [])), **target.tags}


def find_owned_fleets(ec2: Any, target: CapacityTarget) -> list[dict[str, Any]]:
    """Read every fleet page and return all active fleets owned by target."""
    fleets: list[dict[str, Any]] = []
    if hasattr(ec2, "get_paginator"):
        for page in ec2.get_paginator("describe_fleets").paginate():
            fleets.extend(page.get("Fleets", []))
    else:
        fleets.extend(ec2.describe_fleets().get("Fleets", []))
    return [fleet for fleet in fleets if is_owned_fleet(fleet, target) and _fleet_state(fleet) not in INACTIVE_FLEET_STATES]


def find_owned_fleet(ec2: Any, target: CapacityTarget) -> dict[str, Any] | None:
    """Return the sole active owned fleet, rejecting an ownership invariant breach."""
    owned = find_owned_fleets(ec2, target)
    if len(owned) > 1:
        raise RuntimeError(f"target {target.target_id} has more than one owned fleet")
    return owned[0] if owned else None


@dataclass(frozen=True)
class FleetCapacityObservation:
    by_zone: dict[str, int]
    by_instance_type: dict[str, int]
    realized_accelerator_count: int
    accelerator_counts_by_model: dict[str, int]
    realized_h100_gpu_count: int
    owned_instance_ids: tuple[str, ...]


def observe_fleet_capacity(ec2: Any, fleet: dict[str, Any], target: CapacityTarget) -> FleetCapacityObservation:
    """Count running/pending owned Fleet instances by Zone and instance type."""
    response = ec2.describe_fleet_instances(FleetId=fleet["FleetId"])
    instance_ids = [item["InstanceId"] for item in response.get("ActiveInstances", []) if item.get("InstanceId")]
    if not instance_ids:
        return FleetCapacityObservation({}, {}, 0, {}, 0, ())
    described = ec2.describe_instances(InstanceIds=instance_ids)
    instances = [instance for reservation in described.get("Reservations", []) for instance in reservation.get("Instances", [])]
    zone_names = {
        instance.get("Placement", {}).get("AvailabilityZone")
        for instance in instances
        if not instance.get("Placement", {}).get("AvailabilityZoneId") and instance.get("Placement", {}).get("AvailabilityZone")
    }
    zone_ids_by_name: dict[str, str] = {}
    if zone_names:
        zones = ec2.describe_availability_zones(ZoneNames=sorted(zone_names)).get("AvailabilityZones", [])
        zone_ids_by_name = {item["ZoneName"]: item["ZoneId"] for item in zones}
    counts: dict[str, int] = {}
    by_type: dict[str, int] = {}
    accelerator_catalog = {
        item.name: (item.accelerator_model, item.accelerator_count, item.h100_gpu_count)
        for item in target.instance_types
    }
    realized_accelerators = 0
    accelerators_by_model: dict[str, int] = {}
    realized_h100_gpus = 0
    owned_ids: list[str] = []
    for instance in instances:
        if instance.get("State", {}).get("Name") not in {"pending", "running"}:
            continue
        tags = _tag_map(instance.get("Tags", []))
        if not all(tags.get(key) == value for key, value in target.tags.items()):
            continue
        placement = instance.get("Placement", {})
        zone_id = placement.get("AvailabilityZoneId") or zone_ids_by_name.get(placement.get("AvailabilityZone", ""))
        if zone_id:
            owned_ids.append(instance["InstanceId"])
            counts[zone_id] = counts.get(zone_id, 0) + 1
            instance_type = instance.get("InstanceType")
            if instance_type in accelerator_catalog:
                by_type[instance_type] = by_type.get(instance_type, 0) + 1
                model, accelerator_count, h100_count = accelerator_catalog[instance_type]
                realized_accelerators += accelerator_count
                accelerators_by_model[model] = accelerators_by_model.get(model, 0) + accelerator_count
                realized_h100_gpus += h100_count
    return FleetCapacityObservation(
        counts, by_type, realized_accelerators, accelerators_by_model,
        realized_h100_gpus, tuple(sorted(owned_ids)),
    )


def fulfilled_by_zone(ec2: Any, fleet: dict[str, Any], target: CapacityTarget) -> dict[str, int]:
    return observe_fleet_capacity(ec2, fleet, target).by_zone


def find_owned_instances(ec2: Any, target: CapacityTarget) -> list[dict[str, Any]]:
    """Find all non-terminated target instances, even after their Fleet is gone."""
    filters = [
        {"Name": f"tag:{key}", "Values": [value]} for key, value in target.tags.items()
    ] + [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]}]
    pages = ec2.get_paginator("describe_instances").paginate(Filters=filters) if hasattr(ec2, "get_paginator") else [ec2.describe_instances(Filters=filters)]
    instances = [instance for page in pages for reservation in page.get("Reservations", []) for instance in reservation.get("Instances", [])]
    return [instance for instance in instances if all(_tag_map(instance.get("Tags", [])).get(key) == value for key, value in target.tags.items())]


def owned_capacity_inventory(ec2_by_region: dict[str, Any], target: CapacityTarget) -> list[dict[str, Any]]:
    """Read owned Fleets and non-terminated instances in every candidate Region.

    This deliberately returns every owned Fleet instead of enforcing the
    single-Fleet invariant so incident and cleanup evidence cannot hide a
    duplicate. It makes no AWS write.
    """
    inventory: list[dict[str, Any]] = []
    for inputs in target.candidate_regions:
        regional_target = replace(target, active_region=inputs.region)
        fleets = find_owned_fleets(ec2_by_region[inputs.region], regional_target)
        instances = find_owned_instances(ec2_by_region[inputs.region], regional_target)
        inventory.append({
            "region": inputs.region,
            "owned_fleets": [{
                "fleet_id": fleet.get("FleetId"),
                "state": _fleet_state(fleet),
                "type": fleet.get("Type"),
                "target_capacity": fleet.get("TargetCapacitySpecification", {}).get("TotalTargetCapacity"),
                "create_time": fleet.get("CreateTime"),
                "tags": fleet.get("Tags", []),
            } for fleet in fleets],
            "owned_instances": [{
                "instance_id": instance.get("InstanceId"),
                "state": instance.get("State", {}).get("Name"),
                "instance_type": instance.get("InstanceType"),
                "availability_zone": instance.get("Placement", {}).get("AvailabilityZone"),
                "availability_zone_id": instance.get("Placement", {}).get("AvailabilityZoneId"),
                "launch_time": instance.get("LaunchTime"),
                "spot_instance_request_id": instance.get("SpotInstanceRequestId"),
                "tags": instance.get("Tags", []),
            } for instance in instances],
        })
    return inventory


def fleet_request(target: CapacityTarget, active_zone_ids: tuple[str, ...], price_caps: dict[str, Any] | None = None, request_epoch: str | None = None) -> dict[str, Any]:
    """Build one maintain-type, Spot-only request with machine weight one."""
    region = target.region_inputs(target.active_region)
    placements_by_zone = {p.zone_id: p for p in (*region.standard_placements, *region.local_zone_placements)}
    placements = [placements_by_zone[zone_id] for zone_id in active_zone_ids if zone_id in placements_by_zone]
    if not placements:
        placements = [region.standard_placements[0]]
    overrides = []
    for priority, placement in enumerate(placements):
        for instance in target.instance_types:
            cap = (price_caps or {}).get(instance.name, instance.spot_price_cap_usd)
            if cap is None:
                raise ConfigurationError(f"no resolved Linux On-Demand Spot ceiling for {instance.name}")
            overrides.append({
                "InstanceType": instance.name,
                "SubnetId": placement.subnet_id,
                "MaxPrice": str(cap),
                "WeightedCapacity": 1.0,
                "Priority": priority,
            })
    request = {
        "Type": "maintain", "TargetCapacitySpecification": {"TotalTargetCapacity": target.desired_instance_count, "DefaultTargetCapacityType": "spot"},
        "SpotOptions": {"AllocationStrategy": "capacity-optimized-prioritized", "InstanceInterruptionBehavior": "terminate"},
        "LaunchTemplateConfigs": [{"LaunchTemplateSpecification": {"LaunchTemplateId": region.launch_template_id, "Version": region.launch_template_version}, "Overrides": overrides}],
        "TagSpecifications": [{"ResourceType": "fleet", "Tags": [{"Key": key, "Value": value} for key, value in target.tags.items()]}],
    }
    token_material = json.dumps({"request": request, "epoch": request_epoch}, sort_keys=True, separators=(",", ":"))
    return {"ClientToken": sha256(token_material.encode()).hexdigest()[:64], **request}


def _same_launch_template_configs(fleet: dict[str, Any], request: dict[str, Any]) -> bool:
    """Compare desired launch inputs after normalizing the EC2 response shape.

    ``DescribeFleets`` returns an Availability Zone name even when ``CreateFleet``
    used an Availability Zone ID, and serializes priority/weight as floats. The
    approved subnet already fixes the Zone, so those representation differences
    must not cause an unnecessary ``ModifyFleet`` call every reconciliation.
    """
    def normalized(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for config in configs:
            specification = config.get("LaunchTemplateSpecification", {})
            overrides = [{
                "InstanceType": item.get("InstanceType"),
                "SubnetId": item.get("SubnetId"),
                "MaxPrice": str(item.get("MaxPrice")),
                "WeightedCapacity": float(item.get("WeightedCapacity", 0)),
                "Priority": float(item.get("Priority", 0)),
            } for item in config.get("Overrides", [])]
            result.append({
                "LaunchTemplateId": specification.get("LaunchTemplateId"),
                "Version": str(specification.get("Version")),
                "Overrides": sorted(
                    overrides,
                    key=lambda item: (
                        item["Priority"], item["SubnetId"] or "",
                        item["InstanceType"] or "", item["MaxPrice"],
                    ),
                ),
            })
        return sorted(result, key=lambda item: (item["LaunchTemplateId"] or "", item["Version"]))

    return normalized(fleet.get("LaunchTemplateConfigs", [])) == normalized(request["LaunchTemplateConfigs"])


def reconcile_fleet(ec2: Any, target: CapacityTarget, fleet_id: str | None, active_zone_ids: tuple[str, ...], fulfilled: int, price_caps: dict[str, Any] | None = None, request_epoch: str | None = None) -> ReconciliationOutcome:
    """Create exactly once, then change a maintain fleet only for a changed pool set.

    ``fleet_id`` is state supplied by the caller.  Discovery is deliberately
    separate so a state/configuration mismatch can be surfaced before writes.
    """
    if not target.enabled:
        return ReconciliationOutcome("disabled", target.target_id, target.active_region, target.desired_instance_count, fulfilled)
    request = fleet_request(target, active_zone_ids, price_caps, request_epoch)
    if fleet_id is None:
        ec2.create_fleet(**request)
    return ReconciliationOutcome("healthy" if fulfilled >= target.desired_instance_count else "shortfall", target.target_id, target.active_region, target.desired_instance_count, fulfilled)


def reconcile_existing_fleet(ec2: Any, target: CapacityTarget, fleet: dict[str, Any], active_zone_ids: tuple[str, ...], fulfilled: int, price_caps: dict[str, Any] | None = None) -> ReconciliationOutcome:
    """Modify only an owned maintain fleet when the eligible pools changed.

    The desired machine count is always copied unchanged from the validated
    target.  This protects Zone expansion from becoming an accidental scale-up.
    """
    if not target.enabled:
        return ReconciliationOutcome("disabled", target.target_id, target.active_region, target.desired_instance_count, fulfilled)
    if not is_owned_fleet(fleet, target):
        return ReconciliationOutcome("ownership_mismatch", target.target_id, target.active_region, target.desired_instance_count, fulfilled)
    if fleet.get("Type") != "maintain":
        return ReconciliationOutcome("invalid_fleet", target.target_id, target.active_region, target.desired_instance_count, fulfilled)
    request = fleet_request(target, active_zone_ids, price_caps)
    current_target = fleet.get("TargetCapacitySpecification", {}).get("TotalTargetCapacity")
    if current_target is not None and current_target > target.desired_instance_count and not target.excess_instance_termination:
        return ReconciliationOutcome("configuration_error", target.target_id, target.active_region, target.desired_instance_count, fulfilled, "EXCESS_TERMINATION_APPROVAL_REQUIRED")
    capacity_changed = current_target is not None and current_target != target.desired_instance_count
    if not _same_launch_template_configs(fleet, request) or capacity_changed:
        ec2.modify_fleet(
            FleetId=fleet["FleetId"],
            LaunchTemplateConfigs=request["LaunchTemplateConfigs"],
            TargetCapacitySpecification=request["TargetCapacitySpecification"],
            ExcessCapacityTerminationPolicy="termination" if current_target is not None and current_target > target.desired_instance_count else "no-termination",
        )
    return ReconciliationOutcome("healthy" if fulfilled >= target.desired_instance_count else "shortfall", target.target_id, target.active_region, target.desired_instance_count, fulfilled)


def cleanup_owned_fleet(ec2: Any, target: CapacityTarget, fleet: dict[str, Any] | None, *, explicitly_authorized: bool, terminate_instances: bool = True) -> bool:
    """Stop only a verified owned fleet after a separate caller authorization."""
    if not explicitly_authorized or fleet is None or not is_owned_fleet(fleet, target):
        return False
    ec2.delete_fleets(FleetIds=[fleet["FleetId"]], TerminateInstances=terminate_instances)
    return True


def terminate_owned_instances(ec2: Any, target: CapacityTarget, instances: list[dict[str, Any]], *, explicitly_authorized: bool) -> tuple[str, ...]:
    if not explicitly_authorized:
        return ()
    owned = tuple(sorted(
        instance["InstanceId"] for instance in instances
        if all(_tag_map(instance.get("Tags", [])).get(key) == value for key, value in target.tags.items())
    ))
    if owned:
        ec2.terminate_instances(InstanceIds=list(owned))
    return owned
