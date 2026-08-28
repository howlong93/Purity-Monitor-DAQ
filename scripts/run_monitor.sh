#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

python ../LiveDAQ_1c_flat.py \
  --sample-rate 800000 \
  --min-charge-fc 40 \
  --polarity positive \
  --canva-size 0.5 \
  --num-test  10