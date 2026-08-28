# Purity Monitor DAQ System

Live data acquisition and monitoring for CR-150/CR-110 charge-sensitive preamplifiers using a Digilent Analog Discovery 3 (AD3).

This README is the practical starting point for detector users. Detailed engineering notes are available in [`docs/notes_1c.md`](docs/notes_1c.md) and [`docs/notes_2c.md`](docs/notes_2c.md).

> **Current status:** Simulation is available for every live program. Both optimized one-channel GUI variants have sustained 500 kS/s on the physical AD3 with `lost 0` and `corrupted 0` under the tested laboratory configuration. The two-channel programs still require validation with the physical AD3, two CR-110 readout chains, and the purity monitor. Displayed charge currently uses the nominal CR-110 gain and is not yet a detector-calibrated physics result.

## System overview

The DAQ continuously reads one or two CR-110 outputs through the AD3, learns the background noise, detects charge pulses, displays live waveforms, and optionally saves numerical results.

The two-channel programs are intended for the complete purity monitor:

```text
Cathode -> CR-150 / CR-110 -> AD3 Channel 1 --+
                                                  +-> Python DAQ -> Qc, Qa, delay, Qa/Qc
Anode   -> CR-150 / CR-110 -> AD3 Channel 2 --+
```

The one-channel programs are useful for electronics tests, individual-channel commissioning, and performance diagnosis:

```text
Signal source -> CR-150 / CR-110 -> AD3 Channel 1 -> Python DAQ
```

All four live programs share the same basic processing path:

```text
AD3 Record acquisition or simulation
                |
                v
       One-second noise learning
                |
                v
       Charge-pulse detection
                |
                +-> GUI waveform display
                |
                +-> Result table and optional CSV output
```

The two-channel versions add independent detection for each channel and pair cathode/anode pulses by their peak-time separation.

---

## Installation

Clone the repository and enter the repository root:

```bash
git clone https://github.com/howlong93/Purity-Monitor-DAQ.git
cd Purity-Monitor-DAQ
```

Unless stated otherwise, run the commands in this README from the repository root.

### Software

- Python 3.10 or newer.
- NumPy.
- Tkinter for GUI mode.
- Digilent WaveForms with the WaveForms SDK for physical AD3 operation.
- Bash for the supplied `.sh` scripts. On Windows, Git Bash is suitable.

Install the pip-managed dependency from [`requirements.txt`](requirements.txt):

```bash
python -m pip install -r requirements.txt
```

Simulation does not require an AD3 or the WaveForms SDK.

Tkinter is installed with the standard Windows Python distribution but is not a pip package. On Linux, install the operating system's Tkinter package if GUI startup reports that it is missing.

## Choose a program

The four main live programs are:

| Program | Inputs | Canvas style | Use it when... |
|---|---:|---|---|
| [`LiveDAQ_1c.py`](LiveDAQ_1c.py) | 1 | Event-centered, one or more accepted pulses aligned/overlaid | Testing one CR-110 channel or monitoring individual detected pulses |
| [`LiveDAQ_1c_flat.py`](LiveDAQ_1c_flat.py) | 1 | Consecutive raw time windows | Inspecting continuous Channel 1 behavior, noise, baseline, or missed pulses |
| [`LiveDAQ_2c.py`](LiveDAQ_2c.py) | 2 | Latest paired cathode/anode event | Monitoring paired purity-monitor events and preliminary `Qc`, `Qa`, delay, and `Qa/Qc` |
| [`LiveDAQ_2c_flat.py`](LiveDAQ_2c_flat.py) | 2 | Consecutive raw two-channel time windows | Inspecting the full cathode-to-anode time sequence, noise, bumps, false triggers, or pairing behavior |

### Additional offline utility

[`DAQ_1c.py`](DAQ_1c.py) analyzes Channel 1 CSV files exported by Digilent WaveForms. It is not one of the four live programs and does not control the AD3.

### Non-flat versus flat

Use a non-flat program when accepted events and their measured values are the main interest.

Use a flat program when continuous raw context is important. Flat programs show fixed, consecutive, non-overlapping time intervals. They retain at most 50,000 timestamp positions per canvas interval and use a min/max display envelope to preserve narrow extrema.

Display reduction affects only plotting. Pulse detection still processes the acquired samples before canvas reduction.

