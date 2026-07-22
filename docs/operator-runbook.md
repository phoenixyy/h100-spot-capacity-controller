# H100 Spot Capacity Controller — operator runbook

## Safety boundary

The controller starts with a disabled `1/1` target. It does not create EKS, subnets, routes, Local Zone opt-ins, quotas, or Kubernetes workloads. Do not enable a target until its launch template, network, price caps and notification destination have been reviewed.

Every newly created resource with a customizable AWS physical name must begin
with `Phoenix-Codex-Local-Spot-`. Fleet, instance and volume resources use a
matching `Name` tag. AWS-fixed service-linked role names and pre-existing CDK
bootstrap resources are documented exceptions.

The default `h100-production` profile permits only `p5.4xlarge` and `p5.48xlarge`. The separate `functional-validation` profile permits exactly one `g6e.xlarge` L40S machine, requires standalone mode, ordered Tokyo then Seoul candidate Regions, no Local Zones, and the ownership tag `purpose=functional-validation`. L40S fulfillment validates the controller path only; it is not H100 capacity or workload-compatibility evidence.

## Before deployment

1. Install the project dependencies and run the unit tests.
2. Select an SNS destination for capacity and failover alerts.
3. Prepare one launch template per candidate Region with the AMI, instance profile, encrypted storage, security groups and ownership tags.
4. Supply ordered standard-AZ subnets. Local Zone opt-in, subnet creation and routing happen outside this controller; add a Local Zone only after that prerequisite is complete.
5. For `existing-eks`, provide one existing same-Region EKS cluster per candidate Region. The operator-owned template must bootstrap self-managed nodes with the required labels and GPU taint.

## Read-only validation

These commands do not create capacity:

```sh
h100-spot-controller validate-target config/target.yaml
h100-spot-controller dry-run config/target.yaml
h100-spot-controller discover config/target.yaml --profile default
h100-spot-controller capacity-review config/target.yaml --profile default
```

`capacity-review` scans every configured candidate Region, filters Fleets and
non-terminated instances through the controller ownership contract, reports all
duplicates rather than hiding an invariant breach, and performs no AWS write. Add
`--table STATE_TABLE` after deployment to report the state-authoritative active
Region after failover.

Review standard-AZ and Local Zone opt-in state, matching approved subnet, and H100 offering. SPS and price results are advisory; an SPS limit or error is not a shortfall or failover trigger.

For the bounded validation target, start from `config/validation-target.example.yaml` and keep `enabled: false`. Its placeholder launch-template, AMI, profile and security-group values are deliberately invalid until approved inputs exist:

```sh
h100-spot-controller validate-target config/validation-target.example.yaml
h100-spot-controller dry-run config/validation-target.example.yaml --profile default
```

The report must show `accelerator_profile=functional-validation`, L40S count one and H100 count zero; `g6e.xlarge` offerings in the configured standard AZs; matching subnet Zone IDs; current G/VT Spot vCPU quotas; SPS and Spot prices; Linux On-Demand caps; `aws_write=false`; a one-machine destination request; and an `operator-request` manual failover preview. Refresh these observations immediately before deployment because offerings, scores, prices, quotas and AMIs can change.

## Deployment and enabling

Obtain explicit deployment authorization, synthesize and review the CDK changes, then deploy with the approved AWS identity. Supply the exact comma-delimited per-Region instance-role ARN list through `LaunchInstanceRoleArns`; wildcard PassRole is not allowed. Supply every approved per-Region Launch Template ARN through `LaunchTemplateArns`; the controller's required `ec2:RunInstances` permission is conditioned on that exact list. Verify DynamoDB encryption/PITR, one/five/fifteen minute schedules, Lambda logs, SNS subscription and IAM policy before enabling a target.

Persist the initial disabled target only after reviewing the stack outputs and dry-run:

```sh
h100-spot-controller target-put config/target.yaml --table STATE_TABLE --profile default --execute
h100-spot-controller target-review TARGET_ID --table STATE_TABLE --profile default
```

Replacing an existing target requires the version printed by `target-review`. An enabled configuration additionally requires `--enable-capacity`; this is separate from deploying the control plane.

