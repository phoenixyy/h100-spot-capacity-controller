## Context

`linux_ondemand_hourly_price` asks the AWS Price List API for one product matching broad Linux EC2 attributes and immediately reads its first hourly On-Demand dimension. The API may return multiple commercial products for those attributes, and ordering is not a contract. A zero-priced Capacity Block or reservation-related SKU can therefore precede the standard On-Demand `RunInstances` SKU.

The price is used only as a maximum Spot bid ceiling and as Region-readiness evidence. An unresolved or ambiguous price must remain fail-closed before any Fleet write.

## Goals / Non-Goals

**Goals:**

- Resolve the standard positive Linux On-Demand `RunInstances` hourly price deterministically.
- Handle Price List pagination and unrelated/zero-priced SKUs safely.
- Preserve existing caller APIs and fail-closed behavior.

**Non-Goals:**

- Changing Spot Fleet construction, price-cap semantics, Region ranking, Local Zone fallback, EKS integration, or GPU validation.
- Creating or modifying AWS resources.
- Supporting non-Linux, dedicated tenancy, preinstalled software, Savings Plans, Capacity Blocks, or reservation prices.

## Decisions

1. Add `marketoption=OnDemand` and `operation=RunInstances` to the existing server-side product filters. This narrows the response using AWS product attributes while retaining defensive client-side validation because API result shape and test doubles can still contain unrelated entries.
2. Follow `NextToken` until exhausted instead of using `MaxResults=1`. Each product is decoded independently; malformed entries, products whose attributes contradict the requested contract, non-OnDemand terms, non-hourly dimensions, non-USD dimensions, and non-positive prices are ignored.
3. Collect valid prices into a set. One unique positive value is accepted, repeated equal values are harmless, zero values are never ceilings, and zero or multiple unique valid values raise `PricingError`.
4. Keep the public function signatures unchanged. Existing reconciliation, dry-run, and Region-selection callers continue to fail closed through their current error paths.

## Risks / Trade-offs

- [More API pages may increase latency] → Use the Pricing API's normal page size and stop at `NextToken`; server-side filters keep the result small.
- [AWS catalog attributes may evolve] → Validate known matching attributes when present, while relying on the exact request filters and term/dimension checks for minimal fixtures and backwards-compatible tests.
- [Conflicting legitimate prices can temporarily block capacity] → Fail closed and expose the ambiguity rather than selecting an arbitrary spending ceiling.