---

## Quick start with simulation

Open a terminal in the repository root. The safest first test is the one-channel non-flat simulation:

```bash
python LiveDAQ_1c.py --simulate --sample-rate 200000 --simulation-charge-fc 100 --simulation-polarity positive --min-charge-fc 40 --polarity positive --canva-size 1 --no-save
```

A successful first run should show all of the following:

1. The GUI opens without a Python import error.
2. The status changes from `Learning noise (1 s warm-up)` to `Running`.
3. Simulated pulses appear at approximately the configured event rate.
4. Reconstructed charge is near the configured `--simulation-charge-fc` value.
5. The status remains `lost 0` and `corrupted 0`.
6. Clicking **Stop** closes the acquisition worker cleanly.

If this test fails, resolve the software environment before connecting hardware.

### Two-channel event simulation

```bash
python LiveDAQ_2c.py --simulate --sample-rate 200000 --simulation-cathode-charge-fc 100 --simulation-anode-charge-fc 95 --drift-time-us 55 --drift-window-us 15 --min-charge-fc 40 --no-save
```

Simulation verifies software behavior. It does not prove that a physical AD3, USB connection, or computer can sustain the requested sampling rate.

---

## Hardware run

### Pre-run checklist

1. Close the WaveForms desktop application so Python can open the AD3.
2. Connect the correct channel and verify USB power, common ground, coupling, attenuation, polarity, and input range.
3. Close the metal enclosure and keep sensitive wiring short.
4. Start with `--no-save` and a conservative sampling rate.
5. Wait for noise learning to finish, then confirm the pulse polarity and scale.
6. Require `lost 0` and `corrupted 0` before enabling data saving.

### One-channel non-flat hardware template

```bash
python LiveDAQ_1c.py --sample-rate 200000 --input-range 1.0 --min-charge-fc 40 --polarity positive --canva-size 1 --no-save
```

Adjust `--polarity`, `--min-charge-fc`, and `--input-range` to the measured hardware signal.

### Two-channel hardware template

Close WaveForms, connect the cathode output to Channel 1 and the anode output to Channel 2, then run:

```bash
python LiveDAQ_2c_flat.py --sample-rate 200000 --input-range 1.0 --channel-1-polarity negative --channel-2-polarity positive --drift-time-us 55 --drift-window-us 15 --min-charge-fc 40 --canva-size 0.500 --no-save
```

The current `55 +/- 15 us` pairing rule is provisional. Determine the correct timing from physical data before using pairing for production analysis. Two-channel physical throughput and pairing still require validation.

## Stopping a run and CLI help

- GUI mode: click **Stop** and allow the acquisition worker to close safely.
- Headless mode: press `Ctrl+C`.
- Add `--duration N` to stop automatically after `N` wall-clock seconds.

For authoritative options and defaults, run:

```bash
python LiveDAQ_1c.py --help
python LiveDAQ_1c_flat.py --help
python LiveDAQ_2c.py --help
python LiveDAQ_2c_flat.py --help
```

The program's `--help` output is authoritative if the code and README ever differ.

---

## Example scripts

Run a script with:

```bash
bash scripts/run_simulate.sh
```

| Script | Intended use |
|---|---|
| [`scripts/run_simulate.sh`](scripts/run_simulate.sh) | One-channel GUI and detector demonstration without hardware |
| [`scripts/run_monitor.sh`](scripts/run_monitor.sh) | Basic one-channel CR-110 monitoring |
| [`scripts/run_wavegen.sh`](scripts/run_wavegen.sh) | Controlled one-channel electronics bench test; do not use W1 during normal detector operation |
| [`scripts/run_2c_simulate.sh`](scripts/run_2c_simulate.sh) | Two-channel flat GUI demonstration without hardware |
| [`scripts/run_2c_monitor.sh`](scripts/run_2c_monitor.sh) | Initial two-channel purity-monitor hardware run without W1 or waveform-CSV overhead |

Scripts are examples, not fixed experimental configurations. Inspect and adjust their arguments before a new run.

---

## Key options

Commonly used options are:

