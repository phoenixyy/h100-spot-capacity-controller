## ADDED Requirements

### Requirement: Spending and scale guardrails
The system SHALL require a maximum desired instance count and a maximum acceptable
Spot price per instance-hour for every approved instance type before creating a
fleet. It SHALL not set any On-Demand target capacity and SHALL reject a target
exceeding any configured limit.

The default cap source SHALL be the matching AWS Linux On-Demand hourly price for
the target Region and instance type. This value is a Spot ceiling only; the system
SHALL NOT request On-Demand capacity.

The generated initial target SHALL be disabled with desired and maximum instance
counts set to one. Enabling the target MUST be a separate explicit configuration
change after dry-run review; the presence of the initial values alone MUST NOT
request capacity.

#### Scenario: Initial target is generated
- **WHEN** the operator generates the first target configuration
- **THEN** the system SHALL set `enabled` to false and both desired and maximum
  instance counts to one without making an EC2 Fleet API call

#### Scenario: Target is explicitly enabled
- **WHEN** the operator enables a valid target after reviewing its dry-run
- **THEN** reconciliation MAY create its owned fleet within the configured machine
  count and per-type price limits

#### Scenario: Request exceeds a configured guardrail
- **WHEN** a target requests more instances than its maximum or a Spot price cap
  above its approved limit
- **THEN** the system SHALL reject the target and SHALL emit an audit event without
  making an EC2 Fleet API call

#### Scenario: Zone expansion activates
- **WHEN** another standard Availability Zone or approved Local Zone is added as a
  capacity pool
- **THEN** the system SHALL preserve the configured desired count, maximum count,
  Spot-only purchase option, and per-instance price cap

### Requirement: Validation profile cannot weaken production guardrails
The system SHALL keep `h100-production` and `functional-validation` type allowlists,
metrics, ownership identity, scale limits, integration modes, and Region/Zone
constraints separate. Enabling a validation profile MUST require the same dry-run,
explicit target write, and capacity-enable acknowledgement as production.

#### Scenario: L40S is entered as H100 production capacity
- **WHEN** an operator configures `g6e.xlarge` under `h100-production`
- **THEN** the system SHALL reject it without making an AWS resource change

#### Scenario: Validation capacity is explicitly enabled
- **WHEN** a valid disabled Tokyo/Seoul validation target has passed dry-run and the
  operator separately acknowledges capacity enablement
- **THEN** reconciliation MAY request at most one `g6e.xlarge` Spot machine in only
  its active Region

### Requirement: Destructive Region failover approval
The system MUST NOT stop a source fleet, terminate a source instance, or create a
destination fleet for Region failover without an unexpired operator approval bound
to the current failover plan. The approval MUST identify the target and
configuration version, source and destination Regions, full desired count, and
source resources authorized for termination. Approval SHALL be single-use.
An operator-requested healthy-target migration plan SHALL confer no additional
authority and SHALL use this exact approval boundary before any capacity change.

#### Scenario: Manual plan is persisted but not approved
- **WHEN** an operator persists a healthy-target migration plan but has not supplied
  its matching approval
- **THEN** the system SHALL retain the source capacity and SHALL NOT create
  destination capacity

#### Scenario: Approval does not match the current plan
- **WHEN** an approval is expired, already used, or does not exactly match the
  current target version and failover plan
- **THEN** the system SHALL reject it without changing any AWS resource

#### Scenario: Approved source cleanup is scoped
- **WHEN** a valid failover approval authorizes source cleanup
- **THEN** the system SHALL stop and terminate only the controller-owned fleet and
  instances named by the approved target and source Region

#### Scenario: Source capacity still exists
- **WHEN** any controller-owned target instance remains active in the source Region
- **THEN** the system SHALL NOT create or increase destination Region capacity

#### Scenario: Destination request is allowed
- **WHEN** approved cleanup is complete and the source Region has zero active owned
  target instances
- **THEN** the system MAY create the destination fleet for exactly the full desired
  count within the configured price and maximum limits

