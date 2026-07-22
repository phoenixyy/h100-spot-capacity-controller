## 1. Generic GPU target model

- [x] 1.1 Remove the H100 production profile and static P5 allowlist from the production target schema.
- [x] 1.2 Add a batched EC2 `DescribeInstanceTypes` GPU metadata validator and normalized GPU descriptor.
- [x] 1.3 Preserve the bounded `functional-validation` G6e fixture while allowing G6e through the generic production path.
- [x] 1.4 Include normalized GPU metadata in target fingerprints and state-safe serialization.

## 2. Reconciliation and selection

- [x] 2.1 Apply GPU metadata validation to dry-run, target write/review, discovery, and Fleet reconciliation paths.
- [x] 2.2 Apply the same validation to SPS candidate evaluation, Local Zone eligibility, and auto-initial Region selection.
- [x] 2.3 Block new Fleet or migration-destination requests on missing or changed GPU metadata and emit classified outcomes.

## 3. Observability and interfaces

- [x] 3.1 Replace H100-specific metrics and reports with generic GPU model/count observations while retaining machine-count capacity.
- [x] 3.2 Update CLI reports, example targets, README, and operator runbook for generic GPU support and the test-only G6e fixture.
- [x] 3.3 Add migration diagnostics for legacy H100-profile configurations.

## 4. Verification

- [x] 4.1 Add unit tests for valid GPU metadata, CPU/non-GPU rejection, missing metadata, G6e generic acceptance, and test-fixture isolation.
- [x] 4.2 Add tests covering generic GPU metadata through SPS selection, Fleet request construction, Local Zone eligibility, migration destination checks, and metrics.
- [x] 4.3 Run the complete unit suite and strict OpenSpec validation; keep all AWS actions read-only unless separately authorized.
