## 1. Configuration and Domain Models

- [x] 1.1 Add backward-compatible `region_selection` parsing and validation for `manual`, `recommend`, and `auto_initial`, including signal-age and decision-TTL defaults.
- [x] 1.2 Make `active_region` mode-aware while preserving implicit manual behavior for every existing target configuration.
- [x] 1.3 Add normalized regional-readiness, SPS request fingerprint, signal snapshot, ranking explanation, and Region decision domain models.
- [x] 1.4 Update target examples and validation fixtures for manual, recommend, and auto-initial Tokyo/Seoul configurations.

## 2. Decision-Grade Signal Collection

- [x] 2.1 Refactor SPS request construction into a stable fingerprinted request shared by collector, dry-run, and selector tests.
- [x] 2.2 Normalize per-Region SPS status, score, collection time, request fingerprint, and machine-readable failure reason.
- [x] 2.3 Collect and normalize regional launch-contract readiness, instance-type offerings, quota headroom for full desired count, price-cap resolution, and price tie-break evidence without mutating AWS resources.
- [x] 2.4 Persist versioned signal snapshots with timestamps, retention TTL, configuration version, fingerprint, and conditional/idempotent write behavior.
- [x] 2.5 Add tests for partial SPS results, API configuration-limit errors, stale results, fingerprint changes, quota shortfall, missing price evidence, and malformed AWS responses.

## 3. Region Selector and Decision Store

- [x] 3.1 Implement hard eligibility filtering with complete exclusion-reason reporting.
- [x] 3.2 Implement deterministic lexicographic ranking by Region SPS, comparable standard-AZ coverage, price ratio, and configured Region order.
- [x] 3.3 Implement the Region decision repository with decision versions, evidence references, expiry, conditional publication, application state, and invalidation.
- [x] 3.4 Add selector behavior for no eligible Region, low-but-valid scores, missing optional tie-break data, and idempotent recomputation.
- [x] 3.5 Add unit and property-oriented tests proving stable ordering, deterministic ties, freshness enforcement, concurrent-write handling, and configuration-version isolation.

## 4. Safe Reconciliation Handoff

- [x] 4.1 Add owned Fleet and instance discovery across all configured candidate Regions before applying an auto-initial decision.
- [x] 4.2 Pin a singly occupied Region, fail closed on multiple occupied Regions, and preserve the currently occupied Region across later score changes.
- [x] 4.3 Conditionally apply a fresh auto-initial decision to runtime state before invoking the existing selected-Region Fleet reconciler.
- [x] 4.4 Ensure retryable Fleet errors retry the pinned Region and cannot cause a second Region to receive a concurrent request.
- [x] 4.5 Block selector application while failover approval or execution is pending and connect later recommendations only to the existing whole-target failover proposal path.
- [x] 4.6 Add integration tests for globally empty selection, occupied-region pinning, multiple-region safety failure, expired decisions, Fleet retry, and recommendation changes after fulfillment.

## 5. Placement and Local Zone Invariants

- [x] 5.1 Keep Region-level SPS selection limited to parent standard Regions and reject any attempt to assign SPS-derived ranking to Local Zones.
- [x] 5.2 Verify the selected Region still follows preferred standard AZ, additional standard AZ, and eligible Local Zone expansion without changing desired or maximum machine count.
- [x] 5.3 Add regression tests proving Local Zone capacity pins its parent Region and cannot form a cross-Region target.

## 6. Operator Experience and Observability

- [x] 6.1 Extend read-only dry-run/CLI explanation with mode, pinned and recommended Regions, ordered candidates, exclusions, SPS evidence age, fingerprint, decision version, and expiry.
- [x] 6.2 Add structured decision logs and CloudWatch metrics for eligible candidate count, evidence age, no eligible Region, decision result, and recommendation differing from active Region.
- [x] 6.3 Extend alarms and dashboard panels for persistent SPS staleness and persistent no-eligible-region outcomes.
- [x] 6.4 Update runbooks to distinguish initial automatic selection, advisory re-recommendation, manual override, and operator-approved whole-target migration.

## 7. Infrastructure and Verification

- [x] 7.1 Extend DynamoDB keys/index usage, Lambda environment, least-privilege IAM, EventBridge wiring, and CDK outputs for snapshots and decisions without adding EKS or capacity permissions to the collector.
- [x] 7.2 Run formatting, static checks, unit tests, integration tests, CDK synthesis, and security/permission regression tests.
- [x] 7.3 Exercise at least three read-only `recommend` collection cycles against operator-approved Tokyo and Seoul validation inputs and verify deterministic evidence without Fleet or instance mutation.
- [x] 7.4 After separate explicit deployment and capacity authorization, validate `auto_initial` with the bounded one-machine `g6e.xlarge` target and prove exactly one Region owns the Fleet and instance.
- [x] 7.5 Validate interruption replacement remains in the pinned Region, then perform approved cleanup and confirm no owned Fleets or instances remain in any candidate Region.
- [x] 7.6 Document attribution for any copied MIT-0 Spot Placement Score Tracker portions and record final validation evidence before marking the change complete.
