# CR-110 / CR-150 Analog Discovery 3 Data Acquisition System

## Engineering Notes and Developer Handoff

**Project context:** SLAC liquid-argon impurity monitor development

**Hardware:** CR-150-R6 assembly, CR-110 charge-sensitive preamplifier, Digilent Analog Discovery 3 (AD3), and Digilent BNC Adapter

**Software generation:** One input channel, identified by the _1c filename suffix

**Document status:** Updated for the current optimized non-flat and flat programs

**Validation status:** Simulation is working for both GUI variants. Physical AD3 operation at 200 kS/s and above still requires laboratory validation.

## Contents

1. Purpose and scope
2. Hardware and signal assumptions
3. Current software inventory
4. Requirements and device access
5. Recommended commands
6. Live architecture
7. AD3 and DWF acquisition
8. Lost and corrupted samples
9. Noise estimation
10. Pulse detection and charge calculation
11. GUI implementations
12. Command-line parameters
13. Output files
14. Problems encountered and current solutions
15. Validation status
16. Known limitations and future work
17. Troubleshooting
18. Handoff checklist

---

## 1. Purpose and scope

The software replaces a conventional oscilloscope with an AD3 and a computer. It is intended to:

1. Continuously acquire the CR-110 output from AD3 Scope Channel 1.
2. Detect charge pulses without using a function-generator reference channel.
3. Report pulse time, polarity, amplitude, and estimated charge.
4. Display either aligned pulse captures or a continuous raw time interval.
5. Optionally save pulse summaries and accepted pulse waveforms.
6. Support simulation and offline CSV analysis without laboratory hardware.

The current implementation uses one input channel only. Channel 2 was useful during early bench tests to observe the ideal generator signal, but it is deliberately excluded from detection because the final detector system will not provide that reference.

Charge currently uses the nominal CR-110 gain:

    CR-110 nominal gain = 1.4 V/pC
    charge [fC] = amplitude [V] / 1.4 [V/pC] * 1000

This is an engineering estimate, not a complete detector calibration.

---

## 2. Hardware and signal assumptions

### 2.1 Signal path

Normal detector or external-generator operation:

    Detector or external generator
                  |
                  v
           CR-150 / CR-110
                  |
                  v
        AD3 Scope Channel 1
                  |
                  v
             Python DAQ

For an external generator or real detector, omit --wavegen and leave AD3 W1 disabled.

For an all-AD3 bench test, W1 may generate the stimulus while Scope Channel 1 reads the CR-110 output. W1 is enabled only when --wavegen is present.

### 2.2 CR-110 response

The measured decay constant is approximately:

    tau = 140 us

The detector uses tau to size the edge lag, baseline region, peak-search region, refractory interval, and waveform capture. Override it with --tau-us only when justified by measurement.

### 2.3 Coupling, grounding, and shielding

Early measurements contained periodic baseline movement consistent with mains pickup. Closing the metal enclosure substantially improved the signal. Shielding, common ground, cable routing, and BNC Adapter coupling are therefore part of the measurement system.

The Python code does not configure the physical AC/DC coupling selection on the BNC Adapter. Verify the adapter configuration before every hardware run.

### 2.4 Square-wave edge counting

A square wave contains a rising and a falling edge. With --polarity both, one generator cycle may produce a positive and a negative detected event. To observe only one edge type, use --polarity positive or --polarity negative.

---

## 3. Current software inventory

### 3.1 LiveDAQ_1c.py — primary non-flat live DAQ

This is the primary one-channel live program and the recommended starting point for normal operation.

Features:

- AD3 Record-mode acquisition through the Digilent WaveForms SDK.
- Simulated source for development without hardware.
- Channel-1-only pulse detection.
- One-second startup noise-learning period.
- NumPy-vectorized edge calculation and candidate selection.
- Local baseline and peak calculation for each accepted pulse.
- Configurable non-overlapping overlay batches.
- Independent non-overlapping result-table batches.
- Optional W1 square-wave generation.
- Optional CSV summary and pulse-waveform output.
- GUI and headless operation.

The canvas batch is controlled by --canva-size with a positive integer. For example, --canva-size 5 waits for five new pulses and then overlays those five captures. The next canvas update uses the next five pulses; it is not a sliding window.

The result table is controlled independently by --num-test and defaults to ten rows.

