#!/usr/bin/env python3
"""Bitbucket pipe: run AWS SSM commands on EC2 instances during deployment.

Supports two authentication paths, mirroring the official Atlassian AWS pipes:

1. OIDC (``AWS_OIDC_ROLE_ARN``): the step must set ``oidc: true`` so Bitbucket
   exposes ``BITBUCKET_STEP_OIDC_TOKEN``. The token is written to a file and the
   role is assumed via ``AssumeRoleWithWebIdentity`` by the default boto3 chain.
2. Static keys (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` and optional
   ``AWS_SESSION_TOKEN``).

If neither is provided the default boto3 credential chain is used (host env vars
or an instance profile).
"""
import os
import sys
import tempfile
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from bitbucket_pipes_toolkit import Pipe, get_logger

logger = get_logger()

# Terminal SSM invocation statuses (no further polling needed once reached).
TERMINAL_STATUSES = {"Success", "Cancelled", "TimedOut", "Failed", "Undeliverable", "Terminated"}

schema = {
    # --- Authentication ---
    "AWS_ACCESS_KEY_ID": {"type": "string", "required": False},
    "AWS_SECRET_ACCESS_KEY": {"type": "string", "required": False},
    "AWS_SESSION_TOKEN": {"type": "string", "required": False},
    "AWS_OIDC_ROLE_ARN": {"type": "string", "required": False},
    "AWS_DEFAULT_REGION": {"type": "string", "required": True},
    # --- Target & command ---
    "INSTANCE_IDS": {"type": "list", "required": True, "empty": False},
    "DOCUMENT_NAME": {"type": "string", "required": False, "default": "AWS-RunShellScript"},
    "COMMANDS": {"type": "list", "required": False},
    "SCRIPT": {"type": "string", "required": False},
    "RUN_AS": {"type": "string", "required": False},
    "COMMENT": {"type": "string", "required": False, "default": ""},
    # --- Behaviour ---
    "WAIT": {"type": "boolean", "required": False, "default": True},
    "WAIT_TIMEOUT": {"type": "integer", "required": False, "default": 600},
    "POLL_INTERVAL": {"type": "integer", "required": False, "default": 5},
    "FAIL_ON_ERROR": {"type": "boolean", "required": False, "default": True},
    "DEBUG": {"type": "boolean", "required": False, "default": False},
}


