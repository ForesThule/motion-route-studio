#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence
from urllib.parse import parse_qs, urlsplit


EARTH_RADIUS_METERS = 6_371_000
DEFAULT_START_SHARE = 0.2
DEFAULT_STOP_SHARE = 0.2
DEFAULT_TIMING_MODE = "duration"
DEFAULT_SPEED_KMH = 36.0
PLATFORMS = ("android", "ios")


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float


@dataclass(frozen=True)
class MotionProfile:
    start_curve: str
    stop_curve: str
    start_share: float
    stop_share: float


@dataclass(frozen=True)
class RouteSample:
    elapsed_seconds: float
    time_progress: float
    route_progress: float
    point: GeoPoint
    distance_meters: float
    speed_mps: float


@dataclass(frozen=True)
class IosSimulatorDevice:
    udid: str
    name: str
    state: str
    runtime: str


@dataclass(frozen=True)
class TimingInfo:
    mode: str
    duration_seconds: float
    average_speed_mps: float

    @property
    def average_speed_kmh(self) -> float:
        return self.average_speed_mps * 3.6


@dataclass(frozen=True)
class SpeedVariation:
    curve: str
    frequency_hz: float
    amplitude_ratio: float


@dataclass(frozen=True)
class AdvancedSpeedProfile:
    segment_speed_mps: list[float | None]
    variation: SpeedVariation | None


def parse_point(raw: str) -> GeoPoint:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"Expected LAT,LON format, received: {raw!r}"
        )

    try:
        lat = float(parts[0])
        lon = float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Point contains invalid coordinates: {raw!r}"
        ) from exc

    return validate_point(lat=lat, lon=lon)


def validate_point(lat: float, lon: float) -> GeoPoint:
    if not -90 <= lat <= 90:
        raise ValueError(f"Latitude out of range: {lat}")
    if not -180 <= lon <= 180:
        raise ValueError(f"Longitude out of range: {lon}")
    return GeoPoint(lat=lat, lon=lon)


def haversine_distance(start: GeoPoint, end: GeoPoint) -> float:
    lat1 = math.radians(start.lat)
    lat2 = math.radians(end.lat)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(end.lon - start.lon)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def lerp(start: float, end: float, progress: float) -> float:
    return start + (end - start) * progress


def curve_linear(x: float) -> float:
    return x


def curve_ease_in_quad(x: float) -> float:
    return x * x


def curve_ease_out_quad(x: float) -> float:
    return 1 - (1 - x) * (1 - x)


def curve_ease_in_out_cubic(x: float) -> float:
    if x < 0.5:
        return 4 * x * x * x
    return 1 - pow(-2 * x + 2, 3) / 2


def curve_smoothstep(x: float) -> float:
    return x * x * (3 - 2 * x)


def curve_smootherstep(x: float) -> float:
    return x * x * x * (x * (x * 6 - 15) + 10)


def curve_sine_in_out(x: float) -> float:
    return -(math.cos(math.pi * x) - 1) / 2


CURVES: dict[str, tuple[Callable[[float], float], str]] = {
    "linear": (curve_linear, "Constant speed / no easing."),
    "ease-in": (curve_ease_in_quad, "Slow start with acceleration."),
    "ease-out": (curve_ease_out_quad, "Fast start with smooth slowdown."),
    "ease-in-out": (
        curve_ease_in_out_cubic,
        "Slow start and finish with faster movement in the middle.",
    ),
    "smoothstep": (curve_smoothstep, "Balanced S-curve with soft transitions."),
    "smootherstep": (
        curve_smootherstep,
        "Smoother S-curve with gentler transitions near the ends.",
    ),
    "sine": (curve_sine_in_out, "Sinusoidal ease-in-out profile."),
}


class Route:
    def __init__(self, points: Sequence[GeoPoint]) -> None:
        if len(points) < 2:
            raise ValueError("Route requires at least two points.")

        self.points = list(points)
        self.segment_lengths = [
            haversine_distance(points[index], points[index + 1])
            for index in range(len(points) - 1)
        ]
        self.total_distance = sum(self.segment_lengths)
        self.cumulative_lengths = [0.0]

        for segment_length in self.segment_lengths:
            self.cumulative_lengths.append(self.cumulative_lengths[-1] + segment_length)

    def interpolate(self, distance_meters: float) -> GeoPoint:
        if distance_meters <= 0:
            return self.points[0]
        if distance_meters >= self.total_distance:
            return self.points[-1]

        for index, segment_length in enumerate(self.segment_lengths):
            start_distance = self.cumulative_lengths[index]
            end_distance = self.cumulative_lengths[index + 1]

            if distance_meters <= end_distance:
                if segment_length == 0:
                    return self.points[index + 1]

                segment_progress = (distance_meters - start_distance) / segment_length
                start = self.points[index]
                end = self.points[index + 1]
                return GeoPoint(
                    lat=lerp(start.lat, end.lat, segment_progress),
                    lon=lerp(start.lon, end.lon, segment_progress),
                )

        return self.points[-1]


def build_route(points: Sequence[GeoPoint]) -> Route:
    route = Route(points)
    if route.total_distance == 0:
        raise ValueError("Route distance is zero. Provide distinct coordinates.")
    return route


def resolve_timing(
    route: Route,
    duration_seconds: float | None,
    speed_mps: float | None,
) -> TimingInfo:
    if speed_mps is not None:
        if speed_mps <= 0:
            raise ValueError("Speed must be greater than zero.")
        resolved_duration = route.total_distance / speed_mps
        if resolved_duration <= 0:
            raise ValueError("Calculated duration must be greater than zero.")
        average_speed = speed_mps
        mode = "speed"
    else:
        if duration_seconds is None or duration_seconds <= 0:
            raise ValueError("Duration must be greater than zero.")
        resolved_duration = duration_seconds
        average_speed = route.total_distance / resolved_duration
        mode = "duration"

    return TimingInfo(
        mode=mode,
        duration_seconds=resolved_duration,
        average_speed_mps=average_speed,
    )


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def find_segment_index(route: Route, distance_meters: float) -> int:
    for index, end_distance in enumerate(route.cumulative_lengths[1:]):
        if distance_meters <= end_distance:
            return index
    return len(route.segment_lengths) - 1


def build_segment_speed_list(
    route: Route,
    fallback_speed_mps: float,
    overrides: Sequence[float | None] | None,
) -> list[float]:
    speeds: list[float] = []
    for index in range(len(route.segment_lengths)):
        if overrides and index < len(overrides) and overrides[index] is not None:
            speed = overrides[index]
        else:
            speed = fallback_speed_mps
        if speed is None or speed <= 0:
            raise ValueError(f"Segment speed must be greater than zero for segment {index + 1}.")
        speeds.append(speed)
    return speeds


def describe_advanced_speed_profile(profile: AdvancedSpeedProfile) -> str:
    parts: list[str] = []
    if any(speed is not None for speed in profile.segment_speed_mps):
        segment_parts = []
        for index, speed in enumerate(profile.segment_speed_mps, start=1):
            if speed is None:
                continue
            segment_parts.append(f"{index}:{speed * 3.6:.1f} km/h")
        if segment_parts:
            parts.append("segments=" + ", ".join(segment_parts))
    if profile.variation is not None:
        parts.append(
            "variation="
            f"{profile.variation.curve} "
            f"{profile.variation.frequency_hz:.2f} Hz "
            f"+/-{profile.variation.amplitude_ratio * 100:.0f}%"
        )
    return " | ".join(parts)


def periodic_curve_value(curve_name: str, phase: float) -> float:
    normalized_phase = phase % 1.0
    curve = CURVES[curve_name][0]

    if normalized_phase < 0.5:
        rising = curve(normalized_phase * 2)
        return -1 + 2 * rising

    falling = curve((normalized_phase - 0.5) * 2)
    return 1 - 2 * falling


def speed_envelope_factor(
    route_progress: float,
    profile: MotionProfile | None,
) -> float:
    if profile is None:
        return 1.0

    validated = validate_motion_profile(profile)
    progress = clamp(route_progress, 0.0, 1.0)
    factor = 1.0

    if validated.start_share > 0 and progress < validated.start_share:
        normalized = progress / validated.start_share
        factor *= CURVES[validated.start_curve][0](normalized)

    if validated.stop_share > 0 and progress > 1 - validated.stop_share:
        normalized = (progress - (1 - validated.stop_share)) / validated.stop_share
        factor *= 1 - CURVES[validated.stop_curve][0](normalized)

    return max(factor, 0.12)


def speed_variation_factor(
    elapsed_seconds: float,
    variation: SpeedVariation | None,
) -> float:
    if variation is None:
        return 1.0
    wave = periodic_curve_value(
        curve_name=variation.curve,
        phase=elapsed_seconds * variation.frequency_hz,
    )
    return max(0.05, 1 + variation.amplitude_ratio * wave)


def simulate_motion_duration(
    route: Route,
    target_duration_seconds: float,
    interval_seconds: float,
    segment_speeds_mps: Sequence[float],
    profile: MotionProfile | None,
    variation: SpeedVariation | None,
    speed_scale: float,
) -> float:
    elapsed = 0.0
    distance = 0.0
    dt = min(0.2, max(0.02, interval_seconds / 10))

    max_duration = max(target_duration_seconds * 10, 60.0)
    while distance < route.total_distance and elapsed < max_duration:
        eval_time = elapsed + dt / 2
        segment_index = find_segment_index(route, distance)
        base_speed = segment_speeds_mps[segment_index] * speed_scale
        factor = speed_envelope_factor(distance / route.total_distance, profile)
        factor *= speed_variation_factor(eval_time, variation)
        speed = max(0.05, base_speed * factor)
        remaining = route.total_distance - distance
        step_distance = min(remaining, speed * dt)
        distance += step_distance
        elapsed += dt

    if distance < route.total_distance:
        raise ValueError("Unable to resolve motion duration with the current speed profile.")
    return elapsed


def solve_speed_scale(
    route: Route,
    target_duration_seconds: float,
    interval_seconds: float,
    segment_speeds_mps: Sequence[float],
    profile: MotionProfile | None,
    variation: SpeedVariation | None,
) -> float:
    low = 0.01
    high = 1.0

    high_duration = simulate_motion_duration(
        route,
        target_duration_seconds,
        interval_seconds,
        segment_speeds_mps,
        profile,
        variation,
        high,
    )
    while high_duration > target_duration_seconds:
        high *= 2
        high_duration = simulate_motion_duration(
            route,
            target_duration_seconds,
            interval_seconds,
            segment_speeds_mps,
            profile,
            variation,
            high,
        )
        if high > 1_000:
            raise ValueError("Unable to fit target duration for the current speed profile.")

    for _ in range(24):
        mid = (low + high) / 2
        mid_duration = simulate_motion_duration(
            route,
            target_duration_seconds,
            interval_seconds,
            segment_speeds_mps,
            profile,
            variation,
            mid,
        )
        if mid_duration > target_duration_seconds:
            low = mid
        else:
            high = mid

    return high


