# Bitbucket Pipelines Pipe: AWS SSM Deploy

Run AWS Systems Manager (SSM) commands on EC2 instances during a Bitbucket
deployment. Use it to trigger a remote deploy script (or any shell commands) on a
running instance without SSH.

Supports two authentication paths, mirroring the official Atlassian AWS pipes:

- **OIDC** — set `AWS_OIDC_ROLE_ARN` and add `oidc: true` to the step.
- **Static keys** — set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
  (and optionally `AWS_SESSION_TOKEN`).

## YAML Definition

Add the following to your `bitbucket-pipelines.yml`, replacing `<namespace>` with
the Docker Hub namespace the image was published under.

```yaml
- pipe: <namespace>/aws-ssm-deploy:1.0.0
  variables:
    AWS_DEFAULT_REGION: "<string>"
    INSTANCE_IDS: ["<string>"]
    # one of COMMANDS or SCRIPT is required:
    SCRIPT: "<string>" # convenience: a path to run
    # COMMANDS: ["<string>"]    # or: raw shell commands
```

## Variables

| Variable                | Usage                                                                    |
| ----------------------- | ------------------------------------------------------------------------ |
| AWS_DEFAULT_REGION (\*) | The AWS region of the target instance(s).                                |
| INSTANCE_IDS (\*)       | List of EC2 instance ids to run the command on.                          |
| AWS_OIDC_ROLE_ARN       | IAM role ARN to assume via OIDC. Requires `oidc: true` on the step.      |
| AWS_ACCESS_KEY_ID       | AWS access key id (static-key auth).                                     |
| AWS_SECRET_ACCESS_KEY   | AWS secret access key (static-key auth).                                 |
| AWS_SESSION_TOKEN       | AWS session token (optional, for temporary static credentials).          |
| COMMANDS                | List of raw shell commands to run. Mutually exclusive with `SCRIPT`.     |
| SCRIPT                  | A single script/command to run. Mutually exclusive with `COMMANDS`.      |
| RUN_AS                  | Run `SCRIPT` as this user: `sudo -iu $RUN_AS $SCRIPT`. Default: none.    |
| DOCUMENT_NAME           | SSM document to use. Default: `AWS-RunShellScript`.                      |
| COMMENT                 | Comment attached to the SSM command. Default: the Bitbucket build.       |
| WAIT                    | Wait for the command to finish. Default: `true`.                         |
| WAIT_TIMEOUT            | Max seconds to wait for completion. Default: `600`.                      |
| POLL_INTERVAL           | Seconds between status polls. Default: `5`.                              |
| FAIL_ON_ERROR           | Fail the build if any instance status is not `Success`. Default: `true`. |
| DEBUG                   | Turn on extra debug logging. Default: `false`.                           |

_(\*) = required._

Exactly one of `COMMANDS` or `SCRIPT` must be provided.

## Authentication

### OIDC (recommended)

The step must enable `oidc: true` so Bitbucket exposes `BITBUCKET_STEP_OIDC_TOKEN`.
The pipe writes the token to a file and assumes the role via
`AssumeRoleWithWebIdentity`.

```yaml
- step:
    name: Deploy
    deployment: production
    oidc: true
    script:
      - pipe: <namespace>/aws-ssm-deploy:1.0.0
        variables:
          AWS_DEFAULT_REGION: $AWS_DEFAULT_REGION
          AWS_OIDC_ROLE_ARN: "arn:aws:iam::$AWS_ACCOUNT_ID:role/$AWS_OIDC_ROLE_NAME"
          INSTANCE_IDS: ["$AWS_INSTANCE_ID"]
          SCRIPT: "/var/www/prod-backend/deploy.sh"
          RUN_AS: "ubuntu"
```

### Static keys

```yaml
- step:
    name: Deploy
    script:
      - pipe: <namespace>/aws-ssm-deploy:1.0.0
        variables:
          AWS_DEFAULT_REGION: $AWS_DEFAULT_REGION
          AWS_ACCESS_KEY_ID: $AWS_ACCESS_KEY_ID
          AWS_SECRET_ACCESS_KEY: $AWS_SECRET_ACCESS_KEY
          INSTANCE_IDS: ["$AWS_INSTANCE_ID"]
          COMMANDS:
            - "set -e"
            - "cd /var/www/prod-backend"
            - "./deploy.sh"
```

## Prerequisites

- The target EC2 instance(s) must have the SSM agent running and an instance
  profile that allows SSM.
- The IAM role/credentials used by the pipe need `ssm:SendCommand` and
  `ssm:GetCommandInvocation` on the target instances.

## Building & publishing

The image namespace is not hardcoded. The GitHub Actions workflow composes it
from the `DOCKERHUB_NAMESPACE` secret (falling back to `DOCKERHUB_USERNAME`) and
injects it into `pipe.yml` at build time. To build manually, supply your own
namespace:

```bash
docker build -t <namespace>/aws-ssm-deploy:1.0.0 .
docker push <namespace>/aws-ssm-deploy:1.0.0
```

## Testing

```bash
python -m pip install -r test-requirements.txt
python -m pytest test/
```

## License

Released under the MIT License. See [LICENSE.txt](LICENSE.txt).
