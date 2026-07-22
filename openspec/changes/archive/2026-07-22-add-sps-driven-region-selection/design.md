## Context

The controller already collects regional Spot Placement Scores (SPS), Spot price observations, instance-type offerings, and Local Zone readiness, but configuration still declares one `active_region` and reconciliation treats that value as an operator decision. CloudWatch receives the observations, yet metrics are not an appropriate transactional input for a capacity-creating controller: they do not provide a versioned evidence bundle, decision expiry, or conditional ownership of a decision.

This change adds a decision layer between signal collection and the existing Fleet reconciler. It must preserve the established invariants: one target uses one parent Region at a time; an occupied Region/AZ is preferred; Local Zones are fallback pools inside their parent Region; a cross-Region move requests the complete target and requires operator approval plus a verified source-zero barrier. SPS is advisory and does not reserve capacity. The implementation remains Lambda, DynamoDB, EventBridge, CloudWatch, Python/boto3, and CDK; it does not introduce EKS as a control-plane dependency.

The AWS EC2 Spot Placement Score Tracker is the closest reusable reference for stable SPS request construction, periodic collection, and metric dimensions. The Global Capacity Orchestrator and multi-Region EKS routing samples inform the separation between recommendation and execution, signal-freshness handling, and anti-flapping behavior, but their EKS/Kueue/Karpenter execution layers are outside this controller's scope.

## Goals / Non-Goals

**Goals:**

- Select the initial parent Region for an empty target from an operator-approved allowlist using fresh SPS and verified regional readiness.
- Offer `manual`, advisory `recommend`, and bounded `auto_initial` modes without changing the safety of an already-running target.
- Make every recommendation and automatic selection deterministic, versioned, expiring, idempotent, and explainable.
- Reuse the existing Fleet reconciliation and approved whole-target migration engines rather than creating a second capacity path.
- Keep Region selection independent of the standard-AZ-to-Local-Zone placement sequence inside the selected Region.
- Permit read-only rollout and validation before automatic selection is enabled.

**Non-Goals:**

- Automatically migrate an occupied target merely because another Region later has a higher SPS.
- Treat SPS as guaranteed capacity or create capacity in more than one Region concurrently.
- Score Local Zones with SPS, synthesize Local Zone scores, or compare a Local Zone directly with a standard Region.
- Deploy EKS clusters, route Kubernetes jobs, or adopt the reference repositories' Kueue, Karpenter, or On-Demand fallback behavior.
- Search every AWS Region automatically; the operator must provide the candidate Region allowlist and a complete launch contract for every candidate.
- Copy an entire external reference implementation or add its deployment stack as a dependency.

## Decisions

### Add three explicit selection modes

`region_selection.mode` supports:

- `manual`: current behavior. `active_region` is required and reconciliation does not consume a selector decision.
- `recommend`: the selector writes a recommendation, but the reconciler does not use it to create or move capacity. If the operator also supplies `active_region`, execution continues with that manual Region.
- `auto_initial`: for a target with no owned Fleet or owned instance in any candidate Region, the reconciler may consume a fresh selector decision. The selected Region becomes runtime state rather than mutable desired configuration.

The first release deliberately excludes `auto_migrate`. Once any owned Fleet or instance exists, state pins the target to that Region. A later better score is published as an advisory difference only. If the current Region exhausts its standard AZ and Local Zone expansion policy, the existing whole-target failover planner remains the only way to change Region and still requires operator approval.

Alternative considered: always select the highest current SPS on every reconciliation. This would flap on advisory score changes, risk duplicate regional capacity, and conflict with the user's requirement to keep a GPU cluster together.

### Make candidate Regions an operator-owned readiness allowlist

Every candidate Region retains its own exact launch-template version, subnets, security groups, instance profile, optional existing-EKS contract, approved standard AZs, and approved Local Zones. Dynamic selection never invents or copies these inputs across Regions. A Region is eligible only if configuration validation and read-only discovery confirm the launch contract, approved instance-type offerings, quota headroom for the full desired machine count, resolvable On-Demand-derived Spot ceilings, and a fresh SPS observation for the exact target request.

