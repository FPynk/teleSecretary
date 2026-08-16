# TeleSecretary Homelab Setup Notes

TeleSecretary runs as a Docker Compose service on the homelab. It is not
installed as its own `systemd` service. `systemd` manages Docker itself, and
Docker manages the TeleSecretary container.

Project path:

```bash
/srv/apps/teleSecretary
```

Compose service name:

```bash
app
```

Persistent host paths:

```bash
/srv/apps/teleSecretary/data/secretary.sqlite3
/srv/apps/teleSecretary/logs/secretary.log
/srv/apps/teleSecretary/data/backups
```

---

## 1. Install host dependencies

Install the host tools used by the setup, deploy, and backup commands:

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin sqlite3
```

Enable and start Docker:

```bash
sudo systemctl enable --now docker
```

Check Docker:

```bash
docker --version
docker compose version
systemctl status docker
```

The `sqlite3` package is installed on the host because TeleSecretary's current
Docker image does not include the `sqlite3` CLI. Backups are run against the
bind-mounted database file on the host.

---

## 2. Git setup

Use Git to deploy the project into `/srv/apps/teleSecretary`.

Create the apps directory if it does not already exist:

```bash
sudo mkdir -p /srv/apps
sudo chown "$USER":"$USER" /srv/apps
```

For a private GitHub repository, prefer an SSH deploy key or a normal SSH key
already authorized for the repository.

Check that SSH auth works:

```bash
ssh -T git@github.com
```

Clone the repository:

```bash
git clone <repo-url> /srv/apps/teleSecretary
cd /srv/apps/teleSecretary
```

Use this update flow after the first setup:

```bash
cd /srv/apps/teleSecretary
git pull --ff-only
docker compose build app
docker compose run --rm --no-deps app python -m tele_secretary migrate
docker compose up -d
```

Local runtime files should stay untracked. The repository already ignores:

```text
.env
data/
logs/
*.sqlite3
*.db
```

If Git reports a `dubious ownership` warning, run the suggested
`safe.directory` command for the Linux user that will run Git:

```bash
git config --global --add safe.directory /srv/apps/teleSecretary
```

---

## 3. Environment setup

Create the local environment file:

```bash
cd /srv/apps/teleSecretary
cp .env.example .env
nano .env
```

Set these values:

```env
TELEGRAM_BOT_TOKEN=<bot-token-from-botfather>
TELEGRAM_ALLOWED_USER_IDS=<your-telegram-user-id>

