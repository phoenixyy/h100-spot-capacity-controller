"""Versioned state stores; DynamoDB writes are conditional and opt-in at CLI/runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from .region_selection import RegionDecision, RegionSignalSnapshot


class StateConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class VersionedState:
    target_id: str
    version: int
    active_region: str
    active_zone_ids: tuple[str, ...]
    shortfall_since: datetime | None = None
    all_zones_shortfall_since: datetime | None = None
    pending_failover_completion_plan_id: str | None = None
    initial_region_decision_version: int | None = None
    initial_region_snapshot_id: str | None = None
    fleet_request_epoch: str | None = None
    owned_fleet_id: str | None = None

    def as_item(self) -> dict[str, Any]:
        return {
            "pk": f"TARGET#{self.target_id}", "sk": "STATE", "version": self.version,
            "active_region": self.active_region, "active_zone_ids": list(self.active_zone_ids),
            "shortfall_since": self.shortfall_since.isoformat() if self.shortfall_since else None,
            "all_zones_shortfall_since": self.all_zones_shortfall_since.isoformat() if self.all_zones_shortfall_since else None,
            "pending_failover_completion_plan_id": self.pending_failover_completion_plan_id,
            "initial_region_decision_version": self.initial_region_decision_version,
            "initial_region_snapshot_id": self.initial_region_snapshot_id,
            "fleet_request_epoch": self.fleet_request_epoch,
            "owned_fleet_id": self.owned_fleet_id,
        }


class StateStore(Protocol):
    def get(self, target_id: str) -> VersionedState | None: ...
    def put_if_version(self, state: VersionedState, expected_version: int | None) -> None: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self.items: dict[str, VersionedState] = {}

    def put_if_version(self, state: VersionedState, expected_version: int | None) -> None:
        current = self.items.get(state.target_id)
        if (current is None and expected_version is not None) or (current is not None and current.version != expected_version):
            raise StateConflict("state version changed")
        self.items[state.target_id] = state

    def get(self, target_id: str) -> VersionedState | None:
        return self.items.get(target_id)


class DynamoStateStore:
    def __init__(self, table_name: str, dynamodb: Any) -> None:
        self.table = dynamodb.Table(table_name)

    def put_if_version(self, state: VersionedState, expected_version: int | None) -> None:
        from botocore.exceptions import ClientError
        expression = "attribute_not_exists(pk)" if expected_version is None else "version = :expected"
        values = None if expected_version is None else {":expected": expected_version}
        try:
            kwargs: dict[str, Any] = {"Item": state.as_item(), "ConditionExpression": expression}
            if values is not None:
                kwargs["ExpressionAttributeValues"] = values
            self.table.put_item(**kwargs)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise StateConflict("state version changed") from error
            raise

    def get(self, target_id: str) -> VersionedState | None:
        response = self.table.get_item(Key={"pk": f"TARGET#{target_id}", "sk": "STATE"}, ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            return None
        raw_shortfall = item.get("shortfall_since")
        raw_all_zones = item.get("all_zones_shortfall_since")
        return VersionedState(
            item["pk"].removeprefix("TARGET#"), int(item["version"]), item["active_region"], tuple(item["active_zone_ids"]),
            datetime.fromisoformat(raw_shortfall) if raw_shortfall else None,
            datetime.fromisoformat(raw_all_zones) if raw_all_zones else None,
            item.get("pending_failover_completion_plan_id"),
            int(item["initial_region_decision_version"]) if item.get("initial_region_decision_version") is not None else None,
            item.get("initial_region_snapshot_id"),
            item.get("fleet_request_epoch"), item.get("owned_fleet_id"),
        )


class DynamoTargetStore:
    """Read validated target configuration from the controller state table."""
    def __init__(self, table_name: str, dynamodb: Any) -> None:
        self.table = dynamodb.Table(table_name)

    def get_mapping(self, target_id: str) -> dict[str, Any] | None:
        response = self.table.get_item(Key={"pk": f"TARGET#{target_id}", "sk": "CONFIG"}, ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            return None
        raw = item.get("config")
        return _restore_config_numbers(raw) if isinstance(raw, dict) else None


class RegionSignalStore(Protocol):
    def put_if_absent(self, snapshot: RegionSignalSnapshot) -> bool: ...
    def get(self, target_id: str, snapshot_id: str) -> RegionSignalSnapshot | None: ...


class InMemoryRegionSignalStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], RegionSignalSnapshot] = {}

    def put_if_absent(self, snapshot: RegionSignalSnapshot) -> bool:
        key = (snapshot.target_id, snapshot.snapshot_id)
        if key in self.items:
            return False
        self.items[key] = snapshot
        return True

    def get(self, target_id: str, snapshot_id: str) -> RegionSignalSnapshot | None:
        return self.items.get((target_id, snapshot_id))


class DynamoRegionSignalStore:
    def __init__(self, table_name: str, dynamodb: Any) -> None:
        self.table = dynamodb.Table(table_name)

    def put_if_absent(self, snapshot: RegionSignalSnapshot) -> bool:
        try:
            self.table.put_item(
                Item=snapshot.as_item(),
                ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
            )
            return True
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def get(self, target_id: str, snapshot_id: str) -> RegionSignalSnapshot | None:
        response = self.table.get_item(
            Key={"pk": f"TARGET#{target_id}", "sk": f"REGION_SIGNAL#{snapshot_id}"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return RegionSignalSnapshot.from_item(item) if item else None


class RegionDecisionStore(Protocol):
    def get(self, target_id: str) -> RegionDecision | None: ...
    def publish(self, decision: RegionDecision, expected_version: int | None) -> bool: ...
    def mark_applied(self, decision: RegionDecision, applied_at: datetime) -> bool: ...


class InMemoryRegionDecisionStore:
    def __init__(self) -> None:
        self.items: dict[str, RegionDecision] = {}

    def get(self, target_id: str) -> RegionDecision | None:
        return self.items.get(target_id)

    def publish(self, decision: RegionDecision, expected_version: int | None) -> bool:
        current = self.items.get(decision.target_id)
        if current is not None and current.snapshot_id == decision.snapshot_id and current.as_item() == decision.as_item():
            return False
        if (current is None and expected_version is not None) or (
            current is not None and current.decision_version != expected_version
        ):
            raise StateConflict("Region decision version changed")
        self.items[decision.target_id] = decision
        return True

    def mark_applied(self, decision: RegionDecision, applied_at: datetime) -> bool:
        current = self.items.get(decision.target_id)
        if current is None or current.decision_version != decision.decision_version or current.snapshot_id != decision.snapshot_id:
            return False
        if current.applied_at is not None:
            return current.applied_at == applied_at
        self.items[decision.target_id] = RegionDecision(
            **{**current.__dict__, "applied_at": applied_at}
        )
        return True


class DynamoRegionDecisionStore:
    def __init__(self, table_name: str, dynamodb: Any) -> None:
        self.table = dynamodb.Table(table_name)

    def get(self, target_id: str) -> RegionDecision | None:
        response = self.table.get_item(
            Key={"pk": f"TARGET#{target_id}", "sk": "REGION_DECISION"}, ConsistentRead=True,
        )
        item = response.get("Item")
        return RegionDecision.from_item(item) if item else None

    def publish(self, decision: RegionDecision, expected_version: int | None) -> bool:
        expression = "attribute_not_exists(pk)" if expected_version is None else "decision_version = :expected"
        kwargs: dict[str, Any] = {
            "Item": decision.as_item(), "ConditionExpression": expression,
        }
        if expected_version is not None:
            kwargs["ExpressionAttributeValues"] = {":expected": expected_version}
        try:
            self.table.put_item(**kwargs)
            return True
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                current = self.get(decision.target_id)
                if current and current.snapshot_id == decision.snapshot_id and current.as_item() == decision.as_item():
                    return False
                raise StateConflict("Region decision version changed") from error
            raise

    def mark_applied(self, decision: RegionDecision, applied_at: datetime) -> bool:
        try:
            self.table.update_item(
                Key={"pk": f"TARGET#{decision.target_id}", "sk": "REGION_DECISION"},
                UpdateExpression="SET applied_at = :applied",
                ConditionExpression=(
                    "decision_version = :version AND snapshot_id = :snapshot "
                    "AND attribute_not_exists(applied_at)"
                ),
                ExpressionAttributeValues={
                    ":applied": applied_at.isoformat(), ":version": decision.decision_version,
                    ":snapshot": decision.snapshot_id,
                },
            )
            return True
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                current = self.get(decision.target_id)
                return bool(
                    current and current.decision_version == decision.decision_version
                    and current.snapshot_id == decision.snapshot_id and current.applied_at is not None
                )
            raise


def _restore_config_numbers(value: Any) -> Any:
    """Restore integer-like DynamoDB Decimals to the strict YAML/JSON config type."""
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    if isinstance(value, dict):
        return {key: _restore_config_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_config_numbers(item) for item in value]
    return value


@dataclass(frozen=True)
class EksReadiness:
    registered_node_count: int
    ready_node_count: int
    observed_at: datetime | None = None


class DynamoEksReadinessStore:
    """Read operator-owned EKS readiness snapshots; never calls Kubernetes APIs."""
    def __init__(self, table_name: str, dynamodb: Any) -> None:
        self.table = dynamodb.Table(table_name)

    def get(self, target_id: str, region: str) -> EksReadiness | None:
        response = self.table.get_item(Key={"pk": f"TARGET#{target_id}", "sk": f"EKS_READINESS#{region}"}, ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            return None
        raw_time = item.get("observed_at")
        return EksReadiness(int(item.get("registered_node_count", 0)), int(item.get("ready_node_count", 0)), datetime.fromisoformat(raw_time) if raw_time else None)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