def calculate_advanced_speed_samples(
    route: Route,
    target_duration_seconds: float,
    interval_seconds: float,
    profile: MotionProfile | None,
    advanced_profile: AdvancedSpeedProfile,
    fallback_speed_mps: float,
) -> tuple[list[RouteSample], TimingInfo]:
    has_segment_overrides = any(speed is not None for speed in advanced_profile.segment_speed_mps)
    segment_speeds_mps: list[float] = []
    effective_target_duration = target_duration_seconds
    speed_scale = 1.0

    if has_segment_overrides:
        fixed_time = 0.0
        remaining_distance = 0.0
        unresolved_segments: list[int] = []
        for index, segment_length in enumerate(route.segment_lengths):
            override = (
                advanced_profile.segment_speed_mps[index]
                if index < len(advanced_profile.segment_speed_mps)
                else None
            )
            if override is None:
                unresolved_segments.append(index)
                remaining_distance += segment_length
                segment_speeds_mps.append(0.0)
            else:
                if override <= 0:
                    raise ValueError(f"Segment speed must be greater than zero for segment {index + 1}.")
                segment_speeds_mps.append(override)
                fixed_time += segment_length / override

        if unresolved_segments:
            remaining_time = target_duration_seconds - fixed_time
            if remaining_time <= 0:
                raise ValueError(
                    "Segment speed overrides leave no time for the remaining route. "
                    "Increase duration or lower some segment speeds."
                )
            fallback_for_unset_segments = remaining_distance / remaining_time
            if fallback_for_unset_segments <= 0:
                raise ValueError("Calculated speed for unspecified segments must be greater than zero.")
            for index in unresolved_segments:
                segment_speeds_mps[index] = fallback_for_unset_segments
        else:
            effective_target_duration = sum(
                segment_length / speed
                for segment_length, speed in zip(route.segment_lengths, segment_speeds_mps)
            )
    else:
        segment_speeds_mps = [fallback_speed_mps] * len(route.segment_lengths)
        speed_scale = solve_speed_scale(
            route=route,
            target_duration_seconds=target_duration_seconds,
            interval_seconds=interval_seconds,
            segment_speeds_mps=segment_speeds_mps,
            profile=profile,
            variation=advanced_profile.variation,
        )

    samples: list[RouteSample] = []
    elapsed = 0.0
    distance = 0.0
    next_sample_at = 0.0
    previous_sample_distance = 0.0
    previous_sample_elapsed = 0.0
    dt = min(0.2, max(0.02, interval_seconds / 10))

    while distance < route.total_distance:
        while next_sample_at <= elapsed + 1e-9:
            point = route.interpolate(distance)
            if not samples:
                speed = 0.0
            else:
                delta_distance = distance - previous_sample_distance
                delta_time = max(elapsed - previous_sample_elapsed, 1e-9)
                speed = delta_distance / delta_time
            samples.append(
                RouteSample(
                    elapsed_seconds=elapsed,
                    time_progress=clamp(elapsed / effective_target_duration, 0.0, 1.0),
                    route_progress=distance / route.total_distance,
                    point=point,
                    distance_meters=distance,
                    speed_mps=speed,
                )
            )
            previous_sample_distance = distance
            previous_sample_elapsed = elapsed
            next_sample_at += interval_seconds

        eval_time = elapsed + dt / 2
        segment_index = find_segment_index(route, distance)
        base_speed = segment_speeds_mps[segment_index] * speed_scale
        factor = speed_envelope_factor(distance / route.total_distance, profile)
        factor *= speed_variation_factor(eval_time, advanced_profile.variation)
        current_speed = max(0.05, base_speed * factor)
        remaining = route.total_distance - distance
        step_distance = min(remaining, current_speed * dt)
        distance += step_distance
        elapsed += dt

    if not samples or samples[-1].distance_meters < route.total_distance:
        final_elapsed = elapsed
        final_point = route.interpolate(route.total_distance)
        if not samples:
            final_speed = 0.0
        else:
            delta_distance = route.total_distance - previous_sample_distance
            delta_time = max(final_elapsed - previous_sample_elapsed, 1e-9)
            final_speed = delta_distance / delta_time
        samples.append(
            RouteSample(
                elapsed_seconds=final_elapsed,
                time_progress=1.0,
                route_progress=1.0,
                point=final_point,
                distance_meters=route.total_distance,
                speed_mps=final_speed,
            )
        )

    actual_duration = samples[-1].elapsed_seconds
    timing = TimingInfo(
        mode="speed-profile",
        duration_seconds=actual_duration,
        average_speed_mps=route.total_distance / actual_duration,
    )
    return samples, timing


def validate_motion_profile(profile: MotionProfile) -> MotionProfile:
    if profile.start_curve not in CURVES:
        raise ValueError(f"Unknown start curve: {profile.start_curve}")
    if profile.stop_curve not in CURVES:
        raise ValueError(f"Unknown stop curve: {profile.stop_curve}")
    if not 0 <= profile.start_share < 1:
        raise ValueError("Start share must be in the range [0, 1).")
    if not 0 <= profile.stop_share < 1:
        raise ValueError("Stop share must be in the range [0, 1).")
    if profile.start_share + profile.stop_share >= 1:
        raise ValueError("Start share plus stop share must be less than 1.")
    return profile


def calculate_piecewise_progress(time_progress: float, profile: MotionProfile) -> float:
    profile = validate_motion_profile(profile)
    x = clamp(time_progress, 0.0, 1.0)

    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0

    start_share = profile.start_share
    stop_share = profile.stop_share
    cruise_start = start_share
    cruise_end = 1 - stop_share

    if start_share > 0 and x < cruise_start:
        normalized = x / start_share
        return start_share * CURVES[profile.start_curve][0](normalized)

    if stop_share > 0 and x > cruise_end:
        normalized = (x - cruise_end) / stop_share
        stop_progress = CURVES[profile.stop_curve][0](normalized)
        return cruise_end + stop_share * stop_progress

    if cruise_end == cruise_start:
        return x

    cruise_progress = (x - cruise_start) / (cruise_end - cruise_start)
    return cruise_start + cruise_progress * (cruise_end - cruise_start)


def resolve_progress_function(
    legacy_curve_name: str | None,
    profile: MotionProfile | None,
) -> tuple[Callable[[float], float], str]:
    if profile is not None:
        validated = validate_motion_profile(profile)
        description = (
            f"start={validated.start_curve} ({validated.start_share * 100:.0f}%), "
            f"stop={validated.stop_curve} ({validated.stop_share * 100:.0f}%)"
        )

        def piecewise(progress: float) -> float:
            return calculate_piecewise_progress(progress, validated)

        return piecewise, description

    selected_curve = legacy_curve_name or "ease-in-out"
    if selected_curve not in CURVES:
        raise ValueError(f"Unknown curve: {selected_curve}")
    return CURVES[selected_curve][0], selected_curve


def calculate_samples(
    route: Route,
    duration_seconds: float,
    interval_seconds: float,
    legacy_curve_name: str | None = None,
    profile: MotionProfile | None = None,
) -> tuple[list[RouteSample], str]:
    progress_function, profile_label = resolve_progress_function(
        legacy_curve_name=legacy_curve_name,
        profile=profile,
    )
    samples: list[RouteSample] = []
    steps = max(1, math.ceil(duration_seconds / interval_seconds))
    previous_distance = 0.0
    previous_elapsed = 0.0

    for step in range(steps + 1):
        elapsed = min(duration_seconds, step * interval_seconds)
        time_progress = elapsed / duration_seconds
        route_progress = clamp(progress_function(time_progress), 0.0, 1.0)
        distance = route.total_distance * route_progress
        point = route.interpolate(distance)

        if not samples:
            speed = 0.0
        else:
            delta_distance = distance - previous_distance
            delta_time = elapsed - previous_elapsed
            speed = 0.0 if delta_time == 0 else delta_distance / delta_time

        samples.append(
            RouteSample(
                elapsed_seconds=elapsed,
                time_progress=time_progress,
                route_progress=route_progress,
                point=point,
                distance_meters=distance,
                speed_mps=speed,
            )
        )
        previous_distance = distance
        previous_elapsed = elapsed

    if samples[-1].elapsed_seconds < duration_seconds:
        final_point = route.interpolate(route.total_distance)
        final_elapsed = duration_seconds
        delta_distance = route.total_distance - previous_distance
        delta_time = final_elapsed - previous_elapsed
        speed = 0.0 if delta_time == 0 else delta_distance / delta_time
        samples.append(
            RouteSample(
                elapsed_seconds=final_elapsed,
                time_progress=1.0,
                route_progress=1.0,
                point=final_point,
                distance_meters=route.total_distance,
                speed_mps=speed,
            )
        )

    return samples, profile_label


def resolve_emulator_serial(adb_path: str, serial: str | None) -> str:
    if serial:
        return serial

    emulators = list_running_emulators(adb_path)
    if not emulators:
        raise RuntimeError("No running Android Emulator instances found.")
    if len(emulators) > 1:
        raise RuntimeError(
            "Multiple Android Emulator instances found. Pass --serial explicitly."
        )
    return emulators[0]


def list_running_emulators(adb_path: str) -> list[str]:
    result = subprocess.run(
        [adb_path, "devices"],
        capture_output=True,
        text=True,
        check=True,
    )

    emulators = []
    for line in result.stdout.splitlines():
        if "\tdevice" not in line:
            continue
        candidate = line.split("\t", 1)[0]
        if candidate.startswith("emulator-"):
            emulators.append(candidate)

    return emulators


def list_booted_ios_simulators() -> list[IosSimulatorDevice]:
    result = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "available", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    devices_by_runtime = payload.get("devices", {})
    simulators: list[IosSimulatorDevice] = []

    for runtime, devices in devices_by_runtime.items():
        if not runtime.startswith("com.apple.CoreSimulator.SimRuntime.iOS-"):
            continue
        if not isinstance(devices, list):
            continue

        runtime_name = runtime.split("SimRuntime.", 1)[-1].replace("-", " ")
        for device in devices:
            if not isinstance(device, dict):
                continue
            if device.get("state") != "Booted":
                continue
            udid = str(device.get("udid") or "").strip()
            name = str(device.get("name") or "").strip()
            state = str(device.get("state") or "").strip()
            if not udid or not name:
                continue
            simulators.append(
                IosSimulatorDevice(
                    udid=udid,
                    name=name,
                    state=state,
                    runtime=runtime_name,
                )
            )

    return simulators


def resolve_ios_simulator_udid(target_id: str | None) -> str:
    if target_id:
        return target_id

    simulators = list_booted_ios_simulators()
    if not simulators:
        raise RuntimeError("No booted iOS Simulator instances found.")
    if len(simulators) > 1:
        raise RuntimeError("Multiple booted iOS Simulators found. Pass --device-id explicitly.")
    return simulators[0].udid


def send_geo_fix(
    adb_path: str,
    serial: str,
    point: GeoPoint,
    altitude: float | None,
) -> None:
    command = [
        adb_path,
        "-s",
        serial,
        "emu",
        "geo",
        "fix",
        f"{point.lon:.6f}",
        f"{point.lat:.6f}",
    ]
    if altitude is not None:
        command.append(f"{altitude:.1f}")

    subprocess.run(command, check=True)


def send_ios_location_fix(
    device_id: str,
    point: GeoPoint,
) -> None:
    subprocess.run(
        [
            "xcrun",
            "simctl",
            "location",
            device_id,
            "set",
            f"{point.lat:.6f},{point.lon:.6f}",
        ],
        check=True,
    )


def format_point(point: GeoPoint) -> str:
    return f"{point.lat:.6f},{point.lon:.6f}"


def print_curve_table() -> None:
    print("Available curves:")
    for name, (_, description) in CURVES.items():
        print(f"  {name:<14} {description}")


def print_sample_table(
    samples: Iterable[RouteSample],
    log_fn: Callable[[str], None] = print,
) -> None:
    log_fn(" index  time(s)  route(%)  dist(m)  speed(m/s)  coordinate")
    for index, sample in enumerate(samples):
        log_fn(
            f" {index:>5}  "
            f"{sample.elapsed_seconds:>7.2f}  "
            f"{sample.route_progress * 100:>7.2f}  "
            f"{sample.distance_meters:>7.1f}  "
            f"{sample.speed_mps:>10.2f}  "
            f"{format_point(sample.point)}"
        )


def describe_run(
    route: Route,
    samples: Sequence[RouteSample],
    curve_label: str,
    timing: TimingInfo,
    interval: float,
    log_fn: Callable[[str], None] = print,
) -> None:
    log_fn(f"Route points: {len(route.points)}")
    log_fn(f"Route distance: {route.total_distance:.1f} m")
    log_fn(f"Speed profile: {curve_label}")
    log_fn(f"Timing mode: {timing.mode}")
    log_fn(f"Duration: {timing.duration_seconds:.2f} s")
    log_fn(
        f"Average speed: {timing.average_speed_mps:.2f} m/s ({timing.average_speed_kmh:.2f} km/h)"
    )
    log_fn(f"Interval: {interval:.2f} s")
    log_fn(f"Generated samples: {len(samples)}")