An SPS request fingerprint covers accelerator profile, ordered instance types or instance requirements, desired machine count, capacity unit, single-AZ flag, and candidate Region set. Collector calls reuse stable fingerprints and schedules to avoid needlessly consuming AWS SPS configuration combinations. A changed fingerprint invalidates older snapshots and decisions.

Alternative considered: discover all AWS Regions and create missing regional prerequisites. This expands permissions and could select an unreviewed geography or network, so it is rejected.

### Persist decision-grade signal snapshots

The collector writes an immutable logical snapshot for each target configuration version and SPS request fingerprint. The snapshot contains collection time, per-Region SPS status and score, regional readiness checks, price-observation status, quota result, instance-type offerings, and error codes. Large raw AWS responses are not stored; only normalized evidence required to reproduce the decision is retained. A TTL removes historical snapshots after the configured retention period, while CloudWatch retains operational trends.

The default `signal_max_age_minutes` is 20, slightly longer than the existing 15-minute SPS schedule. A Region with a missing, failed, mismatched, or stale SPS observation is ineligible for automatic selection. If no Region is eligible, `auto_initial` waits without creating a Fleet and emits a reasoned `NO_ELIGIBLE_REGION` outcome; the operator can use manual mode if acquisition must proceed while SPS is unavailable.

Alternative considered: read CloudWatch metrics. Metrics may arrive late or be aggregated and cannot atomically bind a decision to a target configuration, so DynamoDB snapshots are used as the control input.

### Use deterministic eligibility followed by lexicographic ranking

The selector first eliminates ineligible Regions and records every rejection reason. It then orders remaining Regions by:

1. higher Region-level SPS for the exact desired count and instance-type set;
2. higher count of approved standard AZs at the best available AZ score, when comparable AZ observations are present;
3. lower freshest observed Spot price as a ratio of the matching On-Demand price ceiling;
4. earlier position in the operator's `candidate_regions` list.

Missing optional AZ or price tie-break data does not replace Region SPS; it sorts behind comparable present data. The selector does not combine unrelated inputs into an opaque weighted score. It records the ordered candidates, normalized comparison keys, winning reason, exclusions, and evidence timestamps.

SPS remains a ranking signal, not a minimum-capacity gate: any fresh returned score is rankable. A future optional minimum score may be proposed after real observations, but is not part of this change.

Alternative considered: a weighted composite score. It is harder to audit, tune, and test, and small weight changes can silently change placement, so deterministic lexicographic ordering is preferred for the first release.

### Persist a conditional, expiring Region decision

A `RegionDecision` record contains target ID, target configuration version, request fingerprint, monotonically increasing decision version, mode, selected Region, ordered candidates, evidence snapshot ID, reason, `created_at`, `expires_at`, and optional `applied_at`. The default `decision_ttl_minutes` is 15.

The selector uses a conditional write so concurrent collector invocations cannot publish conflicting current decisions. Recomputing from the same snapshot is idempotent. A decision is consumable only when all of the following remain true: the target is enabled; mode is `auto_initial`; configuration version and fingerprint match; the decision is unexpired; the destination remains eligible; no owned Fleet or instance exists in any candidate Region; and no failover plan is executing.

The reconciler conditionally marks the decision applied while establishing target runtime state, then uses the existing selected-Region Fleet path. If a Fleet API call fails, subsequent reconciliation retries the same Region decision rather than selecting another Region in parallel. Selection is reconsidered only while the target remains globally empty and the decision expires or its configuration becomes invalid.

Alternative considered: have the SPS collector create the Fleet immediately. Keeping read-only collection and capacity writes in separate handlers preserves least privilege, testability, and the deployment approval boundary.

### Pin occupied targets and reuse approved failover

Before selecting an initial Region, the controller performs owned-resource discovery across every candidate Region. One occupied Region pins runtime state to that Region even if the configured mode changes or another score is higher. Owned resources in multiple Regions produce a safety error and block all capacity writes.

When the active Region cannot satisfy the full desired count after existing per-Zone and all-Zones thresholds, the existing failover planner may use the latest recommendation as destination evidence. It still persists a whole-target plan, notifies the operator, waits for a plan-bound approval, clears and verifies the source Region, and only then requests the full desired count in the destination. Selector output alone never authorizes termination or migration.

