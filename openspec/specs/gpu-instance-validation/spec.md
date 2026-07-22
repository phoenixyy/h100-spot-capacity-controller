# gpu-instance-validation

## Purpose

Define the authoritative AWS metadata checks that make explicitly configured EC2
instance types eligible for generic GPU Spot capacity targets.

## Requirements

### Requirement: AWS-verified GPU instance types
The system SHALL accept explicitly configured EC2 GPU instance types for a
production capacity target only after EC2 `DescribeInstanceTypes` metadata
reports one or more GPUs with a positive total count. The system SHALL normalize
and expose the reported GPU manufacturer, model when available, and count per
machine. It SHALL reject CPU-only, non-GPU, FPGA, Inferentia, Trainium, unknown,
malformed, or unavailable types before creating or modifying an EC2 Fleet.

#### Scenario: Configured GPU type is verified
- **WHEN** an operator configures an EC2 instance type whose metadata reports one
  or more GPUs
- **THEN** the system SHALL accept the type with normalized accelerator metadata
  and machine weight one

#### Scenario: Configured type is not a GPU machine
- **WHEN** an operator configures a type whose EC2 metadata has no positive GPU
  count
- **THEN** the system SHALL reject the target and SHALL NOT create or modify a
  Fleet

#### Scenario: Metadata cannot be verified in a candidate Region
- **WHEN** the EC2 metadata call for a configured type fails or returns
  inconsistent metadata in a candidate Region
- **THEN** the system SHALL mark that Region/type ineligible, emit a classified
  validation result, and SHALL NOT use it for a new capacity request

### Requirement: Bounded G6e functional validation remains isolated
The system SHALL retain the existing `functional-validation` profile for its
one-machine `g6e.xlarge` L40S Tokyo/Seoul test contract. This profile SHALL be a
test fixture only and SHALL NOT be required to configure G6e or any other GPU
type for production use.

#### Scenario: G6e is configured for generic production use
- **WHEN** an operator configures an AWS-verified G6e GPU instance type in a
  production target with valid launch and price contracts
- **THEN** the system SHALL evaluate it through the generic GPU path rather than
  requiring the functional-validation profile
