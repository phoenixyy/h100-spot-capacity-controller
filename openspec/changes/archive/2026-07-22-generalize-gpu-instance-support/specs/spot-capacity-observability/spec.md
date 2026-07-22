## MODIFIED Requirements

### Requirement: Reconciliation visibility
The system SHALL emit structured logs and CloudWatch metrics for each reconciliation
attempt, including desired capacity, fulfilled capacity, fleet identifier, outcome,
active Region, per-Zone fulfilled capacity, Zone expansion state, Region failover
state, and an error classification when applicable.

Desired and fulfilled capacity SHALL be reported as machine counts. When instance
metadata is available, the system SHALL additionally report realized GPU
manufacturer, model when available, and count as separate observations that do
not affect reconciliation. It SHALL NOT assign H100-specific semantics to any
generic GPU metric.

#### Scenario: Fleet creation is partially fulfilled
- **WHEN** AWS creates a fleet but cannot fulfill its complete target capacity
- **THEN** the system SHALL record the requested and fulfilled capacity separately
  and expose the shortfall in metrics

#### Scenario: Heterogeneous GPU machines are running
- **WHEN** the fleet contains explicitly approved GPU instance types with
  different GPU models or counts
- **THEN** the system SHALL report machine capacity and per-type realized GPU
  metadata as separate values

#### Scenario: G6e GPU machine is running
- **WHEN** a generic production target fulfills a G6e GPU instance
- **THEN** the system SHALL report its actual L40S GPU metadata without requiring
  a functional-validation profile

#### Scenario: EKS integration readiness is reported separately
- **WHEN** an `existing-eks` target reports the registration or readiness state of
  a controller-owned node
- **THEN** the system SHALL publish that state separately from EC2 fulfilled
  machine capacity and SHALL NOT increase the Fleet target solely because the
  node is not registered or Ready
