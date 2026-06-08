# BGPC — board game price check

`game_watch.py` fetches configured [tarsasjatekok.com](https://tarsasjatekok.com) product pages, parses offers/prices, compares to the last run, and emails a report.

## Setup

```bash
uv sync
cp config.yaml.example config.yaml
# Edit config.yaml: games, SMTP, optional subject / state_path
```

## Run

```bash
uv run python game_watch.py --config config.yaml
uv run python game_watch.py --config config.yaml --dry-run
```

## Dev

```bash
uv run ruff check .
uv run mypy .
```
