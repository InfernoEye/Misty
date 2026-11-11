#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
if [ ! -f "$root/misty.py" ]; then
  echo "Refusing: unexpected project root: $root" >&2
  exit 1
fi
stamp=$(date +%Y%m%d-%H%M%S)
quarantine="$here/quarantine-$stamp"
mkdir -p "$quarantine/app/engine"
for f in app/main.py app/popup_process.py app/engine/voice_controller.py \
         app/engine/clipboard_bridge.py app/engine/activity_store.py \
         app/engine/confirmations.py; do
  if [ -f "$root/$f" ]; then
    mkdir -p "$quarantine/$(dirname "$f")"
    cp -p "$root/$f" "$quarantine/$f"
  fi
done
for f in app/main.py app/popup_process.py app/engine/voice_controller.py \
         app/engine/clipboard_bridge.py app/engine/activity_store.py; do
  cp -p "$here/$f" "$root/$f"
done
if [ -f "$root/app/engine/confirmations.py" ]; then
  mv "$root/app/engine/confirmations.py" "$quarantine/app/engine/confirmations.py"
fi
printf 'Restored pre-batch-2 files. Current versions quarantined in %s\n' "$quarantine"
