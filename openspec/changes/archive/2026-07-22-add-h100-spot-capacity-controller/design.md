## Context

The project is currently empty. The desired product is a cloud-managed service that
continually attempts to obtain H100 Spot EC2 instances in a Region selected by the
operator. The Spot Placement Score Tracker is a useful reference for score, price,
CDK, Lambda, and CloudWatch patterns, but it does not submit or maintain capacity
requests. Spot Placement Score covers Regions and standard Availability Zones; it
does not provide a direct Local Zone score. Local Zones therefore require a
separate eligibility check based on operator approval, opt-in state, subnet
configuration, H100 instance-type offerings, quotas, and available price history.
The EKS-based references inform a deliberately narrow integration boundary. The
first implementation remains a capacity-acquisition controller, but an operator
can supply an existing EKS cluster and an EKS-ready launch template in each
candidate Region so acquired instances can join the matching Region's cluster.
The controller does not create clusters or schedule workloads.

AWS maintain-type EC2 Fleet is the primary capacity mechanism: it continues to
attempt target replacement after an interruption. A scheduled controller is still
needed to validate configuration, create or update the one owned fleet, publish
observability data, and surface failures that a fleet alone cannot explain.

Real H100 fulfillment cannot be assumed during implementation because the approved
H100 Spot pools may remain unavailable for long periods. Unit tests and read-only
dry-runs prove request construction and safety boundaries but cannot prove that an
owned maintain Fleet can actually launch, be observed, be cleaned up, and move
between Regions. A separate bounded validation profile therefore uses the cheaper
and currently offered `g6e.xlarge` L40S type in Tokyo and Seoul. This profile is a
test vehicle for the same controller path, not an accelerator substitution for an
H100 workload.

## Goals / Non-Goals

**Goals:**

- Continuously maintain a bounded, all-Spot H100 instance target in one Region.
- Count desired and maximum capacity as EC2 instance counts, independent of the
  number of H100 GPUs attached to each approved instance type.
- Prefer the Zone already holding target instances, expand sequentially to other
  standard Availability Zones and then approved Local Zones in the same Region,
  and keep GPU capacity for one target in only one Region at a time.
- Avoid duplicate requests, accidental use of unowned resources, and unbounded cost.
- Make capacity availability, shortfall, interruption, and API failures visible.
- Allow an enabled target to use an operator-owned launch template to attach its
  acquired instances to an existing EKS cluster in the same active Region.
- Deploy a small, auditable serverless control plane with AWS CDK and Python.
- Exercise real Fleet fulfillment and approved whole-target Region migration with
  a separately tagged one-machine `functional-validation` target without weakening
  H100 production configuration.

**Non-Goals:**

- Guarantee H100 capacity, reserve capacity, or substitute On-Demand instances.
- Provision EKS, install Kubernetes add-ons, schedule training jobs, perform gang
  scheduling, or manage user access inside the launched instance.
- Automatically terminate an active Region or request capacity concurrently in
  multiple Regions without an operator-approved whole-target failover plan.
- Enable or disable Local Zones, create Local Zone subnets or routes, request
  quotas, or otherwise provision operator-owned Local Zone prerequisites.
- Estimate or enforce total account spend beyond the explicit per-instance price
  cap and instance-count limit in the first release.
- Treat successful L40S validation as evidence that H100 Spot capacity exists or
  that an H100 training workload is compatible with the validation instance.

## Decisions

### Use EC2 Fleet of type `maintain`

The controller will create one Spot-only maintain-type EC2 Fleet per enabled target.
It will use `capacity-optimized-prioritized` so launch-template overrides can rank
Zones already holding target instances ahead of newly expanded Zones while AWS
still optimizes for available capacity. Priority is best-effort, so the controller
enforces the stronger ordering by exposing only one Zone initially and adding later
Zones after their thresholds. A maintain fleet is the AWS mechanism that attempts
to replace interrupted Spot capacity; periodically creating one-time Spot requests
would create duplicate-request and cleanup risks.

