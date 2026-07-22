"""One target reconciliation transaction, isolated from Lambda plumbing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import CapacityTarget
from .fleet import find_owned_fleet, find_owned_instances, fulfilled_by_zone, reconcile_existing_fleet, reconcile_fleet
from .outcomes import ReconciliationOutcome
from .placement import PlacementState, initial_placement, next_placement_to_activate
from .state import StateStore, VersionedState


def reconcile_target(ec2: Any, target: CapacityTarget, store: StateStore, now: datetime, price_caps: dict[str, Any] | None = None) -> ReconciliationOutcome:
    """Persist Zone progression once; unchanged targets reuse their owned Fleet."""
    previous = store.get(target.target_id)
    state = previous or VersionedState(target.target_id, 0, target.active_region, (initial_placement(target).zone_id,))
    if state.active_region != target.active_region:
        return ReconciliationOutcome("configuration_error", target.target_id, target.active_region, target.desired_instance_count, 0)
    fleet = find_owned_fleet(ec2, target)
    owned_instances = (
        find_owned_instances(ec2, target)
        if fleet is None and target.enabled and state.owned_fleet_id is not None
        else []
    )
    request_epoch = state.fleet_request_epoch
    owned_fleet_id = state.owned_fleet_id
    if fleet is not None:
        owned_fleet_id = fleet["FleetId"]
    elif owned_fleet_id is not None and not owned_instances:
        # The previously recorded Fleet is no longer active and its instances are
        # gone, so rotate the EC2 idempotency token before creating a replacement.
        request_epoch = now.isoformat()
        owned_fleet_id = None
    elif request_epoch is None:
        # Persist this epoch with the shortfall state; retries before a newly
        # created Fleet becomes visible reuse the same EC2 ClientToken.
        request_epoch = now.isoformat()
    zone_capacity = {} if fleet is None else fulfilled_by_zone(ec2, fleet, target)
    fulfilled = sum(zone_capacity.values())
    # Prefer Zones that already hold target capacity, including Local Zones.
    request_zone_ids = tuple(zone_id for zone_id in state.active_zone_ids if zone_capacity.get(zone_id, 0) > 0) + tuple(
        zone_id for zone_id in state.active_zone_ids if zone_capacity.get(zone_id, 0) <= 0
    )
    outcome = reconcile_fleet(
        ec2, target, None if fleet is None else fleet["FleetId"], request_zone_ids,
        fulfilled, price_caps, request_epoch,
    ) if fleet is None else reconcile_existing_fleet(ec2, target, fleet, request_zone_ids, fulfilled, price_caps)
    if outcome.kind != "shortfall":
        next_state = VersionedState(
            target.target_id, state.version + 1, state.active_region, state.active_zone_ids,
            None, None, state.pending_failover_completion_plan_id,
            state.initial_region_decision_version, state.initial_region_snapshot_id,
            request_epoch, owned_fleet_id,
        )
    else:
        shortfall_since = state.shortfall_since or now
        placement = PlacementState(state.active_zone_ids, zone_capacity, shortfall_since)
        next_placement = next_placement_to_activate(target, placement, now)
        zones = state.active_zone_ids if next_placement is None else (*state.active_zone_ids, next_placement.zone_id)
        # Give every newly exposed pool a complete expansion interval.
        next_shortfall_since = now if next_placement is not None else shortfall_since
        configured_zone_ids = {
            placement.zone_id for placement in (
                *target.region_inputs(target.active_region).standard_placements,
                *target.region_inputs(target.active_region).local_zone_placements,
            )
        }
        all_zones_active = configured_zone_ids.issubset(set(zones))
        all_zones_since = None if next_placement is not None or not all_zones_active else (state.all_zones_shortfall_since or now)
        next_state = VersionedState(
            target.target_id, state.version + 1, state.active_region, zones,
            next_shortfall_since, all_zones_since, state.pending_failover_completion_plan_id,
            state.initial_region_decision_version, state.initial_region_snapshot_id,
            request_epoch, owned_fleet_id,
        )
    if next_state != state:
        store.put_if_version(next_state, None if previous is None else previous.version)
    return outcome
