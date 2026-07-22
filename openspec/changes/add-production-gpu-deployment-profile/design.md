## Context

The controller already validates arbitrary EC2 GPU types through AWS metadata,
maintains one Spot Fleet in one Region, replenishes interrupted capacity, and
supports optional existing-EKS metadata. The live production-path evidence is
bounded to one standalone G6e machine. AWS metadata cannot establish that a
training or inference workload supports a GPU model, driver/CUDA image, EFA,
shared storage, multi-node topology, or an EKS bootstrap contract.

Production rollout therefore needs a versioned operator-owned contract above
the existing capacity target. It must make every prerequisite and acceptance
gate reviewable without turning the controller into a Kubernetes, network, or
storage provisioner. Planning and read-only preflight must remain safe; every
AWS write and every increase in capacity still requires explicit authorization.

Stakeholders are the workload owner, AWS infrastructure operator, EKS/platform
owner when applicable, and the on-call recipient for capacity events.

## Goals / Non-Goals

**Goals:**

- Represent workload-compatible GPU types, Regions, scale, integration mode,
  infrastructure dependencies, alert routing, release gates, and rollback in a
  single versioned production deployment profile.
- Produce a per-Region read-only readiness matrix before target persistence or
  capacity enablement.
- Prevent multi-machine, EKS, Local Zone, or migration rollout until the exact
  applicable gate has review evidence and an explicit operator approval.
- Preserve the controller's machine-count, same-AZ preference, single-Region,
  ownership, price, and source-zero safety invariants.
- Make a production rollout reversible to a disabled, zero-request state without
  granting implicit authority to terminate existing capacity.

**Non-Goals:**

- Automatically choose workload-compatible GPU models or substitute an unlisted
  type based only on AWS metadata.
- Create or modify VPCs, subnets, routes, Local Zone opt-in, EKS clusters or node
  authorization, EFA setup, FSx/S3 resources, container images, drivers, training
  jobs, inference services, Kubernetes resources, or scheduler add-ons.
- Guarantee Spot fulfillment from SPS, offerings, quota, or price observations.
- Request production GPU capacity as part of this planning change.
- Allow one logical workload to run controller-owned capacity in two Regions.

## Decisions

### 1. Keep the production profile separate from the capacity target

Add a versioned `ProductionDeploymentProfile` document that references one
capacity target identifier and records:

- workload identity, owner, purpose (`training` or `inference`), and profile
  schema version;
- explicit workload-approved EC2 GPU instance types and compatibility notes;
- desired and maximum machine counts plus the initial validation count;
- ordered candidate Regions, standard-AZ and optional Local Zone policy;
- `standalone` or `existing-eks` integration and its Region-specific contracts;
- image/bootstrap, network/EFA, storage/data, encryption, and cleanup contracts;
- notification destination and event-severity policy;
- required rollout gates, acceptance criteria, and rollback procedure.

The existing `CapacityTarget` remains the authoritative Fleet request. A compiler
or validator derives/checks the target against the reviewed production profile;
it does not let the profile bypass target validation.

Alternative considered: add every field directly to `CapacityTarget`. Rejected
because workload and operational evidence would overload the reconciliation
contract and make backward compatibility harder.

### 2. Treat workload compatibility as an operator attestation

`DescribeInstanceTypes` remains authoritative for whether a type physically has
GPUs. The profile must separately attest that every configured type is compatible
with the workload software and memory/performance needs. For multi-machine work,
the attestation also covers networking, topology, and collective-communication
requirements. The controller never infers this from GPU manufacturer/model.

Alternative considered: maintain a built-in workload/GPU compatibility catalog.
Rejected because framework, model, CUDA, driver, and performance compatibility
changes independently of AWS hardware metadata.

### 3. Produce an immutable read-only preflight report

The CLI will evaluate every candidate Region and emit a profile/version-bound
report containing GPU metadata, instance offerings, quota, On-Demand-derived
Spot ceilings, SPS and freshness, approved subnet/Zone mapping, launch-template
contract, Local Zone prerequisites, and optional EKS configuration/readiness.
The report includes classified failures and `aws_write=false`. A digest of the
profile and report is stored only when the operator separately persists review
evidence.

Alternative considered: make successful preflight automatically persist and
enable the target. Rejected because read-only review and AWS writes must remain
separate authorization boundaries.

### 4. Use explicit, profile-bound rollout gates

Applicable gates are:

1. `profile-reviewed`: schema and read-only preflight pass; target remains
   disabled and candidate Regions are empty.
2. `control-plane-observed`: deployed control plane reconciles the disabled
   target for the configured observation window with no capacity write.
