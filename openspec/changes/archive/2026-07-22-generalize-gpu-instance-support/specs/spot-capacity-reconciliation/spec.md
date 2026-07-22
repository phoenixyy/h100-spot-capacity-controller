## MODIFIED Requirements

### Requirement: Explicit capacity target configuration
The system SHALL require an operator-managed capacity-target configuration before
it can request capacity. The configuration MUST name exactly one AWS Region, one
or more explicitly listed and AWS-verified GPU instance types with a Spot price
cap for each type, a desired instance count, one or more primary subnets in
standard Availability Zones, a launch template, and a controller ownership
identifier. It MAY also name explicitly approved backup Local Zones and existing
subnets in those Local Zones, but each backup Local Zone MUST belong to the
target's parent Region. A desired count represents EC2 instances, not individual
GPUs.

Every configured instance type SHALL have a capacity weight of one. The system
MAY report the realized GPU model and count, but SHALL reconcile desired and
maximum capacity only as EC2 machine counts. The system SHALL NOT require an
H100-specific production profile or use a fixed GPU-family allowlist.

#### Scenario: Valid GPU target is accepted
- **WHEN** an operator supplies a configuration containing all required fields,
  AWS-verified GPU types, valid per-type price caps, and a desired count within
  its configured maximum
- **THEN** the system SHALL record the target as eligible for reconciliation

#### Scenario: Multiple GPU machine types are accepted
- **WHEN** an operator explicitly enters multiple AWS-verified GPU instance types
  with valid per-type price caps
- **THEN** the system SHALL treat each launched instance as one fulfilled machine
  regardless of its physical GPU model or count

#### Scenario: Unsafe target is rejected
- **WHEN** a configuration omits its Region, launch template, ownership
  identifier, instance types, primary standard-AZ subnets, or target limit, or
  names a Local Zone outside the target's parent Region
- **THEN** the system SHALL reject it without creating or changing an AWS resource

#### Scenario: Unverified GPU substitution is rejected
- **WHEN** a configured instance type is unknown, lacks verified positive GPU
  metadata, or lacks its own price cap
- **THEN** the system SHALL reject the target and SHALL NOT silently substitute a
  different GPU type
