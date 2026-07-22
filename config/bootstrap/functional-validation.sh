#!/bin/bash
set -euo pipefail

systemctl enable --now amazon-ssm-agent
mkdir -p /var/lib/h100-spot-functional-validation
nvidia-smi --query-gpu=name,uuid,memory.total,driver_version --format=csv \
  > /var/lib/h100-spot-functional-validation/nvidia-smi.csv
date --iso-8601=seconds > /var/lib/h100-spot-functional-validation/bootstrap-complete-at
