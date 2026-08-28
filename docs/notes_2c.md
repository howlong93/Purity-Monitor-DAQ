# Two-Channel Purity Monitor Data Acquisition System

## Engineering Notes and Developer Handoff

**Project context:** SLAC liquid-argon purity monitor development

**Hardware target:** Two CR-150-R6 assemblies with CR-110 charge-sensitive preamplifiers, one Digilent Analog Discovery 3 (AD3), and Digilent BNC Adapter connections

**Channel assignment:** AD3 Scope Channel 1 = cathode / `Qc`; AD3 Scope Channel 2 = anode / `Qa`

**Software generation:** Two input channels, identified by the `_2c` filename suffix

**Document status:** Initial two-channel implementation, before physical dual-channel hardware validation

**Validation status:** Simulation and software-level tests are working. Operation with two physical CR-110 channels, the AD3, and the purity monitor has not yet been validated.

**Primary files:** [`LiveDAQ_2c.py`](../LiveDAQ_2c.py) | [`LiveDAQ_2c_flat.py`](../LiveDAQ_2c_flat.py) | [`run_2c_simulate.sh`](../scripts/run_2c_simulate.sh)

**Related documentation:** [`notes_1c.md`](notes_1c.md)

---

## Contents

1. [Purpose and scope](#1-purpose-and-scope)
2. [Physical-system model and current assumptions](#2-physical-system-model-and-current-assumptions)
3. [Current two-channel software](#3-current-two-channel-software)
4. [Common acquisition architecture](#4-common-acquisition-architecture)
5. [Pulse detection and noise estimation](#5-pulse-detection-and-noise-estimation)
6. [Cathode/anode pairing](#6-cathodeanode-pairing)
7. [Online charge calculation](#7-online-charge-calculation)
8. [GUI behavior](#8-gui-behavior)
9. [Command-line examples](#9-command-line-examples)
10. [Output files](#10-output-files)
11. [Work already tested and confirmed](#11-work-already-tested-and-confirmed)
12. [Work still requiring physical validation](#12-work-still-requiring-physical-validation)
13. [Calibration required for detector operation](#13-calibration-required-for-detector-operation)
14. [Known limitations and risks](#14-known-limitations-and-risks)
15. [Recommended laboratory validation sequence](#15-recommended-laboratory-validation-sequence)
16. [Future development priorities](#16-future-development-priorities)
17. [Developer handoff checklist](#17-developer-handoff-checklist)

---

## 1. Purpose and scope

The two-channel DAQ is intended to replace oscilloscope-centered readout with continuous computer acquisition from both purity-monitor readout channels.

The immediate goals are to:

1. Continuously acquire cathode and anode CR-110 outputs from the same AD3.
2. Detect cathode and anode pulses independently.
3. Pair pulses that belong to the same electron-drift event.
4. Report cathode charge `Qc`, anode charge `Qa`, their peak-time separation, and `Qa/Qc`.
5. Show the two channels together on a common time axis.
6. Save pair summaries and, optionally, accepted waveforms.
7. Support simulation and headless development when laboratory hardware is unavailable.

The current programs are an engineering prototype for acquisition, detection, pairing, and live monitoring. They do **not** yet implement the final physics-grade purity analysis.

In particular, the current online `Qc` and `Qa` values are derived from peak amplitude and nominal CR-110 gain. A physics-grade analysis may instead need waveform integration or model fitting to account for finite current duration, CSP response, and the unexpected anode bump. This distinction is essential.

---

## 2. Physical-system model and current assumptions

### 2.1 Planned signal chain

The intended system is:

```text
Purity-monitor cathode
        |
        v
Cathode CR-150-R6 + CR-110
        |
        v
AD3 Scope Channel 1 --------------+
                                    |
                                    v
                             LiveDAQ_2c*.py
                                    ^
                                    |
AD3 Scope Channel 2 --------------+
        ^
        |
Anode CR-150-R6 + CR-110
        ^
        |
Purity-monitor anode
```

Both channels use the same AD3 acquisition clock and shared sample index. The software therefore treats their samples as simultaneous.

### 2.2 Channel assignment

The software assumes:

| AD3 input | Detector signal | Reported charge | Default polarity |
|---|---|---|---|
| Scope Channel 1 | Cathode circuit output | `Qc` | Negative |
| Scope Channel 2 | Anode circuit output | `Qa` | Positive |

The physical polarity must be verified in the laboratory. Override the defaults with:

```text
--channel-1-polarity positive|negative|both
--channel-2-polarity positive|negative|both
```

The compatibility option `--polarity` overrides both channels together. Channel-specific options are preferred for normal two-channel operation.

### 2.3 Timing assumption

The initial expected cathode-to-anode delay is based on the estimate that the relevant drift time is approximately `50-60 us`.

The software currently defines this parameter as:

```text
peak delay = anode peak sample - cathode peak sample
```

Default pairing settings are:

```text
--drift-time-us 55
--drift-window-us 15
```

The default accepted peak-to-peak interval is therefore `40-70 us`, including the boundaries after conversion to samples.

This is a provisional software definition. It has not yet been demonstrated that the estimated `50-60 us` physical drift time corresponds to cathode-to-anode **peak time**. It may instead refer to `T2`, a start-time separation, or another fit-derived interval.

### 2.4 Expected real waveform

The simulated source generates relatively simple CR-110-like exponential pulses. Real purity-monitor signals are more complicated:

- The cathode signal may have a finite rise/current interval rather than an instantaneous step.
- The anode signal may have a persistent feature or "bump" before the main anode peak.
- Cathode and anode signal durations can differ.
- Electromagnetic interference from the flash lamp or trigger electronics may create additional spikes.
- AC coupling, enclosure shielding, cable routing, and grounding can change the observed baseline and shape.

Simulation success therefore verifies software behavior, not real-waveform acceptance.

---

## 3. Current two-channel software

### 3.1 [`LiveDAQ_2c.py`](../LiveDAQ_2c.py) - event-centered non-flat monitor

This is the primary two-channel event monitor.

Its functions are:

- Open one AD3 through the Digilent WaveForms SDK.
- Enable Scope Channels 1 and 2 in continuous Record mode.
- Read both channels for the same available sample count.
- Learn independent Channel 1 and Channel 2 noise levels.
- Detect cathode and anode pulses independently.
- Pair pulses by peak-time separation.
- Calculate preliminary `Qc`, `Qa`, and `Qa/Qc`.
- Refresh the canvas after a newly completed pair is received by the GUI.
- Display the latest pair using both channel waveforms on a common time axis.
- Update the result table in non-overlapping batches, defaulting to ten pairs.
- Save pair summaries and optional accepted waveforms.
- Run with hardware, simulation, GUI, or headless operation.

The non-flat canvas displays one most-recent pair. Channel 1 is blue and Channel 2 is pink. Both traces are baseline corrected for display and aligned to cathode peak time.

If multiple pairs arrive between GUI refreshes, the newest received pair is drawn. Every accepted pair is still passed to the result batching and logger.

### 3.2 [`LiveDAQ_2c_flat.py`](../LiveDAQ_2c_flat.py) - continuous-time diagnostic monitor

This program uses the same two-channel acquisition, detection, pairing, noise, logging, and charge-estimation logic. Its canvas instead shows consecutive raw time intervals.

Flat-specific behavior:

- `--canva-size` is a positive duration in seconds.
- `--canva-size 0.500` creates consecutive non-overlapping 0.5-second windows.
- The canvas is replaced after each complete window; it is not a sliding window.
- Both channels use exactly the same absolute sample/time interval.
- A completed interval retains at most 50,000 timestamp positions for plotting.
- Each retained timestamp has one Channel 1 voltage and one Channel 2 voltage.
- A vectorized min/max envelope is applied independently to each channel before sending coordinates to Tk Canvas.
- The envelope retains narrow positive and negative excursions better than simple every-Nth-point drawing.
- An incomplete flat interval is discarded after lost samples, corrupted samples, or a sample-index discontinuity.
- Plot chunks are not published in headless mode.
- Result rows still update independently in batches of `--num-test` pairs.

Examples for a 0.5-second canvas:

| Acquisition rate | Raw timestamps per interval | Retained plot timestamps |
|---:|---:|---:|
| 50 kS/s | 25,000 | 25,000 |
| 200 kS/s | 100,000 | 50,000 |
| 500 kS/s | 250,000 | 50,000 |

Because there are two channels, 50,000 retained timestamps correspond to two voltage arrays of up to 50,000 values each.

### 3.3 Example scripts

The current two-channel scripts are:

- [`run_2c_simulate.sh`](../scripts/run_2c_simulate.sh): flat two-channel simulation at 200 kS/s with a 0.5-second canvas.
- [`run_2c_monitor.sh`](../scripts/run_2c_monitor.sh): initial non-flat physical two-channel monitor at 200 kS/s, with summary saving enabled and W1 disabled.

Scripts are working-directory independent because they change to their own directory before launching Python.

---

## 4. Common acquisition architecture

### 4.1 Hardware data path

The hardware source follows this sequence:

1. Load the WaveForms SDK library (`dwf.dll` on the current Windows system).
2. Open the AD3 with exclusive ownership.
3. Disable automatic reconfiguration.
4. Enable analog input Channels 1 and 2.
5. Apply the same requested input range and average filter to both channels.
6. Select continuous Record acquisition mode.
7. Configure the requested sampling rate and infinite record length.
8. Read the actual configured sampling rate after applying the configuration.
9. Poll the Record buffer.
10. Read the same available sample count from Channel 1 and Channel 2.
11. Attach one shared start sample, lost count, and corrupted count to the dual-channel chunk.

The WaveForms desktop application must be closed before hardware mode. Only one process can own the AD3 at a time.

### 4.2 Acquisition worker

The acquisition worker performs the time-sensitive path:

```text
Read dual-channel DWF chunk
        |
        +--> Channel 1 detector --> cathode candidates
        |
        +--> Channel 2 detector --> anode candidates
        |
        +--> pair cathode/anode pulses
        |
        +--> log accepted pairs
        |
        +--> publish accepted pairs to GUI
```

For the flat program only, the worker also places the original dual-channel chunk into a separate plot queue **by reference**. Window construction, 50k downsampling, envelope reduction, and Tk drawing remain in the GUI thread.

The current design does not yet use a dedicated hardware-read thread separate from detection and logging. If dual-channel hardware operation cannot sustain the required rate, this remains a major optimization option.

### 4.3 Shared state

The GUI reads a lock-protected status snapshot containing:

- Program status.
- Source and DWF version.
- Actual sample rate.
- Acquisition elapsed time.
- Completed pair count.
- Independently detected cathode and anode counts.
- Unmatched cathode and anode counts.
- Lost and corrupted sample totals.
- Separate Channel 1 and Channel 2 noise estimates.
- Output directory.
- Fatal error text.

Accepted pairs use a thread-safe queue. Flat raw chunks use a separate `SimpleQueue`.

---

## 5. Pulse detection and noise estimation

### 5.1 Independent channel detectors

Each channel has its own `StreamingPulseDetector` instance. They share configured values such as nominal CR-110 gain, tau, threshold multiplier, and minimum charge, but maintain independent:

- Signal buffers.
- Baseline estimates.
- Noise samples.
- Median/MAD noise results.
- Active pulse capture.
- Refractory state.
- Pulse count.

Channel 2 is not used as a trigger reference for Channel 1. Both signals must independently satisfy their detection criteria before pairing.

### 5.2 One-second noise learning

Both detectors begin with a one-second learning period. Approximately 5,000 background edge samples per channel per second are selected across the interval.

Noise is calculated using robust statistics:

```text
center = median(background edge samples)
MAD = median(abs(background - center))
edge sigma = 1.4826 * MAD
sample sigma = edge sigma / sqrt(2)
```

The program reports `Running` only after both channel noise estimates are ready.

Pulse-capture regions are excluded from the background sample set. The fixed approximately 5,000-sample workload prevents the once-per-second noise calculation from growing with acquisition rate.

### 5.3 Detection threshold

For each channel:

```text
minimum amplitude = min_charge_fc * gain_v_per_pc / 1000
adaptive edge threshold = threshold_sigma * noise_sigma * sqrt(2)
detection threshold = max(minimum amplitude, adaptive edge threshold)
```

The default threshold multiplier is `8 sigma`. The same `--min-charge-fc` currently applies to both channels.

This may be inadequate if the real anode and cathode signal amplitudes or noise floors differ substantially. Channel-specific minimum charges and gains are recommended future additions.

### 5.4 Capture and peak estimate

After an edge candidate:

1. A local pre-pulse baseline is estimated with a median.
2. The detector waits for the configured post-trigger capture to complete.
3. It searches a finite region for the largest absolute deviation from baseline.
4. Polarity and final amplitude threshold are checked.
5. The accepted pulse receives a sample index, elapsed time, microsecond-resolution timestamp, polarity, peak voltage, baseline, amplitude, and nominal charge.

The two-channel implementation uses a longer peak-search fraction than the original early one-channel detector to better tolerate the expected finite purity-monitor pulse rise. This is still not a physics waveform fit.

---

## 6. Cathode/anode pairing

### 6.1 Pairing rule

Detected pulses are kept in chronological cathode and anode queues. The oldest available pulses are compared.

For cathode peak sample `Sc` and anode peak sample `Sa`:

```text
delay = (Sa - Sc) / sample_rate
```

A pair is accepted when the delay is inside:

```text
drift_time_us +/- drift_window_us
```

With current defaults:

```text
40 us <= delay <= 70 us
```

### 6.2 Unmatched pulses

The pairer does not force unrelated pulses into a pair.

- An anode pulse that is too early is counted as an unmatched anode.
- A cathode whose candidate anode is too late is counted as an unmatched cathode.
- A lost-data gap clears pending queues and counts their contents as unmatched.
- Each pending queue is capped at 1,000 pulses to prevent unbounded memory growth if one channel disappears entirely.

The GUI reports total detected C/A pulses and unmatched C/A pulses. A rising unmatched count is a diagnostic signal and must not be ignored.

### 6.3 Current pairing assumptions

The algorithm assumes:

- Events are chronologically ordered.
- Each physical event produces at most one accepted cathode and one accepted anode pulse.
- Event spacing is much larger than the pairing window.
- The expected 10 Hz flash rate makes ambiguous overlap unlikely.

Noise spikes, the anode bump, multiple features passing threshold, or a wrong timing definition can violate these assumptions.

---

## 7. Online charge calculation

### 7.1 Current preliminary conversion

Both channels currently use one global nominal CR-110 conversion:

```text
nominal gain = 1.4 V/pC
signed charge [fC] = amplitude [V] / gain [V/pC] * 1000
reported Qc or Qa = abs(signed charge)
Qa/Qc = absolute anode charge / absolute cathode charge
```

The signed charge is retained in CSV output, while the GUI uses positive charge magnitudes.

### 7.2 Why this is not yet final physics charge

Peak amplitude is a practical online estimator for the bench-test pulse used during development. For the actual purity-monitor signal, it can depend on:

- Current duration in the cathode or anode region.
- CR-110 decay constant.
- Finite rise time.
- Electron attachment during drift.
- Channel bandwidth.
- AC coupling.
- The anode bump.
- Baseline and fit-window selection.

The thesis states that collected charge is proportional to the voltage-waveform integral and later uses waveform models to extract the relevant parameters. A production purity result should therefore not be based solely on the present peak-amplitude estimate.

### 7.3 Interpretation of live `Qa/Qc`

The current ratio is useful for:

- Verifying two-channel acquisition.
- Checking whether both channels respond consistently.
- Monitoring gross changes.
- Testing pairing and GUI behavior.

It should not yet be interpreted as a calibrated electron survival fraction or used to report liquid-argon purity.

---

## 8. GUI behavior

### 8.1 Common result table

Both programs display the same fixed-width result table. It has:

- A Channel 1 cathode / `Qc` group.
- A Channel 2 anode / `Qa` group.
- Peak time, elapsed time, peak amplitude, and charge for each channel.
- Derived peak delay and `Qa/Qc`.

Column headers are separate from the values. All fields use fixed widths so the vertical separators remain aligned as elapsed time and numerical values change.

The table updates in complete, non-overlapping batches controlled by:

```text
--num-test 10
```

The next display uses the next ten pairs, not a sliding last-ten-pair window.

### 8.2 Non-flat canvas

[`LiveDAQ_2c.py`](../LiveDAQ_2c.py) draws the most recent accepted pair:

- Channel 1 cathode in blue.
- Channel 2 anode in pink.
- Common time origin at the cathode peak.
- Both baselines subtracted for visual comparison.
- Peak markers for both channels.

The non-flat program intentionally has no `--canva-size` parameter.

### 8.3 Flat canvas

[`LiveDAQ_2c_flat.py`](../LiveDAQ_2c_flat.py) draws raw consecutive acquisition time:

- Channel 1 and Channel 2 share an absolute time axis.
- Raw voltage is displayed without per-pulse baseline subtraction.
- The default window is 0.5 seconds.
- The canvas updates once per complete window.
- Cathode and anode peaks belonging to accepted pairs are marked separately.
- The 50k timestamp cap and min/max envelope protect GUI performance.

The retained data are for display only. Detection operates on the full acquired chunks before display downsampling.

---

## 9. Command-line examples

Run commands from the repository root unless an example states otherwise.

### 9.1 Non-flat simulation

```bash
python LiveDAQ_2c.py \
  --simulate \
  --sample-rate 200000 \
  --simulation-pulse-rate 10 \
  --simulation-cathode-charge-fc 100 \
  --simulation-anode-charge-fc 95 \
  --drift-time-us 55 \
  --drift-window-us 15 \
  --min-charge-fc 40 \
  --num-test 10 \
  --no-save
```

### 9.2 Flat simulation

```bash
python LiveDAQ_2c_flat.py \
  --simulate \
  --sample-rate 200000 \
  --simulation-pulse-rate 10 \
  --simulation-cathode-charge-fc 100 \
  --simulation-anode-charge-fc 95 \
  --drift-time-us 55 \
  --drift-window-us 15 \
  --min-charge-fc 40 \
  --canva-size 0.500 \
  --num-test 10 \
  --no-save
```

### 9.3 Non-flat hardware template

```bash
python LiveDAQ_2c.py \
  --sample-rate 200000 \
  --input-range 1.0 \
  --channel-1-polarity negative \
  --channel-2-polarity positive \
  --drift-time-us 55 \
  --drift-window-us 15 \
  --min-charge-fc 40 \
  --num-test 10 \
  --save-waveforms
```

### 9.4 Flat hardware template

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
  --num-test 10 \
  --save-waveforms
```

Do not enable `--wavegen` during normal detector operation. W1 is off unless that option is explicitly supplied.

### 9.5 Headless performance check

```bash
python LiveDAQ_2c.py \
  --simulate \
  --headless \
  --duration 10 \
  --sample-rate 200000 \
  --simulation-cathode-charge-fc 100 \
  --simulation-anode-charge-fc 95 \
  --min-charge-fc 40 \
  --no-save
```

Headless simulation does not validate GUI performance or physical DWF transfer.

---

## 10. Output files

### 10.1 Default output directories

Non-flat:

```text
results/live_2c/<run timestamp>/
```

Flat:

```text
results/live_2c_flat/<run timestamp>/
```

Use `--output-root` to change the parent directory. Use `--no-save` to disable output.

### 10.2 `run_config.json`

This file stores the effective configuration, including:

- Requested sample rate and input range.
- Nominal gain and tau.
- Detection threshold parameters.
- Channel polarities.
- Capture durations.
- Pairing center and window.
- GUI/result settings.
- Flat canvas duration when applicable.
- W1 settings.

It does not currently record independently calibrated channel constants because the code does not yet support them.

### 10.3 `pulse_pairs.csv`

Each accepted pair has one row containing:

- Pair number.
- Full cathode timestamp and elapsed time.
- Cathode peak sample, polarity, peak voltage, baseline, amplitude, signed charge, and absolute `Qc`.
- Full anode timestamp and elapsed time.
- Anode peak sample, polarity, peak voltage, baseline, amplitude, signed charge, and absolute `Qa`.
- Peak-to-peak delay in microseconds.
- `Qa/Qc`.

Timestamps use microsecond precision because the two peaks may be separated by only tens of microseconds.

### 10.4 `pair_waveforms.csv`

Created only with `--save-waveforms`.

The file uses long format:

```text
pair, channel, time_from_cathode_peak_s, voltage_v
```

Accepted waveforms are decimated before CSV writing to limit file size. This file is not a complete raw continuous record.

---

## 11. Work already tested and confirmed

### 11.1 Non-flat software and simulation

Confirmed so far:

- The module compiles successfully.
- The CLI exposes separate simulated cathode and anode charge parameters.
- A 200 kS/s simulated dual-channel stream can be processed.
- Simulated negative cathode and positive anode pulses are independently detected.
- Pulses with a simulated 55 us delay are paired correctly.
- Simulated `Qc`, `Qa`, and `Qa/Qc` are reported with expected approximate values.
- Pair timestamps retain microsecond precision.
- Headless simulation reports zero lost, corrupted, and unmatched data under the tested conditions.
- Pairing-window boundary behavior has been tested.
- Pending-pair queue protection has been tested.
- `pulse_pairs.csv`, `pair_waveforms.csv`, and `run_config.json` are generated with the expected fields.
- The user has run the non-flat simulation GUI and observed correct results.
- Fixed-width table headers and values retain identical separator positions for tested small and large numerical values.

### 11.2 Flat software and simulation

Confirmed so far:

- The module compiles successfully.
- Headless dual-channel simulation and pairing work at a requested 200 kS/s.
- A 0.5-second window at 200 kS/s is constructed as 100,000 raw timestamps.
- The retained plotting data are capped at 50,000 timestamps.
- Both channels retain the same timestamp offsets.
- Incomplete windows reset after a simulated data gap.
- Min/max envelope reduction preserves tested positive and negative extrema.
- Result-table formatting is inherited from and consistent with the non-flat program.
- A zero or negative `--canva-size` is rejected.
- The flat file is standalone and does not import [`LiveDAQ_2c.py`](../LiveDAQ_2c.py).

### 11.3 What these tests establish

The completed tests establish that the software architecture, simulation, detector instances, pairing, table formatting, logging, flat-window accumulation, and display reduction behave as designed for synthetic data.

They do not establish:

- Physical AD3 dual-channel throughput.
- Real CR-110 waveform detection.
- Correct detector charge calibration.
- Correct physical drift-time definition.
- Physics accuracy of `Qa/Qc`.

---

## 12. Work still requiring physical validation

### 12.1 AD3 dual-channel acquisition rate

The primary performance target remains at least:

```text
200 kS/s per enabled channel
```

The following must be measured with the physical AD3:

- Actual configured rate reported by DWF.
- Lost sample count over a long run.
- Corrupted sample count over a long run.
- CPU usage.
- GUI responsiveness.
- Difference between non-flat, flat, headless, saving, and no-saving modes.
- Maximum stable rate with both channels enabled.

One-channel performance does not guarantee the same two-channel rate because DWF transfer volume and Python processing are both larger.

### 12.2 Real pulse detection

Verify separately for cathode and anode:

- Actual polarity.
- Typical peak amplitude and charge range.
- Noise floor.
- Baseline stability.
- Rise/current duration.
- Decay constant.
- Whether one physical event produces exactly one accepted pulse per channel.
- Whether the anode bump or trigger EMI creates extra accepted pulses.
- Appropriate `--threshold-sigma` and `--min-charge-fc`.
- Appropriate pre-trigger, post-trigger, peak-search, and refractory durations.

### 12.3 Pairing and timing

Determine experimentally:

- Whether 50-60 us is peak-to-peak.
- The actual distribution of accepted cathode-to-anode peak delays.
- Whether the pairing center changes with electric field, liquid temperature, detector dimensions, or purity-monitor geometry.
- The narrowest safe pairing window.
- Whether false pairs occur during noise bursts or missing-channel events.

### 12.4 GUI validation

For non-flat mode:

- Confirm the displayed captures include the full physical cathode and anode shapes.
- Confirm the common time alignment is correct.
- Confirm rapid consecutive pairs do not cause misleading display behavior.

For flat mode:

- Confirm 200 kS/s or higher remains stable with the GUI open.
- Confirm 0.5 seconds is a useful viewing interval.
- Confirm the envelope displays narrow real pulses without hiding their shape.
- Confirm the shared vertical scale remains readable when channel amplitudes differ.
- Confirm the plot queue does not grow during long runs.

### 12.5 Saving and long-run behavior

Measure:

- Performance with summary saving enabled.
- Performance with `--save-waveforms` enabled.
- Output growth over hours.
- Whether CSV line-buffered writes create lost/corrupted data.
- Whether a binary or chunked waveform format is required.

---

## 13. Calibration required for detector operation

This section focuses on the calibration required by the two-channel DAQ.

### 13.1 Separate channel gain calibration

The current code assumes both CR-110 channels have exactly the same `1.4 V/pC` gain. This is not sufficient when the final quantity is a precise ratio.

For each complete channel, including CR-150, CR-110, cable, BNC Adapter, and AD3 input:

1. Inject known charge using the test input.
2. Use several charge values spanning the expected detector range.
3. Measure output amplitude and waveform integral.
4. Fit gain and linearity separately for cathode and anode channels.
5. Repeat to estimate run-to-run stability and uncertainty.

For a test capacitor:

```text
Qin = Ctest * DeltaV
```

The actual test-capacitor value and tolerance must be included. A nominal capacitor value with large tolerance cannot support a high-precision absolute calibration without measurement or an uncertainty contribution.

The software will eventually need separate parameters such as:

```text
channel_1_gain_v_per_pc
channel_2_gain_v_per_pc
```

### 13.2 Separate decay-time calibration

Measure the CR-110 decay constant for each assembled channel rather than assuming exactly 140 us.

Channel-specific tau affects:

- Pulse model.
- Peak response for finite-duration current.
- Baseline recovery.
- Capture and refractory timing.
- Waveform-integral or deconvolution analysis.

The final software should store each channel's measured tau and calibration date in the run configuration.

### 13.3 Relative timing calibration

Split or inject the same fast signal into both readout paths and measure:

- AD3 channel-to-channel sample alignment.
- Cable-delay difference.
- CR-150/CR-110 timing difference.
- Any systematic peak-time bias caused by different pulse shapes.

Subtract the measured electronics skew before interpreting the residual delay as detector drift.

### 13.4 Cross-talk measurement

Inject charge into one channel while observing both inputs.

Measure:

- Channel 1 injection appearing on Channel 2.
- Channel 2 injection appearing on Channel 1.
- Cross-talk polarity, amplitude, and delay.
- Dependence on enclosure, cable routing, grounding, and input range.

Cross-talk at the expected drift delay could create false pairs. Near-zero-delay cross-talk could affect baseline or start-time extraction.

### 13.5 Baseline and noise characterization

For each channel and system state, measure:

- RMS and robust MAD-based noise.
- Noise spectrum, especially 60 Hz and harmonics.
- Baseline drift over minutes and hours.
- Noise with the metal enclosure open and closed.
- Noise with HV supplies, flash lamp, trigger electronics, and other laboratory equipment on and off.
- Correlated noise between Channels 1 and 2.

The present detector estimates each channel's noise independently. Correlated two-channel noise rejection is not implemented.

### 13.6 Input range and termination

Record and calibrate:

- AD3 input range.
- BNC Adapter attenuation or gain setting.
- AC/DC coupling setting.
- Input impedance and any termination.
- Cable attenuation.
- CR-110 output loading.

An impedance or attenuation mismatch can create a factor-of-two or other systematic gain error. The physical adapter configuration is not controlled by the Python program.

### 13.7 Physics waveform reconstruction

For final `Qc` and `Qa`, develop and validate one of the following:

- Waveform integration with baseline and tail correction.
- CSP-response deconvolution.
- Simultaneous physics-based waveform fitting.

The analysis must address:

- Finite `T1` and `T3` current intervals.
- Channel-specific tau.
- Electron attachment model where required.
- Anode bump separation.
- EMI spike rejection or modeling.
- Fit-quality and event-rejection criteria.
- Uncertainty propagation.

Peak amplitude can remain as a fast online diagnostic even after a calibrated reconstruction is added.

### 13.8 Ratio calibration and uncertainty

The final ratio must account for unequal channel response:

```text
Qc = cathode observable / calibrated cathode response
Qa = anode observable / calibrated anode response
ratio = Qa / Qc
```

An uncertainty budget should include at least:

- Test-charge uncertainty.
- Gain-fit uncertainty.
- Gain drift.
- Baseline uncertainty.
- Noise and event-to-event variation.
- Waveform-model uncertainty.
- Anode-bump correction.
- Timing-window uncertainty.
- Cross-talk.
- ADC/channel nonlinearity.

The target is not merely to detect two pulses. The ratio precision must be sufficient for the intended electron-lifetime and impurity measurement.

---

## 14. Known limitations and risks

### 14.1 Shared detector parameters

Gain, tau, minimum charge, pre-trigger duration, and post-trigger duration are currently shared by both channels. Real channels may require different settings.

### 14.2 Peak-based charge

Current charge is based on one peak sample relative to a local baseline. It is sensitive to noise, sample phase, finite rise time, and pulse shape.

### 14.3 Simplified simulation

The simulated pulses do not reproduce the full cathode current interval, anode bump, lamp EMI, real cross-talk, gain mismatch, or channel-specific tau.

### 14.4 Provisional timing definition

`--drift-time-us` currently means expected peak-to-peak delay. This may not match the physical timing parameter required for electron lifetime.

### 14.5 Chronological one-to-one pairing

The matcher is intentionally simple. Multiple detected features from one physical event can cause incorrect unmatched counts or pairing.

### 14.6 Corrupted-data policy differs between detection and flat plotting

The flat plot accumulator discards an incomplete interval when DWF reports corrupted samples. The detector currently resets on lost/discontinuous samples but may still process data from a chunk marked corrupted.

A production policy should decide whether corrupted chunks must also be excluded from pulse detection and logging.

### 14.7 Logging remains in the acquisition worker

CSV writing occurs in the same worker that performs acquisition and detection. Slow storage or waveform output can delay hardware reads.

### 14.8 Flat plot queue is unbounded

The flat `SimpleQueue` has no explicit maximum. If the GUI cannot consume chunks as quickly as they are produced, memory can grow. The 50k cap applies after a window is accumulated; it does not bound queued raw chunks.

### 14.9 No complete raw continuous recording

`pair_waveforms.csv` stores decimated accepted captures only. Rejected pulses, unmatched signals, and most background data are not preserved.

### 14.10 No permanent automated test suite

Development checks have been run manually, but the repository does not yet contain a maintained unit/integration test suite for detector, pairing, gap, logger, and flat-window behavior.

### 14.11 Simulation speed is not hardware throughput

The Python simulated source generates samples in a loop and has different performance characteristics from DWF NumPy array transfer. Neither slow nor fast simulation directly establishes physical AD3 performance.

---

## 15. Recommended laboratory validation sequence

### Phase 1 - AD3 two-channel loopback

1. Close WaveForms.
2. Feed a known common signal to both AD3 inputs without the CR-110 system.
3. Confirm polarity, amplitude, sample alignment, and relative delay.
4. Run at 50, 100, 200, and higher kS/s.
5. Compare non-flat, flat, and headless modes.
6. Record lost/corrupted counts for at least several minutes at each setting.

### Phase 2 - Two CR-110 calibration channels

1. Connect one CR-150/CR-110 chain to each AD3 channel.
2. Inject known charge into both test inputs.
3. Verify both measured decay constants.
4. Measure channel gain and linearity.
5. Measure timing skew using a common stimulus.
6. Inject only one channel at a time to measure cross-talk.
7. Determine safe input range and thresholds.

### Phase 3 - Pairing bench test

1. Generate controlled cathode/anode-like pulses with a known delay.
2. Sweep delay across and outside the pairing window.
3. Verify pair count and unmatched count.
4. Test missing cathode and missing anode conditions.
5. Add controlled noise or interference.
6. Verify that false features are not paired.

### Phase 4 - Purity-monitor connection

1. Verify grounding, shielding, coupling, HV routing, and detector safety before acquisition.
2. Begin at a conservative sample rate and input range.
3. Use flat mode to inspect raw cathode, anode, bump, and EMI timing.
4. Determine real polarity, amplitude, pulse duration, and delay distribution.
5. Tune detector and pairing parameters based on recorded evidence.
6. Save sufficient raw data for offline reconstruction development.
7. Increase the sampling rate and repeat the data-integrity test.

### Phase 5 - Physics validation

1. Implement calibrated per-channel charge extraction.
2. Compare Python results with the established oscilloscope/offline analysis.
3. Validate `Qc`, `Qa`, timing parameters, and `Qa/Qc` on the same events or averaged dataset.
4. Quantify bias and uncertainty.
5. Only then derive electron lifetime and impurity concentration from this DAQ.

---

## 16. Future development priorities

Recommended order:

1. Complete physical dual-channel AD3 throughput testing.
2. Record representative real cathode and anode waveforms.
3. Confirm timing and pairing definitions.
4. Add channel-specific gain, tau, threshold, range, and capture parameters.
5. Add a corrupted-chunk acceptance policy.
6. Move logging and, if necessary, detection away from the hardware-read path.
7. Bound or replace the flat raw plot queue.
8. Add complete raw waveform storage in an efficient binary/chunked format.
9. Implement anode-bump-aware calibrated charge reconstruction.
10. Add uncertainty propagation and physics-quality flags.
11. Add automated tests using stored representative waveforms.
12. Update this document with measured hardware limits and calibration constants.

---

## 17. Developer handoff checklist

Before collecting data:

1. Close WaveForms.
2. Connect cathode output to AD3 Channel 1 and anode output to AD3 Channel 2.
3. Verify both CR-150/CR-110 assemblies, power, USB, common ground, coupling, attenuation, input range, and enclosure closure.
4. Choose non-flat mode for accepted event pairs or flat mode for continuous two-channel diagnostics.
5. Review every script parameter, especially channel polarity, minimum charge, drift time, and drift window.
6. Begin the throughput check at 200 kS/s with waveform saving disabled; use `--no-save` when isolating acquisition performance.
7. Confirm that the one-second noise-learning period completes for both channels.
8. Confirm the observed cathode and anode polarities, amplitudes, and approximate charge scales.
9. Check the independent cathode/anode detection counts and investigate increasing unmatched counts.
10. Require `lost 0` and `corrupted 0` before treating a run as valid.
11. Verify the measured peak-delay distribution before relying on the default `55 +/- 15 us` pairing rule.
12. Enable summary and waveform saving only after a clean throughput test.
13. Preserve `run_config.json` with every saved dataset.
14. Record the physical setup, coupling, grounding, channel assignment, and run conditions with the data.

When modifying code:

1. Keep [`LiveDAQ_2c.py`](../LiveDAQ_2c.py) and [`LiveDAQ_2c_flat.py`](../LiveDAQ_2c_flat.py) consistent for acquisition, detection, pairing, charge calculation, and logging fixes.
2. Keep the one-channel programs available as the simpler detector and performance reference.
3. Run simulation before connecting hardware.
4. Test both channel polarities, pairing-window boundaries, missing-channel behavior, and chunk gaps.
5. Preserve the meaning of Channel 1 as cathode / `Qc` and Channel 2 as anode / `Qa`.
6. Do not add `--canva-size` to non-flat 2c or confuse it with the flat duration in seconds.
7. Do not silently change nominal gain, tau, threshold, charge, or drift-time definitions.
8. Add permanent automated tests before major detector or pairing changes.
9. Update scripts, [`README.md`](../README.md), and this document together when user-facing behavior changes.
10. Record measured stable-rate, calibration, timing, and real-waveform results in this document.
11. Commit code and documentation together.

The present implementation is ready for structured two-channel hardware validation. The next milestones are to demonstrate stable physical operation at 200 kS/s or higher, measure the real cathode-to-anode timing distribution, characterize the anode bump and cross-talk, and replace the shared nominal charge conversion with channel-specific calibration. Until those steps are complete, the displayed `Qc`, `Qa`, and `Qa/Qc` should be treated as engineering monitoring values rather than calibrated production purity results.
