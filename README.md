# GPU Spot Capacity Controller

A safety-first AWS controller for maintaining a requested number of EC2 Spot
machines for GPU training or inference capacity. It uses **EC2 Fleet** for the persistent
Spot request, **AWS Lambda + EventBridge** for reconciliation, and
**DynamoDB** for target state, approvals, and operational history.

The controller is deliberately conservative: it starts disabled, treats
capacity as a number of machines (not GPU count), restricts the production
GPU metadata validation for operator-selected EC2 types, and never creates a
cross-Region GPU cluster.

> This is infrastructure software. Read-only validation is safe to run, but
> deployment, target enablement, Fleet cancellation, and instance termination
> must be explicitly authorized by an operator.

## What it does

- Maintains a configured EC2 Spot Fleet size in one active AWS Region.
- Starts with a preferred standard Availability Zone, expands through approved
  standard AZs after sustained shortfall, then uses approved Local Zones only
  as a fallback.
- Pins additional machines to an AZ that already has capacity where possible,
  avoiding unnecessary cross-AZ placement for distributed training.
- Derives a Linux On-Demand price cap for each instance type and keeps
  On-Demand target capacity at zero.
- Collects EC2 Spot Placement Score (SPS), offerings, quota, and price
  evidence to support Region choice.
- Supports three Region-selection modes: manual, read-only recommendation,
  and one-time automatic initial selection (`auto_initial`).
- Requires a reviewed, expiring, single-use approval for a whole-target
  cross-Region migration. It stops and verifies the source Region before
  requesting the full desired count in the destination.
- Publishes metrics, alarms, logs, and SNS notifications for reconciliation,
  selection, and failover outcomes.

## How placement and failover work

```text
Configured Region
      |
      v
Preferred standard AZ ──shortfall──> Other approved standard AZs
      |                                          |
      |                                  still shortfall
      |                                          v
      |                              Approved Local Zones (fallback)
      |
      v
Maintain EC2 Fleet

All approved placements short for the configured period
      |
      v
Create whole-target migration plan → operator approval → source reaches zero
      |
      v
Request the full target only in the destination Region
```

An SPS result is advisory in `manual` and `recommend` modes. In
`auto_initial`, it can select the runtime Region only before the controller
owns a Fleet or non-terminated instance in any candidate Region. Once capacity
exists, the Region remains pinned; moving it requires the explicit migration
workflow above.

## Architecture

| Component | Responsibility |
| --- | --- |
| CDK stack | Deploys the Lambda controller, DynamoDB state table, EventBridge schedules, SNS, CloudWatch metrics/alarms, and dashboard. |
| Reconciler Lambda | Validates the target, discovers owned capacity, obtains SPS evidence, and creates or updates the maintain Fleet within the approved contract. |
| EC2 Fleet | Continuously attempts to maintain the desired Spot machine count between reconciliation cycles. |
| DynamoDB | Stores target versions, runtime Region/AZ state, selection evidence, plans, approvals, and execution claims. |
| CLI | Provides read-only review, controlled target persistence, failover approval, and cleanup commands. |

The controller manages EC2 capacity, not Kubernetes workloads. It supports a
`standalone` mode and preserves an `existing-eks` integration boundary, but it
does not create EKS clusters or call the Kubernetes API.

## Quick start: read-only only

Requirements: Python 3.11+, an AWS profile with read access for the commands
you run, and project dependencies installed from `requirements.lock`.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.lock

# Validate the disabled example; it contains deliberate placeholders.
.venv/bin/h100-spot-controller validate-target config/target.example.yaml
.venv/bin/h100-spot-controller dry-run config/target.example.yaml --profile default
.venv/bin/h100-spot-controller discover config/target.example.yaml --profile default
.venv/bin/h100-spot-controller capacity-review config/target.example.yaml --profile default
```

These commands should not create AWS capacity. Before any deployment or write,
work through the [deployment preflight checklist](docs/deployment-preflight-checklist.md)
and the detailed [operator runbook](docs/operator-runbook.md).

## Configuration model

Start with [config/target.example.yaml](config/target.example.yaml). A target
defines:

- `desired_instance_count` and `maximum_instance_count`: the number of
  machines to maintain; both default to `1`.
- `instance_types`: explicit EC2 GPU types suitable for the workload. The
  controller verifies them through AWS metadata before creating capacity.
- `candidate_regions`: ordered, operator-approved Regions, Launch Templates,
  security groups, AMIs, instance profiles, and placement subnets.
- `region_selection`: `manual`, `recommend`, or `auto_initial`.
- Timing: standard-AZ expansion, Region-failover, and approval-expiry windows.
- `enabled`: must remain `false` until the target has been reviewed and
  capacity enablement separately approved.

Do not commit reviewed configuration files: they contain account-specific AWS
resource IDs and are covered by `.gitignore`.

## Tests and specifications

```sh
JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
  .venv/bin/python -m unittest discover -s tests -q

openspec validate --specs --strict
```

The maintained OpenSpec requirements are in [openspec/specs](openspec/specs).
Completed design changes are retained under [openspec/changes/archive](openspec/changes/archive).

## Operational safety guarantees

- No capacity request until an operator persists an enabled target using the
  explicit capacity-enable path.
- All discovery and mutation is constrained by the target ownership contract.
- Desired/max values are machines, with one Fleet weight per machine.
- No partial Region failover and no simultaneous controller-owned capacity in
  source and destination during an approved migration.
- Local Zones are only used after standard AZs have been exhausted, and remain
  attached to their parent Region.
- EKS integration never turns EC2 fulfillment into workload readiness; node
  registration/readiness remains observable but does not inflate Fleet demand.

For deployment, enablement, incident response, and explicit cleanup procedures,
use the [operator runbook](docs/operator-runbook.md).
