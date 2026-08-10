# SSH configuration

Runtime SSH material belongs in `data/config-backup/`, which is intentionally
ignored by Git. Run `./init.sh` to generate a keypair. Add verified target host
keys to `data/config-backup/known_hosts`; see `docs/SETUP.md`.
