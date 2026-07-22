"""Validated operator-owned capacity-target configuration with no AWS writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

H100_INSTANCE_GPU_COUNTS = {"p5.4xlarge": 1, "p5.48xlarge": 8}
H100_INSTANCE_TYPES = frozenset(H100_INSTANCE_GPU_COUNTS)
ACCELERATOR_CATALOG: dict[str, dict[str, tuple[str, int, int]]] = {
    "h100-production": {
        "p5.4xlarge": ("H100", 1, 1),
        "p5.48xlarge": ("H100", 8, 8),
    },
    "functional-validation": {
        "g6e.xlarge": ("L40S", 1, 0),
    },
}
VALIDATION_REGIONS = ("ap-northeast-1", "ap-northeast-2")
VALIDATION_PURPOSE_TAG = "functional-validation"
MANAGED_BY_TAG = "h100-spot-capacity-controller"
RESOURCE_NAME_PREFIX = "Phoenix-Codex-Local-Spot"


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class InstanceType:
    name: str
    spot_price_cap_usd: Decimal | None
    accelerator_model: str
    accelerator_count: int
    h100_gpu_count: int


@dataclass(frozen=True)
class Placement:
    subnet_id: str
    zone_id: str
    kind: Literal["standard", "local-zone"]


@dataclass(frozen=True)
class EksIntegration:
    cluster_arn: str
    cluster_region: str
    bootstrap_contract_version: str
    required_labels: dict[str, str]
    gpu_taint: str
    source_drain_procedure: str


@dataclass(frozen=True)
class RegionInputs:
    region: str
    launch_template_id: str
    launch_template_version: str
    standard_placements: tuple[Placement, ...]
    local_zone_placements: tuple[Placement, ...] = ()
    eks: EksIntegration | None = None
    ami_id: str | None = None
    iam_instance_profile_arn: str | None = None
    security_group_ids: tuple[str, ...] = ()
    bootstrap_contract_version: str | None = None
    user_data_sha256: str | None = None
    root_volume_encrypted: bool = True
    root_volume_kms_key_arn: str | None = None


@dataclass(frozen=True)
class RegionSelectionPolicy:
    """Operator policy for advisory or initial SPS-driven Region selection."""

    mode: Literal["manual", "recommend", "auto_initial"] = "manual"
    signal_max_age_minutes: int = 20
    decision_ttl_minutes: int = 15


@dataclass(frozen=True)
class CapacityTarget:
    target_id: str
    enabled: bool
    desired_instance_count: int
    maximum_instance_count: int
    active_region: str | None
    candidate_regions: tuple[RegionInputs, ...]
    instance_types: tuple[InstanceType, ...]
    region_selection: RegionSelectionPolicy = field(default_factory=RegionSelectionPolicy)
    accelerator_profile: Literal["h100-production", "functional-validation"] = "h100-production"
    integration_mode: Literal["standalone", "existing-eks"] = "standalone"
    price_cap_source: Literal["linux-ondemand"] = "linux-ondemand"
    zone_expansion_minutes: int = 15
    region_failover_minutes: int = 30
    failover_approval_minutes: int = 30
    capacity_rebalancing: bool = False
    excess_instance_termination: bool = False
    notification_topic_arn: str | None = None
    ownership_tags: dict[str, str] = field(default_factory=dict)

    def region_inputs(self, region: str) -> RegionInputs:
        for item in self.candidate_regions:
            if item.region == region:
                return item
        return _missing_region(region)

    @property
    def tags(self) -> dict[str, str]:
        return {
            "Name": f"{RESOURCE_NAME_PREFIX}-{self.target_id}",
            "managed-by": MANAGED_BY_TAG,
            "capacity-target-id": self.target_id,
            **self.ownership_tags,
        }

    def validate(self) -> None:
        if not self.target_id or self.desired_instance_count < 1 or self.maximum_instance_count < self.desired_instance_count:
            raise ConfigurationError("target id and valid desired/maximum machine counts are required")
        if not isinstance(self.target_id, str):
            raise ConfigurationError("target_id must be a string")
        if self.active_region is not None and not isinstance(self.active_region, str):
            raise ConfigurationError("active_region must be a string when provided")
        if self.region_selection.mode not in {"manual", "recommend", "auto_initial"}:
            raise ConfigurationError("region_selection.mode must be manual, recommend, or auto_initial")
        if min(self.region_selection.signal_max_age_minutes, self.region_selection.decision_ttl_minutes) < 1:
            raise ConfigurationError("region-selection signal age and decision TTL must be positive")
        if self.integration_mode not in {"standalone", "existing-eks"}:
            raise ConfigurationError("integration_mode must be standalone or existing-eks")
        if self.accelerator_profile not in ACCELERATOR_CATALOG:
            raise ConfigurationError("accelerator_profile must be h100-production or functional-validation")
        if min(self.zone_expansion_minutes, self.region_failover_minutes, self.failover_approval_minutes) < 1:
            raise ConfigurationError("thresholds must be positive")
        regions = [item.region for item in self.candidate_regions]
        if not regions or len(regions) != len(set(regions)):
            raise ConfigurationError("candidate regions must be non-empty and unique")
        if self.region_selection.mode == "manual" and self.active_region is None:
            raise ConfigurationError("manual region selection requires active_region")
        if self.active_region is not None and self.active_region not in regions:
            raise ConfigurationError("active_region must be one of the candidate regions")
        if not self.instance_types:
            raise ConfigurationError("at least one accelerator instance type is required")
        if len({item.name for item in self.instance_types}) != len(self.instance_types):
            raise ConfigurationError("accelerator instance types must be unique")
        catalog = ACCELERATOR_CATALOG[self.accelerator_profile]
        for instance in self.instance_types:
            if instance.name not in catalog:
                raise ConfigurationError(f"{instance.name} is not approved for {self.accelerator_profile}")
            if instance.spot_price_cap_usd is not None and (not instance.spot_price_cap_usd.is_finite() or instance.spot_price_cap_usd <= 0):
                raise ConfigurationError(f"{instance.name} requires a positive price cap")
            expected_model, expected_count, expected_h100 = catalog[instance.name]
            if (instance.accelerator_model, instance.accelerator_count, instance.h100_gpu_count) != (expected_model, expected_count, expected_h100):
                raise ConfigurationError(
                    f"{instance.name} must report {expected_count} {expected_model} accelerator(s) and {expected_h100} H100 GPU(s)"
                )
        if self.price_cap_source != "linux-ondemand":
            raise ConfigurationError("price_cap_source must be linux-ondemand")
        if self.capacity_rebalancing:
            raise ConfigurationError("capacity_rebalancing cannot exceed the machine target in the first release")
        if self.accelerator_profile == "functional-validation":
            if tuple(regions) != VALIDATION_REGIONS:
                raise ConfigurationError("functional-validation requires ordered Regions ap-northeast-1 then ap-northeast-2")
            if self.integration_mode != "standalone":
                raise ConfigurationError("functional-validation requires standalone integration")
            if self.desired_instance_count != 1 or self.maximum_instance_count != 1:
                raise ConfigurationError("functional-validation requires desired and maximum machine counts of 1")
            if tuple(item.name for item in self.instance_types) != ("g6e.xlarge",):
                raise ConfigurationError("functional-validation permits exactly g6e.xlarge")
            if any(item.local_zone_placements for item in self.candidate_regions):
                raise ConfigurationError("functional-validation does not permit Local Zone placements")
            if self.ownership_tags.get("purpose") != VALIDATION_PURPOSE_TAG:
                raise ConfigurationError("functional-validation requires ownership_tags purpose: functional-validation")
        elif self.ownership_tags.get("purpose") == VALIDATION_PURPOSE_TAG:
            raise ConfigurationError("h100-production cannot use the functional-validation purpose tag")
        for region in self.candidate_regions:
            if not region.launch_template_id or not region.launch_template_version or not region.standard_placements:
                raise ConfigurationError(f"{region.region} requires launch-template inputs and a standard-AZ placement")
            if any(p.kind != "standard" for p in region.standard_placements) or any(p.kind != "local-zone" for p in region.local_zone_placements):
                raise ConfigurationError("placement kinds do not match their lists")
            placements = (*region.standard_placements, *region.local_zone_placements)
            if any(not placement.subnet_id or not placement.zone_id for placement in placements):
                raise ConfigurationError(f"{region.region} placement identifiers must be non-empty")
            if len({placement.zone_id for placement in placements}) != len(placements) or len({placement.subnet_id for placement in placements}) != len(placements):
                raise ConfigurationError(f"{region.region} placements must use unique Zone and subnet identifiers")
            if self.enabled and not all((region.ami_id, region.iam_instance_profile_arn, region.security_group_ids, region.bootstrap_contract_version, region.user_data_sha256, region.root_volume_encrypted)):
                raise ConfigurationError(f"enabled target requires an approved AMI, instance profile, security groups, bootstrap contract, and encrypted root volume in {region.region}")
            if self.enabled and (len(str(region.user_data_sha256)) != 64 or any(character not in "0123456789abcdef" for character in str(region.user_data_sha256).lower())):
                raise ConfigurationError(f"{region.region} requires a lowercase SHA-256 UserData digest")
            if self.integration_mode == "existing-eks":
                cluster_arn_region = region.eks.cluster_arn.split(":")[3] if region.eks and len(region.eks.cluster_arn.split(":")) > 3 else None
                if region.eks is None or region.eks.cluster_region != region.region or cluster_arn_region != region.region:
                    raise ConfigurationError(f"EKS cluster for {region.region} must be configured in the same Region")
                required_label_keys = {"capacity-target-id", "topology.kubernetes.io/region", "topology.kubernetes.io/zone", "node.kubernetes.io/instance-type", "capacity-source"}
                if not all((region.eks.cluster_arn, region.eks.bootstrap_contract_version, region.eks.required_labels, region.eks.gpu_taint, region.eks.source_drain_procedure)) or not required_label_keys.issubset(region.eks.required_labels):
                    raise ConfigurationError(f"{region.region} has incomplete EKS bootstrap configuration")
                if any(not isinstance(key, str) or not isinstance(value, str) for key, value in region.eks.required_labels.items()):
                    raise ConfigurationError(f"{region.region} EKS labels must be strings")
            elif region.eks is not None:
                raise ConfigurationError("standalone targets must not declare EKS integration")
        reserved_tags = {"Name", "managed-by", "capacity-target-id"}
        if reserved_tags & set(self.ownership_tags) or any(not isinstance(key, str) or not isinstance(value, str) or not key or not value for key, value in self.ownership_tags.items()):
            raise ConfigurationError("ownership_tags cannot override reserved tags or contain empty keys/values")


def _missing_region(region: str) -> RegionInputs:
    raise ConfigurationError(f"no inputs configured for region {region}")


def _placements(items: list[dict[str, Any]] | None, kind: Literal["standard", "local-zone"]) -> tuple[Placement, ...]:
    return tuple(Placement(item["subnet_id"], item["zone_id"], kind) for item in items or [])


def _strict_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if type(value) is not bool:
        raise ConfigurationError(f"{key} must be a boolean")
    return value


def _strict_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if type(value) is not int:
        raise ConfigurationError(f"{key} must be an integer")
    return value


def _strict_string_tuple(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigurationError(f"{key} must be a list of non-empty strings")
    return tuple(value)


def target_from_mapping(raw: dict[str, Any]) -> CapacityTarget:
    try:
        accelerator_profile = raw.get("accelerator_profile", "h100-production")
        catalog = ACCELERATOR_CATALOG.get(accelerator_profile, {})
        regions = tuple(RegionInputs(
            region=item["region"], launch_template_id=item["launch_template_id"], launch_template_version=str(item["launch_template_version"]),
            standard_placements=_placements(item.get("standard_placements"), "standard"), local_zone_placements=_placements(item.get("local_zone_placements"), "local-zone"),
            eks=EksIntegration(**item["eks"]) if item.get("eks") else None,
            ami_id=item.get("ami_id"), iam_instance_profile_arn=item.get("iam_instance_profile_arn"),
            security_group_ids=_strict_string_tuple(item, "security_group_ids"), bootstrap_contract_version=item.get("bootstrap_contract_version"),
            user_data_sha256=item.get("user_data_sha256"),
            root_volume_encrypted=_strict_bool(item, "root_volume_encrypted", True), root_volume_kms_key_arn=item.get("root_volume_kms_key_arn"),
        ) for item in raw["candidate_regions"])
        types = []
        for item in raw["instance_types"]:
            expected_model, expected_count, expected_h100 = catalog.get(item["name"], ("unknown", 0, 0))
            model = item.get("accelerator_model", expected_model)
            if not isinstance(model, str):
                raise ConfigurationError("accelerator_model must be a string")
            types.append(InstanceType(
                item["name"], Decimal(str(item["spot_price_cap_usd"])) if item.get("spot_price_cap_usd") is not None else None,
                model,
                _strict_int(item, "accelerator_count", expected_count),
                _strict_int(item, "h100_gpu_count", expected_h100),
            ))
        selection_raw = raw.get("region_selection", {})
        if not isinstance(selection_raw, dict):
            raise ConfigurationError("region_selection must be a mapping")
        selection = RegionSelectionPolicy(
            mode=selection_raw.get("mode", "manual"),
            signal_max_age_minutes=_strict_int(selection_raw, "signal_max_age_minutes", 20),
            decision_ttl_minutes=_strict_int(selection_raw, "decision_ttl_minutes", 15),
        )
        target = CapacityTarget(
            target_id=raw["target_id"], enabled=_strict_bool(raw, "enabled", False), desired_instance_count=_strict_int(raw, "desired_instance_count", 1),
            maximum_instance_count=_strict_int(raw, "maximum_instance_count", 1), active_region=raw.get("active_region"), candidate_regions=regions, instance_types=tuple(types),
            region_selection=selection,
            accelerator_profile=accelerator_profile,
            integration_mode=raw.get("integration_mode", "standalone"), price_cap_source=raw.get("price_cap_source", "linux-ondemand"),
            zone_expansion_minutes=_strict_int(raw, "zone_expansion_minutes", 15),
            region_failover_minutes=_strict_int(raw, "region_failover_minutes", 30), failover_approval_minutes=_strict_int(raw, "failover_approval_minutes", 30),
            capacity_rebalancing=_strict_bool(raw, "capacity_rebalancing", False), excess_instance_termination=_strict_bool(raw, "excess_instance_termination", False),
            notification_topic_arn=raw.get("notification_topic_arn"), ownership_tags=dict(raw.get("ownership_tags", {})),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise ConfigurationError(f"invalid target configuration: {error}") from error
    target.validate()
    return target


def load_target_mapping(path: str | Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load YAML target configuration")
    with Path(path).open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ConfigurationError("target configuration must be a mapping")
    target_from_mapping(raw)
    return raw


def load_target(path: str | Path) -> CapacityTarget:
    return target_from_mapping(load_target_mapping(path))
