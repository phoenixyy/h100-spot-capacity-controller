## ADDED Requirements

### Requirement: Explicit capacity target configuration
The system SHALL require an operator-managed capacity-target configuration before it
can request capacity. The configuration MUST name exactly one AWS Region, one or
more instance types from an explicitly selected internal accelerator profile with a
Spot price cap for each type, a desired instance count,
one or more primary subnets in standard Availability Zones, a launch template, and
a controller ownership identifier. It MAY also name explicitly approved backup
Local Zones and existing subnets in those Local Zones, but each backup Local Zone
MUST belong to the target's parent Region. A desired count represents EC2
instances, not individual GPUs.

Every configured instance type SHALL have a capacity weight of one. The system MAY
report the realized accelerator model and count, but SHALL reconcile desired and
maximum capacity only as EC2 machine counts.

#### Scenario: Valid target is accepted
- **WHEN** an operator supplies a configuration containing all required fields and
  a desired count within its configured maximum
- **THEN** the system records the target as eligible for reconciliation

#### Scenario: Multiple H100 machine types are accepted
- **WHEN** an operator enters multiple approved H100-compatible instance types with
  valid per-type price caps
- **THEN** the system SHALL treat each launched instance as one fulfilled machine
  regardless of its physical H100 GPU count

#### Scenario: Unsafe target is rejected
- **WHEN** a configuration omits its Region, launch template, ownership identifier,
  instance types, primary standard-AZ subnets, or target limit, or names a Local
  Zone outside the target's parent Region
- **THEN** the system SHALL reject it without creating or changing an AWS resource

#### Scenario: Non-H100 substitution is rejected
- **WHEN** an `h100-production` instance type is unknown, not approved as
  H100-compatible, or lacks its own price cap
- **THEN** the system SHALL reject the target and SHALL NOT silently substitute a
  different accelerator type

### Requirement: Isolated functional-validation profile
The system SHALL support a separate `functional-validation` accelerator profile for
real control-path validation when H100 Spot capacity cannot be fulfilled. In the
first release this profile MUST allow exactly `g6e.xlarge`, MUST identify its
accelerator as one NVIDIA L40S, MUST use `integration_mode: standalone`, MUST use
desired and maximum machine counts of one, MUST reject Local Zone placements, and
MUST use ordered candidate Regions `ap-northeast-1` then `ap-northeast-2`.

A functional-validation target MUST use a target identifier and ownership-purpose
tag distinct from H100 production targets. Its fulfillment SHALL prove only the
shared controller path and SHALL NOT be treated as H100 capacity or workload
compatibility evidence.

#### Scenario: Bounded validation target is accepted
- **WHEN** an operator supplies a disabled one-machine standalone target containing
  only `g6e.xlarge`, Tokyo then Seoul standard-AZ placements, and the required
  validation ownership tag
- **THEN** the system SHALL accept it for dry-run and later explicit enablement

#### Scenario: Validation profile attempts to escape its boundary
- **WHEN** a functional-validation target requests another type, more than one
  machine, existing-EKS integration, Local Zones, different candidate Regions, or
  missing validation ownership identity
- **THEN** the system SHALL reject it before any Fleet write

### Requirement: Maintain Spot capacity
The system SHALL create or reconcile exactly one controller-owned EC2 Fleet of type
`maintain` for each enabled capacity target. The fleet SHALL request only Spot
capacity and SHALL use the configured desired instance count as its Spot target.
The maintain fleet itself SHALL remain the persistent capacity request between
scheduled reconciliation runs.

#### Scenario: No owned fleet exists
- **WHEN** a valid enabled target has no matching controller-owned fleet
- **THEN** the reconciliation run SHALL create one maintain-type EC2 Fleet for it

#### Scenario: Fleet is below target after interruption
- **WHEN** a controller-owned fleet has less fulfilled Spot capacity than its
  configured target because an instance was interrupted or capacity was unavailable
