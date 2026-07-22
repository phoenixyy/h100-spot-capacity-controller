from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import unittest

from h100_spot_controller.config import ConfigurationError, target_from_mapping
from h100_spot_controller.region_selection import (
    NO_ELIGIBLE_REGION,
    RegionalReadiness,
    build_signal_snapshot,
    collect_regional_readiness,
    decision_from_selection,
    resolve_initial_region,
    select_region,
)
from h100_spot_controller.signals import SpsObservation, build_sps_request, collect_sps_regions
from h100_spot_controller.handlers import collect as collect_handler, reconcile as reconcile_handler
from h100_spot_controller.failover import target_configuration_version
from h100_spot_controller.metrics import region_selection_metric_data
from h100_spot_controller.state import (
    DynamoRegionDecisionStore,
    InMemoryRegionDecisionStore,
    InMemoryRegionSignalStore,
    InMemoryStateStore,
    StateConflict,
    VersionedState,
)


def target_mapping(mode="manual", active_region="us-east-1"):
    digest = sha256(b"bootstrap").hexdigest()
    value = {
        "target_id": "selector-test",
        "enabled": True,
        "desired_instance_count": 1,
        "maximum_instance_count": 1,
        "region_selection": {
            "mode": mode,
            "signal_max_age_minutes": 20,
            "decision_ttl_minutes": 15,
        },
        "instance_types": [{"name": "p5.4xlarge"}],
        "candidate_regions": [],
    }
    if active_region is not None:
        value["active_region"] = active_region
    for region, suffix, zone in (
        ("us-east-1", "east", "use1-az1"),
        ("us-west-2", "west", "usw2-az1"),
    ):
        value["candidate_regions"].append({
            "region": region,
            "launch_template_id": f"lt-{suffix}",
            "launch_template_version": "1",
            "ami_id": f"ami-{suffix}",
            "iam_instance_profile_arn": f"arn:aws:iam::123:instance-profile/{suffix}",
            "security_group_ids": [f"sg-{suffix}"],
            "bootstrap_contract_version": "standalone-v1",
            "user_data_sha256": digest,
            "root_volume_encrypted": True,
            "standard_placements": [{"subnet_id": f"subnet-{suffix}", "zone_id": zone}],
            "local_zone_placements": [],
        })
    return value


def evidence(target, now, scores=(5, 8), *, fingerprint=None, configuration_version=7):
    regions = tuple(item.region for item in target.candidate_regions)
    request_fingerprint = fingerprint or build_sps_request(target, regions).fingerprint
    sps = {
        region: SpsObservation(
            "ok", region, ({"Region": region, "Score": score},), observed_at=now,
            request_fingerprint=request_fingerprint,
        )
        for region, score in zip(regions, scores)
    }
    readiness = {
        region: RegionalReadiness(
            region, True, ("p5.4xlarge",), True, True,
            price_ratio=Decimal("0.50"), best_standard_az_score=score,
            best_standard_az_count=1, observed_at=now,
        )
        for region, score in zip(regions, scores)
    }
    return build_signal_snapshot(target, configuration_version, sps, readiness, now)


