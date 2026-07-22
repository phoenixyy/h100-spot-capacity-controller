## ADDED Requirements

### Requirement: Explicit Region-selection modes
The system SHALL support `manual`, `recommend`, and `auto_initial` Region-selection modes. Manual mode SHALL retain the existing operator-selected `active_region` behavior, recommend mode SHALL remain advisory, and auto-initial mode SHALL permit selector output to establish runtime active Region only for a globally empty target.

#### Scenario: Manual mode preserves current behavior
- **WHEN** an enabled target uses `manual` mode with a valid `active_region`
- **THEN** the reconciler SHALL use that Region and SHALL NOT require a Region decision

#### Scenario: Recommend mode does not create from recommendation
- **WHEN** the selector recommends a Region for a target in `recommend` mode
- **THEN** the system SHALL expose the recommendation but SHALL NOT use it alone to create, modify, or terminate capacity

#### Scenario: Auto-initial mode establishes runtime Region
- **WHEN** an enabled auto-initial target has a fresh valid decision and no owned Fleet or instance in any candidate Region
- **THEN** the reconciler SHALL establish the decision's selected Region as runtime active Region before requesting capacity there

### Requirement: Operator-approved candidate Region allowlist
The system SHALL evaluate only uniquely configured candidate Regions, and every candidate SHALL provide the complete Region-specific launch, network, price-cap, and optional existing-EKS contract required by the target. The system MUST NOT discover an unconfigured Region as an automatic destination or synthesize missing regional prerequisites.

#### Scenario: Unconfigured Region has a higher score
- **WHEN** AWS returns a high SPS for a Region that is not in the target's candidate allowlist
- **THEN** the selector SHALL ignore that Region

#### Scenario: Candidate lacks a launch contract
- **WHEN** a candidate Region lacks required launch-template, subnet, security, instance-profile, or integration inputs
- **THEN** configuration validation SHALL reject the target before Region selection or capacity writes

### Requirement: Comparable decision-grade SPS collection
The collector SHALL request and normalize SPS using the target's exact desired machine count, approved instance-type set or requirements, capacity unit, Region set, and single-AZ mode. It SHALL bind each observation to a stable request fingerprint and target configuration version.

#### Scenario: Target capacity changes
- **WHEN** the desired machine count changes after an SPS snapshot was collected
- **THEN** the old snapshot and every decision derived from it SHALL be invalid for automatic selection

#### Scenario: Stable configuration is recollected
- **WHEN** the collector refreshes SPS without a relevant target configuration change
- **THEN** it SHALL reuse the same request fingerprint and SHALL NOT manufacture a new SPS configuration combination

### Requirement: Regional eligibility precedes ranking
The selector SHALL exclude a Region before ranking unless its launch contract validates, approved instance types are offered, quota supports the full desired machine count, On-Demand-derived Spot ceilings resolve, and its SPS observation is successful, fingerprint-matched, and fresh. Every exclusion SHALL have a machine-readable and operator-visible reason.

#### Scenario: Highest-scoring Region is not launch-ready
- **WHEN** the highest-scoring candidate fails a required launch-readiness check and a lower-scoring candidate passes all checks
- **THEN** the selector SHALL exclude the first Region and rank the eligible candidate

#### Scenario: All candidates are ineligible
- **WHEN** no candidate Region passes all eligibility checks
- **THEN** auto-initial reconciliation SHALL create no Fleet or instance and SHALL publish `NO_ELIGIBLE_REGION` with the exclusion reasons

### Requirement: Freshness and unavailable-score safety
The system SHALL apply a configurable maximum signal age with a default of 20 minutes. A missing, errored, expired, or request-mismatched SPS observation MUST NOT authorize auto-initial selection; manual execution SHALL remain available according to its existing safety checks.

#### Scenario: SPS collection is temporarily unavailable
- **WHEN** all candidate SPS observations are missing, failed, or older than the maximum signal age
- **THEN** an auto-initial target SHALL wait without a capacity write and SHALL emit signal-staleness or collection-error observability

#### Scenario: One candidate has a fresh SPS
- **WHEN** exactly one otherwise eligible candidate has a fresh fingerprint-matched SPS observation
- **THEN** the selector SHALL rank that candidate and SHALL record why the other candidates were excluded

### Requirement: Deterministic auditable Region ranking
The selector SHALL rank eligible Regions lexicographically by higher Region SPS, then stronger comparable standard-AZ SPS coverage, then lower fresh Spot-price-to-On-Demand-ceiling ratio, then configured candidate order. It SHALL record the comparison keys, ordered candidates, winning reason, and evidence timestamps and MUST NOT use an undocumented weighted score.

#### Scenario: Region scores differ
- **WHEN** two eligible Regions have different Region-level SPS values
- **THEN** the selector SHALL rank the higher SPS Region first regardless of later tie-breakers

#### Scenario: All observed ranking inputs tie
- **WHEN** eligible Regions tie on Region SPS, comparable AZ coverage, and comparable price ratio
- **THEN** the selector SHALL choose the Region appearing first in the configured candidate order

#### Scenario: The best score is low
- **WHEN** all eligible Regions have low but valid fresh SPS values
- **THEN** the selector SHALL still rank the best eligible Region because SPS is a ranking signal rather than a hard minimum gate

