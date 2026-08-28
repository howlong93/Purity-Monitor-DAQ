#!/usr/bin/env python3
"""Two-channel flat live DAQ for the purity monitor, CR-110s, and AD3.

Scope Channel 1 is the cathode/Qc channel and Scope Channel 2 is the
anode/Qa channel. Pulses are detected independently and paired by peak time.
The GUI displays consecutive, non-overlapping raw two-channel time windows and
updates result rows in independent pair batches.

The online charge estimate uses the uncalibrated CR-110 nominal conversion
(peak amplitude / 1.4 V/pC). It is intentionally a live-monitoring estimate,
not the final waveform-integral/model-fit reconstruction described in the
purity-monitor thesis.

Close WaveForms before hardware mode; only one process can own the AD3.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import math
import queue
import random
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

import numpy as np


DEFAULT_SAMPLE_RATE_HZ = 500_000.0
DEFAULT_INPUT_RANGE_V = 1.0
DEFAULT_GAIN_V_PER_PC = 1.4
DEFAULT_TAU_US = 140.0
DEFAULT_GUI_RATE_HZ = 10.0
DEFAULT_DRIFT_TIME_US = 55.0
DEFAULT_DRIFT_WINDOW_US = 15.0
DEFAULT_CANVA_SIZE_S = 0.5
CANVAS_MAX_SAMPLES = 50_000
NOISE_UPDATE_INTERVAL_S = 1.0
NOISE_TARGET_SAMPLES = 5_000
NOISE_MIN_VALID_SAMPLES = 1_000
NUM_TEST = 10

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = SCRIPT_DIRECTORY / "results" / "live_2c_flat"

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
    channel_1_polarity: str
    channel_2_polarity: str
    pretrigger_ms: float
    posttrigger_ms: float
    gui_rate_hz: float
    num_test: int
    canva_size: float
    drift_time_us: float
    drift_window_us: float
    wavegen_enabled: bool
    wavegen_frequency_hz: float
    wavegen_vpp: float
    wavegen_offset_v: float


@dataclass(frozen=True)
class DualDataChunk:
    start_sample: int
    channel_1_v: np.ndarray
    channel_2_v: np.ndarray
    lost_samples: int = 0
    corrupted_samples: int = 0


@dataclass(frozen=True)
class DualPlotWindow:
    start_sample: int
    source_sample_count: int
    sample_offsets: np.ndarray
    channel_1_v: np.ndarray
    channel_2_v: np.ndarray


@dataclass(frozen=True)
class LivePulse:
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


@dataclass(frozen=True)
class PulsePair:
    number: int
    cathode: LivePulse
    anode: LivePulse
    peak_delay_us: float

    @property
    def charge_ratio(self) -> float:
        if self.cathode.absolute_charge_fc == 0.0:
            return math.nan
        return self.anode.absolute_charge_fc / self.cathode.absolute_charge_fc


@dataclass
class ActiveCapture:
    onset_sample: int
    baseline_v: float
    peak_search_end: int
    capture_end: int


@dataclass(frozen=True)
class MonitorSnapshot:
    status: str
    source_name: str
    sample_rate_hz: float
    elapsed_s: float
    pair_count: int
    cathode_count: int
    anode_count: int
    unmatched_cathodes: int
    unmatched_anodes: int
    lost_samples: int
    corrupted_samples: int
    channel_1_noise_sigma_v: float
    channel_2_noise_sigma_v: float
    output_directory: str
    error: str


class SharedMonitorState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = "Starting"
        self._source_name = ""
        self._sample_rate_hz = 0.0
        self._elapsed_s = 0.0
        self._pair_count = 0
        self._cathode_count = 0
        self._anode_count = 0
        self._unmatched_cathodes = 0
        self._unmatched_anodes = 0
        self._lost_samples = 0
        self._corrupted_samples = 0
        self._channel_1_noise_sigma_v = 0.0
        self._channel_2_noise_sigma_v = 0.0
        self._output_directory = ""
        self._error = ""
        self.events: queue.Queue[PulsePair] = queue.Queue()
        # Chunks are published by reference. The acquisition worker does not
        # copy or reduce plot data, keeping the hardware read path short.
        self.plot_chunks: queue.SimpleQueue[DualDataChunk] = queue.SimpleQueue()

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
                pair_count=self._pair_count,
                cathode_count=self._cathode_count,
                anode_count=self._anode_count,
                unmatched_cathodes=self._unmatched_cathodes,
                unmatched_anodes=self._unmatched_anodes,
                lost_samples=self._lost_samples,
                corrupted_samples=self._corrupted_samples,
                channel_1_noise_sigma_v=self._channel_1_noise_sigma_v,
                channel_2_noise_sigma_v=self._channel_2_noise_sigma_v,
                output_directory=self._output_directory,
                error=self._error,
            )


class StreamingPulseDetector:
    """Chunk-vectorized detector for one channel of a dual-channel stream."""

    def __init__(
        self,
        config: LiveConfig,
        sample_rate_hz: float,
        run_start: datetime,
        polarity: str,
    ) -> None:
        self.config = config
        self.sample_rate_hz = sample_rate_hz
        self.run_start = run_start
        self.polarity = polarity
        self.tau_samples = max(1, round(config.tau_us * 1e-6 * sample_rate_hz))
        self.edge_lag = max(
            1, round(max(5e-6, 0.035 * config.tau_us * 1e-6) * sample_rate_hz)
        )
        self.baseline_guard = max(self.edge_lag * 2, round(0.04 * self.tau_samples))
        self.baseline_samples = max(5, self.tau_samples)
        self.pretrigger_samples = max(1, round(config.pretrigger_ms * 1e-3 * sample_rate_hz))
        self.posttrigger_samples = max(1, round(config.posttrigger_ms * 1e-3 * sample_rate_hz))
        self.peak_lookahead = max(self.edge_lag, round(0.75 * self.tau_samples))
        self.refractory_samples = max(1, round(0.75 * self.tau_samples))
        self.history_samples = max(
            self.pretrigger_samples,
            self.baseline_samples + self.baseline_guard,
            self.edge_lag,
        ) + 16

        self._buffer_start_sample: int | None = None
        self._buffer_values = np.empty(0, dtype=np.float64)
        self.noise_sample_indices: list[int] = []
        self.noise_edge_samples: list[float] = []
        self.noise_exclusion_ranges: deque[tuple[int, int]] = deque()
        self.noise_stride = max(1, round(sample_rate_hz / NOISE_TARGET_SAMPLES))
        self.noise_update_samples = max(1, round(NOISE_UPDATE_INTERVAL_S * sample_rate_hz))
        self.noise_sigma_v = 0.0
        self.noise_ready = False
        self._last_noise_update = 0
        self._last_sample: int | None = None
        self._last_candidate = -10**18
        self._candidate_block_until = -10**18
        self._active: ActiveCapture | None = None
        self.pulse_count = 0

    def reset_after_gap(self, next_sample: int | None = None) -> None:
        self._buffer_start_sample = None
        self._buffer_values = np.empty(0, dtype=np.float64)
        self._active = None
        self._last_sample = None
        self._last_candidate = -10**18
        self._candidate_block_until = -10**18
        self.noise_sample_indices.clear()
        self.noise_edge_samples.clear()
        self.noise_exclusion_ranges.clear()
        if next_sample is not None:
            self._last_noise_update = next_sample

    def _update_noise(self, sample_index: int) -> None:
        if sample_index - self._last_noise_update < self.noise_update_samples:
            return
        indices = np.asarray(self.noise_sample_indices, dtype=np.int64)
        edges = np.asarray(self.noise_edge_samples, dtype=np.float64)
        valid = np.ones(edges.size, dtype=bool)
        interval_start = sample_index - self.noise_update_samples + 1
        while self.noise_exclusion_ranges and self.noise_exclusion_ranges[0][1] < interval_start:
            self.noise_exclusion_ranges.popleft()
        for exclusion_start, exclusion_end in self.noise_exclusion_ranges:
            valid &= (indices < exclusion_start) | (indices > exclusion_end)
        if self._active is not None:
            active_start = self._active.onset_sample - self.pretrigger_samples
            valid &= (indices < active_start) | (indices > self._active.capture_end)
        background = edges[valid]
        if background.size >= NOISE_MIN_VALID_SAMPLES:
            center = np.median(background)
            mad = np.median(np.abs(background - center))
            self.noise_sigma_v = 1.4826 * float(mad) / math.sqrt(2.0)
            self.noise_ready = self.noise_sigma_v > 0.0
        self.noise_sample_indices.clear()
        self.noise_edge_samples.clear()
        self.noise_exclusion_ranges.clear()
        self._last_noise_update = sample_index

    def _threshold_v(self) -> float:
        if not self.noise_ready:
            return math.inf
        minimum = self.config.min_charge_fc * 1e-3 * self.config.gain_v_per_pc
        adaptive = self.config.threshold_sigma * self.noise_sigma_v * math.sqrt(2.0)
        return max(minimum, adaptive)

    def _append_buffer(self, start_sample: int, values: np.ndarray) -> None:
        if values.size == 0:
            return
        if self._buffer_start_sample is None:
            self._buffer_start_sample = start_sample
            self._buffer_values = values.copy()
            return
        expected = self._buffer_start_sample + self._buffer_values.size
        if start_sample != expected:
            raise RuntimeError(f"Detector buffer discontinuity: expected {expected}, got {start_sample}")
        self._buffer_values = np.concatenate((self._buffer_values, values))

    def _slice_buffer(self, start: int, end: int) -> np.ndarray | None:
        if self._buffer_start_sample is None:
            return None
        buffer_end = self._buffer_start_sample + self._buffer_values.size
        if start < self._buffer_start_sample or end > buffer_end or end <= start:
            return None
        return self._buffer_values[start - self._buffer_start_sample : end - self._buffer_start_sample]

    def _trim_buffer(self) -> None:
        if self._buffer_start_sample is None or self._buffer_values.size == 0:
            return
        buffer_end = self._buffer_start_sample + self._buffer_values.size
        keep_from = max(self._buffer_start_sample, buffer_end - self.history_samples)
        if self._active is not None:
            keep_from = min(keep_from, self._active.onset_sample - self.pretrigger_samples)
        drop = keep_from - self._buffer_start_sample
        if drop > 0:
            self._buffer_values = self._buffer_values[drop:].copy()
            self._buffer_start_sample = keep_from

    def _new_edges(self, start_sample: int, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        prior_tail = self._buffer_values[-self.edge_lag :]
        combined = np.concatenate((prior_tail, values)) if prior_tail.size else values
        tail_count = prior_tail.size
        first_local = max(0, self.edge_lag - tail_count)
        if first_local >= values.size:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
        local = np.arange(first_local, values.size, dtype=np.int64)
        current = tail_count + local
        return start_sample + local, values[local] - combined[current - self.edge_lag]

    def _collect_noise_samples(self, indices: np.ndarray, edges: np.ndarray) -> None:
        if indices.size == 0:
            return
        first = (-int(indices[0])) % self.noise_stride
        self.noise_sample_indices.extend(indices[first:: self.noise_stride].tolist())
        self.noise_edge_samples.extend(edges[first:: self.noise_stride].tolist())

    def _create_capture(self, sample_index: int) -> bool:
        baseline_end = sample_index - self.baseline_guard
        baseline = self._slice_buffer(baseline_end - self.baseline_samples, baseline_end)
        if baseline is None or baseline.size < 5:
            return False
        self._active = ActiveCapture(
            onset_sample=sample_index,
            baseline_v=float(np.median(baseline)),
            peak_search_end=sample_index + self.peak_lookahead,
            capture_end=sample_index + self.posttrigger_samples,
        )
        self._last_candidate = sample_index
        self._candidate_block_until = self._active.capture_end
        return True

    def _finalize_active(self) -> LivePulse | None:
        active = self._active
        if active is None:
            return None
        waveform_start = active.onset_sample - self.pretrigger_samples
        waveform_end = active.capture_end + 1
        waveform = self._slice_buffer(waveform_start, waveform_end)
        if waveform is None:
            return None
        peak_start = max(waveform_start, active.onset_sample - self.edge_lag)
        peak_end = min(waveform_end, active.peak_search_end + 1)
        peak_values = self._slice_buffer(peak_start, peak_end)
        if peak_values is None or peak_values.size == 0:
            return None
        peak_offset = int(np.argmax(np.abs(peak_values - active.baseline_v)))
        peak_sample = peak_start + peak_offset
        peak_voltage = float(peak_values[peak_offset])
        amplitude = peak_voltage - active.baseline_v
        self._active = None
        self.noise_exclusion_ranges.append((waveform_start, active.capture_end))
        if abs(amplitude) < self._threshold_v():
            return None
        if self.polarity == "positive" and amplitude <= 0.0:
            return None
        if self.polarity == "negative" and amplitude >= 0.0:
            return None
        self.pulse_count += 1
        elapsed = peak_sample / self.sample_rate_hz
        timestamp = self.run_start + timedelta(seconds=elapsed)
        charge = amplitude / self.config.gain_v_per_pc * 1000.0
        samples = np.arange(waveform_start, waveform_end, dtype=np.int64)
        waveform_time = (samples - peak_sample) / self.sample_rate_hz
        return LivePulse(
            peak_sample=peak_sample,
            peak_elapsed_s=elapsed,
            # Microsecond precision is required because the two peaks are only
            # about 50-60 us apart. The GUI still shows only time-of-day.
            peak_timestamp=timestamp.isoformat(timespec="microseconds"),
            polarity="positive" if amplitude > 0.0 else "negative",
            peak_voltage_v=peak_voltage,
            baseline_v=active.baseline_v,
            amplitude_v=amplitude,
            signed_charge_fc=charge,
            absolute_charge_fc=abs(charge),
            waveform_time_s=tuple(waveform_time.tolist()),
            waveform_voltage_v=tuple(waveform.tolist()),
        )

    def process_chunk(self, start_sample: int, voltages: np.ndarray, gap: bool) -> list[LivePulse]:
        values = np.asarray(voltages, dtype=np.float64).reshape(-1)
        discontinuity = self._last_sample is not None and start_sample != self._last_sample + 1
        if gap or discontinuity:
            self.reset_after_gap(start_sample)
        if values.size == 0:
            return []
        indices, edges = self._new_edges(start_sample, values)
        self._append_buffer(start_sample, values)
        self._collect_noise_samples(indices, edges)
        threshold = self._threshold_v()
        candidates = indices[np.abs(edges) >= threshold] if math.isfinite(threshold) else np.empty(0, dtype=np.int64)
        pulses: list[LivePulse] = []
        buffer_end = start_sample + values.size - 1
        if self._active is not None and self._active.capture_end <= buffer_end:
            pulse = self._finalize_active()
            if pulse is not None:
                pulses.append(pulse)
        for value in candidates:
            candidate = int(value)
            if candidate <= self._candidate_block_until:
                continue
            if candidate - self._last_candidate < self.refractory_samples:
                continue
            if self._active is not None:
                break
            if not self._create_capture(candidate):
                continue
            if self._active.capture_end <= buffer_end:
                pulse = self._finalize_active()
                if pulse is not None:
                    pulses.append(pulse)
        self._last_sample = buffer_end
        self._update_noise(buffer_end)
        self._trim_buffer()
        return pulses


class PulsePairer:
    def __init__(
        self,
        sample_rate_hz: float,
        drift_time_us: float,
        window_us: float,
        max_pending_per_channel: int = 1_000,
    ) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.min_delay = round((drift_time_us - window_us) * 1e-6 * sample_rate_hz)
        self.max_delay = round((drift_time_us + window_us) * 1e-6 * sample_rate_hz)
        self.max_pending_per_channel = max_pending_per_channel
        self.cathodes: deque[LivePulse] = deque()
        self.anodes: deque[LivePulse] = deque()
        self.pair_count = 0
        self.unmatched_cathodes = 0
        self.unmatched_anodes = 0

    def reset_after_gap(self) -> None:
        self.unmatched_cathodes += len(self.cathodes)
        self.unmatched_anodes += len(self.anodes)
        self.cathodes.clear()
        self.anodes.clear()

    def add(self, cathodes: Sequence[LivePulse], anodes: Sequence[LivePulse]) -> list[PulsePair]:
        self.cathodes.extend(cathodes)
        self.anodes.extend(anodes)
        pairs: list[PulsePair] = []
        while self.cathodes and self.anodes:
            cathode = self.cathodes[0]
            anode = self.anodes[0]
            delay = anode.peak_sample - cathode.peak_sample
            if delay < self.min_delay:
                self.anodes.popleft()
                self.unmatched_anodes += 1
                continue
            if delay > self.max_delay:
                self.cathodes.popleft()
                self.unmatched_cathodes += 1
                continue
            self.cathodes.popleft()
            self.anodes.popleft()
            self.pair_count += 1
            pairs.append(PulsePair(self.pair_count, cathode, anode, delay / self.sample_rate_hz * 1e6))
        # Do not expire from the hardware cursor: the two detectors publish only
        # after their independent post-trigger captures finish, and chunk size
        # adds variable latency. A later opposing pulse proves an old pulse is
        # unpairable in the loop above. The caps only protect memory if an input
        # channel disappears completely.
        while len(self.cathodes) > self.max_pending_per_channel:
            self.cathodes.popleft()
            self.unmatched_cathodes += 1
        while len(self.anodes) > self.max_pending_per_channel:
            self.anodes.popleft()
            self.unmatched_anodes += 1
        return pairs


class DualContinuousPlotAccumulator:
    """Build fixed consecutive dual-channel windows without sliding copies."""

    def __init__(self, sample_rate_hz: float, window_s: float) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.window_s = window_s
        self.window_samples = max(1, round(window_s * sample_rate_hz))
        self._start_sample: int | None = None
        self._next_sample: int | None = None
        self._sample_count = 0
        self._channel_1_parts: list[np.ndarray] = []
        self._channel_2_parts: list[np.ndarray] = []

    @property
    def pending_fraction(self) -> float:
        return self._sample_count / self.window_samples

    def reset(self) -> None:
        self._start_sample = None
        self._next_sample = None
        self._sample_count = 0
        self._channel_1_parts.clear()
        self._channel_2_parts.clear()

    @staticmethod
    def _join(parts: Sequence[np.ndarray]) -> np.ndarray:
        if len(parts) == 1:
            return np.asarray(parts[0], dtype=np.float64)
        return np.concatenate(parts)

    def _finish_window(self) -> DualPlotWindow:
        assert self._start_sample is not None
        channel_1 = self._join(self._channel_1_parts)
        channel_2 = self._join(self._channel_2_parts)
        if channel_1.size != channel_2.size:
            raise RuntimeError("Dual plot window channel lengths differ")
        stride = max(1, math.ceil(channel_1.size / CANVAS_MAX_SAMPLES))
        offsets = np.arange(0, channel_1.size, stride, dtype=np.int64)
        window = DualPlotWindow(
            start_sample=self._start_sample,
            source_sample_count=channel_1.size,
            sample_offsets=offsets,
            channel_1_v=channel_1[offsets].copy(),
            channel_2_v=channel_2[offsets].copy(),
        )
        self.reset()
        return window

    def ingest(self, chunk: DualDataChunk) -> list[DualPlotWindow]:
        channel_1 = np.asarray(chunk.channel_1_v, dtype=np.float64).reshape(-1)
        channel_2 = np.asarray(chunk.channel_2_v, dtype=np.float64).reshape(-1)
        if channel_1.size != channel_2.size:
            raise RuntimeError("Dual plot chunk channel lengths differ")
        discontinuity = (
            self._next_sample is not None
            and chunk.start_sample != self._next_sample
        )
        if chunk.lost_samples or chunk.corrupted_samples or discontinuity:
            self.reset()
        if channel_1.size == 0:
            return []

        windows: list[DualPlotWindow] = []
        offset = 0
        while offset < channel_1.size:
            absolute_sample = chunk.start_sample + offset
            if self._start_sample is None:
                self._start_sample = absolute_sample
                self._next_sample = absolute_sample
            if absolute_sample != self._next_sample:
                self.reset()
                self._start_sample = absolute_sample
                self._next_sample = absolute_sample
            take = min(
                self.window_samples - self._sample_count,
                channel_1.size - offset,
            )
            self._channel_1_parts.append(channel_1[offset : offset + take])
            self._channel_2_parts.append(channel_2[offset : offset + take])
            self._sample_count += take
            self._next_sample += take
            offset += take
            if self._sample_count == self.window_samples:
                windows.append(self._finish_window())
        return windows


def min_max_plot_envelope(
    sample_offsets: np.ndarray,
    voltages: np.ndarray,
    pixel_width: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Reduce Canvas points while retaining each pixel bucket's extrema."""
    point_count = voltages.size
    target_buckets = max(1, pixel_width)
    if point_count <= target_buckets * 2:
        return sample_offsets, voltages
    bucket_size = math.ceil(point_count / target_buckets)
    bucket_count = math.ceil(point_count / bucket_size)
    padded_count = bucket_count * bucket_size
    padded = np.full(padded_count, np.nan, dtype=np.float64)
    padded[:point_count] = voltages
    buckets = padded.reshape(bucket_count, bucket_size)
    minima = np.nanargmin(buckets, axis=1)
    maxima = np.nanargmax(buckets, axis=1)
    first = np.minimum(minima, maxima)
    second = np.maximum(minima, maxima)
    starts = np.arange(bucket_count, dtype=np.int64) * bucket_size
    indices = np.column_stack((starts + first, starts + second)).reshape(-1)
    indices = indices[indices < point_count]
    return sample_offsets[indices], voltages[indices]


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
            return [r"C:\Program Files\Digilent\WaveForms3\dwf.dll", r"C:\Program Files (x86)\Digilent\WaveForms3\dwf.dll", "dwf.dll"]
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
        raise DwfError("Could not load WaveForms SDK; install it or pass --dwf-library. " + " | ".join(errors))

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
        self._check(self.dwf.FDwfParamSet(ctypes.c_int(DWF_PARAM_ON_CLOSE), ctypes.c_int(1)), "Set close behavior")
        self._check(self.dwf.FDwfDeviceOpen(ctypes.c_int(self.device_index), ctypes.byref(self.handle)), "Open AD3")
        if self.handle.value == 0:
            raise DwfError("No AD3 opened. Check USB and close WaveForms. " + self._error_message())
        try:
            self._check(self.dwf.FDwfDeviceAutoConfigureSet(self.handle, ctypes.c_int(0)), "Disable auto configuration")
            self._check(self.dwf.FDwfAnalogInChannelEnableSet(self.handle, ctypes.c_int(-1), ctypes.c_int(0)), "Disable channels")
            for channel in (0, 1):
                label = channel + 1
                self._check(self.dwf.FDwfAnalogInChannelEnableSet(self.handle, ctypes.c_int(channel), ctypes.c_int(1)), f"Enable Channel {label}")
                self._check(self.dwf.FDwfAnalogInChannelRangeSet(self.handle, ctypes.c_int(channel), ctypes.c_double(self.config.input_range_v)), f"Set Channel {label} range")
                self._check(self.dwf.FDwfAnalogInChannelOffsetSet(self.handle, ctypes.c_int(channel), ctypes.c_double(0.0)), f"Set Channel {label} offset")
                self._check(self.dwf.FDwfAnalogInChannelFilterSet(self.handle, ctypes.c_int(channel), ctypes.c_int(FILTER_AVERAGE)), f"Set Channel {label} filter")
            self._check(self.dwf.FDwfAnalogInAcquisitionModeSet(self.handle, ctypes.c_int(ACQMODE_RECORD)), "Select Record mode")
            self._check(self.dwf.FDwfAnalogInFrequencySet(self.handle, ctypes.c_double(self.config.sample_rate_hz)), "Set sample rate")
            self._check(self.dwf.FDwfAnalogInRecordLengthSet(self.handle, ctypes.c_double(-1.0)), "Select infinite Record")
            if self.config.wavegen_enabled:
                self._check(self.dwf.FDwfAnalogOutNodeEnableSet(self.handle, ctypes.c_int(0), ctypes.c_int(ANALOG_OUT_NODE_CARRIER), ctypes.c_int(1)), "Enable W1")
                self._check(self.dwf.FDwfAnalogOutNodeFunctionSet(self.handle, ctypes.c_int(0), ctypes.c_int(ANALOG_OUT_NODE_CARRIER), ctypes.c_int(FUNC_SQUARE)), "Set W1 square")
                self._check(self.dwf.FDwfAnalogOutNodeFrequencySet(self.handle, ctypes.c_int(0), ctypes.c_int(ANALOG_OUT_NODE_CARRIER), ctypes.c_double(self.config.wavegen_frequency_hz)), "Set W1 frequency")
                self._check(self.dwf.FDwfAnalogOutNodeAmplitudeSet(self.handle, ctypes.c_int(0), ctypes.c_int(ANALOG_OUT_NODE_CARRIER), ctypes.c_double(self.config.wavegen_vpp / 2.0)), "Set W1 amplitude")
                self._check(self.dwf.FDwfAnalogOutNodeOffsetSet(self.handle, ctypes.c_int(0), ctypes.c_int(ANALOG_OUT_NODE_CARRIER), ctypes.c_double(self.config.wavegen_offset_v)), "Set W1 offset")
                self._check(self.dwf.FDwfAnalogOutNodeSymmetrySet(self.handle, ctypes.c_int(0), ctypes.c_int(ANALOG_OUT_NODE_CARRIER), ctypes.c_double(50.0)), "Set W1 duty")
                self._check(self.dwf.FDwfAnalogOutConfigure(self.handle, ctypes.c_int(0), ctypes.c_int(1)), "Start W1")
            else:
                self._check(self.dwf.FDwfAnalogOutReset(self.handle, ctypes.c_int(0)), "Keep W1 disabled")
            self._check(self.dwf.FDwfAnalogInConfigure(self.handle, ctypes.c_int(1), ctypes.c_int(0)), "Apply input configuration")
            actual = ctypes.c_double()
            self._check(self.dwf.FDwfAnalogInFrequencyGet(self.handle, ctypes.byref(actual)), "Read sample rate")
            self.sample_rate_hz = actual.value
            time.sleep(2.0)
            self._check(self.dwf.FDwfAnalogInConfigure(self.handle, ctypes.c_int(0), ctypes.c_int(1)), "Start acquisition")
            self._started = True
        except Exception:
            self.close()
            raise

    def read_chunk(self) -> DualDataChunk | None:
        if self.dwf is None or not self._started:
            raise DwfError("AD3 source has not been started")
        state = ctypes.c_ubyte()
        self._check(self.dwf.FDwfAnalogInStatus(self.handle, ctypes.c_int(1), ctypes.byref(state)), "Read acquisition status")
        if state.value in (DWF_STATE_CONFIG, DWF_STATE_PREFILL, DWF_STATE_ARMED):
            return None
        available, lost, corrupted = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        self._check(self.dwf.FDwfAnalogInStatusRecord(self.handle, ctypes.byref(available), ctypes.byref(lost), ctypes.byref(corrupted)), "Read Record status")
        if lost.value:
            self.sample_cursor += lost.value
        start = self.sample_cursor
        if available.value <= 0:
            if lost.value or corrupted.value:
                empty = np.empty(0, dtype=np.float64)
                return DualDataChunk(start, empty, empty.copy(), lost.value, corrupted.value)
            return None
        data_1 = (ctypes.c_double * available.value)()
        data_2 = (ctypes.c_double * available.value)()
        self._check(self.dwf.FDwfAnalogInStatusData(self.handle, ctypes.c_int(0), data_1, ctypes.c_int(available.value)), "Read Channel 1")
        self._check(self.dwf.FDwfAnalogInStatusData(self.handle, ctypes.c_int(1), data_2, ctypes.c_int(available.value)), "Read Channel 2")
        self.sample_cursor += available.value
        return DualDataChunk(start, np.ctypeslib.as_array(data_1).copy(), np.ctypeslib.as_array(data_2).copy(), lost.value, corrupted.value)

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
    def __init__(self, config: LiveConfig, pulse_rate_hz: float, cathode_charge_fc: float, anode_charge_fc: float, speed: float) -> None:
        self.config = config
        self.sample_rate_hz = config.sample_rate_hz
        self.speed = speed
        self.name = "Two-channel simulation"
        self.version = "simulated"
        self.sample_cursor = 0
        self.chunk_samples = max(1, round(0.005 * self.sample_rate_hz))
        self.event_interval = max(1, round(self.sample_rate_hz / pulse_rate_hz))
        self.drift_samples = round(config.drift_time_us * 1e-6 * self.sample_rate_hz)
        self.next_cathode = self.event_interval
        self.pending_anodes: deque[int] = deque()
        self.decay = math.exp(-1.0 / (self.sample_rate_hz * config.tau_us * 1e-6))
        self.cathode_step = -cathode_charge_fc * 1e-3 * config.gain_v_per_pc
        self.anode_step = anode_charge_fc * 1e-3 * config.gain_v_per_pc
        self.cathode_state = 0.0
        self.anode_state = 0.0
        self.random = random.Random(2110)
        self._started = False

    def start(self) -> None:
        self._started = True

    def read_chunk(self) -> DualDataChunk:
        start = self.sample_cursor
        ch1 = np.empty(self.chunk_samples, dtype=np.float64)
        ch2 = np.empty(self.chunk_samples, dtype=np.float64)
        for offset, sample in enumerate(range(start, start + self.chunk_samples)):
            self.cathode_state *= self.decay
            self.anode_state *= self.decay
            if sample >= self.next_cathode:
                self.cathode_state += self.cathode_step
                self.pending_anodes.append(self.next_cathode + self.drift_samples)
                self.next_cathode += self.event_interval
            while self.pending_anodes and sample >= self.pending_anodes[0]:
                self.pending_anodes.popleft()
                self.anode_state += self.anode_step
            elapsed = sample / self.sample_rate_hz
            mains = 0.002 * math.sin(2.0 * math.pi * 60.0 * elapsed)
            ch1[offset] = self.cathode_state + mains + self.random.gauss(0.0, 0.00035)
            ch2[offset] = self.anode_state + 0.8 * mains + self.random.gauss(0.0, 0.00040)
        self.sample_cursor += self.chunk_samples
        time.sleep(self.chunk_samples / self.sample_rate_hz / self.speed)
        return DualDataChunk(start, ch1, ch2)

    def close(self) -> None:
        self._started = False