- **THEN** the system SHALL leave the maintain request active and record the
  shortfall for the next reconciliation and notification cycle

#### Scenario: Scheduled reconciliation repeats
- **WHEN** the reconciliation schedule runs and the owned fleet already has the
  correct target and placement configuration
- **THEN** the system SHALL describe and record that fleet without creating another
  fleet or resubmitting an equivalent capacity request

### Requirement: Optional existing-EKS node integration
The system SHALL support `standalone` mode by default and optional `existing-eks`
mode when the operator supplies one existing EKS cluster for every
approved candidate Region. In `existing-eks` mode, the target configuration MUST
identify the matching Region, cluster identifier, launch-template bootstrap
contract version, expected controller labels, and approved GPU taint. The Fleet
launch template SHALL be responsible for joining instances to that Region's EKS
cluster as self-managed nodes.

The controller SHALL validate integration configuration but SHALL NOT create an
EKS cluster, node group, access entry, Kubernetes workload, or scheduler add-on.
It SHALL reject a candidate Region whose declared EKS cluster belongs to another
Region. A Local Zone instance SHALL join only the cluster in its parent Region.

#### Scenario: Matching existing EKS integration is accepted
- **WHEN** an operator enables `existing-eks` mode with complete bootstrap data and
  an EKS cluster declared in the same candidate Region
- **THEN** the system SHALL allow the Fleet launch template for that Region to
  bootstrap controller-owned instances as self-managed nodes of that cluster

#### Scenario: Cross-Region EKS integration is rejected
- **WHEN** an operator declares an EKS cluster whose Region differs from its
  candidate Region
- **THEN** the system SHALL reject the target without creating or modifying a Fleet

#### Scenario: Local Zone node joins its parent-Region cluster
- **WHEN** an approved Local Zone instance is launched for an `existing-eks` target
- **THEN** the system SHALL use only the parent Region's declared EKS integration
  and SHALL NOT direct the instance to a cluster in another Region

### Requirement: Preferred-Zone sequential expansion
The system SHALL initially request capacity from one approved standard Availability
Zone in the active Region. A Zone that fulfills any target capacity SHALL remain
preferred for subsequent machines. If the target remains short for the configured
per-Zone threshold, the system SHALL add the next approved standard Availability
Zone; only after all approved standard Zones are active SHALL it add eligible Local
Zones one at a time. The initial threshold SHALL be fifteen minutes and MAY be
overridden. Zone expansion MUST NOT change desired or maximum capacity.

#### Scenario: Preferred Zone fulfills the target
- **WHEN** one Zone fulfills the desired machine count before its expansion threshold
- **THEN** the system SHALL keep later standard and Local Zones inactive

#### Scenario: Preferred Zone partially fulfills the target
- **WHEN** a Zone holds target instances but remains below desired capacity beyond
  the expansion threshold
- **THEN** the system SHALL retain that Zone as preferred and add the next approved
  Zone for only the fleet's remaining capacity

#### Scenario: Standard Zones are exhausted
- **WHEN** all approved standard Availability Zones have been activated and the
  target remains short beyond the expansion threshold
- **THEN** the system SHALL add the next eligible approved Local Zone without
  changing desired or maximum capacity

#### Scenario: Local Zone obtains target capacity
- **WHEN** an approved Local Zone launches one or more target instances
- **THEN** the system SHALL retain that Local Zone as an active preferred pool and
  SHALL NOT remove it merely because it is a Local Zone

#### Scenario: Non-capacity error does not advance expansion
- **WHEN** reconciliation cannot establish a valid shortfall because configuration,
  authorization, or a required dependency is invalid
- **THEN** the system SHALL classify the error and SHALL NOT advance the Zone timer

#### Scenario: Local Zone prerequisites are not ready
- **WHEN** a configured Local Zone is not opted in, lacks an approved subnet, or
  does not offer an approved H100 instance type
- **THEN** the system SHALL mark it ineligible, report the reason, and continue
  without changing Local Zone resources