The configured desired capacity is the total number of EC2 instances, and every
approved instance type has a fleet weight of one. A target of four therefore means
four machines even when the selected instance types contain different numbers of
accelerators. The controller will report the realized accelerator model and count
separately for visibility, plus H100-specific count only for H100 production
targets, but it will not use GPU count to reconcile capacity in the first release.

Each target has exactly one active parent Region and one owned fleet in that Region.
The fleet begins with one preferred standard Availability Zone selected from the
approved subnets and SPS guidance. If that Zone obtains any target capacity, it
remains the highest-priority pool for the remaining machines. When it cannot expand
to the desired count within the configured threshold, reconciliation adds the next
approved standard Availability Zone. Local Zones are added only after approved
standard Availability Zones have been tried. Sequential expansion can produce a
multi-AZ target within one Region, but never a multi-Region target. The desired and
maximum counts remain unchanged throughout Zone expansion.

Creating the maintain fleet is the capacity request. Reconciliation does not
submit a new fleet on every schedule: it resolves the stable target identifier to
the same owned fleet and modifies it only when the target or eligible subnet set
changes. EC2 Fleet remains responsible for continuously attempting to fulfill and
replace Spot capacity between reconciliation runs.

Alternative considered: Auto Scaling Group. It is a good lifecycle mechanism but
does not naturally model a small explicit, heterogeneous accelerator fleet as
directly as EC2 Fleet. Alternative considered: Karpenter/EKS. It introduces a
Kubernetes cluster and workload scheduler outside this product's first scope.

### Reserve an existing-EKS node integration boundary

Each target supports `standalone` mode (the default) or optional `existing-eks`
mode. In `existing-eks` mode, every approved candidate Region identifies one
already-running EKS cluster, and its operator-owned launch template/AMI/user data
bootstraps Fleet instances as self-managed nodes of that same Region's cluster.
The controller validates the declared Region-to-cluster association but does not
generate user data, create EKS access entries, create node groups, call Kubernetes
workload APIs, or install Kueue, Training Operator, Volcano, Ray, or GPU Operator.

The integration contract is intentionally launch-template based: the operator
supplies the instance profile, EKS-compatible AMI, bootstrap configuration, and
pre-authorized node identity. Bootstrap must apply stable Kubernetes labels for
the controller target, Region, Zone, instance type, and capacity source, plus the
operator-approved GPU taint. This keeps the same controller compatible with
standard-AZ and Local Zone nodes; a Local Zone node joins the EKS control plane in
its parent Region, never a cluster in another Region.

EC2 Fleet fulfillment remains the capacity control signal. Kubernetes node
registration and `Ready` state are a separate integration-readiness signal: a
node failing to join EKS is visible to the operator but does not cause the
controller to request extra machines beyond its configured desired count. This
prevents a bootstrap or cluster-RBAC error from bypassing machine-count and
Spot-price guardrails.

At an approved Region failover, the existing source fleet and source instances
are still cleared before a destination fleet is created. Thus a target's nodes
can belong to only the source EKS cluster or only the destination EKS cluster at
one time. First release cleanup of Kubernetes Node objects and workload draining
remains operator-owned; the approval notification and runbook must state this
explicitly before source instances are terminated.

Alternative considered: deploy an EKS cluster and Karpenter as part of this
controller. That would duplicate the EC2 Fleet capacity mechanism, materially
expand IAM and lifecycle ownership, and introduce workload scheduling before the
capacity-acquisition safety model is proven.

### Separate H100 production from functional validation

The target configuration has an explicit `accelerator_profile`. The default and
production value is `h100-production`, whose internal allowlist contains only
`p5.4xlarge` with one H100 and `p5.48xlarge` with eight H100 GPUs. The controller
continues to reject A100, H200, L40S, and unknown types for that profile.