### 3.2 LiveDAQ_1c_flat.py — continuous-time diagnostic DAQ

This program uses the same current vectorized detector core but displays consecutive raw Channel 1 time intervals.

Features specific to flat mode:

- --canva-size is a positive floating-point duration in seconds.
- --canva-size 0.500 displays a 0.5-second interval and updates every 0.5 seconds.
- Time intervals are consecutive and non-overlapping.
- Each completed interval retains at most 50,000 samples for plotting, independent of the acquisition rate.
- At lower rates, all samples are retained when the interval contains fewer than 50,000 samples.
- Before calling Tk Canvas, a vectorized min/max envelope reduces the retained samples to approximately two display points per horizontal pixel.
- The min/max envelope preserves narrow positive and negative excursions better than simple every-Nth-point drawing.
- The result table still updates independently in complete batches of --num-test pulses.
- Plot data is not published in headless mode.

Examples at the default 0.5-second canvas duration:

| Sampling rate | Raw samples per canvas | Retained plot samples |
|---:|---:|---:|
| 50 kS/s | 25,000 | 25,000 |
| 200 kS/s | 100,000 | 50,000 |
| 500 kS/s | 250,000 | 50,000 |

If DWF reports lost/corrupted data or the sample indices are discontinuous, an incomplete flat interval is discarded. The next canvas begins after the gap instead of drawing a false continuous line across missing data.

### 3.3 DAQ_1c.py — offline WaveForms CSV analysis

This program reads CSV files exported by Digilent WaveForms. It:

- Locates Time (s) and Channel 1 (V) columns.
- Detects pulses using Channel 1 only.
- Estimates baseline, amplitude, polarity, and nominal charge.
- Prints a pulse table.
- Can save charge-pulse summaries for each input file.

The offline algorithm predates the current live vectorized detector and is not numerically identical to it.

### 3.4 Shell scripts

Files under DAQ_system/scripts are editable launch examples, not permanent experiment configuration.

- run_monitor.sh runs LiveDAQ_1c.py with an external stimulus or detector and keeps W1 disabled.
- run_wavegen.sh runs LiveDAQ_1c.py and enables W1.
- run_simulate.sh contains the currently selected simulation configuration. Inspect it before use because it may be changed frequently while testing GUI behavior.

---

## 4. Requirements and device access

### 4.1 Software

Required for all live modes:

- Python 3
- NumPy
- Tkinter for GUI mode

Install NumPy if needed:

    python -m pip install numpy

Hardware mode additionally requires:

- Digilent WaveForms
- WaveForms SDK / DWF shared library
- Analog Discovery 3 connected by USB

Common Windows DWF locations include:

    C:\Program Files\Digilent\WaveForms3\dwf.dll
    C:\Program Files (x86)\Digilent\WaveForms3\dwf.dll

Use --dwf-library for a nonstandard location.

### 4.2 Exclusive ownership

Close the WaveForms application before starting Python hardware mode. WaveForms and the Python process cannot reliably own the same AD3 simultaneously.

If WaveForms Device Manager shows only DEMO devices, close WaveForms, connect the AD3, and reopen WaveForms. This was required during initial setup when the application was opened before the hardware was attached.

### 4.3 Working directory

The shell scripts change into their own directory before launching Python, so they can be run from another location. When entering commands manually, run them from DAQ_system/scripts or adjust the relative path.

---

## 5. Recommended commands

### 5.1 Non-flat simulation

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
      --no-save

Here --canva-size 2 means two pulse captures per canvas update. It does not mean two seconds in non-flat mode.

### 5.2 Flat simulation

    python ../LiveDAQ_1c_flat.py \
      --simulate \
      --sample-rate 500000 \
      --simulation-pulse-rate 10 \
      --simulation-charge-fc 100 \
      --simulation-polarity positive \
      --min-charge-fc 50 \
      --polarity positive \
      --canva-size 0.500 \
      --num-test 10 \
      --no-save

Here --canva-size 0.500 means one 0.5-second raw waveform interval per canvas update.

Simulation validates software behavior. It does not validate USB throughput or establish a safe physical AD3 sampling rate.

### 5.3 Hardware monitor using an external source

    python ../LiveDAQ_1c.py \
      --sample-rate 200000 \
      --min-charge-fc 40 \
      --polarity positive \
      --canva-size 1 \
      --no-save

