## 1. Price resolution

- [x] 1.1 Narrow Pricing API filters to the standard On-Demand `RunInstances` product contract and follow all response pages.
- [x] 1.2 Parse products defensively, ignore invalid/non-positive/non-matching hourly dimensions, deduplicate identical prices, and fail closed on missing or conflicting prices.

## 2. Verification

- [x] 2.1 Add regression tests for zero-price unrelated products, pagination, duplicate equal prices, missing prices, conflicting prices, and request filters.
- [x] 2.2 Run focused tests, the complete test suite, and strict OpenSpec validation.
