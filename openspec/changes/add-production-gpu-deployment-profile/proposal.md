## Why

The controller's generic GPU acquisition path is proven with one standalone
G6e machine, but production use still depends on manually assembling workload,
Region, scale, EKS, network, storage, and notification decisions. A reviewed
production profile and staged release contract are needed before expensive or
multi-machine GPU capacity can be enabled safely.

## What Changes

- Add a declarative production deployment profile that records workload purpose,
  approved GPU types, desired/maximum machine counts, candidate Regions and
  placements, integration mode, workload dependencies, and alert destinations.
- Add read-only preflight output that evaluates GPU metadata, offerings, quota,
  price ceilings, SPS evidence, launch contracts, EKS integration inputs, and
  Local Zone prerequisites for every configured candidate Region.
- Add explicit deployment gates for disabled review, one-machine validation,
  same-AZ scale validation, Local Zone fallback validation, and approved
  whole-target Region migration validation.
- Require workload compatibility to remain operator-owned: AWS GPU metadata
  proves that a machine has GPUs but does not prove framework, model, EFA,
  storage, or distributed-training compatibility.
- Define production alert routing and acceptance evidence for shortfall,
  interruption, stale/no-eligible Region signals, EKS readiness, and migration
  approval events.
- Keep this change planning-only until a later explicit implementation request;
  creating these artifacts does not authorize deployment or GPU capacity.

## Capabilities

### New Capabilities

- `production-deployment-profile`: Defines the reviewed production workload
  contract, candidate infrastructure inputs, staged release gates, evidence,
  and rollback expectations.

### Modified Capabilities

- `spot-capacity-reconciliation`: Adds production admission and staged
  same-AZ/multi-machine readiness requirements while preserving machine-count
  reconciliation and single-Region ownership.
- `spot-capacity-observability`: Adds production notification routing,
  acceptance evidence, and workload-readiness separation.
- `spot-capacity-safety`: Adds explicit authorization boundaries for each
  production rollout gate and fail-closed rollback behavior.

## Impact

The change will affect target configuration, CLI review/dry-run output,
deployment templates, validation evidence, CloudWatch/SNS configuration,
operator documentation, and tests. Existing disabled or functional-validation
targets remain backward compatible. Existing EKS clusters, networking, Local
Zone opt-in, storage, workload manifests, and GPU software images remain
operator-owned prerequisites; the controller will not create or modify them.
