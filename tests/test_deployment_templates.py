from hashlib import sha256
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class CloudFormationLoader(yaml.SafeLoader):
    pass


def _construct_intrinsic(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag_suffix: value}


CloudFormationLoader.add_multi_constructor("!", _construct_intrinsic)


def load_template(name):
    return yaml.load(
        (ROOT / "deployment" / name).read_text(),
        Loader=CloudFormationLoader,
    )


class ValidationDeploymentTemplateTests(unittest.TestCase):
    def test_instance_role_is_dedicated_and_least_privilege(self):
        template = load_template("validation-instance-role.yaml")
        resources = template["Resources"]
        role = resources["ValidationInstanceRole"]["Properties"]
        profile = resources["ValidationInstanceProfile"]["Properties"]

        self.assertEqual("Phoenix-Codex-Local-Spot-Validation-Instance", role["RoleName"])
        self.assertEqual(
            ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"],
            role["ManagedPolicyArns"],
        )
        self.assertNotIn("Policies", role)
        statement = role["AssumeRolePolicyDocument"]["Statement"]
        self.assertEqual(
            [{
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
            statement,
        )
        self.assertEqual("Phoenix-Codex-Local-Spot-Validation-Instance", profile["InstanceProfileName"])
        self.assertEqual([{"Ref": "ValidationInstanceRole"}], profile["Roles"])

    def test_launch_template_preserves_bounded_contract(self):
        template = load_template("validation-launch-template.yaml")
        resources = template["Resources"]
        security_group = resources["ValidationSecurityGroup"]["Properties"]
        launch = resources["ValidationLaunchTemplate"]["Properties"]
        data = launch["LaunchTemplateData"]

        self.assertEqual([], security_group["SecurityGroupIngress"])
        self.assertTrue(security_group["GroupName"].startswith("Phoenix-Codex-Local-Spot-"))
        self.assertTrue(launch["LaunchTemplateName"].startswith("Phoenix-Codex-Local-Spot-"))
        self.assertEqual(
            [{
                "IpProtocol": "-1",
                "CidrIp": "0.0.0.0/0",
                "Description": "Outbound access for SSM and AWS services",
            }],
            security_group["SecurityGroupEgress"],
        )
        self.assertNotIn("InstanceType", data)
        self.assertNotIn("SubnetId", data)
        self.assertNotIn("KeyName", data)
        self.assertEqual("required", data["MetadataOptions"]["HttpTokens"])
        root = data["BlockDeviceMappings"][0]["Ebs"]
        self.assertTrue(root["Encrypted"])
        self.assertTrue(root["DeleteOnTermination"])
        self.assertEqual("gp3", root["VolumeType"])
        self.assertEqual(30, root["VolumeSize"])

        script = (ROOT / "config" / "bootstrap" / "functional-validation.sh").read_text()
        self.assertEqual(script, data["UserData"]["Fn::Base64"])
        target = yaml.safe_load((ROOT / "config" / "validation-target.example.yaml").read_text())
        expected_hash = sha256(script.encode()).hexdigest()
        for region in target["candidate_regions"]:
            self.assertEqual(expected_hash, region["user_data_sha256"])

        tag_specs = {item["ResourceType"]: item["Tags"] for item in data["TagSpecifications"]}
        for resource_type in ("instance", "volume"):
            tags = tag_specs[resource_type]
            self.assertIn({"Key": "Name", "Value": "Phoenix-Codex-Local-Spot-g6e-functional-validation"}, tags)
            self.assertIn({"Key": "managed-by", "Value": "h100-spot-capacity-controller"}, tags)
            self.assertIn({"Key": "purpose", "Value": "functional-validation"}, tags)
            self.assertIn(
                {"Key": "bootstrap-contract-version", "Value": "standalone-validation-v1"},
                tags,
            )


if __name__ == "__main__":
    unittest.main()
