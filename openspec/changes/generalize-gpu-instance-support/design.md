## Context

The existing configuration catalog exposes a static `h100-production` P5/H100
allowlist and a tightly bounded `functional-validation` G6e profile. Yet the
core controller path is generic and has already fulfilled, replaced, migrated,
and cleaned up a real `g6e.xlarge` L40S Spot target. The product should be a GPU
Spot capacity controller rather than an H100-only controller.

The controller must accept approved GPU EC2 types without accepting CPU-only,
FPGA, Inferentia, Trainium, malformed, or unknown types. It must not infer that
different GPU types are suitable to run together for a training workload.

## Goals / Non-Goals

**Goals:**

- Make generic GPU production the only production configuration model.
- Validate each operator-listed instance type with EC2 metadata before use.
- Retain H100, H200, L40S, L4, A10G, and future GPU model/count as metadata,
  not hard-coded product profiles.
- Retain the one-machine Tokyo/Seoul G6e contract as a bounded test fixture.
- Preserve all existing Spot-only, price, scale, ownership, AZ/Local Zone, and
  whole-target migration guardrails.

**Non-Goals:**

- Supporting non-GPU accelerators or CPU-only EC2 instance types.
- Automatically choosing a different GPU model or asserting workload, CUDA,
  driver, EFA, NCCL, architecture, or distributed-training compatibility.
- Creating AWS resources while validating metadata.

## Decisions

### One generic GPU target schema

Production targets will use a generic GPU schema and explicitly list the
instance types that the operator's workload can accept. `h100-production` is
removed; H100 is simply one possible verified GPU model. The target remains
machine-count based and requires a price cap per explicitly listed type.

The `functional-validation` profile remains only as a deliberately constrained
test fixture. It is not required for G6e: a production target may select any
AWS-verified G6e type as it would any other GPU type.

Alternative: retain H100 as a preset alongside generic GPU. Rejected because it
keeps the product's primary interface and documentation falsely centered on one
GPU family without adding safety beyond the explicit instance-type list.

### AWS metadata establishes GPU eligibility

For every configured type, the controller will batch EC2
`DescribeInstanceTypes` calls in each candidate Region. A type is eligible only
when its metadata reports `GpuInfo.Gpus` with a positive count. The controller
normalizes manufacturer, model/name when supplied, and total GPU count for logs,
metrics, reports, and request fingerprints.

Alternative: a static catalog. Rejected because new AWS GPU types would require
code releases and could become stale. A test-only static G6e restriction remains
appropriate because its purpose is bounded live validation rather than product
selection.

### Explicit workload compatibility remains operator-owned

The controller never silently substitutes an unlisted type. A target may list
multiple types only when the operator declares them suitable for the same AMI,
architecture, bootstrap, network, driver, and workload. The controller reports
per-type GPU metadata so a mixed target is visible, but does not claim that a
mixed GPU Fleet is valid for distributed training.

### Accelerator-neutral observability

Machine counts remain the reconciliation units. Metrics and reports expose each
realized GPU model/count without an H100 count dimension or H100-specific
semantic. This ensures L40S, H200, and future GPU models are reported accurately.

## Risks / Trade-offs

- [Metadata unavailable or differs by Region] → mark the type/Region ineligible,
  emit a classified result, and make no new Fleet or migration request.
- [A mixed target is incompatible with a workload] → require explicit operator
  selection and surface per-type metadata; do not auto-substitute types.
- [Legacy H100 target cannot be replayed] → provide an upgrade diagnostic and
  require a reviewed generic GPU target before a new write.
- [Additional EC2 reads add latency] → batch types by Region and cache only
  within a single invocation.

## Migration Plan

1. Release read-only GPU metadata validation, generic examples, and diagnostics.
2. Convert reviewed H100 targets by removing `h100-production` and explicitly
   listing their P5 type(s); dry-run before persisting a new target version.
3. Deploy only after a reviewed change and explicit operator authorization.
4. Roll back by restoring a prior disabled target configuration; never change an
   existing Fleet solely to migrate its metadata representation.

## Open Questions

- Whether a future target schema should support an optional
  `require_homogeneous_gpu_model` guardrail. The initial design leaves workload
  compatibility to the explicit operator type list and does not silently mix.
