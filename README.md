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

## GitHub Actions

The [game-watch workflow](.github/workflows/game-watch.yml) runs automatically and can be triggered manually.

**Schedule:** cron `*/10 * * * *` (UTC) — every **10 minutes**. GitHub cron uses UTC and does **not** follow daylight saving time, so local run times shift by one hour when clocks change.

**Manual run:** **Actions → Game watch → Run workflow**.

The job copies `config.ci.yaml` to `config.yaml`, runs the watcher with `SMTP_PASSWORD` and `SMTP_TO` from secrets, then commits `state.json` if prices changed.

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
