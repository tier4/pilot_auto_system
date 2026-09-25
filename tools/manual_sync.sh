#!/usr/bin/env bash
# Manual sync equivalent to .github/workflows/mirror.yaml (with --push).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DOWNSTREAM="${DOWNSTREAM:-git@github.com:tier4/pilot_auto_system.git}"
WORK="${WORK:-/tmp/pilot_auto_system-sync}"
PUSH="${PUSH:-1}"
# Optional: space-separated source names, e.g. SOURCES=autoware_universe
SOURCES_FILTER="${SOURCES:-}"

mkdir -p "$WORK"
export PYTHONPATH="$ROOT/tools${PYTHONPATH:+:$PYTHONPATH}"

echo "==> validate"
tools/sync_config.py validate

mapfile -t ALL_SOURCES < <(tools/mirror.py list-sources | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)))')
mapfile -t COMBINED < <(tools/mirror.py list-combined | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)))')

if [[ -n "$SOURCES_FILTER" ]]; then
  SOURCES=()
  for source in $SOURCES_FILTER; do
    SOURCES+=("$source")
  done
else
  SOURCES=("${ALL_SOURCES[@]}")
fi

push_flag=()
if [[ "$PUSH" == "1" ]]; then
  push_flag=(--push)
  echo "==> live sync (will push to $DOWNSTREAM)"
else
  echo "==> dry run (no push)"
fi

for source in "${SOURCES[@]}"; do
  echo
  echo "==> mirror $source"
  tools/mirror.py mirror "$source" --work "$WORK/mirror" --downstream "$DOWNSTREAM" "${push_flag[@]+"${push_flag[@]}"}"
done

if [[ -z "$SOURCES_FILTER" ]]; then
  for target in "${COMBINED[@]}"; do
    echo
    echo "==> combine $target"
    if [[ "$PUSH" == "1" ]]; then
      tools/mirror.py combine "$target" --work "$WORK/combine" --downstream "$DOWNSTREAM" --verify --push
    else
      tools/mirror.py combine "$target" --work "$WORK/combine" --downstream "$DOWNSTREAM" --verify --allow-missing-members
    fi
  done
fi

echo
echo "==> done"
