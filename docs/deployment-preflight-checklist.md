# Deployment preflight checklist

Use this checklist before deploying or enabling the H100 Spot Capacity Controller.
Deployment and capacity creation require explicit operator authorization.

## 1. Local verification

- [ ] Create and activate a Python 3.11+ environment.
- [ ] Install the locked project dependencies.
- [ ] Run `python -m unittest discover -s tests -q` successfully.
- [ ] Run `openspec validate --specs --strict` successfully.
- [ ] Start from `config/target.example.yaml`; do not commit reviewed account-specific target files.

## 2. Read-only AWS review

- [ ] Confirm the intended AWS profile and account ID.
- [ ] Run `h100-spot-controller validate-target CONFIG` and `dry-run CONFIG --profile PROFILE`.
- [ ] Run `discover CONFIG --profile PROFILE` and `capacity-review CONFIG --profile PROFILE`.
- [ ] Verify the H100 allowlist, approved candidate Regions, standard-AZ order, and optional Local Zone prerequisites.
- [ ] Review current quotas, offerings, SPS evidence, Spot price caps, and Linux On-Demand prices.
- [ ] Verify each launch contract: AMI, exact instance-profile ARN, encrypted root volume, IMDSv2, security groups, UserData hash, and ownership tags.

## 3. Control-plane deployment review

- [ ] Obtain an authorization that names the AWS resources permitted to be created or changed.
- [ ] Synthesize and review the CloudFormation/CDK change set.
- [ ] Restrict `LaunchTemplateArns` and `LaunchInstanceRoleArns` to the approved per-Region resources; do not use wildcard `iam:PassRole`.
- [ ] Verify DynamoDB encryption and point-in-time recovery, EventBridge schedules, SNS subscription, Lambda log retention, CloudWatch alarms, dashboard, and IAM policy boundaries.
- [ ] Persist an initial target with `enabled: false`, then review the stored version before enabling capacity.

## 4. Capacity enablement and operations

- [ ] Obtain separate approval to enable capacity, including desired/max **machine** counts and price caps.
- [ ] For `auto_initial`, confirm fresh SPS/offerings/quota/price evidence and that no controller-owned capacity exists in any candidate Region.
- [ ] For cross-Region movement, create a whole-target plan, review it, and obtain an unexpired explicit approval before any source mutation.
- [ ] Monitor Fleet, instances, EBS, public IPv4 (if any), Lambda, CloudWatch, and SNS costs.
- [ ] Keep a cleanup plan: disable the target first, then separately approve Fleet cancellation and instance termination.
