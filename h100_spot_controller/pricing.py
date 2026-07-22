"""Read Linux On-Demand prices used strictly as Spot price ceilings."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any


class PricingError(RuntimeError):
    pass


_PRODUCT_FILTERS = {
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "capacitystatus": "Used",
    "preInstalledSw": "NA",
    "marketoption": "OnDemand",
    "operation": "RunInstances",
}


def _matching_hourly_prices(raw_product: str) -> set[Decimal]:
    try:
        product = json.loads(raw_product)
    except (TypeError, json.JSONDecodeError):
        return set()
    if not isinstance(product, dict):
        return set()

    product_details = product.get("product", {})
    if not isinstance(product_details, dict):
        return set()
    attributes = product_details.get("attributes", {})
    if not isinstance(attributes, dict):
        return set()
    for field, expected in _PRODUCT_FILTERS.items():
        if field in attributes and attributes[field] != expected:
            return set()

    prices: set[Decimal] = set()
    all_terms = product.get("terms", {})
    if not isinstance(all_terms, dict):
        return prices
    terms = all_terms.get("OnDemand", {})
    if not isinstance(terms, dict):
        return prices
    for term in terms.values():
        if not isinstance(term, dict):
            continue
        dimensions = term.get("priceDimensions", {})
        if not isinstance(dimensions, dict):
            continue
        for dimension in dimensions.values():
            if not isinstance(dimension, dict) or dimension.get("unit") != "Hrs":
                continue
            price_per_unit = dimension.get("pricePerUnit", {})
            if not isinstance(price_per_unit, dict):
                continue
            usd = price_per_unit.get("USD")
            try:
                price = Decimal(str(usd))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if price.is_finite() and price > 0:
                prices.add(price)
    return prices


def linux_ondemand_hourly_price(pricing: Any, region: str, instance_type: str) -> Decimal:
    request = {
        "ServiceCode": "AmazonEC2",
        "Filters": [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
            *(
                {"Type": "TERM_MATCH", "Field": field, "Value": value}
                for field, value in _PRODUCT_FILTERS.items()
            ),
        ],
        "MaxResults": 100,
    }
    prices: set[Decimal] = set()
    next_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        page_request = dict(request)
        if next_token is not None:
            page_request["NextToken"] = next_token
        response = pricing.get_products(**page_request)
        for raw_product in response.get("PriceList") or []:
            prices.update(_matching_hourly_prices(raw_product))

        next_token = response.get("NextToken")
        if not next_token:
            break
        if next_token in seen_tokens:
            raise PricingError(f"repeated Pricing API token for {instance_type} in {region}")
        seen_tokens.add(next_token)

    if not prices:
        raise PricingError(f"no positive Linux On-Demand RunInstances hourly price found for {instance_type} in {region}")
    if len(prices) > 1:
        raise PricingError(f"conflicting Linux On-Demand RunInstances hourly prices found for {instance_type} in {region}")
    return prices.pop()


def ondemand_caps(pricing: Any, region: str, instance_types: tuple[str, ...]) -> dict[str, Decimal]:
    return {instance_type: linux_ondemand_hourly_price(pricing, region, instance_type) for instance_type in instance_types}
