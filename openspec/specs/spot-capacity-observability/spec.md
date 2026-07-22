# spot-capacity-observability

## Purpose

TBD — maintained from the completed `add-h100-spot-capacity-controller` change.

## Requirements

### Requirement: Capacity signal collection
The system SHALL periodically collect the configured accelerator fleet's Spot Placement
Score and current Spot price data, and SHALL publish them with Region, target, and
instance-type and accelerator-profile dimensions. Capacity signals SHALL be
advisory and SHALL NOT claim to guarantee fleet fulfillment.

Spot Placement Score SHALL be used only for standard Region and Availability Zone
guidance. For configured backup Local Zones, the system SHALL separately publish
opt-in, approved-subnet, H100 instance-offering, and Spot-price-observation status
without fabricating a Local Zone placement score or claiming current capacity.

#### Scenario: Scheduled signal collection succeeds
- **WHEN** the collection schedule runs for a valid target
- **THEN** the system SHALL publish the score and available price observations with
  the target identifier and observation timestamp

#### Scenario: Capacity signal collection fails
- **WHEN** the Spot Placement Score or price API call fails
- **THEN** the system SHALL publish an error metric and retain the failure context
  in structured logs without disabling the maintain fleet

#### Scenario: Local Zone eligibility is collected
- **WHEN** the collection schedule evaluates an approved backup Local Zone
- **THEN** the system SHALL publish its parent Region, zone identifier, eligibility
  state, and any ineligibility reason separately from standard-AZ SPS data

### Requirement: Reconciliation visibility
The system SHALL emit structured logs and CloudWatch metrics for each reconciliation
attempt, including desired capacity, fulfilled capacity, fleet identifier, outcome,
active Region, per-Zone fulfilled capacity, Zone expansion state, Region failover
state, and an error classification when applicable.

Desired and fulfilled capacity SHALL be reported as machine counts. When instance
metadata is available, the system SHALL additionally report realized accelerator
model and count as separate metrics that do not affect reconciliation. It SHALL
report realized H100 GPU count only for `h100-production`; the value SHALL be zero
for `functional-validation` so an L40S instance is never represented as H100.

#### Scenario: Fleet creation is partially fulfilled
- **WHEN** AWS creates a fleet but cannot fulfill its complete target capacity
- **THEN** the system SHALL record the requested and fulfilled capacity separately
  and expose the shortfall in metrics

#### Scenario: Heterogeneous H100 machines are running
- **WHEN** the fleet contains approved instance types with different H100 GPU counts
- **THEN** the system SHALL report machine capacity and realized GPU count as
  separate values

#### Scenario: L40S functional-validation machine is running
- **WHEN** a `functional-validation` target fulfills one `g6e.xlarge` machine
- **THEN** the system SHALL report one realized L40S accelerator and zero realized
  H100 GPUs while retaining machine capacity of one

#### Scenario: EKS integration readiness is reported separately
- **WHEN** an `existing-eks` target reports the registration or readiness state of
  a controller-owned node
- **THEN** the system SHALL publish that state separately from EC2 fulfilled
  machine capacity and SHALL NOT increase the Fleet target solely because the node
  is not registered or Ready

#### Scenario: Zone expansion activates
- **WHEN** sustained shortfall activates another standard or Local Zone
- **THEN** the system SHALL emit an audit event and metric containing the target,
  threshold, active and preferred Zones, desired capacity, and unchanged maximum

#### Scenario: Region failover plan is created
- **WHEN** all Zones in the active Region remain unable to fulfill the target beyond
  the configured Region threshold or an operator explicitly persists a manual
  whole-target migration request
- **THEN** the system SHALL emit the source and proposed destination Regions, full
  desired count, exact source Fleet and instances affected, plan trigger, plan
  version, expiry, and approval state

### Requirement: Operator notifications
The system SHALL notify the configured operator channel when a target remains below
desired capacity beyond its configured threshold, when an interruption or rebalance
event is observed, or when reconciliation reaches its configured error threshold.

#### Scenario: Prolonged capacity shortfall
- **WHEN** a target remains below desired capacity for longer than its configured
  notification threshold
- **THEN** the system SHALL send one deduplicated shortfall notification containing
  the Region, target identifier, desired count, fulfilled count, and last error

#### Scenario: Region failover requires approval
- **WHEN** a whole-target Region failover plan is ready
- **THEN** the system SHALL send one deduplicated approval-required notification
  that states no failover will execute until the matching approval is supplied
  and, for an `existing-eks` target, identifies source EKS node cleanup and
  workload draining as operator-owned prerequisites

