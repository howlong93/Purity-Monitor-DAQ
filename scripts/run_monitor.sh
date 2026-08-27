#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

python ../LiveDAQ_1c.py \
  --sample-rate 200000 \
  --min-charge-fc 40 \
  --polarity positive \
  --canva-size 1 \
  --no-save