SECRETARY_DATA_DIR=./data
SECRETARY_LOG_DIR=./logs
SECRETARY_DB_PATH=./data/secretary.sqlite3
SECRETARY_USER_TIMEZONE=America/Chicago
SECRETARY_LOG_LEVEL=INFO
```

`TELEGRAM_ALLOWED_USER_IDS` contains one or more authorized Telegram IDs. Each
authorized sender has an independent owner record, tasks, reminders, and
timezone; allowlist order is not used as an ownership fallback. If this value
is empty, Telegram commands are disabled.

The Docker Compose file overrides the data paths inside the container:

```yaml
SECRETARY_DATA_DIR: /data
SECRETARY_LOG_DIR: /logs
SECRETARY_DB_PATH: /data/secretary.sqlite3
```

The host paths are still `./data` and `./logs` because Compose bind-mounts them
into the container.

---

## 4. First startup

Build the image:

```bash
cd /srv/apps/teleSecretary
docker compose build app
```

Apply database migrations:

```bash
docker compose run --rm app python -m tele_secretary migrate
```

Start TeleSecretary:

```bash
docker compose up -d
```

After Telegram starts, the reminder worker runs one cycle immediately and then
every 30 seconds. It uses bounded batches, so a large backlog continues over
later cycles rather than keeping a single SQLite transaction or network call
open indefinitely.

Run a health check:

```bash
docker compose exec app python -m tele_secretary healthcheck
```

Check the container:

```bash
docker compose ps
```

Follow startup logs:

```bash
docker compose logs -f app
```

In Telegram, send:

```text
/ping
/help
/list
/addtask Test homelab setup -due 31/12/2026
```

---

## 5. Runtime model

The container starts TeleSecretary as:

```bash
python -m tele_secretary bot
```

This is set in:

```bash
/srv/apps/teleSecretary/Dockerfile
```

Runtime behavior:

- Docker starts the container.
- The container starts the Python bot process.
- `tele_secretary` loads configuration from the environment.
- The bot applies pending migrations on startup.
- Telegram command handlers do not run migrations per command.
- The bot starts Telegram long polling.

The SQLite database uses WAL mode. Backups should use SQLite's `.backup`
command instead of copying the live database file directly.

---

## 6. Autostart after reboot or power loss

Autostart is handled in two layers:

1. `systemd` starts Docker.
2. Docker restarts the TeleSecretary container because Compose sets:

```yaml
restart: unless-stopped
```

Meaning:

- If the homelab reboots, `systemd` brings Docker back up.
- Docker remembers that the TeleSecretary container should restart
  automatically.
- Docker restarts the container unless it was manually stopped.

Check Docker's systemd service:

```bash
systemctl status docker
```

This checks whether Docker itself is enabled, active, and running under `systemd`.

Check the TeleSecretary container:

```bash
cd /srv/apps/teleSecretary
docker compose ps
```

This moves into the Compose project directory and shows whether the container is
running.

Check the container restart policy:

```bash
docker inspect "$(docker compose ps -q app)" --format '{{json .HostConfig.RestartPolicy}}'
```

This asks Docker what restart behavior is configured for the container. You
should see `unless-stopped`.

Restart TeleSecretary manually:

```bash
cd /srv/apps/teleSecretary
docker compose restart app
```

This asks Docker Compose to gracefully stop and start the application container
without changing the database or persistent files.

---

## 7. Logging

TeleSecretary does not have a dedicated `systemd` unit, so this is not the right command:

```bash
journalctl -u teleSecretary
```

That would only work if there were a dedicated `teleSecretary.service`, which there is not.

View Docker daemon logs:

```bash
journalctl -u docker
```

This shows logs from the Docker service itself, such as Docker startup, shutdown, or daemon-level errors.

View app logs through Compose:

```bash
cd /srv/apps/teleSecretary
docker compose logs -f app
```

This follows the live logs emitted by the bot container. Use this to see polling, startup messages, errors, and Telegram activity.

View the app's file log on the host:

```bash
tail -f /srv/apps/teleSecretary/logs/secretary.log
```

This follows the same application logs, but through the Compose project context.

View recent logs only:

```bash
docker compose logs --since=1h --tail=200 app
```

This prints up to the last 200 log lines from the past hour.

Search logs for a user:

```bash
docker compose logs --since=24h app 2>&1 | grep -i "username_here"
```

This gets the last 24 hours of bot logs and filters them for a specific username or keyword.

The current `docker-compose.yml` does not define custom Docker log rotation.
Keep an eye on Docker disk usage during maintenance:

```bash
docker system df
```

---

## 8. Database

The SQLite database lives on the host at:

```bash
/srv/apps/teleSecretary/data/secretary.sqlite3
```

Inside the container, the app sees the same database at:

```bash
/data/secretary.sqlite3
```

This works because `docker-compose.yml` bind-mounts host data into the
container:

```yaml
volumes:
  - ./data:/data
  - ./logs:/logs
