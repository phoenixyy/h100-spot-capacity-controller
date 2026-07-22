## Why

H100 Spot capacity is scarce and can be interrupted at any time. Manually checking
capacity and repeatedly submitting requests is unreliable, provides little audit
history, and makes it easy to exceed an intended cost or instance-count limit.

## What Changes

- Add a configuration-driven controller that continuously reconciles a desired
  number of H100 Spot instances in one operator-selected AWS Region.
- Add a separately identified, tightly bounded `functional-validation` target
  profile that may use one `g6e.xlarge` L40S Spot instance in Tokyo and Seoul to
  exercise real Fleet fulfillment and whole-target Region failover without
  weakening the H100 production allowlist.
- Count capacity in EC2 instances, accept one or more operator-entered H100 EC2
  instance types with per-type Spot price caps derived from matching Linux
  On-Demand prices, and ship a disabled initial target
  template with desired and maximum instance counts set to one.
- Add read-only Spot Placement Score discovery for standard Regions and
  Availability Zones, while treating scores as advisory inputs to the operator's
  Region selection.
- Prefer the Zone that first fulfills H100 capacity, expand sequentially to other
  standard Availability Zones and then explicitly approved Local Zones in the same
  Region only after configured shortfall thresholds.
- When every approved Zone in a Region cannot fulfill the target, prepare a
  whole-target failover to another approved Region, notify the operator, and wait
  for explicit approval before terminating the old Region and requesting the full
  desired count in the new Region.
- Allow an operator to request the same whole-target failover plan for a healthy
  target, so planned migration and functional validation can exercise the exact
  approval and execution path without fabricating a capacity failure. Requesting
  a plan does not authorize or execute any capacity change.
- Add a safe EC2 Fleet request lifecycle with explicit ownership tags, target
  limits, price controls, and idempotent retries.
- Prefix every newly created AWS resource whose service permits an operator-defined
  name with the exact string `Phoenix-Codex-Local-Spot-`; AWS-fixed names such as
  `AWSServiceRoleForEC2Fleet` and pre-existing CDK bootstrap resources are explicit
  exceptions.
- Add Spot Placement Score and Spot-price collection, CloudWatch metrics, logs,
  alarms, and operator notifications.
- Add interruption detection and replacement reconciliation for controller-owned
  instances.
- Reserve an optional integration path for operator-owned Amazon EKS clusters:
  an approved per-Region launch template can bootstrap acquired instances into
  the existing cluster in the same Region, without the controller creating or
  operating Kubernetes workloads.
- Provide AWS CDK infrastructure, least-privilege IAM policies, automated tests,
  and an operator runbook.
- Prevent a target from combining GPU capacity across Regions or requesting only a
  regional shortfall elsewhere; at most one Region may own active target capacity.
- Report generic realized accelerator count and model for every target while
  retaining H100-specific visibility only for H100 production targets, so an L40S
  validation instance is never reported as H100 capacity.

## Capabilities

### New Capabilities

- `spot-capacity-reconciliation`: Maintain an explicitly configured H100 Spot
  instance target in one AWS Region across primary standard Availability Zones
  and optional backup Local Zones without duplicating controller-owned fleets.
- `spot-capacity-observability`: Capture capacity signals, fleet state, retries,
  costs signals, and interruption events for operators.
- `spot-capacity-safety`: Require explicit guardrails and restrict automation to
  resources that the controller owns.

### Modified Capabilities

None.

## Impact

Adds a Python/AWS CDK application using EventBridge, Lambda, DynamoDB, CloudWatch,
SNS, and EC2 Fleet/Spot APIs. Deployment requires a dedicated AWS IAM role and
operator-supplied network and instance-launch settings. A target can optionally
reference an existing EKS cluster in each approved Region; its launch template,
node access, and bootstrap configuration remain operator-owned. The controller
does not create EKS clusters or manage Kubernetes workloads. Local Zone opt-in,
subnet creation, routing, quotas, and other prerequisites remain operator-owned
and require explicit authorization outside the controller.

The first real-capacity validation target is standalone, disabled by default, and
uses `g6e.xlarge` with desired and maximum counts of one across
`ap-northeast-1` and `ap-northeast-2`. It has a distinct target identifier and
ownership tags from every H100 production target. Creating its per-Region launch
templates or enabling it remains an explicitly approved deployment action that can
incur EC2, EBS, public IPv4, logging, and related AWS charges.

The initial deployment uses the operator-required resource-name prefix
`Phoenix-Codex-Local-Spot-` for every created resource with a customizable AWS
name, including CloudFormation stacks, IAM roles/profiles, security groups, Launch
Templates, controller resources, dashboards, rules, functions, topics, tables and
log groups. This does not rename AWS-managed service-linked roles or pre-existing
account resources.
