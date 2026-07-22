import unittest

try:
    import aws_cdk as cdk
    from aws_cdk import assertions
    from h100_spot_controller.infrastructure import CapacityControllerStack, LAMBDA_ASSET_EXCLUDES
    from h100_spot_controller.config import RESOURCE_NAME_PREFIX
    CDK_AVAILABLE = True
except ImportError:
    CDK_AVAILABLE = False


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


@unittest.skipUnless(CDK_AVAILABLE, "CDK dependencies are installed in the project validation environment")
class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        app = cdk.App()
        self.template = assertions.Template.from_stack(CapacityControllerStack(app, "TestCapacityController")).to_json()

    def test_state_is_encrypted_and_retained(self):
        tables = [item for item in self.template["Resources"].values() if item["Type"] == "AWS::DynamoDB::Table"]
        self.assertEqual(1, len(tables))
        properties = tables[0]["Properties"]
        self.assertEqual("PAY_PER_REQUEST", properties["BillingMode"])
        self.assertEqual(True, properties["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"])
        self.assertEqual(True, properties["SSESpecification"]["SSEEnabled"])
        self.assertEqual("Retain", tables[0]["DeletionPolicy"])
        log_groups = [item for item in self.template["Resources"].values() if item["Type"] == "AWS::Logs::LogGroup"]
        self.assertEqual(3, len(log_groups))
        self.assertTrue(all(item["Properties"]["RetentionInDays"] == 30 for item in log_groups))
        self.assertTrue(all(item["DeletionPolicy"] == "Retain" for item in log_groups))

    def test_template_has_tags_and_no_destructive_or_network_mutation_permissions(self):
        rendered = str(self.template)
        self.assertIn("h100-spot-capacity-controller", rendered)
        forbidden = {"ec2:CreateSubnet", "ec2:ModifySubnetAttribute", "ec2:CreateRoute", "ec2:ModifyAvailabilityZoneGroup"}
        actions = set()
        for item in _walk(self.template):
            if "Action" in item:
                action = item["Action"]
                actions.update(action if isinstance(action, list) else [action])
        self.assertFalse(forbidden & actions)
        destructive = {"ec2:TerminateInstances", "ec2:DeleteFleets", "ec2:ModifyFleet"}
        destructive_statements = [item for item in _walk(self.template) if destructive & set(item.get("Action", []) if isinstance(item.get("Action"), list) else [item.get("Action")])]
        self.assertTrue(destructive_statements)
        self.assertTrue(all("ec2:ResourceTag/managed-by" in str(item.get("Condition", {})) for item in destructive_statements))
        pass_role = [item for item in _walk(self.template) if "iam:PassRole" in (item.get("Action", []) if isinstance(item.get("Action"), list) else [item.get("Action")])]
        self.assertEqual(1, len(pass_role))
        self.assertNotEqual("*", pass_role[0]["Resource"])
        self.assertIn("LaunchInstanceRoleArns", str(pass_role[0]["Resource"]))
        run_instances = [item for item in _walk(self.template) if "ec2:RunInstances" in (item.get("Action", []) if isinstance(item.get("Action"), list) else [item.get("Action")])]
        self.assertEqual(1, len(run_instances))
        self.assertIn("LaunchTemplateArns", str(run_instances[0].get("Condition", {})))
        self.assertIn("ec2:LaunchTemplate", str(run_instances[0].get("Condition", {})))
        create_tags = [item for item in _walk(self.template) if "ec2:CreateTags" in (item.get("Action", []) if isinstance(item.get("Action"), list) else [item.get("Action")])]
        self.assertTrue(any("RunInstances" in str(item.get("Condition", {})) for item in create_tags))
        self.assertTrue(all("aws:RequestTag/managed-by" in str(item.get("Condition", {})) for item in create_tags))

    def test_dashboard_and_metric_publish_permission_exist(self):
        resources = list(self.template["Resources"].values())
        self.assertTrue(any(item["Type"] == "AWS::CloudWatch::Dashboard" for item in resources))
        self.assertIn("cloudwatch:PutMetricData", str(self.template))
        self.assertIn("pricing:GetProducts", str(self.template))
        self.assertIn("ec2:DescribeSubnets", str(self.template))

    def test_reconciler_can_atomically_claim_failover_execution_on_state_table_only(self):
        statements = [item for item in _walk(self.template) if "dynamodb:TransactWriteItems" in (
            item.get("Action", []) if isinstance(item.get("Action"), list) else [item.get("Action")]
        )]
        self.assertEqual(1, len(statements))
        self.assertNotEqual("*", statements[0]["Resource"])
        self.assertIn("State", str(statements[0]["Resource"]))

    def test_reconciler_can_verify_gpu_instance_metadata(self):
        reconciler = next(
            item for item in self.template["Resources"].values()
            if item["Type"] == "AWS::Lambda::Function"
            and item["Properties"]["FunctionName"].endswith("-Reconciler")
        )
        controller_role_id = reconciler["Properties"]["Role"]["Fn::GetAtt"][0]
        policies = [
            item for item in self.template["Resources"].values()
            if item["Type"] == "AWS::IAM::Policy"
            and {entry.get("Ref") for entry in item["Properties"].get("Roles", []) if isinstance(entry, dict)} == {controller_role_id}
        ]
        self.assertIn("ec2:DescribeInstanceTypes", str(policies))

    def test_collector_role_is_read_only_for_capacity_and_has_selection_reads(self):
        collector = next(
            item for item in self.template["Resources"].values()
            if item["Type"] == "AWS::Lambda::Function"
            and item["Properties"]["FunctionName"].endswith("-Collector")
        )
        collector_role_id = collector["Properties"]["Role"]["Fn::GetAtt"][0]
        policies = [
            item for item in self.template["Resources"].values()
            if item["Type"] == "AWS::IAM::Policy"
            and {entry.get("Ref") for entry in item["Properties"].get("Roles", []) if isinstance(entry, dict)} == {collector_role_id}
        ]
        rendered = str(policies)
        for required in ("ec2:GetSpotPlacementScores", "ec2:DescribeInstanceTypes", "servicequotas:GetServiceQuota", "pricing:GetProducts"):
            self.assertIn(required, rendered)
        for forbidden in ("ec2:CreateFleet", "ec2:ModifyFleet", "ec2:DeleteFleets", "ec2:RunInstances", "ec2:TerminateInstances", "eks:DescribeCluster"):
            self.assertNotIn(forbidden, rendered)

    def test_region_selection_dashboard_metrics_and_alarms_exist(self):
        rendered = str(self.template)
        for metric in ("EligibleRegionCount", "NoEligibleRegion", "RegionSignalAgeSeconds", "RecommendationDiffersFromActive"):
            self.assertIn(metric, rendered)
        alarms = [item for item in self.template["Resources"].values() if item["Type"] == "AWS::CloudWatch::Alarm"]
        self.assertEqual(2, len(alarms))
        self.assertTrue(all(item["Properties"]["AlarmName"].startswith(f"{RESOURCE_NAME_PREFIX}-") for item in alarms))

    def test_schedules_are_configurable_and_default_to_one_five_fifteen_minutes(self):
        parameters = self.template["Parameters"]
        self.assertEqual("rate(1 minute)", parameters["ReconcileScheduleExpression"]["Default"])
        self.assertEqual("rate(5 minutes)", parameters["SignalScheduleExpression"]["Default"])
        self.assertEqual("rate(15 minutes)", parameters["SpsScheduleExpression"]["Default"])
        rules = [item for item in self.template["Resources"].values() if item["Type"] == "AWS::Events::Rule"]
        self.assertIn("ReconcileScheduleExpression", str(rules))
        self.assertIn("SignalScheduleExpression", str(rules))
        self.assertIn("SpsScheduleExpression", str(rules))
        self.assertIn("price-and-local", str(rules))
        self.assertIn('sps', str(rules))

    def test_default_notification_topic_is_retained_and_passed_to_runtime(self):
        topics = [item for item in self.template["Resources"].values() if item["Type"] == "AWS::SNS::Topic"]
        self.assertEqual(1, len(topics))
        self.assertEqual("Retain", topics[0]["DeletionPolicy"])
        functions = [item for item in self.template["Resources"].values() if item["Type"] == "AWS::Lambda::Function"]
        self.assertTrue(all("NOTIFICATION_TOPIC_ARN" in item["Properties"]["Environment"]["Variables"] for item in functions))

    def test_lambda_memory_is_explicitly_sized_for_aws_sdk_clients(self):
        functions = [item for item in self.template["Resources"].values() if item["Type"] == "AWS::Lambda::Function"]
        self.assertEqual(3, len(functions))
        self.assertTrue(all(item["Properties"]["MemorySize"] == 512 for item in functions))

    def test_lambda_asset_excludes_generated_assemblies_and_python_caches(self):
        self.assertIn("cdk.out*", LAMBDA_ASSET_EXCLUDES)
        self.assertIn("__pycache__", LAMBDA_ASSET_EXCLUDES)
        self.assertIn("*.pyc", LAMBDA_ASSET_EXCLUDES)
        self.assertIn("deployment", LAMBDA_ASSET_EXCLUDES)
        self.assertIn("app.py", LAMBDA_ASSET_EXCLUDES)

    def test_every_customizable_controller_resource_name_uses_required_prefix(self):
        name_properties = {
            "AWS::DynamoDB::Table": "TableName",
            "AWS::SNS::Topic": "TopicName",
            "AWS::IAM::Role": "RoleName",
            "AWS::IAM::Policy": "PolicyName",
            "AWS::Logs::LogGroup": "LogGroupName",
            "AWS::Lambda::Function": "FunctionName",
            "AWS::Events::Rule": "Name",
            "AWS::CloudWatch::Dashboard": "DashboardName",
        }
        for resource in self.template["Resources"].values():
            property_name = name_properties.get(resource["Type"])
            if property_name is None:
                continue
            value = resource["Properties"].get(property_name)
            self.assertIsInstance(value, str, f"{resource['Type']} lacks an explicit physical name")
            self.assertTrue(value.startswith(f"{RESOURCE_NAME_PREFIX}-"), value)