| Option | Purpose |
|---|---|
| `--simulate` | Use synthetic input instead of opening the AD3 |
| `--headless` | Run without the Tk GUI |
| `--sample-rate` | Requested samples per second for each enabled channel |
| `--input-range` | AD3 input range in volts |
| `--min-charge-fc` | Absolute minimum accepted charge |
| `--no-save` | Disable summary and waveform output |
| `--save-waveforms` | Save decimated accepted waveform captures in addition to summaries |
| `--output-root` | Select a different output parent directory |
| `--dwf-library` | Supply a nonstandard WaveForms SDK library path |

`--canva-size` means a pulse count in `LiveDAQ_1c.py` and a time window in seconds in the flat programs. `LiveDAQ_2c.py` updates when a completed pair arrives and does not use this option.

---

## Output files

Saving is enabled by default unless `--no-save` is present. Each run creates a timestamped subdirectory.

Output directories are created automatically. The repository root must be writable when saving is enabled.

| Program family | Default parent directory | Summary | Optional waveform file |
|---|---|---|---|
| 1c non-flat and flat | `results/live/` | `pulses.csv` | `pulse_waveforms.csv` |
| 2c non-flat | `results/live_2c/` | `pulse_pairs.csv` | `pair_waveforms.csv` |
| 2c flat | `results/live_2c_flat/` | `pulse_pairs.csv` | `pair_waveforms.csv` |

Every saved run also contains `run_config.json` with the effective DAQ settings.

Use `--save-waveforms` only when accepted waveform captures are needed. CSV waveform writing adds storage and CPU load and may reduce the highest stable hardware sampling rate.

The waveform CSV files contain decimated accepted captures, not a complete raw continuous recording.

---

## Operating notes

### Charge values are preliminary

The live programs currently calculate:

```text
charge [fC] = abs(peak amplitude [V]) / gain [V/pC] * 1000
```

The default gain is the nominal CR-110 value of `1.4 V/pC`. Final purity-monitor operation requires channel-specific calibration and a validated waveform-integration or model-fitting method. Do not interpret the current live `Qa/Qc` as a final electron survival ratio without calibration.

### Noise learning

Every run begins with approximately one second of background-noise learning. Pulse detection is intentionally disabled until a robust noise estimate is ready.

If the program remains at `Learning noise`, check whether data are arriving continuously and whether lost samples repeatedly reset the detector.

### Lost and corrupted data

The GUI reports `lost` and `corrupted` counters from DWF.

- `lost > 0` means samples were overwritten before Python read them.
- `corrupted > 0` means DWF marked returned samples as unreliable.

For measurement data, both should remain zero. Lower the sampling rate, disable waveform saving, use headless/non-flat mode, or reduce other computer load if the counters increase.

### W1 safety

`--wavegen` enables AD3 W1. Use it only for a planned electronics bench test. Do not connect or enable W1 as part of normal detector acquisition unless the setup has been explicitly reviewed.

### Square-wave edge counting

A square wave contains a rising edge and a falling edge. With `--polarity both`, one generator cycle may produce one positive and one negative detected pulse. Select `--polarity positive` or `--polarity negative` when only one edge per cycle should be counted.

---

## Common troubleshooting

| Symptom | Checks |
|---|---|
| WaveForms shows only a DEMO device | Close WaveForms, connect and power the AD3, then reopen WaveForms |
| Python cannot open the AD3 | Close WaveForms; verify USB, power, and SDK installation; then check `--device-index` or `--dwf-library` |
| Program remains at `Learning noise` | Confirm acquisition time is advancing and repeated lost samples are not resetting the detector |
| A visible pulse is not detected | Check channel, ground, coupling, polarity, `--min-charge-fc`, and input clipping |
| Too many pulses are detected | Select one polarity, raise `--min-charge-fc`, and correct grounding or shielding |
| GUI becomes unresponsive | Stop stale DAQ processes, try `--headless` or non-flat mode, and reduce display or saving load |
| `lost` or `corrupted` is nonzero | Stop the run, retry with `--no-save`, verify USB, reduce system load, and lower the sampling rate if needed |

---

## Further documentation

- [`docs/notes_1c.md`](docs/notes_1c.md): detailed one-channel architecture, performance history, detector logic, and troubleshooting.
- [`docs/notes_2c.md`](docs/notes_2c.md): detailed two-channel architecture, pairing, validation status, calibration plan, limitations, and laboratory test sequence.

For a new user, read this README first, run a simulation, then read the channel-specific engineering notes before connecting detector hardware.
