## ADDED Requirements

### Requirement: Versioned production workload contract
The system SHALL support a versioned production deployment profile bound to one
capacity target. The profile MUST identify the workload, owner, training or
inference purpose, explicitly workload-approved GPU instance types, desired and
maximum machine counts, candidate Regions and placement policy, integration
mode, image/bootstrap contract, network and storage dependencies, notification
policy, applicable release gates, acceptance criteria, and rollback procedure.
The profile SHALL NOT enable capacity merely by existing.

#### Scenario: Complete production profile is reviewed
- **WHEN** an operator submits a production profile containing every required
  workload, infrastructure, safety, and operational field
- **THEN** the system SHALL produce a stable profile version and SHALL keep its
  capacity target disabled until a separate gate approval authorizes enablement

#### Scenario: Production profile omits a workload dependency
- **WHEN** a profile omits a required image/bootstrap, network, storage, EKS, or
  notification contract applicable to its workload
- **THEN** the system SHALL reject the profile without persisting an enabled
  target or making an AWS capacity write

### Requirement: Workload compatibility is explicitly attested
The system SHALL require the workload owner to explicitly approve every GPU
instance type listed in a production profile. AWS GPU metadata validation SHALL
remain necessary but SHALL NOT be treated as proof of framework, model, CUDA,
driver, memory, EFA, storage, or distributed-workload compatibility.

#### Scenario: AWS reports a valid GPU but workload approval is absent
- **WHEN** `DescribeInstanceTypes` reports positive GPU metadata for a configured
  type but the production profile lacks workload-owner compatibility approval
- **THEN** the system SHALL mark the type ineligible for that production profile
  and SHALL NOT request it

#### Scenario: Multiple compatible GPU types are approved
- **WHEN** the workload owner explicitly approves multiple AWS-verified GPU types
  with their workload constraints and price contracts
- **THEN** the system SHALL retain exactly that allowlist and SHALL NOT infer or
  substitute another GPU type

### Requirement: Read-only production preflight matrix
The system SHALL produce a deterministic read-only preflight report for every
candidate Region in the exact profile version. The report MUST include GPU
metadata, instance offerings, quota, On-Demand-derived Spot ceilings, SPS status
and freshness, subnet/Zone mapping, launch contract, configured Local Zone
prerequisites, integration inputs, and classified failures. It SHALL report
`aws_write=false` and SHALL NOT create or modify a target, Fleet, instance,
network, Local Zone opt-in, EKS resource, storage resource, or notification
subscription.

#### Scenario: All candidate Regions are inspected
- **WHEN** an operator runs production preflight for a disabled profile
- **THEN** the system SHALL return a profile-bound readiness result for every
  configured candidate Region without making an AWS write

#### Scenario: A candidate is not ready
- **WHEN** a candidate fails GPU, offering, quota, price, SPS, launch, placement,
  Local Zone, or integration validation
- **THEN** the report SHALL identify the candidate and classified reason and
  SHALL NOT silently remove or repair the operator-owned prerequisite

### Requirement: Profile-bound staged release evidence
The system SHALL support append-only evidence for `profile-reviewed`,
`control-plane-observed`, `single-machine-validated`, `same-az-scale-validated`,
`local-zone-fallback-validated`, `region-migration-validated`, and
`production-approved` gates. Each record MUST bind the profile and target
versions, applicable scope, observed resources or read-only report digest,
outcome, operator identity, timestamp, and expiry when configured. Evidence
SHALL NOT itself authorize an AWS capacity write.

#### Scenario: Relevant profile input changes
- **WHEN** a GPU allowlist, Region, placement, scale, integration, launch,
  dependency, or notification field changes after gate evidence was recorded
- **THEN** the system SHALL invalidate every downstream approval that depended on
  the prior profile version

#### Scenario: Optional production feature is not enabled
- **WHEN** a profile does not enable multi-machine scale, Local Zones, or Region
  migration execution
- **THEN** the system SHALL NOT require that feature's validation gate for the
  narrower production approval and SHALL continue to prohibit the feature

### Requirement: Disabled-first rollback contract
The production profile SHALL define rollback as a disabled target write followed
by observation that no new capacity request occurs. Fleet cancellation and
instance termination MUST remain separate explicitly approved actions scoped to
the previewed controller-owned resources.

#### Scenario: Production rollback is initiated
- **WHEN** an operator conditionally disables the exact active target version
- **THEN** reconciliation SHALL stop new or increased capacity requests while
  preserving running capacity until separately authorized cleanup

#### Scenario: Cleanup is not separately approved
- **WHEN** a target is disabled but no owned-resource cleanup approval exists
- **THEN** the system SHALL NOT cancel its Fleet or terminate its instances