### Requirement: Operator-approved whole-target Region failover
The system SHALL keep each target active in at most one Region. After every approved
Zone in the active Region has been activated and the target remains short for the
configured all-Zones threshold, initially thirty minutes, the system SHALL prepare
a failover plan for another eligible Region and notify the operator. The destination
request MUST equal the full desired machine count and MUST NOT equal only the source
Region shortfall.

The system SHALL also allow an operator to request a versioned whole-target
migration plan for a healthy enabled target. Manual request MUST be plan generation
only and MUST NOT itself stop, terminate, create, or modify EC2 capacity. The
destination MUST be another configured candidate Region with a valid launch
contract and price caps, and no conflicting current plan or execution may exist.

#### Scenario: Failover plan awaits approval
- **WHEN** the active Region meets the all-Zones shortfall threshold
- **THEN** the system SHALL persist and notify a plan containing the target and
  configuration version, source and destination Regions, full desired count,
  source instances to terminate, and approval expiry, without changing AWS capacity

#### Scenario: Operator requests a healthy-target migration plan
- **WHEN** an operator previews and explicitly persists a manual migration request
  for the current target version and a configured destination Region
- **THEN** the system SHALL persist and notify the same exact-source, full-target,
  expiring plan used by automatic failover without making an EC2 capacity write

#### Scenario: Repeated or conflicting manual request
- **WHEN** the same request is repeated or another current plan/execution exists
- **THEN** the system SHALL return the existing identical plan idempotently or
  reject the conflict without replacing its approval boundary

#### Scenario: Failover is not approved
- **WHEN** the operator rejects the plan, approval expires, or target configuration
  changes before approval
- **THEN** the system SHALL invalidate the plan, retain the source fleet, and SHALL
  NOT create destination capacity

#### Scenario: Failover is approved
- **WHEN** the operator supplies a valid approval bound to the current plan
- **THEN** the system SHALL stop the source maintain request, terminate only its
  controller-owned target instances, and wait until no owned source instance remains

#### Scenario: Source Region is empty after approval
- **WHEN** the approved failover has observed zero active controller-owned target
  instances in the source Region
- **THEN** the system SHALL create one destination maintain fleet requesting the
  full desired machine count

#### Scenario: Claimed migration retries after a control-plane interruption
- **WHEN** an unexpired approval was atomically claimed before source cleanup and
  a controller outage or source-drain delay outlasts the original approval window
- **THEN** reconciliation SHALL resume only that exact claimed plan, preserve the
  zero-source barrier, and create at most one destination Fleet for the full
  desired count

#### Scenario: Destination Region also cannot fulfill
- **WHEN** the destination Region later meets the all-Zones shortfall threshold
- **THEN** the system SHALL require a new plan and new operator approval before any
  further Region change

### Requirement: Idempotent reconciliation
The system SHALL identify fleets and launched instances through ownership tags and
stable target identifiers. Repeated reconciliation runs for an unchanged target
MUST NOT create duplicate fleets, repeatedly reactivate the same fallback
transition, or modify resources not owned by the controller.

#### Scenario: Reconciliation is repeated
- **WHEN** two reconciliation runs process the same unchanged enabled target
- **THEN** both runs SHALL resolve to the same controller-owned fleet and only one
  fleet SHALL exist for that target

### Requirement: Controlled target changes
The system SHALL reconcile a changed desired count only by modifying its owned
maintain-type fleet. A reduction in desired count MUST require an explicitly
configured excess-instance termination policy.

#### Scenario: Target grows
- **WHEN** an operator increases the desired count within the configured maximum
- **THEN** the system SHALL update the owned fleet Spot target to the new count

#### Scenario: Target shrinks without termination approval
- **WHEN** an operator lowers a desired count and no excess-instance termination
  policy is enabled
- **THEN** the system SHALL report the configuration as requiring operator action
  and SHALL NOT terminate running instances
