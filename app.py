# This is the entry point for the CDK app
# It loads the configuration from config.yml and creates the FashionAgentStack

import os
from pathlib import Path
import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks, NagSuppressions
import yaml
from components.stacks.fashion_agent_stack import FashionAgentStack


# Load the configuration from config.yml
with open(os.path.join(Path(__file__).parent, "config.yml"), "r") as ymlfile:
    stack_config = yaml.load(ymlfile, Loader=yaml.loader.SafeLoader)


current_file_path = Path(__file__).resolve()

# Create the CDK app and environment
app = cdk.App()
env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"), region=os.getenv("CDK_DEFAULT_REGION")
)

# Create the FashionAgentStack with the loaded configuration
stack = FashionAgentStack(
    scope=app, stack_name=stack_config["stack_name"], config=stack_config, env=env
)

NagSuppressions.add_stack_suppressions(
    stack,
    [
        {"id": "AwsSolutions-IAM5", "reason": "Need the wildcard for CloudWatch logs so the S3 Vectors custom-resource provider Lambda can create several log streams"},
        {"id": "AwsSolutions-IAM4", "reason": "The S3 Vectors custom-resource provider Lambdas use the AWS-managed basic execution role (standard CDK provider pattern)"},
        {"id": "AwsSolutions-L1", "reason": "The custom_resources.Provider framework pins its own Lambda runtime (CDK-managed, not user-selectable)"},
    ],
    True,
)


cdk.Aspects.of(app).add(AwsSolutionsChecks())
# Synthesize the AWS CloudFormation template for the stack
app.synth()