The only first-release exception is `functional-validation`. It permits exactly
`g6e.xlarge`, records one NVIDIA L40S accelerator per machine, requires
`integration_mode: standalone`, requires desired and maximum instance counts of
one, rejects Local Zone placements, and initially requires the ordered candidate
Regions `ap-northeast-1` then `ap-northeast-2`. It uses a separate target identifier
and ownership tag declaring the validation purpose. These restrictions prevent a
validation configuration from becoming a general escape hatch around the H100
allowlist or scale guardrails.

Both profiles use the same maintain Fleet, price-cap, ownership, reconciliation,
approval, zero-source, and cleanup implementation. The generic
`RealizedAcceleratorCount` and accelerator model dimensions describe either
profile. `RealizedH100GpuCount` remains available for an H100 production target and
is zero for the L40S validation target. A successful validation run proves the AWS
control path, not H100 availability or workload compatibility.

### Use a serverless reconciliation control plane

EventBridge invokes a Python Lambda on a fixed interval. The Lambda reads target
configuration and state from DynamoDB, validates the guardrails, describes the
owned fleet and instances, and creates or modifies that fleet when required. The
same function records a reconciliation result. A separate scheduled collector
queries placement scores and prices, or shares the function when the code remains
small.

The initial schedules will be one minute for fleet reconciliation, five minutes
for price and capacity-signal collection, and fifteen minutes for standard
Region/AZ SPS refresh. Repeated SPS calls use the same approved configuration to
avoid treating score fluctuations as configuration changes. These values remain
configurable, but reconciliation cannot be slower than the Zone-expansion and
failover states it is expected to observe.

Alternative considered: a continuously running daemon. Lambda removes host
maintenance and credentials persistence, and the fleet itself retains the
continuous capacity request between invocations.

### Separate standard SPS discovery from Local Zone discovery

The collector will request Spot Placement Scores for the operator-approved Region
allowlist and standard Availability Zones. The operator selects the parent Region;
the controller will not automatically deploy into the highest-scoring Region.
Because SPS does not directly score Local Zones, the collector will separately
inventory Local Zones in the selected parent Region and report whether each one is
approved, opted in, backed by an approved subnet, and offers an approved H100
instance type. Instance-type offerings and Spot price history are advisory and do
not prove current Spot capacity.

Only one standard Availability Zone is eligible initially. Zone expansion uses a
default continuous-shortfall threshold of fifteen minutes per newly activated
Zone. The timer starts only for a valid, enabled target whose owned fleet is below
desired capacity; configuration, authorization, and dependency errors do not
advance it. Full fulfillment resets the timer. If a Zone already contains target
instances, its overrides stay active and highest priority while later Zones are
added for remaining capacity.

After all approved standard Availability Zones have been activated without full
fulfillment, reconciliation adds eligible Local Zones one at a time using the same
threshold. A Local Zone that obtains capacity is retained like any standard Zone;
the controller does not remove a Zone holding healthy target instances merely to
move them elsewhere. Zone expansion never changes desired or maximum capacity. The
controller never calls APIs that opt in a zone group, create or modify a subnet or
route, or request a quota increase.

### Make the launch template and network inputs operator-owned

The controller receives a launch-template ID/version, security groups, IAM instance
profile, and approved subnet/AZ overrides rather than inventing them. This prevents
an infrastructure controller from silently selecting a public network, AMI, SSH
key, or instance role. The target configuration explicitly declares an accelerator
profile and instance types from that profile's internal allowlist. Each entry
contains an exact EC2 instance-type name and a
per-instance-hour Spot price cap derived from its matching Linux On-Demand price.
The price is a Spot ceiling only and never requests On-Demand capacity. Validation
rejects an unknown type, a type outside the selected profile, or a type unavailable
in every approved placement. The controller never silently substitutes one
accelerator for another. Every configured instance type contributes one machine to
the target capacity.
Subnets are classified as `standard` or `local-zone`; every Local Zone subnet must
belong to an approved, already enabled Local Zone in the target's parent Region.
For `existing-eks` mode, each candidate Region additionally provides its existing
cluster identifier, node-bootstrap contract version, expected labels and GPU taint.
The controller rejects a target whose cluster Region differs from the candidate
Region or whose integration data is incomplete. Cluster endpoint access, node IAM
authorization, and bootstrap implementation remain outside the controller role.

