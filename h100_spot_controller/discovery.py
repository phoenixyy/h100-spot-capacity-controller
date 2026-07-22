"""Read-only AZ, Local Zone and instance-offering discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Placement


@dataclass(frozen=True)
class ZoneDiscovery:
    zone_name: str
    zone_id: str
    zone_type: str
    opt_in_status: str | None
    eligible: bool
    reason: str | None
    approved_subnet_id: str | None = None


@dataclass(frozen=True)
class RegionDiscovery:
    region: str
    zones: tuple[ZoneDiscovery, ...]
    offered_instance_types_by_zone: dict[str, tuple[str, ...]]


def discover_region(
    ec2: Any,
    region: str,
    instance_types: list[str],
    approved_local_placements: tuple[Placement, ...] = (),
) -> RegionDiscovery:
    """Read Local Zone prerequisites without opting in or changing networking."""
    zones = ec2.describe_availability_zones(AllAvailabilityZones=True).get("AvailabilityZones", [])
    offerings = ec2.describe_instance_type_offerings(
        LocationType="availability-zone-id",
        Filters=[{"Name": "instance-type", "Values": instance_types}],
    ).get("InstanceTypeOfferings", [])
    offered: dict[str, list[str]] = {}
    for item in offerings:
        offered.setdefault(item["Location"], []).append(item["InstanceType"])
    approved_subnets = {item.subnet_id: item.zone_id for item in approved_local_placements}
    subnets: dict[str, str] = {}
    if approved_subnets:
        response = ec2.describe_subnets(SubnetIds=sorted(approved_subnets))
        subnets = {item["SubnetId"]: item.get("AvailabilityZoneId", "") for item in response.get("Subnets", [])}
    result = []
    for zone in zones:
        kind = zone.get("ZoneType", "availability-zone")
        opt_in = zone.get("OptInStatus")
        zone_id = zone["ZoneId"]
        if kind == "availability-zone":
            eligible, reason, subnet_id = True, None, None
        else:
            subnet_id = next((subnet for subnet, expected_zone in approved_subnets.items() if expected_zone == zone_id and subnets.get(subnet) == zone_id), None)
            has_offering = bool(set(offered.get(zone_id, ())) & set(instance_types))
            eligible = opt_in == "opted-in" and subnet_id is not None and has_offering
            if eligible:
                reason = None
            elif opt_in != "opted-in":
                reason = "not-opted-in"
            elif subnet_id is None:
                reason = "approved-subnet-unavailable"
            else:
                reason = "h100-offering-unavailable"
        result.append(ZoneDiscovery(zone["ZoneName"], zone_id, kind, opt_in, eligible, reason, subnet_id))
    return RegionDiscovery(region, tuple(result), {key: tuple(sorted(value)) for key, value in offered.items()})


def ec2_client(profile: str, region: str) -> Any:
    import boto3
    return boto3.Session(profile_name=profile, region_name=region).client("ec2")
