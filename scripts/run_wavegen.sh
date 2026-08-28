#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

python ../LiveDAQ_1c.py \
  --wavegen \
  --wavegen-frequency 10 \
  --wavegen-vpp 0.100 \
  --wavegen-offset 0 \
  --min-charge-fc 40 \
  --save-waveforms
