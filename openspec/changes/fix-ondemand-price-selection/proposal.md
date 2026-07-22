## Why

The AWS Pricing API can return several EC2 SKUs for the same instance type and Region, including Capacity Block and reservation products with a zero hourly dimension. The current `MaxResults=1` behavior can therefore select `$0` instead of the matching positive Linux On-Demand `RunInstances` price, causing valid GPU targets to fail closed or use incorrect readiness evidence.

## What Changes

- Select only matching Linux, shared-tenancy, no-preinstalled-software, used-capacity, On-Demand `RunInstances` products.
- Inspect all returned pages and accept only positive USD hourly dimensions.
- Deterministically accept repeated identical valid prices and fail closed when no valid price or conflicting positive prices remain.
- Add focused regression tests without changing Fleet behavior, Region selection policy, EKS integration, Local Zone fallback, or GPU instance-type support.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `spot-capacity-safety`: Tighten the existing Linux On-Demand Spot-ceiling contract so only a uniquely resolved positive `RunInstances` On-Demand hourly price is accepted.

## Impact

- Affects `h100_spot_controller/pricing.py` and its unit tests.
- Does not change configuration shape, AWS infrastructure, IAM permissions, Fleet capacity, or deployment state.
