# Changelog

## 1.0.0

- Initial release.
- Run AWS SSM commands on EC2 instances via `AWS-RunShellScript` (or a custom document).
- OIDC and static-key authentication paths.
- Parametrize via a generic `COMMANDS` list or a convenience `SCRIPT` + `RUN_AS`.
- Wait for completion with timeout, surface stdout/stderr, fail the build on non-`Success`.
