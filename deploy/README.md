# EC2 deployment

Run on Ubuntu 24.04 with Python 3.10 or newer. No Python package installation is required.

- Code: `/opt/psx-lens`, root-owned, readable by service user `psxlens`.
- Data: `/var/lib/psx-lens/workspace.sqlite3`, writable only by the service user.
- Secrets: `/etc/psx-lens/pypsx.env`, root-owned mode 600. Use the names in `.env.example`; never commit actual values.
- Install `psx-lens.service` into `/etc/systemd/system/`, run `systemctl daemon-reload`, then `systemctl enable --now psx-lens`.
- Service listens on loopback port 8766. Nginx can proxy port 80 with an explicit client-IP allowlist. Never expose port 8766 in the EC2 security group.
- The application verifies Host/Origin against its loopback port. A reverse proxy must use `proxy_set_header Host 127.0.0.1:8766` and `proxy_set_header Origin http://127.0.0.1:8766`. Keep access restricted at both Nginx and the EC2 security group. Add HTTPS and authentication before wider access.
- Direct private access from your Mac: `ssh -N -L 8766:127.0.0.1:8766 psx-lens`, then open `http://localhost:8766`.
- Health: `systemctl status psx-lens`; logs: `journalctl -u psx-lens --since '10 minutes ago'`.
- The service starts the actual-data updater on boot. A new deployment creates a new paper workspace; the local database is not transferred automatically.

Back up SQLite with its backup API (not a live file copy). Retain the database and environment file when updating source. Keep deployment artifacts free of `.env`, private keys, runtime databases and caches. Record the deployed Git commit in `/opt/psx-lens/.release`.
