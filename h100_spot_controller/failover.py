"""Version-bound whole-target failover planning and single-use approval."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Callable

from .config import CapacityTarget
from .fleet import fleet_request, is_owned_fleet
from .outcomes import ReconciliationOutcome


FailoverEventNotifier = Callable[[str, "FailoverPlan", datetime], None]


@dataclass(frozen=True)
class FailoverPlan:
    plan_id: str; target_id: str; configuration_version: int; source_region: str; destination_region: str; desired_instance_count: int; source_instance_ids: tuple[str, ...]; expires_at: datetime; source_fleet_id: str | None = None; notification_topic_arn: str | None = None; trigger: str = "capacity-shortfall"


@dataclass(frozen=True)
class FailoverApproval:
    plan_id: str; target_id: str; configuration_version: int; source_region: str; destination_region: str; desired_instance_count: int; approved_at: datetime; used: bool = False; source_fleet_id: str | None = None; source_instance_ids: tuple[str, ...] = ()
    execution_claimed_at: datetime | None = None


def same_plan_request(left: FailoverPlan, right: FailoverPlan) -> bool:
    """Compare the reviewed capacity/destruction contract, excluding clock fields."""
    return (
        left.target_id, left.configuration_version, left.source_region,
        left.destination_region, left.desired_instance_count, left.source_fleet_id,
        tuple(sorted(left.source_instance_ids)), left.trigger,
    ) == (
        right.target_id, right.configuration_version, right.source_region,
        right.destination_region, right.desired_instance_count, right.source_fleet_id,
        tuple(sorted(right.source_instance_ids)), right.trigger,
    )


def approval_contract_matches(plan: FailoverPlan, approval: FailoverApproval) -> bool:
    return (
        approval.plan_id, approval.target_id, approval.configuration_version, approval.source_region,
        approval.destination_region, approval.desired_instance_count, approval.source_fleet_id,
        tuple(sorted(approval.source_instance_ids)),
    ) == (
        plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region,
        plan.destination_region, plan.desired_instance_count, plan.source_fleet_id,
        tuple(sorted(plan.source_instance_ids)),
    )


def approval_matches(plan: FailoverPlan, approval: FailoverApproval, now: datetime) -> bool:
    return not approval.used and now <= plan.expires_at and approval_contract_matches(plan, approval)


def destination_allowed(source_owned_instance_count: int, plan: FailoverPlan, approval: FailoverApproval, now: datetime) -> bool:
    return source_owned_instance_count == 0 and approval_matches(plan, approval, now)


def build_failover_plan(
    target: CapacityTarget,
    configuration_version: int,
    source_instance_ids: tuple[str, ...],
    now: datetime,
    *,
    plan_epoch: datetime | None = None,
    approval_generation: int = 0,
    source_fleet_id: str | None = None,
    notification_topic_arn: str | None = None,
    destination_region: str | None = None,
    trigger: str = "capacity-shortfall",
) -> FailoverPlan:
    """Plan a complete target move to an approved Region; no AWS calls."""
    candidates = [item.region for item in target.candidate_regions]
    source_index = candidates.index(target.active_region)
    if len(candidates) < 2:
        raise ValueError("no configured destination Region remains")
    destination = destination_region or candidates[(source_index + 1) % len(candidates)]
    if destination not in candidates or destination == target.active_region:
        raise ValueError("destination Region must be another configured candidate")
    if trigger not in {"capacity-shortfall", "operator-request"}:
        raise ValueError("unsupported failover plan trigger")
    created_at = now if plan_epoch is None else plan_epoch + timedelta(
        minutes=target.region_failover_minutes + approval_generation * target.failover_approval_minutes
    )
    material = (
        f"{target.target_id}|{configuration_version}|{target.active_region}|{destination}|"
        f"{target.desired_instance_count}|{plan_epoch.isoformat() if plan_epoch else created_at.isoformat()}|"
        f"{approval_generation}|{source_fleet_id or ''}|{','.join(sorted(source_instance_ids))}|{trigger}"
    )
    return FailoverPlan(
        plan_id=sha256(material.encode()).hexdigest()[:32], target_id=target.target_id,
        configuration_version=configuration_version, source_region=target.active_region,
        destination_region=destination, desired_instance_count=target.desired_instance_count,
        source_instance_ids=source_instance_ids, expires_at=created_at + timedelta(minutes=target.failover_approval_minutes),
        source_fleet_id=source_fleet_id,
        notification_topic_arn=notification_topic_arn,
        trigger=trigger,
    )


def target_configuration_version(target: CapacityTarget) -> int:
    """Stable version that changes whenever the reviewed target contract changes."""
    material = json.dumps(asdict(target), sort_keys=True, default=str, separators=(",", ":"))
    return int(sha256(material.encode()).hexdigest()[:15], 16)


def plan_as_item(plan: FailoverPlan) -> dict[str, Any]:
    return {
        "pk": f"TARGET#{plan.target_id}", "sk": f"FAILOVER_PLAN#{plan.plan_id}",
        "plan_id": plan.plan_id, "target_id": plan.target_id,
        "configuration_version": plan.configuration_version, "source_region": plan.source_region,
        "destination_region": plan.destination_region, "desired_instance_count": plan.desired_instance_count,
        "source_instance_ids": list(plan.source_instance_ids), "expires_at": plan.expires_at.isoformat(),
        "source_fleet_id": plan.source_fleet_id,
        "notification_topic_arn": plan.notification_topic_arn,
        "trigger": plan.trigger,
        "ttl": int(plan.expires_at.timestamp()),
    }


def plan_from_item(item: dict[str, Any]) -> FailoverPlan:
    return FailoverPlan(
        plan_id=item["plan_id"], target_id=item["target_id"], configuration_version=int(item["configuration_version"]),
        source_region=item["source_region"], destination_region=item["destination_region"],
        desired_instance_count=int(item["desired_instance_count"]), source_instance_ids=tuple(item["source_instance_ids"]),
        expires_at=datetime.fromisoformat(item["expires_at"]), source_fleet_id=item.get("source_fleet_id"),
        notification_topic_arn=item.get("notification_topic_arn"),
        trigger=item.get("trigger", "capacity-shortfall"),
    )


def plan_as_dict(plan: FailoverPlan) -> dict[str, Any]:
    return {**plan_as_item(plan), "source_instance_ids": list(plan.source_instance_ids)}


class DynamoFailoverApprovalStore:
    """DynamoDB record writer used only by the local, IAM-authenticated CLI."""
    def __init__(self, table: Any, event_notifier: FailoverEventNotifier | None = None) -> None:
        self.table = table
        self.event_notifier = event_notifier

    def _notify(self, event_type: str, plan: FailoverPlan, now: datetime) -> None:
        if self.event_notifier is not None:
            self.event_notifier(event_type, plan, now)

    def notify_expired(self, plan: FailoverPlan, now: datetime) -> None:
        if now > plan.expires_at:
            self._notify("failover_approval_expired", plan, now)

    def put_plan(self, plan: FailoverPlan, now: datetime | None = None) -> bool:
        """Persist and notify a plan once; repeated reconciliation is silent."""
        try:
            self.table.put_item(Item=plan_as_item(plan), ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        self._notify("failover_approval_required", plan, now or utc_now())
        return True

    def put_current_plan(self, plan: FailoverPlan, now: datetime | None = None) -> bool:
        """Persist one operator-requested current plan, idempotently.

        A non-expired different current plan is a conflict.  The current slot is
        only a planning/approval record and grants no EC2 authority.
        """
        requested_at = now or utc_now()
        current_item = {**plan_as_item(plan), "sk": "FAILOVER_CURRENT"}
        try:
            self.table.put_item(
                Item=current_item,
                ConditionExpression="attribute_not_exists(plan_id) OR expires_at < :now",
                ExpressionAttributeValues={":now": requested_at.isoformat()},
            )
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            current = self.get_current_plan(plan.target_id)
            if current is not None and same_plan_request(current, plan):
                return False
            raise ValueError("another unexpired failover plan is already current") from error
        try:
            self.table.put_item(
                Item=plan_as_item(plan),
                ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
            )
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        self._notify("failover_approval_required", plan, requested_at)
        return True

    def get_current_plan(self, target_id: str) -> FailoverPlan | None:
        response = self.table.get_item(
            Key={"pk": f"TARGET#{target_id}", "sk": "FAILOVER_CURRENT"}, ConsistentRead=True,
        )
        item = response.get("Item")
        return plan_from_item(item) if item is not None else None

    def clear_current_plan(self, plan: FailoverPlan) -> None:
        try:
            self.table.delete_item(
                Key={"pk": f"TARGET#{plan.target_id}", "sk": "FAILOVER_CURRENT"},
                ConditionExpression="plan_id = :plan_id",
                ExpressionAttributeValues={":plan_id": plan.plan_id},
            )
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def get_plan(self, target_id: str, plan_id: str) -> FailoverPlan:
        response = self.table.get_item(Key={"pk": f"TARGET#{target_id}", "sk": f"FAILOVER_PLAN#{plan_id}"}, ConsistentRead=True)
        if "Item" not in response:
            raise ValueError("failover plan not found")
        return plan_from_item(response["Item"])

    def get_approval(self, plan: FailoverPlan) -> FailoverApproval | None:
        response = self.table.get_item(
            Key={"pk": f"TARGET#{plan.target_id}", "sk": f"FAILOVER_APPROVAL#{plan.plan_id}"}, ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        return FailoverApproval(
            item["plan_id"], item["target_id"], int(item["configuration_version"]), item["source_region"],
            item["destination_region"], int(item["desired_instance_count"]), datetime.fromisoformat(item["approved_at"]), bool(item.get("used", False)),
            item.get("source_fleet_id"), tuple(item.get("source_instance_ids", ())),
            datetime.fromisoformat(item["execution_claimed_at"]) if item.get("execution_claimed_at") else None,
        )

    def is_rejected(self, plan: FailoverPlan) -> bool:
        response = self.table.get_item(
            Key={"pk": f"TARGET#{plan.target_id}", "sk": f"FAILOVER_REJECTION#{plan.plan_id}"}, ConsistentRead=True,
        )
        return "Item" in response

    def reject(self, plan: FailoverPlan, now: datetime, operator_arn: str) -> None:
        if now > plan.expires_at:
            self._notify("failover_approval_expired", plan, now)
            raise ValueError("failover plan has expired")
        self.table.put_item(
            Item={"pk": f"TARGET#{plan.target_id}", "sk": f"FAILOVER_REJECTION#{plan.plan_id}", "plan_id": plan.plan_id,
                  "rejected_at": now.isoformat(), "operator_arn": operator_arn, "ttl": int(plan.expires_at.timestamp())},
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
        self._notify("failover_rejected", plan, now)

    def consume_approval(self, plan: FailoverPlan) -> bool:
        """Legacy one-record claim retained for in-memory test doubles."""
        try:
            self.table.update_item(
                Key={"pk": f"TARGET#{plan.target_id}", "sk": f"FAILOVER_APPROVAL#{plan.plan_id}"},
                UpdateExpression="SET used = :true",
                ConditionExpression="used = :false AND configuration_version = :version AND source_region = :source AND destination_region = :destination AND desired_instance_count = :desired AND source_fleet_id = :fleet AND source_instance_ids = :instances",
                ExpressionAttributeValues={
                    ":true": True, ":false": False, ":version": plan.configuration_version,
                    ":source": plan.source_region, ":destination": plan.destination_region,
                    ":desired": plan.desired_instance_count, ":fleet": plan.source_fleet_id,
                    ":instances": list(plan.source_instance_ids),
                },
            )
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def claim_execution(self, plan: FailoverPlan, now: datetime) -> bool:
        """Atomically claim a valid approval before the first source mutation."""
        if not hasattr(self.table, "update_item"):
            return self.consume_approval(plan)
        retention = now + timedelta(days=1)
        contract = (
            "configuration_version = :version AND source_region = :source AND "
            "destination_region = :destination AND desired_instance_count = :desired AND "
            "source_fleet_id = :fleet AND source_instance_ids = :instances"
        )
        try:
            self.table.update_item(
                Key={"pk": f"TARGET#{plan.target_id}", "sk": f"FAILOVER_APPROVAL#{plan.plan_id}"},
                UpdateExpression="SET #used = :true, execution_claimed_at = :now, #ttl = :ttl",
                ConditionExpression="#used = :false AND " + contract,
                ExpressionAttributeNames={"#ttl": "ttl", "#used": "used"},
                ExpressionAttributeValues={
                    ":true": True, ":false": False, ":now": now.isoformat(),
                    ":ttl": int(retention.timestamp()), ":version": plan.configuration_version,
                    ":source": plan.source_region, ":destination": plan.destination_region,
                    ":desired": plan.desired_instance_count, ":fleet": plan.source_fleet_id,
                    ":instances": list(plan.source_instance_ids),
                },
            )
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        self.retain_execution(plan, now)
        return True

    def retain_execution(self, plan: FailoverPlan, now: datetime) -> None:
        """Retain exact execution records before any EC2 mutation."""
        retention = now + timedelta(days=1)
        contract = (
            "configuration_version = :version AND source_region = :source AND "
            "destination_region = :destination AND desired_instance_count = :desired AND "
            "source_fleet_id = :fleet AND source_instance_ids = :instances"
        )
        values = {
            ":now": now.isoformat(), ":ttl": int(retention.timestamp()), ":plan": plan.plan_id,
            ":version": plan.configuration_version, ":source": plan.source_region,
            ":destination": plan.destination_region, ":desired": plan.desired_instance_count,
            ":fleet": plan.source_fleet_id, ":instances": list(plan.source_instance_ids),
        }
        for key in (f"FAILOVER_PLAN#{plan.plan_id}", "FAILOVER_CURRENT"):
            self.table.update_item(
                Key={"pk": f"TARGET#{plan.target_id}", "sk": key},
                UpdateExpression="SET execution_claimed_at = :now, #ttl = :ttl",
                ConditionExpression="plan_id = :plan AND " + contract,
                ExpressionAttributeNames={"#ttl": "ttl"}, ExpressionAttributeValues=values,
            )

    def approve(self, plan: FailoverPlan, now: datetime, operator_arn: str) -> FailoverApproval:
        if now > plan.expires_at:
            self._notify("failover_approval_expired", plan, now)
            raise ValueError("failover plan has expired")
        approval = FailoverApproval(
            plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region,
            plan.destination_region, plan.desired_instance_count, now, False, plan.source_fleet_id, plan.source_instance_ids,
        )
        item = {
            "pk": f"TARGET#{plan.target_id}", "sk": f"FAILOVER_APPROVAL#{plan.plan_id}",
            "plan_id": approval.plan_id, "target_id": approval.target_id,
            "configuration_version": approval.configuration_version, "source_region": approval.source_region,
            "destination_region": approval.destination_region, "desired_instance_count": approval.desired_instance_count,
            "approved_at": approval.approved_at.isoformat(), "operator_arn": operator_arn,
            "source_fleet_id": approval.source_fleet_id, "source_instance_ids": list(approval.source_instance_ids),
            "ttl": int(plan.expires_at.timestamp()), "used": False,
        }
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        return approval


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def owned_instance_ids(instances: list[dict[str, Any]], target: CapacityTarget) -> tuple[str, ...]:
    """Select only instances carrying every controller ownership tag."""
    result = []
    for instance in instances:
        tags = {item["Key"]: item["Value"] for item in instance.get("Tags", [])}
        if all(tags.get(key) == value for key, value in target.tags.items()):
            result.append(instance["InstanceId"])
    return tuple(result)


def execute_approved_failover(
    source_ec2: Any,
    destination_ec2: Any,
    target: CapacityTarget,
    plan: FailoverPlan,
    approval: FailoverApproval,
    source_fleet: dict[str, Any] | None,
    source_instances: list[dict[str, Any]],
    now: datetime,
    consume_approval: Callable[..., bool] | None = None,
    event_notifier: FailoverEventNotifier | None = None,
    destination_price_caps: dict[str, Any] | None = None,
    activate_destination: Callable[[FailoverPlan], None] | None = None,
    mark_destination_complete: Callable[[FailoverPlan], None] | None = None,
    approval_already_consumed: bool = False,
) -> ReconciliationOutcome:
    """Safely introduce a capacity gap before requesting the destination fleet.

    A caller must re-describe the source on subsequent invocations.  This makes
    the zero-source barrier observable rather than assuming termination worked.
    """
    continuing_claimed_plan = (
        approval.used
        and approval.execution_claimed_at is not None
        and approval_contract_matches(plan, approval)
    )
    if not approval_matches(plan, approval, now) and not continuing_claimed_plan:
        if now > plan.expires_at and event_notifier is not None:
            event_notifier("failover_approval_expired", plan, now)
        return ReconciliationOutcome("awaiting_approval", target.target_id, target.active_region, target.desired_instance_count, len(source_instances))
    if (
        plan.configuration_version != target_configuration_version(target)
        or plan.source_region != target.active_region
        or plan.desired_instance_count != target.desired_instance_count
    ):
        return ReconciliationOutcome("configuration_error", target.target_id, target.active_region, target.desired_instance_count, len(source_instances))
    owned_ids = owned_instance_ids(source_instances, target)
    unplanned_owned = set(owned_ids) - set(plan.source_instance_ids)
    if unplanned_owned:
        return ReconciliationOutcome("ownership_mismatch", target.target_id, target.active_region, target.desired_instance_count, len(owned_ids))
    if not continuing_claimed_plan and consume_approval is not None:
        try:
            claimed = consume_approval(plan, now)
        except TypeError:
            claimed = consume_approval(plan)
        if not claimed:
            return ReconciliationOutcome("awaiting_approval", target.target_id, target.active_region, target.desired_instance_count, len(owned_ids))
        continuing_claimed_plan = True
    source_fleet_stopped = False
    if source_fleet is not None:
        if source_fleet.get("FleetId") != plan.source_fleet_id:
            return ReconciliationOutcome("ownership_mismatch", target.target_id, target.active_region, target.desired_instance_count, len(owned_ids))
        if not is_owned_fleet(source_fleet, target):
            return ReconciliationOutcome("ownership_mismatch", target.target_id, target.active_region, target.desired_instance_count, len(owned_ids))
        source_ec2.delete_fleets(FleetIds=[source_fleet["FleetId"]], TerminateInstances=False)
        source_fleet_stopped = True
    if owned_ids:
        source_ec2.terminate_instances(InstanceIds=list(owned_ids))
        return ReconciliationOutcome("source_terminating", target.target_id, target.active_region, target.desired_instance_count, len(owned_ids))
    if source_fleet_stopped:
        return ReconciliationOutcome("source_terminating", target.target_id, target.active_region, target.desired_instance_count, 0)
    if activate_destination is not None and not approval_already_consumed:
        activate_destination(plan)
    destination_target = replace(target, active_region=plan.destination_region)
    destination_ec2.create_fleet(**fleet_request(destination_target, (), destination_price_caps))
    if event_notifier is not None:
        event_notifier("failover_completed", plan, now)
    if mark_destination_complete is not None:
        mark_destination_complete(plan)
    return ReconciliationOutcome("failover_complete", target.target_id, plan.destination_region, target.desired_instance_count, 0)
