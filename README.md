# BGPC — board game price check

`game_watch.py` fetches configured [tarsasjatekok.com](https://tarsasjatekok.com) product pages, parses offers/prices, compares to the last run, and emails a report.

## Setup

```bash
uv sync
cp config.yaml.example config.yaml
cp .env.example .env
```

Edit `config.yaml` for games and non-secret SMTP settings (host, username, etc.). **Do not** put the SMTP password or recipient addresses in YAML — set them in `.env` instead.

Edit `.env` and set `SMTP_PASSWORD` (a Gmail [app password](https://support.google.com/accounts/answer/185833) if you use Gmail) and `SMTP_TO` (comma-separated recipient list).

`config.yaml` and `.env` are gitignored and stay on your machine only. For CI, the committed [`config.ci.yaml`](config.ci.yaml) is used instead of a local `config.yaml`.

## Secrets

Local development and GitHub Actions use the **same environment variable names** (see [`.env.example`](.env.example)):

| Variable | Required | Source |
|----------|----------|--------|
| `SMTP_PASSWORD` | Yes (when `smtp.username` is set) | `.env` locally; GitHub Secret in CI |
| `SMTP_TO` | Yes | Comma-separated recipient list; `.env` locally; GitHub Secret in CI |
| `SMTP_USERNAME` | No | Overrides `smtp.username` from YAML |
| `SMTP_HOST` | No | Overrides `smtp.host` |
| `SMTP_FROM` | No | Overrides `smtp.from` |

### Local (`.env`)

At startup, `game_watch.py` loads `.env` from next to the config file, then from the current working directory. Shell environment variables take precedence (`override=False`), so you can override `.env` values when needed.

Load order:

1. `.env` → populates `SMTP_*` in the process environment
2. `config.yaml` → games and non-secret SMTP fields
3. `SMTP_*` env vars → applied on top of YAML (`apply_smtp_env_overrides`)

### GitHub Actions (repository secrets)

The workflow does **not** create a `.env` file on the runner. Map secrets directly to environment variables in the workflow step — same keys as `.env.example`:

```yaml
env:
  SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
  SMTP_TO: ${{ secrets.SMTP_TO }}
```

Add **SMTP_PASSWORD** and **SMTP_TO** under **Settings → Secrets and variables → Actions** (same values as in your local `.env`). Optional overrides (`SMTP_USERNAME`, etc.) only need GitHub Secrets if you want them out of committed `config.ci.yaml`.

## Run

```bash
uv run python game_watch.py --config config.yaml
uv run python game_watch.py --config config.yaml --dry-run
```

## Schedule (recommended: Windows Task Scheduler)

GitHub Actions scheduled workflows are **unreliable** for frequent checks: runs can be delayed by hours, disabled after repo inactivity, or blocked by org/repo settings. For a machine that is usually on, **local scheduling is more dependable**.

### Windows

Prerequisites: `config.yaml`, `.env`, and `uv` on PATH (or in a standard install location).

```powershell
# One-time: register a task that runs every 10 minutes
.\scripts\register-scheduled-task.ps1

# Test without sending email
.\scripts\run-game-watch.ps1 -DryRun
```

- Task name: `BGPC-GameWatch` (customize with `-TaskName`)
- Interval: 10 minutes by default (`-IntervalMinutes 60` for hourly)
- Logs: `logs/game-watch.log` (gitignored)
- Remove: `Unregister-ScheduledTask -TaskName BGPC-GameWatch -Confirm:$false`

The task runs while you are logged in. If the PC sleeps, missed runs are picked up when it wakes (`StartWhenAvailable`).

### Linux / macOS

Use cron or a systemd timer, pointing at the same command:

```bash
*/10 * * * * cd /path/to/BGPC && uv run python game_watch.py --config config.yaml >> logs/game-watch.log 2>&1
```

## GitHub Actions (optional)

The [game-watch workflow](.github/workflows/game-watch.yml) can run on a schedule or manually, but treat it as a backup — not the primary scheduler.

**Manual run:** **Actions → Game watch → Run workflow**.

The job copies `config.ci.yaml` to `config.yaml`, runs the watcher with `SMTP_PASSWORD` and `SMTP_TO` from secrets, then commits `state.json` if prices changed.

**If scheduled runs never appear:**

1. **Settings → Actions → General** — ensure Actions are enabled.
2. **Settings → Secrets and variables → Actions** — add `SMTP_PASSWORD` and `SMTP_TO` (manual runs fail without these).
3. Workflow must live on the **default branch** (`main`).
4. GitHub does not guarantee cron timing; `*/10` may actually run much less often during load.
5. On **public** repos, scheduled workflows are disabled after **60 days** without repository activity.

**Schedule:** cron `*/10 * * * *` (UTC). GitHub cron does not follow daylight saving time.

## Security

**Keep this repository private.** Committed `state.json` records watched game URLs, titles, and price history. A public repo would expose what you track and historical prices.

| Item | Status |
|------|--------|
| `config.yaml` | Gitignored — never committed |
| `.env` | Gitignored — passwords stay local |
| `.env.example` | Tracked — placeholders only |
| `config.ci.yaml` | Tracked — no secrets |
| SMTP password & recipients | `.env` or GitHub Secret only, never in YAML |

The workflow is hardened for a secrets-bearing scheduled job:

- **No `pull_request` trigger** — only `schedule` and `workflow_dispatch`, so fork PRs cannot run with access to `SMTP_PASSWORD`.
- **Pinned action SHAs** — third-party actions use full commit hashes (with version comments), not mutable tags.
- **Minimal `GITHUB_TOKEN` permissions** — `contents: write` only (checkout + commit `state.json`); no extra scopes.
- **Secrets via environment** — `SMTP_PASSWORD` and `SMTP_TO` are injected as env vars on the runner, not written to disk.
- **No secret logging** — do not echo env vars or config in workflow logs.

## Dev

```bash
uv run ruff check .
uv run mypy .
```
