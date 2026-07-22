"""Lambda entry points with explicit event-supplied target configuration."""

from __future__ import annotations

import os
import json
import logging
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any

from .config import ConfigurationError, target_from_mapping
from .discovery import discover_region
from .fleet import find_owned_fleet, find_owned_instances, observe_fleet_capacity, owned_capacity_inventory
from .failover import DynamoFailoverApprovalStore, build_failover_plan, execute_approved_failover, target_configuration_version
from .metrics import NAMESPACE, eks_readiness_metric_data, operational_metric_data, publish_metrics, publish_signal_metrics, region_selection_metric_data, zone_metric_data
from .launch_contract import inspect_launch_contract
from .notifications import NotificationDeduplicator, failover_event_notifier, publish_controller_event, shortfall_notification_due
from .outcomes import classify_api_error
from .reconciliation import reconcile_target
from .pricing import ondemand_caps
from .signals import CandidateSignalSnapshot, collect_spot_prices, collect_sps_regions
from .region_selection import (
    build_signal_snapshot, collect_regional_readiness, decision_from_selection,
    resolve_initial_region, select_region,
)
from .state import (
    DynamoEksReadinessStore, DynamoRegionDecisionStore, DynamoRegionSignalStore,
    DynamoStateStore, DynamoTargetStore, VersionedState, utc_now,
)


LOGGER = logging.getLogger(__name__)


def _target_from_event(event: dict[str, Any], target_store: Any = None):
    raw = event.get("target")
    if not isinstance(raw, dict) and target_store is not None and event.get("target_id"):
        raw = target_store.get_mapping(str(event["target_id"]))
        if raw is None:
            raise ConfigurationError(f"persisted target configuration not found: {event['target_id']}")
    if not isinstance(raw, dict):
        raise ConfigurationError("event must contain target_id for a persisted target or an explicit target mapping")
    return target_from_mapping(raw)


def _event_target_id(event: dict[str, Any]) -> str | None:
    return str(event.get("target_id") or os.environ.get("TARGET_ID") or "") or None


def _clients(target: Any) -> tuple[dict[str, Any], Any, Any, Any, Any, Any, Any, Any, Any, dict[str, Any]]:
    import boto3
    session = boto3.Session()
    by_region = {region.region: session.client("ec2", region_name=region.region) for region in target.candidate_regions}
    table_name = os.environ["STATE_TABLE_NAME"]
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)
    quota_by_region = {
        region.region: session.client("service-quotas", region_name=region.region)
        for region in target.candidate_regions
    }
    return (
        by_region, DynamoStateStore(table_name, dynamodb),
        session.client("pricing", region_name="us-east-1"), session.client("cloudwatch"),
        session.client("sns"), table, DynamoEksReadinessStore(table_name, dynamodb),
        DynamoRegionSignalStore(table_name, dynamodb),
        DynamoRegionDecisionStore(table_name, dynamodb), quota_by_region,
    )


def _with_eligible_local_zones(target: Any, state: Any, ec2: Any) -> Any:
    """Filter only inactive Local Zones immediately before fallback expansion."""
    if not target.enabled or state is None:
        return target
    inputs = target.region_inputs(target.active_region)
    active = set(state.active_zone_ids)
    if not set(item.zone_id for item in inputs.standard_placements).issubset(active):
        return target
    inactive_local = [item for item in inputs.local_zone_placements if item.zone_id not in active]
    if not inactive_local:
        return target
    discovery = discover_region(ec2, target.active_region, [item.name for item in target.instance_types], inputs.local_zone_placements)
    eligible = {item.zone_id for item in discovery.zones if item.zone_type == "local-zone" and item.eligible}
    filtered = replace(inputs, local_zone_placements=tuple(
        item for item in inputs.local_zone_placements if item.zone_id in active or item.zone_id in eligible
    ))
    return replace(target, candidate_regions=tuple(filtered if item.region == filtered.region else item for item in target.candidate_regions))


