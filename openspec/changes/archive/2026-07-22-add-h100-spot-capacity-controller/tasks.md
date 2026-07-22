## 1. Confirm operating contract

- [x] 1.1 Use `us-east-1` and `us-west-2` for H100 production discovery, and use a distinct disabled `functional-validation` target with `g6e.xlarge`, ordered `ap-northeast-1` then `ap-northeast-2`, standalone mode, and desired/maximum machine counts of 1/1 for real control-path validation.
- [x] 1.2 Use matching Linux On-Demand pricing as each instance type's Spot cap; confirm notification destination, default 15-minute per-Zone expansion threshold, default 30-minute all-Zones Region threshold, and whether capacity rebalancing may temporarily exceed target; use local CLI approval with a 30-minute expiry for Region failover.
- [x] 1.3 Obtain approved per-Region launch templates, IAM instance profiles, ordered standard-AZ subnets, security groups, AMI/bootstrap behavior, and encryption requirements for the Tokyo/Seoul validation target; retain the same gate for later H100 standard-AZ and optional already-enabled Local Zone inputs.
- [x] 1.4 Confirm the first enabled validation target uses `standalone`; retain a validation gate requiring one approved existing EKS cluster, node access, EKS-compatible AMI/bootstrap contract, labels, GPU taint, and source drain procedure before any later `existing-eks` target can be enabled.

## 2. Project and infrastructure foundation

- [x] 2.1 Extend the Python project, CLI, and target schema with explicit `h100-production` and tightly bounded `functional-validation` accelerator profiles while preserving the disabled 1/1 initial target and optional `standalone`/`existing-eks` production integration boundary.
- [x] 2.2 Define encrypted DynamoDB state/configuration storage with target identifiers, optimistic locking, active Region and Zone order, per-Zone timers, and versioned single-use failover plans and approvals.
- [x] 2.3 Define configurable EventBridge schedules with initial one-minute reconciliation, five-minute signal collection, and fifteen-minute SPS refresh; add Lambda functions, CloudWatch log groups, SNS notification integration, and least-privilege IAM policies.
- [x] 2.4 Apply ownership tags and retention/cleanup policies to every controller-managed resource; use the exact `Phoenix-Codex-Local-Spot-` prefix for every customizable created resource name and a matching `Name` tag where no name field exists, with documented AWS-managed/pre-existing exceptions.

## 3. Capacity reconciliation

- [x] 3.1 Implement profile-aware validation: exact H100 production types and GPU counts; exact one-machine `g6e.xlarge` L40S standalone Tokyo/Seoul validation boundary; weight one; On-Demand-derived caps; ordered standard/Local Zone inputs; thresholds; and failover settings.
- [x] 3.2 Implement owned EC2 Fleet discovery, create, describe, persistent maintain behavior, preferred-Zone retention, sequential standard-AZ then Local Zone expansion, and target modification with idempotency protection and unchanged target limits.
- [x] 3.3 Implement disabled-target behavior and explicit, separately authorized cleanup behavior.
- [x] 3.4 Implement structured reconciliation outcomes, error classification, and DynamoDB concurrency control.
- [x] 3.5 Implement read-only Local Zone eligibility discovery for opt-in state, parent Region, approved subnet, and approved H100 instance-type offerings without mutating zone or network configuration.
- [x] 3.6 Extend versioned whole-target failover with a local `failover-request` preview/persist flow for healthy targets; require a configured destination, validated launch contract and caps, exact source resources, idempotency or conflict rejection, then reuse review, single-use 30-minute approval, exact matching, and zero-source execution before AWS destination capacity writes.
- [x] 3.7 Implement approved failover execution that stops the source fleet, terminates only approved owned source instances, waits for zero source capacity, and then creates the destination fleet for the full desired count.
- [x] 3.8 Implement `existing-eks` configuration validation for same-Region clusters, bootstrap contract version, labels, and GPU taint; preserve the launch-template-owned node join process and reject cross-Region mappings.

## 4. Signals and notifications

- [x] 4.1 Adapt Spot Placement Score and Spot-price collection patterns for approved active and failover Region/AZ candidates, and collect separate non-SPS eligibility and price observations for approved Local Zones without treating signals as failover approval.
- [x] 4.2 Publish profile-aware CloudWatch metrics and dashboard views for score, active Region, per-Zone capacity and expansion state, Local Zone eligibility, failover state and trigger, per-type price, desired/fulfilled machine capacity, realized accelerator model/count, H100 count only for H100 production, shortfall, retries, and interruptions.
- [x] 4.3 Implement deduplicated SNS notifications for prolonged shortfall, Zone expansion, failover approval required/expired/rejected/completed, interruption/rebalance events, and repeated reconciliation failures.
- [x] 4.4 Publish separate EKS node registration/readiness metrics for configured integrations without using them to increase desired Fleet capacity; include operator-owned source drain/cleanup reminders in failover notifications.

## 5. Validation and operations

- [x] 5.1 Extend unit tests to cover profile isolation, L40S not counted as H100, validation Region/type/mode/count/Local Zone restrictions, manual plan preview and persistence, healthy-source preservation before approval, idempotent/conflicting requests, exact source binding, zero-source barrier, full destination count, approval non-reuse, and existing H100/EKS/Local Zone behavior.
- [x] 5.2 Add CDK/IAM assertions that reject wildcard destructive permissions and Local Zone/network mutation permissions, and verify tags and encryption settings.
- [x] 5.3 Extend read-only integration dry-runs to Tokyo and Seoul `g6e.xlarge`, including live offerings, quotas, SPS, prices/caps, standard AZs, launch-contract readiness, manual full-target plan preview, exact source impact, and destination request without AWS writes; retain H100 east/west dry-runs.
- [x] 5.4 Update the operator runbook for accelerator profiles, bounded validation deployment, Tokyo/Seoul launch inputs, manual plan request/review/approval, proof of source-zero before destination, reverse migration, cost monitoring, disable, explicit cleanup, and the distinction between L40S control-path evidence and H100 capacity evidence.
- [x] 5.5 Review the updated deployment plan, obtain explicit authorization for every AWS write and cost, verify the `Phoenix-Codex-Local-Spot-` naming contract before creation, then execute and retain evidence for the bounded Tokyo fulfillment and approved Tokyo-to-Seoul whole-target migration (optionally reverse it), followed by explicit cleanup; do not treat this as proof of H100 capacity.
- [x] 5.6 Atomically claim a valid failover approval before the first source mutation, retain claimed execution records through source drain, reject unclaimed expiry or changed-contract replay, persist Fleet request epochs so a deleted Fleet receives a new EC2 idempotency token only after its owned instances are gone, and cover interruption/expiry/replacement recovery with automated tests before repeating bounded validation.