### Requirement: Claimed failover execution survives source-drain delay
Before its first destructive source action, the system SHALL atomically claim an
unexpired matching approval and confirm retention of the plan, current-plan pointer,
and approval records for the duration of execution. A retry of an existing claim
MUST repeat that retention check before touching EC2. The claim MUST remain bound to the exact
plan identifier, target configuration version, source and destination Regions,
full desired count, source Fleet, and source instance identifiers. A claimed plan
MAY continue after its original approval expiry only to complete that exact
execution. It MUST NOT authorize a new plan, a second migration, a different
source resource, or destination capacity before the source-zero barrier.

#### Scenario: Source termination outlasts the approval window
- **WHEN** the controller claimed a valid approval before stopping the source Fleet
  and source termination remains in progress after the original approval expiry
- **THEN** retries SHALL retain the exact execution claim, wait for zero owned
  source instances, and only then request the full destination target

#### Scenario: Approval expires before execution claim
- **WHEN** a matching approval expires before the controller atomically claims it
- **THEN** the system SHALL make no source or destination capacity write and SHALL
  retain the source capacity

#### Scenario: Claimed execution is replayed with a changed contract
- **WHEN** a retry presents a claimed approval but its plan, target version,
  source Fleet, source instances, or destination differs from the claim
- **THEN** the system SHALL reject the retry without changing AWS resources

### Requirement: Least-privilege resource ownership
The deployment SHALL grant the controller only the EC2, CloudWatch, DynamoDB,
EventBridge, notification, and optional read-only EKS permissions required by its
implementation. Any destructive EC2 action MUST be constrained to resources
carrying the controller's ownership tags.

The controller role MUST NOT permit Local Zone opt-in or opt-out, subnet creation or
modification, route modification, or quota-increase operations. Local Zone
prerequisites are operator-owned and SHALL be inspected read-only.
The controller role MUST NOT create, delete, or modify EKS clusters, node groups,
access entries, Kubernetes workloads, or scheduler add-ons. EKS node authorization
and launch-template bootstrap remain operator-owned.

Every resource created by the solution whose AWS API permits an operator-defined
physical name SHALL begin with `Phoenix-Codex-Local-Spot-`. Resources without a
name field SHALL receive a `Name` tag with that prefix when the resource supports
tags. AWS-fixed service-linked role names and pre-existing account/bootstrap
resources SHALL be documented exceptions and SHALL NOT be recreated merely to
force the prefix.

#### Scenario: An unowned fleet is discovered
- **WHEN** reconciliation observes an EC2 Fleet without the configured ownership
  tags
- **THEN** the system SHALL treat it as read-only and SHALL NOT modify or cancel it

#### Scenario: Backup Local Zone is not ready
- **WHEN** an approved Local Zone is not opted in or has no approved subnet
- **THEN** the system SHALL skip that location without enabling the zone group or
  creating or modifying network resources

#### Scenario: Existing EKS integration is configured
- **WHEN** a target uses `existing-eks` mode
- **THEN** the controller SHALL validate only the declared integration metadata and
  SHALL NOT create or modify an EKS cluster, node group, access entry, Kubernetes
  workload, or scheduler add-on

#### Scenario: A customizable AWS resource is created
- **WHEN** an authorized deployment creates a resource with an operator-defined
  physical name
- **THEN** the name SHALL begin with `Phoenix-Codex-Local-Spot-`

#### Scenario: AWS owns the resource name
- **WHEN** an authorized deployment requires `AWSServiceRoleForEC2Fleet` or uses a
  pre-existing CDK bootstrap resource
- **THEN** the deployment SHALL document the fixed-name exception and SHALL NOT
  attempt to replace or rename that AWS-managed or pre-existing resource

### Requirement: Safe disable and rollback
The system SHALL support disabling a target so that it makes no new capacity
requests. Cancellation or termination of existing instances MUST be a separate,
explicit operator action.

#### Scenario: Target is disabled
- **WHEN** an operator disables an existing target
- **THEN** subsequent reconciliation runs SHALL not create or increase fleet
  capacity and SHALL preserve existing running instances