def reconcile(event: dict[str, Any], context: object, *, clients: dict[str, Any] | None = None, store: Any = None, pricing: Any = None, target_store: Any = None, cloudwatch: Any = None, sns: Any = None, deduplicator: Any = None, eks_readiness_store: Any = None, failover_store: Any = None, decision_store: Any = None) -> dict[str, Any]:
    """Reconcile only an event-supplied target; no implicit default target exists."""
    target = None
    table = None
    try:
        event_target_id = _event_target_id(event)
        if target_store is None and event_target_id:
            import boto3
            session = boto3.Session()
            target_store = DynamoTargetStore(os.environ["STATE_TABLE_NAME"], session.resource("dynamodb"))
        if event_target_id and "target_id" not in event:
            event = {**event, "target_id": event_target_id}
        target = _target_from_event(event, target_store)
        if clients is None or store is None or pricing is None:
            clients, store, pricing, default_cloudwatch, default_sns, table, default_eks_readiness, _, default_decisions, _ = _clients(target)
            cloudwatch = cloudwatch or default_cloudwatch
            sns = sns or default_sns
            deduplicator = deduplicator or NotificationDeduplicator(table)
            eks_readiness_store = eks_readiness_store or default_eks_readiness
            decision_store = decision_store or default_decisions
        previous_state = store.get(target.target_id)
        now = utc_now()
        selection_configuration_version = target_configuration_version(target)
        inventory = (
            owned_capacity_inventory(clients, target)
            if target.region_selection.mode != "manual"
            else []
        )
        decision = decision_store.get(target.target_id) if decision_store is not None else None
        resolution = resolve_initial_region(
            target, previous_state, inventory, decision, selection_configuration_version, now,
        )
        if resolution.status == "ownership_mismatch":
            return {"status": "ownership_mismatch", "aws_write": False, "error": resolution.detail}
        if resolution.region is None:
            return {
                "status": resolution.status, "aws_write": False, "error": resolution.detail,
                "recommended_region": decision.selected_region if decision else None,
            }
        target = replace(target, active_region=resolution.region)
        if previous_state is None and resolution.apply_decision:
            initial_state = VersionedState(
                target.target_id, 0, resolution.region,
                (target.region_inputs(resolution.region).standard_placements[0].zone_id,),
                initial_region_decision_version=decision.decision_version if decision else None,
                initial_region_snapshot_id=decision.snapshot_id if decision else None,
            )
            store.put_if_version(initial_state, None)
            previous_state = initial_state
            if decision_store is None or decision is None or not decision_store.mark_applied(decision, now):
                return {"status": "region_decision_conflict", "aws_write": False}
        elif (
            target.region_selection.mode == "auto_initial"
            and previous_state is not None
            and previous_state.initial_region_decision_version is not None
            and not any(item.get("owned_fleets") or item.get("owned_instances") for item in inventory)
        ):
            # Recover safely if a Lambda stopped after pinning runtime state but
            # before claiming the matching decision.  Never let a newer/different
            # recommendation authorize capacity in the already pinned Region.
            if (
                decision_store is None
                or decision is None
                or decision.decision_version != previous_state.initial_region_decision_version
                or decision.snapshot_id != previous_state.initial_region_snapshot_id
                or decision.selected_region != previous_state.active_region
                or decision.configuration_version != selection_configuration_version
                or (decision.applied_at is None and not decision_store.mark_applied(decision, now))
            ):
                return {"status": "region_decision_conflict", "aws_write": False}
        if target.enabled:
            for inputs in target.candidate_regions:
                inspection = inspect_launch_contract(clients[inputs.region], target, inputs)
                if not inspection.valid:
                    raise ConfigurationError(f"launch contract invalid in {inputs.region}: {', '.join(inspection.violations)}")
        target = _with_eligible_local_zones(target, previous_state, clients[target.active_region])
        caps = ondemand_caps(pricing, target.active_region, tuple(item.name for item in target.instance_types))
        fleet = find_owned_fleet(clients[target.active_region], target)
        capacity = observe_fleet_capacity(clients[target.active_region], fleet, target) if fleet else None
        topic_arn = target.notification_topic_arn or os.environ.get("NOTIFICATION_TOPIC_ARN")
        notifier = None
        if sns is not None and deduplicator is not None:
            notifier = failover_event_notifier(
                sns, topic_arn, deduplicator, existing_eks=target.integration_mode == "existing-eks",
            )
        if failover_store is None and table is not None:
            failover_store = DynamoFailoverApprovalStore(table, notifier)

        state = previous_state
        manual_plan = None
        approval = None
        if target.enabled and failover_store is not None and hasattr(failover_store, "get_current_plan"):
            manual_plan = failover_store.get_current_plan(target.target_id)
            if manual_plan is not None:
                approval = failover_store.get_approval(manual_plan)
            if manual_plan is not None and now > manual_plan.expires_at:
                claimed = approval is not None and approval.used and approval.execution_claimed_at is not None
                if not claimed and hasattr(failover_store, "notify_expired"):
                    failover_store.notify_expired(manual_plan, now)
                if not claimed:
                    manual_plan = None
        failover_ready = (
            target.enabled and state is not None and state.all_zones_shortfall_since is not None
            and now >= state.all_zones_shortfall_since + timedelta(minutes=target.region_failover_minutes)
            and failover_store is not None and manual_plan is None
        )
        plan = manual_plan
        plan_rejected = False
        if plan is not None:
            plan_rejected = failover_store.is_rejected(plan)
            approval = None if plan_rejected else (approval or failover_store.get_approval(plan))
        if failover_ready:
            ready_at = state.all_zones_shortfall_since + timedelta(minutes=target.region_failover_minutes)  # type: ignore[union-attr]
            generation = int((now - ready_at).total_seconds() // (target.failover_approval_minutes * 60))
            source_instances = find_owned_instances(clients[target.active_region], target)
            source_ids = tuple(sorted(instance["InstanceId"] for instance in source_instances))
            recommended_destination = (
                decision.selected_region
                if decision is not None
                and decision.selected_region is not None
                and decision.selected_region != target.active_region
                and decision.expires_at > now
                else None
            )
            if generation > 0 and hasattr(failover_store, "notify_expired"):
                previous_candidate = build_failover_plan(
                    target, target_configuration_version(target), source_ids, now,
                    plan_epoch=state.all_zones_shortfall_since, approval_generation=generation - 1,  # type: ignore[union-attr]
                    source_fleet_id=fleet.get("FleetId") if fleet else None,
                    notification_topic_arn=topic_arn,
                    destination_region=recommended_destination,
                )
                try:
                    failover_store.notify_expired(failover_store.get_plan(target.target_id, previous_candidate.plan_id), now)
                except ValueError:
                    pass
            candidate = build_failover_plan(
                target, target_configuration_version(target), source_ids, now,
                plan_epoch=state.all_zones_shortfall_since, approval_generation=generation,  # type: ignore[union-attr]
                source_fleet_id=fleet.get("FleetId") if fleet else None,
                notification_topic_arn=topic_arn,
                destination_region=recommended_destination,
            )
            failover_store.put_plan(candidate, now)
            plan = failover_store.get_plan(target.target_id, candidate.plan_id)
            plan_rejected = failover_store.is_rejected(plan)
            approval = None if plan_rejected else failover_store.get_approval(plan)

        if plan is not None and approval is not None:
            if approval.used and approval.execution_claimed_at is not None and hasattr(failover_store, "retain_execution"):
                # A prior invocation may have claimed approval immediately before
                # a process failure.  Refresh durable plan retention before any
                # retry is permitted to touch source or destination EC2 capacity.
                failover_store.retain_execution(plan, now)
            pending_execution = (
                state is not None
                and state.pending_failover_completion_plan_id == plan.plan_id
                and state.active_region == plan.destination_region
                and approval.used
                and approval.execution_claimed_at is not None
            )
            execution_target = replace(target, active_region=plan.source_region) if pending_execution else target
            source_fleet = find_owned_fleet(clients[plan.source_region], execution_target)
            source_instances = find_owned_instances(clients[plan.source_region], execution_target)
            destination_caps = ondemand_caps(pricing, plan.destination_region, tuple(item.name for item in target.instance_types))

            def activate_destination(approved_plan: Any) -> None:
                current = store.get(target.target_id)
                if pending_execution:
                    if (
                        current is None
                        or current.active_region != approved_plan.destination_region
                        or current.pending_failover_completion_plan_id != approved_plan.plan_id
                    ):
                        raise RuntimeError("pending failover execution state changed before retry")
                    return
                if current is None or current.active_region != approved_plan.source_region:
                    raise RuntimeError("source state changed before destination activation")
                destination_target = replace(target, active_region=approved_plan.destination_region)
                destination_state = VersionedState(
                    target.target_id, current.version + 1, approved_plan.destination_region,
                    (destination_target.region_inputs(approved_plan.destination_region).standard_placements[0].zone_id,),
                    pending_failover_completion_plan_id=approved_plan.plan_id,
                )
                store.put_if_version(destination_state, current.version)

            def mark_destination_complete(approved_plan: Any) -> None:
                current = store.get(target.target_id)
                if current is None or current.pending_failover_completion_plan_id != approved_plan.plan_id:
                    return
                store.put_if_version(replace(current, version=current.version + 1, pending_failover_completion_plan_id=None), current.version)

            claim_execution = getattr(failover_store, "claim_execution", None)
            if claim_execution is None:
                claim_execution = failover_store.consume_approval
            outcome = execute_approved_failover(
                clients[plan.source_region], clients[plan.destination_region], execution_target, plan, approval,
                source_fleet, source_instances, now, claim_execution, notifier,
                destination_price_caps=destination_caps, activate_destination=activate_destination,
                mark_destination_complete=mark_destination_complete,
                approval_already_consumed=pending_execution,
            )
            if outcome.kind == "failover_complete" and hasattr(failover_store, "clear_current_plan"):
                failover_store.clear_current_plan(plan)
        else:
            outcome = reconcile_target(clients[target.active_region], target, store, now, caps)
            if plan is not None:
                outcome = replace(outcome, kind="failover_rejected" if plan_rejected else "awaiting_approval")

        fleet = find_owned_fleet(clients[target.active_region], target)
        capacity = observe_fleet_capacity(clients[target.active_region], fleet, target) if fleet else None
        state = store.get(target.target_id)
        if state is not None and state.pending_failover_completion_plan_id and fleet is not None and failover_store is not None:
            pending_plan = failover_store.get_plan(target.target_id, state.pending_failover_completion_plan_id)
            if notifier is not None:
                notifier("failover_completed", pending_plan, now)
            current = store.get(target.target_id)
            if current is not None and current.pending_failover_completion_plan_id == pending_plan.plan_id:
                store.put_if_version(replace(current, version=current.version + 1, pending_failover_completion_plan_id=None), current.version)
                state = store.get(target.target_id)
        if cloudwatch is not None:
            publish_metrics(
                cloudwatch, outcome, capacity.realized_h100_gpu_count if capacity else 0,
                len(state.active_zone_ids) if state else 0,
                accelerator_profile=target.accelerator_profile,
                realized_accelerator_count=capacity.realized_accelerator_count if capacity else 0,
                accelerator_counts_by_model=capacity.accelerator_counts_by_model if capacity else {},
            )
            if state is not None:
                cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=zone_metric_data(target.target_id, target.active_region, state.active_zone_ids, capacity.by_zone if capacity else {}))
            cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=operational_metric_data(
                target.target_id, target.active_region, outcome.kind,
                1 if outcome.kind in {"authorization_error", "dependency_error"} else 0, 0,
                plan.trigger if plan is not None else "none",
            ))
            if target.integration_mode == "existing-eks" and eks_readiness_store is not None:
                readiness = eks_readiness_store.get(target.target_id, target.active_region)
                if readiness:
                    cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=eks_readiness_metric_data(target.target_id, target.active_region, readiness.registered_node_count, readiness.ready_node_count))
        if sns is not None and deduplicator is not None:
            if outcome.kind == "shortfall" and state is not None and shortfall_notification_due(
                state.shortfall_since, target.zone_expansion_minutes, now,
            ):
                publish_controller_event(sns, topic_arn, deduplicator, target.target_id, "prolonged_shortfall", asdict(outcome), now)
            elif outcome.kind in {"configuration_error", "authorization_error", "dependency_error", "ownership_mismatch"}:
                publish_controller_event(sns, topic_arn, deduplicator, target.target_id, "repeated_reconciliation_failure", asdict(outcome), now)
            if previous_state and state and len(state.active_zone_ids) > len(previous_state.active_zone_ids):
                publish_controller_event(sns, topic_arn, deduplicator, target.target_id, "zone_expansion", {"active_zone_ids": state.active_zone_ids}, now)
        return {"status": outcome.kind, "aws_write": target.enabled, "outcome": asdict(outcome)}
    except ConfigurationError as error:
        return {"status": "configuration_error", "aws_write": False, "error": str(error)}
    except Exception as error:
        kind, code = classify_api_error(error)
        if target is not None and sns is not None and deduplicator is not None and kind != "rate_limited":
            try:
                publish_controller_event(
                    sns, target.notification_topic_arn or os.environ.get("NOTIFICATION_TOPIC_ARN"), deduplicator,
                    target.target_id, "repeated_reconciliation_failure",
                    {"status": kind, "error_code": code}, utc_now(),
                )
            except Exception:
                pass
        return {"status": kind, "aws_write": False, "error_code": code}