### Keep Local Zone selection inside the chosen Region

Region-level SPS compares parent standard Regions only. After a parent Region is selected, the existing placement policy starts with its preferred standard AZ, expands through approved standard AZs, and finally adds eligible Local Zones. Local Zone eligibility continues to depend on explicit operator approval, opt-in state, an approved subnet, instance-type offerings, quota and price evidence. Capacity obtained in a Local Zone pins it like any other occupied Zone.

### Extend observability and operator tooling

Dry-run and CLI output show selection mode, current pinned Region, recommended Region, ordered eligible Regions, exclusions, scores, evidence age, decision version, and expiry. CloudWatch gains decision, signal-age, eligible-candidate-count, recommendation-differs-from-active, and no-eligible-region metrics plus alarms for persistent stale/no-eligible states. Logs include decision identifiers and reasons but not entire launch-template user data or credentials.

Configuration validation remains local and side-effect free. A CLI recommendation command performs read-only discovery and can explain the same deterministic result without writing a decision unless explicitly invoked against the deployed collector workflow.

### Reuse reference code selectively

The implementation may adapt the Spot Placement Score Tracker's stable boto3 request construction, response normalization, and CloudWatch dimension conventions under its MIT-0 license, retaining required attribution for copied portions. The controller keeps its existing configuration, DynamoDB, and CDK layout rather than importing the tracker's S3-configured stack. The two EKS references contribute architectural patterns only; their Kubernetes controllers, job-routing metrics, and On-Demand fallback are not copied into the Fleet execution path.

## Risks / Trade-offs

- [SPS can be high immediately before Fleet capacity becomes unavailable] → Record that SPS is advisory, freeze one decision for its TTL, and rely on the existing persistent maintain Fleet rather than hopping Regions.
- [SPS API configuration limits or temporary service failures prevent a decision] → Reuse stable fingerprints, collect every 15 minutes, publish staleness/errors, wait safely in `auto_initial`, and retain manual mode as an operator-controlled fallback.
- [A partially configured Region wins on score] → Apply launch-contract, offering, quota, price, and freshness eligibility checks before ranking.
- [Concurrent Lambdas select different Regions] → Use conditional DynamoDB writes, configuration versions, decision versions, and globally empty owned-resource discovery before any capacity write.
- [Eventually consistent AWS discovery misses newly created resources] → Recheck all candidate Regions immediately before consuming a decision and fail closed on ambiguity or multiple occupied Regions.
- [Price history is missing for a rare GPU pool] → Treat price only as a tie-breaker and require a resolvable On-Demand-derived ceiling; do not infer capacity from price history.
- [Dynamic selection makes configuration migration confusing] → Keep explicit modes, preserve manual behavior, and roll out through `recommend` before `auto_initial`.
- [More cross-Region read calls increase latency and API usage] → Run SPS on the 15-minute collector schedule, cache normalized snapshots, and keep one-minute reconciliation reads bounded to candidate Regions.

## Migration Plan

1. Add the configuration and data models behind a default `manual` mode so existing targets retain identical behavior.
2. Deploy snapshot storage, selector code, read-only discovery permissions, metrics, and CLI explanation in `recommend` mode only.
3. Observe multiple SPS cycles with no capacity-write behavior change and verify decision determinism, freshness, and AWS error handling.
4. Validate `auto_initial` with a disabled or globally empty functional-validation target in Tokyo and Seoul; deployment and later capacity enablement each require the existing explicit operator authorization gates.
5. Enable one bounded `g6e.xlarge` target and confirm exactly one Region receives one Fleet, including interruption replacement and cleanup tests.
6. Retain current H100 targets in `manual` or `recommend` mode until validation is accepted.

Rollback changes selection mode to `manual`, removes or ignores current selector decisions, and leaves the runtime active Region and owned Fleet untouched. Rolling back must never terminate capacity or clear an active Region. Snapshot and decision records can expire naturally through TTL.

## Open Questions

None required for implementation. A configurable minimum acceptable SPS and automatic post-acquisition migration are explicitly deferred until operational data justifies separate proposals.