Begin with --no-save when testing a new sampling rate. Only enable output after lost and corrupted remain zero.

### 5.4 Flat hardware monitor

    python ../LiveDAQ_1c_flat.py \
      --sample-rate 200000 \
      --min-charge-fc 40 \
      --polarity positive \
      --canva-size 0.500 \
      --num-test 10 \
      --no-save

Flat mode is valuable for observing the background between pulses, but it adds GUI work. Validate it independently at each requested sampling rate.

### 5.5 AD3 W1 bench stimulus

    python ../LiveDAQ_1c.py \
      --wavegen \
      --wavegen-frequency 10 \
      --wavegen-vpp 0.100 \
      --wavegen-offset 0 \
      --min-charge-fc 40 \
      --canva-size 1

The W1 Vpp argument is peak-to-peak voltage. DWF receives half this value as peak amplitude.

### 5.6 Headless mode

Add --headless to disable Tkinter. Detected pulses are printed to the terminal. Flat mode also stops publishing raw plot chunks, making headless mode useful for separating acquisition/detection performance from GUI performance.

### 5.7 Offline CSV processing

    python ../DAQ_1c.py ../results/csv \
      --min-charge-fc 40 \
      --polarity positive \
      --output-dir ../results/offline_charge

---

## 6. Live architecture

### 6.1 Common acquisition path

    AD3 DWF Record mode or SimulatedSource
                      |
                      v
              NumPy DataChunk
                      |
                      v
         StreamingPulseDetector
                      |
                      v
                  LivePulse
                 /         \
                v           v
          PulseLogger    GUI event queue

The acquisition worker currently performs the DWF read, pulse detection, charge calculation, and CSV logging sequentially. Tkinter runs on the main thread.

### 6.2 Additional flat plot path

Flat mode also publishes each NumPy DataChunk by reference:

    acquisition worker
            |
            v
       plot chunk queue
            |
            v
    Tkinter main thread
            |
            v
    fixed-duration accumulator
            |
            v
    <= 50,000 retained samples
            |
            v
    per-pixel min/max envelope
            |
            v
        Tk Canvas

Publishing the existing NumPy array reference avoids another full acquisition-buffer copy. Concatenation, plot decimation, coordinate generation, and Tk drawing occur outside the acquisition worker.

The plot queue is not used in headless mode.

---

## 7. AD3 and DWF acquisition

DWF means Digilent WaveForms SDK. It is the native library used to configure the AD3 and retrieve samples.

Hardware startup:

1. Open the selected device.
2. Disable automatic reconfiguration.
3. Disable unused analog-input channels.
4. Enable Scope Channel 1.
5. Set input range and zero offset.
6. Select the DWF average input filter.
7. Select infinite Record acquisition mode.
8. Request the sample rate.
9. Optionally configure W1.
10. Apply the staged settings.
11. Read back the actual configured sample rate.
12. Wait two seconds for input offset stabilization.
13. Start continuous acquisition.

An earlier bug read the sample rate before applying staged settings, which returned the approximately 100 MHz system clock instead of the requested Record rate. The current order reads the rate only after configuration is applied.

For each Record read, DWF reports:

- available sample count
- lost sample count
- corrupted sample count

The C double buffer is converted directly to NumPy:

    np.ctypeslib.as_array(data).copy()

This avoids creating one Python float object per sample.

---

## 8. Lost and corrupted samples

### Lost

The device produced samples faster than the computer retrieved them. Older data was overwritten before Python received it.

    Lost data is missing and cannot be recovered.

### Corrupted

DWF returned data from a buffer region that may have changed during the read.

    Corrupted data exists, but its values may not be trustworthy.

Either can hide pulses, create false pulses, alter peak charge, and damage timing. The GUI counters are cumulative for the current process.

Production-quality runs should remain:

    lost 0 | corrupted 0

Restart after changing performance settings so the counters begin from zero.

---

## 9. Noise estimation

### 9.1 Startup learning

The GUI initially reports Learning noise (1 s warm-up). Detection is disabled until a valid background estimate is available.

### 9.2 Fixed statistical workload

The detector uniformly collects approximately 5,000 background edge samples per one-second interval, regardless of acquisition rate.

Examples:

- 200 kS/s: approximately every 40th edge
- 500 kS/s: approximately every 100th edge

This keeps the statistics workload nearly constant as the sample rate increases.