### Requirement: Versioned expiring Region decisions
The system SHALL persist Region decisions containing target and configuration versions, SPS request fingerprint, decision version, selection mode, selected and ordered Regions, evidence reference, reason, creation time, expiry, and application state. The default decision validity SHALL be 15 minutes, and conflicting current decisions SHALL be prevented with conditional writes.

#### Scenario: Concurrent selectors process one snapshot
- **WHEN** concurrent invocations compute a decision for the same target version and snapshot
- **THEN** the stored current decision SHALL be identical and at most one conditional publication SHALL succeed

#### Scenario: Decision expires before reconciliation
- **WHEN** an auto-initial decision reaches its expiry before it is applied
- **THEN** the reconciler SHALL reject it and wait for a fresh decision without creating capacity

#### Scenario: Configuration changes after decision
- **WHEN** target configuration version or request fingerprint no longer matches a decision
- **THEN** the reconciler SHALL invalidate that decision and SHALL NOT apply it

### Requirement: Globally empty target precondition
Before applying an auto-initial decision, the system SHALL discover owned Fleets and instances in every configured candidate Region. It SHALL apply the decision only when all candidate Regions are empty, pin a singly occupied Region, and block on evidence of owned capacity in multiple Regions.

#### Scenario: Existing capacity is found in one Region
- **WHEN** owned capacity exists in exactly one candidate Region
- **THEN** the controller SHALL pin runtime state to that Region and SHALL NOT apply a different automatic recommendation

#### Scenario: Existing capacity is found in multiple Regions
- **WHEN** owned Fleets or instances for one target exist in more than one candidate Region
- **THEN** the controller SHALL emit a safety error and SHALL perform no capacity-creating, modifying, or terminating action

### Requirement: Atomic decision handoff to reconciliation
The reconciler SHALL consume only an enabled target's fresh, configuration-matched, eligibility-valid auto-initial decision when no failover plan is executing. It SHALL conditionally establish runtime Region and mark the decision applied before using the existing Fleet acquisition path. A failed Fleet API operation SHALL retry the same pinned Region rather than select another Region concurrently.

#### Scenario: Fleet creation fails after decision application
- **WHEN** the selected Region is pinned but the first Fleet API call fails retryably
- **THEN** later reconciliation SHALL retry in the same Region while the target remains pinned and SHALL NOT request capacity in another Region

#### Scenario: Failover execution is pending
- **WHEN** an approved or executing whole-target failover plan exists
- **THEN** auto-initial selection SHALL be blocked until failover state is resolved

### Requirement: No automatic migration of occupied capacity
A new or higher later SPS result MUST NOT automatically move, terminate, or duplicate an occupied target. A post-acquisition Region change SHALL use the existing whole-target migration plan, operator approval, source-zero verification, and full desired destination count.

#### Scenario: Another Region becomes higher-scoring
- **WHEN** a target has an owned Fleet or instance and a different Region later receives a higher SPS
- **THEN** the system SHALL publish the recommendation difference but SHALL leave current capacity and runtime Region unchanged

#### Scenario: Active Region exhausts all placements
- **WHEN** the active Region satisfies the existing conditions for proposing cross-Region failover
- **THEN** the latest eligible recommendation MAY inform the proposed destination but SHALL NOT bypass operator approval or the source-zero barrier

### Requirement: Local Zones remain within-Region fallback
The selector SHALL compare parent standard Regions and MUST NOT assign an SPS-derived score to a Local Zone. After a Region is selected, placement SHALL continue through the existing preferred standard AZ, additional standard AZ, and approved eligible Local Zone sequence without changing desired or maximum machine count.

#### Scenario: Selected Region has an eligible Local Zone
- **WHEN** standard AZ expansion in the selected Region remains short and its configured Local Zone is eligible
- **THEN** the existing placement policy SHALL add that Local Zone as an in-Region fallback rather than compare it with another parent Region's SPS

#### Scenario: Local Zone obtains capacity
- **WHEN** an owned target instance is fulfilled in a Local Zone
- **THEN** that capacity SHALL pin the target to the Local Zone's parent Region like capacity in a standard AZ

### Requirement: Region-selection observability and explanation
The system SHALL expose selection mode, pinned Region, recommended Region, ordered eligible candidates, excluded candidates and reasons, SPS values, evidence age, request fingerprint, decision version, expiry, and recommendation-versus-active status through structured logs and operator tooling. It SHALL publish metrics and alarms for persistent stale evidence and no eligible Region.

#### Scenario: Operator performs a read-only dry-run
- **WHEN** an operator requests Region-selection explanation without authorizing deployment
- **THEN** the output SHALL show the deterministic result and evidence without creating or modifying a Fleet, instance, target decision, or AWS prerequisite

#### Scenario: Recommendation differs from occupied Region
- **WHEN** the latest recommendation differs from the pinned active Region
- **THEN** the system SHALL emit an advisory metric and explanation without initiating migration

### Requirement: Backward-compatible and reversible rollout
Existing targets SHALL default to manual behavior unless an operator explicitly configures another mode. Disabling dynamic selection or rolling back this feature MUST NOT terminate, move, or duplicate an existing Fleet or instance.

#### Scenario: Existing target has no new policy block
- **WHEN** a previously valid target is loaded without `region_selection`
- **THEN** the system SHALL interpret it as manual mode and preserve its configured active Region

#### Scenario: Auto-initial target is changed to manual after fulfillment
- **WHEN** an occupied auto-initial target is changed to manual mode with the same runtime active Region
- **THEN** reconciliation SHALL continue managing the same owned Fleet without recreation or termination
