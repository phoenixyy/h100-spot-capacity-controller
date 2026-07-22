"""Pure Zone sequencing; this module performs no AWS calls."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import CapacityTarget, Placement


@dataclass(frozen=True)
class PlacementState:
    active_zone_ids: tuple[str, ...]
    fulfilled_by_zone: dict[str, int]
    shortfall_since: datetime | None


def fulfilled_machine_count(state: PlacementState) -> int:
    return sum(max(0, count) for count in state.fulfilled_by_zone.values())


def initial_placement(target: CapacityTarget) -> Placement:
    return target.region_inputs(target.active_region).standard_placements[0]


def next_placement_to_activate(target: CapacityTarget, state: PlacementState, now: datetime) -> Placement | None:
    if not target.enabled or fulfilled_machine_count(state) >= target.desired_instance_count or state.shortfall_since is None:
        return None
    if now < state.shortfall_since + timedelta(minutes=target.zone_expansion_minutes):
        return None
    region = target.region_inputs(target.active_region)
    for placement in (*region.standard_placements, *region.local_zone_placements):
        if placement.zone_id not in state.active_zone_ids:
            return placement
    return None
