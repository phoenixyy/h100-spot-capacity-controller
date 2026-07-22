## ADDED Requirements

### Requirement: Production notification policy and routing
Every production-approved profile SHALL map prolonged shortfall,
interruption/rebalance, reconciliation error, stale or no-eligible Region
evidence, EKS readiness failure when applicable, Local Zone activation, and
migration approval events to an operator-defined severity and controller-owned
SNS topic. Notifications MUST include target and profile versions, active Region,
machine counts, classification, evidence timestamp, and runbook context, and
SHALL remain deduplicated by event scope.

#### Scenario: Critical production shortfall persists
- **WHEN** fulfilled production capacity remains below desired capacity beyond
  the profile's critical threshold
- **THEN** the system SHALL send one deduplicated critical notification to the
  approved topic with the target/profile identity and current capacity evidence

#### Scenario: External paging or chat delivery is desired
- **WHEN** an operator wants email, chat, paging, or ticket delivery from the
  production topic
- **THEN** the controller SHALL expose the SNS event contract while creation and
  ownership of external subscriptions remain outside the controller

### Requirement: Production gate evidence is observable
The system SHALL expose the current production profile version, applicable
gates, pass/fail/pending state, evidence digest and age, approved scale and
features, approver identity, approval expiry, and invalidation reason through
read-only operator tooling and structured audit logs. Gate visibility SHALL NOT
be combined with an implicit capacity-enable action.

#### Scenario: Operator reviews production readiness
- **WHEN** an operator requests a readiness review for a disabled profile
- **THEN** the system SHALL show every applicable gate and missing or stale item
  with `aws_write=false`

#### Scenario: Profile change invalidates approval
- **WHEN** a relevant profile change invalidates downstream gate evidence
- **THEN** the system SHALL emit an audit event naming the old and new profile
  versions and SHALL suppress production admission until re-approved

### Requirement: Workload readiness is reported separately
Production observability SHALL distinguish EC2 machine fulfillment, GPU metadata,
bootstrap completion, optional EKS registration/readiness, and workload smoke or
service readiness. Only EC2 Fleet instances SHALL count toward desired machine
capacity.

#### Scenario: Machine is fulfilled but workload smoke fails
- **WHEN** an owned GPU instance is running and counted by the Fleet but the
  operator-owned workload smoke check fails
- **THEN** the system SHALL report fulfilled machine capacity and failed workload
  readiness as separate conditions without automatically requesting another
  machine