Set final desired and maximum **machine** counts, per-instance Spot caps, and `enabled: true` in a reviewed target. Each machine has weight one even when it exposes eight H100 GPUs. The maintain Fleet continues requesting capacity between reconciliations.

The H100-only allowlist is `p5.4xlarge` (one H100) and `p5.48xlarge` (eight H100s). GPU count is derived by the controller. `p5e.48xlarge` and `p5en.48xlarge` use H200 GPUs and are intentionally rejected.

For validation, deploy the control plane and per-Region launch prerequisites only after a separate approval names every resource. The initial contract is:

- target `g6e-functional-validation`, standalone, desired/max `1/1`;
- source `ap-northeast-1`, destination `ap-northeast-2`;
- only the approved standard subnets whose discovered Zone IDs offer `g6e.xlarge`;
- an approved Region-specific AMI, exact instance-profile ARN, no inbound security-group rules, IMDSv2 required, encrypted root EBS with delete-on-termination, deterministic UserData hash, and instance/volume ownership plus bootstrap-contract tags;
- exact controller `iam:PassRole`, never wildcard;
- On-Demand-derived Spot caps and zero On-Demand target capacity.

Record the test start time and watch the Fleet, instance, EBS, public IPv4 (if used), CloudWatch and Lambda costs. The controller's one-machine limit and price cap bound compute scale, but they do not automatically end the test.

## Normal operation

The controller begins in one standard AZ, retains an AZ that has capacity as highest priority, then after 15 minutes of continuous shortfall adds standard AZs one-by-one. It only adds approved Local Zones after standard AZs are exhausted. Zone expansion never changes desired or maximum count.

For `existing-eks`, EC2 Fleet fulfillment and EKS readiness are distinct. A node that fails to register points to a launch-template, bootstrap, or EKS access issue; it must not increase desired Fleet capacity.

The controller does not call the Kubernetes API. An operator-owned integration may write a readiness snapshot to the state table at `pk=TARGET#<target-id>`, `sk=EKS_READINESS#<region>` with `registered_node_count`, `ready_node_count`, and `observed_at`. The controller publishes those values as separate CloudWatch metrics and never feeds them into Fleet desired capacity.

The DynamoDB state/configuration table, controller SNS topic, and Lambda log groups are retained if the CloudFormation stack is deleted. DynamoDB point-in-time recovery is enabled; notification deduplication and failover records use TTL where appropriate. Lambda logs expire after 30 days. Lambda functions, EventBridge rules, IAM resources, and dashboards follow normal stack deletion. EC2 Fleet cancellation and instance termination always remain separate, explicit operator actions.

## Region failover

When every approved Zone is active and the Region is short for the configured period (30 minutes by default), review the whole-target plan:

```sh
h100-spot-controller failover-review TARGET_ID PLAN_ID --table STATE_TABLE --profile default
```

Approve only when ready to accept a capacity gap:

```sh
h100-spot-controller failover-approve TARGET_ID PLAN_ID --table STATE_TABLE --profile default --execute
```

The IAM-attributed approval is single-use and expires after 30 minutes until the
controller atomically claims it. Before the first source Fleet stop or instance
termination, the controller records that exact execution claim and extends the
retention of its plan records. A claimed execution may finish after the original
expiry only for the same target version, source Fleet/instances, destination and
full count; an unclaimed expired approval makes no EC2 write. To decline it:

```sh
h100-spot-controller failover-reject TARGET_ID PLAN_ID --table STATE_TABLE --profile default --execute
```

For a healthy planned migration, first render a read-only plan. The command must fail unless the source target is fully fulfilled, the destination is configured and empty, and its launch contract and price cap resolve:

```sh
h100-spot-controller failover-request TARGET_ID \
  --destination-region ap-northeast-2 \
  --table STATE_TABLE --profile default
```

After reviewing exact source Fleet/instance IDs, full destination count, expiry and `trigger=operator-request`, persist and notify the plan. This writes DynamoDB/SNS state only and makes no EC2 capacity change:

```sh
h100-spot-controller failover-request TARGET_ID \
  --destination-region ap-northeast-2 \
  --table STATE_TABLE --profile default --execute
```