def collect(event: dict[str, Any], context: object, *, clients: dict[str, Any] | None = None, target_store: Any = None, cloudwatch: Any = None, pricing: Any = None, quota_clients: dict[str, Any] | None = None, signal_store: Any = None, decision_store: Any = None) -> dict[str, Any]:
    """Collect advisory capacity signals; it never changes Fleet capacity."""
    try:
        event_target_id = _event_target_id(event)
        if target_store is None and event_target_id:
            import boto3
            session = boto3.Session()
            target_store = DynamoTargetStore(os.environ["STATE_TABLE_NAME"], session.resource("dynamodb"))
        if event_target_id and "target_id" not in event:
            event = {**event, "target_id": event_target_id}
        target = _target_from_event(event, target_store)
        if clients is None:
            clients, _, default_pricing, default_cloudwatch, _, _, _, default_signals, default_decisions, default_quotas = _clients(target)
            cloudwatch = cloudwatch or default_cloudwatch
            pricing = pricing or default_pricing
            quota_clients = quota_clients or default_quotas
            signal_store = signal_store or default_signals
            decision_store = decision_store or default_decisions
        collection = str(event.get("collection", "all"))
        if collection not in {"all", "price-and-local", "sps"}:
            raise ConfigurationError("collection must be all, price-and-local, or sps")
        regions = [item.region for item in target.candidate_regions]
        discoveries = {} if collection == "sps" else {
            item.region: discover_region(clients[item.region], item.region, [kind.name for kind in target.instance_types], item.local_zone_placements)
            for item in target.candidate_regions
        }
        now = utc_now()
        regional_sps = {} if collection == "price-and-local" else collect_sps_regions(clients[regions[0]], target, tuple(regions), now)
        snapshot = CandidateSignalSnapshot(
            sps_by_region=regional_sps,
            price_by_region={} if collection == "sps" else {region: collect_spot_prices(clients[region], target, region) for region in regions},
            local_zone_eligibility=discoveries,
            observed_at=now,
            request_fingerprint=next(iter(regional_sps.values())).request_fingerprint if regional_sps else None,
        )
        if cloudwatch is not None:
            publish_signal_metrics(cloudwatch, target.target_id, snapshot, target.accelerator_profile)
        selection_report = None
        if collection in {"all", "sps"} and target.region_selection.mode != "manual":
            if pricing is None or quota_clients is None:
                raise ConfigurationError("dynamic Region selection requires pricing and per-Region quota clients")
            az_sps = collect_sps_regions(
                clients[regions[0]], target, tuple(regions), now, single_availability_zone=True,
            )
            readiness = {
                region: collect_regional_readiness(
                    target, region, clients[region], pricing, quota_clients[region], now,
                    az_sps=az_sps.get(region),
                )
                for region in regions
            }
            decision_snapshot = build_signal_snapshot(
                target, target_configuration_version(target), regional_sps, readiness, now,
            )
            selection = select_region(target, decision_snapshot, now)
            current = decision_store.get(target.target_id) if decision_store is not None else None
            decision = decision_from_selection(
                target, target_configuration_version(target),
                1 if current is None else current.decision_version + 1,
                decision_snapshot, selection, now,
            )
            if signal_store is not None:
                signal_store.put_if_absent(decision_snapshot)
            if decision_store is not None:
                decision_store.publish(decision, None if current is None else current.decision_version)
            if cloudwatch is not None:
                cloudwatch.put_metric_data(
                    Namespace=NAMESPACE,
                    MetricData=region_selection_metric_data(
                        target.target_id, target.region_selection.mode, selection,
                        decision_snapshot, now, target.active_region,
                    ),
                )
            selection_report = {
                "mode": target.region_selection.mode,
                "selected_region": selection.selected_region,
                "reason": selection.reason,
                "ordered_regions": [item.region for item in selection.ordered_candidates],
                "exclusions": {item.region: list(item.exclusion_reasons) for item in selection.excluded_candidates},
                "snapshot_id": decision_snapshot.snapshot_id,
                "request_fingerprint": decision_snapshot.request_fingerprint,
                "decision_version": decision.decision_version,
                "expires_at": decision.expires_at.isoformat(),
            }
            LOGGER.info("region_selection_decision %s", json.dumps(selection_report, sort_keys=True))
        return {
            "status": "ok", "aws_write": False, "collection": collection,
            "sps": {region: observation.status for region, observation in snapshot.sps_by_region.items()},
            "prices": {region: observation.status for region, observation in snapshot.price_by_region.items()},
            "region_selection": selection_report,
        }
    except ConfigurationError as error:
        return {"status": "configuration_error", "aws_write": False, "error": str(error)}
    except Exception as error:
        kind, code = classify_api_error(error)
        return {"status": kind, "aws_write": False, "error_code": code}


