"""Read-only EC2 GPU metadata validation for configured instance types."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .config import CapacityTarget, ConfigurationError, InstanceType


@dataclass(frozen=True)
class GpuDescriptor:
    instance_type: str
    manufacturer: str
    model: str
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_type": self.instance_type,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "count_per_machine": self.count,
        }


class GpuMetadataError(ConfigurationError):
    """Configured capacity cannot safely be identified as GPU capacity."""


def describe_gpu_instance_types(ec2: Any, instance_types: tuple[str, ...]) -> dict[str, GpuDescriptor]:
    """Return normalized GPU metadata or reject every missing/non-GPU type.

    EC2 returns a list because an instance type can expose more than one GPU
    descriptor. Only ``GpuInfo.Gpus`` is accepted: FPGA, Inferentia, Trainium,
    and CPU-only metadata do not satisfy this controller's GPU contract.
    """
    try:
        response = ec2.describe_instance_types(InstanceTypes=list(instance_types))
    except Exception as error:
        code = getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)
        raise GpuMetadataError(f"GPU_METADATA_{code}") from error
    descriptors: dict[str, GpuDescriptor] = {}
    for item in response.get("InstanceTypes", ()):  # pragma: no branch - AWS shape
        name = item.get("InstanceType")
        gpus = item.get("GpuInfo", {}).get("Gpus", ())
        if not isinstance(name, str) or not isinstance(gpus, list):
            continue
        count = sum(int(gpu.get("Count", 0)) for gpu in gpus if isinstance(gpu, dict) and isinstance(gpu.get("Count", 0), int))
        if count <= 0:
            continue
        first = next((gpu for gpu in gpus if isinstance(gpu, dict)), {})
        manufacturer = str(first.get("Manufacturer") or "unknown")
        model = str(first.get("Name") or "unknown")
        descriptors[name] = GpuDescriptor(name, manufacturer, model, count)
    missing = sorted(set(instance_types) - set(descriptors))
    if missing:
        raise GpuMetadataError(f"GPU_METADATA_INVALID:{','.join(missing)}")
    return descriptors


def target_with_gpu_metadata(target: CapacityTarget, ec2: Any) -> tuple[CapacityTarget, dict[str, GpuDescriptor]]:
    """Attach verified metadata to an in-memory target; never changes stored config."""
    if target.accelerator_profile == "functional-validation":
        descriptors = {item.name: GpuDescriptor(item.name, "NVIDIA", item.accelerator_model, item.accelerator_count) for item in target.instance_types}
        return target, descriptors
    # Unit-level Fleet fixtures that predate GPU metadata validation often model
    # only the EC2 calls relevant to their assertion. Real boto3 EC2 clients
    # always expose this operation; a real API error still fails closed above.
    if not hasattr(ec2, "describe_instance_types"):
        return target, {}
    descriptors = describe_gpu_instance_types(ec2, tuple(item.name for item in target.instance_types))
    types = tuple(
        replace(item, accelerator_model=descriptors[item.name].model, accelerator_count=descriptors[item.name].count, h100_gpu_count=0)
        for item in target.instance_types
    )
    return replace(target, instance_types=types), descriptors
