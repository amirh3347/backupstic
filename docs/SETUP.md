# Production setup

## 1. Prepare the host

Install Docker Engine, Docker Compose v2, OpenSSL, and the OpenSSH client. Use
a dedicated Linux host with enough capacity for retention and temporary restore
work. Keep the host patched and restrict inbound network access.

## 2. Initialize secrets

```bash
./init.sh
```

This creates `configs/backup.env`, an SSH application keypair, and a strict
`known_hosts` file. These paths are ignored by Git. Back up the encryption keys
separately: losing `RESTIC_PASSWORD` makes snapshots unreadable; changing
`PROFILE_SECRET_KEY` makes stored profile credentials unreadable.

For bcrypt authentication, replace `ADMIN_PASSWORD` with a hash:

```bash
python3 -c "import bcrypt,getpass; print(bcrypt.hashpw(getpass.getpass().encode(), bcrypt.gensalt()).decode())"
```

Put the result in `ADMIN_PASSWORD_HASH` and remove `ADMIN_PASSWORD`.

## 3. Configure access

The dashboard listens only on `127.0.0.1:5000` by default. Place an HTTPS
reverse proxy on the same host, or expose it only to a trusted private network:

```bash
cp .env.example .env
# Edit DASHBOARD_BIND and DASHBOARD_PORT as needed.
```

Do not publish the Docker socket or mount unrelated host directories into the
containers.

## 4. Start and verify

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:5000/api/health
docker compose logs --tail=100 dashboard backup-cron backup-monitor
```

Create profiles in the UI. For SSH profiles, install the public key on the
target. Backupstic enrolls a new host key on first use and rejects later key
changes:

```bash
ssh-copy-id -i data/config-backup/id_ed25519.pub user@host
```

To require manual pre-enrollment instead, set
`CONFIG_BACKUP_SSH_HOST_KEY_CHECKING=yes`, verify the fingerprint through a
trusted channel, and add it with `ssh-keyscan`.

## 5. Repository strategy

The default repository lives in the `backupstic-data` Docker volume. For real
production use, add a separate S3/MinIO repository or place repository storage
on independent durable infrastructure. Follow the 3-2-1 rule: three copies,
two storage types, and one off-site copy.

## 6. Restore drill

Run a restore into an isolated path and validate application-level integrity:

```bash
docker compose exec backup-cron /scripts/restore.sh postgres
docker compose exec backup-cron restic check --read-data-subset=5%
```

Never restore over a live service without a reviewed runbook and a current
secondary copy. Record recovery time and recovery point results.

## Maintenance

```bash
docker compose pull
docker compose build --pull
docker compose up -d
docker compose logs -f
```

Review dependency update pull requests, rotate credentials, monitor repository
growth, and repeat restore drills on a schedule. `docker compose down` preserves
the data volume. `docker compose down -v` permanently deletes local backup data.