### 9.3 Pulse exclusion

Samples near an active or completed pulse capture are excluded from the background estimate. Large rejected candidates are also excluded so transient activity does not inflate normal background noise.

### 9.4 Median and MAD

The detector uses NumPy median and median absolute deviation:

    center = median(background)
    MAD = median(abs(background - center))
    edge sigma = 1.4826 * MAD
    voltage noise sigma = edge sigma / sqrt(2)

At least 1,000 valid background samples are required for an update.

### 9.5 Threshold

The trigger threshold is the larger of:

    min-charge-fc converted to voltage

and:

    threshold-sigma * measured noise sigma * sqrt(2)

Recent bench configurations used approximately 40–50 fC to reject unwanted transients. This is setup-dependent and not a calibrated universal threshold.

---

## 10. Pulse detection and charge calculation

The current detector is chunk-vectorized.

### 10.1 Edge detection

A sharp CR-110 onset is identified by:

    edge[n] = voltage[n] - voltage[n - edge_lag]

Edge lag is derived from tau. NumPy calculates an entire chunk at once, including edges that cross a chunk boundary.

### 10.2 Candidate selection

NumPy selects all indices where absolute edge exceeds the current threshold. Python examines only these candidates instead of running a Python state-machine step for every acquired sample.

### 10.3 Baseline and capture

For each viable candidate:

- Baseline is the median of approximately one tau before the onset.
- A short guard separates the baseline from the onset.
- Default pretrigger capture is 0.5 ms.
- Default posttrigger capture is 1.5 ms.
- Peak search extends through approximately 0.35 tau after onset.
- The peak is the sample with maximum absolute difference from baseline.
- Capture can continue across DWF chunk boundaries.

### 10.4 Acceptance

A candidate is accepted if:

1. Absolute amplitude exceeds the threshold.
2. Sign matches --polarity unless polarity is both.
3. It is outside the prior candidate's refractory/capture-blocking interval.

### 10.5 Time and charge

Peak time uses the global sample index and actual DWF rate. The GUI shows a shortened time-of-day field, while saved output retains the full timestamp.

Charge is:

    signed charge [fC] = amplitude [V] / gain [V/pC] * 1000
    absolute charge [fC] = abs(signed charge)

---

## 11. GUI implementations

### 11.1 Non-flat overlay mode

LiveDAQ_1c.py:

- Polls GUI queues at --gui-rate, default 10 Hz.
- Accumulates --canva-size pulses for the canvas.
- Draws that complete batch aligned to peak time zero.
- Keeps the previous canvas until another complete batch is available.
- Uses non-overlapping canvas batches.
- Independently accumulates --num-test results for the text panel.
- Defaults to one pulse per canvas and ten rows per result update.

The --canva-size type is integer.

### 11.2 Flat continuous mode

LiveDAQ_1c_flat.py:

- Uses --canva-size as seconds.
- Collects exactly that many acquired seconds per plot interval.
- Uses consecutive, non-overlapping intervals rather than a sliding history.
- Updates the entire canvas when a complete interval is available.
- Retains no more than 50,000 plot samples.
- Reduces Canvas drawing coordinates with a min/max envelope.
- Independently replaces the result panel after --num-test pulses.
- Defaults to 0.5 seconds per canvas and ten result rows.

The --canva-size type is floating point.

For compatibility, flat mode still accepts --gui-rate, but actual flat update timing is determined by --canva-size.

### 11.3 Why retained samples and Canvas points differ

The flat plot may retain 50,000 samples for waveform representation, but sending 50,000 points directly to Tk created approximately 100,000 coordinate arguments and caused severe GUI slowdown.

The current renderer keeps the retained data but sends roughly two extrema per horizontal pixel to Tk. A 1,000-pixel plot therefore draws about 2,000 points while preserving each pixel bucket's minimum and maximum.

---

## 12. Command-line parameters