Alternative considered: have CDK create all networking and an AMI. That would make
the tool unsafe and overly prescriptive for an account-specific GPU environment.

### Keep one active Region and require approval for whole-target failover

Read-only SPS discovery compares an operator-approved Region allowlist, and every
candidate Region has its own operator-supplied launch template and network inputs.
The target state records exactly one active Region. If all approved standard and
Local Zones in that Region have been activated and the fleet remains short for an
additional default thirty minutes, the controller creates a failover plan for the
next eligible Region. The plan requests the full desired machine count in the
destination, never only the source Region's shortfall.

The controller publishes the plan, notifies the operator, and enters
`FAILOVER_AWAITING_APPROVAL`. It makes no destructive or destination capacity call
until it receives an explicit approval bound to the target identifier, target
configuration version, source Region, destination Region, desired count, and an
expiry time. Rejection, expiry, or any intervening target change invalidates the
plan and leaves the source fleet active.

The first release exposes approval through a small local CLI authenticated with the
operator's AWS IAM credentials. The SNS notification includes the plan identifier;
the operator reviews it and runs one `failover approve --plan-id <id>` command. The
CLI conditionally writes a single-use approval record to DynamoDB. Approvals expire
after thirty minutes. No web UI, API Gateway, email reply handling, or multi-party
workflow is included; target-version and plan matching remain internal safety
checks rather than additional operator steps.

After valid approval, the controller atomically claims the matching single-use
approval, then confirms retention of the plan, current-plan pointer, and approval
records before it stops the source maintain request or terminates any source
instance. A retry of an existing execution claim repeats that retention check
before it can touch EC2. A claim is permitted only while the approval is unexpired and still
matches the target version, source and destination Regions, full desired count,
source Fleet, and exact source instance list. The durable claim timestamp is a
separate execution marker; it is not an approval mechanism that an operator or a
retry can fabricate.

Once a valid execution claim exists, retries of that exact plan may continue past
the original approval expiry. They retain every original ownership and contract
check, and cannot be used for a different plan or a second migration. This avoids
stranding a target with its source Fleet stopped when EC2 termination, a Lambda
retry, or a network outage takes longer than the approval window. An unclaimed
approval still expires normally and makes no destructive or destination capacity
write. The controller must observe zero active controller-owned target instances
in the source Region before creating the destination maintain fleet for the full
desired count. This deliberate capacity gap guarantees that one target never owns
GPU capacity in two Regions at once. If destination fulfillment also fails, moving
again requires a new plan and approval; a completed execution claim is never
reused.

### Allow an operator-requested whole-target migration plan

Capacity shortfall remains the automatic reason to propose a Region failover, but
it is not the only legitimate reason to prepare a plan. The local CLI also supports
an operator-requested plan for a healthy enabled target. This makes planned
maintenance and the Tokyo-to-Seoul validation path deterministic instead of
deliberately causing an invalid price cap, unsupported placement, or artificial
capacity failure.

`failover-request` first renders a read-only preview. Persisting the request requires
an explicit `--execute` acknowledgement and writes only the versioned plan and its
notification; it does not stop a Fleet, terminate an instance, or create destination
capacity. The requested destination must be a configured candidate Region other
than the state-authoritative active Region, its launch-template contract and price
caps must validate, and no other current plan or failover execution may exist.

