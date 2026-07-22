## 1. Production profile model

- [ ] 1.1 Add a versioned `ProductionDeploymentProfile` schema and parser for workload identity, GPU compatibility attestations, scale, Regions, integration, dependencies, notifications, gates, and rollback.
- [ ] 1.2 Validate that each profile references one compatible capacity target and cannot weaken its GPU, price, ownership, placement, Region, or maximum-machine constraints.
- [ ] 1.3 Add a disabled production-profile example with placeholders only and migration diagnostics for existing targets that opt into production gating.

## 2. Read-only production preflight

- [ ] 2.1 Implement a per-Region preflight matrix for GPU metadata, offerings, quota, price ceilings, SPS freshness, subnet/Zone mapping, launch contracts, Local Zone prerequisites, and integration inputs.
- [ ] 2.2 Add workload compatibility checks that require explicit operator attestations and never infer compatibility or substitute an unlisted GPU type.
- [ ] 2.3 Add CLI commands to review a profile and render a deterministic report/digest with classified failures and `aws_write=false`.
- [ ] 2.4 Add existing-EKS preflight for cluster Region, bootstrap contract, labels, GPU taint, authorization owner, drain procedure, EFA expectation, and storage dependencies without Kubernetes or EKS writes.

## 3. Gate evidence and admission

- [ ] 3.1 Add append-only, profile/target-version-bound gate evidence and approval records to the existing state table with conditional writes and optional expiry.
- [ ] 3.2 Implement read-only gate review plus separate explicit commands for persisting evidence or approval; no evidence write may enable capacity automatically.
- [ ] 3.3 Invalidate downstream approvals when relevant GPU, Region, placement, scale, integration, launch, dependency, notification, or target fields change.
- [ ] 3.4 Enforce `production-approved` admission before opted-in production reconciliation while preserving existing behavior for targets that have not opted in.
- [ ] 3.5 Enforce single-machine and same-AZ scale bounds, plus conditional Local Zone and Region-migration gates, without weakening existing per-plan failover approval.

## 4. Production observability and rollback

- [ ] 4.1 Add profile-defined severity and SNS routing for shortfall, interruption, reconciliation error, stale/no-eligible Region, EKS readiness, Local Zone activation, and migration approval events.
- [ ] 4.2 Expose profile version, gate state/age, approved scale/features, approver, expiry, and invalidation reason in CLI output, structured logs, metrics, alarms, and dashboard widgets.
- [ ] 4.3 Report EC2 fulfillment, GPU metadata, bootstrap, optional EKS readiness, and workload readiness as separate conditions that do not inflate Fleet demand.
- [ ] 4.4 Implement disabled-first rollback review and an ownership-scoped cleanup preview that requires separate approval and verifies zero active target capacity in every candidate Region.

## 5. Verification and operator delivery

- [ ] 5.1 Add unit tests for profile parsing, compatibility attestation, preflight classifications, deterministic digests, gate versioning/expiry/invalidation, and backward compatibility.
- [ ] 5.2 Add tests proving scale, EKS, Local Zone, migration, production enablement, and cleanup remain fail-closed without their exact approvals.
- [ ] 5.3 Add infrastructure tests for least-privilege read/write permissions, notification resources, dashboard signals, and the `Phoenix-Codex-Local-Spot-` naming contract.
- [ ] 5.4 Update README, configuration documentation, deployment checklist, and operator runbook with the staged production gate flow and explicit external prerequisites.
- [ ] 5.5 Run the complete unit suite and strict OpenSpec validation; produce a read-only production preflight report only after the operator supplies the first workload's GPU, Region, scale, integration, and notification choices.
