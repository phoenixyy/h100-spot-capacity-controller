## ADDED Requirements

### Requirement: Production profile admission precedes capacity reconciliation
An opted-in production target SHALL be eligible for capacity reconciliation only
when its exact production profile version has passed read-only preflight and has
a current `production-approved` record covering its GPU allowlist, Regions,
integration mode, desired and maximum machine counts, and optional features. The
existing target configuration SHALL remain the authoritative Fleet request and
MUST satisfy every existing validation rule.

#### Scenario: Approved profile and target versions match
- **WHEN** an enabled production target matches a current production approval and
  all existing target, GPU, price, ownership, placement, and Region checks pass
- **THEN** the reconciler MAY use the existing maintain-Fleet path within the
  approved profile scope

#### Scenario: Profile approval is stale or mismatched
- **WHEN** an enabled target differs from its approved profile or target version
- **THEN** reconciliation SHALL make no capacity-creating or capacity-increasing
  write and SHALL report a production admission error

### Requirement: Multi-machine production scale requires same-AZ evidence
Before an opted-in production target may set desired capacity above one, the
system SHALL require current `single-machine-validated` and
`same-az-scale-validated` evidence for the workload, GPU allowlist, integration
mode, and approved scale bound. Reconciliation SHALL continue to prefer an AZ
that already holds target capacity and SHALL preserve the configured maximum.

#### Scenario: Desired capacity increases above one after scale validation
- **WHEN** the exact profile has same-AZ scale evidence covering the requested
  desired count and every other production gate is current
- **THEN** the reconciler MAY increase its owned Fleet to that machine count using
  the existing preferred-Zone expansion behavior

#### Scenario: Desired capacity exceeds validated scale
- **WHEN** a target requests more than one machine or exceeds the machine count
  covered by current same-AZ scale evidence
- **THEN** the system SHALL reject the increase without modifying the Fleet

### Requirement: Production integration readiness remains separate from capacity
For a production `existing-eks` target, the system SHALL validate the declared
cluster, bootstrap, labels, taint, authorization owner, EFA expectation, storage
dependencies, and drain procedure for each candidate Region. EC2 fulfilled
machine count SHALL remain independent from EKS registration, node readiness,
and workload scheduling. A standalone production profile SHALL NOT imply
multi-machine orchestration.

#### Scenario: EKS node is not ready after EC2 fulfillment
- **WHEN** an owned production machine is running but its expected EKS node is not
  registered or Ready within the profile threshold
- **THEN** the system SHALL retain the EC2 fulfilled count, emit an integration
  readiness failure, and SHALL NOT increase Fleet capacity solely for that reason

#### Scenario: Multi-machine workload declares standalone without orchestrator
- **WHEN** a production profile requests distributed multi-machine behavior in
  standalone mode without an approved external orchestration contract
- **THEN** the system SHALL reject production admission before a Fleet write