class RegionSelectionTests(unittest.TestCase):
    def test_existing_configuration_defaults_to_manual(self):
        target = target_from_mapping(target_mapping(mode="manual"))
        self.assertEqual("manual", target.region_selection.mode)
        self.assertEqual(20, target.region_selection.signal_max_age_minutes)
        self.assertEqual("us-east-1", target.active_region)

        implicit = target_mapping()
        implicit.pop("region_selection")
        self.assertEqual("manual", target_from_mapping(implicit).region_selection.mode)

    def test_dynamic_modes_allow_omitted_active_region_but_manual_does_not(self):
        self.assertIsNone(target_from_mapping(target_mapping("recommend", None)).active_region)
        self.assertIsNone(target_from_mapping(target_mapping("auto_initial", None)).active_region)
        with self.assertRaises(ConfigurationError):
            target_from_mapping(target_mapping("manual", None))

    def test_region_selection_policy_is_strict(self):
        invalid = target_mapping("auto_initial", None)
        invalid["region_selection"]["signal_max_age_minutes"] = "20"
        with self.assertRaises(ConfigurationError):
            target_from_mapping(invalid)
        invalid = target_mapping("auto_initial", None)
        invalid["region_selection"]["mode"] = "auto_migrate"
        with self.assertRaises(ConfigurationError):
            target_from_mapping(invalid)

    def test_sps_request_fingerprint_is_stable_and_capacity_sensitive(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        regions = ("us-east-1", "us-west-2")
        first = build_sps_request(target, regions)
        second = build_sps_request(target, regions)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertFalse(first.as_api_kwargs()["SingleAvailabilityZone"])
        changed = target_mapping("auto_initial", None)
        changed["desired_instance_count"] = changed["maximum_instance_count"] = 2
        self.assertNotEqual(first.fingerprint, build_sps_request(target_from_mapping(changed), regions).fingerprint)

    def test_partial_and_rate_limited_sps_results_are_normalized(self):
        target = target_from_mapping(target_mapping("recommend", None))
        class Partial:
            def get_spot_placement_scores(self, **kwargs):
                self.kwargs = kwargs
                return {"SpotPlacementScores": [{"Region": "us-east-1", "Score": 3}]}
        client = Partial()
        observations = collect_sps_regions(client, target, ("us-east-1", "us-west-2"))
        self.assertEqual(3, observations["us-east-1"].scores[0]["Score"])
        self.assertEqual((), observations["us-west-2"].scores)
        self.assertEqual(observations["us-east-1"].request_fingerprint, observations["us-west-2"].request_fingerprint)

        class LimitError(Exception):
            response = {"Error": {"Code": "MaxConfigLimitExceeded"}}
        class Limited:
            def get_spot_placement_scores(self, **kwargs): raise LimitError()
        limited = collect_sps_regions(Limited(), target, ("us-east-1", "us-west-2"))
        self.assertTrue(all(item.status == "rate_limited" for item in limited.values()))

        class Malformed:
            def get_spot_placement_scores(self, **kwargs):
                return {"SpotPlacementScores": [None, "bad", {"Score": 9}, {"Region": "outside", "Score": 9}]}
        malformed = collect_sps_regions(Malformed(), target, ("us-east-1", "us-west-2"))
        self.assertTrue(all(item.scores == () for item in malformed.values()))

    def test_selector_uses_sps_before_tie_breakers_and_accepts_low_scores(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        now = datetime.now(timezone.utc)
        selection = select_region(target, evidence(target, now, (1, 2)), now)
        self.assertEqual("us-west-2", selection.selected_region)
        self.assertEqual(("us-west-2", "us-east-1"), tuple(item.region for item in selection.ordered_candidates))

    def test_selector_uses_configured_order_for_complete_tie(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        now = datetime.now(timezone.utc)
        selection = select_region(target, evidence(target, now, (5, 5)), now)
        self.assertEqual("us-east-1", selection.selected_region)

    def test_ranking_is_stable_across_score_combinations(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        now = datetime.now(timezone.utc)
        for east in range(1, 10):
            for west in range(1, 10):
                with self.subTest(east=east, west=west):
                    selected = select_region(target, evidence(target, now, (east, west)), now).selected_region
                    self.assertEqual("us-east-1" if east >= west else "us-west-2", selected)

    def test_hard_filter_excludes_high_score_region_and_reports_reasons(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        now = datetime.now(timezone.utc)
        snapshot = evidence(target, now, (9, 4))
        snapshot.readiness_by_region["us-east-1"] = RegionalReadiness(
            "us-east-1", False, (), False, False, error_codes=("READ_DISCOVERY_FAILED",), observed_at=now,
        )
        result = select_region(target, snapshot, now)
        self.assertEqual("us-west-2", result.selected_region)
        reasons = result.excluded_candidates[0].exclusion_reasons
        self.assertIn("LAUNCH_CONTRACT_INVALID", reasons)
        self.assertIn("QUOTA_INSUFFICIENT", reasons)

    def test_stale_or_fingerprint_mismatched_signals_cannot_select(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        now = datetime.now(timezone.utc)
        stale = evidence(target, now - timedelta(minutes=21), (8, 7))
        result = select_region(target, stale, now)
        self.assertIsNone(result.selected_region)
        self.assertEqual(NO_ELIGIBLE_REGION, result.reason)
        self.assertTrue(all("SPS_STALE" in item.exclusion_reasons for item in result.excluded_candidates))

        mismatch = evidence(target, now, fingerprint="0" * 64)
        result = select_region(target, mismatch, now)
        self.assertIsNone(result.selected_region)
        self.assertTrue(all("SPS_FINGERPRINT_MISMATCH" in item.exclusion_reasons for item in result.excluded_candidates))

    def test_snapshot_and_decision_stores_are_idempotent_and_conflict_safe(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        now = datetime.now(timezone.utc)
        snapshot = evidence(target, now)
        snapshots = InMemoryRegionSignalStore()
        self.assertTrue(snapshots.put_if_absent(snapshot))
        self.assertFalse(snapshots.put_if_absent(snapshot))
        restored = type(snapshot).from_item(snapshot.as_item())
        self.assertEqual(snapshot.snapshot_id, restored.snapshot_id)

        selection = select_region(target, snapshot, now)
        decision = decision_from_selection(target, 7, 1, snapshot, selection, now)
        decisions = InMemoryRegionDecisionStore()
        self.assertTrue(decisions.publish(decision, None))
        self.assertFalse(decisions.publish(decision, 1))
        conflicting = decision_from_selection(target, 7, 2, snapshot, selection, now + timedelta(seconds=1))
        with self.assertRaises(StateConflict):
            decisions.publish(conflicting, 99)
        self.assertTrue(decisions.mark_applied(decision, now + timedelta(seconds=2)))

    def test_dynamo_decision_omits_null_apply_marker_and_claims_once(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        now = datetime.now(timezone.utc)
        snapshot = evidence(target, now)
        decision = decision_from_selection(target, 7, 1, snapshot, select_region(target, snapshot, now), now)
        self.assertNotIn("applied_at", decision.as_item())

        class Conditional(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        class Table:
            def __init__(self): self.item = None
            def put_item(self, **kwargs): self.item = dict(kwargs["Item"])
            def get_item(self, **kwargs): return {"Item": dict(self.item)} if self.item else {}
            def update_item(self, **kwargs):
                if self.item is None or "applied_at" in self.item:
                    raise Conditional()
                values = kwargs["ExpressionAttributeValues"]
                if self.item["decision_version"] != values[":version"] or self.item["snapshot_id"] != values[":snapshot"]:
                    raise Conditional()
                self.item["applied_at"] = values[":applied"]
        class Dynamo:
            def __init__(self, table): self.table = table
            def Table(self, name): return self.table

        table = Table()
        store = DynamoRegionDecisionStore("state", Dynamo(table))
        self.assertTrue(store.publish(decision, None))
        applied_at = now + timedelta(seconds=1)
        self.assertTrue(store.mark_applied(decision, applied_at))
        self.assertEqual(applied_at, store.get(target.target_id).applied_at)
        self.assertTrue(store.mark_applied(decision, applied_at + timedelta(seconds=1)))

    def test_runtime_state_preserves_initial_decision_audit_origin(self):
        state = VersionedState(
            "selector-test", 0, "us-west-2", ("usw2-az1",),
            initial_region_decision_version=4, initial_region_snapshot_id="snapshot-4",
        )
        item = state.as_item()
        self.assertEqual(4, item["initial_region_decision_version"])
        self.assertEqual("snapshot-4", item["initial_region_snapshot_id"])

    def test_decision_consumption_checks_mode_enabled_fingerprint_and_expiry(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        now = datetime.now(timezone.utc)
        version = target_configuration_version(target)
        snapshot = evidence(target, now, configuration_version=version)
        decision = decision_from_selection(target, version, 1, snapshot, select_region(target, snapshot, now), now)
        self.assertTrue(decision.is_consumable(target, now))
        self.assertFalse(decision.is_consumable(target, now + timedelta(minutes=16)))
        manual = target_from_mapping(target_mapping("manual", "us-east-1"))
        self.assertFalse(decision.is_consumable(manual, now))

    def test_initial_resolution_pins_owned_or_state_region_and_blocks_duplicates(self):
        target = target_from_mapping(target_mapping("auto_initial", None))
        now = datetime.now(timezone.utc)
        decision_snapshot = evidence(target, now)
        decision = decision_from_selection(
            target, 7, 1, decision_snapshot, select_region(target, decision_snapshot, now), now,
        )
        empty = [
            {"region": "us-east-1", "owned_fleets": [], "owned_instances": []},
            {"region": "us-west-2", "owned_fleets": [], "owned_instances": []},
        ]
        chosen = resolve_initial_region(target, None, empty, decision, 7, now)
        self.assertEqual("us-west-2", chosen.region)
        self.assertTrue(chosen.apply_decision)
        self.assertEqual(
            "awaiting_region_decision",
            resolve_initial_region(target, None, empty, decision, 8, now).status,
        )

        occupied = [dict(empty[0]), dict(empty[1])]
        occupied[0]["owned_instances"] = [{"instance_id": "i-east"}]
        discovered = resolve_initial_region(target, None, occupied, decision, 7, now)
        self.assertEqual("us-east-1", discovered.region)
        self.assertFalse(discovered.apply_decision)

        state = VersionedState("selector-test", 3, "us-east-1", ("use1-az1",))
        self.assertEqual("us-east-1", resolve_initial_region(target, state, empty, decision, 7, now).region)
        occupied[1]["owned_fleets"] = [{"fleet_id": "fleet-west"}]
        self.assertEqual("ownership_mismatch", resolve_initial_region(target, None, occupied, decision, 7, now).status)

    def test_recommendation_never_applies_and_pending_or_expired_decision_waits(self):
        now = datetime.now(timezone.utc)
        recommend = target_from_mapping(target_mapping("recommend", None))
        empty = [
            {"region": "us-east-1", "owned_fleets": [], "owned_instances": []},
            {"region": "us-west-2", "owned_fleets": [], "owned_instances": []},
        ]
        self.assertEqual("recommendation_only", resolve_initial_region(recommend, None, empty, None, 7, now).status)
        auto = target_from_mapping(target_mapping("auto_initial", None))
        snapshot = evidence(auto, now - timedelta(minutes=16))
        expired = decision_from_selection(auto, 7, 1, snapshot, select_region(auto, snapshot, now - timedelta(minutes=16)), now - timedelta(minutes=16))
        self.assertEqual("awaiting_region_decision", resolve_initial_region(auto, None, empty, expired, 7, now).status)

    def test_readiness_collector_normalizes_launch_offering_quota_price_and_az_evidence(self):
        import base64
        target = target_from_mapping(target_mapping("recommend", None))
        inputs = target.region_inputs("us-east-1")
        tags = [{"Key": key, "Value": value} for key, value in {**target.tags, "bootstrap-contract-version": inputs.bootstrap_contract_version}.items()]
        class Ec2:
            def describe_launch_template_versions(self, **kwargs):
                return {"LaunchTemplateVersions": [{"LaunchTemplateData": {
                    "ImageId": inputs.ami_id,
                    "IamInstanceProfile": {"Arn": inputs.iam_instance_profile_arn},
                    "SecurityGroupIds": list(inputs.security_group_ids),
                    "MetadataOptions": {"HttpTokens": "required"},
                    "UserData": base64.b64encode(b"bootstrap").decode(),
                    "BlockDeviceMappings": [{"Ebs": {"Encrypted": True, "DeleteOnTermination": True}}],
                    "TagSpecifications": [
                        {"ResourceType": "instance", "Tags": tags},
                        {"ResourceType": "volume", "Tags": tags},
                    ],
                }}]}
            def describe_availability_zones(self, **kwargs):
                return {"AvailabilityZones": [{"ZoneName": "us-east-1a", "ZoneId": "use1-az1", "ZoneType": "availability-zone"}]}
            def describe_instance_type_offerings(self, **kwargs):
                return {"InstanceTypeOfferings": [{"Location": "use1-az1", "InstanceType": "p5.4xlarge"}]}
            def describe_instance_types(self, **kwargs):
                return {"InstanceTypes": [{"InstanceType": "p5.4xlarge", "VCpuInfo": {"DefaultVCpus": 16}}]}
            def describe_spot_price_history(self, **kwargs):
                return {"SpotPriceHistory": [{"InstanceType": "p5.4xlarge", "SpotPrice": "4"}]}
        class Pricing:
            def get_products(self, **kwargs):
                import json
                return {"PriceList": [json.dumps({"terms": {"OnDemand": {"x": {"priceDimensions": {"y": {"unit": "Hrs", "pricePerUnit": {"USD": "8"}}}}}}})]}
        class Quota:
            def get_service_quota(self, **kwargs):
                self.code = kwargs["QuotaCode"]
                return {"Quota": {"Value": 64.0}}
        quota = Quota()
        az_sps = SpsObservation(
            "ok", "us-east-1", ({"AvailabilityZoneId": "use1-az1", "Score": 7},),
            observed_at=datetime.now(timezone.utc), request_fingerprint="az",
        )
        readiness = collect_regional_readiness(
            target, "us-east-1", Ec2(), Pricing(), quota, datetime.now(timezone.utc), az_sps=az_sps,
        )
        self.assertTrue(readiness.launch_contract_ready)
        self.assertTrue(readiness.quota_sufficient)
        self.assertTrue(readiness.price_caps_resolved)
        self.assertEqual(Decimal("0.5"), readiness.price_ratio)
        self.assertEqual(7, readiness.best_standard_az_score)
        self.assertEqual("L-7212CCBC", quota.code)

        class LowQuota(Quota):
            def get_service_quota(self, **kwargs): return {"Quota": {"Value": 8.0}}
        insufficient = collect_regional_readiness(
            target, "us-east-1", Ec2(), Pricing(), LowQuota(), datetime.now(timezone.utc), az_sps=az_sps,
        )
        self.assertFalse(insufficient.quota_sufficient)

    def test_auto_initial_handler_creates_only_in_selected_region_and_retries_pinned_region(self):
        import base64
        import json
        raw = target_mapping("auto_initial", None)
        target = target_from_mapping(raw)
        now = datetime.now(timezone.utc)
        version = target_configuration_version(target)
        snapshot = evidence(target, now, configuration_version=version)
        decision = decision_from_selection(target, version, 1, snapshot, select_region(target, snapshot, now), now)
        decisions = InMemoryRegionDecisionStore()
        decisions.publish(decision, None)
        state = InMemoryStateStore()

        class Ec2:
            def __init__(self, region, fail_once=False):
                self.region = region
                self.fail_once = fail_once
                self.created = []
            def describe_fleets(self): return {"Fleets": []}
            def describe_instances(self, **kwargs): return {"Reservations": []}
            def describe_launch_template_versions(self, **kwargs):
                inputs = target.region_inputs(self.region)
                tags = [{"Key": key, "Value": value} for key, value in {**target.tags, "bootstrap-contract-version": inputs.bootstrap_contract_version}.items()]
                return {"LaunchTemplateVersions": [{"LaunchTemplateData": {
                    "ImageId": inputs.ami_id,
                    "IamInstanceProfile": {"Arn": inputs.iam_instance_profile_arn},
                    "SecurityGroupIds": list(inputs.security_group_ids),
                    "MetadataOptions": {"HttpTokens": "required"},
                    "UserData": base64.b64encode(b"bootstrap").decode(),
                    "BlockDeviceMappings": [{"Ebs": {"Encrypted": True, "DeleteOnTermination": True}}],
                    "TagSpecifications": [
                        {"ResourceType": "instance", "Tags": tags},
                        {"ResourceType": "volume", "Tags": tags},
                    ],
                }}]}
            def create_fleet(self, **kwargs):
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("transient")
                self.created.append(kwargs)

        class Pricing:
            def get_products(self, **kwargs):
                return {"PriceList": [json.dumps({"terms": {"OnDemand": {"x": {"priceDimensions": {"y": {"unit": "Hrs", "pricePerUnit": {"USD": "8"}}}}}}})]}

        clients = {"us-east-1": Ec2("us-east-1"), "us-west-2": Ec2("us-west-2", fail_once=True)}
        first = reconcile_handler(
            {"target": raw}, object(), clients=clients, store=state, pricing=Pricing(), decision_store=decisions,
        )
        self.assertFalse(first["aws_write"])
        self.assertEqual("us-west-2", state.get(target.target_id).active_region)
        self.assertEqual([], clients["us-east-1"].created)
        second = reconcile_handler(
            {"target": raw}, object(), clients=clients, store=state, pricing=Pricing(), decision_store=decisions,
        )
        self.assertEqual("shortfall", second["status"])
        self.assertEqual(1, len(clients["us-west-2"].created))
        self.assertEqual([], clients["us-east-1"].created)

    def test_auto_initial_handler_blocks_owned_capacity_in_multiple_regions(self):
        raw = target_mapping("auto_initial", None)
        target = target_from_mapping(raw)
        tags = [{"Key": key, "Value": value} for key, value in target.tags.items()]
        class Ec2:
            def describe_fleets(self): return {"Fleets": []}
            def describe_instances(self, **kwargs):
                return {"Reservations": [{"Instances": [{"InstanceId": "i-owned", "State": {"Name": "running"}, "Tags": tags}]}]}
        clients = {region.region: Ec2() for region in target.candidate_regions}
        result = reconcile_handler(
            {"target": raw}, object(), clients=clients, store=InMemoryStateStore(), pricing=object(),
            decision_store=InMemoryRegionDecisionStore(),
        )
        self.assertEqual("ownership_mismatch", result["status"])
        self.assertFalse(result["aws_write"])

    def test_pinned_empty_state_rejects_a_different_decision_before_fleet_write(self):
        raw = target_mapping("auto_initial", None)
        target = target_from_mapping(raw)
        version = target_configuration_version(target)
        now = datetime.now(timezone.utc)
        old_snapshot = evidence(target, now - timedelta(minutes=1), (8, 3), configuration_version=version)
        old = decision_from_selection(target, version, 1, old_snapshot, select_region(target, old_snapshot, now - timedelta(minutes=1)), now - timedelta(minutes=1))
        state = InMemoryStateStore()
        state.put_if_version(VersionedState(
            target.target_id, 0, "us-east-1", ("use1-az1",),
            initial_region_decision_version=old.decision_version,
            initial_region_snapshot_id=old.snapshot_id,
        ), None)
        new_snapshot = evidence(target, now, (3, 8), configuration_version=version)
        newer = decision_from_selection(target, version, 2, new_snapshot, select_region(target, new_snapshot, now), now)
        decisions = InMemoryRegionDecisionStore()
        decisions.publish(newer, None)
        class Ec2:
            def describe_fleets(self): return {"Fleets": []}
            def describe_instances(self, **kwargs): return {"Reservations": []}
            def create_fleet(self, **kwargs): raise AssertionError("conflicting decision must not create a Fleet")
        result = reconcile_handler(
            {"target": raw}, object(), clients={region.region: Ec2() for region in target.candidate_regions},
            store=state, pricing=object(), decision_store=decisions,
        )
        self.assertEqual("region_decision_conflict", result["status"])
        self.assertFalse(result["aws_write"])

    def test_local_zone_is_not_part_of_region_sps_and_pins_parent_region(self):
        raw = target_mapping("auto_initial", None)
        raw["candidate_regions"][0]["local_zone_placements"] = [
            {"subnet_id": "subnet-nyc", "zone_id": "use1-nyc-1a"}
        ]
        target = target_from_mapping(raw)
        request = build_sps_request(target, ("us-east-1", "us-west-2"))
        self.assertEqual(["us-east-1", "us-west-2"], request.as_api_kwargs()["RegionNames"])
        self.assertNotIn("use1-nyc-1a", str(request.as_api_kwargs()))
        state = VersionedState(target.target_id, 2, "us-east-1", ("use1-az1", "use1-nyc-1a"))
        empty = [
            {"region": "us-east-1", "owned_fleets": [], "owned_instances": []},
            {"region": "us-west-2", "owned_fleets": [], "owned_instances": []},
        ]
        resolution = resolve_initial_region(target, state, empty, None, 1, datetime.now(timezone.utc))
        self.assertEqual("us-east-1", resolution.region)
        self.assertEqual(1, target.desired_instance_count)
        self.assertEqual(1, target.maximum_instance_count)

    def test_selection_metrics_cover_freshness_eligibility_and_active_difference(self):
        target = target_from_mapping(target_mapping("recommend", "us-east-1"))
        now = datetime.now(timezone.utc)
        snapshot = evidence(target, now, (3, 8))
        selection = select_region(target, snapshot, now)
        metrics = region_selection_metric_data(
            target.target_id, "recommend", selection, snapshot, now, "us-east-1",
        )
        by_name = {item["MetricName"]: item for item in metrics if item["MetricName"] != "RegionSignalAgeSeconds"}
        self.assertEqual(2, by_name["EligibleRegionCount"]["Value"])
        self.assertEqual(0, by_name["NoEligibleRegion"]["Value"])
        self.assertEqual(1, by_name["RecommendationDiffersFromActive"]["Value"])
        self.assertEqual(2, sum(item["MetricName"] == "RegionSignalAgeSeconds" for item in metrics))

    def test_collector_persists_explainable_recommendation_without_capacity_write(self):
        import base64
        import json
        raw = target_mapping("recommend", None)
        target = target_from_mapping(raw)
        regions = tuple(item.region for item in target.candidate_regions)

        class Ec2:
            def __init__(self, region): self.region = region
            def get_spot_placement_scores(self, **kwargs):
                if kwargs["SingleAvailabilityZone"]:
                    return {"SpotPlacementScores": [
                        {"Region": "us-east-1", "AvailabilityZoneId": "use1-az1", "Score": 4},
                        {"Region": "us-west-2", "AvailabilityZoneId": "usw2-az1", "Score": 8},
                    ]}
                return {"SpotPlacementScores": [
                    {"Region": "us-east-1", "Score": 4},
                    {"Region": "us-west-2", "Score": 8},
                ]}
            def describe_launch_template_versions(self, **kwargs):
                inputs = target.region_inputs(self.region)
                tags = [{"Key": key, "Value": value} for key, value in {**target.tags, "bootstrap-contract-version": inputs.bootstrap_contract_version}.items()]
                return {"LaunchTemplateVersions": [{"LaunchTemplateData": {
                    "ImageId": inputs.ami_id, "IamInstanceProfile": {"Arn": inputs.iam_instance_profile_arn},
                    "SecurityGroupIds": list(inputs.security_group_ids), "MetadataOptions": {"HttpTokens": "required"},
                    "UserData": base64.b64encode(b"bootstrap").decode(),
                    "BlockDeviceMappings": [{"Ebs": {"Encrypted": True, "DeleteOnTermination": True}}],
                    "TagSpecifications": [{"ResourceType": kind, "Tags": tags} for kind in ("instance", "volume")],
                }}]}
            def describe_availability_zones(self, **kwargs):
                inputs = target.region_inputs(self.region)
                return {"AvailabilityZones": [
                    {"ZoneName": f"{self.region}a", "ZoneId": item.zone_id, "ZoneType": "availability-zone"}
                    for item in inputs.standard_placements
                ]}
            def describe_instance_type_offerings(self, **kwargs):
                inputs = target.region_inputs(self.region)
                return {"InstanceTypeOfferings": [
                    {"Location": item.zone_id, "InstanceType": "p5.4xlarge"}
                    for item in inputs.standard_placements
                ]}
            def describe_instance_types(self, **kwargs):
                return {"InstanceTypes": [{"VCpuInfo": {"DefaultVCpus": 16}}]}
            def describe_spot_price_history(self, **kwargs):
                return {"SpotPriceHistory": [{"InstanceType": "p5.4xlarge", "SpotPrice": "4"}]}
        class Pricing:
            def get_products(self, **kwargs):
                return {"PriceList": [json.dumps({"terms": {"OnDemand": {"x": {"priceDimensions": {"y": {"unit": "Hrs", "pricePerUnit": {"USD": "8"}}}}}}})]}
        class Quota:
            def get_service_quota(self, **kwargs): return {"Quota": {"Value": 64.0}}
        class CloudWatch:
            def __init__(self): self.calls = []
            def put_metric_data(self, **kwargs): self.calls.append(kwargs)

        signals = InMemoryRegionSignalStore()
        decisions = InMemoryRegionDecisionStore()
        cloudwatch = CloudWatch()
        result = collect_handler(
            {"target": raw, "collection": "sps"}, object(),
            clients={region: Ec2(region) for region in regions}, pricing=Pricing(),
            quota_clients={region: Quota() for region in regions}, signal_store=signals,
            decision_store=decisions, cloudwatch=cloudwatch,
        )
        self.assertEqual("ok", result["status"])
        self.assertFalse(result["aws_write"])
        self.assertEqual("us-west-2", result["region_selection"]["selected_region"])
        decision = decisions.get(target.target_id)
        self.assertEqual("us-west-2", decision.selected_region)
        self.assertIsNotNone(signals.get(target.target_id, decision.snapshot_id))
        self.assertTrue(any(
            any(item["MetricName"] == "RegionDecision" for item in call["MetricData"])
            for call in cloudwatch.calls
        ))


if __name__ == "__main__":
    unittest.main()