3. `single-machine-validated`: one workload-compatible GPU machine launches,
   bootstraps, reports expected GPU metadata/readiness, replenishes after one
   controlled termination, and is cleaned up.
4. `same-az-scale-validated`: required before desired capacity exceeds one;
   verifies the requested machine count, preferred-AZ behavior, and workload
   networking/storage or EKS node readiness.
5. `local-zone-fallback-validated`: required only before Local Zones may be
   activated for production.
6. `region-migration-validated`: required only before production Region failover
   execution is enabled; the existing per-plan approval remains mandatory.
7. `production-approved`: binds the exact profile/target versions, desired/max,
   enabled optional features, approver, and expiry.

Gate evidence is append-only and does not itself make an AWS capacity write.
Changing a relevant profile or target field invalidates downstream approvals.

Alternative considered: one global production approval. Rejected because it
would grant scale, Local Zone, EKS, or migration authority that may never have
been tested.

### 5. Keep orchestration mode explicit

`standalone` remains valid for independent single-machine workloads. A profile
with multi-machine distributed training or managed cluster inference must use
`existing-eks` unless the operator documents another external orchestrator as an
out-of-band dependency in a later change. For `existing-eks`, the profile records
cluster ARN/Region, node bootstrap contract, labels, GPU taint, authorization
owner, workload drain procedure, EFA expectation, and storage/data dependencies.
The controller observes node registration/readiness but never creates Kubernetes
objects or interprets workload scheduling as EC2 fulfillment.

### 6. Make alert policy mandatory for production approval

The profile maps shortfall, interruption/rebalance, reconciliation errors,
stale/no-eligible Region evidence, EKS readiness failure, Local Zone activation,
and migration approval to severity and one controller-owned SNS topic. Email,
chat, paging, or ticket integrations are subscriptions owned outside the
controller. Alerts remain deduplicated and include target/profile versions and
runbook context.

### 7. Roll back by disabling first, cleaning up separately

Rollback first conditionally writes the exact target version as disabled and
observes `aws_write=false`. Fleet cancellation and instance termination require a
separate inventory preview and explicit approval scoped to controller-owned IDs.
The retained profile/evidence provides an audit trail and can be revised for a
new preflight without reusing stale approvals.

## Risks / Trade-offs

- [Profile evidence becomes stale as AWS offerings, quota, SPS, prices, AMIs, or
  EKS state change] → Timestamp every observation, enforce freshness at each
  gate, and rerun preflight before enablement.
- [AWS metadata passes but the workload fails] → Require workload-owner
  compatibility attestation and single-machine workload smoke evidence.
- [One-machine success does not prove distributed behavior] → Require a separate
  same-AZ scale gate before desired capacity can exceed one.
- [Spot scarcity prevents an expensive GPU gate from completing] → Keep the
  target disabled, retain read-only evidence, and do not treat G6e results as
  workload compatibility evidence for another GPU model.
- [Local Zone networking or service availability differs from standard AZs] →
  Keep Local Zones disabled until their dedicated prerequisite/fallback gate.
- [EKS nodes launch but cannot schedule workloads] → Report EC2 fulfillment and
  EKS registration/readiness separately; workload remediation remains operator
  owned.
- [More gates increase operational effort] → Make gates conditional on features;
  standalone one-machine profiles do not require EKS, scale, Local Zone, or
  migration gates.

## Migration Plan

1. Add the profile schema, parser, validator, and disabled example without
   changing existing targets.
2. Add read-only preflight and deterministic report digests; validate against
   fixtures and existing disabled targets.
3. Add append-only gate evidence and approval records in the existing state
   table, with no automatic migration of existing records.
4. Add admission checks for scale, EKS, Local Zone, migration, and production
   enablement. Existing targets without a production profile retain current
   behavior until an operator opts them into production gating.
5. Add alert-policy rendering, dashboard/runbook updates, and tests.
6. For a chosen workload, create a disabled reviewed profile and execute gates
   only under separate deployment/capacity approvals.
7. Roll back by disabling the target; cancel/terminate only after a separate
   owned-resource preview and approval. Code rollback must preserve disabled
   targets and retained evidence.

## Open Questions

- Which workload is first: training or inference, and who owns compatibility
  acceptance?
- Which exact GPU instance types, desired/max counts, and candidate Regions are
  approved for that workload?
- Is the first production deployment standalone or attached to an existing EKS
  cluster, and are EFA and shared storage required?
- Which standard AZs and Local Zones have approved networking and quotas?
- Which SNS topic and external subscriptions receive warning, critical, and
  approval-required events?
- Which gates must expire and be repeated periodically versus only after a
  profile-relevant change?