def run_motion_loop(
    samples: Sequence[RouteSample],
    send_point: Callable[[GeoPoint], None],
    log_fn: Callable[[str], None] = print,
    stop_event: threading.Event | None = None,
) -> None:
    start_time = time.perf_counter()

    for index, sample in enumerate(samples):
        if stop_event and stop_event.is_set():
            log_fn("Motion cancelled.")
            return

        target_time = start_time + sample.elapsed_seconds
        remaining = target_time - time.perf_counter()
        if remaining > 0:
            if stop_event and stop_event.wait(remaining):
                log_fn("Motion cancelled.")
                return
            if not stop_event:
                time.sleep(remaining)

        send_point(sample.point)
        log_fn(
            f"[{index + 1}/{len(samples)}] "
            f"t={sample.elapsed_seconds:6.2f}s "
            f"speed={sample.speed_mps:7.2f} m/s "
            f"point={format_point(sample.point)}"
        )


def run_android_motion(
    adb_path: str,
    serial: str,
    samples: Sequence[RouteSample],
    altitude: float | None,
    log_fn: Callable[[str], None] = print,
    stop_event: threading.Event | None = None,
) -> None:
    def send_point(point: GeoPoint) -> None:
        send_geo_fix(adb_path, serial, point, altitude)

    run_motion_loop(samples=samples, send_point=send_point, log_fn=log_fn, stop_event=stop_event)


def run_ios_motion(
    device_id: str,
    samples: Sequence[RouteSample],
    log_fn: Callable[[str], None] = print,
    stop_event: threading.Event | None = None,
) -> None:
    def send_point(point: GeoPoint) -> None:
        send_ios_location_fix(device_id, point)

    run_motion_loop(samples=samples, send_point=send_point, log_fn=log_fn, stop_event=stop_event)


def parse_motion_profile_from_args(args: argparse.Namespace) -> MotionProfile | None:
    if args.start_curve or args.stop_curve:
        start_curve = args.start_curve or "ease-in"
        stop_curve = args.stop_curve or "ease-out"
        return validate_motion_profile(
            MotionProfile(
                start_curve=start_curve,
                stop_curve=stop_curve,
                start_share=args.start_share,
                stop_share=args.stop_share,
            )
        )
    return None


def parse_advanced_speed_profile_from_args(
    args: argparse.Namespace,
    segment_count: int,
) -> AdvancedSpeedProfile | None:
    segment_speeds: list[float | None] = [None] * segment_count
    for segment_index, speed_kmh in args.segment_speed:
        segment_speeds[segment_index - 1] = speed_kmh / 3.6

    variation = None
    if args.variation_curve:
        variation = SpeedVariation(
            curve=args.variation_curve,
            frequency_hz=args.variation_frequency,
            amplitude_ratio=args.variation_amplitude / 100,
        )

    if any(speed is not None for speed in segment_speeds) or variation is not None:
        return AdvancedSpeedProfile(
            segment_speed_mps=segment_speeds,
            variation=variation,
        )
    return None


def build_samples_from_options(
    points: Sequence[GeoPoint],
    duration: float | None,
    interval: float,
    legacy_curve_name: str | None = None,
    profile: MotionProfile | None = None,
    speed_mps: float | None = None,
    advanced_profile: AdvancedSpeedProfile | None = None,
) -> tuple[Route, list[RouteSample], str, TimingInfo]:
    route = build_route(points)
    timing = resolve_timing(route, duration_seconds=duration, speed_mps=speed_mps)
    advanced_enabled = advanced_profile is not None and (
        any(speed is not None for speed in advanced_profile.segment_speed_mps)
        or advanced_profile.variation is not None
    )
    if advanced_enabled:
        samples, timing = calculate_advanced_speed_samples(
            route=route,
            target_duration_seconds=timing.duration_seconds,
            interval_seconds=interval,
            profile=profile,
            advanced_profile=advanced_profile,
            fallback_speed_mps=timing.average_speed_mps,
        )
        curve_label = describe_advanced_speed_profile(advanced_profile)
        if profile is not None:
            base_label = (
                f"start={profile.start_curve} ({profile.start_share * 100:.0f}%), "
                f"stop={profile.stop_curve} ({profile.stop_share * 100:.0f}%)"
            )
            curve_label = f"{base_label} | {curve_label}" if curve_label else base_label
    else:
        samples, curve_label = calculate_samples(
            route=route,
            duration_seconds=timing.duration_seconds,
            interval_seconds=interval,
            legacy_curve_name=legacy_curve_name,
            profile=profile,
        )
    return route, samples, curve_label, timing


def collect_preview_lines(
    route: Route,
    samples: Sequence[RouteSample],
    curve_label: str,
    timing: TimingInfo,
    interval: float,
) -> list[str]:
    lines: list[str] = []
    describe_run(route, samples, curve_label, timing, interval, lines.append)
    print_sample_table(samples, lines.append)
    return lines


def parse_segment_speed_overrides(
    raw_values: object,
    segment_count: int,
) -> list[float | None]:
    if raw_values is None:
        return [None] * segment_count
    if not isinstance(raw_values, list):
        raise ValueError("Segment speeds must be an array.")

    overrides: list[float | None] = []
    for index in range(segment_count):
        raw = raw_values[index] if index < len(raw_values) else None
        if raw in (None, ""):
            overrides.append(None)
            continue
        try:
            speed_kmh = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Segment speed must be numeric for segment {index + 1}.") from exc
        if speed_kmh <= 0:
            raise ValueError(f"Segment speed must be greater than zero for segment {index + 1}.")
        overrides.append(speed_kmh / 3.6)
    return overrides


