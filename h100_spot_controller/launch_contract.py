"""Read-only validation of operator-owned Launch Template contracts."""

from __future__ import annotations

from dataclasses import dataclass
import base64
from hashlib import sha256
from typing import Any

from .config import CapacityTarget, RegionInputs


@dataclass(frozen=True)
class LaunchContractInspection:
    region: str
    launch_template_id: str
    version: str
    valid: bool
    violations: tuple[str, ...]


def _tag_map(data: dict[str, Any], resource_type: str) -> dict[str, str]:
    for specification in data.get("TagSpecifications", []):
        if specification.get("ResourceType") == resource_type:
            return {item["Key"]: item["Value"] for item in specification.get("Tags", [])}
    return {}


def inspect_launch_contract(ec2: Any, target: CapacityTarget, inputs: RegionInputs) -> LaunchContractInspection:
    response = ec2.describe_launch_template_versions(
        LaunchTemplateId=inputs.launch_template_id,
        Versions=[inputs.launch_template_version],
    )
    versions = response.get("LaunchTemplateVersions", [])
    if len(versions) != 1:
        return LaunchContractInspection(inputs.region, inputs.launch_template_id, inputs.launch_template_version, False, ("launch-template-version-not-found",))
    data = versions[0].get("LaunchTemplateData", {})
    violations: list[str] = []
    if data.get("ImageId") != inputs.ami_id:
        violations.append("ami-mismatch")
    profile = data.get("IamInstanceProfile", {})
    if profile.get("Arn") != inputs.iam_instance_profile_arn:
        violations.append("instance-profile-mismatch")
    security_groups = set(data.get("SecurityGroupIds", ()))
    for interface in data.get("NetworkInterfaces", []):
        security_groups.update(interface.get("Groups", ()))
    if security_groups != set(inputs.security_group_ids):
        violations.append("security-groups-mismatch")
    if data.get("MetadataOptions", {}).get("HttpTokens") != "required":
        violations.append("imdsv2-not-required")
    try:
        user_data_hash = sha256(base64.b64decode(data.get("UserData", ""), validate=True)).hexdigest()
    except Exception:
        user_data_hash = "invalid"
    if user_data_hash != inputs.user_data_sha256:
        violations.append("user-data-mismatch")
    ebs_mappings = [item["Ebs"] for item in data.get("BlockDeviceMappings", []) if item.get("Ebs")]
    if inputs.root_volume_encrypted and (not ebs_mappings or any(item.get("Encrypted") is not True for item in ebs_mappings)):
        violations.append("ebs-encryption-not-explicit")
    if not ebs_mappings or ebs_mappings[0].get("DeleteOnTermination") is not True:
        violations.append("root-volume-delete-on-termination-not-explicit")
    if inputs.root_volume_kms_key_arn and any(item.get("KmsKeyId") != inputs.root_volume_kms_key_arn for item in ebs_mappings):
        violations.append("kms-key-mismatch")
    required_tags = {**target.tags, "bootstrap-contract-version": str(inputs.bootstrap_contract_version)}
    for resource_type in ("instance", "volume"):
        tags = _tag_map(data, resource_type)
        if any(tags.get(key) != value for key, value in required_tags.items()):
            violations.append(f"{resource_type}-ownership-tags-mismatch")
    return LaunchContractInspection(
        inputs.region, inputs.launch_template_id, inputs.launch_template_version,
        not violations, tuple(violations),
    )
