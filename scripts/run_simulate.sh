#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

python ../LiveDAQ_1c.py \
  --simulate \
  --sample-rate 500000 \
  --simulation-pulse-rate 10 \
  --simulation-charge-fc 100 \
  --simulation-polarity positive \
  --min-charge-fc 50 \
  --polarity positive \
  --canva-size 2 \
  --num-test 5 \
  --duration 60 \
  --no-save