class AWSSSMDeployPipe(Pipe):
    def run(self):
        super().run()

        region = self.get_variable("AWS_DEFAULT_REGION")

        commands = self.build_commands()
        self.configure_auth()

        try:
            ssm = boto3.client("ssm", region_name=region)
        except (BotoCoreError, ClientError) as error:
            self.fail(f"Failed to create SSM client: {error}")

        command_id = self.send_command(ssm, commands)

        if not self.get_variable("WAIT"):
            self.success(f"SSM command dispatched (CommandId={command_id}). Not waiting for completion.")
            return

        self.wait_and_report(ssm, command_id)

    # -- Authentication ------------------------------------------------------

    def configure_auth(self):
        """Set up credentials in the environment for the default boto3 chain.

        Precedence: OIDC role > static keys > existing environment / instance
        profile. boto3 reads ``AWS_*`` env vars natively, so we only need to
        populate them; the SDK does the actual STS calls.
        """
        oidc_role_arn = self.get_variable("AWS_OIDC_ROLE_ARN")
        access_key = self.get_variable("AWS_ACCESS_KEY_ID")
        secret_key = self.get_variable("AWS_SECRET_ACCESS_KEY")
        session_token = self.get_variable("AWS_SESSION_TOKEN")

        if oidc_role_arn:
            oidc_token = os.environ.get("BITBUCKET_STEP_OIDC_TOKEN")
            if not oidc_token:
                self.fail(
                    "AWS_OIDC_ROLE_ARN is set but BITBUCKET_STEP_OIDC_TOKEN is missing. "
                    "Add `oidc: true` to the pipeline step."
                )
            token_file = tempfile.NamedTemporaryFile(
                mode="w", prefix="web-identity-token-", delete=False
            )
            token_file.write(oidc_token)
            token_file.close()
            os.environ["AWS_ROLE_ARN"] = oidc_role_arn
            os.environ["AWS_WEB_IDENTITY_TOKEN_FILE"] = token_file.name
            # Avoid leftover static creds shadowing the web-identity flow.
            for stale in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
                os.environ.pop(stale, None)
            logger.info("Authenticating via OIDC web identity (role %s).", oidc_role_arn)
            return

        if access_key and secret_key:
            os.environ["AWS_ACCESS_KEY_ID"] = access_key
            os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key
            if session_token:
                os.environ["AWS_SESSION_TOKEN"] = session_token
            logger.info("Authenticating via static access keys.")
            return

        logger.info(
            "No explicit credentials provided; using default AWS credential chain "
            "(environment / instance profile)."
        )

    # -- Command assembly ----------------------------------------------------

    def build_commands(self):
        """Resolve the shell command list from COMMANDS or SCRIPT (+ RUN_AS)."""
        commands = self.get_variable("COMMANDS")
        script = self.get_variable("SCRIPT")

        if commands and script:
            self.fail("Provide either COMMANDS or SCRIPT, not both.")
        if not commands and not script:
            self.fail("One of COMMANDS or SCRIPT is required.")

        if commands:
            return list(commands)

        run_as = self.get_variable("RUN_AS")
        invocation = f"sudo -iu {run_as} {script}" if run_as else script
        return ["set -e", invocation]

    # -- SSM calls -----------------------------------------------------------

    def send_command(self, ssm, commands):
        instance_ids = [str(i).strip() for i in self.get_variable("INSTANCE_IDS")]
        document_name = self.get_variable("DOCUMENT_NAME")
        comment = self.get_variable("COMMENT") or self.default_comment()

        logger.info("Sending SSM command to %s using %s.", ", ".join(instance_ids), document_name)
        try:
            response = ssm.send_command(
                InstanceIds=instance_ids,
                DocumentName=document_name,
                Comment=comment[:100],
                Parameters={"commands": commands},
            )
        except (BotoCoreError, ClientError) as error:
            self.fail(f"ssm send-command failed: {error}")

        command_id = response["Command"]["CommandId"]
        logger.info("SSM command id: %s", command_id)
        return command_id

    def wait_and_report(self, ssm, command_id):
        instance_ids = [str(i).strip() for i in self.get_variable("INSTANCE_IDS")]
        timeout = self.get_variable("WAIT_TIMEOUT")
        poll_interval = self.get_variable("POLL_INTERVAL")
        fail_on_error = self.get_variable("FAIL_ON_ERROR")

        deadline = time.monotonic() + timeout
        results = {}

        for instance_id in instance_ids:
            status = self.poll_invocation(ssm, command_id, instance_id, deadline, poll_interval)
            results[instance_id] = status

        failed = [iid for iid, status in results.items() if status != "Success"]
        if failed and fail_on_error:
            self.fail(
                "SSM command did not succeed on: "
                + ", ".join(f"{iid} ({results[iid]})" for iid in failed)
            )

        self.success("SSM command completed on all instances.")

    def poll_invocation(self, ssm, command_id, instance_id, deadline, poll_interval):
        while True:
            try:
                invocation = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
            except ClientError as error:
                # The invocation record can lag right after send-command.
                if error.response["Error"]["Code"] == "InvocationDoesNotExist" and time.monotonic() < deadline:
                    time.sleep(poll_interval)
                    continue
                self.fail(f"get-command-invocation failed for {instance_id}: {error}")
            except BotoCoreError as error:
                self.fail(f"get-command-invocation failed for {instance_id}: {error}")

            status = invocation["Status"]
            if status in TERMINAL_STATUSES:
                self.report_invocation(instance_id, invocation)
                return status

            if time.monotonic() >= deadline:
                logger.warning("Timed out waiting for %s (last status: %s).", instance_id, status)
                self.report_invocation(instance_id, invocation)
                return "TimedOut"

            time.sleep(poll_interval)

    def report_invocation(self, instance_id, invocation):
        status = invocation["Status"]
        stdout = invocation.get("StandardOutputContent", "")
        stderr = invocation.get("StandardErrorContent", "")
        log = logger.info if status == "Success" else logger.error
        log("[%s] Status: %s", instance_id, status)
        if stdout:
            log("[%s] Stdout:\n%s", instance_id, stdout)
        if stderr:
            log("[%s] Stderr:\n%s", instance_id, stderr)

    def default_comment(self):
        build_number = os.environ.get("BITBUCKET_BUILD_NUMBER")
        return f"Bitbucket build {build_number}" if build_number else "Bitbucket SSM deploy"


def main():
    # The namespace is injected at build time; fall back to a neutral default
    # so the pipe runs locally without a hardcoded registry namespace.
    image = os.environ.get("PIPE_IMAGE", "aws-ssm-deploy:1.0.0")
    metadata = {"name": "AWS SSM Deploy", "image": image}
    pipe = AWSSSMDeployPipe(pipe_metadata=metadata, schema=schema)
    pipe.run()


if __name__ == "__main__":
    sys.exit(main())
