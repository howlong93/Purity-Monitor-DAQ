#!/usr/bin/env python3
"""Live charge-pulse monitor for an Analog Discovery 3 and CR-110.

The hardware mode owns the AD3 through the Digilent WaveForms SDK, streams
Scope Channel 1 continuously in Record mode, detects pulses without Channel 2,
and displays the newest event in a small Tk GUI while result rows update in batches. The default charge
conversion is the uncalibrated CR-110 nominal gain of 1.4 V/pC.

Close the WaveForms application before starting hardware mode: only one process
can own a Digilent device at a time.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import math
import os
import queue
import random
import statistics
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence


DEFAULT_SAMPLE_RATE_HZ = 500_000.0
DEFAULT_INPUT_RANGE_V = 1.0
DEFAULT_GAIN_V_PER_PC = 1.4
DEFAULT_TAU_US = 140.0
DEFAULT_GUI_RATE_HZ = 10.0

# Number of result rows replaced together as one non-overlapping batch.
# The canvas itself always shows only the newest detected pulse.
NUM_TEST = 10

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = SCRIPT_DIRECTORY / "AD3 results" / "live"

# Relevant WaveForms SDK constants. These values match the official
# dwfconstants.py installed with WaveForms SDK (revision 2024-07-24).
DWF_PARAM_ON_CLOSE = 4
DWF_STATE_CONFIG = 4
DWF_STATE_PREFILL = 5
DWF_STATE_ARMED = 1
ACQMODE_RECORD = 3
FILTER_AVERAGE = 1
ANALOG_OUT_NODE_CARRIER = 0
FUNC_SQUARE = 2


@dataclass(frozen=True)
class LiveConfig:
    sample_rate_hz: float
    input_range_v: float
    gain_v_per_pc: float
    tau_us: float
    threshold_sigma: float
    min_charge_fc: float
    polarity: str
    pretrigger_ms: float
    posttrigger_ms: float
    gui_rate_hz: float
    num_test: int
    wavegen_enabled: bool
    wavegen_frequency_hz: float
    wavegen_vpp: float
    wavegen_offset_v: float


@dataclass(frozen=True)
class DataChunk:
    start_sample: int
    voltages: list[float]
    lost_samples: int = 0
    corrupted_samples: int = 0


@dataclass(frozen=True)
class LivePulse:
    number: int
    peak_sample: int
    peak_elapsed_s: float
    peak_timestamp: str
    polarity: str
    peak_voltage_v: float
    baseline_v: float
    amplitude_v: float
    signed_charge_fc: float
    absolute_charge_fc: float
    waveform_time_s: tuple[float, ...]
    waveform_voltage_v: tuple[float, ...]


@dataclass
class ActiveCapture:
    onset_sample: int
    baseline_v: float
    peak_search_end: int
    capture_end: int
    peak_sample: int
    peak_voltage_v: float
    waveform_samples: list[int] = field(default_factory=list)
    waveform_voltages: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class MonitorSnapshot:
    status: str
    source_name: str
    sample_rate_hz: float
    elapsed_s: float
    event_count: int
    lost_samples: int
    corrupted_samples: int
    noise_sigma_v: float
    output_directory: str
    error: str


class SharedMonitorState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = "Starting"
        self._source_name = ""
        self._sample_rate_hz = 0.0
        self._elapsed_s = 0.0
        self._event_count = 0
        self._lost_samples = 0
        self._corrupted_samples = 0
        self._noise_sigma_v = 0.0
        self._output_directory = ""
        self._error = ""
        self.events: queue.Queue[LivePulse] = queue.Queue()

    def update(self, **values: object) -> None:
        with self._lock:
            for name, value in values.items():
                attribute = f"_{name}"
                if not hasattr(self, attribute):
                    raise AttributeError(f"Unknown monitor state field: {name}")
                setattr(self, attribute, value)

    def snapshot(self) -> MonitorSnapshot:
        with self._lock:
            return MonitorSnapshot(
                status=self._status,
                source_name=self._source_name,
                sample_rate_hz=self._sample_rate_hz,
                elapsed_s=self._elapsed_s,
                event_count=self._event_count,
                lost_samples=self._lost_samples,
                corrupted_samples=self._corrupted_samples,
                noise_sigma_v=self._noise_sigma_v,
                output_directory=self._output_directory,
                error=self._error,
            )


def robust_sigma(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    center = statistics.median(values)
    mad = statistics.median(abs(value - center) for value in values)
    return 1.4826 * mad


class StreamingPulseDetector:
    """Stateful Channel-1-only detector suitable for chunked live data."""

    def __init__(
        self,
        config: LiveConfig,
        sample_rate_hz: float,
        run_start: datetime,
    ) -> None:
        self.config = config
        self.sample_rate_hz = sample_rate_hz
        self.dt_s = 1.0 / sample_rate_hz
        self.run_start = run_start
        self.tau_samples = max(1, round(config.tau_us * 1e-6 * sample_rate_hz))
        self.edge_lag = max(
            1,
            round(max(5e-6, 0.035 * config.tau_us * 1e-6) * sample_rate_hz),
        )
        self.baseline_guard = max(self.edge_lag * 2, round(0.04 * self.tau_samples))
        self.baseline_samples = max(5, self.tau_samples)
        self.pretrigger_samples = max(1, round(config.pretrigger_ms * 1e-3 * sample_rate_hz))
        self.posttrigger_samples = max(1, round(config.posttrigger_ms * 1e-3 * sample_rate_hz))
        self.peak_lookahead = max(self.edge_lag, round(0.35 * self.tau_samples))
        self.refractory_samples = max(1, round(0.75 * self.tau_samples))
        history_size = (
            self.pretrigger_samples
            + self.baseline_samples
            + self.baseline_guard
            + self.edge_lag
            + 16
        )
        self.history: deque[tuple[int, float]] = deque(maxlen=history_size)
        self.noise_edges: deque[float] = deque(maxlen=50_000)
        self.noise_stride = max(1, round(sample_rate_hz / 50_000.0))
        self.noise_update_samples = max(1, round(0.02 * sample_rate_hz))
        self.noise_sigma_v = 0.0
        self.noise_ready = False
        self._last_noise_update = 0
        self._last_sample: int | None = None
        self._last_candidate = -10**18
        self._active: ActiveCapture | None = None
        self._pulse_count = 0

    def reset_after_gap(self) -> None:
        self.history.clear()
        self._active = None
        self._last_sample = None

    def _update_noise(self, sample_index: int) -> None:
        if (
            sample_index - self._last_noise_update >= self.noise_update_samples
            and len(self.noise_edges) >= 100
        ):
            edge_sigma = robust_sigma(list(self.noise_edges))
            self.noise_sigma_v = edge_sigma / math.sqrt(2.0)
            self.noise_ready = self.noise_sigma_v > 0.0
            self._last_noise_update = sample_index

    def _threshold_v(self) -> float:
        # Do not trigger during the short startup interval used to learn the
        # actual Channel 1 noise. This prevents a random startup excursion from
        # opening a long event capture and hiding the first physical pulse.
        if not self.noise_ready:
            return math.inf
        minimum = self.config.min_charge_fc * 1e-3 * self.config.gain_v_per_pc
        adaptive = self.config.threshold_sigma * self.noise_sigma_v * math.sqrt(2.0)
        return max(minimum, adaptive)

    def _create_capture(self, sample_index: int, voltage_v: float) -> None:
        baseline_end = sample_index - self.baseline_guard
        baseline_start = baseline_end - self.baseline_samples
        baseline_values = [
            value
            for index, value in self.history
            if baseline_start <= index < baseline_end
        ]
        if len(baseline_values) < 5:
            return
        baseline_v = statistics.median(baseline_values)
        waveform_start = sample_index - self.pretrigger_samples
        waveform = [
            (index, value)
            for index, value in self.history
            if index >= waveform_start
        ]
        waveform.append((sample_index, voltage_v))
        peak_candidates = [
            item for item in waveform if item[0] >= sample_index - self.edge_lag
        ]
        peak_sample, peak_voltage = max(
            peak_candidates,
            key=lambda item: abs(item[1] - baseline_v),
        )
        self._active = ActiveCapture(
            onset_sample=sample_index,
            baseline_v=baseline_v,
            peak_search_end=sample_index + self.peak_lookahead,
            capture_end=sample_index + self.posttrigger_samples,
            peak_sample=peak_sample,
            peak_voltage_v=peak_voltage,
            waveform_samples=[item[0] for item in waveform],
            waveform_voltages=[item[1] for item in waveform],
        )
        self._last_candidate = sample_index

    def _advance_capture(
        self,
        sample_index: int,
        voltage_v: float,
    ) -> LivePulse | None:
        active = self._active
        if active is None:
            return None
        active.waveform_samples.append(sample_index)
        active.waveform_voltages.append(voltage_v)
        if (
            sample_index <= active.peak_search_end
            and abs(voltage_v - active.baseline_v)
            > abs(active.peak_voltage_v - active.baseline_v)
        ):
            active.peak_sample = sample_index
            active.peak_voltage_v = voltage_v
        if sample_index < active.capture_end:
            return None

        self._active = None
        amplitude_v = active.peak_voltage_v - active.baseline_v
        if abs(amplitude_v) < self._threshold_v():
            return None
        if self.config.polarity == "positive" and amplitude_v <= 0.0:
            return None
        if self.config.polarity == "negative" and amplitude_v >= 0.0:
            return None

        self._pulse_count += 1
        elapsed_s = active.peak_sample / self.sample_rate_hz
        timestamp = self.run_start + timedelta(seconds=elapsed_s)
        charge_fc = amplitude_v / self.config.gain_v_per_pc * 1000.0
        waveform_time = tuple(
            (index - active.peak_sample) / self.sample_rate_hz
            for index in active.waveform_samples
        )
        return LivePulse(
            number=self._pulse_count,
            peak_sample=active.peak_sample,
            peak_elapsed_s=elapsed_s,
            peak_timestamp=timestamp.isoformat(timespec="milliseconds"),
            polarity="positive" if amplitude_v > 0.0 else "negative",
            peak_voltage_v=active.peak_voltage_v,
            baseline_v=active.baseline_v,
            amplitude_v=amplitude_v,
            signed_charge_fc=charge_fc,
            absolute_charge_fc=abs(charge_fc),
            waveform_time_s=waveform_time,
            waveform_voltage_v=tuple(active.waveform_voltages),
        )

    def process_chunk(self, chunk: DataChunk) -> list[LivePulse]:
        pulses: list[LivePulse] = []
        if chunk.lost_samples:
            self.reset_after_gap()
        if self._last_sample is not None and chunk.start_sample != self._last_sample + 1:
            self.reset_after_gap()

        for offset, voltage_v in enumerate(chunk.voltages):
            sample_index = chunk.start_sample + offset
            edge: float | None = None
            if len(self.history) >= self.edge_lag:
                lagged_voltage = self.history[-self.edge_lag][1]
                edge = voltage_v - lagged_voltage
                if sample_index % self.noise_stride == 0:
                    self.noise_edges.append(edge)
                self._update_noise(sample_index)

            if self._active is not None:
                pulse = self._advance_capture(sample_index, voltage_v)
                if pulse is not None:
                    pulses.append(pulse)
            elif (
                edge is not None
                and abs(edge) >= self._threshold_v()
                and sample_index - self._last_candidate >= self.refractory_samples
            ):
                self._create_capture(sample_index, voltage_v)

            self.history.append((sample_index, voltage_v))
            self._last_sample = sample_index
        return pulses


class DwfError(RuntimeError):
    pass


class DwfAD3Source:
    def __init__(self, config: LiveConfig, device_index: int, dll_path: str | None) -> None:
        self.config = config
        self.device_index = device_index
        self.dll_path = dll_path
        self.dwf: ctypes.CDLL | None = None
        self.handle = ctypes.c_int(0)
        self.sample_rate_hz = config.sample_rate_hz
        self.sample_cursor = 0
        self.name = "Analog Discovery 3"
        self.version = ""
        self._started = False

    @staticmethod
    def _default_library_candidates() -> list[str]:
        if sys.platform.startswith("win"):
            return [
                r"C:\Program Files\Digilent\WaveForms3\dwf.dll",
                r"C:\Program Files (x86)\Digilent\WaveForms3\dwf.dll",
                "dwf.dll",
            ]
        if sys.platform.startswith("darwin"):
            return ["/Library/Frameworks/dwf.framework/dwf"]
        return ["libdwf.so"]

    def _load_library(self) -> ctypes.CDLL:
        candidates = [self.dll_path] if self.dll_path else self._default_library_candidates()
        errors: list[str] = []
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return ctypes.CDLL(candidate)
            except OSError as error:
                errors.append(f"{candidate}: {error}")
        raise DwfError(
            "Could not load the WaveForms SDK library. Install WaveForms with "
            "the SDK, or pass --dwf-library. Tried: " + " | ".join(errors)
        )

    def _error_message(self) -> str:
        if self.dwf is None:
            return "WaveForms SDK is not loaded"
        message = ctypes.create_string_buffer(512)
        self.dwf.FDwfGetLastErrorMsg(message)
        return message.value.decode(errors="replace")

    def _check(self, result: int, operation: str) -> None:
        if not result:
            raise DwfError(f"{operation} failed: {self._error_message()}")

    def start(self) -> None:
        self.dwf = self._load_library()
        version = ctypes.create_string_buffer(32)
        self._check(self.dwf.FDwfGetVersion(version), "Read DWF version")
        self.version = version.value.decode(errors="replace")
        self._check(
            self.dwf.FDwfParamSet(ctypes.c_int(DWF_PARAM_ON_CLOSE), ctypes.c_int(1)),
            "Set stop-on-close behavior",
        )
        self._check(
            self.dwf.FDwfDeviceOpen(ctypes.c_int(self.device_index), ctypes.byref(self.handle)),
            "Open AD3 (close the WaveForms app if it is running)",
        )
        if self.handle.value == 0:
            raise DwfError(
                "No AD3 was opened. Check the USB connection and close WaveForms. "
                + self._error_message()
            )
        try:
            self._check(
                self.dwf.FDwfDeviceAutoConfigureSet(self.handle, ctypes.c_int(0)),
                "Disable automatic reconfiguration",
            )
            self._check(
                self.dwf.FDwfAnalogInChannelEnableSet(self.handle, ctypes.c_int(-1), ctypes.c_int(0)),
                "Disable analog input channels",
            )
            self._check(
                self.dwf.FDwfAnalogInChannelEnableSet(self.handle, ctypes.c_int(0), ctypes.c_int(1)),
                "Enable Scope Channel 1",
            )
            self._check(
                self.dwf.FDwfAnalogInChannelRangeSet(
                    self.handle,
                    ctypes.c_int(0),
                    ctypes.c_double(self.config.input_range_v),
                ),
                "Set Channel 1 range",
            )
            self._check(
                self.dwf.FDwfAnalogInChannelOffsetSet(
                    self.handle,
                    ctypes.c_int(0),
                    ctypes.c_double(0.0),
                ),
                "Set Channel 1 offset",
            )
            self._check(
                self.dwf.FDwfAnalogInChannelFilterSet(
                    self.handle,
                    ctypes.c_int(0),
                    ctypes.c_int(FILTER_AVERAGE),
                ),
                "Set Channel 1 average filter",
            )
            self._check(
                self.dwf.FDwfAnalogInAcquisitionModeSet(
                    self.handle,
                    ctypes.c_int(ACQMODE_RECORD),
                ),
                "Select Record acquisition mode",
            )
            self._check(
                self.dwf.FDwfAnalogInFrequencySet(
                    self.handle,
                    ctypes.c_double(self.config.sample_rate_hz),
                ),
                "Set acquisition sample rate",
            )
            self._check(
                self.dwf.FDwfAnalogInRecordLengthSet(
                    self.handle,
                    ctypes.c_double(-1.0),
                ),
                "Select infinite Record length",
            )

            if self.config.wavegen_enabled:
                self._check(
                    self.dwf.FDwfAnalogOutNodeEnableSet(
                        self.handle,
                        ctypes.c_int(0),
                        ctypes.c_int(ANALOG_OUT_NODE_CARRIER),
                        ctypes.c_int(1),
                    ),
                    "Enable W1 carrier",
                )
                self._check(
                    self.dwf.FDwfAnalogOutNodeFunctionSet(
                        self.handle,
                        ctypes.c_int(0),
                        ctypes.c_int(ANALOG_OUT_NODE_CARRIER),
                        ctypes.c_int(FUNC_SQUARE),
                    ),
                    "Set W1 square wave",
                )
                self._check(
                    self.dwf.FDwfAnalogOutNodeFrequencySet(
                        self.handle,
                        ctypes.c_int(0),
                        ctypes.c_int(ANALOG_OUT_NODE_CARRIER),
                        ctypes.c_double(self.config.wavegen_frequency_hz),
                    ),
                    "Set W1 frequency",
                )
                self._check(
                    self.dwf.FDwfAnalogOutNodeAmplitudeSet(
                        self.handle,
                        ctypes.c_int(0),
                        ctypes.c_int(ANALOG_OUT_NODE_CARRIER),
                        ctypes.c_double(self.config.wavegen_vpp / 2.0),
                    ),
                    "Set W1 peak amplitude",
                )
                self._check(
                    self.dwf.FDwfAnalogOutNodeOffsetSet(
                        self.handle,
                        ctypes.c_int(0),
                        ctypes.c_int(ANALOG_OUT_NODE_CARRIER),
                        ctypes.c_double(self.config.wavegen_offset_v),
                    ),
                    "Set W1 offset",
                )
                self._check(
                    self.dwf.FDwfAnalogOutNodeSymmetrySet(
                        self.handle,
                        ctypes.c_int(0),
                        ctypes.c_int(ANALOG_OUT_NODE_CARRIER),
                        ctypes.c_double(50.0),
                    ),
                    "Set W1 duty cycle",
                )
                self._check(
                    self.dwf.FDwfAnalogOutConfigure(
                        self.handle,
                        ctypes.c_int(0),
                        ctypes.c_int(1),
                    ),
                    "Start W1",
                )
            else:
                self._check(
                    self.dwf.FDwfAnalogOutReset(self.handle, ctypes.c_int(0)),
                    "Keep W1 disabled",
                )

            self._check(
                self.dwf.FDwfAnalogInConfigure(
                    self.handle,
                    ctypes.c_int(1),
                    ctypes.c_int(0),
                ),
                "Apply analog input configuration",
            )
            # Auto-configure is disabled, so FrequencyGet must occur after the
            # staged settings have been applied. Reading it before Configure
            # returns the device system clock (100 MHz on this AD3) instead of
            # the requested Record sample rate.
            actual_rate = ctypes.c_double()
            self._check(
                self.dwf.FDwfAnalogInFrequencyGet(
                    self.handle,
                    ctypes.byref(actual_rate),
                ),
                "Read configured acquisition sample rate",
            )
            self.sample_rate_hz = actual_rate.value
            time.sleep(2.0)
            self._check(
                self.dwf.FDwfAnalogInConfigure(
                    self.handle,
                    ctypes.c_int(0),
                    ctypes.c_int(1),
                ),
                "Start continuous acquisition",
            )
            self._started = True
        except Exception:
            self.close()
            raise

    def read_chunk(self) -> DataChunk | None:
        if self.dwf is None or not self._started:
            raise DwfError("AD3 source has not been started")
        state = ctypes.c_ubyte()
        self._check(
            self.dwf.FDwfAnalogInStatus(self.handle, ctypes.c_int(1), ctypes.byref(state)),
            "Read acquisition status",
        )
        if state.value in (DWF_STATE_CONFIG, DWF_STATE_PREFILL, DWF_STATE_ARMED):
            return None
        available = ctypes.c_int()
        lost = ctypes.c_int()
        corrupted = ctypes.c_int()
        self._check(
            self.dwf.FDwfAnalogInStatusRecord(
                self.handle,
                ctypes.byref(available),
                ctypes.byref(lost),
                ctypes.byref(corrupted),
            ),
            "Read Record buffer status",
        )
        if lost.value:
            self.sample_cursor += lost.value
        start_sample = self.sample_cursor
        if available.value <= 0:
            if lost.value or corrupted.value:
                return DataChunk(
                    start_sample=start_sample,
                    voltages=[],
                    lost_samples=lost.value,
                    corrupted_samples=corrupted.value,
                )
            return None
        data = (ctypes.c_double * available.value)()
        self._check(
            self.dwf.FDwfAnalogInStatusData(
                self.handle,
                ctypes.c_int(0),
                data,
                ctypes.c_int(available.value),
            ),
            "Read Channel 1 data",
        )
        self.sample_cursor += available.value
        return DataChunk(
            start_sample=start_sample,
            voltages=list(data),
            lost_samples=lost.value,
            corrupted_samples=corrupted.value,
        )

    def close(self) -> None:
        if self.dwf is None:
            return
        if self.handle.value:
            try:
                self.dwf.FDwfAnalogInReset(self.handle)
                self.dwf.FDwfAnalogOutReset(self.handle, ctypes.c_int(0))
                self.dwf.FDwfDeviceClose(self.handle)
            except Exception:
                pass
        self.handle = ctypes.c_int(0)
        self._started = False


class SimulatedSource:
    def __init__(
        self,
        config: LiveConfig,
        pulse_rate_hz: float,
        pulse_charge_fc: float,
        pulse_polarity: str,
        speed: float,
    ) -> None:
        self.config = config
        self.sample_rate_hz = config.sample_rate_hz
        self.pulse_rate_hz = pulse_rate_hz
        self.pulse_charge_fc = pulse_charge_fc
        self.pulse_polarity = pulse_polarity
        self.speed = speed
        self.name = "Simulation"
        self.version = "simulated"
        self.sample_cursor = 0
        self.chunk_samples = max(1, round(0.005 * self.sample_rate_hz))
        self.event_interval = max(1, round(self.sample_rate_hz / pulse_rate_hz))
        self.next_event = self.event_interval
        self.decay = math.exp(-1.0 / (self.sample_rate_hz * config.tau_us * 1e-6))
        sign = 1.0 if pulse_polarity == "positive" else -1.0
        self.pulse_amplitude_v = sign * pulse_charge_fc * 1e-3 * config.gain_v_per_pc
        self.pulse_state_v = 0.0
        self.random = random.Random(110)
        self._started = False

    def start(self) -> None:
        self._started = True

    def read_chunk(self) -> DataChunk:
        start = self.sample_cursor
        values: list[float] = []
        for sample_index in range(start, start + self.chunk_samples):
            self.pulse_state_v *= self.decay
            if sample_index >= self.next_event:
                self.pulse_state_v += self.pulse_amplitude_v
                self.next_event += self.event_interval
            elapsed = sample_index / self.sample_rate_hz
            mains_v = 0.002 * math.sin(2.0 * math.pi * 60.0 * elapsed)
            noise_v = self.random.gauss(0.0, 0.00035)
            values.append(self.pulse_state_v + mains_v + noise_v)
        self.sample_cursor += self.chunk_samples
        if self.speed > 0.0:
            time.sleep(self.chunk_samples / self.sample_rate_hz / self.speed)
        return DataChunk(start_sample=start, voltages=values)

    def close(self) -> None:
        self._started = False


class PulseLogger:
    def __init__(
        self,
        root: Path,
        config: LiveConfig,
        enabled: bool,
        save_waveforms: bool,
    ) -> None:
        self.enabled = enabled
        self.save_waveforms = save_waveforms
        self.run_directory: Path | None = None
        self.summary_stream = None
        self.summary_writer = None
        self.waveform_stream = None
        self.waveform_writer = None
        if not enabled:
            return
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.run_directory = root / timestamp
        self.run_directory.mkdir(parents=True, exist_ok=False)
        with (self.run_directory / "run_config.json").open("w", encoding="utf-8") as stream:
            json.dump(asdict(config), stream, indent=2)
        self.summary_stream = (self.run_directory / "pulses.csv").open(
            "w", encoding="utf-8", newline="", buffering=1
        )
        self.summary_writer = csv.writer(self.summary_stream)
        self.summary_writer.writerow(
            [
                "pulse",
                "peak_timestamp",
                "peak_elapsed_s",
                "peak_sample",
                "polarity",
                "peak_voltage_v",
                "baseline_v",
                "amplitude_v",
                "signed_charge_fc",
                "absolute_charge_fc",
            ]
        )
        if save_waveforms:
            self.waveform_stream = (self.run_directory / "pulse_waveforms.csv").open(
                "w", encoding="utf-8", newline="", buffering=1
            )
            self.waveform_writer = csv.writer(self.waveform_stream)
            self.waveform_writer.writerow(
                ["pulse", "time_from_peak_s", "channel_1_v"]
            )

    def log(self, pulse: LivePulse) -> None:
        if not self.enabled or self.summary_writer is None:
            return
        self.summary_writer.writerow(
            [
                pulse.number,
                pulse.peak_timestamp,
                f"{pulse.peak_elapsed_s:.12g}",
                pulse.peak_sample,
                pulse.polarity,
                f"{pulse.peak_voltage_v:.12g}",
                f"{pulse.baseline_v:.12g}",
                f"{pulse.amplitude_v:.12g}",
                f"{pulse.signed_charge_fc:.12g}",
                f"{pulse.absolute_charge_fc:.12g}",
            ]
        )
        if self.waveform_writer is not None:
            stride = max(1, math.ceil(len(pulse.waveform_time_s) / 500))
            for time_s, voltage_v in zip(
                pulse.waveform_time_s[::stride],
                pulse.waveform_voltage_v[::stride],
            ):
                self.waveform_writer.writerow(
                    [pulse.number, f"{time_s:.12g}", f"{voltage_v:.12g}"]
                )

    def close(self) -> None:
        if self.summary_stream is not None:
            self.summary_stream.close()
        if self.waveform_stream is not None:
            self.waveform_stream.close()


class AcquisitionWorker(threading.Thread):
    def __init__(
        self,
        source: DwfAD3Source | SimulatedSource,
        config: LiveConfig,
        state: SharedMonitorState,
        stop_event: threading.Event,
        logger: PulseLogger,
    ) -> None:
        super().__init__(name="LiveDAQ acquisition", daemon=True)
        self.source = source
        self.config = config
        self.state = state
        self.stop_event = stop_event
        self.logger = logger

    def run(self) -> None:
        lost_total = 0
        corrupted_total = 0
        try:
            self.state.update(status="Opening device/source")
            self.source.start()
            run_start = datetime.now().astimezone()
            detector = StreamingPulseDetector(
                self.config,
                self.source.sample_rate_hz,
                run_start,
            )
            output_directory = (
                str(self.logger.run_directory) if self.logger.run_directory else "Disabled"
            )
            self.state.update(
                status="Running",
                source_name=f"{self.source.name} (DWF {self.source.version})",
                sample_rate_hz=self.source.sample_rate_hz,
                output_directory=output_directory,
            )
            while not self.stop_event.is_set():
                chunk = self.source.read_chunk()
                if chunk is None:
                    time.sleep(0.002)
                    continue
                lost_total += chunk.lost_samples
                corrupted_total += chunk.corrupted_samples
                pulses = detector.process_chunk(chunk)
                for pulse in pulses:
                    self.logger.log(pulse)
                    self.state.events.put(pulse)
                elapsed_s = (
                    (chunk.start_sample + len(chunk.voltages))
                    / self.source.sample_rate_hz
                )
                self.state.update(
                    elapsed_s=elapsed_s,
                    event_count=detector._pulse_count,
                    lost_samples=lost_total,
                    corrupted_samples=corrupted_total,
                    noise_sigma_v=detector.noise_sigma_v,
                )
        except Exception as error:
            self.state.update(status="Error", error=str(error))
            self.stop_event.set()
        finally:
            self.state.update(status="Stopping")
            self.source.close()
            self.logger.close()
            if not self.state.snapshot().error:
                self.state.update(status="Stopped")


class LiveDAQWindow:
    def __init__(
        self,
        state: SharedMonitorState,
        stop_event: threading.Event,
        worker: AcquisitionWorker,
        gui_rate_hz: float,
        num_test: int,
    ) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError as error:
            raise RuntimeError(
                "Tkinter is required for GUI mode. Use --headless or install Tk."
            ) from error

        self.tk = tk
        self.state = state
        self.stop_event = stop_event
        self.worker = worker
        self.update_ms = max(20, round(1000.0 / gui_rate_hz))
        self.num_test = num_test
        self.pending_pulses: list[LivePulse] = []
        self.displayed_pulses: list[LivePulse] = []
        self.closing = False

        self.root = tk.Tk()
        self.root.title("CR-110 Live DAQ")
        self.root.geometry("1050x620")
        self.root.minsize(800, 500)
        self.root.protocol("WM_DELETE_WINDOW", self.request_stop)

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        title = ttk.Label(outer, text="CR-110 / Analog Discovery 3 Live Monitor")
        title.configure(font=("Segoe UI", 16, "bold"))
        title.pack(anchor="w")

        self.status_var = tk.StringVar(value="Starting...")
        self.status_label = ttk.Label(outer, textvariable=self.status_var)
        self.status_label.pack(anchor="w", pady=(4, 8))

        self.canvas = tk.Canvas(
            outer,
            background="#0c1118",
            highlightthickness=1,
            highlightbackground="#4b5563",
        )
        self.canvas.pack(fill="both", expand=True)

        self.result_frame = ttk.LabelFrame(
            outer,
            text=f"Latest complete batch ({self.num_test} pulses)",
            padding=10,
        )
        self.result_frame.pack(fill="x", pady=(10, 6))
        self.result_var = tk.StringVar(
            value=f"Waiting for {self.num_test} charge pulses... (0/{self.num_test})"
        )
        result_label = ttk.Label(self.result_frame, textvariable=self.result_var)
        result_label.configure(font=("Consolas", 10))
        result_label.pack(anchor="w")

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x")
        self.output_var = tk.StringVar(value="Output: starting...")
        ttk.Label(bottom, textvariable=self.output_var).pack(side="left")
        self.stop_button = ttk.Button(bottom, text="Stop", command=self.request_stop)
        self.stop_button.pack(side="right")

        self.root.after(self.update_ms, self.refresh)

    def request_stop(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.stop_button.configure(state="disabled")
        self.status_var.set("Stopping acquisition safely...")
        self.stop_event.set()
        self.root.after(50, self._wait_for_worker)

    def _wait_for_worker(self) -> None:
        if self.worker.is_alive():
            self.root.after(50, self._wait_for_worker)
        else:
            self.root.destroy()

    def _draw_waveforms(self, pulses: Sequence[LivePulse]) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(100, canvas.winfo_width())
        height = max(100, canvas.winfo_height())
        left, right, top, bottom = 65, width - 20, 20, height - 42
        plot_width = max(1, right - left)
        plot_height = max(1, bottom - top)

        for division in range(11):
            x = left + plot_width * division / 10
            canvas.create_line(x, top, x, bottom, fill="#263241")
        for division in range(9):
            y = top + plot_height * division / 8
            canvas.create_line(left, y, right, y, fill="#263241")

        usable_pulses = [
            pulse
            for pulse in pulses
            if pulse.waveform_time_s and pulse.waveform_voltage_v
        ]
        if not usable_pulses:
            return
        time_min = min(min(pulse.waveform_time_s) for pulse in usable_pulses)
        time_max = max(max(pulse.waveform_time_s) for pulse in usable_pulses)
        corrected_waveforms = [
            tuple(voltage - pulse.baseline_v for voltage in pulse.waveform_voltage_v)
            for pulse in usable_pulses
        ]
        voltage_min = min(min(values) for values in corrected_waveforms)
        voltage_max = max(max(values) for values in corrected_waveforms)
        voltage_min = min(voltage_min, 0.0)
        voltage_max = max(voltage_max, 0.0)
        voltage_pad = max((voltage_max - voltage_min) * 0.12, 0.001)
        voltage_min -= voltage_pad
        voltage_max += voltage_pad

        def x_of(value: float) -> float:
            return left + (value - time_min) / (time_max - time_min) * plot_width

        def y_of(value: float) -> float:
            return bottom - (value - voltage_min) / (voltage_max - voltage_min) * plot_height

        baseline_y = y_of(0.0)
        canvas.create_line(
            left,
            baseline_y,
            right,
            baseline_y,
            fill="#78909c",
            dash=(5, 4),
        )
        colors = (
            "#f4d03f",
            "#48c9b0",
            "#5dade2",
            "#af7ac5",
            "#ec7063",
            "#f5b041",
            "#58d68d",
            "#85c1e9",
        )
        for pulse_index, (pulse, corrected) in enumerate(
            zip(usable_pulses, corrected_waveforms)
        ):
            color = colors[pulse_index % len(colors)]
            times = pulse.waveform_time_s
            stride = max(1, math.ceil(len(times) / plot_width))
            points: list[float] = []
            for time_s, voltage_v in zip(times[::stride], corrected[::stride]):
                points.extend((x_of(time_s), y_of(voltage_v)))
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2)
            peak_x = x_of(0.0)
            peak_y = y_of(pulse.amplitude_v)
            canvas.create_oval(
                peak_x - 3,
                peak_y - 3,
                peak_x + 3,
                peak_y + 3,
                fill=color,
                outline="",
            )
            legend_x = left + 10 + (pulse_index % 5) * 105
            legend_y = top + 12 + (pulse_index // 5) * 18
            canvas.create_text(
                legend_x,
                legend_y,
                anchor="w",
                fill=color,
                text=f"#{pulse.number}",
            )
        canvas.create_text(
            left,
            height - 18,
            anchor="w",
            fill="#cbd5e1",
            text=f"{time_min * 1e3:.3f} ms",
        )
        canvas.create_text(
            right,
            height - 18,
            anchor="e",
            fill="#cbd5e1",
            text=f"{time_max * 1e3:.3f} ms",
        )
        canvas.create_text(
            8,
            top,
            anchor="nw",
            fill="#cbd5e1",
            text=f"ΔV {voltage_max * 1e3:.1f} mV",
        )
        canvas.create_text(
            8,
            bottom,
            anchor="sw",
            fill="#cbd5e1",
            text=f"{voltage_min * 1e3:.1f} mV",
        )
        canvas.create_text(
            (left + right) / 2,
            height - 18,
            fill="#cbd5e1",
            text="Time relative to peak",
        )

    def refresh(self) -> None:
        if self.closing:
            return
        batch_updated = False
        while True:
            try:
                self.pending_pulses.append(self.state.events.get_nowait())
            except queue.Empty:
                break
        while len(self.pending_pulses) >= self.num_test:
            self.displayed_pulses = self.pending_pulses[: self.num_test]
            del self.pending_pulses[: self.num_test]
            batch_updated = True
        snapshot = self.state.snapshot()
        warning = ""
        if snapshot.lost_samples or snapshot.corrupted_samples:
            warning = "  DATA WARNING"
        self.status_var.set(
            f"{snapshot.status} | {snapshot.source_name} | "
            f"{snapshot.sample_rate_hz / 1e3:.3f} kS/s | "
            f"elapsed {snapshot.elapsed_s:.2f} s | events {snapshot.event_count} | "
            f"lost {snapshot.lost_samples} | corrupted {snapshot.corrupted_samples} | "
            f"noise {snapshot.noise_sigma_v * 1e3:.3f} mV{warning}"
        )
        self.output_var.set(f"Output: {snapshot.output_directory}")
        self.result_frame.configure(
            text=(
                f"Latest complete batch ({self.num_test} pulses) | "
                f"next batch {len(self.pending_pulses)}/{self.num_test}"
            )
        )
        if snapshot.error:
            self.result_var.set(f"ERROR: {snapshot.error}")
        elif batch_updated:
            self.result_var.set(
                "\n".join(
                    f"#{pulse.number:06d} | {pulse.peak_timestamp} | "
                    f"t={pulse.peak_elapsed_s:.6f} s | {pulse.polarity:8s} | "
                    f"A={pulse.amplitude_v * 1e3:+.3f} mV | "
                    f"Q={pulse.absolute_charge_fc:.3f} fC"
                    for pulse in self.displayed_pulses
                )
            )
            self._draw_waveforms(self.displayed_pulses)
        elif not self.displayed_pulses:
            self.result_var.set(
                f"Waiting for {self.num_test} charge pulses... "
                f"({len(self.pending_pulses)}/{self.num_test})"
            )
        if snapshot.error or snapshot.status == "Stopped":
            self.stop_button.configure(text="Close", command=self.root.destroy)
        self.root.after(self.update_ms, self.refresh)

    def run(self) -> None:
        self.root.mainloop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Continuously stream AD3 Channel 1, detect CR-110 charge pulses, "
            "and show events in configurable GUI batches."
        )
    )
    parser.add_argument("--simulate", action="store_true", help="Use a simulated 10 Hz source.")
    parser.add_argument("--headless", action="store_true", help="Run without the Tk GUI.")
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Stop after this many wall-clock seconds; 0 means run until stopped.",
    )
    parser.add_argument("--sample-rate", type=float, default=DEFAULT_SAMPLE_RATE_HZ)
    parser.add_argument("--input-range", type=float, default=DEFAULT_INPUT_RANGE_V)
    parser.add_argument("--gain-v-per-pc", type=float, default=DEFAULT_GAIN_V_PER_PC)
    parser.add_argument("--tau-us", type=float, default=DEFAULT_TAU_US)
    parser.add_argument("--threshold-sigma", type=float, default=8.0)
    parser.add_argument("--min-charge-fc", type=float, default=1.0)
    parser.add_argument(
        "--polarity",
        choices=("both", "positive", "negative"),
        default="both",
    )
    parser.add_argument("--pretrigger-ms", type=float, default=0.5)
    parser.add_argument("--posttrigger-ms", type=float, default=1.5)
    parser.add_argument("--gui-rate", type=float, default=DEFAULT_GUI_RATE_HZ)
    parser.add_argument(
        "--num-test",
        type=int,
        default=NUM_TEST,
        help=(
            "Number of newly detected pulses displayed per GUI batch "
            f"(default: NUM_TEST={NUM_TEST})."
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Parent directory for timestamped run folders.",
    )
    parser.add_argument("--no-save", action="store_true", help="Do not save pulse summaries.")
    parser.add_argument(
        "--save-waveforms",
        action="store_true",
        help="Also append decimated per-pulse waveforms to pulse_waveforms.csv.",
    )
    parser.add_argument(
        "--wavegen",
        action="store_true",
        help="Enable W1 square wave. W1 is OFF by default for detector operation.",
    )
    parser.add_argument("--wavegen-frequency", type=float, default=500.0)
    parser.add_argument("--wavegen-vpp", type=float, default=0.100)
    parser.add_argument("--wavegen-offset", type=float, default=0.0)
    parser.add_argument("--device-index", type=int, default=-1)
    parser.add_argument("--dwf-library", default=None)
    parser.add_argument("--simulation-pulse-rate", type=float, default=10.0)
    parser.add_argument("--simulation-charge-fc", type=float, default=100.0)
    parser.add_argument(
        "--simulation-polarity",
        choices=("positive", "negative"),
        default="negative",
    )
    parser.add_argument("--simulation-speed", type=float, default=1.0)
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    positive_fields = {
        "--sample-rate": args.sample_rate,
        "--input-range": args.input_range,
        "--gain-v-per-pc": args.gain_v_per_pc,
        "--tau-us": args.tau_us,
        "--threshold-sigma": args.threshold_sigma,
        "--gui-rate": args.gui_rate,
        "--simulation-pulse-rate": args.simulation_pulse_rate,
        "--simulation-charge-fc": args.simulation_charge_fc,
    }
    for name, value in positive_fields.items():
        if value <= 0.0:
            parser.error(f"{name} must be positive")
    if args.min_charge_fc < 0.0:
        parser.error("--min-charge-fc cannot be negative")
    if args.pretrigger_ms <= 0.0 or args.posttrigger_ms <= 0.0:
        parser.error("--pretrigger-ms and --posttrigger-ms must be positive")
    if args.num_test <= 0:
        parser.error("--num-test must be a positive integer")
    if args.duration < 0.0:
        parser.error("--duration cannot be negative")
    if args.simulation_speed <= 0.0:
        parser.error("--simulation-speed must be positive")
    if args.wavegen_frequency <= 0.0 or args.wavegen_vpp <= 0.0:
        parser.error("Wavegen frequency and Vpp must be positive")


def make_config(args: argparse.Namespace) -> LiveConfig:
    return LiveConfig(
        sample_rate_hz=args.sample_rate,
        input_range_v=args.input_range,
        gain_v_per_pc=args.gain_v_per_pc,
        tau_us=args.tau_us,
        threshold_sigma=args.threshold_sigma,
        min_charge_fc=args.min_charge_fc,
        polarity=args.polarity,
        pretrigger_ms=args.pretrigger_ms,
        posttrigger_ms=args.posttrigger_ms,
        gui_rate_hz=args.gui_rate,
        num_test=args.num_test,
        wavegen_enabled=args.wavegen,
        wavegen_frequency_hz=args.wavegen_frequency,
        wavegen_vpp=args.wavegen_vpp,
        wavegen_offset_v=args.wavegen_offset,
    )


def run_headless(
    state: SharedMonitorState,
    stop_event: threading.Event,
    worker: AcquisitionWorker,
    duration_s: float,
) -> int:
    def print_pulse(pulse: LivePulse) -> None:
        print(
            f"pulse={pulse.number} time={pulse.peak_timestamp} "
            f"elapsed={pulse.peak_elapsed_s:.6f}s "
            f"polarity={pulse.polarity} "
            f"amplitude={pulse.amplitude_v * 1e3:+.3f}mV "
            f"charge={pulse.absolute_charge_fc:.3f}fC",
            flush=True,
        )

    started = time.monotonic()
    try:
        while worker.is_alive():
            while True:
                try:
                    pulse = state.events.get_nowait()
                except queue.Empty:
                    break
                print_pulse(pulse)
            snapshot = state.snapshot()
            if snapshot.error:
                print(f"LiveDAQ error: {snapshot.error}", file=sys.stderr)
                break
            if duration_s and time.monotonic() - started >= duration_s:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        worker.join(timeout=5.0)
        while True:
            try:
                print_pulse(state.events.get_nowait())
            except queue.Empty:
                break
    snapshot = state.snapshot()
    print(
        f"Stopped: events={snapshot.event_count}, lost={snapshot.lost_samples}, "
        f"corrupted={snapshot.corrupted_samples}, output={snapshot.output_directory}"
    )
    return 1 if snapshot.error else 0


class SinglePulseLiveDAQWindow(LiveDAQWindow):
    """Show the newest waveform and replace result rows in complete batches."""

    def __init__(
        self,
        state: SharedMonitorState,
        stop_event: threading.Event,
        worker: AcquisitionWorker,
        gui_rate_hz: float,
        num_test: int,
    ) -> None:
        super().__init__(state, stop_event, worker, gui_rate_hz, num_test)
        self.latest_pulse: LivePulse | None = None
        self.pending_results: list[LivePulse] = []
        self.displayed_results: list[LivePulse] = []
        self.result_frame.configure(
            text=f"Latest complete result batch ({self.num_test} pulses)"
        )
        self.result_var.set(
            f"Waiting for {self.num_test} charge pulses... (0/{self.num_test})"
        )

    def refresh(self) -> None:
        if self.closing:
            return

        newest_pulse: LivePulse | None = None
        while True:
            try:
                pulse = self.state.events.get_nowait()
            except queue.Empty:
                break
            newest_pulse = pulse
            self.pending_results.append(pulse)

        batch_updated = False
        while len(self.pending_results) >= self.num_test:
            self.displayed_results = self.pending_results[: self.num_test]
            del self.pending_results[: self.num_test]
            batch_updated = True

        if newest_pulse is not None:
            self.latest_pulse = newest_pulse
            self._draw_waveforms((newest_pulse,))

        snapshot = self.state.snapshot()
        warning = ""
        if snapshot.lost_samples or snapshot.corrupted_samples:
            warning = "  DATA WARNING"
        self.status_var.set(
            f"{snapshot.status} | {snapshot.source_name} | "
            f"{snapshot.sample_rate_hz / 1e3:.3f} kS/s | "
            f"elapsed {snapshot.elapsed_s:.2f} s | events {snapshot.event_count} | "
            f"lost {snapshot.lost_samples} | "
            f"corrupted {snapshot.corrupted_samples} | "
            f"noise {snapshot.noise_sigma_v * 1e3:.3f} mV{warning}"
        )
        self.output_var.set(f"Output: {snapshot.output_directory}")
        self.result_frame.configure(
            text=(
                f"Latest complete result batch ({self.num_test} pulses) | "
                f"next batch {len(self.pending_results)}/{self.num_test}"
            )
        )

        if snapshot.error:
            self.result_var.set(f"ERROR: {snapshot.error}")
        elif batch_updated:
            self.result_var.set(
                "\n".join(
                    f"#{pulse.number:06d} | {pulse.peak_timestamp} | "
                    f"t={pulse.peak_elapsed_s:.6f} s | {pulse.polarity:8s} | "
                    f"A={pulse.amplitude_v * 1e3:+.3f} mV | "
                    f"Q={pulse.absolute_charge_fc:.3f} fC"
                    for pulse in self.displayed_results
                )
            )
        elif not self.displayed_results:
            self.result_var.set(
                f"Waiting for {self.num_test} charge pulses... "
                f"({len(self.pending_results)}/{self.num_test})"
            )

        if snapshot.error or snapshot.status == "Stopped":
            self.stop_button.configure(text="Close", command=self.root.destroy)
        self.root.after(self.update_ms, self.refresh)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    config = make_config(args)

    state = SharedMonitorState()
    stop_event = threading.Event()
    try:
        logger = PulseLogger(
            args.output_root,
            config,
            enabled=not args.no_save,
            save_waveforms=args.save_waveforms,
        )
    except OSError as error:
        print(f"Could not create output directory: {error}", file=sys.stderr)
        return 1

    if args.simulate:
        source: DwfAD3Source | SimulatedSource = SimulatedSource(
            config,
            args.simulation_pulse_rate,
            args.simulation_charge_fc,
            args.simulation_polarity,
            args.simulation_speed,
        )
    else:
        source = DwfAD3Source(config, args.device_index, args.dwf_library)

    worker = AcquisitionWorker(source, config, state, stop_event, logger)
    worker.start()
    if args.headless:
        return run_headless(state, stop_event, worker, args.duration)

    try:
        window = SinglePulseLiveDAQWindow(
            state,
            stop_event,
            worker,
            args.gui_rate,
            args.num_test,
        )
        if args.duration:
            window.root.after(round(args.duration * 1000), window.request_stop)
        window.run()
    except Exception as error:
        stop_event.set()
        worker.join(timeout=5.0)
        print(f"Could not start GUI: {error}", file=sys.stderr)
        return 1
    return 1 if state.snapshot().error else 0


if __name__ == "__main__":
    raise SystemExit(main())
