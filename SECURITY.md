# Security Policy

## Supported versions

Security fixes are applied to the latest release on the default branch.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting feature under **Security → Advisories → Report
a vulnerability**. Include the affected version, impact, reproduction steps,
and any suggested mitigation. Maintainers should acknowledge reports within
seven days.

## Deployment expectations

- Keep the dashboard behind TLS and network access controls.
- Never commit `configs/backup.env`, SSH private keys, repository passwords, or
  runtime data.
- SSH host keys use trust-on-first-use by default (`accept-new`): new keys are
  recorded automatically and changed keys are rejected. For higher assurance,
  set `CONFIG_BACKUP_SSH_HOST_KEY_CHECKING=yes` and verify fingerprints before
  adding them to `known_hosts`.
- Use an independent off-host repository and test restores regularly.
- Rotate any credential that was ever committed, even if later deleted.