| Argument | Default | Meaning |
|---|---:|---|
| --sample-rate | 500,000 S/s | Requested AD3 Record rate. Always check the returned actual rate and integrity counters. |
| --input-range | 1.0 V | Scope Channel 1 input range. Increase if clipping occurs. |
| --gain-v-per-pc | 1.4 | Nominal CR-110 conversion gain. |
| --tau-us | 140 us | Decay constant used to size detector windows. |
| --threshold-sigma | 8 | Adaptive edge threshold in robust noise sigma. |
| --min-charge-fc | 1 fC | Absolute minimum threshold. Bench scripts normally use a higher value. |
| --polarity | both | Accepted sign: both, positive, or negative. |
| --pretrigger-ms | 0.5 ms | Waveform retained before onset. |
| --posttrigger-ms | 1.5 ms | Waveform retained after onset. |
| --canva-size | File dependent | Non-flat: integer pulse count. Flat: floating-point seconds. |
| --num-test | 10 | Number of result rows replaced as one non-overlapping batch. |
| --gui-rate | 10 Hz non-flat | GUI polling rate in non-flat mode. Flat timing comes from --canva-size. |
| --no-save | Off | Disable output files; recommended for throughput tests. |
| --save-waveforms | Off | Save accepted per-pulse waveform windows in addition to summaries. |
| --output-root | results/live | Parent of timestamped run directories. |
| --wavegen | Off | Enable W1 square-wave output. |
| --wavegen-frequency | 500 Hz | W1 frequency. |
| --wavegen-vpp | 0.100 V | W1 peak-to-peak amplitude. |
| --wavegen-offset | 0 V | W1 DC offset. |
| --device-index | -1 | Automatic/first-device selection. |
| --dwf-library | automatic | Explicit DWF library path if discovery fails. |
| --simulate | Off | Use the synthetic source instead of hardware. |
| --headless | Off | Disable Tkinter and print pulses to the terminal. |
| --duration | 0 | Wall-clock run duration; zero means run until stopped. |

The historical --canva-pulse and --canvas-pulse names are no longer accepted. Use --canva-size.

---

## 13. Output files

Unless --no-save is present, each run creates a timestamped directory under:

    DAQ_system/results/live/

### run_config.json

Records the effective acquisition, detector, GUI, output, and W1 settings. In flat mode, gui_rate_hz is derived from the selected canva_size.

### pulses.csv

One row per accepted pulse:

- pulse number
- full peak timestamp
- elapsed time
- global peak sample
- polarity
- peak voltage
- baseline
- baseline-corrected amplitude
- signed charge
- absolute charge

### pulse_waveforms.csv

Created only with --save-waveforms. It stores decimated accepted pulse captures, limited to approximately 500 saved points per pulse.

The software does not save the complete uninterrupted raw stream, including in flat mode.

---

## 14. Problems encountered and current solutions

### 14.1 WaveForms displayed only DEMO

Cause: WaveForms was opened before the AD3 was connected.

Solution: Close WaveForms, connect AD3, reopen WaveForms.

### 14.2 Python could not open AD3

Likely causes include WaveForms owning the device, missing USB connection, missing DWF library, or wrong device selection.

Solution: Close WaveForms, verify USB and SDK installation, then use --device-index or --dwf-library if necessary.

### 14.3 Periodic baseline movement

Evidence showed substantial improvement after closing the metal enclosure, consistent with mains pickup.

Solution: Use the enclosure lid, verify shielding and common ground, shorten sensitive wiring, and verify coupling. Do not rely on software thresholds to repair poor shielding.

### 14.4 Noise was detected as charge

Cause: Minimum charge threshold was too permissive for the bench transients.

Solution: Combine --min-charge-fc with the adaptive MAD threshold. Increase the minimum gradually while confirming that real pulses remain detectable.

### 14.5 Visible pulses were not detected

The current minimum charge parameter affects both edge triggering and final amplitude acceptance. At lower rates, a physical edge can be split between samples and fail the edge threshold even when the final peak would represent sufficient charge.

Current workaround: lower --min-charge-fc enough to trigger reliably while remaining above background.

Future correction: separate candidate edge threshold from final charge threshold.

### 14.6 Lost/corrupted increased at high rates

Pulse frequency and sample rate are independent. A 1 Hz input still creates 500,000 samples per second at 500 kS/s.

Implemented performance improvements:

- direct DWF-to-NumPy conversion
- chunk-vectorized edge calculation
- vectorized candidate selection
- one noise update per second
- approximately 5,000 noise samples per update
- pulse-region exclusion from noise
- GUI queue separate from acquisition
- no flat plot publishing in headless mode
- flat fixed-duration non-sliding windows
- at most 50,000 retained flat plot samples
- vectorized per-pixel min/max Canvas envelope