```

The database survives container restarts, image updates, and redeploys.

Open the database from the host:

```bash
sqlite3 /srv/apps/teleSecretary/data/secretary.sqlite3
```

List tables:

```sql
.tables
```

Show schema:

```sql
.schema
```

List tables without opening an interactive shell:

```bash
sqlite3 -header -column /srv/apps/teleSecretary/data/secretary.sqlite3 "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
```

---

## 9. Backups

Backups are handled by the normal user crontab. This keeps TeleSecretary's
scheduled jobs in the same place as the other homelab app cron jobs.

Create the backup directory:

```bash
sudo mkdir -p /srv/apps/teleSecretary/data/backups
sudo chown -R "$USER":"$USER" /srv/apps/teleSecretary/data/backups
```

Manually create a backup:

```bash
sqlite3 /srv/apps/teleSecretary/data/secretary.sqlite3 ".backup '/srv/apps/teleSecretary/data/backups/manual-$(date -u +%Y%m%d-%H%M%S).sqlite3'"
```

List recent backups:

```bash
ls -lt /srv/apps/teleSecretary/data/backups | head
```

Append the job to your user crontab:

```bash
crontab -e
```

Add this daily backup and pruning job:

```cron
5 3 * * * sqlite3 /srv/apps/teleSecretary/data/secretary.sqlite3 ".backup '/srv/apps/teleSecretary/data/backups/secretary-$(date -u +\%Y\%m\%d-\%H\%M\%S).sqlite3'" && find /srv/apps/teleSecretary/data/backups -type f -mtime +30 -delete
```

Meaning:

- Every day at 03:05 local cron time, your user runs the backup.
- The backup filename timestamp is UTC.
- SQLite's `.backup` command safely copies the live WAL-mode database.
- Backups are written to `/srv/apps/teleSecretary/data/backups`.
- Backups older than 30 days are deleted.

Check your user crontab:

```bash
crontab -l
```

Check backup folder ownership and files:

```bash
ls -ld /srv/apps/teleSecretary/data/backups
ls -lt /srv/apps/teleSecretary/data/backups | head
```

Check old backups that should be pruned:

```bash
find /srv/apps/teleSecretary/data/backups -type f -mtime +30 -print
```

---

## 10. Restore from backup

Stop TeleSecretary:

```bash
cd /srv/apps/teleSecretary
docker compose stop app
```

Move the current database aside:

```bash
mv /srv/apps/teleSecretary/data/secretary.sqlite3 /srv/apps/teleSecretary/data/secretary.sqlite3.before-restore
```

Copy the backup into place:

```bash
cp /srv/apps/teleSecretary/data/backups/<backup-file>.sqlite3 /srv/apps/teleSecretary/data/secretary.sqlite3
```

Start TeleSecretary:

```bash
docker compose start app
```

Run a health check:

```bash
docker compose exec app python -m tele_secretary healthcheck
```

---

## 11. Health checks

Run the app health check:

```bash
cd /srv/apps/teleSecretary
docker compose exec app python -m tele_secretary healthcheck
```

Check current container state:

```bash
docker compose ps
```

Inspect health and restart count:

```bash
docker inspect "$(docker compose ps -q app)" --format '{{.State.Status}} / health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} / restarts={{.RestartCount}}'
```

---

## 12. Common maintenance commands

Pull the latest code and redeploy:

```bash
cd /srv/apps/teleSecretary
git pull --ff-only
docker compose build app
docker compose run --rm --no-deps app python -m tele_secretary migrate
docker compose up -d
```

Run migrations manually:

```bash
cd /srv/apps/teleSecretary
docker compose run --rm app python -m tele_secretary migrate
```

Restart TeleSecretary:

```bash
cd /srv/apps/teleSecretary
docker compose restart app
```

Stop TeleSecretary:

```bash
cd /srv/apps/teleSecretary
docker compose stop app
```

Start TeleSecretary again:

```bash
cd /srv/apps/teleSecretary
docker compose start app
```

Check Docker disk usage:

```bash
docker system df
```

This shows Docker image, container, volume, and build-cache disk usage.

Prune unused build cache:

```bash
docker builder prune -af
```

This removes unused build cache. It does not delete the live TeleSecretary
container or the bind-mounted database, logs, or backups.
