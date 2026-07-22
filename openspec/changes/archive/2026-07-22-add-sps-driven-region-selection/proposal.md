## Why

Capacity targets currently require an operator to choose `active_region` before acquisition starts, even though the controller already collects Spot Placement Scores for candidate Regions. The controller should use fresh, comparable SPS evidence to choose the best eligible Region for a new, empty target while preserving the existing single-Region cluster and operator-approved migration safety rules.

## What Changes

- Add `manual`, `recommend`, and `auto_initial` Region-selection modes over an operator-approved candidate Region allowlist.
- Persist versioned, expiring SPS and regional-readiness snapshots instead of using CloudWatch metrics as a decision source.
- Add a deterministic Region selector that applies hard eligibility checks before ranking Regions by SPS and auditable tie-breakers.
- Persist a Region decision, its evidence, reason, expiry, and configuration fingerprint before the reconciler may create capacity.
- Treat the dynamically selected Region as runtime state in `auto_initial` mode while retaining explicit `active_region` behavior in `manual` mode.
- Pin a target to its current Region once a Fleet or owned instance exists; a better later SPS result may recommend migration but must not cause automatic cross-Region movement.
- Route any post-acquisition Region change through the existing whole-target migration plan, operator approval, and source-zero barrier.
- Keep standard AZ placement first and Local Zone fallback inside the selected parent Region; Local Zones are not ranked by Region-level SPS.
- Add decision freshness, idempotency, observability, dry-run output, and test coverage without requiring EKS.

## Capabilities

### New Capabilities

- `sps-region-selection`: Collects decision-grade regional signals, determines and records an initial Region recommendation or automatic selection, and safely hands the selected Region to capacity reconciliation.

### Modified Capabilities

None. The existing capacity-reconciliation, safety, and observability capabilities are still part of the active foundational change and are integrated through the new capability without weakening their requirements.

## Impact

- Configuration schema and examples gain Region-selection policy fields; `active_region` becomes optional outside manual mode.
- DynamoDB state gains regional signal snapshots and Region decision records with conditional-write/version semantics.
- The signal collector, reconciler handler, CLI/dry-run commands, CloudWatch metrics, alarms, dashboard, IAM policy, and CDK schedules are extended.
- Existing Fleet acquisition, within-Region AZ/Local Zone fallback, price guardrails, ownership checks, EKS registration hooks, and approved whole-target failover execution remain in place.
- No deployment or AWS resource mutation is authorized by this proposal.