### 14.7 Flat simulation froze the computer

Cause: the first optimized flat renderer retained 50,000 samples and sent all of them directly to Tk Canvas, producing approximately 100,000 coordinate arguments every update.

Solution: retain up to 50,000 samples internally, then use a NumPy min/max envelope to draw approximately two points per horizontal pixel. This preserves extrema while greatly reducing Tk work.

The temporary _test_flat_windows.py file used during diagnosis was deleted after testing and is not part of the system.

### 14.8 Shell option reported as a command

Cause: a missing line-continuation backslash, or characters after the backslash.

Solution: ensure every continued shell line ends with backslash as its final character.

### 14.9 Lower sampling rate improved stability but reduced resolution

For tau = 140 us:

| Rate | Sample interval | Samples per tau |
|---:|---:|---:|
| 50 kS/s | 20 us | 7 |
| 100 kS/s | 10 us | 14 |
| 200 kS/s | 5 us | 28 |
| 500 kS/s | 2 us | 70 |

At 50 kS/s, sampling phase can noticeably reduce the measured peak. Use low rates for functional diagnosis, but target at least 200 kS/s when lost/corrupted remain zero.

---

## 15. Validation status

### Confirmed hardware observations

- Oscilloscope measurement reproduced tau near 140 us.
- AD3 acquired the CR-110 output successfully.
- Closing the metal enclosure reduced periodic pickup.
- An earlier non-flat version reached approximately 120 kS/s without data warnings when the computer was otherwise lightly loaded.
- Earlier flat implementations produced more throughput problems.

### Current software validation

The optimized detector has been exercised in simulation and synthetic checks at rates including 50, 200, and 500 kS/s.

Checks performed during development include:

- positive and negative simulated pulses
- pulses crossing chunk boundaries
- irregular chunk sizes
- global sample timing
- one-second noise learning
- MAD noise updates
- non-overlapping pulse batches
- configurable non-flat canvas batch size
- configurable flat canvas duration
- 50,000-sample flat retention cap
- lost/discontinuity reset for flat intervals
- min/max envelope preservation of positive and negative extrema
- ten-row result batching

Current simulation runs for both GUI variants complete without an identified functional problem.

### Required physical validation

The current optimized files have not yet demonstrated their maximum stable rate on the laboratory AD3.

Recommended sequence for each GUI:

1. 200 kS/s with --no-save.
2. 300 kS/s with --no-save.
3. 400 kS/s with --no-save.
4. 500 kS/s with --no-save.
5. Repeat the highest clean rate with summary saving.
6. Repeat with --save-waveforms if waveform output is required.

Run long enough to observe many pulses. Require continuous lost 0 and corrupted 0.

Test non-flat and flat separately; a clean result in one does not prove the other GUI has the same performance margin.

---

## 16. Known limitations and future work

### 16.1 No detector-specific calibration

The 1.4 V/pC conversion is nominal. A final calibration must include the real injection capacitance, cables, coupling, electronics, and AD3 configuration.

### 16.2 Coupled trigger and acceptance threshold

One min-charge-fc value currently controls candidate edge triggering and final charge acceptance. Separate these thresholds in a future detector revision.

### 16.3 One acquisition/detector/logger worker

Current order:

    read AD3 -> detect -> calculate noise when due -> log -> read AD3

If 200 kS/s cannot remain clean, the next major optimization should be:

    AD3 reader thread
            |
            v
    bounded NumPy chunk queue
            |
            v
      detector worker
          /     \
         v       v
    GUI queue  logger queue

This can remain real-time with tens of milliseconds of latency. The software queue must be bounded and expose its own overflow counter.

### 16.4 Logging can pause acquisition

PulseLogger writes in the acquisition worker. Summary output around 10 Hz is small, but --save-waveforms may write hundreds of rows per pulse. A separate writer worker is recommended if saving causes data warnings.

### 16.5 Flat plot queue is not bounded

Flat GUI chunks currently use a thread-safe simple queue. At normal real-time operation, the GUI is expected to drain it every canvas interval. If Tk or the system stalls for a long time, queued plot chunks can consume increasing memory.

A future bounded plot queue should drop incomplete visualization data without blocking acquisition and should display a separate plot-drop warning.

### 16.6 Corrupted-data policy