Then use the unchanged `failover-review` and `failover-approve` commands. Before approval, capture evidence that the Tokyo Fleet and instance remain running. After approval, capture consecutive reconciliation evidence that the source Fleet was stopped, the exact approved instance was terminated, independent tag-based source discovery returned zero, and only then a full one-machine Seoul Fleet request was created. Verify no timestamp shows owned active instances in both Regions. A reverse Seoul-to-Tokyo test requires a new plan and approval; never reuse the first approval.

After approval the controller first atomically claims the exact execution, then
stops the owned source Fleet, terminates only planned, owned source instances,
waits for a later read to confirm zero source capacity, and only then requests the
full desired count in the destination Region. It never moves a partial shortfall
or creates a cross-Region cluster. For EKS, workload drain and source Node cleanup
are operator-owned prerequisites.

## Disable, cleanup, and incidents

Set `enabled: false` to stop controller reconciliation writes; the existing maintain Fleet continues its AWS-managed target until explicitly cancelled. Review cleanup without writing:

```sh
h100-spot-controller cleanup config/target.yaml --table STATE_TABLE --profile default
```

After reviewing the exact Fleet and instance IDs, cancel the maintain request while preserving instances with `--execute`. Add `--terminate-instances` only when those tagged instances are also approved for termination:

```sh
h100-spot-controller cleanup config/target.yaml --table STATE_TABLE --profile default --execute --terminate-instances
```

Supplying the state table makes cleanup resolve the authoritative active Region after failover instead of trusting the file's original Region. Cleanup refuses an enabled target and rechecks ownership tags. Verify both Tokyo and Seoul have no owned non-terminated instances and no active owned Fleet. Retain plan, approval, reconciliation, CloudWatch and cleanup evidence, then decide separately whether to retain or delete operator-owned launch templates, security groups and instance profile, and whether to retain the control plane for H100 tests. During interruptions or repeated API failures, inspect logs and notifications, validate caps/subnets/quotas, and re-run read-only discovery before changing configuration.

Use `docs/validation-evidence-template.md` for the bounded Tokyo–Seoul run. If full
validation teardown is approved, export evidence before deleting anything, destroy
the controller stack, explicitly delete its retained table/topic/log groups by
recorded physical ID, then delete Seoul and Tokyo Launch Template stacks and the
global instance-role stack last. Keep the account-wide EC2 Fleet service-linked
role unless its deletion receives a separate account-level approval. Stack deletion
never substitutes for the capacity cleanup and two-Region zero check above.
# SPS-driven Region selection

Region selection is an explicit target policy and never expands the operator-approved
`candidate_regions` allowlist.

- `manual` is backward-compatible: `active_region` is required and SPS remains
  advisory.
- `recommend` records and displays a deterministic recommendation but never uses
  that recommendation to create, modify, terminate, or migrate capacity.
- `auto_initial` may establish runtime `active_region` only while no owned Fleet or
  non-terminated owned instance exists in any candidate Region. It requires a fresh
  decision derived from matching SPS, launch-contract, offering, quota, and price-cap
  evidence.

Use the read-only integration dry-run before changing mode. Review the selected
Region, ordered candidates, exclusions, request fingerprint, evidence timestamp,
decision version, and expiry. `NO_ELIGIBLE_REGION` means the controller waits; use
manual mode only when the operator deliberately accepts proceeding without a fresh
SPS decision.

After any target Fleet or instance exists, the runtime Region is pinned. A later
higher SPS elsewhere emits `RecommendationDiffersFromActive` but performs no move.
Cross-Region movement continues to require a whole-target failover plan, an unexpired
operator approval that is claimed before source mutation, source Fleet/instance termination, and a later verified source-zero
observation before requesting the full desired count in the destination. Selector
output alone is never migration approval.

Local Zones are not assigned SPS values. The controller first selects a parent
Region, then follows that Region's existing standard-AZ expansion order and finally
its approved eligible Local Zones. Capacity in a Local Zone pins the target to its
parent Region.

Rollback is non-destructive: set `region_selection.mode: manual` and set
`active_region` to the current runtime Region. Do not delete decision/state records
or change Region while capacity exists. Historical signal snapshots and expired
decisions age out through DynamoDB TTL.
