#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

# Physical purity-monitor assignment:
#   AD3 Channel 1 <- cathode CR-150 / CR-110 output (Qc)
#   AD3 Channel 2 <- anode   CR-150 / CR-110 output (Qa)
#
# W1 is intentionally disabled. Summary CSV output is enabled by default;
# accepted waveform CSV output is not enabled to reduce acquisition overhead.
python ../LiveDAQ_2c.py \
  --sample-rate 500000 \
  --input-range 1.0 \
  --channel-1-polarity negative \
  --channel-2-polarity positive \
  --drift-time-us 55 \
  --drift-window-us 15 \
  --threshold-sigma 8 \
  --min-charge-fc 40 \
  --pretrigger-ms 0.5 \
  --posttrigger-ms 1.5 \
  --gui-rate 10 \
  --num-test 10