The persisted manual plan contains the same target/configuration version, exact
source Fleet identifier, exact source instance identifiers, source/destination
Regions, full desired count, expiry, and single-use identity as an automatically
generated plan. It then uses the unchanged `failover-review`, `failover-approve`,
zero-source barrier, and destination creation path. A repeated identical request is
idempotent; a conflicting current plan is rejected. Manual plan creation never
bypasses operator approval or allows simultaneous cross-Region target capacity.

### Start disabled with conservative capacity values

The generated target template sets `enabled: false`, `desired_instance_count: 1`,
and `maximum_instance_count: 1`. The values are visible and operator-editable, but
no capacity request occurs until the operator reviews the dry-run and explicitly
enables the target. Later increases must remain within the configured maximum; an
increase to the maximum is itself an operator-managed configuration change.

### Separate production candidates from real-capacity validation Regions

H100 production discovery and Local Zone dry-run validation continue to focus on
`us-east-1` and `us-west-2`. They are candidate parent Regions, not evidence that a
particular standard or Local Zone currently has H100 Spot capacity. Every run must
discover the account's current Zones, opt-in states, approved subnets,
instance-type offerings, and price observations before declaring a backup placement
eligible.

The separately tagged real-capacity validation target uses `ap-northeast-1` as its
initial active Region and `ap-northeast-2` as its approved destination. Account
discovery on 2026-07-21 found `g6e.xlarge` offerings in two standard AZs in each
Region, G/VT Spot quotas sufficient for one four-vCPU instance, and default VPC
subnets, but found no existing launch templates. Those observations are inputs to a
new dry-run, not permanent assumptions. Per-Region launch templates, AMIs, instance
profiles, security groups, and encryption settings must still be approved, and no
resource may be created until deployment authorization is given.

### Reuse the tracker concepts, not its deployment wholesale

The score-tracker repository's configuration, Lambda, metric, dashboard, and
least-privilege patterns will be adapted. The controller will not use a placement
score as an absolute launch gate because it is a probability signal; the maintain
fleet remains active to fulfil the user's continuous-request goal.

### Tag every controller resource

Every fleet, launched instance, volume where supported, DynamoDB record, log group,
and metric will include a stable `managed-by` and target identifier. Those tags are
the authorization boundary for reconciliation and any future cleanup command.

Every resource created by this solution whose AWS service exposes an
operator-defined physical name will additionally use the exact prefix
`Phoenix-Codex-Local-Spot-`. This includes validation CloudFormation stacks,
instance IAM role/profile, regional security groups and Launch Templates, and CDK
controller tables, topics, roles, functions, log groups, EventBridge rules and
dashboard. Fleet, instance and volume resources without a separate name field use
a `Name` tag beginning with the same prefix. AWS-fixed service-linked role names
and pre-existing CDK bootstrap resources are exceptions because the deployment
cannot rename them.

## Risks / Trade-offs

- [No Spot capacity exists] → retain the maintain fleet, publish shortfall state,
  and alert; never promise eventual availability.
- [H100 has few eligible pools] → accept multiple approved H100 instance variants
  and AZ subnets only when the workload is compatible; expose the trade-off rather
  than silently substituting a GPU class.
- [SPS omits Local Zones] → use SPS only for standard Region/AZ guidance and expose
  Local Zone readiness separately without inventing a synthetic placement score.
- [Local Zone is listed but not Spot-capable] → treat instance-type offerings and
  price history as advisory, retain the bounded maintain request, and report the
  unfulfilled backup pool without claiming capacity exists.
- [Zone expansion changes placement] → activate one Zone at a time, retain Zones
  already holding target instances as preferred, keep target capacity unchanged,
  and record every expansion as an audit event.
- [Region failover destroys partial capacity] → require an expiring, version-bound
  operator approval that identifies the source instances to terminate and the full
  destination request before execution.
- [Region failover creates a cross-Region cluster] → do not create the destination
  fleet until the source fleet is stopped and zero owned source instances remain.
