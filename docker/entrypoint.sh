#!/usr/bin/env sh
set -eu

echo "[aoc] running database migrations..."
uv run --no-sync alembic upgrade head

echo "[aoc] starting AOC server..."
exec uv run --no-sync python main.py