Corrupted counts are shown, but the pulse detector may still process returned values. A stricter production policy should reject corrupted chunks and mark an explicit time gap.

### 16.7 Peak-sample estimator

Charge uses the largest baseline-corrected sample and is sensitive to sampling phase. Possible improvements:

- exponential fit using measured tau
- template or matched-filter amplitude
- peak interpolation
- sampling-rate-specific calibration

### 16.8 Complete raw recording is absent

Only accepted pulse windows are optionally saved. Possible diagnostic additions:

- short raw captures
- rolling raw buffer saved on warnings
- binary NumPy or HDF5 storage rather than text CSV

### 16.9 Permanent automated tests are absent

Temporary regression scripts were removed after use. A permanent test suite should cover:

- positive and negative pulses
- pulse position at every chunk boundary
- noise-only false-positive rate
- closely spaced pulses
- lost and corrupted chunks
- threshold behavior near acceptance
- CSV schemas
- equivalence of detector core between non-flat and flat files
- flat interval length and 50,000-sample cap

### 16.10 Two-channel acquisition is not implemented

The _1c suffix reserves these validated programs as the one-channel generation. Future two-channel work should use new filenames and should preserve the one-channel files for comparison and rollback.

---

## 17. Troubleshooting

### Stuck at Learning noise

- Wait for at least one second of acquired sample time.
- Confirm elapsed time is increasing.
- Confirm NumPy is installed.
- Restart after a DWF error.

### Stuck at Waiting for a charge pulse

- Verify CR-110 output is connected to Scope Channel 1.
- Verify common ground and BNC Adapter configuration.
- Check --polarity.
- Lower --min-charge-fc carefully.
- Confirm the signal is not clipped.
- Use WaveForms briefly to inspect hardware, then close it before restarting Python.

### Too many detected events

- Determine whether both square-wave edges are counted.
- Select one polarity.
- Increase --min-charge-fc gradually.
- Correct shielding and mains pickup before relying on a higher threshold.

### Pulse visible in flat canvas but not marked

- Verify polarity.
- Lower --min-charge-fc temporarily.
- Increase sampling rate only if integrity remains clean.
- Remember that edge triggering and final charge acceptance currently share one threshold.

### DATA WARNING appears

1. Stop the run and do not treat it as production-quality.
2. Restart with --no-save.
3. Use non-flat LiveDAQ_1c.py first.
4. Close unnecessary applications.
5. Reduce sample rate.
6. Check USB.
7. If the optimized version still fails, separate the reader, detector, and logger workers.

### GUI becomes unresponsive

- Confirm the current flat renderer includes min_max_plot_envelope.
- Stop stale Python processes before starting another run.
- Use --headless to isolate GUI cost.
- Increase flat --canva-size cautiously; longer windows increase raw samples before the 50,000-point cap.
- Use non-flat mode when continuous background display is unnecessary.

### Argument error after updating code

- Replace old --canva-pulse or --canvas-pulse with --canva-size.
- Non-flat requires an integer.
- Flat accepts seconds as a floating-point value.
- Check shell continuation backslashes.

---

## 18. Handoff checklist

Before collecting data:

1. Close WaveForms.
2. Connect AD3 and verify hardware power and USB.
3. Close the metal enclosure.
4. Verify Channel 1, coupling, range, and common ground.
5. Choose non-flat or flat based on the diagnostic need.
6. Review every script parameter.
7. Start at 200 kS/s with --no-save.
8. Confirm the one-second noise-learning period completes.
9. Confirm expected polarity and charge scale.
10. Require lost 0 and corrupted 0.
11. Enable saving only after a clean throughput test.
12. Preserve run_config.json with saved data.

When modifying code:

1. Keep _1c programs available as the one-channel reference.
2. Apply detector fixes consistently to non-flat and flat versions.
3. Test simulation before hardware.
4. Test positive and negative signals.
5. Test chunk-boundary behavior.
6. Do not confuse --canva-size semantics between GUI variants.
7. Do not silently change nominal gain or threshold meaning.
8. Add permanent automated tests before large algorithm changes.
9. Record physical stable-rate results in this document.
10. Commit code and documentation together.

The immediate next milestone is laboratory validation of both optimized GUI variants at 200 kS/s and above. If either variant produces lost or corrupted samples, first isolate GUI and logging cost with --headless and --no-save, then consider separating acquisition, detection, and logging into independent workers.
