"""Deduplicated SNS notifications with DynamoDB conditional-write backing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

EVENT_TYPES = frozenset({
    "prolonged_shortfall", "zone_expansion", "failover_approval_required", "failover_approval_expired",
    "failover_rejected", "failover_completed", "spot_interruption", "capacity_rebalance", "repeated_reconciliation_failure",
})


def shortfall_notification_due(shortfall_since: datetime | None, threshold_minutes: int, now: datetime) -> bool:
    """Notify only after one continuous configured shortfall interval."""
    return shortfall_since is not None and now >= shortfall_since + timedelta(minutes=threshold_minutes)


def notification_key(target_id: str, event_type: str, fingerprint: str) -> str:
    return f"NOTICE#{target_id}#{event_type}#{fingerprint}"


class NotificationDeduplicator:
    def __init__(self, table: Any) -> None:
        self.table = table

    def claim(self, target_id: str, event_type: str, fingerprint: str, now: datetime, window: timedelta = timedelta(minutes=30)) -> bool:
        try:
            self.table.put_item(
                Item={"pk": notification_key(target_id, event_type, fingerprint), "sk": "NOTICE", "ttl": int((now + window).timestamp())},
                ConditionExpression="attribute_not_exists(pk)",
            )
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def release(self, target_id: str, event_type: str, fingerprint: str) -> None:
        self.table.delete_item(
            Key={"pk": notification_key(target_id, event_type, fingerprint), "sk": "NOTICE"},
            ConditionExpression="attribute_exists(pk)",
        )


def publish_once(sns: Any, topic_arn: str | None, deduplicator: NotificationDeduplicator, target_id: str, event_type: str, payload: dict[str, Any], now: datetime) -> bool:
    if not topic_arn:
        return False
    fingerprint = json.dumps(payload, sort_keys=True, default=str)
    if not deduplicator.claim(target_id, event_type, fingerprint, now):
        return False
    try:
        sns.publish(TopicArn=topic_arn, Subject=f"H100 Spot controller: {event_type}", Message=json.dumps(payload, sort_keys=True, default=str))
    except Exception:
        try:
            deduplicator.release(target_id, event_type, fingerprint)
        except Exception:
            pass
        raise
    return True


def publish_controller_event(sns: Any, topic_arn: str | None, deduplicator: NotificationDeduplicator, target_id: str, event_type: str, payload: dict[str, Any], now: datetime, *, existing_eks: bool = False) -> bool:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported controller notification event: {event_type}")
    body = dict(payload)
    if existing_eks and event_type == "failover_approval_required":
        body["operator_prerequisite"] = "Drain source EKS workloads and clean up source Node objects before approval."
    return publish_once(sns, topic_arn, deduplicator, target_id, event_type, body, now)


def failover_event_notifier(
    sns: Any,
    topic_arn: str | None,
    deduplicator: NotificationDeduplicator,
    *,
    existing_eks: bool = False,
):
    """Return the notification callback consumed by the failover state machine."""
    from .failover import plan_as_dict

    def notify(event_type: str, plan: Any, now: datetime) -> None:
        publish_controller_event(
            sns, topic_arn, deduplicator, plan.target_id, event_type,
            plan_as_dict(plan), now, existing_eks=existing_eks,
        )

    return notify