class PairLogger:
    def __init__(self, root: Path, config: LiveConfig, enabled: bool, save_waveforms: bool) -> None:
        self.enabled = enabled
        self.save_waveforms = save_waveforms
        self.run_directory: Path | None = None
        self.summary_stream = None
        self.summary_writer = None
        self.waveform_stream = None
        self.waveform_writer = None
        if not enabled:
            return
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.run_directory = root / stamp
        self.run_directory.mkdir(parents=True, exist_ok=False)
        with (self.run_directory / "run_config.json").open("w", encoding="utf-8") as stream:
            json.dump(asdict(config), stream, indent=2)
        self.summary_stream = (self.run_directory / "pulse_pairs.csv").open("w", encoding="utf-8", newline="", buffering=1)
        self.summary_writer = csv.writer(self.summary_stream)
        columns = ["pair"]
        for prefix in ("cathode_ch1", "anode_ch2"):
            columns.extend([f"{prefix}_peak_timestamp", f"{prefix}_peak_elapsed_s", f"{prefix}_peak_sample", f"{prefix}_polarity", f"{prefix}_peak_voltage_v", f"{prefix}_baseline_v", f"{prefix}_amplitude_v", f"{prefix}_signed_charge_fc", f"{prefix}_absolute_charge_fc"])
        columns.extend(["peak_delay_us", "qa_over_qc"])
        self.summary_writer.writerow(columns)
        if save_waveforms:
            self.waveform_stream = (self.run_directory / "pair_waveforms.csv").open("w", encoding="utf-8", newline="", buffering=1)
            self.waveform_writer = csv.writer(self.waveform_stream)
            self.waveform_writer.writerow(["pair", "channel", "time_from_cathode_peak_s", "voltage_v"])

    def log(self, pair: PulsePair) -> None:
        if not self.enabled or self.summary_writer is None:
            return
        row: list[object] = [pair.number]
        for pulse in (pair.cathode, pair.anode):
            row.extend([pulse.peak_timestamp, f"{pulse.peak_elapsed_s:.12g}", pulse.peak_sample, pulse.polarity, f"{pulse.peak_voltage_v:.12g}", f"{pulse.baseline_v:.12g}", f"{pulse.amplitude_v:.12g}", f"{pulse.signed_charge_fc:.12g}", f"{pulse.absolute_charge_fc:.12g}"])
        row.extend([f"{pair.peak_delay_us:.12g}", f"{pair.charge_ratio:.12g}"])
        self.summary_writer.writerow(row)
        if self.waveform_writer is not None:
            for label, pulse in (("cathode_ch1", pair.cathode), ("anode_ch2", pair.anode)):
                shift = pulse.peak_elapsed_s - pair.cathode.peak_elapsed_s
                stride = max(1, math.ceil(len(pulse.waveform_time_s) / 750))
                for t, voltage in zip(pulse.waveform_time_s[::stride], pulse.waveform_voltage_v[::stride]):
                    self.waveform_writer.writerow([pair.number, label, f"{t + shift:.12g}", f"{voltage:.12g}"])

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
        logger: PairLogger,
        publish_plot_data: bool,
    ) -> None:
        super().__init__(name="LiveDAQ 2c acquisition", daemon=True)
        self.source = source
        self.config = config
        self.state = state
        self.stop_event = stop_event
        self.logger = logger
        self.publish_plot_data = publish_plot_data

    def run(self) -> None:
        lost_total = corrupted_total = 0
        try:
            self.state.update(status="Opening device/source")
            self.source.start()
            run_start = datetime.now().astimezone()
            cathode_detector = StreamingPulseDetector(self.config, self.source.sample_rate_hz, run_start, self.config.channel_1_polarity)
            anode_detector = StreamingPulseDetector(self.config, self.source.sample_rate_hz, run_start, self.config.channel_2_polarity)
            pairer = PulsePairer(
                self.source.sample_rate_hz,
                self.config.drift_time_us,
                self.config.drift_window_us,
            )
            output = str(self.logger.run_directory) if self.logger.run_directory else "Disabled"
            self.state.update(status="Learning noise (1 s warm-up)", source_name=f"{self.source.name} (DWF {self.source.version})", sample_rate_hz=self.source.sample_rate_hz, output_directory=output)
            while not self.stop_event.is_set():
                chunk = self.source.read_chunk()
                if chunk is None:
                    time.sleep(0.002)
                    continue
                lost_total += chunk.lost_samples
                corrupted_total += chunk.corrupted_samples
                gap = bool(chunk.lost_samples)
                if gap:
                    pairer.reset_after_gap()
                cathodes = cathode_detector.process_chunk(chunk.start_sample, chunk.channel_1_v, gap)
                anodes = anode_detector.process_chunk(chunk.start_sample, chunk.channel_2_v, gap)
                pairs = pairer.add(cathodes, anodes)
                for pair in pairs:
                    self.logger.log(pair)
                    self.state.events.put(pair)
                if self.publish_plot_data:
                    self.state.plot_chunks.put(chunk)
                elapsed = (chunk.start_sample + len(chunk.channel_1_v)) / self.source.sample_rate_hz
                noise_ready = cathode_detector.noise_ready and anode_detector.noise_ready
                self.state.update(
                    status="Running" if noise_ready else "Learning noise (1 s warm-up)",
                    elapsed_s=elapsed,
                    pair_count=pairer.pair_count,
                    cathode_count=cathode_detector.pulse_count,
                    anode_count=anode_detector.pulse_count,
                    unmatched_cathodes=pairer.unmatched_cathodes,
                    unmatched_anodes=pairer.unmatched_anodes,
                    lost_samples=lost_total,
                    corrupted_samples=corrupted_total,
                    channel_1_noise_sigma_v=cathode_detector.noise_sigma_v,
                    channel_2_noise_sigma_v=anode_detector.noise_sigma_v,
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


class LiveDAQ2CWindow:
    PAIR_WIDTH = 7
    TIME_WIDTH = 15
    ELAPSED_WIDTH = 12
    PEAK_WIDTH = 10
    CHARGE_WIDTH = 14
    DERIVED_WIDTH = 22

    def __init__(self, state: SharedMonitorState, stop_event: threading.Event, worker: AcquisitionWorker, gui_rate_hz: float, num_test: int) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError as error:
            raise RuntimeError("Tkinter is required; use --headless or install Tk") from error
        self.tk = tk
        self.state = state
        self.stop_event = stop_event
        self.worker = worker
        self.update_ms = max(20, round(1000.0 / gui_rate_hz))
        self.num_test = num_test
        self.pending_results: list[PulsePair] = []
        self.displayed_results: list[PulsePair] = []
        self.closing = False
        self.root = tk.Tk()
        self.root.title("Purity Monitor Two-Channel Live DAQ")
        self.root.geometry("1500x720")
        self.root.minsize(1050, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.request_stop)
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        title = ttk.Label(outer, text="Purity Monitor / CR-110 / Analog Discovery 3 - Two Channels")
        title.configure(font=("Segoe UI", 16, "bold"))
        title.pack(anchor="w")
        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(4, 8))
        self.canvas = tk.Canvas(outer, background="#0c1118", highlightthickness=1, highlightbackground="#4b5563")
        self.canvas.pack(fill="both", expand=True)
        self.result_frame = ttk.LabelFrame(outer, text=f"Latest complete result batch ({num_test} pairs)", padding=10)
        self.result_frame.pack(fill="x", pady=(10, 6))
        self.result_var = tk.StringVar(value=f"Waiting for {num_test} cathode/anode pairs... (0/{num_test})")
        label = ttk.Label(self.result_frame, textvariable=self.result_var)
        label.configure(font=("Consolas", 9))
        label.pack(anchor="w")
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

    def _draw_pair(self, pair: PulsePair) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width, height = max(100, canvas.winfo_width()), max(100, canvas.winfo_height())
        left, right, top, bottom = 75, width - 25, 25, height - 45
        plot_w, plot_h = max(1, right - left), max(1, bottom - top)
        for division in range(11):
            x = left + plot_w * division / 10
            canvas.create_line(x, top, x, bottom, fill="#263241")
        for division in range(9):
            y = top + plot_h * division / 8
            canvas.create_line(left, y, right, y, fill="#263241")
        cathode_t = np.asarray(pair.cathode.waveform_time_s)
        anode_t = np.asarray(pair.anode.waveform_time_s) + pair.peak_delay_us * 1e-6
        cathode_v = np.asarray(pair.cathode.waveform_voltage_v) - pair.cathode.baseline_v
        anode_v = np.asarray(pair.anode.waveform_voltage_v) - pair.anode.baseline_v
        # Display only the shared absolute sample interval so both colored
        # traces are continuous across exactly the same cathode-to-anode view.
        time_min = float(max(cathode_t.min(), anode_t.min()))
        time_max = float(min(cathode_t.max(), anode_t.max()))
        cathode_mask = (cathode_t >= time_min) & (cathode_t <= time_max)
        anode_mask = (anode_t >= time_min) & (anode_t <= time_max)
        cathode_t, cathode_v = cathode_t[cathode_mask], cathode_v[cathode_mask]
        anode_t, anode_v = anode_t[anode_mask], anode_v[anode_mask]
        voltage_min = float(min(cathode_v.min(), anode_v.min(), 0.0))
        voltage_max = float(max(cathode_v.max(), anode_v.max(), 0.0))
        pad = max((voltage_max - voltage_min) * 0.12, 0.001)
        voltage_min -= pad
        voltage_max += pad
        x_of = lambda value: left + (value - time_min) / (time_max - time_min) * plot_w
        y_of = lambda value: bottom - (value - voltage_min) / (voltage_max - voltage_min) * plot_h
        canvas.create_line(left, y_of(0.0), right, y_of(0.0), fill="#78909c", dash=(5, 4))
        for times, volts, color in ((cathode_t, cathode_v, "#5dade2"), (anode_t, anode_v, "#ff5bd7")):
            stride = max(1, math.ceil(len(times) / plot_w))
            points: list[float] = []
            for t, v in zip(times[::stride], volts[::stride]):
                points.extend((x_of(float(t)), y_of(float(v))))
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2)
        for t, amp, color in ((0.0, pair.cathode.amplitude_v, "#5dade2"), (pair.peak_delay_us * 1e-6, pair.anode.amplitude_v, "#ff5bd7")):
            canvas.create_oval(x_of(t) - 4, y_of(amp) - 4, x_of(t) + 4, y_of(amp) + 4, fill=color, outline="")
        canvas.create_text(left + 10, top + 12, anchor="w", fill="#5dade2", text="Channel 1: cathode / Qc")
        canvas.create_text(left + 190, top + 12, anchor="w", fill="#ff5bd7", text="Channel 2: anode / Qa")
        canvas.create_text(left, height - 18, anchor="w", fill="#cbd5e1", text=f"{time_min * 1e3:.3f} ms")
        canvas.create_text(right, height - 18, anchor="e", fill="#cbd5e1", text=f"{time_max * 1e3:.3f} ms")
        canvas.create_text((left + right) / 2, height - 18, fill="#cbd5e1", text="Time relative to cathode peak")
        canvas.create_text(8, top, anchor="nw", fill="#cbd5e1", text=f"{voltage_max * 1e3:.1f} mV")
        canvas.create_text(8, bottom, anchor="sw", fill="#cbd5e1", text=f"{voltage_min * 1e3:.1f} mV")

    @staticmethod
    def _channel_header(charge_name: str) -> str:
        return (
            f"{'Peak Time':<{LiveDAQ2CWindow.TIME_WIDTH}}  "
            f"{'Elapsed (s)':>{LiveDAQ2CWindow.ELAPSED_WIDTH}}  "
            f"{'Peak (mV)':>{LiveDAQ2CWindow.PEAK_WIDTH}}  "
            f"{charge_name:>{LiveDAQ2CWindow.CHARGE_WIDTH}}"
        )

    @staticmethod
    def _channel_values(pulse: LivePulse) -> str:
        return (
            f"{pulse.peak_timestamp[11:26]:<{LiveDAQ2CWindow.TIME_WIDTH}}  "
            f"{pulse.peak_elapsed_s:{LiveDAQ2CWindow.ELAPSED_WIDTH}.6f}  "
            f"{pulse.amplitude_v * 1e3:{LiveDAQ2CWindow.PEAK_WIDTH}.3f}  "
            f"{pulse.absolute_charge_fc:{LiveDAQ2CWindow.CHARGE_WIDTH}.3f}"
        )

    @classmethod
    def _result_header_lines(cls) -> tuple[str, str, str]:
        channel_width = len(cls._channel_header("Charge Qc (fC)"))
        group_header = (
            f"{'':<{cls.PAIR_WIDTH}} | "
            f"{'Channel 1 - Cathode / Qc':^{channel_width}} | "
            f"{'Channel 2 - Anode / Qa':^{channel_width}} | "
            f"{'Derived':^{cls.DERIVED_WIDTH}}"
        )
        column_header = (
            f"{'Pair':<{cls.PAIR_WIDTH}} | "
            f"{cls._channel_header('Charge Qc (fC)')} | "
            f"{cls._channel_header('Charge Qa (fC)')} | "
            f"{'Delay (us)':>10}  {'Qa/Qc':>10}"
        )
        return group_header, column_header, "-" * len(column_header)

    @staticmethod
    def _result_line(pair: PulsePair) -> str:
        c, a = pair.cathode, pair.anode
        return (
            f"#{pair.number:06d} | "
            f"{LiveDAQ2CWindow._channel_values(c)} | "
            f"{LiveDAQ2CWindow._channel_values(a)} | "
            f"{pair.peak_delay_us:10.2f}  "
            f"{pair.charge_ratio:10.6f}"
        )

    def refresh(self) -> None:
        if self.closing:
            return
        newest: PulsePair | None = None
        while True:
            try:
                newest = self.state.events.get_nowait()
            except queue.Empty:
                break
            self.pending_results.append(newest)
        if newest is not None:
            self._draw_pair(newest)
        batch_updated = False
        while len(self.pending_results) >= self.num_test:
            self.displayed_results = self.pending_results[: self.num_test]
            del self.pending_results[: self.num_test]
            batch_updated = True
        snapshot = self.state.snapshot()
        warning = "  DATA WARNING" if snapshot.lost_samples or snapshot.corrupted_samples else ""
        self.status_var.set(
            f"{snapshot.status} | {snapshot.source_name} | {snapshot.sample_rate_hz / 1e3:.3f} kS/s | "
            f"elapsed {snapshot.elapsed_s:.2f}s | pairs {snapshot.pair_count} | detected C/A "
            f"{snapshot.cathode_count}/{snapshot.anode_count} | unmatched C/A "
            f"{snapshot.unmatched_cathodes}/{snapshot.unmatched_anodes} | lost {snapshot.lost_samples} | "
            f"corrupted {snapshot.corrupted_samples} | noise C/A "
            f"{snapshot.channel_1_noise_sigma_v * 1e3:.3f}/{snapshot.channel_2_noise_sigma_v * 1e3:.3f}mV{warning}"
        )
        self.output_var.set(f"Output: {snapshot.output_directory}")
        self.result_frame.configure(text=f"Latest complete result batch ({self.num_test} pairs) | next batch {len(self.pending_results)}/{self.num_test}")
        if snapshot.error:
            self.result_var.set(f"ERROR: {snapshot.error}")
        elif batch_updated:
            table_lines = [
                *self._result_header_lines(),
                *(self._result_line(pair) for pair in self.displayed_results),
            ]
            self.result_var.set("\n".join(table_lines))
        elif not self.displayed_results:
            self.result_var.set(f"Waiting for {self.num_test} cathode/anode pairs... ({len(self.pending_results)}/{self.num_test})")
        if snapshot.error or snapshot.status == "Stopped":
            self.stop_button.configure(text="Close", command=self.root.destroy)
        self.root.after(self.update_ms, self.refresh)

    def run(self) -> None:
        self.root.mainloop()


