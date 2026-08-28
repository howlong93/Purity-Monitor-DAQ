#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

python ../LiveDAQ_2c.py \
  --simulate \
  --sample-rate 200000 \
  --simulation-pulse-rate 10 \
  --simulation-cathode-charge-fc 100 \
  --simulation-anode-charge-fc 95 \
  --drift-time-us 55 \
  --drift-window-us 15 \
  --min-charge-fc 40 \
  --num-test 5 \
  --no-save