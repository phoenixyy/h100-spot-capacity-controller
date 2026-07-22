# Tokyo–Seoul functional-validation evidence

Status: **template; populate only from actual command and AWS API output**.

This record proves the controller control path with one `g6e.xlarge` L40S Spot
machine. It is not H100 capacity, H100 workload, distributed training, or EKS
evidence. Preserve raw JSON outputs alongside this summary and use UTC timestamps.

## Run identity and approvals

| Field | Recorded value |
|---|---|
| AWS account and operator ARN | |
| Git commit / source digest | |
| CDK synthesized-template digest | |
| Target ID | `g6e-functional-validation` |
| Required resource-name prefix | `Phoenix-Codex-Local-Spot-` |
| Target configuration versions | |
| Gate A approval and timestamp | |
| Gate B approval and timestamp | |
| Gate C approval and timestamp | |
| Gate D1 approval and timestamp | |
| Gate D2 plan-bound approval and timestamp | |
| Gate E approval and timestamp | |
| Gate F approval and timestamp | |

## Refreshed launch inputs

Record both Regions' AMI ID, Launch Template ID/version/ARN, UserData SHA-256,
instance-profile ARN, security-group ID and ingress count, encrypted root-volume
contract, standard subnet IDs and Zone IDs, `g6e.xlarge` offering, G/VT Spot quota,
SPS result/status, observed Spot price, and Linux On-Demand cap.

Record a synthesized-template and deployed-inventory assertion that every
customizable created physical name begins with `Phoenix-Codex-Local-Spot-`, and
list only `AWSServiceRoleForEC2Fleet` and pre-existing CDK bootstrap resources as
fixed-name exceptions.

## Disabled control-plane proof

- Persisted target is `enabled: false`, desired/max `1/1`.
- Three or more reconciliations made no EC2 capacity write.
- Tokyo and Seoul owned Fleet inventories are empty.
- Tokyo and Seoul owned non-terminated instance inventories are empty.

Raw evidence files:

## Tokyo fulfillment proof

Record Fleet ID/status/type/allocation strategy/target, instance ID/type/state/AZ,
launch time, Spot price/status, Launch Template version, tags, IMDSv2, encrypted
30-GiB gp3 volume, and `nvidia-smi` model/UUID/memory/driver output.

- Three unchanged reconciliations resolve the same Fleet ID.
- `RealizedAcceleratorCount=1` and model is NVIDIA L40S.
- `RealizedH100GpuCount=0`.

Raw evidence files:

## Manual plan and pre-approval proof

Record plan ID, trigger, target/configuration version, expiry, full desired count,
source/destination Regions, exact source Fleet/instance IDs, and operator ARN.

- Preview reports `aws_write=false` and `ec2_write=false`.
- Persisted plan makes DynamoDB/SNS writes only.
- Tokyo Fleet and instance remain active before Gate D2 approval.

Raw evidence files:

## Source-zero barrier and Seoul proof

| UTC timestamp | Event / independent observation | Region | Fleet IDs | Active instance IDs |
|---|---|---|---|---|
| | Gate D2 approval accepted | | | |
| | Tokyo Fleet stopped | `ap-northeast-1` | | |
| | Approved Tokyo instance terminated | `ap-northeast-1` | | |
| | Independent Tokyo owned-capacity count = 0 | `ap-northeast-1` | | |
| | Seoul Fleet create request | `ap-northeast-2` | | |
| | Seoul full desired count fulfilled | `ap-northeast-2` | | |

- Seoul Fleet create time is strictly later than independently observed Tokyo zero.
- Seoul request is the full count `1`, not a source shortfall.
- No interval contains active owned instances in both Regions.
- Approval record is atomically marked used and cannot match a later execution;
  automated failure-injection tests retain the explicit reuse-rejection evidence.

Raw evidence files:

## Cleanup proof

Record the reviewed cleanup Fleet/instance IDs and Gate E approval.

- Tokyo active owned Fleets: zero.
- Seoul active owned Fleets: zero.
- Tokyo non-terminated owned instances: zero.
- Seoul non-terminated owned instances: zero.
- Validation-exclusive stacks and retained resources deleted under Gate F, or each
  intentionally retained resource and owner decision is named explicitly.
- The account-wide `AWSServiceRoleForEC2Fleet` disposition is recorded separately.

Raw evidence files:

## Result

Overall result: **UNVERIFIED**

Do not change this to PASS until every item above has direct evidence and the
OpenSpec task 5.5 completion audit has checked the raw outputs.
