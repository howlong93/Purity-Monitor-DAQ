# Purity Monitor DAQ System

Live data acquisition and monitoring for CR-150/CR-110 charge-sensitive preamplifiers using a Digilent Analog Discovery 3 (AD3).

This README is the practical starting point for detector users. Detailed engineering notes are available in [`docs/notes_1c.md`](docs/notes_1c.md) and [`docs/notes_2c.md`](docs/notes_2c.md).

> **Current status:** Simulation is available for every live program. Both optimized one-channel GUI variants have sustained 500 kS/s on the physical AD3 with `lost 0` and `corrupted 0` under the tested laboratory configuration. The two-channel programs still require validation with the physical AD3, two CR-110 readout chains, and the purity monitor. Displayed charge currently uses the nominal CR-110 gain and is not yet a detector-calibrated physics result.

## Contents

1. [System overview and validation status](#system-overview-and-validation-status)
2. [Installation](#installation)
3. [Choose a program](#choose-a-program)
4. [First simulation and success criteria](#first-simulation-and-success-criteria)
5. [First hardware run](#first-hardware-run)
6. [Stopping a run and CLI help](#stopping-a-run-and-cli-help)
7. [Example scripts](#example-scripts)
8. [Arguments common to all four live programs](#arguments-common-to-all-four-live-programs)
9. [Program-specific arguments](#program-specific-arguments)
10. [Output files](#output-files)
11. [Operating notes](#operating-notes)
12. [Common troubleshooting](#common-troubleshooting)
13. [Further documentation](#further-documentation)

---

## System overview and validation status

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

### Validated operating points

- One-channel non-flat: physical AD3 operation demonstrated at 500 kS/s with `lost 0` and `corrupted 0`.
- One-channel flat: physical AD3 operation demonstrated at 500 kS/s with `lost 0` and `corrupted 0`.
- Two-channel simulation: available for both GUI styles.
- Two-channel physical hardware: not yet validated with the complete purity monitor.

The 500 kS/s result is a demonstrated operating point for the tested computer, USB connection, script parameters, and output settings. Recheck the integrity counters after changing any of those conditions.

---

## Installation

Clone the repository and enter the repository root:

```bash
git clone https://github.com/howlong93/Purity-Monitor-DAQ.git
cd Purity-Monitor-DAQ
```

Run the commands below from the repository root unless stated otherwise.

### Software

- Python 3.10 or newer.
- NumPy.
- Tkinter for the GUI. It is normally included with standard Windows Python installations.
- Digilent WaveForms with the WaveForms SDK for physical AD3 operation.
- Bash for the supplied `.sh` scripts. On Windows, Git Bash is suitable.

Install the pip-managed dependency from [`requirements.txt`](requirements.txt):

```bash
python -m pip install -r requirements.txt
```

Simulation does not require an AD3 or the WaveForms SDK. Physical acquisition requires both.

On Windows, the programs automatically search the standard DWF locations:

```text
C:\Program Files\Digilent\WaveForms3\dwf.dll
C:\Program Files (x86)\Digilent\WaveForms3\dwf.dll
```

macOS and Linux use the normal WaveForms framework/shared-library locations. Only a nonstandard installation requires an explicit path:

```bash
python LiveDAQ_1c.py --dwf-library "D:/custom/path/dwf.dll"
```

Tkinter is installed with the standard Windows Python distribution but is not a pip package. On Linux, install the operating system's Tkinter package if GUI startup reports that it is missing.

### Before using physical hardware

1. Install Digilent WaveForms and its SDK.
2. Connect the AD3 before starting Python.
3. Close the WaveForms desktop application. WaveForms and Python cannot own the same AD3 simultaneously.
4. Verify BNC Adapter coupling, attenuation, grounding, and signal polarity.
5. Verify that the requested AD3 input range will not clip the signal.
6. Leave W1 disabled during detector operation. It is enabled only with `--wavegen`.

---

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

## First simulation and success criteria

Start with the one-channel non-flat simulation:

```bash
python LiveDAQ_1c.py \
  --simulate \
  --sample-rate 200000 \
  --simulation-charge-fc 100 \
  --min-charge-fc 40 \
  --canva-size 1 \
  --no-save
```

A successful first run should show all of the following:

1. The GUI opens without a Python import error.
2. The status changes from `Learning noise (1 s warm-up)` to `Running`.
3. Simulated pulses appear at approximately the configured event rate.
4. Reconstructed charge is near the configured `--simulation-charge-fc` value.
5. The status remains `lost 0` and `corrupted 0`.
6. Clicking **Stop** closes the acquisition worker cleanly.

If this test fails, resolve the software environment before connecting hardware.

### One-channel continuous-time simulation

Use the flat version to verify continuous non-overlapping waveform windows:

```bash
python LiveDAQ_1c_flat.py \
  --simulate \
  --sample-rate 200000 \
  --simulation-charge-fc 100 \
  --min-charge-fc 40 \
  --polarity positive \
  --canva-size 0.500 \
  --num-test 10 \
  --no-save
```

Here `--canva-size 0.500` means a 0.5-second waveform window and a 0.5-second canvas update interval.

### Two-channel event simulation

```bash
python LiveDAQ_2c.py \
  --simulate \
  --sample-rate 200000 \
  --simulation-cathode-charge-fc 100 \
  --simulation-anode-charge-fc 95 \
  --drift-time-us 55 \
  --drift-window-us 15 \
  --min-charge-fc 40 \
  --no-save
```

### Two-channel continuous-time simulation

```bash
python LiveDAQ_2c_flat.py \
  --simulate \
  --sample-rate 200000 \
  --simulation-cathode-charge-fc 100 \
  --simulation-anode-charge-fc 95 \
  --drift-time-us 55 \
  --drift-window-us 15 \
  --min-charge-fc 40 \
  --canva-size 0.500 \
  --no-save
```

Simulation verifies software behavior. It does not prove that a physical AD3, USB connection, or computer can sustain the requested sampling rate.

---

## First hardware run

### Pre-run checklist

1. Close the WaveForms desktop application.
2. Connect the AD3 and confirm its USB/power state.
3. Connect the intended CR-110 output to the correct AD3 channel.
4. Verify common ground, BNC Adapter coupling/attenuation, input range, and expected polarity.
5. Close the metal enclosure and keep sensitive wiring short.
6. Start with `--no-save` so disk output cannot affect the first throughput check.
7. Use a previously validated sampling rate before attempting a higher rate.
8. Wait for the one-second noise-learning period to finish.
9. Confirm the pulse polarity, amplitude, and approximate charge scale.
10. Require `lost 0` and `corrupted 0` throughout the run.
11. Enable summary or waveform saving only after a clean no-save test.

### One-channel non-flat hardware template

```bash
python LiveDAQ_1c.py \
  --sample-rate 200000 \
  --input-range 1.0 \
  --min-charge-fc 40 \
  --polarity positive \
  --canva-size 1 \
  --no-save
```

Adjust `--polarity`, `--min-charge-fc`, and `--input-range` to the measured hardware signal. Both optimized one-channel GUI variants have already sustained 500 kS/s without DWF integrity errors in the tested setup, but a new computer or configuration must be checked again.

### One-channel flat hardware template

```bash
python LiveDAQ_1c_flat.py \
  --sample-rate 200000 \
  --input-range 1.0 \
  --min-charge-fc 40 \
  --polarity positive \
  --canva-size 0.500 \
  --num-test 10 \
  --no-save
```

Use this version when the continuous baseline and the intervals between pulses are important.

### Two-channel hardware template

Close WaveForms, connect the cathode output to Channel 1 and the anode output to Channel 2, then run:

```bash
python LiveDAQ_2c_flat.py \
  --sample-rate 200000 \
  --input-range 1.0 \
  --channel-1-polarity negative \
  --channel-2-polarity positive \
  --drift-time-us 55 \
  --drift-window-us 15 \
  --min-charge-fc 40 \
  --canva-size 0.500 \
  --no-save
```

The current `55 +/- 15 us` pairing rule is provisional. Determine the correct timing from physical data before using pairing for production analysis. Two-channel physical throughput and pairing still require validation.

### Offline WaveForms CSV analysis

Place exported CSV files under `results/csv/` or pass an explicit file/directory path:

```bash
python DAQ_1c.py results/csv \
  --min-charge-fc 40 \
  --polarity positive \
  --output-dir results/offline_charge
```

The input CSV directory is not created with measurement data automatically; the user must supply the exported files.

---

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

Scripts are examples, not fixed experimental configurations. Open the relevant script and copy or adjust its arguments for a new run.

On Linux/macOS, the executable bit is stored in Git, so a script can also be launched directly:

```bash
./scripts/run_simulate.sh
```

---

## Arguments common to all four live programs

These arguments are accepted by all four live programs. Defaults that intentionally differ by program are identified explicitly.

| Argument | Datatype | Default | Meaning |
|---|---|---|---|
| `--simulate` | Boolean flag | Off | Use a synthetic source instead of opening physical AD3 hardware. |
| `--headless` | Boolean flag | Off | Disable Tk GUI and print accepted events to the terminal. Flat programs also avoid publishing plot data. |
| `--duration` | Float, seconds | `0.0` | Stop after this many wall-clock seconds. Zero runs until stopped manually. |
| `--sample-rate` | Float, samples/s | `500000.0` | Requested AD3 Record rate per enabled channel. Always check the actual rate and integrity counters. |
| `--input-range` | Float, volts | `1.0` | AD3 input range. Applies to Channel 1 and, in 2c programs, Channel 2. Increase it if clipping occurs. |
| `--gain-v-per-pc` | Float, V/pC | `1.4` | Nominal CR-110 charge-to-voltage gain. One global value currently applies to both 2c channels. |
| `--tau-us` | Float, microseconds | `140.0` | CR-110 decay constant used to size detection and capture intervals. One global value currently applies to both 2c channels. |
| `--threshold-sigma` | Float | `8.0` | Adaptive edge threshold in robust background-noise sigma. |
| `--min-charge-fc` | Float, fC | `1.0` | Absolute minimum accepted charge threshold. Practical scripts use higher values to reject noise. |
| `--polarity` | Choice string: `both`, `positive`, `negative` | 1c: `both`; 2c: unset | 1c: accepted pulse sign. 2c: optional compatibility override applied to both channels; channel-specific options are preferred. |
| `--pretrigger-ms` | Float, milliseconds | `0.5` | Waveform duration retained before detected onset. |
| `--posttrigger-ms` | Float, milliseconds | `1.5` | Waveform duration retained after detected onset. |
| `--gui-rate` | Float, Hz | Non-flat: `10.0`; 1c flat: `2.0`; 2c flat: `10.0` | GUI polling rate for non-flat programs. Accepted only for compatibility in flat programs; flat refresh timing is set by `--canva-size`. |
| `--num-test` | Integer | `10` | Number of result rows replaced together as one non-overlapping batch. |
| `--output-root` | Path | Program-dependent under `results/` | Parent directory for timestamped run folders. See [Output files](#output-files). |
| `--no-save` | Boolean flag | Off | Disable all summary and waveform output. Useful for throughput tests. |
| `--save-waveforms` | Boolean flag | Off | In addition to the default summary, save decimated accepted waveform captures. |
| `--wavegen` | Boolean flag | Off | Enable AD3 W1 square-wave output. Keep it off during normal detector operation. |
| `--wavegen-frequency` | Float, Hz | `500.0` | W1 square-wave frequency when `--wavegen` is enabled. |
| `--wavegen-vpp` | Float, volts | `0.100` | W1 peak-to-peak amplitude. |
| `--wavegen-offset` | Float, volts | `0.0` | W1 DC offset. |
| `--device-index` | Integer | `-1` | DWF device selection. `-1` requests automatic/first-device selection. |
| `--dwf-library` | String or path | Automatic (`None`) | Explicit WaveForms SDK library path if automatic discovery fails. |
| `--simulation-pulse-rate` | Float, Hz | `10.0` | Simulated physical-event repetition rate. |
| `--simulation-polarity` | Choice string: `positive`, `negative` | 1c: `negative`; 2c: unset | 1c simulated pulse sign. Retained only for CLI compatibility in 2c; 2c simulation uses negative cathode and positive anode defaults. |
| `--simulation-speed` | Float, multiplier | `1.0` | Simulated time divided by wall time. This does not model physical AD3 throughput. |

Boolean flags are enabled by including the argument, for example `--simulate`. Do not write `--simulate true`.

---

## Program-specific arguments

### `LiveDAQ_1c.py`

| Argument | Datatype | Default | Meaning |
|---|---|---:|---|
| `--canva-size` | Integer, pulses | `1` | Wait for this many newly accepted pulses, then replace the canvas with their aligned overlay. This is not a sliding window. |
| `--simulation-charge-fc` | Float, fC | `100.0` | Charge of each simulated one-channel pulse. |

Example:

```bash
python LiveDAQ_1c.py --simulate --canva-size 5 --simulation-charge-fc 100
```

### `LiveDAQ_1c_flat.py`

| Argument | Datatype | Default | Meaning |
|---|---|---:|---|
| `--canva-size` | Float, seconds | `0.500` | Duration of each consecutive non-overlapping raw Channel 1 canvas interval and its update period. |
| `--simulation-charge-fc` | Float, fC | `100.0` | Charge of each simulated one-channel pulse. |

Example:

```bash
python LiveDAQ_1c_flat.py --simulate --canva-size 0.500 --simulation-charge-fc 100
```

### `LiveDAQ_2c.py`

| Argument | Datatype | Default | Meaning |
|---|---|---:|---|
| `--channel-1-polarity` | Choice string: `both`, `positive`, `negative` | `negative` | Accepted polarity for cathode / Channel 1. |
| `--channel-2-polarity` | Choice string: `both`, `positive`, `negative` | `positive` | Accepted polarity for anode / Channel 2. |
| `--drift-time-us` | Float, microseconds | `55.0` | Expected anode-peak minus cathode-peak delay used as the pairing-window center. |
| `--drift-window-us` | Float, microseconds | `15.0` | Pairing tolerance around `--drift-time-us`. It must be smaller than the center value. |
| `--simulation-cathode-charge-fc` | Float, fC | `100.0` | Simulated cathode charge `Qc`. |
| `--simulation-anode-charge-fc` | Float, fC | `95.0` | Simulated anode charge `Qa`. |

There is no `--canva-size` in the non-flat two-channel program. Its canvas updates when a new completed pair reaches the GUI.

Example:

```bash
python LiveDAQ_2c.py \
  --simulate \
  --simulation-cathode-charge-fc 100 \
  --simulation-anode-charge-fc 95 \
  --drift-time-us 55 \
  --drift-window-us 15
```

### `LiveDAQ_2c_flat.py`

| Argument | Datatype | Default | Meaning |
|---|---|---:|---|
| `--channel-1-polarity` | Choice string: `both`, `positive`, `negative` | `negative` | Accepted polarity for cathode / Channel 1. |
| `--channel-2-polarity` | Choice string: `both`, `positive`, `negative` | `positive` | Accepted polarity for anode / Channel 2. |
| `--canva-size` | Float, seconds | `0.500` | Duration of each consecutive non-overlapping raw two-channel canvas interval and its update period. |
| `--drift-time-us` | Float, microseconds | `55.0` | Expected anode-peak minus cathode-peak delay used as the pairing-window center. |
| `--drift-window-us` | Float, microseconds | `15.0` | Pairing tolerance around `--drift-time-us`. It must be smaller than the center value. |
| `--simulation-cathode-charge-fc` | Float, fC | `100.0` | Simulated cathode charge `Qc`. |
| `--simulation-anode-charge-fc` | Float, fC | `95.0` | Simulated anode charge `Qa`. |

Example:

```bash
python LiveDAQ_2c_flat.py \
  --simulate \
  --canva-size 0.500 \
  --simulation-cathode-charge-fc 100 \
  --simulation-anode-charge-fc 95 \
  --drift-time-us 55 \
  --drift-window-us 15
```

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

### Pairing is provisional

The 2c programs currently pair pulses using cathode-to-anode **peak time**. The default accepted range is `55 +/- 15 us`, or approximately 40-70 us. Confirm the correct timing definition and range with physical purity-monitor data.

### Coupling and shielding

The Python programs do not configure physical AC/DC coupling on the BNC Adapter. Verify coupling, attenuation, grounding, enclosure closure, and cable routing before every hardware run.

### W1 safety

`--wavegen` enables AD3 W1. Use it only for a planned electronics bench test. Do not connect or enable W1 as part of normal detector acquisition unless the setup has been explicitly reviewed.

### Square-wave edge counting

A square wave contains a rising edge and a falling edge. With `--polarity both`, one generator cycle may produce one positive and one negative detected pulse. Select `--polarity positive` or `--polarity negative` when only one edge per cycle should be counted.

---

## Common troubleshooting

### WaveForms shows only a DEMO device

Close WaveForms, connect and power the AD3, then reopen WaveForms. During initial setup, starting the application before connecting the device caused only the DEMO entry to appear.

### Python cannot open the AD3

- Close the WaveForms desktop application so it releases exclusive device ownership.
- Verify USB and power.
- Confirm that WaveForms and the DWF SDK are installed.
- Use `--device-index` when multiple devices are attached.
- Use `--dwf-library` only when DWF is installed outside the standard location.

### The program remains at `Learning noise`

- Wait for at least one second of acquired sample time.
- Confirm that elapsed acquisition time is increasing.
- Check whether repeated lost samples are resetting the detector.
- Restart after correcting a DWF/device error.

### The program remains at `Waiting for a charge pulse`

- Verify the input channel and common ground.
- Check BNC Adapter coupling/attenuation.
- Verify `--polarity` or the channel-specific 2c polarity arguments.
- Lower `--min-charge-fc` carefully.
- Confirm that the signal is not clipped by `--input-range`.

### Too many pulses are detected

- Determine whether both square-wave edges are being counted.
- Select one polarity.
- Increase `--min-charge-fc` gradually.
- Correct shielding, grounding, and mains pickup before relying only on a higher software threshold.

### A pulse is visible but has no marker

- Verify polarity and threshold.
- Lower `--min-charge-fc` temporarily.
- Increase sample rate only while `lost` and `corrupted` remain zero.
- See the detector-threshold discussion in [`docs/notes_1c.md`](docs/notes_1c.md).

### `DATA WARNING`, lost, or corrupted becomes nonzero

1. Stop the run and do not treat it as production-quality data.
2. Restart with `--no-save`.
3. Use headless or non-flat mode to isolate GUI cost.
4. Close unnecessary applications and verify USB.
5. Reduce the sampling rate if necessary.
6. Re-enable saving only after a clean test.

### The GUI becomes unresponsive

- Stop any stale Python DAQ processes before launching another run.
- Confirm that flat implementations contain the min/max Canvas envelope optimization.
- Use `--headless` to determine whether acquisition/detection remains healthy without Tkinter.
- Use a shorter flat `--canva-size` or non-flat mode when continuous context is unnecessary.

### Bash reports an option as a command

Every continued command line must end with `\` as its final character. Remove trailing spaces or tabs after a continuation backslash.

### `--canva-size` is rejected

- `LiveDAQ_1c.py` requires a positive integer pulse count, such as `--canva-size 5`.
- Flat programs require a positive duration in seconds, such as `--canva-size 0.500`.
- `LiveDAQ_2c.py` does not use `--canva-size` because its canvas updates when a completed pair arrives.

---

## Further documentation

- [`docs/notes_1c.md`](docs/notes_1c.md): detailed one-channel architecture, performance history, detector logic, and troubleshooting.
- [`docs/notes_2c.md`](docs/notes_2c.md): detailed two-channel architecture, pairing, validation status, calibration plan, limitations, and laboratory test sequence.

For a new user, read this README first, run a simulation, then read the channel-specific engineering notes before connecting detector hardware.
