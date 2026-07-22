#!/usr/bin/env python3
import aws_cdk as cdk

from h100_spot_controller.infrastructure import CapacityControllerStack

app = cdk.App()
CapacityControllerStack(app, "Phoenix-Codex-Local-Spot-Controller")
app.synth()
