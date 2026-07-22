## MODIFIED Requirements

### Requirement: Validation profile cannot weaken production guardrails
The system SHALL apply the same price, scale, ownership, integration-mode,
Region/Zone, dry-run, explicit target-write, and capacity-enable guardrails to
every generic GPU production target. It MUST reject configured types that lack
AWS-verified positive GPU metadata. That metadata SHALL NOT weaken any price,
scale, ownership, or whole-target migration guardrail. The bounded
`functional-validation` profile SHALL retain its separate test-only identity and
constraints.

#### Scenario: CPU-only type is entered as GPU production capacity
- **WHEN** an operator configures a CPU-only or non-GPU EC2 type in a production
  target
- **THEN** the system SHALL reject it without making an AWS resource change

#### Scenario: Different GPU type is not silently substituted
- **WHEN** configured GPU capacity is unavailable in an approved placement
- **THEN** the system SHALL retain the configured instance-type allowlist and
  SHALL NOT request an unlisted GPU type

#### Scenario: Validation capacity is explicitly enabled
- **WHEN** a valid disabled Tokyo/Seoul validation target has passed dry-run and
  the operator separately acknowledges capacity enablement
- **THEN** reconciliation MAY request at most one `g6e.xlarge` Spot machine in
  only its active Region
