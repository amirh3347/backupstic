# Backupstic

Backupstic is a self-hosted backup control plane for PostgreSQL, MongoDB, Redis,
Elasticsearch, local files, and remote files over SSH. It stores encrypted,
deduplicated snapshots with [restic](https://restic.net/) and provides a small
authenticated web dashboard for profiles, schedules, retention, downloads, and
health information.

> Backup software is only trustworthy after a restore drill. Test restores in
> an isolated environment before relying on this project for critical data.

## Highlights

- Profile-based and legacy backup jobs
- Local, S3, and MinIO restic repositories
- Encrypted profile and repository credentials at rest
- Cross-container backup locking and atomic JSON storage
- Scheduled jobs through a dedicated cron container
- SSH host-key verification and key/password authentication
- Health checks, retention policies, monitoring reports, and a responsive UI

## Requirements

- Linux host with Docker Engine and Docker Compose v2
- At least one reachable data source
- A reverse proxy with TLS for any non-local deployment

## Quick start

```bash
git clone YOUR_REPOSITORY_URL backupstic
cd backupstic
./init.sh
docker compose up -d --build
docker compose ps
```

Open <http://127.0.0.1:5000> and sign in with the password printed by
`./init.sh`. The dashboard binds to localhost by default. To expose it to a
private network, run:

```bash
DASHBOARD_BIND=0.0.0.0 docker compose up -d
```

Do not expose the dashboard directly to the public internet. Put it behind an
HTTPS reverse proxy, restrict source networks, and keep `configs/backup.env`
private.

## SSH profile setup

`./init.sh` creates one application keypair under `data/config-backup/`. Copy
only its public key to each target. New host keys are enrolled automatically on
the first connection and changed host keys are still rejected:

```bash
ssh-copy-id -i data/config-backup/id_ed25519.pub backup@example.internal
```

For stricter pre-enrollment, set `CONFIG_BACKUP_SSH_HOST_KEY_CHECKING=yes` and
add verified fingerprints to `data/config-backup/known_hosts` manually.

## Operations

```bash
docker compose logs -f dashboard
docker compose exec backup-cron /scripts/backup-all.sh full
docker compose exec backup-cron /scripts/restore.sh postgres
docker compose down
```

The named Docker volume `backupstic-data` contains restic data, profiles,
schedules, and logs. `docker compose down` keeps it; `docker compose down -v`
deletes it and therefore must not be used unless data loss is intended.

The cron container rebuilds `schedules.cron` from profile storage whenever it
starts, and profile create/update/delete operations reconcile it immediately.
Profiles with the same cron expression are placed in one sequential batch so
the global backup lock cannot make same-minute jobs skip each other. Jobs that
overlap for another reason wait up to `BACKUP_LOCK_WAIT_SECONDS` (six hours by
default) before failing visibly in `cron.log`.

For production, store the restic repository on durable independent storage,
monitor failed jobs and disk usage, follow the 3-2-1 backup rule, and schedule
regular restore tests. See [Setup](docs/SETUP.md), [API](docs/API.md), and
[Security](SECURITY.md).

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q backend frontend scripts tests
for script in init.sh scripts/*.sh; do bash -n "$script"; done
docker compose config
```

Contributions are welcome; read [CONTRIBUTING.md](CONTRIBUTING.md). This project
is available under the [MIT License](LICENSE).