class FlatLiveDAQ2CWindow(LiveDAQ2CWindow):
    """Display fixed raw dual-channel windows and independent pair batches."""

    def __init__(
        self,
        state: SharedMonitorState,
        stop_event: threading.Event,
        worker: AcquisitionWorker,
        num_test: int,
        canva_size: float,
    ) -> None:
        super().__init__(state, stop_event, worker, 1.0 / canva_size, num_test)
        self.root.title("Purity Monitor Two-Channel Flat Live DAQ")
        self.canva_size = canva_size
        self.update_ms = max(20, round(canva_size * 1000))
        self.plot_accumulator: DualContinuousPlotAccumulator | None = None
        self.latest_window: DualPlotWindow | None = None
        self.recent_pairs: deque[PulsePair] = deque()
        self.pending_results: list[PulsePair] = []
        self.displayed_results: list[PulsePair] = []
        self.result_frame.configure(
            text=f"Latest complete result batch ({self.num_test} pairs)"
        )
        self.result_var.set(
            f"Waiting for {self.num_test} cathode/anode pairs... "
            f"(0/{self.num_test})"
        )

    def _draw_continuous_window(
        self,
        window: DualPlotWindow,
        pairs: Sequence[PulsePair],
        sample_rate_hz: float,
    ) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(100, canvas.winfo_width())
        height = max(100, canvas.winfo_height())
        left, right, top, bottom = 80, width - 25, 28, height - 48
        plot_width = max(1, right - left)
        plot_height = max(1, bottom - top)

        for division in range(11):
            x = left + plot_width * division / 10
            canvas.create_line(x, top, x, bottom, fill="#263241")
        for division in range(9):
            y = top + plot_height * division / 8
            canvas.create_line(left, y, right, y, fill="#263241")

        channel_1 = window.channel_1_v
        channel_2 = window.channel_2_v
        if channel_1.size == 0 or channel_2.size == 0:
            return
        voltage_min = float(min(np.min(channel_1), np.min(channel_2), 0.0))
        voltage_max = float(max(np.max(channel_1), np.max(channel_2), 0.0))
        voltage_pad = max((voltage_max - voltage_min) * 0.12, 0.001)
        voltage_min -= voltage_pad
        voltage_max += voltage_pad

        denominator = max(1, window.source_sample_count - 1)

        def x_of_offset(sample_offset: float) -> float:
            return left + sample_offset / denominator * plot_width

        def y_of(voltage_v: float) -> float:
            return bottom - (
                (voltage_v - voltage_min)
                / (voltage_max - voltage_min)
                * plot_height
            )

        canvas.create_line(
            left,
            y_of(0.0),
            right,
            y_of(0.0),
            fill="#78909c",
            dash=(5, 4),
        )
        for voltages, color in (
            (channel_1, "#5dade2"),
            (channel_2, "#ff5bd7"),
        ):
            draw_offsets, draw_voltages = min_max_plot_envelope(
                window.sample_offsets,
                voltages,
                round(plot_width),
            )
            x_values = (
                left
                + draw_offsets.astype(np.float64) / denominator * plot_width
            )
            y_values = (
                bottom
                - (draw_voltages - voltage_min)
                / (voltage_max - voltage_min)
                * plot_height
            )
            coordinates = np.column_stack((x_values, y_values)).reshape(-1).tolist()
            if len(coordinates) >= 4:
                canvas.create_line(*coordinates, fill=color, width=1.5)

        window_end = window.start_sample + window.source_sample_count
        for pair in pairs:
            for pulse, color, row, label in (
                (pair.cathode, "#5dade2", 0, "C"),
                (pair.anode, "#ff5bd7", 1, "A"),
            ):
                if not window.start_sample <= pulse.peak_sample < window_end:
                    continue
                offset = pulse.peak_sample - window.start_sample
                peak_x = x_of_offset(offset)
                peak_y = y_of(pulse.peak_voltage_v)
                canvas.create_line(
                    peak_x,
                    top,
                    peak_x,
                    bottom,
                    fill=color,
                    dash=(3, 4),
                )
                canvas.create_oval(
                    peak_x - 3,
                    peak_y - 3,
                    peak_x + 3,
                    peak_y + 3,
                    fill=color,
                    outline="",
                )
                canvas.create_text(
                    peak_x + 4,
                    top + 28 + row * 16,
                    anchor="nw",
                    fill=color,
                    text=f"#{pair.number} {label}",
                )

        canvas.create_text(
            left + 10,
            top + 12,
            anchor="w",
            fill="#5dade2",
            text="Channel 1: cathode / Qc",
        )
        canvas.create_text(
            left + 195,
            top + 12,
            anchor="w",
            fill="#ff5bd7",
            text="Channel 2: anode / Qa",
        )
        start_s = window.start_sample / sample_rate_hz
        end_s = (window_end - 1) / sample_rate_hz
        canvas.create_text(
            left,
            height - 18,
            anchor="w",
            fill="#cbd5e1",
            text=f"{start_s:.6f} s",
        )
        canvas.create_text(
            right,
            height - 18,
            anchor="e",
            fill="#cbd5e1",
            text=f"{end_s:.6f} s",
        )
        canvas.create_text(
            (left + right) / 2,
            height - 18,
            fill="#cbd5e1",
            text=(
                "Continuous acquisition time "
                f"(non-overlapping {self.canva_size:g} s window)"
            ),
        )
        canvas.create_text(
            8,
            top,
            anchor="nw",
            fill="#cbd5e1",
            text=f"{voltage_max * 1e3:.1f} mV",
        )
        canvas.create_text(
            8,
            bottom,
            anchor="sw",
            fill="#cbd5e1",
            text=f"{voltage_min * 1e3:.1f} mV",
        )

    def refresh(self) -> None:
        if self.closing:
            return
        snapshot = self.state.snapshot()
        if self.plot_accumulator is None and snapshot.sample_rate_hz > 0.0:
            self.plot_accumulator = DualContinuousPlotAccumulator(
                snapshot.sample_rate_hz,
                self.canva_size,
            )

        while True:
            try:
                pair = self.state.events.get_nowait()
            except queue.Empty:
                break
            self.recent_pairs.append(pair)
            self.pending_results.append(pair)

        batch_updated = False
        while len(self.pending_results) >= self.num_test:
            self.displayed_results = self.pending_results[: self.num_test]
            del self.pending_results[: self.num_test]
            batch_updated = True

        latest_complete_window: DualPlotWindow | None = None
        while True:
            try:
                chunk = self.state.plot_chunks.get_nowait()
            except queue.Empty:
                break
            if self.plot_accumulator is None:
                continue
            completed = self.plot_accumulator.ingest(chunk)
            if completed:
                latest_complete_window = completed[-1]

        if latest_complete_window is not None:
            self.latest_window = latest_complete_window
            window_end = (
                latest_complete_window.start_sample
                + latest_complete_window.source_sample_count
            )
            while (
                self.recent_pairs
                and max(
                    self.recent_pairs[0].cathode.peak_sample,
                    self.recent_pairs[0].anode.peak_sample,
                )
                < latest_complete_window.start_sample
            ):
                self.recent_pairs.popleft()
            window_pairs = tuple(
                pair
                for pair in self.recent_pairs
                if (
                    latest_complete_window.start_sample
                    <= pair.cathode.peak_sample
                    < window_end
                    or latest_complete_window.start_sample
                    <= pair.anode.peak_sample
                    < window_end
                )
            )
            self._draw_continuous_window(
                latest_complete_window,
                window_pairs,
                snapshot.sample_rate_hz,
            )

        warning = (
            "  DATA WARNING"
            if snapshot.lost_samples or snapshot.corrupted_samples
            else ""
        )
        progress = (
            self.plot_accumulator.pending_fraction
            if self.plot_accumulator is not None
            else 0.0
        )
        self.status_var.set(
            f"{snapshot.status} | {snapshot.source_name} | "
            f"{snapshot.sample_rate_hz / 1e3:.3f} kS/s | "
            f"elapsed {snapshot.elapsed_s:.2f}s | pairs {snapshot.pair_count} | "
            f"detected C/A {snapshot.cathode_count}/{snapshot.anode_count} | "
            f"unmatched C/A {snapshot.unmatched_cathodes}/"
            f"{snapshot.unmatched_anodes} | lost {snapshot.lost_samples} | "
            f"corrupted {snapshot.corrupted_samples} | noise C/A "
            f"{snapshot.channel_1_noise_sigma_v * 1e3:.3f}/"
            f"{snapshot.channel_2_noise_sigma_v * 1e3:.3f}mV | "
            f"next canvas {progress * 100:.0f}%{warning}"
        )
        self.output_var.set(f"Output: {snapshot.output_directory}")
        self.result_frame.configure(
            text=(
                f"Latest complete result batch ({self.num_test} pairs) | "
                f"next batch {len(self.pending_results)}/{self.num_test}"
            )
        )
        if snapshot.error:
            self.result_var.set(f"ERROR: {snapshot.error}")
        elif batch_updated:
            table_lines = [
                *self._result_header_lines(),
                *(self._result_line(pair) for pair in self.displayed_results),
            ]
            self.result_var.set("\n".join(table_lines))
        elif not self.displayed_results:
            self.result_var.set(
                f"Waiting for {self.num_test} cathode/anode pairs... "
                f"({len(self.pending_results)}/{self.num_test})"
            )
        if snapshot.error or snapshot.status == "Stopped":
            self.stop_button.configure(text="Close", command=self.root.destroy)
        self.root.after(self.update_ms, self.refresh)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream AD3 Channels 1/2, show consecutive raw time windows, "
            "pair cathode/anode pulses, and estimate Qc, Qa, and Qa/Qc."
        )
    )
    parser.add_argument("--simulate", action="store_true", help="Use a simulated paired-pulse source.")
    parser.add_argument("--headless", action="store_true", help="Run without Tk GUI.")
    parser.add_argument("--duration", type=float, default=0.0, help="Wall-clock seconds; 0 runs until stopped.")
    parser.add_argument("--sample-rate", type=float, default=DEFAULT_SAMPLE_RATE_HZ)
    parser.add_argument("--input-range", type=float, default=DEFAULT_INPUT_RANGE_V)
    parser.add_argument("--gain-v-per-pc", type=float, default=DEFAULT_GAIN_V_PER_PC)
    parser.add_argument("--tau-us", type=float, default=DEFAULT_TAU_US)
    parser.add_argument("--threshold-sigma", type=float, default=8.0)
    parser.add_argument("--min-charge-fc", type=float, default=1.0)
    parser.add_argument("--polarity", choices=("both", "positive", "negative"), default=None, help="Compatibility override applied to both channels.")
    parser.add_argument("--channel-1-polarity", choices=("both", "positive", "negative"), default="negative")
    parser.add_argument("--channel-2-polarity", choices=("both", "positive", "negative"), default="positive")
    parser.add_argument("--pretrigger-ms", type=float, default=0.5)
    parser.add_argument("--posttrigger-ms", type=float, default=1.5)
    parser.add_argument(
        "--gui-rate",
        type=float,
        default=DEFAULT_GUI_RATE_HZ,
        help="Retained for compatibility; flat refresh timing uses --canva-size.",
    )
    parser.add_argument("--num-test", type=int, default=NUM_TEST, help="Result rows replaced per non-overlapping batch.")
    parser.add_argument(
        "--canva-size",
        type=float,
        default=DEFAULT_CANVA_SIZE_S,
        help=(
            "Seconds in each consecutive non-overlapping canvas window "
            f"(default: {DEFAULT_CANVA_SIZE_S:.3f})."
        ),
    )
    parser.add_argument("--drift-time-us", type=float, default=DEFAULT_DRIFT_TIME_US, help="Expected Channel-1 to Channel-2 peak delay.")
    parser.add_argument("--drift-window-us", type=float, default=DEFAULT_DRIFT_WINDOW_US, help="Allowed +/- tolerance around --drift-time-us.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--save-waveforms", action="store_true")
    parser.add_argument("--wavegen", action="store_true", help="Enable W1 square wave; OFF by default.")
    parser.add_argument("--wavegen-frequency", type=float, default=500.0)
    parser.add_argument("--wavegen-vpp", type=float, default=0.100)
    parser.add_argument("--wavegen-offset", type=float, default=0.0)
    parser.add_argument("--device-index", type=int, default=-1)
    parser.add_argument("--dwf-library", default=None)
    parser.add_argument("--simulation-pulse-rate", type=float, default=10.0)
    parser.add_argument(
        "--simulation-cathode-charge-fc",
        type=float,
        default=100.0,
        help="Simulated cathode charge Qc in fC.",
    )
    parser.add_argument("--simulation-anode-charge-fc", type=float, default=95.0)
    parser.add_argument("--simulation-polarity", choices=("positive", "negative"), default=None, help="Retained for CLI compatibility; 2c simulation uses physical default polarities.")
    parser.add_argument("--simulation-speed", type=float, default=1.0)
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    positive = {
        "--sample-rate": args.sample_rate,
        "--input-range": args.input_range,
        "--gain-v-per-pc": args.gain_v_per_pc,
        "--tau-us": args.tau_us,
        "--threshold-sigma": args.threshold_sigma,
        "--gui-rate": args.gui_rate,
        "--canva-size": args.canva_size,
        "--simulation-pulse-rate": args.simulation_pulse_rate,
        "--simulation-cathode-charge-fc": args.simulation_cathode_charge_fc,
        "--simulation-anode-charge-fc": args.simulation_anode_charge_fc,
        "--drift-time-us": args.drift_time_us,
        "--drift-window-us": args.drift_window_us,
    }
    for name, value in positive.items():
        if value <= 0.0:
            parser.error(f"{name} must be positive")
    if args.drift_window_us >= args.drift_time_us:
        parser.error("--drift-window-us must be smaller than --drift-time-us")
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
    channel_1_polarity = args.polarity or args.channel_1_polarity
    channel_2_polarity = args.polarity or args.channel_2_polarity
    return LiveConfig(
        sample_rate_hz=args.sample_rate,
        input_range_v=args.input_range,
        gain_v_per_pc=args.gain_v_per_pc,
        tau_us=args.tau_us,
        threshold_sigma=args.threshold_sigma,
        min_charge_fc=args.min_charge_fc,
        channel_1_polarity=channel_1_polarity,
        channel_2_polarity=channel_2_polarity,
        pretrigger_ms=args.pretrigger_ms,
        posttrigger_ms=args.posttrigger_ms,
        gui_rate_hz=1.0 / args.canva_size,
        num_test=args.num_test,
        canva_size=args.canva_size,
        drift_time_us=args.drift_time_us,
        drift_window_us=args.drift_window_us,
        wavegen_enabled=args.wavegen,
        wavegen_frequency_hz=args.wavegen_frequency,
        wavegen_vpp=args.wavegen_vpp,
        wavegen_offset_v=args.wavegen_offset,
    )


