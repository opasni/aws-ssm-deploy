import os
import sys
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipe"))

from pipe import AWSSSMDeployPipe, schema  # noqa: E402

METADATA = {"name": "AWS SSM Deploy", "image": "test/aws-ssm-deploy:test"}


def build_pipe(monkeypatch, extra_env, oidc_token=None):
    """Construct the pipe with a clean, controlled environment."""
    for key in list(os.environ):
        if key.startswith(("AWS_", "INSTANCE_IDS", "COMMANDS", "SCRIPT", "RUN_AS",
                           "DOCUMENT_NAME", "WAIT", "COMMENT", "POLL_INTERVAL",
                           "FAIL_ON_ERROR", "DEBUG", "BITBUCKET_")):
            monkeypatch.delenv(key, raising=False)

    base = {"AWS_DEFAULT_REGION": "eu-central-1", "INSTANCE_IDS_COUNT": "1",
            "INSTANCE_IDS_0": "i-123"}
    base.update(extra_env)
    for key, value in base.items():
        monkeypatch.setenv(key, str(value))
    if oidc_token is not None:
        monkeypatch.setenv("BITBUCKET_STEP_OIDC_TOKEN", oidc_token)

    return AWSSSMDeployPipe(pipe_metadata=METADATA, schema=schema)


# --- Command assembly -------------------------------------------------------

def test_build_commands_from_commands_list(monkeypatch):
    pipe = build_pipe(monkeypatch, {"COMMANDS_COUNT": "2", "COMMANDS_0": "echo hi",
                                    "COMMANDS_1": "ls -la"})
    assert pipe.build_commands() == ["echo hi", "ls -la"]


def test_build_commands_from_script_with_run_as(monkeypatch):
    pipe = build_pipe(monkeypatch, {"SCRIPT": "/var/www/deploy.sh", "RUN_AS": "ubuntu"})
    assert pipe.build_commands() == ["set -e", "sudo -iu ubuntu /var/www/deploy.sh"]


def test_build_commands_from_script_without_run_as(monkeypatch):
    pipe = build_pipe(monkeypatch, {"SCRIPT": "/var/www/deploy.sh"})
    assert pipe.build_commands() == ["set -e", "/var/www/deploy.sh"]


def test_build_commands_rejects_both(monkeypatch):
    pipe = build_pipe(monkeypatch, {"SCRIPT": "/x.sh", "COMMANDS_COUNT": "1",
                                    "COMMANDS_0": "echo hi"})
    with pytest.raises(SystemExit):
        pipe.build_commands()


def test_build_commands_requires_one(monkeypatch):
    pipe = build_pipe(monkeypatch, {})
    with pytest.raises(SystemExit):
        pipe.build_commands()


# --- Authentication ---------------------------------------------------------

def test_configure_auth_oidc(monkeypatch):
    pipe = build_pipe(
        monkeypatch,
        {"SCRIPT": "/x.sh", "AWS_OIDC_ROLE_ARN": "arn:aws:iam::1:role/deploy"},
        oidc_token="token-value",
    )
    pipe.configure_auth()
    assert os.environ["AWS_ROLE_ARN"] == "arn:aws:iam::1:role/deploy"
    token_file = os.environ["AWS_WEB_IDENTITY_TOKEN_FILE"]
    with open(token_file) as handle:
        assert handle.read() == "token-value"
    assert "AWS_ACCESS_KEY_ID" not in os.environ


def test_configure_auth_oidc_without_token_fails(monkeypatch):
    pipe = build_pipe(
        monkeypatch,
        {"SCRIPT": "/x.sh", "AWS_OIDC_ROLE_ARN": "arn:aws:iam::1:role/deploy"},
    )
    with pytest.raises(SystemExit):
        pipe.configure_auth()


def test_configure_auth_static_keys(monkeypatch):
    pipe = build_pipe(
        monkeypatch,
        {"SCRIPT": "/x.sh", "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret",
         "AWS_SESSION_TOKEN": "session"},
    )
    pipe.configure_auth()
    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIA"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "secret"
    assert os.environ["AWS_SESSION_TOKEN"] == "session"
    assert "AWS_ROLE_ARN" not in os.environ


def test_configure_auth_oidc_takes_precedence(monkeypatch):
    pipe = build_pipe(
        monkeypatch,
        {"SCRIPT": "/x.sh", "AWS_OIDC_ROLE_ARN": "arn:aws:iam::1:role/deploy",
         "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret"},
        oidc_token="token-value",
    )
    pipe.configure_auth()
    assert os.environ["AWS_ROLE_ARN"] == "arn:aws:iam::1:role/deploy"
    assert "AWS_ACCESS_KEY_ID" not in os.environ


# --- SSM interaction --------------------------------------------------------

def test_send_command_builds_request(monkeypatch):
    pipe = build_pipe(monkeypatch, {"SCRIPT": "/var/www/deploy.sh", "RUN_AS": "ubuntu"})
    ssm = MagicMock()
    ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}

    command_id = pipe.send_command(ssm, ["set -e", "sudo -iu ubuntu /var/www/deploy.sh"])

    assert command_id == "cmd-1"
    _, kwargs = ssm.send_command.call_args
    assert kwargs["InstanceIds"] == ["i-123"]
    assert kwargs["DocumentName"] == "AWS-RunShellScript"
    assert kwargs["Parameters"] == {"commands": ["set -e", "sudo -iu ubuntu /var/www/deploy.sh"]}


def test_wait_and_report_fails_on_non_success(monkeypatch):
    pipe = build_pipe(monkeypatch, {"SCRIPT": "/x.sh", "POLL_INTERVAL": "0"})
    ssm = MagicMock()
    ssm.get_command_invocation.return_value = {
        "Status": "Failed", "StandardOutputContent": "", "StandardErrorContent": "boom"
    }
    with pytest.raises(SystemExit):
        pipe.wait_and_report(ssm, "cmd-1")


def test_wait_and_report_succeeds(monkeypatch):
    pipe = build_pipe(monkeypatch, {"SCRIPT": "/x.sh", "POLL_INTERVAL": "0"})
    ssm = MagicMock()
    ssm.get_command_invocation.return_value = {
        "Status": "Success", "StandardOutputContent": "done", "StandardErrorContent": ""
    }
    pipe.wait_and_report(ssm, "cmd-1")  # should not raise


def test_poll_invocation_retries_on_missing_record(monkeypatch):
    pipe = build_pipe(monkeypatch, {"SCRIPT": "/x.sh", "POLL_INTERVAL": "0"})
    ssm = MagicMock()
    missing = ClientError({"Error": {"Code": "InvocationDoesNotExist"}}, "GetCommandInvocation")
    ssm.get_command_invocation.side_effect = [
        missing,
        {"Status": "Success", "StandardOutputContent": "ok", "StandardErrorContent": ""},
    ]
    status = pipe.poll_invocation(ssm, "cmd-1", "i-123", deadline=float("inf"), poll_interval=0)
    assert status == "Success"
    assert ssm.get_command_invocation.call_count == 2
