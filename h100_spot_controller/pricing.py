"""Read Linux On-Demand prices used strictly as Spot price ceilings."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


class PricingError(RuntimeError):
    pass


def linux_ondemand_hourly_price(pricing: Any, region: str, instance_type: str) -> Decimal:
    response = pricing.get_products(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
            {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        ],
        MaxResults=1,
    )
    if not response.get("PriceList"):
        raise PricingError(f"no Linux On-Demand price found for {instance_type} in {region}")
    product = json.loads(response["PriceList"][0])
    terms = product.get("terms", {}).get("OnDemand", {})
    for term in terms.values():
        for dimension in term.get("priceDimensions", {}).values():
            if dimension.get("unit") == "Hrs":
                return Decimal(dimension["pricePerUnit"]["USD"])
    raise PricingError(f"no hourly Linux On-Demand price found for {instance_type} in {region}")


def ondemand_caps(pricing: Any, region: str, instance_types: tuple[str, ...]) -> dict[str, Decimal]:
    return {instance_type: linux_ondemand_hourly_price(pricing, region, instance_type) for instance_type in instance_types}
