## MODIFIED Requirements

### Requirement: Spending and scale guardrails
The system SHALL require a maximum desired instance count and a maximum acceptable
Spot price per instance-hour for every approved instance type before creating a
fleet. It SHALL not set any On-Demand target capacity and SHALL reject a target
exceeding any configured limit.

The default cap source SHALL be the uniquely resolved, positive AWS Linux
On-Demand `RunInstances` hourly price for shared tenancy, used capacity, and no
preinstalled software in the target Region and instance type. The resolver SHALL
inspect every returned Pricing API page, ignore Capacity Block, reservation,
non-On-Demand, non-hourly, non-USD, zero, and negative price dimensions, and fail
closed when no valid price or more than one distinct valid price remains. Repeated
identical valid prices SHALL resolve to that price. This value is a Spot ceiling
only; the system SHALL NOT request On-Demand capacity.

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

#### Scenario: Pricing response contains unrelated zero-price products
- **WHEN** a zero-price Capacity Block or reservation product is returned before a
  matching positive Linux On-Demand `RunInstances` hourly price
- **THEN** the system SHALL ignore the unrelated product and use the matching
  positive hourly price as the Spot ceiling

#### Scenario: Matching price appears on a later page
- **WHEN** the Pricing API returns the matching positive hourly price after a
  pagination token
- **THEN** the system SHALL inspect the later page before resolving the Spot ceiling

#### Scenario: Valid price is absent or ambiguous
- **WHEN** no matching positive hourly price exists or multiple distinct matching
  positive hourly prices remain
- **THEN** the system SHALL fail closed without making an EC2 Fleet API call

#### Scenario: Matching price is duplicated
- **WHEN** multiple matching products contain the same positive hourly price
- **THEN** the system SHALL deterministically resolve that shared value
