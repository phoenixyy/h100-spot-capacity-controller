"""Local operator CLI with explicit acknowledgements for every AWS write."""

import argparse
import json
from dataclasses import asdict, replace
from pathlib import Path

from .config import ConfigurationError, load_target, load_target_mapping, target_from_mapping
from .discovery import discover_region, ec2_client
from .dryrun import integration_dry_run
from .failover import DynamoFailoverApprovalStore, build_failover_plan, plan_as_dict, target_configuration_version, utc_now
from .fleet import cleanup_owned_fleet, find_owned_fleet, find_owned_instances, fleet_request, observe_fleet_capacity, owned_capacity_inventory, terminate_owned_instances
from .launch_contract import inspect_launch_contract
from .notifications import NotificationDeduplicator, failover_event_notifier
from .placement import initial_placement
from .pricing import ondemand_caps
from .state import DynamoStateStore, DynamoTargetStore


def main() -> int:
    parser = argparse.ArgumentParser(prog="h100-spot-controller")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-target", "dry-run"):
        command = commands.add_parser(name)
        command.add_argument("path", type=Path)
        if name == "dry-run":
            command.add_argument("--profile", default="default")
    discover = commands.add_parser("discover", help="read-only AZ and offering discovery")
    discover.add_argument("path", type=Path)
    discover.add_argument("--profile", default="default")
    capacity_review = commands.add_parser("capacity-review", help="read-only owned capacity inventory across every candidate Region")
    capacity_review.add_argument("path", type=Path)
    capacity_review.add_argument("--profile", default="default")
    capacity_review.add_argument("--table", help="optional state table used to report the authoritative active Region")
    cleanup = commands.add_parser("cleanup", help="review or explicitly cancel owned capacity resources")
    cleanup.add_argument("path", type=Path)
    cleanup.add_argument("--profile", default="default")
    cleanup.add_argument("--table", help="optional state table used to resolve the post-failover active Region")
    cleanup.add_argument("--execute", action="store_true", help="cancel the owned maintain Fleet")
    cleanup.add_argument("--terminate-instances", action="store_true", help="also terminate all tagged target instances")
    target_review = commands.add_parser("target-review", help="read one persisted target configuration")
    target_review.add_argument("target_id")
    target_review.add_argument("--table", required=True)
    target_review.add_argument("--profile", default="default")
    target_put = commands.add_parser("target-put", help="conditionally persist a validated target configuration")
    target_put.add_argument("path", type=Path)
    target_put.add_argument("--table", required=True)
    target_put.add_argument("--profile", default="default")
    target_put.add_argument("--expected-version", type=int, help="required when replacing an existing target")
    target_put.add_argument("--execute", action="store_true")
    target_put.add_argument("--enable-capacity", action="store_true", help="additional acknowledgement for enabled targets")
    failover_request = commands.add_parser("failover-request", help="preview or persist a healthy-target whole-Region migration plan")
    failover_request.add_argument("target_id")
    failover_request.add_argument("--destination-region", required=True)
    failover_request.add_argument("--table", required=True, help="controller DynamoDB state table")
    failover_request.add_argument("--profile", default="default")
    failover_request.add_argument("--execute", action="store_true", help="persist and notify the plan; never changes EC2 capacity")
    for name in ("failover-review", "failover-approve", "failover-reject"):
        command = commands.add_parser(name, help="review or decide a persisted failover plan")
        command.add_argument("target_id")
        command.add_argument("plan_id")
        command.add_argument("--table", required=True, help="controller DynamoDB state table")
        command.add_argument("--profile", default="default")
        if name != "failover-review":
            command.add_argument("--execute", action="store_true", help="required acknowledgement before the DynamoDB write")
    args = parser.parse_args()
    if args.command == "target-review":
        import boto3
        table = boto3.Session(profile_name=args.profile).resource("dynamodb").Table(args.table)
        item = table.get_item(Key={"pk": f"TARGET#{args.target_id}", "sk": "CONFIG"}, ConsistentRead=True).get("Item")
        if item is None:
            parser.error("persisted target configuration not found")
        print(json.dumps({"target_id": args.target_id, "configuration_version": int(item["configuration_version"]), "config": item["config"], "aws_write": False}, default=str))
        return 0
    if args.command == "failover-request":
        import boto3
        try:
            session = boto3.Session(profile_name=args.profile)
            dynamodb = session.resource("dynamodb")
            table = dynamodb.Table(args.table)
            raw_target = DynamoTargetStore(args.table, dynamodb).get_mapping(args.target_id)
            if raw_target is None:
                raise ValueError("persisted target configuration not found")
            target = target_from_mapping(raw_target)
            if not target.enabled:
                raise ValueError("manual failover requires an enabled target")
            state = DynamoStateStore(args.table, dynamodb).get(target.target_id)
            if state is None:
                raise ValueError("target state not found; reconcile the enabled target first")
            target = replace(target, active_region=state.active_region)
            if args.destination_region == target.active_region:
                raise ValueError("destination Region must differ from the active Region")
            destination_inputs = target.region_inputs(args.destination_region)
            source_ec2 = session.client("ec2", region_name=target.active_region)
            destination_ec2 = session.client("ec2", region_name=args.destination_region)
            source_fleet = find_owned_fleet(source_ec2, target)
            if source_fleet is None:
                raise ValueError("no owned source Fleet exists")
            source_capacity = observe_fleet_capacity(source_ec2, source_fleet, target)
            if sum(source_capacity.by_zone.values()) < target.desired_instance_count:
                raise ValueError("manual failover requires a fully fulfilled source target")
            source_instances = find_owned_instances(source_ec2, target)
            destination_target = replace(target, active_region=args.destination_region)
            if find_owned_fleet(destination_ec2, destination_target) is not None or find_owned_instances(destination_ec2, destination_target):
                raise ValueError("destination Region already contains owned target capacity")
            inspection = inspect_launch_contract(destination_ec2, target, destination_inputs)
            if not inspection.valid:
                raise ValueError(f"destination launch contract invalid: {', '.join(inspection.violations)}")
            caps = ondemand_caps(
                session.client("pricing", region_name="us-east-1"), args.destination_region,
                tuple(item.name for item in target.instance_types),
            )
            now = utc_now()
            plan = build_failover_plan(
                target, target_configuration_version(target),
                tuple(sorted(item["InstanceId"] for item in source_instances)), now,
                source_fleet_id=source_fleet["FleetId"],
                notification_topic_arn=target.notification_topic_arn,
                destination_region=args.destination_region, trigger="operator-request",
            )
            payload = {
                "plan": plan_as_dict(plan), "aws_write": False, "ec2_write": False,
                "source_capacity": asdict(source_capacity),
                "destination_launch_contract": asdict(inspection),
                "destination_request_preview": fleet_request(destination_target, (), caps),
            }
            if args.execute:
                notifier = failover_event_notifier(
                    session.client("sns"), plan.notification_topic_arn,
                    NotificationDeduplicator(table), existing_eks=False,
                ) if plan.notification_topic_arn else None
                created = DynamoFailoverApprovalStore(table, notifier).put_current_plan(plan, now)
                if not created:
                    current = DynamoFailoverApprovalStore(table).get_current_plan(target.target_id)
                    if current is not None:
                        plan = current
                        payload["plan"] = plan_as_dict(plan)
                payload.update(aws_write=True, plan_persisted=created)
            print(json.dumps(payload, default=str))
            return 0
        except (ConfigurationError, ValueError, RuntimeError) as error:
            parser.error(str(error))
    if args.command.startswith("failover-"):
        if not getattr(args, "execute", False):
            if args.command == "failover-review":
                pass
            else:
                parser.error("failover decision is a DynamoDB write; repeat with --execute after review")
        import boto3
        try:
            session = boto3.Session(profile_name=args.profile)
            dynamodb = session.resource("dynamodb")
            table = dynamodb.Table(args.table)
            store = DynamoFailoverApprovalStore(table)
            plan = store.get_plan(args.target_id, args.plan_id)
            if args.command == "failover-review":
                print(json.dumps({"plan": plan_as_dict(plan), "aws_write": False}))
                return 0
            raw_target = DynamoTargetStore(args.table, dynamodb).get_mapping(args.target_id)
            if raw_target is None:
                raise ValueError("persisted target configuration not found")
            target = target_from_mapping(raw_target)
            notifier = failover_event_notifier(
                session.client("sns"), plan.notification_topic_arn or target.notification_topic_arn,
                NotificationDeduplicator(table),
                existing_eks=target.integration_mode == "existing-eks",
            )
            store = DynamoFailoverApprovalStore(table, notifier)
            operator_arn = session.client("sts").get_caller_identity()["Arn"]
            if args.command == "failover-approve":
                approval = store.approve(plan, utc_now(), operator_arn)
                print(json.dumps({"approved": True, "plan_id": approval.plan_id, "operator_arn": operator_arn}))
            else:
                store.reject(plan, utc_now(), operator_arn)
                store.clear_current_plan(plan)
                print(json.dumps({"rejected": True, "plan_id": plan.plan_id, "operator_arn": operator_arn}))
        except (ValueError, KeyError, RuntimeError) as error:
            parser.error(str(error))
        return 0
    try:
        target = load_target(args.path)
    except (ConfigurationError, RuntimeError) as error:
        parser.error(str(error))
    payload = {"target_id": target.target_id, "aws_write": False}
    if args.command == "capacity-review":
        import boto3
        session = boto3.Session(profile_name=args.profile)
        active_region = target.active_region
        if args.table:
            state = DynamoStateStore(args.table, session.resource("dynamodb")).get(target.target_id)
            if state is not None:
                active_region = state.active_region
        clients = {inputs.region: session.client("ec2", region_name=inputs.region) for inputs in target.candidate_regions}
        payload.update(
            active_region=active_region,
            regions=owned_capacity_inventory(clients, target),
        )
    elif args.command == "cleanup":
        if target.enabled:
            parser.error("cleanup requires a reviewed target with enabled: false")
        if args.table:
            import boto3
            dynamodb = boto3.Session(profile_name=args.profile).resource("dynamodb")
            state = DynamoStateStore(args.table, dynamodb).get(target.target_id)
            if state is not None:
                target = replace(target, active_region=state.active_region)
        client = ec2_client(args.profile, target.active_region)
        fleet = find_owned_fleet(client, target)
        instances = find_owned_instances(client, target)
        payload.update(
            active_region=target.active_region,
            owned_fleet_id=fleet.get("FleetId") if fleet else None,
            owned_instance_ids=[instance["InstanceId"] for instance in instances],
            terminate_instances=bool(args.terminate_instances),
        )
        if args.terminate_instances and not args.execute:
            parser.error("--terminate-instances requires --execute")
        if args.execute:
            cleanup_owned_fleet(client, target, fleet, explicitly_authorized=True, terminate_instances=False)
            terminated = terminate_owned_instances(client, target, instances, explicitly_authorized=args.terminate_instances)
            payload.update(aws_write=True, fleet_cancelled=fleet is not None, terminated_instance_ids=list(terminated))
    elif args.command == "target-put":
        if not args.execute:
            parser.error("target-put is a DynamoDB write; repeat with --execute after review")
        if target.enabled and not args.enable_capacity:
            parser.error("enabled target may create Spot capacity; repeat with --enable-capacity after dry-run review")
        import boto3
        raw = load_target_mapping(args.path)
        version = target_configuration_version(target)
        kwargs = {
            "Item": {"pk": f"TARGET#{target.target_id}", "sk": "CONFIG", "configuration_version": version, "config": raw},
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)" if args.expected_version is None else "configuration_version = :expected",
        }
        if args.expected_version is not None:
            kwargs["ExpressionAttributeValues"] = {":expected": args.expected_version}
        try:
            boto3.Session(profile_name=args.profile).resource("dynamodb").Table(args.table).put_item(**kwargs)
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                parser.error("target configuration changed or already exists; run target-review and supply its --expected-version")
            raise
        payload.update(aws_write=True, configuration_version=version, enabled=target.enabled)
    elif args.command == "validate-target":
        payload.update(valid=True, enabled=target.enabled)
    elif args.command == "dry-run":
        import boto3
        session = boto3.Session(profile_name=args.profile)
        clients = {item.region: session.client("ec2", region_name=item.region) for item in target.candidate_regions}
        quota_clients = {item.region: session.client("service-quotas", region_name=item.region) for item in target.candidate_regions}
        payload = integration_dry_run(
            target, clients, session.client("pricing", region_name="us-east-1"), utc_now(), quota_clients,
        )
    else:
        regions = []
        for inputs in target.candidate_regions:
            discovery = discover_region(
                ec2_client(args.profile, inputs.region), inputs.region,
                [item.name for item in target.instance_types], inputs.local_zone_placements,
            )
            regions.append({"region": discovery.region, "zones": [item.__dict__ for item in discovery.zones], "offerings": discovery.offered_instance_types_by_zone})
        payload.update(regions=regions)
    print(json.dumps(payload, default=str))
    return 0
