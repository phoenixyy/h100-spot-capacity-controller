"""CDK foundation; synthesis is local and deployment remains operator-authorized."""

from aws_cdk import CfnOutput, CfnParameter, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct

from .config import RESOURCE_NAME_PREFIX


LAMBDA_ASSET_EXCLUDES = [
    ".venv",
    ".git",
    ".codex",
    ".agents",
    "cdk.out*",
    "__pycache__",
    "*.pyc",
    ".gitignore",
    "AGENTS.md",
    "app.py",
    "deployment",
    "pyproject.toml",
    "requirements.lock",
    "tests",
    "openspec",
    "docs",
    "config",
    "*.egg-info",
]


class CapacityControllerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("managed-by", "h100-spot-capacity-controller")
        Tags.of(self).add("Name", f"{RESOURCE_NAME_PREFIX}-Controller")
        target_id = CfnParameter(self, "TargetId", type="String", default="h100-training", description="Persisted capacity target identifier")
        launch_instance_role_arns = CfnParameter(self, "LaunchInstanceRoleArns", type="CommaDelimitedList", description="Exact IAM role ARN(s) referenced by the approved per-Region launch templates")
        launch_template_arns = CfnParameter(self, "LaunchTemplateArns", type="CommaDelimitedList", description="Exact approved per-Region Launch Template ARN(s) that EC2 Fleet may use to run instances")
        reconcile_schedule = CfnParameter(self, "ReconcileScheduleExpression", type="String", default="rate(1 minute)")
        signal_schedule = CfnParameter(self, "SignalScheduleExpression", type="String", default="rate(5 minutes)")
        sps_schedule = CfnParameter(self, "SpsScheduleExpression", type="String", default="rate(15 minutes)")
        self.state_table = dynamodb.Table(
            self, "State", partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING), encryption=dynamodb.TableEncryption.AWS_MANAGED,
            table_name=f"{RESOURCE_NAME_PREFIX}-State",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            time_to_live_attribute="ttl", removal_policy=RemovalPolicy.RETAIN,
        )
        self.notifications = sns.Topic(self, "Notifications", topic_name=f"{RESOURCE_NAME_PREFIX}-Notifications")
        self.notifications.apply_removal_policy(RemovalPolicy.RETAIN)
        role = iam.Role(
            self, "ControllerRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name=f"{RESOURCE_NAME_PREFIX}-Controller-Role",
        )
        self.state_table.grant_read_write_data(role)
        role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:TransactWriteItems"], resources=[self.state_table.table_arn],
        ))
        self.notifications.grant_publish(role)
        role.add_to_policy(iam.PolicyStatement(actions=[
            "ec2:DescribeAvailabilityZones", "ec2:DescribeFleets", "ec2:DescribeFleetInstances", "ec2:DescribeInstances",
            "ec2:DescribeInstanceTypes", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeSpotPriceHistory", "ec2:DescribeSubnets",
            "ec2:GetSpotPlacementScores", "pricing:GetProducts", "eks:DescribeCluster",
            "cloudwatch:PutMetricData",
        ], resources=["*"]))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:CreateFleet"], resources=["*"],
            conditions={"StringEquals": {"aws:RequestTag/managed-by": "h100-spot-capacity-controller"}},
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:RunInstances"], resources=["*"],
            conditions={"ArnEquals": {"ec2:LaunchTemplate": launch_template_arns.value_as_list}},
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:CreateTags"], resources=["*"],
            conditions={"StringEquals": {
                "aws:RequestTag/managed-by": "h100-spot-capacity-controller",
                "ec2:CreateAction": "CreateFleet",
            }},
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:CreateTags"], resources=["*"],
            conditions={"StringEquals": {
                "aws:RequestTag/managed-by": "h100-spot-capacity-controller",
                "ec2:CreateAction": "RunInstances",
            }},
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:ModifyFleet", "ec2:DeleteFleets", "ec2:TerminateInstances"], resources=["*"],
            conditions={"StringEquals": {"ec2:ResourceTag/managed-by": "h100-spot-capacity-controller"}},
        ))
        role.add_to_policy(iam.PolicyStatement(actions=["iam:PassRole"], resources=launch_instance_role_arns.value_as_list))
        default_policy = role.node.try_find_child("DefaultPolicy")
        if not isinstance(default_policy, iam.Policy):
            raise RuntimeError("controller role default policy was not created")
        default_policy_resource = default_policy.node.default_child
        if not isinstance(default_policy_resource, iam.CfnPolicy):
            raise RuntimeError("controller role default policy has no CfnPolicy resource")
        default_policy_resource.policy_name = f"{RESOURCE_NAME_PREFIX}-Controller-Policy"
        collector_role = iam.Role(
            self, "CollectorRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name=f"{RESOURCE_NAME_PREFIX}-Collector-Role",
        )
        self.state_table.grant_read_write_data(collector_role)
        collector_role.add_to_policy(iam.PolicyStatement(actions=[
            "ec2:DescribeAvailabilityZones", "ec2:DescribeInstanceTypes",
            "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeLaunchTemplateVersions",
            "ec2:DescribeSpotPriceHistory", "ec2:DescribeSubnets",
            "ec2:GetSpotPlacementScores", "pricing:GetProducts",
            "servicequotas:GetServiceQuota", "cloudwatch:PutMetricData",
        ], resources=["*"]))
        collector_default_policy = collector_role.node.try_find_child("DefaultPolicy")
        if not isinstance(collector_default_policy, iam.Policy):
            raise RuntimeError("collector role default policy was not created")
        collector_policy_resource = collector_default_policy.node.default_child
        if not isinstance(collector_policy_resource, iam.CfnPolicy):
            raise RuntimeError("collector role default policy has no CfnPolicy resource")
        collector_policy_resource.policy_name = f"{RESOURCE_NAME_PREFIX}-Collector-Policy"
        code = lambda_.Code.from_asset(".", exclude=LAMBDA_ASSET_EXCLUDES)
        runtime_environment = {
            "STATE_TABLE_NAME": self.state_table.table_name,
            "TARGET_ID": target_id.value_as_string,
            "NOTIFICATION_TOPIC_ARN": self.notifications.topic_arn,
        }

        def retained_log_group(name: str, function_role: iam.IRole = role) -> logs.LogGroup:
            group = logs.LogGroup(
                self, f"{name}Logs", log_group_name=f"{RESOURCE_NAME_PREFIX}-{name}-Logs",
                retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.RETAIN,
            )
            group.grant_write(function_role)
            return group

        reconciler = lambda_.Function(self, "Reconciler", function_name=f"{RESOURCE_NAME_PREFIX}-Reconciler", runtime=lambda_.Runtime.PYTHON_3_12, handler="runtime_handler.reconcile", code=code, role=role, memory_size=512, timeout=Duration.minutes(1), log_group=retained_log_group("Reconciler"), environment=runtime_environment)
        collector = lambda_.Function(self, "Collector", function_name=f"{RESOURCE_NAME_PREFIX}-Collector", runtime=lambda_.Runtime.PYTHON_3_12, handler="runtime_handler.collect", code=code, role=collector_role, memory_size=512, timeout=Duration.minutes(5), log_group=retained_log_group("Collector", collector_role), environment=runtime_environment)
        spot_event_handler = lambda_.Function(self, "SpotEventHandler", function_name=f"{RESOURCE_NAME_PREFIX}-Spot-Event-Handler", runtime=lambda_.Runtime.PYTHON_3_12, handler="runtime_handler.spot_event", code=code, role=role, memory_size=512, timeout=Duration.minutes(1), log_group=retained_log_group("Spot-Event-Handler"), environment=runtime_environment)
        scheduled_event = events.RuleTargetInput.from_object({"target_id": target_id.value_as_string})
        price_event = events.RuleTargetInput.from_object({"target_id": target_id.value_as_string, "collection": "price-and-local"})
        sps_event = events.RuleTargetInput.from_object({"target_id": target_id.value_as_string, "collection": "sps"})
        events.Rule(self, "ReconcileSchedule", rule_name=f"{RESOURCE_NAME_PREFIX}-Reconcile-Schedule", schedule=events.Schedule.expression(reconcile_schedule.value_as_string), targets=[targets.LambdaFunction(reconciler, event=scheduled_event)])
        events.Rule(self, "SignalSchedule", rule_name=f"{RESOURCE_NAME_PREFIX}-Signal-Schedule", schedule=events.Schedule.expression(signal_schedule.value_as_string), targets=[targets.LambdaFunction(collector, event=price_event)])
        events.Rule(self, "SpsSchedule", rule_name=f"{RESOURCE_NAME_PREFIX}-SPS-Schedule", schedule=events.Schedule.expression(sps_schedule.value_as_string), targets=[targets.LambdaFunction(collector, event=sps_event)])
        events.Rule(self, "SpotLifecycleEvents", rule_name=f"{RESOURCE_NAME_PREFIX}-Spot-Lifecycle-Events", event_pattern=events.EventPattern(source=["aws.ec2"], detail_type=["EC2 Spot Instance Interruption Warning", "EC2 Instance Rebalance Recommendation"]), targets=[targets.LambdaFunction(spot_event_handler)])
        cloudwatch.Dashboard(self, "CapacityDashboard", dashboard_name=f"{RESOURCE_NAME_PREFIX}-Capacity-Dashboard", widgets=[
            [cloudwatch.GraphWidget(title="Machine capacity", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="DesiredMachineCapacity", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="FulfilledMachineCapacity", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="MachineShortfall", statistic="Maximum"),
            ])],
            [cloudwatch.GraphWidget(title="Accelerator and Zone state", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RealizedAcceleratorCount", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RealizedAcceleratorModelCount", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RealizedAcceleratorCount", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="ActiveZoneCount", statistic="Maximum"),
            ])],
            [cloudwatch.GraphWidget(title="Placement and price signals", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="SpotPlacementScore", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="SpotPriceUsd", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="LocalZoneEligible", statistic="Minimum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="CapacitySignalError", statistic="Sum"),
            ])],
            [cloudwatch.GraphWidget(title="SPS Region selection", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="EligibleRegionCount", statistic="Minimum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="NoEligibleRegion", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RegionSignalAgeSeconds", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RecommendationDiffersFromActive", statistic="Maximum"),
            ])],
            [cloudwatch.GraphWidget(title="Failover, retries and interruptions", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="FailoverState", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="FailoverTrigger", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RetryCount", statistic="Sum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="InterruptionCount", statistic="Sum"),
            ])],
            [cloudwatch.GraphWidget(title="Existing EKS node readiness", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="EksRegisteredNodeCount", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="EksReadyNodeCount", statistic="Maximum"),
            ])],
            [cloudwatch.GraphWidget(title="Per-Zone expansion and capacity", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="ZoneActive", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="ZoneMachineCapacity", statistic="Maximum"),
            ])],
        ])
        cloudwatch.Alarm(
            self, "NoEligibleRegionAlarm",
            alarm_name=f"{RESOURCE_NAME_PREFIX}-No-Eligible-Region",
            metric=cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="NoEligibleRegion", statistic="Maximum", period=Duration.minutes(15)),
            threshold=1, evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        )
        cloudwatch.Alarm(
            self, "StaleRegionSignalAlarm",
            alarm_name=f"{RESOURCE_NAME_PREFIX}-Stale-Region-Signal",
            metric=cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RegionSignalAgeSeconds", statistic="Maximum", period=Duration.minutes(15)),
            threshold=1200, evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )
        CfnOutput(self, "StateTableName", value=self.state_table.table_name)
        CfnOutput(self, "NotificationTopicArn", value=self.notifications.topic_arn)
        CfnOutput(self, "RegionSelectionModes", value="manual,recommend,auto_initial")
