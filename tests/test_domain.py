from datetime import datetime, timedelta, timezone
import base64
from decimal import Decimal
from hashlib import sha256
import unittest

from h100_spot_controller.config import ConfigurationError, target_from_mapping
from h100_spot_controller.failover import DynamoFailoverApprovalStore, FailoverApproval, FailoverPlan, build_failover_plan, destination_allowed, execute_approved_failover, target_configuration_version
from h100_spot_controller.placement import PlacementState, initial_placement, next_placement_to_activate
from h100_spot_controller.signals import collect_candidate_signals, collect_spot_prices, collect_sps, collect_sps_regions
from h100_spot_controller.state import DynamoTargetStore, InMemoryStateStore, StateConflict, VersionedState
from h100_spot_controller.outcomes import ReconciliationOutcome
from h100_spot_controller.fleet import cleanup_owned_fleet, find_owned_fleet, find_owned_instances, fleet_request, fulfilled_by_zone, observe_fleet_capacity, owned_capacity_inventory, reconcile_existing_fleet, reconcile_fleet, terminate_owned_instances
from h100_spot_controller.discovery import discover_region
from h100_spot_controller.dryrun import integration_dry_run
from h100_spot_controller.metrics import eks_readiness_metric_data, metric_data, operational_metric_data, signal_metric_data, zone_metric_data
from h100_spot_controller.reconciliation import reconcile_target
from h100_spot_controller.handlers import collect as collect_handler, reconcile as reconcile_handler, spot_event
from h100_spot_controller.notifications import NotificationDeduplicator, failover_event_notifier, publish_controller_event, publish_once, shortfall_notification_due
from h100_spot_controller.pricing import linux_ondemand_hourly_price
from h100_spot_controller.launch_contract import inspect_launch_contract


def target_mapping(mode="standalone"):
    user_data_hash = sha256(b"bootstrap").hexdigest()
    data = {
        "target_id": "training", "enabled": False, "desired_instance_count": 1, "maximum_instance_count": 1,
        "active_region": "us-east-1", "integration_mode": mode,
        "instance_types": [{"name": "p5.48xlarge", "spot_price_cap_usd": "10", "h100_gpu_count": 8}],
        "candidate_regions": [
            {"region": "us-east-1", "launch_template_id": "lt-east", "launch_template_version": "1", "ami_id": "ami-east", "iam_instance_profile_arn": "arn:aws:iam::123:instance-profile/east", "security_group_ids": ["sg-east"], "bootstrap_contract_version": "standalone-v1", "user_data_sha256": user_data_hash, "root_volume_encrypted": True, "standard_placements": [{"subnet_id": "subnet-a", "zone_id": "use1-az1"}], "local_zone_placements": [{"subnet_id": "subnet-lz", "zone_id": "use1-nyc-1a"}]},
            {"region": "us-west-2", "launch_template_id": "lt-west", "launch_template_version": "1", "ami_id": "ami-west", "iam_instance_profile_arn": "arn:aws:iam::123:instance-profile/west", "security_group_ids": ["sg-west"], "bootstrap_contract_version": "standalone-v1", "user_data_sha256": user_data_hash, "root_volume_encrypted": True, "standard_placements": [{"subnet_id": "subnet-b", "zone_id": "usw2-az1"}]},
        ],
    }
    if mode == "existing-eks":
        for region in data["candidate_regions"]:
            region["bootstrap_contract_version"] = "eks-nodeadm-v1"
            region["eks"] = {
                "cluster_arn": f"arn:aws:eks:{region['region']}:123:cluster/training", "cluster_region": region["region"],
                "bootstrap_contract_version": "v1", "required_labels": {
                    "capacity-target-id": "training", "topology.kubernetes.io/region": region["region"],
                    "topology.kubernetes.io/zone": "from-instance-metadata", "node.kubernetes.io/instance-type": "from-instance-metadata",
                    "capacity-source": "spot",
                }, "gpu_taint": "nvidia.com/gpu=true:NoSchedule", "source_drain_procedure": "runbook:drain-and-delete-source-nodes",
            }
    return data


def validation_target_mapping(enabled=False):
    user_data_hash = sha256(b"bootstrap").hexdigest()
    return {
        "target_id": "g6e-validation", "accelerator_profile": "functional-validation",
        "enabled": enabled, "desired_instance_count": 1, "maximum_instance_count": 1,
        "active_region": "ap-northeast-1", "integration_mode": "standalone",
        "ownership_tags": {"purpose": "functional-validation"},
        "instance_types": [{"name": "g6e.xlarge"}],
        "candidate_regions": [
            {"region": "ap-northeast-1", "launch_template_id": "lt-tokyo", "launch_template_version": "1", "ami_id": "ami-tokyo", "iam_instance_profile_arn": "arn:aws:iam::123:instance-profile/validation", "security_group_ids": ["sg-tokyo"], "bootstrap_contract_version": "standalone-validation-v1", "user_data_sha256": user_data_hash, "root_volume_encrypted": True, "standard_placements": [{"subnet_id": "subnet-tokyo-c", "zone_id": "apne1-az1"}, {"subnet_id": "subnet-tokyo-a", "zone_id": "apne1-az4"}], "local_zone_placements": []},
            {"region": "ap-northeast-2", "launch_template_id": "lt-seoul", "launch_template_version": "1", "ami_id": "ami-seoul", "iam_instance_profile_arn": "arn:aws:iam::123:instance-profile/validation", "security_group_ids": ["sg-seoul"], "bootstrap_contract_version": "standalone-validation-v1", "user_data_sha256": user_data_hash, "root_volume_encrypted": True, "standard_placements": [{"subnet_id": "subnet-seoul-b", "zone_id": "apne2-az2"}, {"subnet_id": "subnet-seoul-a", "zone_id": "apne2-az1"}], "local_zone_placements": []},
        ],
    }


def launch_template_response(target, region):
    inputs = target.region_inputs(region)
    tags = [{"Key": key, "Value": value} for key, value in {**target.tags, "bootstrap-contract-version": inputs.bootstrap_contract_version}.items()]
    return {"LaunchTemplateVersions": [{"LaunchTemplateData": {
        "ImageId": inputs.ami_id,
        "IamInstanceProfile": {"Arn": inputs.iam_instance_profile_arn},
        "SecurityGroupIds": list(inputs.security_group_ids),
        "MetadataOptions": {"HttpTokens": "required"},
        "UserData": base64.b64encode(b"bootstrap").decode(),
        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"Encrypted": True, "DeleteOnTermination": True}}],
        "TagSpecifications": [{"ResourceType": "instance", "Tags": tags}, {"ResourceType": "volume", "Tags": tags}],
    }}]}