def spot_event(event: dict[str, Any], context: object, *, clients: dict[str, Any] | None = None, target_store: Any = None, cloudwatch: Any = None, sns: Any = None, deduplicator: Any = None) -> dict[str, Any]:
    """Publish owned-instance interruption/rebalance events without capacity writes."""
    try:
        target_id = _event_target_id(event)
        if not target_id:
            raise ConfigurationError("spot event has no configured target_id")
        if target_store is None:
            import boto3
            session = boto3.Session()
            target_store = DynamoTargetStore(os.environ["STATE_TABLE_NAME"], session.resource("dynamodb"))
        target = _target_from_event({"target_id": target_id}, target_store)
        if clients is None:
            clients, _, _, default_cloudwatch, default_sns, table, _, _, _, _ = _clients(target)
            cloudwatch = cloudwatch or default_cloudwatch
            sns = sns or default_sns
            deduplicator = deduplicator or NotificationDeduplicator(table)
        instance_id = event.get("detail", {}).get("instance-id")
        if not instance_id:
            raise ConfigurationError("spot event has no instance-id")
        response = clients[target.active_region].describe_instances(InstanceIds=[instance_id])
        instances = [item for reservation in response.get("Reservations", []) for item in reservation.get("Instances", [])]
        tags = {tag["Key"]: tag["Value"] for tag in (instances[0].get("Tags", []) if instances else [])}
        if not instances or not all(tags.get(key) == value for key, value in target.tags.items()):
            return {"status": "ignored_unowned_instance", "aws_write": False}
        detail_type = event.get("detail-type", "")
        event_type = "spot_interruption" if "Interruption" in detail_type else "capacity_rebalance"
        if cloudwatch is not None:
            cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=operational_metric_data(target.target_id, target.active_region, "active", 0, 1 if event_type == "spot_interruption" else 0))
        if sns is not None and deduplicator is not None:
            topic_arn = target.notification_topic_arn or os.environ.get("NOTIFICATION_TOPIC_ARN")
            publish_controller_event(sns, topic_arn, deduplicator, target.target_id, event_type, {"instance_id": instance_id, "detail_type": detail_type}, utc_now())
        return {"status": event_type, "aws_write": False, "instance_id": instance_id}
    except ConfigurationError as error:
        return {"status": "configuration_error", "aws_write": False, "error": str(error)}
    except Exception as error:
        kind, code = classify_api_error(error)
        return {"status": kind, "aws_write": False, "error_code": code}