def run_headless(state: SharedMonitorState, stop_event: threading.Event, worker: AcquisitionWorker, duration_s: float) -> int:
    def print_pair(pair: PulsePair) -> None:
        print(
            f"pair={pair.number} cathode_time={pair.cathode.peak_timestamp} "
            f"Qc={pair.cathode.absolute_charge_fc:.3f}fC anode_time={pair.anode.peak_timestamp} "
            f"Qa={pair.anode.absolute_charge_fc:.3f}fC delay={pair.peak_delay_us:.3f}us "
            f"Qa/Qc={pair.charge_ratio:.6f}", flush=True
        )
    started = time.monotonic()
    try:
        while worker.is_alive():
            while True:
                try:
                    print_pair(state.events.get_nowait())
                except queue.Empty:
                    break
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
                print_pair(state.events.get_nowait())
            except queue.Empty:
                break
    snapshot = state.snapshot()
    print(f"Stopped: pairs={snapshot.pair_count}, unmatched C/A={snapshot.unmatched_cathodes}/{snapshot.unmatched_anodes}, lost={snapshot.lost_samples}, corrupted={snapshot.corrupted_samples}, output={snapshot.output_directory}")
    return 1 if snapshot.error else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    config = make_config(args)
    state = SharedMonitorState()
    stop_event = threading.Event()
    try:
        logger = PairLogger(args.output_root, config, enabled=not args.no_save, save_waveforms=args.save_waveforms)
    except OSError as error:
        print(f"Could not create output directory: {error}", file=sys.stderr)
        return 1
    if args.simulate:
        source: DwfAD3Source | SimulatedSource = SimulatedSource(
            config,
            args.simulation_pulse_rate,
            args.simulation_cathode_charge_fc,
            args.simulation_anode_charge_fc,
            args.simulation_speed,
        )
    else:
        source = DwfAD3Source(config, args.device_index, args.dwf_library)
    worker = AcquisitionWorker(
        source,
        config,
        state,
        stop_event,
        logger,
        publish_plot_data=not args.headless,
    )
    worker.start()
    if args.headless:
        return run_headless(state, stop_event, worker, args.duration)
    try:
        window = FlatLiveDAQ2CWindow(
            state,
            stop_event,
            worker,
            args.num_test,
            args.canva_size,
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