def parse_variation_from_payload(payload: dict[str, object]) -> SpeedVariation | None:
    enabled = bool(payload.get("variationEnabled"))
    amplitude_raw = payload.get("variationAmplitudePercent", 0)
    frequency_raw = payload.get("variationFrequencyHz", 0)
    curve_name = str(payload.get("variationCurve") or "sine").strip()

    try:
        amplitude_percent = float(amplitude_raw)
        frequency_hz = float(frequency_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Variation amplitude and frequency must be numeric.") from exc

    if not enabled and amplitude_percent <= 0:
        return None
    if curve_name not in CURVES:
        raise ValueError(f"Unknown variation curve: {curve_name}")
    if amplitude_percent <= 0:
        raise ValueError("Variation amplitude must be greater than zero.")
    if frequency_hz <= 0:
        raise ValueError("Variation frequency must be greater than zero.")
    if amplitude_percent >= 95:
        raise ValueError("Variation amplitude must be lower than 95%.")

    return SpeedVariation(
        curve=curve_name,
        frequency_hz=frequency_hz,
        amplitude_ratio=amplitude_percent / 100,
    )


def parse_options_payload(
    payload: dict[str, object],
) -> tuple[
    str,
    list[GeoPoint],
    float | None,
    float,
    str,
    str | None,
    float | None,
    MotionProfile,
    float | None,
    AdvancedSpeedProfile | None,
]:
    raw_points = payload.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise ValueError("Add at least two route points.")

    points: list[GeoPoint] = []
    for raw_point in raw_points:
        if not isinstance(raw_point, dict):
            raise ValueError("Each point must be an object with lat/lon.")
        try:
            lat = float(raw_point["lat"])
            lon = float(raw_point["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Each point must contain numeric lat/lon values.") from exc
        points.append(validate_point(lat=lat, lon=lon))
    segment_count = max(0, len(points) - 1)

    timing_mode = str(payload.get("timingMode") or DEFAULT_TIMING_MODE).strip().lower()
    if timing_mode not in {"duration", "speed"}:
        raise ValueError(f"Unsupported timing mode: {timing_mode}")

    try:
        interval = float(payload.get("interval", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("Interval must be a number.") from exc

    if interval <= 0:
        raise ValueError("Interval must be greater than zero.")

    duration: float | None = None
    speed_mps: float | None = None
    if timing_mode == "duration":
        try:
            duration = float(payload.get("duration", 60))
        except (TypeError, ValueError) as exc:
            raise ValueError("Duration must be a number.") from exc
        if duration <= 0:
            raise ValueError("Duration must be greater than zero.")
    else:
        speed_mps_raw = payload.get("speedMps")
        speed_kmh_raw = payload.get("speedKmh", DEFAULT_SPEED_KMH)
        try:
            if speed_mps_raw not in (None, ""):
                speed_mps = float(speed_mps_raw)
            else:
                speed_mps = float(speed_kmh_raw) / 3.6
        except (TypeError, ValueError) as exc:
            raise ValueError("Speed must be a number.") from exc
        if speed_mps <= 0:
            raise ValueError("Speed must be greater than zero.")

    platform = str(payload.get("platform") or "android").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform}")

    adb_path = str(payload.get("adbPath") or "adb").strip() or "adb"
    target_raw = payload.get("targetId")
    target_id = None if target_raw in (None, "") else str(target_raw).strip() or None

    altitude_raw = payload.get("altitude")
    if altitude_raw in (None, ""):
        altitude = None
    else:
        try:
            altitude = float(altitude_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Altitude must be a number.") from exc

    profile = validate_motion_profile(
        MotionProfile(
            start_curve=str(payload.get("startCurve") or "ease-in"),
            stop_curve=str(payload.get("stopCurve") or "ease-out"),
            start_share=float(payload.get("startShare", DEFAULT_START_SHARE)),
            stop_share=float(payload.get("stopShare", DEFAULT_STOP_SHARE)),
        )
    )

    segment_speeds = parse_segment_speed_overrides(
        payload.get("segmentSpeedsKmh"),
        segment_count=segment_count,
    )
    variation = parse_variation_from_payload(payload)
    advanced_profile = None
    if any(speed is not None for speed in segment_speeds) or variation is not None:
        advanced_profile = AdvancedSpeedProfile(
            segment_speed_mps=segment_speeds,
            variation=variation,
        )

    return (
        platform,
        points,
        duration,
        interval,
        adb_path,
        target_id,
        altitude,
        profile,
        speed_mps,
        advanced_profile,
    )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    def parse_segment_speed(raw: str) -> tuple[int, float]:
        try:
            segment_index_raw, speed_raw = raw.split(":", 1)
            segment_index = int(segment_index_raw)
            speed_kmh = float(speed_raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "Expected SEGMENT:SPEED_KMH format, for example 2:35"
            ) from exc
        if segment_index <= 0:
            raise argparse.ArgumentTypeError("Segment index must be 1 or greater.")
        if speed_kmh <= 0:
            raise argparse.ArgumentTypeError("Segment speed must be greater than zero.")
        return segment_index, speed_kmh

    parser = argparse.ArgumentParser(
        description=(
            "Emulate GPS movement in Android Emulator with configurable speed curves."
        )
    )
    parser.add_argument(
        "--platform",
        choices=PLATFORMS,
        default="android",
        help="Target platform: android or ios. Default: android",
    )
    parser.add_argument(
        "--point",
        dest="points",
        metavar="LAT,LON",
        type=parse_point,
        action="append",
        help="Route point. Provide at least two points; can be repeated.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="Total duration of the route in seconds. Default: 60",
    )
    parser.add_argument(
        "--speed-mps",
        type=float,
        help="Average route speed in meters per second. Overrides duration.",
    )
    parser.add_argument(
        "--speed-kmh",
        type=float,
        help="Average route speed in kilometers per hour. Overrides duration.",
    )
    parser.add_argument(
        "--segment-speed",
        action="append",
        type=parse_segment_speed,
        default=[],
        help="Override speed on a specific route segment using SEGMENT:SPEED_KMH (1-based).",
    )
    parser.add_argument(
        "--variation-curve",
        choices=sorted(CURVES.keys()),
        help="Periodic curve for global speed variation.",
    )
    parser.add_argument(
        "--variation-frequency",
        type=float,
        help="Frequency in Hz for periodic speed variation.",
    )
    parser.add_argument(
        "--variation-amplitude",
        type=float,
        help="Amplitude in percent for periodic speed variation, for example 20 for +/-20%.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Update interval in seconds. Default: 1",
    )
    parser.add_argument(
        "--curve",
        choices=sorted(CURVES.keys()),
        help="Legacy single curve applied over the full route.",
    )
    parser.add_argument(
        "--start-curve",
        choices=sorted(CURVES.keys()),
        help="Curve used for the acceleration phase.",
    )
    parser.add_argument(
        "--stop-curve",
        choices=sorted(CURVES.keys()),
        help="Curve used for the deceleration phase.",
    )
    parser.add_argument(
        "--start-share",
        type=float,
        default=DEFAULT_START_SHARE,
        help="Fraction of total time reserved for acceleration. Default: 0.2",
    )
    parser.add_argument(
        "--stop-share",
        type=float,
        default=DEFAULT_STOP_SHARE,
        help="Fraction of total time reserved for deceleration. Default: 0.2",
    )
    parser.add_argument(
        "--serial",
        help="Specific Android Emulator serial, for example emulator-5554. Android only.",
    )
    parser.add_argument(
        "--device-id",
        help="Target device identifier. For iOS use Simulator UDID; for Android this can also be an emulator serial.",
    )
    parser.add_argument(
        "--adb-path",
        default="adb",
        help="Path to adb executable. Default: adb",
    )
    parser.add_argument(
        "--altitude",
        type=float,
        help="Optional altitude in meters passed to geo fix.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not send anything to adb; print the generated route samples instead.",
    )
    parser.add_argument(
        "--list-curves",
        action="store_true",
        help="Print supported curves and exit.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch a local web UI for selecting points and motion curves.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for the local web UI. Default: 8765",
    )
    args = parser.parse_args(argv)

    if args.list_curves or args.gui:
        return args

    if not args.points or len(args.points) < 2:
        parser.error("Provide at least two --point values.")
    if args.interval <= 0:
        parser.error("--interval must be greater than zero.")
    if args.speed_mps is not None and args.speed_kmh is not None:
        parser.error("Use only one of --speed-mps or --speed-kmh.")
    if args.speed_mps is None and args.speed_kmh is None and args.duration <= 0:
        parser.error("--duration must be greater than zero.")
    if args.speed_mps is not None and args.speed_mps <= 0:
        parser.error("--speed-mps must be greater than zero.")
    if args.speed_kmh is not None and args.speed_kmh <= 0:
        parser.error("--speed-kmh must be greater than zero.")
    if args.variation_curve and args.variation_frequency is None:
        parser.error("--variation-frequency is required when --variation-curve is used.")
    if args.variation_curve and args.variation_amplitude is None:
        parser.error("--variation-amplitude is required when --variation-curve is used.")
    if args.variation_frequency is not None and args.variation_frequency <= 0:
        parser.error("--variation-frequency must be greater than zero.")
    if args.variation_amplitude is not None and args.variation_amplitude <= 0:
        parser.error("--variation-amplitude must be greater than zero.")
    if args.variation_amplitude is not None and args.variation_amplitude >= 95:
        parser.error("--variation-amplitude must be below 95.")
    if args.segment_speed:
        segment_count = len(args.points) - 1
        for segment_index, _speed_kmh in args.segment_speed:
            if segment_index > segment_count:
                parser.error(
                    f"--segment-speed segment index {segment_index} exceeds route segment count {segment_count}."
                )

    try:
        parse_motion_profile_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    return args


class MotionWebState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.logs: list[str] = []
        self.worker_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def is_running(self) -> bool:
        with self.lock:
            return self.worker_thread is not None and self.worker_thread.is_alive()

    def clear_logs(self) -> None:
        with self.lock:
            self.logs.clear()

    def append_log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)

    def snapshot_logs(self, since: int) -> tuple[list[str], int, bool]:
        with self.lock:
            logs = self.logs[since:]
            next_cursor = len(self.logs)
            running = self.worker_thread is not None and self.worker_thread.is_alive()
        return logs, next_cursor, running

    def preview(self, payload: dict[str, object]) -> dict[str, object]:
        (
            _platform,
            points,
            duration,
            interval,
            _adb_path,
            _target_id,
            _altitude,
            profile,
            speed_mps,
            advanced_profile,
        ) = parse_options_payload(payload)
        route, samples, curve_label, timing = build_samples_from_options(
            points=points,
            duration=duration,
            interval=interval,
            profile=profile,
            speed_mps=speed_mps,
            advanced_profile=advanced_profile,
        )
        return {
            "lines": collect_preview_lines(route, samples, curve_label, timing, interval),
            "running": self.is_running(),
        }

    def list_devices(self, payload: dict[str, object]) -> dict[str, object]:
        platform = str(payload.get("platform") or "android").strip().lower()
        if platform not in PLATFORMS:
            raise ValueError(f"Unsupported platform: {platform}")

        if platform == "android":
            adb_path = str(payload.get("adbPath") or "adb").strip() or "adb"
            devices = [
                {"id": emulator, "label": emulator, "state": "device"}
                for emulator in list_running_emulators(adb_path)
            ]
        else:
            devices = [
                {
                    "id": simulator.udid,
                    "label": f"{simulator.name} ({simulator.runtime})",
                    "state": simulator.state,
                }
                for simulator in list_booted_ios_simulators()
            ]

        return {
            "devices": devices,
            "running": self.is_running(),
        }

    def list_emulators(self, payload: dict[str, object]) -> dict[str, object]:
        adb_path = str(payload.get("adbPath") or "adb").strip() or "adb"
        emulators = list_running_emulators(adb_path)
        return {
            "emulators": emulators,
            "running": self.is_running(),
        }

    def start_run(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            if self.worker_thread is not None and self.worker_thread.is_alive():
                raise ValueError("A run is already in progress.")

        (
            platform,
            points,
            duration,
            interval,
            adb_path,
            target_id,
            altitude,
            profile,
            speed_mps,
            advanced_profile,
        ) = parse_options_payload(payload)
        route, samples, curve_label, timing = build_samples_from_options(
            points=points,
            duration=duration,
            interval=interval,
            profile=profile,
            speed_mps=speed_mps,
            advanced_profile=advanced_profile,
        )

        self.stop_event = threading.Event()
        initial_lines = collect_preview_lines(route, samples, curve_label, timing, interval)
        with self.lock:
            self.logs = list(initial_lines)

        def worker() -> None:
            try:
                if platform == "android":
                    selected_target = resolve_emulator_serial(
                        adb_path,
                        target_id,
                    )
                    self.append_log(f"Using Android emulator: {selected_target}")
                    run_android_motion(
                        adb_path=adb_path,
                        serial=selected_target,
                        samples=samples,
                        altitude=altitude,
                        log_fn=self.append_log,
                        stop_event=self.stop_event,
                    )
                else:
                    selected_target = resolve_ios_simulator_udid(target_id)
                    self.append_log(f"Using iOS simulator: {selected_target}")
                    if altitude is not None:
                        self.append_log("Altitude is ignored by iOS Simulator location set.")
                    run_ios_motion(
                        device_id=selected_target,
                        samples=samples,
                        log_fn=self.append_log,
                        stop_event=self.stop_event,
                    )
                if not self.stop_event.is_set():
                    self.append_log("Motion finished.")
            except Exception as exc:
                self.append_log(f"Error: {exc}")
            finally:
                with self.lock:
                    self.worker_thread = None

        worker_thread = threading.Thread(target=worker, daemon=True)
        with self.lock:
            self.worker_thread = worker_thread
        worker_thread.start()
        return {"started": True, "running": True}

    def stop_run(self, _payload: dict[str, object] | None = None) -> dict[str, object]:
        with self.lock:
            running = self.worker_thread is not None and self.worker_thread.is_alive()
        if running:
            self.stop_event.set()
            self.append_log("Stop requested.")
        return {"stopping": running, "running": running}


def build_web_ui_html() -> str:
    curves = sorted(CURVES.keys())
    config = {
        "curves": curves,
        "defaults": {
            "points": [
                {"lat": 37.4219999, "lon": -122.0840575},
                {"lat": 37.4225, "lon": -122.0835},
            ],
            "platform": "android",
            "mapLanguage": "ru",
            "timingMode": DEFAULT_TIMING_MODE,
            "duration": 60,
            "speedKmh": DEFAULT_SPEED_KMH,
            "segmentSpeedsKmh": [],
            "variationEnabled": False,
            "variationCurve": "sine",
            "variationFrequencyHz": 0.2,
            "variationAmplitudePercent": 20,
            "interval": 1,
            "adbPath": "adb",
            "targetId": "",
            "altitude": "",
            "startCurve": "ease-in",
            "stopCurve": "ease-out",
            "startShare": DEFAULT_START_SHARE,
            "stopShare": DEFAULT_STOP_SHARE,
        },
    }
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Motion Route Studio</title>
  <link href="https://unpkg.com/maplibre-gl@5.16.0/dist/maplibre-gl.css" rel="stylesheet">
  <style>
    :root {{
      --bg: #f6f0e6;
      --panel: rgba(255, 251, 245, 0.92);
      --panel-strong: #fffdf8;
      --line: #dbc7aa;
      --line-strong: #c6ab87;
      --ink: #2a231b;
      --muted: #6e6458;
      --accent: #b55434;
      --accent-dark: #7b3823;
      --accent-soft: rgba(181, 84, 52, 0.14);
      --soft: #efe1cb;
      --soft-2: #f8f1e4;
      --success: #2f7a52;
      --shadow: 0 20px 40px rgba(73, 45, 16, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(181, 84, 52, 0.2), transparent 28%),
        radial-gradient(circle at bottom left, rgba(102, 58, 32, 0.08), transparent 34%),
        linear-gradient(180deg, #fbf7f0 0%, #efe2cd 100%);
    }}
    .page {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 24px;
    }}
    .hero {{
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      gap: 20px;
      margin-bottom: 20px;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 10px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--accent-dark);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(30px, 4vw, 42px);
      line-height: 1.05;
    }}
    .subtitle {{
      max-width: 760px;
      margin: 0;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.55;
    }}
    .hero-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: flex-end;
    }}
    .workspace {{
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) 360px;
      gap: 20px;
      align-items: start;
    }}
    .bottom-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 20px;
      margin-top: 20px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 20px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }}
    .panel h2 {{
      margin: 0;
      font-size: 20px;
    }}
    .panel-heading {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .panel-heading p,
    .section-copy {{
      margin: 6px 0 0;
      color: var(--muted);
      line-height: 1.5;
      font-size: 13px;
    }}
    .status-group {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }}
    label {{
      display: block;
      margin-bottom: 6px;
      font-size: 12px;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    input, select, textarea, button {{
      font: inherit;
    }}
    input, select, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      background: rgba(255, 255, 255, 0.92);
      color: var(--ink);
      transition: border-color 120ms ease, box-shadow 120ms ease, background 120ms ease;
    }}
    input:focus,
    select:focus,
    textarea:focus {{
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 4px rgba(181, 84, 52, 0.1);
      background: white;
    }}
    textarea {{
      min-height: 260px;
      resize: vertical;
      font-family: "SFMono-Regular", "Menlo", monospace;
      font-size: 12px;
      line-height: 1.5;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 11px 16px;
      background: var(--soft);
      color: var(--ink);
      cursor: pointer;
      transition: transform 120ms ease, background 120ms ease, box-shadow 120ms ease;
      box-shadow: 0 8px 18px rgba(73, 45, 16, 0.08);
    }}
    button:hover {{
      transform: translateY(-1px);
      background: #e8d6bc;
    }}
    button.primary {{
      background: linear-gradient(135deg, var(--accent) 0%, #cf734c 100%);
      color: white;
    }}
    button.primary:hover {{
      background: linear-gradient(135deg, #a84b2b 0%, #c76841 100%);
    }}
    button.warn {{
      background: #d7a06b;
      color: white;
    }}
    button.ghost {{
      background: rgba(255, 255, 255, 0.76);
      border: 1px solid var(--line);
      box-shadow: none;
    }}
    button.small {{
      padding: 9px 13px;
      font-size: 13px;
    }}
    .toolbar-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .field-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }}
    .field-grid.compact-2 {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .field-grid.compact-4 {{
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }}
    .field-grid.compact-6 {{
      grid-template-columns: repeat(6, minmax(0, 1fr));
    }}
    .button-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .button-row.spread {{
      justify-content: space-between;
      align-items: center;
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.85);
      color: var(--accent-dark);
      font-size: 13px;
      border: 1px solid rgba(198, 171, 135, 0.75);
      white-space: nowrap;
    }}
    .status.warn {{
      background: #f6e6d4;
    }}
    .status.live {{
      background: rgba(47, 122, 82, 0.14);
      color: var(--success);
      border-color: rgba(47, 122, 82, 0.24);
    }}
    .map-shell {{
      position: relative;
    }}
    #mapCanvas {{
      height: 540px;
      border-radius: 20px;
      overflow: hidden;
      background:
        linear-gradient(135deg, rgba(181, 84, 52, 0.14), rgba(123, 56, 35, 0.03)),
        var(--soft-2);
      border: 1px solid var(--line);
    }}
    .map-footer {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-top: 14px;
    }}
    .inline-note {{
      font-size: 13px;
      color: var(--muted);
      line-height: 1.45;
      margin: 0;
    }}
    .sidebar {{
      display: grid;
      gap: 20px;
      position: sticky;
      top: 20px;
      align-self: start;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 8px;
    }}
    .summary-card {{
      border: 1px solid rgba(198, 171, 135, 0.65);
      border-radius: 18px;
      padding: 14px;
      background: linear-gradient(180deg, rgba(255,255,255,0.85), rgba(248,241,228,0.78));
    }}
    .summary-card strong {{
      display: block;
      font-size: 22px;
      line-height: 1.1;
      margin-bottom: 6px;
    }}
    .summary-card span {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .callout {{
      margin-top: 14px;
      padding: 13px 14px;
      border-radius: 16px;
      background: var(--accent-soft);
      color: var(--accent-dark);
      font-size: 13px;
      line-height: 1.5;
    }}
    .device-meta {{
      margin-top: 10px;
      font-size: 13px;
      color: var(--muted);
      line-height: 1.5;
    }}
    #pointsList {{
      width: 100%;
      min-height: 280px;
    }}
    .hint {{
      font-size: 13px;
      color: var(--muted);
      line-height: 1.5;
      margin: 0;
    }}
    .map-marker {{
      width: 28px;
      height: 28px;
      border-radius: 999px;
      background: #bd5d38;
      color: white;
      display: grid;
      place-items: center;
      font-size: 12px;
      font-weight: 700;
      box-shadow: 0 6px 18px rgba(43, 36, 27, 0.22);
      border: 2px solid rgba(255, 250, 240, 0.95);
    }}
    .map-marker.selected {{
      background: #2b241b;
    }}
    .route-toolbar {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto auto;
      gap: 10px;
      margin-bottom: 14px;
    }}
    .route-actions {{
      justify-content: flex-start;
    }}
    .advanced-details {{
      margin-top: 16px;
      border: 1px solid rgba(198, 171, 135, 0.8);
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.56);
      overflow: hidden;
    }}
    .advanced-details summary {{
      cursor: pointer;
      list-style: none;
      padding: 16px 18px;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .advanced-details summary::-webkit-details-marker {{
      display: none;
    }}
    .advanced-body {{
      padding: 0 18px 18px;
      border-top: 1px solid rgba(198, 171, 135, 0.65);
    }}
    .segments-list {{
      display: grid;
      gap: 8px;
      max-height: 230px;
      overflow: auto;
      padding-right: 4px;
      margin-top: 12px;
    }}
    .segment-row {{
      display: grid;
      grid-template-columns: 1.6fr 130px;
      gap: 10px;
      align-items: center;
    }}
    .segment-label {{
      font-size: 13px;
      color: var(--muted);
      line-height: 1.45;
    }}
    .section-divider {{
      height: 1px;
      margin: 16px 0;
      background: linear-gradient(90deg, rgba(198, 171, 135, 0.9), rgba(198, 171, 135, 0));
    }}
    .output-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }}
    .muted-chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 10px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.74);
      color: var(--muted);
      border: 1px solid rgba(198, 171, 135, 0.55);
      font-size: 12px;
    }}
    code {{
      font-family: "SFMono-Regular", "Menlo", monospace;
      font-size: 0.92em;
    }}
    @media (max-width: 1140px) {{
      .workspace {{
        grid-template-columns: 1fr;
      }}
      .sidebar {{
        position: static;
      }}
      .bottom-grid {{
        grid-template-columns: 1fr;
      }}
    }}
    @media (max-width: 920px) {{
      .hero {{
        flex-direction: column;
        align-items: stretch;
      }}
      .hero-actions {{
        justify-content: flex-start;
      }}
      .toolbar-grid,
      .field-grid,
      .field-grid.compact-2,
      .field-grid.compact-4,
      .field-grid.compact-6,
      .route-toolbar {{
        grid-template-columns: 1fr 1fr;
      }}
      .map-footer {{
        flex-direction: column;
        align-items: stretch;
      }}
    }}
    @media (max-width: 680px) {{
      .page {{
        padding: 16px;
      }}
      .toolbar-grid,
      .field-grid,
      .field-grid.compact-2,
      .field-grid.compact-4,
      .field-grid.compact-6,
      .route-toolbar,
      .summary-grid,
      .segment-row {{
        grid-template-columns: 1fr;
      }}
      #mapCanvas {{
        height: 400px;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="hero">
      <div>
        <p class="eyebrow">Motion Route Studio</p>
        <h1>Маршрут, устройство и скорость в одном рабочем экране</h1>
        <p class="subtitle">Интерфейс собран вокруг карты и короткой сводки: сначала вы строите путь, затем выбираете устройство, а тонкую настройку скорости открываете только когда она действительно нужна.</p>
      </div>
      <div class="hero-actions">
        <button id="previewButton">Предпросмотр</button>
        <button class="primary" id="runButton">Запустить</button>
        <button class="warn" id="stopButton">Стоп</button>
      </div>
    </header>

    <div class="workspace">
      <section class="panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">Шаг 1</p>
            <h2>Карта маршрута</h2>
            <p>Кликайте по карте, чтобы добавлять точки, и держите весь маршрут в фокусе во время настройки.</p>
          </div>
          <div class="status-group">
            <div class="status" id="runStatus">Ожидание</div>
            <div class="status" id="mapStatus">Карта загружена</div>
          </div>
        </div>

        <div class="toolbar-grid">
          <div>
            <label for="platformSelect">Платформа</label>
            <select id="platformSelect">
              <option value="android">Android Emulator</option>
              <option value="ios">iOS Simulator</option>
            </select>
          </div>
          <div>
            <label for="mapLanguageSelect">Язык карты</label>
            <select id="mapLanguageSelect">
              <option value="ru">Русский</option>
              <option value="en">English</option>
              <option value="local">Локальный</option>
            </select>
          </div>
          <div>
            <label for="deviceSelect">Активное устройство</label>
            <select id="deviceSelect"></select>
          </div>
          <div>
            <label>&nbsp;</label>
            <button id="refreshDevicesButton">Обновить список</button>
          </div>
        </div>

        <div class="map-shell">
          <div id="mapCanvas"></div>
        </div>

        <div class="map-footer">
          <p class="inline-note" id="mapHint">Кликайте по карте для добавления точек. Подписи карты можно локализовать отдельно от языка браузера.</p>
          <div class="button-row">
            <button class="ghost small" id="fitRouteButton">Вписать маршрут</button>
            <button class="ghost small" id="undoPointButton">Отменить последнюю точку</button>
            <button class="ghost small" id="clearPoints">Очистить маршрут</button>
          </div>
        </div>
      </section>

      <aside class="sidebar">
        <section class="panel">
          <div class="panel-heading">
            <div>
              <p class="eyebrow">Сводка</p>
              <h2>Что получится на выходе</h2>
            </div>
          </div>
          <div class="summary-grid">
            <div class="summary-card">
              <strong id="summaryPointsValue">2 точки</strong>
              <span id="summaryPointsMeta">1 отрезок</span>
            </div>
            <div class="summary-card">
              <strong id="summaryDistanceValue">0 м</strong>
              <span id="summaryDistanceMeta">Длина маршрута</span>
            </div>
            <div class="summary-card">
              <strong id="summaryTimingValue">60 c</strong>
              <span id="summaryTimingMeta">Базовая длительность</span>
            </div>
            <div class="summary-card">
              <strong id="summaryProfileValue">ease-in → ease-out</strong>
              <span id="summaryProfileMeta">Базовый профиль скорости</span>
            </div>
          </div>
          <div class="callout" id="summaryHint">Сначала соберите маршрут хотя бы из двух точек, затем выберите режим по длительности или по скорости.</div>
        </section>

        <section class="panel">
          <div class="panel-heading">
            <div>
              <p class="eyebrow">Шаг 2</p>
              <h2>Устройство</h2>
            </div>
          </div>
          <div class="field-grid compact-2">
            <div id="adbField">
              <label for="adbInput">ADB путь</label>
              <input id="adbInput" type="text">
            </div>
            <div>
              <label for="targetIdInput">Ручной ID устройства</label>
              <input id="targetIdInput" type="text" placeholder="emulator-5554 или UDID">
            </div>
          </div>
          <div class="field-grid compact-2">
            <div id="altitudeField">
              <label for="altitudeInput">Высота (м)</label>
              <input id="altitudeInput" type="number" step="any">
            </div>
          </div>
          <p class="device-meta" id="deviceHint">Список активных устройств можно обновить вручную. Ручной ID имеет приоритет над выбранным устройством.</p>
        </section>

        <section class="panel">
          <div class="output-header">
            <div>
              <p class="eyebrow">Лог</p>
              <h2>Предпросмотр и выполнение</h2>
            </div>
            <button class="ghost small" id="clearOutputButton">Очистить</button>
          </div>
          <div class="muted-chip" id="outputModeChip">Готово к предпросмотру</div>
          <div class="section-divider"></div>
          <textarea id="output" readonly></textarea>
        </section>
      </aside>
    </div>

    <div class="bottom-grid">
      <section class="panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">Шаг 3</p>
            <h2>Редактор точек</h2>
            <p>Можно редактировать точную геометрию маршрута вручную после кликов по карте.</p>
          </div>
        </div>

        <div class="route-toolbar">
          <div>
            <label for="latInput">Latitude</label>
            <input id="latInput" type="number" step="any">
          </div>
          <div>
            <label for="lonInput">Longitude</label>
            <input id="lonInput" type="number" step="any">
          </div>
          <div>
            <label>&nbsp;</label>
            <button id="addPoint">Добавить</button>
          </div>
          <div>
            <label>&nbsp;</label>
            <button id="updatePoint">Обновить</button>
          </div>
        </div>

        <select id="pointsList" size="12"></select>

        <div class="button-row route-actions" style="margin-top:12px;">
          <button class="ghost" id="removePoint">Удалить</button>
          <button class="ghost" id="moveUp">Выше</button>
          <button class="ghost" id="moveDown">Ниже</button>
        </div>
        <p class="section-copy">Подсказка: сначала добавьте точки грубо на карте, затем при необходимости поправьте координаты здесь.</p>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">Шаг 4</p>
            <h2>Профиль движения</h2>
            <p>Базовые параметры всегда на виду, а сложные speed-сценарии раскрываются по мере необходимости.</p>
          </div>
        </div>

        <div class="field-grid compact-6">
          <div>
            <label for="timingModeSelect">Режим</label>
            <select id="timingModeSelect">
              <option value="duration">По длительности</option>
              <option value="speed">По скорости</option>
            </select>
          </div>
          <div>
            <label for="durationInput">Длительность (с)</label>
            <input id="durationInput" type="number" step="any">
          </div>
          <div>
            <label for="speedKmhInput">Средняя скорость (км/ч)</label>
            <input id="speedKmhInput" type="number" step="any">
          </div>
          <div>
            <label for="intervalInput">Интервал (с)</label>
            <input id="intervalInput" type="number" step="any">
          </div>
          <div>
            <label for="startCurveInput">Старт</label>
            <select id="startCurveInput"></select>
          </div>
          <div>
            <label for="stopCurveInput">Остановка</label>
            <select id="stopCurveInput"></select>
          </div>
        </div>

        <div class="field-grid compact-4">
          <div>
            <label for="startShareInput">Доля старта</label>
            <input id="startShareInput" type="number" min="0" max="1" step="0.01">
          </div>
          <div>
            <label for="stopShareInput">Доля остановки</label>
            <input id="stopShareInput" type="number" min="0" max="1" step="0.01">
          </div>
        </div>

        <p class="hint" id="motionHint">Сочетание <code>ease-in</code> и <code>ease-out</code> обычно даёт естественное движение для большинства сценариев.</p>

        <details class="advanced-details" id="advancedSpeedDetails">
          <summary>
            <span>Продвинутая настройка скорости</span>
            <span class="muted-chip" id="advancedSummaryChip">Выключена</span>
          </summary>
          <div class="advanced-body">
            <div class="field-grid compact-4">
              <div>
                <label for="variationCurveInput">Кривая модуляции</label>
                <select id="variationCurveInput"></select>
              </div>
              <div>
                <label for="variationFrequencyInput">Частота (Гц)</label>
                <input id="variationFrequencyInput" type="number" step="any">
              </div>
              <div>
                <label for="variationAmplitudeInput">Амплитуда (%)</label>
                <input id="variationAmplitudeInput" type="number" step="any">
              </div>
              <div>
                <label>&nbsp;</label>
                <button id="toggleVariationButton">Включить модуляцию</button>
              </div>
            </div>

            <p class="section-copy">Модуляция меняет скорость всего маршрута по выбранной кривой и частоте. Это удобно для имитации живого движения без ручной разбивки каждого участка.</p>

            <div class="section-divider"></div>

            <h2>Скорость на отдельных отрезках</h2>
            <p class="section-copy">Пустое поле означает: использовать базовый режим маршрута. Заполненные значения задают фиксированную скорость только для выбранного отрезка.</p>
            <div class="segments-list" id="segmentSpeedList"></div>
          </div>
        </details>
      </section>
    </div>
  </div>
  <script src="https://unpkg.com/maplibre-gl@5.16.0/dist/maplibre-gl.js"></script>
  <script>
    const CONFIG = {json.dumps(config, ensure_ascii=False)};
    let points = CONFIG.defaults.points.map((point) => ({{ ...point }}));
    let selectedIndex = 0;
    let logCursor = 0;
    let pollTimer = null;
    let map = null;
    let markers = [];
    let routePolyline = null;

    const pointsList = document.getElementById('pointsList');
    const latInput = document.getElementById('latInput');
    const lonInput = document.getElementById('lonInput');
    const platformSelect = document.getElementById('platformSelect');
    const mapLanguageSelect = document.getElementById('mapLanguageSelect');
    const timingModeSelect = document.getElementById('timingModeSelect');
    const durationInput = document.getElementById('durationInput');
    const speedKmhInput = document.getElementById('speedKmhInput');
    const variationCurveInput = document.getElementById('variationCurveInput');
    const variationFrequencyInput = document.getElementById('variationFrequencyInput');
    const variationAmplitudeInput = document.getElementById('variationAmplitudeInput');
    const segmentSpeedList = document.getElementById('segmentSpeedList');
    const intervalInput = document.getElementById('intervalInput');
    const adbInput = document.getElementById('adbInput');
    const deviceSelect = document.getElementById('deviceSelect');
    const targetIdInput = document.getElementById('targetIdInput');
    const altitudeInput = document.getElementById('altitudeInput');
    const startCurveInput = document.getElementById('startCurveInput');
    const stopCurveInput = document.getElementById('stopCurveInput');
    const startShareInput = document.getElementById('startShareInput');
    const stopShareInput = document.getElementById('stopShareInput');
    const output = document.getElementById('output');
    const outputModeChip = document.getElementById('outputModeChip');
    const runStatus = document.getElementById('runStatus');
    const mapStatus = document.getElementById('mapStatus');
    const mapCanvas = document.getElementById('mapCanvas');
    const mapHint = document.getElementById('mapHint');
    const deviceHint = document.getElementById('deviceHint');
    const summaryPointsValue = document.getElementById('summaryPointsValue');
    const summaryPointsMeta = document.getElementById('summaryPointsMeta');
    const summaryDistanceValue = document.getElementById('summaryDistanceValue');
    const summaryDistanceMeta = document.getElementById('summaryDistanceMeta');
    const summaryTimingValue = document.getElementById('summaryTimingValue');
    const summaryTimingMeta = document.getElementById('summaryTimingMeta');
    const summaryProfileValue = document.getElementById('summaryProfileValue');
    const summaryProfileMeta = document.getElementById('summaryProfileMeta');
    const summaryHint = document.getElementById('summaryHint');
    const motionHint = document.getElementById('motionHint');
    const adbField = document.getElementById('adbField');
    const altitudeField = document.getElementById('altitudeField');
    const advancedSpeedDetails = document.getElementById('advancedSpeedDetails');
    const advancedSummaryChip = document.getElementById('advancedSummaryChip');
    let variationEnabled = CONFIG.defaults.variationEnabled;
    let segmentSpeedOverrides = [];
    let advancedSummaryWasActive = false;

    function setRunStatus(running) {{
      runStatus.textContent = running ? 'Выполняется' : 'Ожидание';
      runStatus.className = running ? 'status live' : 'status';
    }}

    function setOutputMode(message) {{
      outputModeChip.textContent = message;
    }}

    function fillDefaults() {{
      platformSelect.value = CONFIG.defaults.platform;
      mapLanguageSelect.value = CONFIG.defaults.mapLanguage;
      timingModeSelect.value = CONFIG.defaults.timingMode;
      durationInput.value = CONFIG.defaults.duration;
      speedKmhInput.value = CONFIG.defaults.speedKmh;
      variationFrequencyInput.value = CONFIG.defaults.variationFrequencyHz;
      variationAmplitudeInput.value = CONFIG.defaults.variationAmplitudePercent;
      intervalInput.value = CONFIG.defaults.interval;
      adbInput.value = CONFIG.defaults.adbPath;
      targetIdInput.value = CONFIG.defaults.targetId;
      altitudeInput.value = CONFIG.defaults.altitude;
      startShareInput.value = CONFIG.defaults.startShare;
      stopShareInput.value = CONFIG.defaults.stopShare;
      for (const curve of CONFIG.curves) {{
        startCurveInput.add(new Option(curve, curve));
        stopCurveInput.add(new Option(curve, curve));
        variationCurveInput.add(new Option(curve, curve));
      }}
      startCurveInput.value = CONFIG.defaults.startCurve;
      stopCurveInput.value = CONFIG.defaults.stopCurve;
      variationCurveInput.value = CONFIG.defaults.variationCurve;
      deviceSelect.innerHTML = '';
      deviceSelect.add(new Option('Автовыбор', ''));
      segmentSpeedOverrides = [...CONFIG.defaults.segmentSpeedsKmh];
      updatePlatformVisibility();
      updateTimingVisibility();
      updateVariationButton();
      updateAdvancedSummary();
      updateSummary();
    }}

    function updateVariationButton() {{
      document.getElementById('toggleVariationButton').textContent =
        variationEnabled ? 'Выключить модуляцию' : 'Включить модуляцию';
    }}

    function updatePlatformVisibility() {{
      const isAndroid = platformSelect.value === 'android';
      adbInput.disabled = !isAndroid;
      altitudeInput.disabled = !isAndroid;
      adbField.style.display = isAndroid ? '' : 'none';
      altitudeField.style.display = isAndroid ? '' : 'none';
      targetIdInput.placeholder = isAndroid ? 'emulator-5554' : 'Simulator UDID';
      mapHint.textContent = isAndroid
        ? 'Кликайте по карте для добавления точек. Подписи карты можно локализовать отдельно от языка браузера.'
        : 'Кликайте по карте для добавления точек. Для iOS используются только загруженные booted симуляторы.';
      updateSummary();
    }}

    function updateTimingVisibility() {{
      const useDuration = timingModeSelect.value === 'duration';
      durationInput.disabled = !useDuration;
      speedKmhInput.disabled = useDuration;
      updateSummary();
    }}

    function formatDistance(distanceMeters) {{
      if (!Number.isFinite(distanceMeters) || distanceMeters <= 0) {{
        return '0 м';
      }}
      if (distanceMeters >= 1000) {{
        return `${{(distanceMeters / 1000).toFixed(distanceMeters >= 10000 ? 1 : 2)}} км`;
      }}
      return `${{Math.round(distanceMeters)}} м`;
    }}

    function formatDuration(seconds) {{
      if (!Number.isFinite(seconds) || seconds <= 0) {{
        return '0 c';
      }}
      if (seconds >= 3600) {{
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.round((seconds % 3600) / 60);
        return `${{hours}} ч ${{minutes}} мин`;
      }}
      if (seconds >= 60) {{
        const minutes = Math.floor(seconds / 60);
        const remainder = Math.round(seconds % 60);
        return `${{minutes}} мин ${{remainder}} c`;
      }}
      return `${{Math.round(seconds)}} c`;
    }}

    function pluralizeRu(count, one, few, many) {{
      const absolute = Math.abs(Number(count));
      const mod100 = absolute % 100;
      const mod10 = absolute % 10;
      if (mod100 >= 11 && mod100 <= 14) {{
        return many;
      }}
      if (mod10 === 1) {{
        return one;
      }}
      if (mod10 >= 2 && mod10 <= 4) {{
        return few;
      }}
      return many;
    }}

    function haversineMeters(pointA, pointB) {{
      const toRadians = (value) => value * Math.PI / 180;
      const earthRadius = 6371000;
      const lat1 = toRadians(Number(pointA.lat));
      const lat2 = toRadians(Number(pointB.lat));
      const dLat = lat2 - lat1;
      const dLon = toRadians(Number(pointB.lon) - Number(pointA.lon));
      const sinLat = Math.sin(dLat / 2);
      const sinLon = Math.sin(dLon / 2);
      const a = sinLat * sinLat + Math.cos(lat1) * Math.cos(lat2) * sinLon * sinLon;
      return 2 * earthRadius * Math.asin(Math.sqrt(a));
    }}

    function computeRouteDistanceMeters() {{
      let total = 0;
      for (let index = 0; index < points.length - 1; index += 1) {{
        total += haversineMeters(points[index], points[index + 1]);
      }}
      return total;
    }}

    function countSegmentOverrides() {{
      return segmentSpeedOverrides.filter((value) => String(value).trim() !== '').length;
    }}

    function fitRouteToPoints() {{
      if (map) {{
        renderMapRoute(true);
      }}
    }}

    function updateAdvancedSummary() {{
      const segmentsWithSpeed = countSegmentOverrides();
      const items = [];
      if (variationEnabled) {{
        items.push(`модуляция ${{variationCurveInput.value}}`);
      }}
      if (segmentsWithSpeed > 0) {{
        items.push(
          `${{segmentsWithSpeed}} ${{
            pluralizeRu(segmentsWithSpeed, 'отрезок', 'отрезка', 'отрезков')
          }} с фиксированной скоростью`
        );
      }}
      const isActive = items.length > 0;
      advancedSummaryChip.textContent = isActive ? items.join(' · ') : 'Выключена';
      if (isActive && !advancedSummaryWasActive) {{
        advancedSpeedDetails.open = true;
      }}
      if (!isActive) {{
        advancedSummaryWasActive = false;
      }} else {{
        advancedSummaryWasActive = true;
      }}
    }}

    function updateSummary() {{
      const pointCount = points.length;
      const segmentCount = Math.max(0, pointCount - 1);
      const distanceMeters = computeRouteDistanceMeters();
      const baseSpeedKmh = Number(speedKmhInput.value);
      const durationSeconds = Number(durationInput.value);
      const baseMode = timingModeSelect.value;
      const segmentsWithSpeed = countSegmentOverrides();
      const advancedEnabled = variationEnabled || segmentsWithSpeed > 0;

      summaryPointsValue.textContent = `${{pointCount}} ${{
        pluralizeRu(pointCount, 'точка', 'точки', 'точек')
      }}`;
      summaryPointsMeta.textContent = segmentCount
        ? `${{segmentCount}} ${{
            pluralizeRu(segmentCount, 'отрезок', 'отрезка', 'отрезков')
          }}`
        : 'Добавьте вторую точку';

      summaryDistanceValue.textContent = formatDistance(distanceMeters);
      summaryDistanceMeta.textContent = segmentCount ? 'Длина построенного маршрута' : 'Маршрут ещё не собран';

      if (segmentCount === 0) {{
        summaryTimingValue.textContent = 'Нет маршрута';
        summaryTimingMeta.textContent = 'Нужно минимум две точки';
      }} else if (baseMode === 'duration' && Number.isFinite(durationSeconds) && durationSeconds > 0) {{
        const estimatedSpeedKmh = distanceMeters > 0 ? (distanceMeters / durationSeconds) * 3.6 : 0;
        summaryTimingValue.textContent = formatDuration(durationSeconds);
        summaryTimingMeta.textContent = estimatedSpeedKmh > 0
          ? `Около ${{estimatedSpeedKmh.toFixed(1)}} км/ч в среднем`
          : 'Скорость появится после расчёта маршрута';
      }} else if (baseMode === 'speed' && Number.isFinite(baseSpeedKmh) && baseSpeedKmh > 0) {{
        const estimatedDuration = distanceMeters > 0 ? distanceMeters / (baseSpeedKmh / 3.6) : 0;
        summaryTimingValue.textContent = `${{baseSpeedKmh.toFixed(1)}} км/ч`;
        summaryTimingMeta.textContent = estimatedDuration > 0
          ? `Ориентировочно ${{
              formatDuration(estimatedDuration)
            }} на весь маршрут`
          : 'Длительность появится после расчёта маршрута';
      }} else {{
        summaryTimingValue.textContent = 'Проверьте режим';
        summaryTimingMeta.textContent = 'Нужны корректные длительность или скорость';
      }}

      summaryProfileValue.textContent = `${{startCurveInput.value}} → ${{stopCurveInput.value}}`;
      if (advancedEnabled) {{
        const extras = [];
        if (variationEnabled) {{
          extras.push(`модуляция ${{variationCurveInput.value}} @ ${{variationFrequencyInput.value || '?'}} Гц`);
        }}
        if (segmentsWithSpeed > 0) {{
          extras.push(
            `${{segmentsWithSpeed}} ${{
              pluralizeRu(segmentsWithSpeed, 'отрезок', 'отрезка', 'отрезков')
            }} с фиксированной скоростью`
          );
        }}
        summaryProfileMeta.textContent = extras.join(' · ');
      }} else {{
        summaryProfileMeta.textContent = 'Базовый профиль скорости';
      }}

      if (segmentCount === 0) {{
        summaryHint.textContent = 'Сначала соберите маршрут хотя бы из двух точек, затем выберите режим по длительности или по скорости.';
      }} else if (advancedEnabled) {{
        summaryHint.textContent = 'На маршруте включён расширенный speed-profile. Итоговая длительность и средняя скорость будут уточняться в предпросмотре.';
      }} else if (baseMode === 'duration') {{
        summaryHint.textContent = 'Вы контролируете общее время движения. Полезно, когда нужно уложить маршрут в точный сценарий.';
      }} else {{
        summaryHint.textContent = 'Вы контролируете базовую среднюю скорость. Это удобно для более реалистичного движения между точками.';
      }}

      motionHint.textContent = advancedEnabled
        ? 'Сейчас включены расширенные настройки скорости. Предпросмотр покажет итоговый профиль точнее, чем базовая сводка.'
        : 'Сочетание ease-in и ease-out обычно даёт естественное движение для большинства сценариев.';

      updateAdvancedSummary();
    }}

    function renderSegmentSpeedInputs() {{
      segmentSpeedList.innerHTML = '';
      const segmentCount = Math.max(0, points.length - 1);
      while (segmentSpeedOverrides.length < segmentCount) {{
        segmentSpeedOverrides.push('');
      }}
      segmentSpeedOverrides = segmentSpeedOverrides.slice(0, segmentCount);

      if (segmentCount === 0) {{
        const empty = document.createElement('div');
        empty.className = 'segment-label';
        empty.textContent = 'Добавьте минимум две точки, чтобы появились отрезки.';
        segmentSpeedList.appendChild(empty);
        return;
      }}

      for (let index = 0; index < segmentCount; index += 1) {{
        const row = document.createElement('div');
        row.className = 'segment-row';

        const label = document.createElement('div');
        label.className = 'segment-label';
        label.textContent =
          `Отрезок ${{index + 1}}: ${{points[index].lat.toFixed(5)}},${{points[index].lon.toFixed(5)}} -> ` +
          `${{points[index + 1].lat.toFixed(5)}},${{points[index + 1].lon.toFixed(5)}}`;

        const input = document.createElement('input');
        input.type = 'number';
        input.step = 'any';
        input.placeholder = 'км/ч';
        input.value = segmentSpeedOverrides[index] ?? '';
        input.addEventListener('input', () => {{
          segmentSpeedOverrides[index] = input.value;
          updateSummary();
        }});

        row.appendChild(label);
        row.appendChild(input);
        segmentSpeedList.appendChild(row);
      }}
    }}

    function renderPoints(fitMap = false) {{
      pointsList.innerHTML = '';
      points.forEach((point, index) => {{
        const option = document.createElement('option');
        option.value = String(index);
        option.textContent = `${{String(index + 1).padStart(2, '0')}}. ${{Number(point.lat).toFixed(6)}},${{Number(point.lon).toFixed(6)}}`;
        pointsList.appendChild(option);
      }});
      if (points.length === 0) {{
        latInput.value = '';
        lonInput.value = '';
        selectedIndex = -1;
        renderMapRoute(fitMap);
        updateSummary();
        return;
      }}
      if (selectedIndex < 0 || selectedIndex >= points.length) {{
        selectedIndex = Math.max(0, points.length - 1);
      }}
      pointsList.selectedIndex = selectedIndex;
      syncSelectedPoint();
      renderSegmentSpeedInputs();
      renderMapRoute(fitMap);
      updateSummary();
    }}

    function syncSelectedPoint() {{
      if (selectedIndex < 0 || selectedIndex >= points.length) {{
        return;
      }}
      latInput.value = points[selectedIndex].lat;
      lonInput.value = points[selectedIndex].lon;
    }}

    function setOutput(lines) {{
      output.value = Array.isArray(lines) ? lines.join('\\n') : String(lines || '');
      output.scrollTop = 0;
    }}

    function collectPayload() {{
      return {{
        platform: platformSelect.value,
        timingMode: timingModeSelect.value,
        points,
        duration: durationInput.value,
        speedKmh: speedKmhInput.value,
        segmentSpeedsKmh: segmentSpeedOverrides,
        variationEnabled,
        variationCurve: variationCurveInput.value,
        variationFrequencyHz: variationFrequencyInput.value,
        variationAmplitudePercent: variationAmplitudeInput.value,
        interval: intervalInput.value,
        adbPath: adbInput.value,
        targetId: targetIdInput.value.trim() || deviceSelect.value,
        altitude: altitudeInput.value,
        startCurve: startCurveInput.value,
        stopCurve: stopCurveInput.value,
        startShare: startShareInput.value,
        stopShare: stopShareInput.value,
      }};
    }}

    async function postJson(url, payload) {{
      const response = await fetch(url, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await response.json();
      if (!response.ok) {{
        throw new Error(data.error || 'Request failed');
      }}
      return data;
    }}

    async function pollLogs() {{
      const response = await fetch(`/api/logs?since=${{logCursor}}`);
      const data = await response.json();
      if (data.logs.length) {{
        const existing = output.value ? output.value + '\\n' : '';
        output.value = existing + data.logs.join('\\n');
        output.scrollTop = output.scrollHeight;
      }}
      logCursor = data.nextCursor;
      setRunStatus(data.running);
      if (!data.running && pollTimer) {{
        clearInterval(pollTimer);
        pollTimer = null;
        setOutputMode('Выполнение завершено');
      }}
    }}

    function ensurePolling() {{
      if (!pollTimer) {{
        pollTimer = setInterval(pollLogs, 1000);
      }}
    }}

    function parsePointInputs() {{
      if (latInput.value.trim() === '' || lonInput.value.trim() === '') {{
        throw new Error('Заполните latitude и longitude.');
      }}
      const lat = Number(latInput.value);
      const lon = Number(lonInput.value);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {{
        throw new Error('Latitude и longitude должны быть числами.');
      }}
      return {{ lat, lon }};
    }}

    async function refreshEmulators() {{
      try {{
        const currentValue = deviceSelect.value;
        const data = await postJson('/api/devices', {{
          platform: platformSelect.value,
          adbPath: adbInput.value,
        }});
        deviceSelect.innerHTML = '';
        deviceSelect.add(new Option('Автовыбор', ''));
        for (const device of data.devices) {{
          deviceSelect.add(new Option(device.label, device.id));
        }}
        if (data.devices.some((device) => device.id === currentValue)) {{
          deviceSelect.value = currentValue;
        }}
        const entityLabel = platformSelect.value === 'android' ? 'эмуляторов' : 'симуляторов';
        deviceHint.textContent = data.devices.length
          ? `Найдено ${{data.devices.length}} активных ${{
              entityLabel
            }}. Ручной ID имеет приоритет над выбранным устройством.`
          : `Активные устройства не найдены. Можно указать ID вручную или сначала запустить ${{
              platformSelect.value === 'android' ? 'Android Emulator' : 'iOS Simulator'
            }}.`;
        setRunStatus(data.running);
      }} catch (error) {{
        setOutput(`Error: ${{error.message}}`);
        setOutputMode('Ошибка запроса');
      }}
    }}

    function setMapStatus(message, loaded = false) {{
      mapStatus.textContent = message;
      mapStatus.className = loaded ? 'status' : 'status warn';
    }}

    function mapLanguageExpression() {{
      const language = mapLanguageSelect.value;
      if (language === 'ru') {{
        return ['coalesce', ['get', 'name_ru'], ['get', 'name:ru'], ['get', 'name_en'], ['get', 'name']];
      }}
      if (language === 'en') {{
        return ['coalesce', ['get', 'name_en'], ['get', 'name:en'], ['get', 'name']];
      }}
      return ['coalesce', ['get', 'name'], ['get', 'name_en']];
    }}

    function expressionContainsName(value) {{
      if (typeof value === 'string') {{
        return /^name($|_|:)/.test(value);
      }}
      if (Array.isArray(value)) {{
        return value.some(expressionContainsName);
      }}
      if (value && typeof value === 'object') {{
        return Object.values(value).some(expressionContainsName);
      }}
      return false;
    }}

    function applyMapLanguage() {{
      if (!map || !map.isStyleLoaded()) {{
        return;
      }}
      const expression = mapLanguageExpression();
      const style = map.getStyle();
      for (const layer of style.layers || []) {{
        if (layer.type !== 'symbol') {{
          continue;
        }}
        const textField = map.getLayoutProperty(layer.id, 'text-field');
        if (!textField || !expressionContainsName(textField)) {{
          continue;
        }}
        try {{
          map.setLayoutProperty(layer.id, 'text-field', expression);
        }} catch (_error) {{
        }}
      }}
    }}

    function createMarkerElement(index, isSelected) {{
      const element = document.createElement('div');
      element.className = `map-marker${{isSelected ? ' selected' : ''}}`;
      element.textContent = String(index + 1);
      return element;
    }}

    function ensureRouteLayer() {{
      if (!map || !map.isStyleLoaded()) {{
        return false;
      }}
      if (!map.getSource('route-source')) {{
        map.addSource('route-source', {{
          type: 'geojson',
          data: {{
            type: 'FeatureCollection',
            features: [],
          }},
        }});
      }}
      if (!map.getLayer('route-line')) {{
        map.addLayer({{
          id: 'route-line',
          type: 'line',
          source: 'route-source',
          paint: {{
            'line-color': '#bd5d38',
            'line-opacity': 0.92,
            'line-width': 4,
          }},
        }});
      }}
      return true;
    }}

    function renderMapRoute(fitMap = false) {{
      if (!map || !map.isStyleLoaded()) {{
        return;
      }}
      for (const marker of markers) {{
        marker.remove();
      }}
      markers = [];

      const coordinates = [];
      points.forEach((point, index) => {{
        const lngLat = [Number(point.lon), Number(point.lat)];
        coordinates.push(lngLat);
        const marker = new maplibregl.Marker({{
          element: createMarkerElement(index, index === selectedIndex),
        }})
          .setLngLat(lngLat)
          .addTo(map);
        marker.getElement().addEventListener('click', () => {{
          selectedIndex = index;
          renderPoints(false);
        }});
        markers.push(marker);
      }});

      if (ensureRouteLayer()) {{
        const featureCollection = {{
          type: 'FeatureCollection',
          features: points.length >= 2
            ? [{{
                type: 'Feature',
                geometry: {{
                  type: 'LineString',
                  coordinates,
                }},
                properties: {{}},
              }}]
            : [],
        }};
        map.getSource('route-source').setData(featureCollection);
      }}

      if (fitMap && points.length > 1) {{
        const bounds = coordinates.reduce(
          (acc, coord) => acc.extend(coord),
          new maplibregl.LngLatBounds(coordinates[0], coordinates[0]),
        );
        map.fitBounds(bounds, {{ padding: 48, duration: 0 }});
      }} else if (fitMap && points.length === 1) {{
        map.jumpTo({{ center: coordinates[0], zoom: 15 }});
      }}
    }}

    function initializeMap() {{
      if (typeof maplibregl === 'undefined') {{
        throw new Error('MapLibre не загрузился.');
      }}
      if (!map) {{
        const center = points[0] ? [Number(points[0].lon), Number(points[0].lat)] : [-122.0840575, 37.4219999];
        map = new maplibregl.Map({{
          container: 'mapCanvas',
          style: 'https://tiles.openfreemap.org/styles/liberty',
          center,
          zoom: 14,
          attributionControl: true,
        }});
        map.addControl(new maplibregl.NavigationControl(), 'top-right');
        map.on('load', () => {{
          applyMapLanguage();
          renderMapRoute(true);
        }});
        map.on('click', (event) => {{
          points.push({{
            lat: event.lngLat.lat,
            lon: event.lngLat.lng,
          }});
          selectedIndex = points.length - 1;
          renderPoints(true);
          setOutputMode('Маршрут обновлён');
        }});
      }}
      setMapStatus('Карта загружена. Кликайте по карте для добавления точек.', true);
      if (map.isStyleLoaded()) {{
        applyMapLanguage();
        renderMapRoute(true);
      }}
    }}

    document.getElementById('addPoint').addEventListener('click', () => {{
      try {{
        points.push(parsePointInputs());
        selectedIndex = points.length - 1;
        renderPoints(true);
        setOutputMode('Точка добавлена');
      }} catch (error) {{
        setOutput(`Error: ${{error.message}}`);
        setOutputMode('Ошибка ввода');
      }}
    }});

    document.getElementById('updatePoint').addEventListener('click', () => {{
      if (selectedIndex < 0 || selectedIndex >= points.length) return;
      try {{
        points[selectedIndex] = parsePointInputs();
        renderPoints(false);
        setOutputMode('Точка обновлена');
      }} catch (error) {{
        setOutput(`Error: ${{error.message}}`);
        setOutputMode('Ошибка ввода');
      }}
    }});

    document.getElementById('removePoint').addEventListener('click', () => {{
      if (selectedIndex < 0 || selectedIndex >= points.length) return;
      points.splice(selectedIndex, 1);
      selectedIndex = Math.min(selectedIndex, points.length - 1);
      renderPoints(true);
      setOutputMode('Точка удалена');
    }});

    document.getElementById('moveUp').addEventListener('click', () => {{
      if (selectedIndex <= 0) return;
      [points[selectedIndex - 1], points[selectedIndex]] = [points[selectedIndex], points[selectedIndex - 1]];
      selectedIndex -= 1;
      renderPoints(false);
      setOutputMode('Порядок точек изменён');
    }});

    document.getElementById('moveDown').addEventListener('click', () => {{
      if (selectedIndex < 0 || selectedIndex >= points.length - 1) return;
      [points[selectedIndex + 1], points[selectedIndex]] = [points[selectedIndex], points[selectedIndex + 1]];
      selectedIndex += 1;
      renderPoints(false);
      setOutputMode('Порядок точек изменён');
    }});

    document.getElementById('clearPoints').addEventListener('click', () => {{
      points = [];
      selectedIndex = -1;
      renderPoints(true);
      setOutputMode('Маршрут очищен');
    }});

    document.getElementById('undoPointButton').addEventListener('click', () => {{
      if (!points.length) return;
      points.pop();
      selectedIndex = Math.min(selectedIndex, points.length - 1);
      renderPoints(true);
      setOutputMode('Последняя точка отменена');
    }});

    document.getElementById('fitRouteButton').addEventListener('click', () => {{
      fitRouteToPoints();
      setOutputMode('Маршрут вписан в карту');
    }});

    document.getElementById('clearOutputButton').addEventListener('click', () => {{
      setOutput('');
      setOutputMode('Лог очищен');
    }});

    pointsList.addEventListener('change', () => {{
      selectedIndex = pointsList.selectedIndex;
      syncSelectedPoint();
    }});

    document.getElementById('previewButton').addEventListener('click', async () => {{
      try {{
        const data = await postJson('/api/preview', collectPayload());
        setOutput(data.lines);
        setRunStatus(data.running);
        setOutputMode('Показан предпросмотр');
      }} catch (error) {{
        setOutput(`Error: ${{error.message}}`);
        setOutputMode('Ошибка предпросмотра');
      }}
    }});

    document.getElementById('refreshDevicesButton').addEventListener('click', refreshEmulators);

    platformSelect.addEventListener('change', () => {{
      updatePlatformVisibility();
      refreshEmulators();
    }});

    mapLanguageSelect.addEventListener('change', applyMapLanguage);

    timingModeSelect.addEventListener('change', updateTimingVisibility);

    document.getElementById('toggleVariationButton').addEventListener('click', () => {{
      variationEnabled = !variationEnabled;
      updateVariationButton();
      updateSummary();
      setOutputMode(variationEnabled ? 'Модуляция включена' : 'Модуляция выключена');
    }});

    deviceSelect.addEventListener('change', () => {{
      if (deviceSelect.value) {{
        targetIdInput.value = '';
      }}
    }});

    targetIdInput.addEventListener('input', () => {{
      if (targetIdInput.value.trim()) {{
        deviceSelect.value = '';
      }}
    }});

    document.getElementById('runButton').addEventListener('click', async () => {{
      try {{
        logCursor = 0;
        setOutput('');
        const data = await postJson('/api/run', collectPayload());
        setRunStatus(data.running);
        setOutputMode('Маршрут выполняется');
        await pollLogs();
        ensurePolling();
      }} catch (error) {{
        setOutput(`Error: ${{error.message}}`);
        setOutputMode('Ошибка запуска');
      }}
    }});

    document.getElementById('stopButton').addEventListener('click', async () => {{
      try {{
        await postJson('/api/stop', {{}});
        await pollLogs();
        setOutputMode('Остановка запрошена');
      }} catch (error) {{
        setOutput(`Error: ${{error.message}}`);
        setOutputMode('Ошибка остановки');
      }}
    }});

    [
      durationInput,
      speedKmhInput,
      intervalInput,
      startCurveInput,
      stopCurveInput,
      startShareInput,
      stopShareInput,
      variationCurveInput,
      variationFrequencyInput,
      variationAmplitudeInput,
    ].forEach((element) => {{
      element.addEventListener('input', updateSummary);
      element.addEventListener('change', updateSummary);
    }});

    fillDefaults();
    initializeMap();
    renderPoints(true);
    refreshEmulators();
    pollLogs();
  </script>
</body>
</html>"""


class MotionHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], state: MotionWebState) -> None:
        super().__init__(server_address, MotionRequestHandler)
        self.state = state


class MotionRequestHandler(BaseHTTPRequestHandler):
    server_version = "MotionEmulator/1.0"

    def do_HEAD(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        if parsed.path in {"/api/logs", "/api/devices"}:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._send_html(build_web_ui_html())
            return
        if parsed.path == "/api/logs":
            raw_since = parse_qs(parsed.query).get("since", ["0"])[0]
            try:
                since = max(0, int(raw_since))
            except ValueError:
                since = 0
            logs, next_cursor, running = self.server.state.snapshot_logs(since)  # type: ignore[attr-defined]
            self._send_json(
                {
                    "logs": logs,
                    "nextCursor": next_cursor,
                    "running": running,
                }
            )
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path == "/api/preview":
            self._handle_json_action(self.server.state.preview)  # type: ignore[attr-defined]
            return
        if self.path == "/api/devices":
            self._handle_json_action(self.server.state.list_devices)  # type: ignore[attr-defined]
            return
        if self.path == "/api/emulators":
            self._handle_json_action(self.server.state.list_emulators)  # type: ignore[attr-defined]
            return
        if self.path == "/api/run":
            self._handle_json_action(self.server.state.start_run)  # type: ignore[attr-defined]
            return
        if self.path == "/api/stop":
            self._handle_json_action(self.server.state.stop_run)  # type: ignore[attr-defined]
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle_json_action(
        self,
        action: Callable[[dict[str, object]], dict[str, object]],
    ) -> None:
        try:
            payload = self._read_json_body()
            response = action(payload)
            self._send_json(response)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON body."}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json(
                {"error": f"Internal server error: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _read_json_body(self) -> dict[str, object]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        return payload

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def launch_gui(port: int) -> int:
    if not 1 <= port <= 65535:
        print("Error: --port must be between 1 and 65535.", file=sys.stderr)
        return 2

    state = MotionWebState()
    try:
        server = MotionHTTPServer(("127.0.0.1", port), state)
    except OSError as exc:
        print(f"Unable to start web UI on port {port}: {exc}", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{port}"
    print(f"Web UI available at {url}")
    print("Open this URL in your browser. Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web UI...")
    finally:
        state.stop_event.set()
        server.server_close()
    return 0


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)

    if args.list_curves:
        print_curve_table()
        return 0

    if args.gui:
        return launch_gui(args.port)

    target_id = args.device_id
    if args.platform == "android" and not target_id:
        target_id = args.serial
    speed_mps = args.speed_mps
    if speed_mps is None and args.speed_kmh is not None:
        speed_mps = args.speed_kmh / 3.6
    advanced_profile = parse_advanced_speed_profile_from_args(
        args,
        segment_count=len(args.points) - 1,
    )

    try:
        route, samples, curve_label, timing = build_samples_from_options(
            points=args.points,
            duration=args.duration if speed_mps is None else None,
            interval=args.interval,
            legacy_curve_name=args.curve,
            profile=parse_motion_profile_from_args(args),
            speed_mps=speed_mps,
            advanced_profile=advanced_profile,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    describe_run(route, samples, curve_label, timing, args.interval)

    if args.dry_run:
        print_sample_table(samples)
        return 0

    try:
        if args.platform == "android":
            serial = resolve_emulator_serial(args.adb_path, target_id)
            print(f"Using Android emulator: {serial}")
            run_android_motion(args.adb_path, serial, samples, args.altitude)
        else:
            device_id = resolve_ios_simulator_udid(target_id)
            print(f"Using iOS simulator: {device_id}")
            if args.altitude is not None:
                print("Note: altitude is ignored by iOS Simulator location set.")
            run_ios_motion(device_id, samples)
    except subprocess.CalledProcessError as exc:
        print(
            f"Platform command failed with code {exc.returncode}: {exc.cmd}",
            file=sys.stderr,
        )
        return exc.returncode or 1
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
