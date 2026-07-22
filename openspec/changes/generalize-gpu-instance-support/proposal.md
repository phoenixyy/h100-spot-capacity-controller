## Why

The controller's Spot, Fleet, placement, price, SPS, replacement, and migration
mechanics are already instance-type agnostic, and the full controller path has
been verified with one `g6e.xlarge` L40S Spot instance. Its configuration layer
and documentation still present H100/P5 as the production boundary, preventing
operators from using the same safe control path for other EC2 GPU types such as
H200, L4, L40S, A10G, and RTX PRO.

## What Changes

- Replace the H100 production preset and static P5 allowlist with one generic
  GPU production configuration model.
- Accept only operator-listed EC2 types that AWS metadata verifies as having a
  positive GPU count; reject CPU-only and non-GPU accelerators before any Fleet
  mutation.
- Treat GPU model/count as informative metadata; reconcile only a requested
  number of EC2 machines with an explicit price cap per type.
- Retain the fixed one-machine Tokyo/Seoul G6e configuration only as a bounded
  functional-validation fixture, not as G6e's only supported path.
- Replace H100-specific documentation and metrics language with accelerator-
  neutral terminology.
- **BREAKING**: existing H100 target files must remove the deprecated
  `h100-production` profile and use the generic GPU target schema before being
  persisted or enabled.

## Capabilities

### New Capabilities

- `gpu-instance-validation`: verifies each configured EC2 type is a GPU machine
  and exposes normalized accelerator metadata.

### Modified Capabilities

- `spot-capacity-reconciliation`: changes production target configuration from
  the fixed H100 profile to verified operator-selected GPU types.
- `spot-capacity-observability`: changes accelerator reporting from H100-specific
  fields to generic GPU model/count observations.
- `spot-capacity-safety`: preserves price, scale, ownership, and migration
  guardrails while rejecting non-GPU types and retaining the bounded test fixture.

## Impact

Affected areas include configuration parsing, EC2 instance-type metadata
collection, SPS selection fingerprints, metrics, CLI reports, example targets,
the operator runbook, README, and tests. This planning change makes no AWS
resource deployment or capacity request.
