## ADDED Requirements

### Requirement: Production rollout gates grant narrow authority
Each production rollout action MUST require an explicit operator approval bound
to the exact profile and target versions, gate, Region, desired and maximum
machine counts, GPU allowlist, optional feature scope, and expiry. Approval for a
disabled control-plane observation, one-machine validation, scale validation,
Local Zone fallback, Region migration validation, or production enablement SHALL
authorize only that gate and SHALL NOT authorize another gate or broader scale.

#### Scenario: One-machine validation is approved
- **WHEN** an operator approves the exact `single-machine-validated` execution
  scope
- **THEN** the system MAY request at most one approved GPU machine in one active
  Region and SHALL NOT activate Local Zones, multi-machine scale, or migration

#### Scenario: Approval is replayed for broader capacity
- **WHEN** a gate approval is expired, used, version-mismatched, for another
  Region or GPU allowlist, or requests a larger machine count
- **THEN** the system SHALL reject it without creating, increasing, moving, or
  terminating capacity

### Requirement: Optional production features fail closed
Multi-machine scale, existing-EKS integration, Local Zone activation, and Region
migration execution SHALL remain disabled unless explicitly declared in the
production profile and covered by their current validation evidence and
production approval. Missing, failed, expired, or stale evidence MUST NOT be
treated as permission to proceed.

#### Scenario: Local Zone is configured but not production-approved
- **WHEN** standard AZs remain short but Local Zone fallback evidence or approval
  is missing for the exact profile version
- **THEN** the system SHALL keep the Local Zone inactive and SHALL report the
  blocked production gate without changing desired or maximum capacity

#### Scenario: Region migration validation is absent
- **WHEN** a production target meets Region failover proposal conditions but the
  profile does not authorize migration execution
- **THEN** the system MAY notify and prepare read-only destination evidence but
  SHALL NOT approve, stop, terminate, or create migration capacity

### Requirement: Production cleanup remains ownership-scoped and separate
Disabling or invalidating a production profile SHALL NOT itself cancel a Fleet or
terminate an instance. Cleanup MUST require a fresh inventory preview and an
explicit approval naming only controller-owned target resources. The system SHALL
verify zero owned capacity in every candidate Region after cleanup.

#### Scenario: Approved production cleanup completes
- **WHEN** an operator approves cleanup for the exact previewed Fleet and instance
  identifiers after the target is disabled
- **THEN** the system SHALL affect only those owned resources and SHALL report
  zero owned active capacity across all candidate Regions before declaring
  cleanup complete

#### Scenario: Unowned resource appears in a candidate Region
- **WHEN** cleanup discovery finds a Fleet or instance without the complete target
  ownership contract
- **THEN** the system SHALL leave that resource unchanged and identify it as
  outside the cleanup authorization