- [EKS bootstrap or RBAC fails] → report EC2 fulfilment and EKS readiness
  separately; retain the configured Fleet target and require the operator to fix
  the launch template or cluster-side access rather than requesting extra nodes.
- [EKS integration crosses Regions] → validate each declared cluster's Region and
  only allow its matching candidate Region; reject mismatched configuration before
  any Fleet write.
- [Destination Region also lacks capacity] → require a new plan and approval before
  moving again; never reuse an approval.
- [Multiple configured H100 types have different GPU counts] → reconcile machines
  with weight one and report actual GPU count separately so the operator can see
  the heterogeneous result.
- [Validation type leaks into production] → require an explicit profile, exact
  internal type/Region allowlists, standalone mode, one-machine limits, no Local
  Zones, separate ownership tags, and profile-aware metrics.
- [Healthy manual migration bypasses safety] → allow only plan persistence, bind it
  to exact current source resources, and reuse the same expiry, approval,
  zero-source, and full-destination invariants as capacity-triggered failover.
- [Price cap prevents fulfilment] → surface the current observed price and cap in
  the notification; do not raise caps automatically.
- [Fleet replacement overlaps an at-risk instance] → allow a bounded temporary
  overage only if the operator enables capacity rebalancing and approves it;
  otherwise start with interruption replacement only.
- [Configuration mistake launches costly capacity] → require approval before
  deployment, a maximum count, a maximum price, ownership tags, and a dry-run mode.
- [Concurrent Lambda invocations] → use a conditional DynamoDB lock/state version
  and idempotency tokens before create or modify calls.
- [Approval expires during source drain] → atomically record a version- and
  resource-bound execution claim before the first source mutation, retain the
  plan records through completion, and let retries proceed only for that claimed
  execution; leave an unclaimed expired plan non-destructive.
- [Deleted Fleet reuses an EC2 idempotency token] → persist the active Fleet ID
  and a Fleet-request epoch; retain the epoch while a create is becoming visible,
  and rotate it only after the recorded Fleet and all of its owned instances are
  no longer active.

## Migration Plan

1. Deploy the CDK stack with no enabled target and verify IAM, logging, dashboards,
   and dry-run reconciliation.
2. Generate one disabled target with desired and maximum instance counts of one,
   then validate its launch template, permitted instance types and per-type price
   caps, standard subnets, optional Local Zone allowlist, tags, and notifications.
   Verify that no Local Zone opt-in or network mutation API is present in the
   controller role.
   When `existing-eks` mode is selected, also validate the pre-existing cluster,
   the matching Region, node access, bootstrap labels/taint, and launch-template
   join behavior without creating a workload.
3. After an operator-reviewed change, raise the target to the approved count and
   observe fleet events and CloudWatch metrics.
4. After separate deployment approval, enable the one-machine standalone
   `functional-validation` target in Tokyo, verify real fulfillment, request and
   approve Tokyo-to-Seoul migration, prove zero source capacity before destination
   creation, optionally reverse the migration, and retain the evidence.
5. Keep H100 production targets disabled until their own launch inputs and dry-runs
   are approved; L40S validation is not an H100 capacity signal.
6. To roll back, disable the target first. Canceling the fleet and terminating its
   instances is intentionally a separate command that requires explicit approval.

## Open Questions

- Which already enabled Local Zones and Local Zone subnets, if any, are approved as
  backup? The initial per-Zone expansion threshold is fifteen minutes unless
  overridden.
- What notification destination should receive controller events?
- Which launch template, AMI, EBS size, IAM instance profile, subnets, and security
  groups are approved for GPU instances?
- For any later `existing-eks` target, which per-Region cluster, EKS-compatible
  AMI, node access entry, bootstrap contract, labels, and GPU taint are approved?
- Must capacity be held idle after launch, or will the operator use the existing
  EKS cluster manually until a later phase adds Kueue and distributed-training
  scheduling?
- Is capacity rebalancing worth a controlled temporary overage, or must the running
  instance count never exceed the requested target?