class DomainTests(unittest.TestCase):
    def test_disabled_one_machine_template(self):
        target = target_from_mapping(target_mapping())
        self.assertFalse(target.enabled)
        self.assertTrue(target.tags["Name"].startswith("Phoenix-Codex-Local-Spot-"))
        self.assertEqual(1, target.desired_instance_count)
        self.assertEqual("use1-az1", initial_placement(target).zone_id)

    def test_reject_non_h100_instance(self):
        data = target_mapping(); data["instance_types"][0]["name"] = "g5.48xlarge"
        with self.assertRaises(ConfigurationError): target_from_mapping(data)

    def test_h100_instance_types_have_authoritative_gpu_counts_and_reject_h200(self):
        one_gpu = target_mapping(); one_gpu["instance_types"] = [{"name": "p5.4xlarge"}]
        self.assertEqual(1, target_from_mapping(one_gpu).instance_types[0].h100_gpu_count)
        h200 = target_mapping(); h200["instance_types"] = [{"name": "p5e.48xlarge", "h100_gpu_count": 8}]
        with self.assertRaises(ConfigurationError): target_from_mapping(h200)
        wrong = target_mapping(); wrong["instance_types"][0]["h100_gpu_count"] = 1
        with self.assertRaises(ConfigurationError): target_from_mapping(wrong)

    def test_functional_validation_profile_is_exactly_bounded(self):
        target = target_from_mapping(validation_target_mapping())
        self.assertEqual("functional-validation", target.accelerator_profile)
        self.assertEqual("L40S", target.instance_types[0].accelerator_model)
        self.assertEqual(1, target.instance_types[0].accelerator_count)
        self.assertEqual(0, target.instance_types[0].h100_gpu_count)
        mutations = []
        wrong_type = validation_target_mapping(); wrong_type["instance_types"] = [{"name": "p5.4xlarge"}]; mutations.append(wrong_type)
        wrong_count = validation_target_mapping(); wrong_count["maximum_instance_count"] = 2; mutations.append(wrong_count)
        wrong_mode = validation_target_mapping(); wrong_mode["integration_mode"] = "existing-eks"; mutations.append(wrong_mode)
        wrong_region = validation_target_mapping(); wrong_region["candidate_regions"].reverse(); mutations.append(wrong_region)
        local_zone = validation_target_mapping(); local_zone["candidate_regions"][0]["local_zone_placements"] = [{"subnet_id": "subnet-lz", "zone_id": "apne1-lz1"}]; mutations.append(local_zone)
        no_purpose = validation_target_mapping(); no_purpose["ownership_tags"] = {}; mutations.append(no_purpose)
        for invalid in mutations:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ConfigurationError):
                    target_from_mapping(invalid)

    def test_l40s_validation_cannot_enter_h100_production_profile(self):
        data = target_mapping(); data["instance_types"] = [{"name": "g6e.xlarge"}]
        with self.assertRaises(ConfigurationError):
            target_from_mapping(data)

    def test_reject_cross_region_eks(self):
        data = target_mapping("existing-eks"); data["candidate_regions"][0]["eks"]["cluster_region"] = "us-west-2"
        with self.assertRaises(ConfigurationError): target_from_mapping(data)
        arn_mismatch = target_mapping("existing-eks"); arn_mismatch["candidate_regions"][0]["eks"]["cluster_arn"] = "arn:aws:eks:us-west-2:123:cluster/training"
        with self.assertRaises(ConfigurationError): target_from_mapping(arn_mismatch)

    def test_reject_duplicate_regions_missing_placements_and_invalid_machine_limits(self):
        duplicate = target_mapping(); duplicate["candidate_regions"][1]["region"] = "us-east-1"
        with self.assertRaises(ConfigurationError): target_from_mapping(duplicate)
        missing = target_mapping(); missing["candidate_regions"][0]["standard_placements"] = []
        with self.assertRaises(ConfigurationError): target_from_mapping(missing)
        invalid = target_mapping(); invalid["desired_instance_count"] = 2; invalid["maximum_instance_count"] = 1
        with self.assertRaises(ConfigurationError): target_from_mapping(invalid)
        rebalance = target_mapping(); rebalance["capacity_rebalancing"] = True
        with self.assertRaises(ConfigurationError): target_from_mapping(rebalance)

    def test_configuration_rejects_string_booleans_duplicate_pools_and_reserved_tag_overrides(self):
        string_false = target_mapping(); string_false["enabled"] = "false"
        with self.assertRaises(ConfigurationError): target_from_mapping(string_false)
        duplicate_type = target_mapping(); duplicate_type["instance_types"].append(dict(duplicate_type["instance_types"][0]))
        with self.assertRaises(ConfigurationError): target_from_mapping(duplicate_type)
        duplicate_zone = target_mapping(); duplicate_zone["candidate_regions"][0]["standard_placements"].append({"subnet_id": "subnet-other", "zone_id": "use1-az1"})
        with self.assertRaises(ConfigurationError): target_from_mapping(duplicate_zone)
        reserved = target_mapping(); reserved["ownership_tags"] = {"managed-by": "someone-else"}
        with self.assertRaises(ConfigurationError): target_from_mapping(reserved)

    def test_standard_zone_precedes_local_zone(self):
        data = target_mapping(); data["enabled"] = True; data["desired_instance_count"] = data["maximum_instance_count"] = 2
        data["candidate_regions"][0]["standard_placements"].append({"subnet_id": "subnet-c", "zone_id": "use1-az2"})
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        state = PlacementState(("use1-az1",), {"use1-az1": 1}, now - timedelta(minutes=15))
        self.assertEqual("use1-az2", next_placement_to_activate(target, state, now).zone_id)

    def test_each_sequential_zone_expansion_waits_a_full_interval(self):
        data = target_mapping(); data["enabled"] = True; data["desired_instance_count"] = data["maximum_instance_count"] = 3
        data["candidate_regions"][0]["standard_placements"].extend([
            {"subnet_id": "subnet-c", "zone_id": "use1-az2"},
            {"subnet_id": "subnet-d", "zone_id": "use1-az3"},
        ])
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        request = fleet_request(target, ("use1-az1",))
        fleet = {"FleetId": "fleet-1", "Type": "maintain", "Tags": [{"Key": key, "Value": value} for key, value in target.tags.items()], "LaunchTemplateConfigs": request["LaunchTemplateConfigs"], "TargetCapacitySpecification": request["TargetCapacitySpecification"]}
        class Client:
            def describe_fleets(self): return {"Fleets": [fleet]}
            def describe_fleet_instances(self, **kwargs): return {"ActiveInstances": []}
            def modify_fleet(self, **kwargs): pass
        store = InMemoryStateStore()
        store.put_if_version(VersionedState(target.target_id, 1, target.active_region, ("use1-az1",), now - timedelta(minutes=15)), None)
        reconcile_target(Client(), target, store, now)
        self.assertEqual(("use1-az1", "use1-az2"), store.get(target.target_id).active_zone_ids)
        self.assertEqual(now, store.get(target.target_id).shortfall_since)
        reconcile_target(Client(), target, store, now + timedelta(minutes=1))
        self.assertEqual(("use1-az1", "use1-az2"), store.get(target.target_id).active_zone_ids)
        reconcile_target(Client(), target, store, now + timedelta(minutes=15))
        self.assertEqual(("use1-az1", "use1-az2", "use1-az3"), store.get(target.target_id).active_zone_ids)

    def test_zone_with_existing_gpu_capacity_becomes_highest_priority(self):
        data = target_mapping(); data["enabled"] = True; data["desired_instance_count"] = data["maximum_instance_count"] = 2
        data["candidate_regions"][0]["standard_placements"].append({"subnet_id": "subnet-c", "zone_id": "use1-az2"})
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        request = fleet_request(target, ("use1-az1", "use1-az2"))
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        fleet = {"FleetId": "fleet-1", "Type": "maintain", "Tags": tags, "LaunchTemplateConfigs": request["LaunchTemplateConfigs"], "TargetCapacitySpecification": request["TargetCapacitySpecification"]}
        class Client:
            modified = []
            def describe_fleets(self): return {"Fleets": [fleet]}
            def describe_fleet_instances(self, **kwargs): return {"ActiveInstances": [{"InstanceId": "i-1"}]}
            def describe_instances(self, **kwargs): return {"Reservations": [{"Instances": [{"InstanceId": "i-1", "InstanceType": "p5.48xlarge", "State": {"Name": "running"}, "Placement": {"AvailabilityZoneId": "use1-az2"}, "Tags": tags}]}]}
            def modify_fleet(self, **kwargs): self.modified.append(kwargs)
        store = InMemoryStateStore(); store.put_if_version(VersionedState(target.target_id, 1, target.active_region, ("use1-az1", "use1-az2"), now), None)
        client = Client(); reconcile_target(client, target, store, now)
        overrides = client.modified[0]["LaunchTemplateConfigs"][0]["Overrides"]
        self.assertEqual("subnet-c", overrides[0]["SubnetId"])
        self.assertEqual(0, overrides[0]["Priority"])

    def test_destination_requires_zero_source(self):
        now = datetime.now(timezone.utc)
        plan = FailoverPlan("p", "training", 1, "us-east-1", "us-west-2", 2, ("i-1",), now + timedelta(minutes=30))
        approval = FailoverApproval("p", "training", 1, "us-east-1", "us-west-2", 2, now, source_instance_ids=("i-1",))
        self.assertFalse(destination_allowed(1, plan, approval, now))
        self.assertTrue(destination_allowed(0, plan, approval, now))

    def test_owned_instances_remain_visible_after_source_fleet_disappears(self):
        target = target_from_mapping(target_mapping())
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        class Client:
            def describe_instances(self, **kwargs):
                self.filters = kwargs["Filters"]
                return {"Reservations": [{"Instances": [
                    {"InstanceId": "i-owned", "State": {"Name": "shutting-down"}, "Tags": tags},
                    {"InstanceId": "i-foreign", "State": {"Name": "running"}, "Tags": []},
                ]}]}
        client = Client()
        self.assertEqual(["i-owned"], [item["InstanceId"] for item in find_owned_instances(client, target)])
        self.assertIn("shutting-down", client.filters[-1]["Values"])

    def test_failover_plan_is_full_target_and_approval_is_single_use(self):
        data = target_mapping(); data["desired_instance_count"] = data["maximum_instance_count"] = 3
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        plan = build_failover_plan(target, 7, ("i-1",), now)
        self.assertEqual(3, plan.desired_instance_count)
        self.assertEqual("us-west-2", plan.destination_region)
        class Table:
            calls = []
            def put_item(self, **kwargs): self.calls.append(kwargs)
        table = Table()
        approval = DynamoFailoverApprovalStore(table).approve(plan, now, "arn:aws:iam::123:user/operator")
        self.assertTrue(destination_allowed(0, plan, approval, now))
        self.assertIn("attribute_not_exists", table.calls[0]["ConditionExpression"])

    def test_destination_region_failure_requires_new_cyclic_whole_target_plan(self):
        target = target_from_mapping({**target_mapping(), "active_region": "us-west-2"})
        plan = build_failover_plan(target, 2, (), datetime.now(timezone.utc))
        self.assertEqual("us-west-2", plan.source_region)
        self.assertEqual("us-east-1", plan.destination_region)
        self.assertEqual(target.desired_instance_count, plan.desired_instance_count)

    def test_operator_requested_plan_is_exact_idempotent_and_conflict_safe(self):
        class Conditional(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        class Table:
            def __init__(self): self.items = {}
            def put_item(self, **kwargs):
                item = kwargs["Item"]
                key = (item["pk"], item["sk"])
                if key in self.items:
                    raise Conditional()
                self.items[key] = dict(item)
            def get_item(self, **kwargs):
                key = kwargs["Key"]
                item = self.items.get((key["pk"], key["sk"]))
                return {"Item": dict(item)} if item else {}
            def delete_item(self, **kwargs):
                key = kwargs["Key"]
                self.items.pop((key["pk"], key["sk"]), None)
        target = target_from_mapping(validation_target_mapping(enabled=True))
        now = datetime.now(timezone.utc)
        plan = build_failover_plan(
            target, target_configuration_version(target), ("i-tokyo",), now,
            source_fleet_id="fleet-tokyo", destination_region="ap-northeast-2",
            trigger="operator-request",
        )
        events = []
        store = DynamoFailoverApprovalStore(Table(), lambda event, value, emitted_at: events.append((event, value.trigger)))
        self.assertTrue(store.put_current_plan(plan, now))
        self.assertFalse(store.put_current_plan(plan, now))
        self.assertEqual(plan, store.get_current_plan(target.target_id))
        semantically_identical = build_failover_plan(
            target, target_configuration_version(target), ("i-tokyo",), now + timedelta(seconds=1),
            source_fleet_id="fleet-tokyo", destination_region="ap-northeast-2",
            trigger="operator-request",
        )
        self.assertNotEqual(plan.plan_id, semantically_identical.plan_id)
        self.assertFalse(store.put_current_plan(semantically_identical, now + timedelta(seconds=1)))
        conflicting = build_failover_plan(
            target, target_configuration_version(target), ("i-replaced",), now + timedelta(seconds=1),
            source_fleet_id="fleet-replaced", destination_region="ap-northeast-2",
            trigger="operator-request",
        )
        with self.assertRaises(ValueError):
            store.put_current_plan(conflicting, now + timedelta(seconds=1))
        self.assertEqual([("failover_approval_required", "operator-request")], events)

    def test_operator_requested_plan_still_needs_matching_approval(self):
        target = target_from_mapping(validation_target_mapping(enabled=True))
        now = datetime.now(timezone.utc)
        plan = build_failover_plan(
            target, target_configuration_version(target), ("i-tokyo",), now,
            source_fleet_id="fleet-tokyo", destination_region="ap-northeast-2",
            trigger="operator-request",
        )
        used = FailoverApproval(
            plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region,
            plan.destination_region, 1, now, used=True,
            source_fleet_id=plan.source_fleet_id, source_instance_ids=plan.source_instance_ids,
        )
        class Client:
            def __getattr__(self, name): raise AssertionError(f"manual plan without approval called {name}")
        outcome = execute_approved_failover(Client(), Client(), target, plan, used, None, [], now)
        self.assertEqual("awaiting_approval", outcome.kind)

    def test_failover_waits_for_zero_source_before_destination_request(self):
        data = target_mapping(); data["enabled"] = True; data["desired_instance_count"] = data["maximum_instance_count"] = 2
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        plan = build_failover_plan(target, target_configuration_version(target), ("i-owned",), now, source_fleet_id="fleet-source")
        approval = FailoverApproval(plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region, plan.destination_region, 2, now, source_fleet_id=plan.source_fleet_id, source_instance_ids=plan.source_instance_ids)
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        class Source:
            terminated = []; deleted = []
            def delete_fleets(self, **kwargs): self.deleted.append(kwargs)
            def terminate_instances(self, **kwargs): self.terminated.append(kwargs)
        class Destination:
            created = []
            def create_fleet(self, **kwargs): self.created.append(kwargs)
        source, destination = Source(), Destination()
        fleet = {"FleetId": "fleet-source", "Tags": tags}
        first = execute_approved_failover(source, destination, target, plan, approval, fleet, [{"InstanceId": "i-owned", "Tags": tags}], now)
        self.assertEqual("source_terminating", first.kind)
        self.assertEqual([], destination.created)
        second = execute_approved_failover(source, destination, target, plan, approval, None, [], now)
        self.assertEqual("failover_complete", second.kind)
        self.assertEqual(2, destination.created[0]["TargetCapacitySpecification"]["TotalTargetCapacity"])

    def test_execution_claim_happens_before_any_source_mutation(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        plan = build_failover_plan(
            target, target_configuration_version(target), ("i-owned",), now,
            source_fleet_id="fleet-source",
        )
        approval = FailoverApproval(
            plan.plan_id, plan.target_id, plan.configuration_version,
            plan.source_region, plan.destination_region, 1, now,
            source_fleet_id=plan.source_fleet_id, source_instance_ids=plan.source_instance_ids,
        )
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        events = []
        class Source:
            def delete_fleets(self, **kwargs): events.append("delete")
            def terminate_instances(self, **kwargs): events.append("terminate")
        def claim(value, claimed_at):
            events.append("claim")
            return True
        outcome = execute_approved_failover(
            Source(), object(), target, plan, approval,
            {"FleetId": "fleet-source", "Tags": tags},
            [{"InstanceId": "i-owned", "Tags": tags}], now, claim,
        )
        self.assertEqual("source_terminating", outcome.kind)
        self.assertEqual(["claim", "delete", "terminate"], events)

    def test_stopping_empty_source_fleet_still_requires_a_later_zero_observation(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        plan = build_failover_plan(target, target_configuration_version(target), (), now, source_fleet_id="fleet-source")
        approval = FailoverApproval(plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region, plan.destination_region, 1, now, source_fleet_id="fleet-source")
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        class Source:
            def delete_fleets(self, **kwargs): pass
        class Destination:
            def create_fleet(self, **kwargs): raise AssertionError("must wait for a later zero-source observation")
        outcome = execute_approved_failover(Source(), Destination(), target, plan, approval, {"FleetId": "fleet-source", "Tags": tags}, [], now)
        self.assertEqual("source_terminating", outcome.kind)

    def test_failover_approval_rejects_changed_source_resources(self):
        now = datetime.now(timezone.utc)
        plan = FailoverPlan("p", "training", 1, "us-east-1", "us-west-2", 1, ("i-original",), now + timedelta(minutes=30), "fleet-original")
        changed = FailoverApproval("p", "training", 1, "us-east-1", "us-west-2", 1, now, source_fleet_id="fleet-replaced", source_instance_ids=("i-original",))
        self.assertFalse(destination_allowed(0, plan, changed, now))

    def test_expired_or_used_approval_cannot_make_failover_writes(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        plan = build_failover_plan(target, target_configuration_version(target), (), now - timedelta(minutes=31))
        used = FailoverApproval(plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region, plan.destination_region, 1, now, used=True)
        class Client:
            def __getattr__(self, name): raise AssertionError(f"must not call {name}")
        outcome = execute_approved_failover(Client(), Client(), target, plan, used, None, [], now)
        self.assertEqual("awaiting_approval", outcome.kind)

    def test_unowned_source_instance_is_never_terminated(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        plan = build_failover_plan(target, target_configuration_version(target), (), now)
        approval = FailoverApproval(plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region, plan.destination_region, 1, now)
        class Source:
            terminated = []
            def terminate_instances(self, **kwargs): self.terminated.append(kwargs)
        class Destination:
            created = []
            def create_fleet(self, **kwargs): self.created.append(kwargs)
        source, destination = Source(), Destination()
        execute_approved_failover(source, destination, target, plan, approval, None, [{"InstanceId": "i-foreign", "Tags": []}], now)
        self.assertEqual([], source.terminated)
        self.assertEqual(1, len(destination.created))

    def test_destination_creation_requires_atomic_approval_consumption(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data); now = datetime.now(timezone.utc)
        plan = build_failover_plan(target, target_configuration_version(target), (), now)
        approval = FailoverApproval(plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region, plan.destination_region, 1, now)
        class Destination:
            def create_fleet(self, **kwargs): raise AssertionError("approval claim failed")
        outcome = execute_approved_failover(object(), Destination(), target, plan, approval, None, [], now, lambda _: False)
        self.assertEqual("awaiting_approval", outcome.kind)

    def test_atomic_approval_consumption_matches_exact_source_resources(self):
        now = datetime.now(timezone.utc)
        plan = FailoverPlan("p", "training", 1, "us-east-1", "us-west-2", 1, ("i-1",), now + timedelta(minutes=30), "fleet-1")
        class Table:
            kwargs = None
            def update_item(self, **kwargs): self.kwargs = kwargs
        table = Table()
        self.assertTrue(DynamoFailoverApprovalStore(table).consume_approval(plan))
        self.assertIn("source_fleet_id = :fleet", table.kwargs["ConditionExpression"])
        self.assertEqual("fleet-1", table.kwargs["ExpressionAttributeValues"][":fleet"])
        self.assertEqual(["i-1"], table.kwargs["ExpressionAttributeValues"][":instances"])

    def test_sps_rate_limit_is_degraded_not_capacity_failure(self):
        class LimitError(Exception):
            response = {"Error": {"Code": "MaxConfigLimitExceeded"}}
        class Client:
            def get_spot_placement_scores(self, **kwargs):
                raise LimitError()
        observation = collect_sps(Client(), target_from_mapping(target_mapping()), "us-east-1")
        self.assertEqual("rate_limited", observation.status)
        self.assertEqual("MaxConfigLimitExceeded", observation.error_code)

    def test_signal_failures_publish_separate_error_metrics(self):
        class AwsError(Exception):
            response = {"Error": {"Code": "RequestLimitExceeded"}}
        class Client:
            def get_spot_placement_scores(self, **kwargs): raise AwsError()
            def describe_spot_price_history(self, **kwargs): raise AwsError()
        target = target_from_mapping(target_mapping())
        snapshot = collect_candidate_signals(
            target,
            {"us-east-1": Client(), "us-west-2": Client()},
            {"us-east-1": Client(), "us-west-2": Client()},
            {},
        )
        errors = [item for item in signal_metric_data(target.target_id, snapshot) if item["MetricName"] == "CapacitySignalError"]
        self.assertEqual(4, len(errors))
        self.assertEqual({"sps", "spot-price"}, {
            next(dimension["Value"] for dimension in item["Dimensions"] if dimension["Name"] == "SignalType")
            for item in errors
        })

    def test_shortfall_notification_waits_for_configured_continuous_interval(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(shortfall_notification_due(None, 15, now))
        self.assertFalse(shortfall_notification_due(now - timedelta(minutes=14), 15, now))
        self.assertTrue(shortfall_notification_due(now - timedelta(minutes=15), 15, now))

    def test_sps_compares_candidate_regions_in_one_stable_configuration(self):
        class Client:
            calls = []
            def get_spot_placement_scores(self, **kwargs):
                self.calls.append(kwargs)
                return {"SpotPlacementScores": [
                    {"Region": "us-east-1", "AvailabilityZoneId": "use1-az1", "Score": 4},
                    {"Region": "us-west-2", "AvailabilityZoneId": "usw2-az1", "Score": 7},
                ]}
        client = Client()
        observations = collect_sps_regions(client, target_from_mapping(target_mapping()), ("us-east-1", "us-west-2"))
        self.assertEqual(1, len(client.calls))
        self.assertEqual(["us-east-1", "us-west-2"], client.calls[0]["RegionNames"])
        self.assertEqual(4, observations["us-east-1"].scores[0]["Score"])
        self.assertEqual(7, observations["us-west-2"].scores[0]["Score"])

    def test_spot_price_collection_is_read_only_and_does_not_choose_a_region(self):
        class Client:
            def describe_spot_price_history(self, **kwargs):
                self.kwargs = kwargs
                return {"SpotPriceHistory": [{"InstanceType": "p5.48xlarge", "SpotPrice": "9.99", "AvailabilityZone": "us-east-1a"}]}
        client = Client()
        observation = collect_spot_prices(client, target_from_mapping(target_mapping()), "us-east-1")
        self.assertEqual("ok", observation.status)
        self.assertEqual("p5.48xlarge", observation.prices[0]["InstanceType"])
        self.assertNotIn("RegionNames", client.kwargs)

    def test_candidate_signal_snapshot_collects_all_approved_regions_without_authorizing_failover(self):
        class Client:
            def get_spot_placement_scores(self, **kwargs): return {"SpotPlacementScores": []}
            def describe_spot_price_history(self, **kwargs): return {"SpotPriceHistory": []}
        target = target_from_mapping(target_mapping())
        clients = {region.region: Client() for region in target.candidate_regions}
        snapshot = collect_candidate_signals(target, clients, clients, {})
        self.assertEqual({"us-east-1", "us-west-2"}, set(snapshot.sps_by_region))
        self.assertEqual({}, snapshot.local_zone_eligibility)

    def test_scheduled_collectors_do_not_call_sps_on_five_minute_price_schedule(self):
        class Client:
            sps_calls = 0
            def describe_availability_zones(self, **kwargs): return {"AvailabilityZones": []}
            def describe_instance_type_offerings(self, **kwargs): return {"InstanceTypeOfferings": []}
            def describe_subnets(self, **kwargs): return {"Subnets": []}
            def describe_spot_price_history(self, **kwargs): return {"SpotPriceHistory": []}
            def get_spot_placement_scores(self, **kwargs): self.sps_calls += 1; return {"SpotPlacementScores": []}
        clients = {"us-east-1": Client(), "us-west-2": Client()}
        result = collect_handler({"target": target_mapping(), "collection": "price-and-local"}, object(), clients=clients)
        self.assertEqual("ok", result["status"])
        self.assertEqual({}, result["sps"])
        self.assertEqual(0, sum(client.sps_calls for client in clients.values()))
        result = collect_handler({"target": target_mapping(), "collection": "sps"}, object(), clients=clients)
        self.assertEqual({}, result["prices"])
        self.assertEqual(1, sum(client.sps_calls for client in clients.values()))

    def test_integration_dry_run_reports_full_destination_request_without_writes(self):
        class Client:
            def describe_availability_zones(self, **kwargs):
                return {"AvailabilityZones": [{"ZoneName": "zone-a", "ZoneId": "use1-az1", "ZoneType": "availability-zone", "OptInStatus": "opt-in-not-required"}]}
            def describe_instance_type_offerings(self, **kwargs): return {"InstanceTypeOfferings": []}
            def describe_subnets(self, **kwargs): return {"Subnets": []}
            def describe_fleets(self): return {"Fleets": []}
            def get_spot_placement_scores(self, **kwargs): return {"SpotPlacementScores": []}
            def __getattr__(self, name):
                if name in {"create_fleet", "modify_fleet", "delete_fleets", "terminate_instances"}: raise AssertionError("dry-run attempted an AWS write")
                raise AttributeError(name)
        class Pricing:
            def get_products(self, **kwargs):
                return {"PriceList": ['{"terms":{"OnDemand":{"term":{"priceDimensions":{"hour":{"unit":"Hrs","pricePerUnit":{"USD":"55.04"}}}}}}}']}
        target = target_from_mapping(target_mapping())
        report = integration_dry_run(target, {"us-east-1": Client(), "us-west-2": Client()}, Pricing(), datetime.now(timezone.utc))
        self.assertFalse(report["aws_write"])
        self.assertEqual(1, report["regions"]["us-west-2"]["request_preview"]["TargetCapacitySpecification"]["TotalTargetCapacity"])
        self.assertEqual(["use1-az1", "use1-nyc-1a"], report["regions"]["us-east-1"]["zone_expansion_order"])

    def test_validation_dry_run_reports_tokyo_seoul_quota_accelerator_and_manual_plan(self):
        mapping = validation_target_mapping()
        mapping["notification_topic_arn"] = "arn:aws:sns:ap-northeast-1:123:validation"
        target = target_from_mapping(mapping)
        class Client:
            def __init__(self, region): self.region = region
            def describe_availability_zones(self, **kwargs):
                inputs = target.region_inputs(self.region)
                return {"AvailabilityZones": [{"ZoneName": f"{self.region}{index}", "ZoneId": placement.zone_id, "ZoneType": "availability-zone", "OptInStatus": "opt-in-not-required"} for index, placement in enumerate(inputs.standard_placements)]}
            def describe_instance_type_offerings(self, **kwargs):
                return {"InstanceTypeOfferings": [{"Location": placement.zone_id, "InstanceType": "g6e.xlarge"} for placement in target.region_inputs(self.region).standard_placements]}
            def describe_subnets(self, **kwargs):
                return {"Subnets": [{"SubnetId": placement.subnet_id, "AvailabilityZoneId": placement.zone_id} for placement in target.region_inputs(self.region).standard_placements]}
            def describe_launch_template_versions(self, **kwargs): return launch_template_response(target, self.region)
            def describe_fleets(self): return {"Fleets": []}
            def get_spot_placement_scores(self, **kwargs):
                return {"SpotPlacementScores": [{"Region": region, "Score": 3} for region in kwargs["RegionNames"]]}
            def __getattr__(self, name):
                if name in {"create_fleet", "modify_fleet", "delete_fleets", "terminate_instances"}: raise AssertionError("validation dry-run attempted an AWS write")
                raise AttributeError(name)
        class Pricing:
            def get_products(self, **kwargs):
                return {"PriceList": ['{"terms":{"OnDemand":{"term":{"priceDimensions":{"hour":{"unit":"Hrs","pricePerUnit":{"USD":"2.699"}}}}}}}']}
        class Quota:
            def get_service_quota(self, **kwargs): return {"Quota": {"Value": 64.0, "Adjustable": True}}
        clients = {region: Client(region) for region in ("ap-northeast-1", "ap-northeast-2")}
        report = integration_dry_run(target, clients, Pricing(), datetime.now(timezone.utc), {region: Quota() for region in clients})
        self.assertFalse(report["aws_write"])
        self.assertEqual("functional-validation", report["accelerator_profile"])
        self.assertEqual("L40S", report["accelerators"][0]["model"])
        self.assertEqual(0, report["accelerators"][0]["h100_count_per_machine"])
        self.assertEqual("operator-request", report["manual_failover_plan_preview"]["trigger"])
        self.assertEqual("ap-northeast-2", report["manual_failover_plan_preview"]["destination_region"])
        self.assertEqual(target.notification_topic_arn, report["manual_failover_plan_preview"]["notification_topic_arn"])
        self.assertEqual(target.notification_topic_arn, report["automatic_failover_plan_preview"]["notification_topic_arn"])
        for region in clients:
            self.assertTrue(report["regions"][region]["standard_placements"]["valid"])
            self.assertEqual(64.0, report["regions"][region]["g_and_vt_spot_vcpu_quota"]["value_vcpus"])

    def test_state_updates_require_the_expected_version(self):
        store = InMemoryStateStore()
        first = VersionedState("training", 1, "us-east-1", ("use1-az1",))
        store.put_if_version(first, None)
        with self.assertRaises(StateConflict):
            store.put_if_version(VersionedState("training", 2, "us-east-1", ("use1-az1", "use1-az2")), 0)

    def test_only_capacity_shortfall_advances_zone_timer(self):
        self.assertTrue(ReconciliationOutcome("shortfall", "training", "us-east-1", 2, 1).advances_zone_timer)
        self.assertFalse(ReconciliationOutcome("rate_limited", "training", "us-east-1", 2, 1).advances_zone_timer)

    def test_metrics_include_machine_shortfall_and_realized_gpu_count(self):
        data = metric_data(ReconciliationOutcome("shortfall", "training", "us-east-1", 3, 1), 8, 2)
        by_name = {item["MetricName"]: item["Value"] for item in data}
        self.assertEqual(2, by_name["MachineShortfall"])
        self.assertEqual(8, by_name["RealizedH100GpuCount"])

    def test_validation_metrics_report_l40s_and_zero_h100(self):
        data = metric_data(
            ReconciliationOutcome("healthy", "g6e-validation", "ap-northeast-1", 1, 1),
            0, 1, accelerator_profile="functional-validation",
            realized_accelerator_count=1, accelerator_counts_by_model={"L40S": 1},
        )
        by_name = {item["MetricName"]: item for item in data}
        self.assertEqual(1, by_name["RealizedAcceleratorCount"]["Value"])
        self.assertEqual(0, by_name["RealizedH100GpuCount"]["Value"])
        model_metric = by_name["RealizedAcceleratorModelCount"]
        self.assertIn({"Name": "AcceleratorModel", "Value": "L40S"}, model_metric["Dimensions"])

    def test_eks_readiness_metrics_are_separate_from_machine_capacity(self):
        names = {item["MetricName"] for item in eks_readiness_metric_data("training", "us-east-1", 2, 1)}
        self.assertEqual({"EksRegisteredNodeCount", "EksReadyNodeCount"}, names)
        self.assertNotIn("DesiredMachineCapacity", names)

    def test_operational_metrics_cover_failover_retries_and_interruptions(self):
        names = {item["MetricName"] for item in operational_metric_data("training", "us-east-1", "awaiting_approval", 2, 1)}
        self.assertEqual({"FailoverState", "FailoverTrigger", "RetryCount", "InterruptionCount"}, names)

    def test_zone_metrics_report_activation_and_capacity_separately(self):
        data = zone_metric_data("training", "us-east-1", ("use1-az1", "use1-az2"), {"use1-az1": 1})
        self.assertEqual(4, len(data))

    def test_disabled_target_never_calls_create_fleet(self):
        class Client:
            def create_fleet(self, **kwargs):
                raise AssertionError("disabled target must not create a fleet")
        target = target_from_mapping(target_mapping())
        outcome = reconcile_fleet(Client(), target, None, ("use1-az1",), 0)
        self.assertEqual("disabled", outcome.kind)

    def test_reconciliation_initializes_zone_state_without_writing_disabled_capacity(self):
        class Client:
            def describe_fleets(self): return {"Fleets": []}
            def create_fleet(self, **kwargs): raise AssertionError("disabled capacity must not be created")
        target = target_from_mapping(target_mapping())
        store = InMemoryStateStore()
        outcome = reconcile_target(Client(), target, store, datetime.now(timezone.utc))
        self.assertEqual("disabled", outcome.kind)
        self.assertEqual(("use1-az1",), store.get("training").active_zone_ids)

    def test_lambda_reconciliation_requires_explicit_target_configuration(self):
        result = reconcile_handler({}, object())
        self.assertEqual("configuration_error", result["status"])
        self.assertFalse(result["aws_write"])

        class MissingTarget:
            def get_mapping(self, target_id): return None
        result = reconcile_handler({"target_id": "not-persisted"}, object(), target_store=MissingTarget())
        self.assertEqual("configuration_error", result["status"])
        self.assertEqual("persisted target configuration not found: not-persisted", result["error"])
        self.assertFalse(result["aws_write"])

    def test_lambda_can_load_persisted_target_by_id(self):
        class Targets:
            def get_mapping(self, target_id): return target_mapping()
        class Client:
            def describe_fleets(self): return {"Fleets": []}
        class Pricing:
            def get_products(self, **kwargs):
                return {"PriceList": ['{"terms":{"OnDemand":{"term":{"priceDimensions":{"hour":{"unit":"Hrs","pricePerUnit":{"USD":"55.04"}}}}}}}']}
        result = reconcile_handler({"target_id": "training"}, object(), clients={"us-east-1": Client()}, store=InMemoryStateStore(), pricing=Pricing(), target_store=Targets())
        self.assertEqual("disabled", result["status"])

    def test_dynamo_target_store_restores_strict_integer_config_types(self):
        persisted = target_mapping()
        for key in (
            "desired_instance_count", "maximum_instance_count", "zone_expansion_minutes",
            "region_failover_minutes", "failover_approval_minutes",
        ):
            if key in persisted:
                persisted[key] = Decimal(persisted[key])
        persisted["instance_types"][0]["h100_gpu_count"] = Decimal(8)

        class Table:
            def get_item(self, **kwargs):
                return {"Item": {"config": persisted}}
        class Dynamo:
            def Table(self, name): return Table()

        restored = DynamoTargetStore("state", Dynamo()).get_mapping("training")
        target = target_from_mapping(restored)
        self.assertEqual(1, target.desired_instance_count)
        self.assertEqual(8, target.instance_types[0].h100_gpu_count)
        self.assertIs(type(restored["desired_instance_count"]), int)

    def test_lambda_classifies_authorization_and_rate_limit_api_failures_without_writes(self):
        class AwsError(Exception):
            def __init__(self, code): self.response = {"Error": {"Code": code}}
        class Client:
            def __init__(self, code): self.code = code
            def describe_fleets(self): raise AwsError(self.code)
        class Pricing:
            def get_products(self, **kwargs): return {"PriceList": ['{"terms":{"OnDemand":{"term":{"priceDimensions":{"hour":{"unit":"Hrs","pricePerUnit":{"USD":"55.04"}}}}}}}']}
        target = target_mapping()
        denied = reconcile_handler({"target": target}, object(), clients={"us-east-1": Client("AccessDenied")}, store=InMemoryStateStore(), pricing=Pricing())
        limited = reconcile_handler({"target": target}, object(), clients={"us-east-1": Client("RequestLimitExceeded")}, store=InMemoryStateStore(), pricing=Pricing())
        self.assertEqual("authorization_error", denied["status"])
        self.assertEqual("rate_limited", limited["status"])
        self.assertFalse(denied["aws_write"])
        self.assertFalse(limited["aws_write"])

    def test_lambda_executes_approved_whole_target_failover_without_recreating_source(self):
        data = target_mapping(); data["enabled"] = True; data["instance_types"][0].pop("spot_price_cap_usd")
        target = target_from_mapping(data)
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        request = fleet_request(target, ("use1-az1",), {"p5.48xlarge": 55.04})
        fleet = {"FleetId": "fleet-source", "Type": "maintain", "Tags": tags, "LaunchTemplateConfigs": request["LaunchTemplateConfigs"], "TargetCapacitySpecification": request["TargetCapacitySpecification"]}

        class Source:
            fleet_exists = True
            instance_exists = True
            def describe_fleets(self): return {"Fleets": [fleet] if self.fleet_exists else []}
            def describe_launch_template_versions(self, **kwargs): return launch_template_response(target, "us-east-1")
            def describe_fleet_instances(self, **kwargs): return {"ActiveInstances": [{"InstanceId": "i-owned"}] if self.instance_exists else []}
            def describe_instances(self, **kwargs):
                instances = [{"InstanceId": "i-owned", "InstanceType": "p5.48xlarge", "State": {"Name": "running"}, "Placement": {"AvailabilityZoneId": "use1-az1"}, "Tags": tags}] if self.instance_exists else []
                return {"Reservations": [{"Instances": instances}]}
            def delete_fleets(self, **kwargs): self.fleet_exists = False
            def terminate_instances(self, **kwargs): self.instance_exists = False
            def create_fleet(self, **kwargs): raise AssertionError("approved failover must not recreate source capacity")
        class Destination:
            created = []
            def describe_fleets(self): return {"Fleets": []}
            def describe_launch_template_versions(self, **kwargs): return launch_template_response(target, "us-west-2")
            def create_fleet(self, **kwargs): self.created.append(kwargs)
        class Pricing:
            def get_products(self, **kwargs): return {"PriceList": ['{"terms":{"OnDemand":{"term":{"priceDimensions":{"hour":{"unit":"Hrs","pricePerUnit":{"USD":"55.04"}}}}}}}']}
        class Failovers:
            plan = None
            consumed = False
            def put_plan(self, plan, now):
                if self.plan is None: self.plan = plan
                return self.plan is plan
            def get_plan(self, target_id, plan_id): return self.plan
            def is_rejected(self, plan): return False
            def get_approval(self, plan): return FailoverApproval(plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region, plan.destination_region, plan.desired_instance_count, datetime.now(timezone.utc), source_fleet_id=plan.source_fleet_id, source_instance_ids=plan.source_instance_ids)
            def consume_approval(self, plan): self.consumed = True; return True
        now = datetime.now(timezone.utc)
        store = InMemoryStateStore()
        store.put_if_version(VersionedState(target.target_id, 1, target.active_region, ("use1-az1", "use1-nyc-1a"), now - timedelta(minutes=45), now - timedelta(minutes=31)), None)
        source, destination, failovers = Source(), Destination(), Failovers()
        first = reconcile_handler({"target": data}, object(), clients={"us-east-1": source, "us-west-2": destination}, store=store, pricing=Pricing(), failover_store=failovers)
        self.assertEqual("source_terminating", first["status"])
        self.assertEqual([], destination.created)
        second = reconcile_handler({"target": data}, object(), clients={"us-east-1": source, "us-west-2": destination}, store=store, pricing=Pricing(), failover_store=failovers)
        self.assertEqual("failover_complete", second["status"])
        self.assertEqual("us-west-2", store.get(target.target_id).active_region)
        self.assertEqual(1, destination.created[0]["TargetCapacitySpecification"]["TotalTargetCapacity"])

    def test_lambda_executes_operator_requested_tokyo_to_seoul_plan_through_same_barrier(self):
        data = validation_target_mapping(enabled=True)
        target = target_from_mapping(data)
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        request = fleet_request(target, ("apne1-az1",), {"g6e.xlarge": 2.699})
        fleet = {"FleetId": "fleet-tokyo", "Type": "maintain", "Tags": tags, "LaunchTemplateConfigs": request["LaunchTemplateConfigs"], "TargetCapacitySpecification": request["TargetCapacitySpecification"]}
        now = datetime.now(timezone.utc)
        plan = build_failover_plan(
            target, target_configuration_version(target), ("i-tokyo",), now,
            source_fleet_id="fleet-tokyo", destination_region="ap-northeast-2",
            trigger="operator-request",
        )
        class Tokyo:
            fleet_exists = True; instance_exists = True
            def describe_fleets(self): return {"Fleets": [fleet] if self.fleet_exists else []}
            def describe_launch_template_versions(self, **kwargs): return launch_template_response(target, "ap-northeast-1")
            def describe_fleet_instances(self, **kwargs): return {"ActiveInstances": [{"InstanceId": "i-tokyo"}] if self.instance_exists else []}
            def describe_instances(self, **kwargs):
                instances = [{"InstanceId": "i-tokyo", "InstanceType": "g6e.xlarge", "State": {"Name": "running"}, "Placement": {"AvailabilityZoneId": "apne1-az1"}, "Tags": tags}] if self.instance_exists else []
                return {"Reservations": [{"Instances": instances}]}
            def delete_fleets(self, **kwargs): self.fleet_exists = False
            def terminate_instances(self, **kwargs): self.instance_exists = False
            def create_fleet(self, **kwargs): raise AssertionError("manual migration recreated Tokyo")
        class Seoul:
            created = []
            def describe_fleets(self): return {"Fleets": []}
            def describe_launch_template_versions(self, **kwargs): return launch_template_response(target, "ap-northeast-2")
            def create_fleet(self, **kwargs): self.created.append(kwargs)
        class Pricing:
            def get_products(self, **kwargs): return {"PriceList": ['{"terms":{"OnDemand":{"term":{"priceDimensions":{"hour":{"unit":"Hrs","pricePerUnit":{"USD":"2.699"}}}}}}}']}
        class Failovers:
            cleared = False; consumed = False
            def get_current_plan(self, target_id): return None if self.cleared else plan
            def is_rejected(self, value): return False
            def get_approval(self, value): return FailoverApproval(value.plan_id, value.target_id, value.configuration_version, value.source_region, value.destination_region, value.desired_instance_count, now, source_fleet_id=value.source_fleet_id, source_instance_ids=value.source_instance_ids)
            def consume_approval(self, value): self.consumed = True; return True
            def clear_current_plan(self, value): self.cleared = True
            def get_plan(self, target_id, plan_id): return plan
        store = InMemoryStateStore()
        store.put_if_version(VersionedState(target.target_id, 1, target.active_region, ("apne1-az1",)), None)
        tokyo, seoul, failovers = Tokyo(), Seoul(), Failovers()
        first = reconcile_handler({"target": data}, object(), clients={"ap-northeast-1": tokyo, "ap-northeast-2": seoul}, store=store, pricing=Pricing(), failover_store=failovers)
        self.assertEqual("source_terminating", first["status"])
        self.assertEqual([], seoul.created)
        second = reconcile_handler({"target": data}, object(), clients={"ap-northeast-1": tokyo, "ap-northeast-2": seoul}, store=store, pricing=Pricing(), failover_store=failovers)
        self.assertEqual("failover_complete", second["status"])
        self.assertTrue(failovers.consumed)
        self.assertTrue(failovers.cleared)
        self.assertEqual("ap-northeast-2", store.get(target.target_id).active_region)
        self.assertEqual(1, seoul.created[0]["TargetCapacitySpecification"]["TotalTargetCapacity"])

    def test_lambda_retries_same_claimed_failover_after_destination_create_error(self):
        data = validation_target_mapping(enabled=True)
        target = target_from_mapping(data)
        now = datetime.now(timezone.utc)
        plan = build_failover_plan(
            target, target_configuration_version(target), (), now,
            source_fleet_id=None, destination_region="ap-northeast-2",
            trigger="operator-request",
        )

        class Tokyo:
            def describe_fleets(self): return {"Fleets": []}
            def describe_instances(self, **kwargs): return {"Reservations": []}
            def describe_launch_template_versions(self, **kwargs): return launch_template_response(target, "ap-northeast-1")

        class Seoul:
            attempts = 0; created = []
            def describe_fleets(self): return {"Fleets": []}
            def describe_launch_template_versions(self, **kwargs): return launch_template_response(target, "ap-northeast-2")
            def create_fleet(self, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("transient destination error")
                self.created.append(kwargs)

        class Pricing:
            def get_products(self, **kwargs): return {"PriceList": ['{"terms":{"OnDemand":{"term":{"priceDimensions":{"hour":{"unit":"Hrs","pricePerUnit":{"USD":"2.699"}}}}}}}']}

        class Failovers:
            consumed = False; cleared = False; claimed_at = None
            def get_current_plan(self, target_id): return None if self.cleared else plan
            def is_rejected(self, value): return False
            def get_approval(self, value):
                return FailoverApproval(
                    value.plan_id, value.target_id, value.configuration_version,
                    value.source_region, value.destination_region,
                    value.desired_instance_count, now, self.consumed,
                    value.source_fleet_id, value.source_instance_ids, self.claimed_at,
                )
            def claim_execution(self, value, claimed_at):
                self.consumed = True; self.claimed_at = claimed_at; return True
            def clear_current_plan(self, value): self.cleared = True
            def get_plan(self, target_id, plan_id): return plan

        store = InMemoryStateStore()
        store.put_if_version(VersionedState(target.target_id, 1, target.active_region, ("apne1-az1",)), None)
        tokyo, seoul, failovers = Tokyo(), Seoul(), Failovers()
        first = reconcile_handler(
            {"target": data}, object(),
            clients={"ap-northeast-1": tokyo, "ap-northeast-2": seoul},
            store=store, pricing=Pricing(), failover_store=failovers,
        )
        self.assertEqual("dependency_error", first["status"])
        self.assertTrue(failovers.consumed)
        self.assertEqual("ap-northeast-2", store.get(target.target_id).active_region)
        self.assertEqual(plan.plan_id, store.get(target.target_id).pending_failover_completion_plan_id)

        second = reconcile_handler(
            {"target": data}, object(),
            clients={"ap-northeast-1": tokyo, "ap-northeast-2": seoul},
            store=store, pricing=Pricing(), failover_store=failovers,
        )
        self.assertEqual("failover_complete", second["status"])
        self.assertEqual(2, seoul.attempts)
        self.assertEqual(1, len(seoul.created))
        self.assertTrue(failovers.cleared)
        self.assertIsNone(store.get(target.target_id).pending_failover_completion_plan_id)

    def test_claimed_approval_can_finish_after_original_expiry(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data)
        approved_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        plan = build_failover_plan(
            target, target_configuration_version(target), (), approved_at,
            source_fleet_id=None,
        )
        claimed = FailoverApproval(
            plan.plan_id, plan.target_id, plan.configuration_version,
            plan.source_region, plan.destination_region, plan.desired_instance_count,
            approved_at, used=True, source_fleet_id=plan.source_fleet_id,
            source_instance_ids=plan.source_instance_ids,
            execution_claimed_at=approved_at + timedelta(minutes=1),
        )
        class Destination:
            created = []
            def create_fleet(self, **kwargs): self.created.append(kwargs)
        destination = Destination()
        outcome = execute_approved_failover(
            object(), destination, target, plan, claimed, None, [], datetime.now(timezone.utc),
        )
        self.assertEqual("failover_complete", outcome.kind)
        self.assertEqual(1, len(destination.created))

    def test_expired_unclaimed_approval_cannot_start_source_cleanup(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data)
        approved_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        plan = build_failover_plan(
            target, target_configuration_version(target), ("i-owned",), approved_at,
            source_fleet_id="fleet-source",
        )
        approval = FailoverApproval(
            plan.plan_id, plan.target_id, plan.configuration_version,
            plan.source_region, plan.destination_region, plan.desired_instance_count,
            approved_at, source_fleet_id=plan.source_fleet_id,
            source_instance_ids=plan.source_instance_ids,
        )
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        class Source:
            def __getattr__(self, name): raise AssertionError(f"expired approval called {name}")
        outcome = execute_approved_failover(
            Source(), object(), target, plan, approval,
            {"FleetId": "fleet-source", "Tags": tags},
            [{"InstanceId": "i-owned", "Tags": tags}], datetime.now(timezone.utc),
        )
        self.assertEqual("awaiting_approval", outcome.kind)

    def test_spot_event_ignores_unowned_instance(self):
        class Targets:
            def get_mapping(self, target_id): return target_mapping()
        class Client:
            def describe_instances(self, **kwargs): return {"Reservations": [{"Instances": [{"InstanceId": "i-foreign", "Tags": []}]}]}
        result = spot_event({"target_id": "training", "detail-type": "EC2 Spot Instance Interruption Warning", "detail": {"instance-id": "i-foreign"}}, object(), clients={"us-east-1": Client()}, target_store=Targets())
        self.assertEqual("ignored_unowned_instance", result["status"])

    def test_notifications_are_deduplicated_before_sns_publish(self):
        class Conditional(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        class Table:
            claimed = False
            def put_item(self, **kwargs):
                if self.claimed: raise Conditional()
                self.claimed = True
        class Sns:
            messages = []
            def publish(self, **kwargs): self.messages.append(kwargs)
        table, sns = Table(), Sns()
        deduplicator = NotificationDeduplicator(table)
        self.assertTrue(publish_once(sns, "arn:topic", deduplicator, "training", "shortfall", {"count": 1}, datetime.now(timezone.utc)))
        self.assertFalse(publish_once(sns, "arn:topic", deduplicator, "training", "shortfall", {"count": 1}, datetime.now(timezone.utc)))
        self.assertEqual(1, len(sns.messages))

    def test_failed_sns_publish_releases_deduplication_claim_for_retry(self):
        class Table:
            deleted = []
            def put_item(self, **kwargs): pass
            def delete_item(self, **kwargs): self.deleted.append(kwargs)
        class Sns:
            def publish(self, **kwargs): raise RuntimeError("sns unavailable")
        table = Table()
        with self.assertRaises(RuntimeError):
            publish_once(Sns(), "arn:topic", NotificationDeduplicator(table), "training", "shortfall", {"count": 1}, datetime.now(timezone.utc))
        self.assertEqual(1, len(table.deleted))
        self.assertIn("NOTICE#training#shortfall#", table.deleted[0]["Key"]["pk"])

    def test_failover_notification_includes_eks_drain_reminder(self):
        class Table:
            def put_item(self, **kwargs): pass
        class Sns:
            message = None
            def publish(self, **kwargs): self.message = kwargs["Message"]
        sns = Sns()
        publish_controller_event(sns, "arn:topic", NotificationDeduplicator(Table()), "training", "failover_approval_required", {"plan_id": "p"}, datetime.now(timezone.utc), existing_eks=True)
        self.assertIn("Drain source EKS workloads", sns.message)

    def test_failover_plan_notification_is_emitted_only_after_new_plan_is_persisted(self):
        class Conditional(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        class Table:
            duplicate = False
            def put_item(self, **kwargs):
                if self.duplicate:
                    raise Conditional()
                self.duplicate = True
        events = []
        now = datetime.now(timezone.utc)
        plan = build_failover_plan(target_from_mapping(target_mapping()), 1, (), now)
        store = DynamoFailoverApprovalStore(Table(), lambda event, value, emitted_at: events.append((event, value.plan_id, emitted_at)))
        self.assertTrue(store.put_plan(plan, now))
        self.assertFalse(store.put_plan(plan, now))
        self.assertEqual([("failover_approval_required", plan.plan_id, now)], events)

    def test_failover_rejection_expiry_and_completion_emit_notifications(self):
        class Table:
            def put_item(self, **kwargs): pass
        events = []
        now = datetime.now(timezone.utc)
        target = target_from_mapping({**target_mapping(), "enabled": True})
        plan = build_failover_plan(target, target_configuration_version(target), (), now)
        store = DynamoFailoverApprovalStore(Table(), lambda event, value, emitted_at: events.append(event))
        store.reject(plan, now, "arn:aws:iam::123:user/operator")

        expired = build_failover_plan(target, target_configuration_version(target), (), now - timedelta(minutes=31))
        with self.assertRaises(ValueError):
            store.approve(expired, now, "arn:aws:iam::123:user/operator")
        store.notify_expired(expired, now)

        approval = FailoverApproval(plan.plan_id, plan.target_id, plan.configuration_version, plan.source_region, plan.destination_region, 1, now)
        class Destination:
            def create_fleet(self, **kwargs): pass
        outcome = execute_approved_failover(object(), Destination(), target, plan, approval, None, [], now, event_notifier=lambda event, value, emitted_at: events.append(event))
        self.assertEqual("failover_complete", outcome.kind)
        self.assertEqual(["failover_rejected", "failover_approval_expired", "failover_approval_expired", "failover_completed"], events)

    def test_failover_event_notifier_uses_sns_deduplication(self):
        class Table:
            def put_item(self, **kwargs): pass
        class Sns:
            messages = []
            def publish(self, **kwargs): self.messages.append(kwargs)
        now = datetime.now(timezone.utc)
        plan = build_failover_plan(target_from_mapping(target_mapping()), 1, (), now)
        sns = Sns()
        failover_event_notifier(sns, "arn:topic", NotificationDeduplicator(Table()))("failover_completed", plan, now)
        self.assertEqual(1, len(sns.messages))
        self.assertIn(plan.plan_id, sns.messages[0]["Message"])

    def test_linux_ondemand_price_is_read_as_spot_ceiling(self):
        class Pricing:
            def get_products(self, **kwargs):
                self.kwargs = kwargs
                return {"PriceList": ['{"terms":{"OnDemand":{"term":{"priceDimensions":{"hour":{"unit":"Hrs","pricePerUnit":{"USD":"55.0400000000"}}}}}}}']}
        pricing = Pricing()
        self.assertEqual("55.0400000000", str(linux_ondemand_hourly_price(pricing, "us-east-1", "p5.48xlarge")))
        self.assertEqual("us-east-1", pricing.kwargs["Filters"][1]["Value"])

    def test_fleet_request_is_spot_maintain_with_machine_weight_one(self):
        request = fleet_request(target_from_mapping(target_mapping()), ("use1-az1",))
        self.assertEqual("maintain", request["Type"])
        self.assertEqual("spot", request["TargetCapacitySpecification"]["DefaultTargetCapacityType"])
        self.assertEqual(1.0, request["LaunchTemplateConfigs"][0]["Overrides"][0]["WeightedCapacity"])
        self.assertEqual("10", request["LaunchTemplateConfigs"][0]["Overrides"][0]["MaxPrice"])
        self.assertEqual("subnet-a", request["LaunchTemplateConfigs"][0]["Overrides"][0]["SubnetId"])

    def test_fleet_client_token_is_stable_for_same_request_and_changes_with_contract(self):
        target = target_from_mapping(target_mapping())
        first = fleet_request(target, ("use1-az1",))
        repeated = fleet_request(target, ("use1-az1",))
        changed = fleet_request(target, ("use1-az1",), {"p5.48xlarge": 55.04})
        replacement = fleet_request(target, ("use1-az1",), request_epoch="replacement-epoch")
        self.assertEqual(first["ClientToken"], repeated["ClientToken"])
        self.assertNotEqual(first["ClientToken"], changed["ClientToken"])
        self.assertNotEqual(first["ClientToken"], replacement["ClientToken"])

    def test_deleted_recorded_fleet_rotates_token_only_after_instances_are_gone(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data)
        store = InMemoryStateStore()
        store.put_if_version(
            VersionedState(target.target_id, 1, target.active_region, ("use1-az1",),
                           fleet_request_epoch="old-epoch", owned_fleet_id="fleet-deleted"),
            None,
        )
        class Client:
            created = []
            def describe_fleets(self): return {"Fleets": []}
            def describe_instances(self, **kwargs): return {"Reservations": []}
            def create_fleet(self, **kwargs): self.created.append(kwargs)
        client = Client()
        now = datetime.now(timezone.utc)
        first = reconcile_target(client, target, store, now)
        self.assertEqual("shortfall", first.kind)
        self.assertEqual(1, len(client.created))
        self.assertNotEqual(
            fleet_request(target, ("use1-az1",), request_epoch="old-epoch")["ClientToken"],
            client.created[0]["ClientToken"],
        )
        second = reconcile_target(client, target, store, now + timedelta(seconds=1))
        self.assertEqual("shortfall", second.kind)
        self.assertEqual(2, len(client.created))
        self.assertEqual(client.created[0]["ClientToken"], client.created[1]["ClientToken"])

    def test_fleet_requests_validate_against_botocore_ec2_models(self):
        import boto3
        from botocore.stub import Stubber
        target = target_from_mapping(target_mapping())
        client = boto3.client("ec2", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        create = fleet_request(target, ("use1-az1",))
        with Stubber(client) as stubber:
            stubber.add_response("create_fleet", {"FleetId": "fleet-1", "Errors": [], "Instances": []}, create)
            client.create_fleet(**create)
        modify = {
            "FleetId": "fleet-1234567890abcdef0",
            "LaunchTemplateConfigs": create["LaunchTemplateConfigs"],
            "TargetCapacitySpecification": create["TargetCapacitySpecification"],
            "ExcessCapacityTerminationPolicy": "no-termination",
        }
        with Stubber(client) as stubber:
            stubber.add_response("modify_fleet", {"Return": True}, modify)
            client.modify_fleet(**modify)

    def test_ondemand_cap_overrides_static_example_cap_in_fleet_request(self):
        from decimal import Decimal
        request = fleet_request(target_from_mapping(target_mapping()), ("use1-az1",), {"p5.48xlarge": Decimal("55.04")})
        self.assertEqual("55.04", request["LaunchTemplateConfigs"][0]["Overrides"][0]["MaxPrice"])

    def test_automatic_price_source_requires_resolved_ondemand_ceiling_before_fleet_write(self):
        data = target_mapping(); data["instance_types"][0].pop("spot_price_cap_usd")
        target = target_from_mapping(data)
        self.assertEqual("linux-ondemand", target.price_cap_source)
        with self.assertRaises(ConfigurationError): fleet_request(target, ("use1-az1",))
        from decimal import Decimal
        request = fleet_request(target, ("use1-az1",), {"p5.48xlarge": Decimal("55.04")})
        self.assertEqual("55.04", request["LaunchTemplateConfigs"][0]["Overrides"][0]["MaxPrice"])

    def test_owned_fleet_discovery_rejects_foreign_fleets(self):
        target = target_from_mapping(target_mapping())
        class Client:
            def describe_fleets(self):
                return {"Fleets": [
                    {"FleetId": "foreign", "Tags": [{"Key": "managed-by", "Value": "another-controller"}]},
                    {"FleetId": "owned", "Tags": [{"Key": key, "Value": value} for key, value in target.tags.items()]},
                ]}
        self.assertEqual("owned", find_owned_fleet(Client(), target)["FleetId"])

    def test_fleet_fulfillment_counts_only_active_owned_instances_by_zone_id(self):
        target = target_from_mapping(target_mapping())
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        class Client:
            def describe_fleet_instances(self, **kwargs):
                return {"ActiveInstances": [{"InstanceId": "i-owned"}, {"InstanceId": "i-foreign"}]}
            def describe_instances(self, **kwargs):
                return {"Reservations": [{"Instances": [
                    {"InstanceId": "i-owned", "InstanceType": "p5.48xlarge", "State": {"Name": "running"}, "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": tags},
                    {"InstanceId": "i-foreign", "State": {"Name": "running"}, "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": []},
                ]}]}
            def describe_availability_zones(self, **kwargs):
                return {"AvailabilityZones": [{"ZoneName": "us-east-1a", "ZoneId": "use1-az1"}]}
        self.assertEqual({"use1-az1": 1}, fulfilled_by_zone(Client(), {"FleetId": "fleet-1"}, target))
        self.assertEqual(8, observe_fleet_capacity(Client(), {"FleetId": "fleet-1"}, target).realized_h100_gpu_count)

    def test_validation_fleet_observation_reports_l40s_not_h100(self):
        target = target_from_mapping(validation_target_mapping(enabled=True))
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        class Client:
            def describe_fleet_instances(self, **kwargs): return {"ActiveInstances": [{"InstanceId": "i-g6e"}]}
            def describe_instances(self, **kwargs):
                return {"Reservations": [{"Instances": [{
                    "InstanceId": "i-g6e", "InstanceType": "g6e.xlarge",
                    "State": {"Name": "running"}, "Placement": {"AvailabilityZoneId": "apne1-az1"},
                    "Tags": tags,
                }]}]}
        observation = observe_fleet_capacity(Client(), {"FleetId": "fleet-validation"}, target)
        self.assertEqual(1, observation.realized_accelerator_count)
        self.assertEqual({"L40S": 1}, observation.accelerator_counts_by_model)
        self.assertEqual(0, observation.realized_h100_gpu_count)

    def test_existing_fleet_only_updates_changed_zone_overrides(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data)
        fleet = {"FleetId": "fleet-1", "Type": "maintain", "Tags": [{"Key": key, "Value": value} for key, value in target.tags.items()], "LaunchTemplateConfigs": []}
        class Client:
            calls = []
            def modify_fleet(self, **kwargs): self.calls.append(kwargs)
        client = Client()
        outcome = reconcile_existing_fleet(client, target, fleet, ("use1-az1",), 0)
        self.assertEqual("shortfall", outcome.kind)
        self.assertEqual(target.desired_instance_count, client.calls[0]["TargetCapacitySpecification"]["TotalTargetCapacity"])

    def test_unchanged_owned_fleet_is_not_modified(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data); request = fleet_request(target, ("use1-az1",))
        fleet = {"FleetId": "fleet-1", "Type": "maintain", "Tags": [{"Key": key, "Value": value} for key, value in target.tags.items()], "LaunchTemplateConfigs": request["LaunchTemplateConfigs"]}
        class Client:
            calls = []
            def modify_fleet(self, **kwargs): self.calls.append(kwargs)
        client = Client()
        reconcile_existing_fleet(client, target, fleet, ("use1-az1",), 0)
        self.assertEqual([], client.calls)

    def test_describe_fleets_response_shape_is_not_modified(self):
        data = target_mapping(); data["enabled"] = True
        target = target_from_mapping(data); request = fleet_request(target, ("use1-az1",))
        desired_override = request["LaunchTemplateConfigs"][0]["Overrides"][0]
        described_configs = [{
            "LaunchTemplateSpecification": {"LaunchTemplateId": "lt-east", "Version": "1"},
            "Overrides": [{
                "InstanceType": desired_override["InstanceType"],
                "MaxPrice": desired_override["MaxPrice"],
                "SubnetId": desired_override["SubnetId"],
                "AvailabilityZone": "us-east-1a",
                "WeightedCapacity": 1.0,
                "Priority": 0.0,
            }],
        }]
        fleet = {
            "FleetId": "fleet-1", "Type": "maintain",
            "Tags": [{"Key": key, "Value": value} for key, value in target.tags.items()],
            "LaunchTemplateConfigs": described_configs,
            "TargetCapacitySpecification": {"TotalTargetCapacity": 1, "SpotTargetCapacity": 1},
        }
        class Client:
            calls = []
            def modify_fleet(self, **kwargs): self.calls.append(kwargs)
        client = Client()
        outcome = reconcile_existing_fleet(client, target, fleet, ("use1-az1",), 1)
        self.assertEqual("healthy", outcome.kind)
        self.assertEqual([], client.calls)

    def test_target_growth_modifies_owned_fleet_and_shrink_requires_explicit_policy(self):
        grow_data = target_mapping(); grow_data["enabled"] = True; grow_data["desired_instance_count"] = grow_data["maximum_instance_count"] = 2
        grow = target_from_mapping(grow_data)
        tags = [{"Key": key, "Value": value} for key, value in grow.tags.items()]
        grow_request = fleet_request(grow, ("use1-az1",))
        fleet = {"FleetId": "fleet-1", "Type": "maintain", "Tags": tags, "LaunchTemplateConfigs": grow_request["LaunchTemplateConfigs"], "TargetCapacitySpecification": {"TotalTargetCapacity": 1, "DefaultTargetCapacityType": "spot"}}
        class Client:
            calls = []
            def modify_fleet(self, **kwargs): self.calls.append(kwargs)
        client = Client()
        reconcile_existing_fleet(client, grow, fleet, ("use1-az1",), 1)
        self.assertEqual(2, client.calls[-1]["TargetCapacitySpecification"]["TotalTargetCapacity"])
        self.assertEqual("no-termination", client.calls[-1]["ExcessCapacityTerminationPolicy"])

        shrink_data = target_mapping(); shrink_data["enabled"] = True
        shrink = target_from_mapping(shrink_data)
        shrink_request = fleet_request(shrink, ("use1-az1",))
        fleet["LaunchTemplateConfigs"] = shrink_request["LaunchTemplateConfigs"]
        fleet["TargetCapacitySpecification"]["TotalTargetCapacity"] = 2
        before = len(client.calls)
        outcome = reconcile_existing_fleet(client, shrink, fleet, ("use1-az1",), 2)
        self.assertEqual("configuration_error", outcome.kind)
        self.assertEqual(before, len(client.calls))

        shrink_data["excess_instance_termination"] = True
        approved = target_from_mapping(shrink_data)
        reconcile_existing_fleet(client, approved, fleet, ("use1-az1",), 2)
        self.assertEqual("termination", client.calls[-1]["ExcessCapacityTerminationPolicy"])

    def test_local_zone_uses_only_parent_region_eks_launch_template(self):
        target = target_from_mapping(target_mapping("existing-eks"))
        request = fleet_request(target, ("use1-nyc-1a",))
        self.assertEqual("lt-east", request["LaunchTemplateConfigs"][0]["LaunchTemplateSpecification"]["LaunchTemplateId"])
        self.assertEqual("subnet-lz", request["LaunchTemplateConfigs"][0]["Overrides"][0]["SubnetId"])

    def test_eks_readiness_cannot_increase_fleet_target(self):
        data = target_mapping("existing-eks"); data["enabled"] = True; data["desired_instance_count"] = data["maximum_instance_count"] = 2
        target = target_from_mapping(data)
        self.assertEqual(2, fleet_request(target, ("use1-az1",))["TargetCapacitySpecification"]["TotalTargetCapacity"])

    def test_cleanup_requires_separate_authorization_and_owned_tag(self):
        target = target_from_mapping(target_mapping())
        fleet = {"FleetId": "fleet-1", "Tags": [{"Key": key, "Value": value} for key, value in target.tags.items()]}
        class Client:
            calls = []
            def delete_fleets(self, **kwargs): self.calls.append(kwargs)
        client = Client()
        self.assertFalse(cleanup_owned_fleet(client, target, fleet, explicitly_authorized=False))
        self.assertEqual([], client.calls)
        self.assertTrue(cleanup_owned_fleet(client, target, fleet, explicitly_authorized=True))

    def test_cleanup_can_cancel_maintain_request_without_terminating_and_termination_rechecks_tags(self):
        target = target_from_mapping(target_mapping())
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        fleet = {"FleetId": "fleet-1", "Tags": tags}
        class Client:
            deleted = []; terminated = []
            def delete_fleets(self, **kwargs): self.deleted.append(kwargs)
            def terminate_instances(self, **kwargs): self.terminated.append(kwargs)
        client = Client()
        cleanup_owned_fleet(client, target, fleet, explicitly_authorized=True, terminate_instances=False)
        self.assertFalse(client.deleted[-1]["TerminateInstances"])
        terminated = terminate_owned_instances(client, target, [{"InstanceId": "i-owned", "Tags": tags}, {"InstanceId": "i-foreign", "Tags": []}], explicitly_authorized=True)
        self.assertEqual(("i-owned",), terminated)
        self.assertEqual(["i-owned"], client.terminated[-1]["InstanceIds"])

    def test_owned_capacity_inventory_reads_every_region_and_exposes_duplicate_fleets(self):
        target = target_from_mapping(validation_target_mapping())
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]

        class Client:
            def __init__(self, region, fleet_count, instance_count):
                self.region = region
                self.fleet_count = fleet_count
                self.instance_count = instance_count

            def describe_fleets(self):
                return {"Fleets": [{
                    "FleetId": f"fleet-{self.region}-{index}", "FleetState": "active", "Type": "maintain",
                    "TargetCapacitySpecification": {"TotalTargetCapacity": 1}, "Tags": tags,
                } for index in range(self.fleet_count)]}

            def describe_instances(self, **kwargs):
                return {"Reservations": [{"Instances": [{
                    "InstanceId": f"i-{self.region}-{index}", "State": {"Name": "running"},
                    "InstanceType": "g6e.xlarge", "Placement": {"AvailabilityZoneId": f"{self.region}-az"},
                    "Tags": tags,
                } for index in range(self.instance_count)]}]}

        inventory = owned_capacity_inventory({
            "ap-northeast-1": Client("tokyo", 2, 1),
            "ap-northeast-2": Client("seoul", 0, 0),
        }, target)
        self.assertEqual(2, len(inventory[0]["owned_fleets"]))
        self.assertEqual("active", inventory[0]["owned_fleets"][0]["state"])
        self.assertEqual("i-tokyo-0", inventory[0]["owned_instances"][0]["instance_id"])
        self.assertEqual([], inventory[1]["owned_fleets"])
        self.assertEqual([], inventory[1]["owned_instances"])

    def test_owned_capacity_inventory_excludes_all_deleted_fleet_states(self):
        target = target_from_mapping(validation_target_mapping())
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]

        class Client:
            def __init__(self, include_active): self.include_active = include_active
            def describe_fleets(self):
                states = ["deleted", "deleted_running", "deleted_terminating"]
                if self.include_active: states.append("active")
                return {"Fleets": [{"FleetId": f"fleet-{state}", "FleetState": state, "Tags": tags} for state in states]}
            def describe_instances(self, **kwargs): return {"Reservations": []}

        inventory = owned_capacity_inventory({
            "ap-northeast-1": Client(True),
            "ap-northeast-2": Client(False),
        }, target)
        self.assertEqual(["fleet-active"], [item["fleet_id"] for item in inventory[0]["owned_fleets"]])
        self.assertEqual([], inventory[1]["owned_fleets"])

    def test_local_zone_requires_opt_in_and_matching_approved_subnet(self):
        target = target_from_mapping(target_mapping())
        class Client:
            def describe_availability_zones(self, **kwargs):
                return {"AvailabilityZones": [{"ZoneName": "us-east-1-nyc-1a", "ZoneId": "use1-nyc-1a", "ZoneType": "local-zone", "OptInStatus": "opted-in"}]}
            def describe_instance_type_offerings(self, **kwargs):
                return {"InstanceTypeOfferings": [{"Location": "use1-nyc-1a", "InstanceType": "p5.48xlarge"}]}
            def describe_subnets(self, **kwargs):
                return {"Subnets": [{"SubnetId": "subnet-lz", "AvailabilityZoneId": "use1-nyc-1a"}]}
        region = target.region_inputs("us-east-1")
        result = discover_region(Client(), "us-east-1", ["p5.48xlarge"], region.local_zone_placements)
        self.assertTrue(result.zones[0].eligible)
        self.assertEqual("subnet-lz", result.zones[0].approved_subnet_id)

    def test_local_zone_without_matching_h100_offering_is_ineligible(self):
        target = target_from_mapping(target_mapping())
        class Client:
            def describe_availability_zones(self, **kwargs):
                return {"AvailabilityZones": [{"ZoneName": "us-east-1-nyc-1a", "ZoneId": "use1-nyc-1a", "ZoneType": "local-zone", "OptInStatus": "opted-in"}]}
            def describe_instance_type_offerings(self, **kwargs): return {"InstanceTypeOfferings": []}
            def describe_subnets(self, **kwargs): return {"Subnets": [{"SubnetId": "subnet-lz", "AvailabilityZoneId": "use1-nyc-1a"}]}
        region = target.region_inputs("us-east-1")
        result = discover_region(Client(), "us-east-1", ["p5.48xlarge"], region.local_zone_placements)
        self.assertFalse(result.zones[0].eligible)
        self.assertEqual("h100-offering-unavailable", result.zones[0].reason)

    def test_launch_template_contract_enforces_ami_profile_network_encryption_imdsv2_and_tags(self):
        target = target_from_mapping(target_mapping())
        class Client:
            response = launch_template_response(target, "us-east-1")
            def describe_launch_template_versions(self, **kwargs): return self.response
        client = Client()
        self.assertTrue(inspect_launch_contract(client, target, target.region_inputs("us-east-1")).valid)
        broken = launch_template_response(target, "us-east-1")
        broken["LaunchTemplateVersions"][0]["LaunchTemplateData"]["MetadataOptions"]["HttpTokens"] = "optional"
        client.response = broken
        inspection = inspect_launch_contract(client, target, target.region_inputs("us-east-1"))
        self.assertFalse(inspection.valid)
        self.assertIn("imdsv2-not-required", inspection.violations)
        broken = launch_template_response(target, "us-east-1")
        broken["LaunchTemplateVersions"][0]["LaunchTemplateData"]["BlockDeviceMappings"][0]["Ebs"]["DeleteOnTermination"] = False
        client.response = broken
        inspection = inspect_launch_contract(client, target, target.region_inputs("us-east-1"))
        self.assertIn("root-volume-delete-on-termination-not-explicit", inspection.violations)
