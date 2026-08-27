#!/usr/bin/env python3
"""Detect CR-110 charge pulses in Digilent WaveForms CSV acquisitions.

Only Channel 1 is used for pulse detection and charge reconstruction.  The
default conversion uses the CR-110 nominal charge gain of 1.4 V/pC; it does
not include any channel-specific calibration correction.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIRECTORY / "AD3 results" / "csv"
DEFAULT_GAIN_V_PER_PC = 1.4
DEFAULT_TAU_US = 140.0


@dataclass(frozen=True)
class Pulse:
    peak_time_s: float
    peak_voltage_v: float
    baseline_v: float
    amplitude_v: float
    charge_pc: float


def median_absolute_deviation(values: Sequence[float]) -> float:
    """Return the unscaled median absolute deviation."""
    if not values:
        return 0.0
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def robust_sigma(values: Sequence[float]) -> float:
    """Estimate Gaussian sigma from the median absolute deviation."""
    return 1.4826 * median_absolute_deviation(values)


def moving_average(values: Sequence[float], width: int) -> list[float]:
    """Centered moving average with shortened windows at the two ends."""
    width = max(1, width)
    if width % 2 == 0:
        width += 1
    half_width = width // 2
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)

    averaged: list[float] = []
    for index in range(len(values)):
        left = max(0, index - half_width)
        right = min(len(values), index + half_width + 1)
        averaged.append((prefix[right] - prefix[left]) / (right - left))
    return averaged


def read_waveforms_csv(path: Path) -> tuple[list[float], list[float]]:
    """Read time and Channel 1 from a WaveForms oscilloscope CSV export."""
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream)
        header: list[str] | None = None
        time_index = -1
        channel_index = -1
        times: list[float] = []
        channel_1: list[float] = []

        for row_number, row in enumerate(rows, start=1):
            if not row:
                continue

            normalized = [cell.strip() for cell in row]
            if header is None:
                if normalized[0].lower() == "time (s)":
                    header = normalized
                    try:
                        time_index = next(
                            i for i, name in enumerate(header)
                            if name.lower() == "time (s)"
                        )
                        channel_index = next(
                            i for i, name in enumerate(header)
                            if name.lower() == "channel 1 (v)"
                        )
                    except StopIteration as error:
                        raise ValueError(
                            "CSV header must contain 'Time (s)' and "
                            "'Channel 1 (V)'."
                        ) from error
                continue

            try:
                time_value = float(normalized[time_index])
                voltage_value = float(normalized[channel_index])
            except (IndexError, ValueError) as error:
                raise ValueError(
                    f"Invalid numeric data at CSV row {row_number}."
                ) from error

            if not (math.isfinite(time_value) and math.isfinite(voltage_value)):
                raise ValueError(f"Non-finite data at CSV row {row_number}.")
            times.append(time_value)
            channel_1.append(voltage_value)

    if header is None:
        raise ValueError(
            "Could not find the WaveForms CSV data header. Expected "
            "'Time (s),Channel 1 (V),...'."
        )
    if len(times) < 10:
        raise ValueError("CSV contains fewer than 10 waveform samples.")
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("Time samples must be strictly increasing.")
    return times, channel_1


def detect_pulses(
    times: Sequence[float],
    voltages: Sequence[float],
    *,
    gain_v_per_pc: float,
    tau_us: float,
    threshold_sigma: float,
    relative_threshold: float,
    min_charge_fc: float | None,
    polarity: str,
) -> tuple[list[Pulse], float, float]:
    """Detect sharp pulse onsets and reconstruct charge from local peak height.

    Detection uses a short-timescale change in Channel 1, so slow baseline
    drift and mains pickup do not act as triggers.  Pulse height is measured
    relative to the median voltage immediately before each onset.
    """
    sample_intervals = [
        right - left for left, right in zip(times, times[1:])
    ]
    dt_s = statistics.median(sample_intervals)
    sample_rate_hz = 1.0 / dt_s
    tau_s = tau_us * 1e-6

    smooth_width = max(1, round(2e-6 / dt_s))
    smoothed = moving_average(voltages, smooth_width)

    # Compare samples across a short window. A CR-110 pulse has a sharp onset,
    # whereas its nominal 140 us decay and low-frequency baseline drift change
    # only slightly across this interval.
    edge_span = max(1, round(max(5e-6, 0.035 * tau_s) / dt_s))
    edge_values = [0.0] * len(smoothed)
    valid_edge_values: list[float] = []
    for index in range(edge_span, len(smoothed) - edge_span):
        edge = smoothed[index + edge_span] - smoothed[index - edge_span]
        edge_values[index] = edge
        valid_edge_values.append(edge)

    edge_center = statistics.median(valid_edge_values)
    edge_sigma = robust_sigma(valid_edge_values)
    if edge_sigma == 0.0:
        edge_sigma = max(abs(value - edge_center) for value in valid_edge_values) * 1e-6
    strongest_edge = max(
        abs(value - edge_center) for value in valid_edge_values
    )
    edge_threshold = max(
        threshold_sigma * edge_sigma,
        relative_threshold * strongest_edge,
    )

    above_threshold = [
        edge_span <= index < len(smoothed) - edge_span
        and abs(edge_values[index] - edge_center) >= edge_threshold
        for index in range(len(smoothed))
    ]

    # Collapse each contiguous threshold crossing to its strongest edge.
    candidate_indices: list[int] = []
    index = edge_span
    while index < len(smoothed) - edge_span:
        if not above_threshold[index]:
            index += 1
            continue
        group_start = index
        while index < len(smoothed) - edge_span and above_threshold[index]:
            index += 1
        group_end = index
        candidate_indices.append(
            max(
                range(group_start, group_end),
                key=lambda item: abs(edge_values[item] - edge_center),
            )
        )

    # A single physical edge can make more than one nearby threshold island.
    # Events closer than most of one decay constant cannot be cleanly resolved
    # by simple peak-height reconstruction. Treat nearby threshold islands as
    # one pulse instead of counting decay/ringing structure more than once.
    merge_samples = max(1, round(0.75 * tau_s / dt_s))
    merged_candidates: list[int] = []
    for candidate in candidate_indices:
        if not merged_candidates or candidate - merged_candidates[-1] > merge_samples:
            merged_candidates.append(candidate)
        elif abs(edge_values[candidate] - edge_center) > abs(
            edge_values[merged_candidates[-1]] - edge_center
        ):
            merged_candidates[-1] = candidate

    # Estimate point noise from first differences. Sparse pulse edges do not
    # dominate the median-based estimator.
    first_differences = [
        right - left for left, right in zip(voltages, voltages[1:])
    ]
    voltage_noise_sigma = robust_sigma(first_differences) / math.sqrt(2.0)
    amplitude_threshold_v = threshold_sigma * voltage_noise_sigma
    if min_charge_fc is not None:
        amplitude_threshold_v = max(
            amplitude_threshold_v,
            min_charge_fc * 1e-3 * gain_v_per_pc,
        )

    baseline_length = max(5, round(tau_s / dt_s))
    baseline_guard = max(edge_span * 2, round(0.04 * tau_s / dt_s))
    peak_lookback = edge_span
    peak_lookahead = max(edge_span, round(0.35 * tau_s / dt_s))

    pulses: list[Pulse] = []
    for candidate in merged_candidates:
        baseline_end = candidate - baseline_guard
        baseline_start = max(0, baseline_end - baseline_length)
        if baseline_end - baseline_start < 5:
            continue
        baseline_v = statistics.median(voltages[baseline_start:baseline_end])

        peak_start = max(0, candidate - peak_lookback)
        peak_end = min(len(voltages), candidate + peak_lookahead + 1)
        peak_index = max(
            range(peak_start, peak_end),
            key=lambda item: abs(voltages[item] - baseline_v),
        )
        amplitude_v = voltages[peak_index] - baseline_v

        if abs(amplitude_v) < amplitude_threshold_v:
            continue
        if polarity == "positive" and amplitude_v <= 0.0:
            continue
        if polarity == "negative" and amplitude_v >= 0.0:
            continue

        pulses.append(
            Pulse(
                peak_time_s=times[peak_index],
                peak_voltage_v=voltages[peak_index],
                baseline_v=baseline_v,
                amplitude_v=amplitude_v,
                charge_pc=amplitude_v / gain_v_per_pc,
            )
        )

    # Sort and remove the rare duplicate that selects the same physical peak.
    pulses.sort(key=lambda pulse: pulse.peak_time_s)
    deduplicated: list[Pulse] = []
    min_peak_spacing_s = 0.75 * tau_s
    for pulse in pulses:
        if (
            not deduplicated
            or pulse.peak_time_s - deduplicated[-1].peak_time_s > min_peak_spacing_s
        ):
            deduplicated.append(pulse)
        elif abs(pulse.amplitude_v) > abs(deduplicated[-1].amplitude_v):
            deduplicated[-1] = pulse

    return deduplicated, sample_rate_hz, voltage_noise_sigma


def iter_csv_paths(inputs: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for input_name in inputs:
        path = Path(input_name)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.csv")))
        elif path.is_file():
            paths.append(path)
        else:
            raise FileNotFoundError(f"Input path does not exist: {path}")
    # Preserve order while removing duplicate paths.
    return list(dict.fromkeys(path.resolve() for path in paths))


def write_results(path: Path, pulses: Sequence[Pulse]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "pulse",
                "peak_time_s",
                "peak_voltage_v",
                "baseline_v",
                "amplitude_v",
                "signed_charge_pc",
                "signed_charge_fc",
                "absolute_charge_fc",
                "polarity",
            ]
        )
        for number, pulse in enumerate(pulses, start=1):
            writer.writerow(
                [
                    number,
                    f"{pulse.peak_time_s:.12g}",
                    f"{pulse.peak_voltage_v:.12g}",
                    f"{pulse.baseline_v:.12g}",
                    f"{pulse.amplitude_v:.12g}",
                    f"{pulse.charge_pc:.12g}",
                    f"{pulse.charge_pc * 1000.0:.12g}",
                    f"{abs(pulse.charge_pc) * 1000.0:.12g}",
                    "positive" if pulse.amplitude_v > 0.0 else "negative",
                ]
            )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detect charge pulses using only Channel 1 of Digilent WaveForms "
            "CSV files and convert peak height with the nominal CR-110 gain."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[str(DEFAULT_INPUT)],
        help=(
            "CSV file(s) or directories containing CSV files. Default: "
            f"'{DEFAULT_INPUT}'."
        ),
    )
    parser.add_argument(
        "--gain-v-per-pc",
        type=float,
        default=DEFAULT_GAIN_V_PER_PC,
        help="Nominal CR-110 conversion gain in V/pC (default: 1.4).",
    )
    parser.add_argument(
        "--tau-us",
        type=float,
        default=DEFAULT_TAU_US,
        help="CR-110 decay time in microseconds (default: 140).",
    )
    parser.add_argument(
        "--threshold-sigma",
        type=float,
        default=8.0,
        help="Pulse detection threshold in robust noise sigma (default: 8).",
    )
    parser.add_argument(
        "--relative-threshold",
        type=float,
        default=0.05,
        help=(
            "Minimum onset size as a fraction of the strongest onset in each "
            "file (default: 0.05; use 0 to disable)."
        ),
    )
    parser.add_argument(
        "--min-charge-fc",
        type=float,
        default=None,
        help="Optional absolute minimum detected charge in fC.",
    )
    parser.add_argument(
        "--polarity",
        choices=("both", "positive", "negative"),
        default="both",
        help="Pulse polarity to report (default: both).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional directory for per-input *_charge_pulses.csv files.",
    )
    return parser


def process_file(path: Path, args: argparse.Namespace) -> int:
    times, channel_1 = read_waveforms_csv(path)
    pulses, sample_rate_hz, noise_sigma_v = detect_pulses(
        times,
        channel_1,
        gain_v_per_pc=args.gain_v_per_pc,
        tau_us=args.tau_us,
        threshold_sigma=args.threshold_sigma,
        relative_threshold=args.relative_threshold,
        min_charge_fc=args.min_charge_fc,
        polarity=args.polarity,
    )

    print(f"\nFile: {path}")
    print(
        f"Samples: {len(times)} | Sample rate: {sample_rate_hz:.6g} Hz | "
        f"Channel 1 noise estimate: {noise_sigma_v * 1e3:.4g} mV"
    )
    print(
        f"CR-110 nominal gain: {args.gain_v_per_pc:g} V/pC | "
        f"Detected pulses: {len(pulses)}"
    )
    print(
        " pulse      peak time (s)    polarity    amplitude (mV)"
        "    signed charge (fC)    |charge| (fC)"
    )
    for number, pulse in enumerate(pulses, start=1):
        pulse_polarity = "+" if pulse.amplitude_v > 0.0 else "-"
        print(
            f" {number:5d}  {pulse.peak_time_s:17.10g}"
            f"       {pulse_polarity:>1}       {pulse.amplitude_v * 1e3:14.6g}"
            f"    {pulse.charge_pc * 1000.0:18.6g}"
            f"    {abs(pulse.charge_pc) * 1000.0:13.6g}"
        )

    if args.output_dir is not None:
        output_path = args.output_dir / f"{path.stem}_charge_pulses.csv"
        write_results(output_path, pulses)
        print(f"Saved: {output_path}")
    return len(pulses)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.gain_v_per_pc <= 0.0:
        parser.error("--gain-v-per-pc must be positive.")
    if args.tau_us <= 0.0:
        parser.error("--tau-us must be positive.")
    if args.threshold_sigma <= 0.0:
        parser.error("--threshold-sigma must be positive.")
    if not 0.0 <= args.relative_threshold <= 1.0:
        parser.error("--relative-threshold must be between 0 and 1.")
    if args.min_charge_fc is not None and args.min_charge_fc < 0.0:
        parser.error("--min-charge-fc cannot be negative.")

    try:
        paths = iter_csv_paths(args.inputs)
        if not paths:
            raise FileNotFoundError("No CSV files were found in the input path(s).")
        for path in paths:
            process_file(path, args)
    except (OSError, ValueError) as error:
        print(f"DAQ error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
