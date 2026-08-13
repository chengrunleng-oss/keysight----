from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
import time
from typing import Protocol

import numpy as np

from .analysis import DeviationResult, overlapping_deviations, write_latest_deviations
from .connection_gate import ConnectionFingerprint, ParameterFingerprint
from .instrument import InstrumentCancelled, InstrumentSettings, ParameterCheck


class MeasurementSource(Protocol):
    identity: str
    actual_gate_time_s: float
    max_samples_per_segment: int | None

    def connect(self) -> str: ...
    def configure(self) -> float: ...
    def start(self) -> None: ...
    def read_available(self) -> np.ndarray: ...
    def memory_overflowed(self) -> bool: ...
    def read_memory_overflow_event(self) -> bool: ...
    def abort(self) -> None: ...
    def cancel_io(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class SessionSettings:
    instrument: InstrumentSettings
    reference_hz: float
    analysis_window_points: int
    output_root: Path
    simulated: bool = False
    analysis_interval_s: float = 2.0
    first_read_timeout_s: float | None = None
    no_data_timeout_s: float | None = None

    def validate(self) -> None:
        self.instrument.validate()
        if self.reference_hz < 0 or not np.isfinite(self.reference_hz):
            raise ValueError("reference frequency must be zero or a positive number")
        if self.analysis_window_points < 100:
            raise ValueError("analysis window must contain at least 100 points")
        if self.analysis_interval_s <= 0:
            raise ValueError("analysis interval must be greater than zero")
        if self.first_read_timeout_s is not None and self.first_read_timeout_s <= 0:
            raise ValueError("first reading timeout must be greater than zero")
        if self.no_data_timeout_s is not None and self.no_data_timeout_s <= 0:
            raise ValueError("no-data timeout must be greater than zero")


@dataclass(frozen=True)
class SessionEvent:
    kind: str
    message: str = ""
    sample_count: int = 0
    latest_hz: float = 0.0
    elapsed_s: float = 0.0
    segment_index: int = 0
    session_dir: str = ""
    result: DeviationResult | None = None
    connection_fingerprint: ConnectionFingerprint | None = None
    parameter_fingerprint: ParameterFingerprint | None = None
    connection_test_id: int = 0
    parameter_checks: tuple[ParameterCheck, ...] = ()


class RecentSampleBuffer:
    """A bounded, chunked buffer that does not copy while acquisition appends."""

    def __init__(self, maximum_points: int) -> None:
        self.maximum_points = maximum_points
        self._chunks: deque[np.ndarray] = deque()
        self._size = 0
        self._total_count = 0
        self._segment_index = 0
        self._lock = threading.Lock()

    @property
    def total_count(self) -> int:
        with self._lock:
            return self._total_count

    @property
    def segment_index(self) -> int:
        with self._lock:
            return self._segment_index

    def append(self, values: np.ndarray) -> None:
        chunk = np.asarray(values, dtype=np.float64).copy()
        if not chunk.size:
            return
        with self._lock:
            self._chunks.append(chunk)
            self._size += int(chunk.size)
            self._total_count += int(chunk.size)
            while self._size > self.maximum_points and self._chunks:
                excess = self._size - self.maximum_points
                oldest = self._chunks[0]
                if oldest.size <= excess:
                    self._chunks.popleft()
                    self._size -= int(oldest.size)
                else:
                    self._chunks[0] = oldest[excess:].copy()
                    self._size -= excess

    def start_new_segment(self) -> None:
        """Discard the analysis window without resetting the global sample count."""
        with self._lock:
            self._chunks.clear()
            self._size = 0
            self._segment_index += 1

    def snapshot(
        self, last_points: int | None = None
    ) -> tuple[np.ndarray, int, int, int]:
        with self._lock:
            chunks = tuple(self._chunks)
            total = self._total_count
            size = self._size
            segment_index = self._segment_index
        if not chunks:
            return np.empty(0, dtype=np.float64), total, total, segment_index
        values = np.concatenate(chunks)
        if last_points is not None and values.size > last_points:
            values = values[-last_points:]
        start_index = total - int(values.size)
        return values, start_index, total, segment_index


class AcquisitionSession:
    def __init__(
        self,
        settings: SessionSettings,
        source: MeasurementSource,
        event_queue: queue.Queue[SessionEvent],
    ) -> None:
        settings.validate()
        self.settings = settings
        self.source = source
        self.events = event_queue
        self.buffer = RecentSampleBuffer(settings.analysis_window_points)
        self.stop_event = threading.Event()
        self.analysis_wakeup = threading.Event()
        self.session_dir: Path | None = None
        self.actual_gate_time_s = settings.instrument.gate_time_s
        self.started_at_utc = datetime.now(timezone.utc)
        self._acquisition_thread: threading.Thread | None = None
        self._analysis_thread: threading.Thread | None = None
        self._finished = threading.Event()
        self._segment_restarts_utc: list[str] = []
        self._acquisition_ready_at_utc: str | None = None
        self._first_reading_at_utc: str | None = None
        self._overflow_detected = False
        self._rejected_readings = 0
        self._finalization_errors: list[str] = []
        self._stop_requested = False
        self._stop_reason = ""

    @property
    def is_running(self) -> bool:
        return bool(self._acquisition_thread and self._acquisition_thread.is_alive())

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("session is already running")
        self.stop_event.clear()
        self.analysis_wakeup.clear()
        self.buffer = RecentSampleBuffer(self.settings.analysis_window_points)
        self.session_dir = None
        self.started_at_utc = datetime.now(timezone.utc)
        self._segment_restarts_utc = []
        self._acquisition_ready_at_utc = None
        self._first_reading_at_utc = None
        self._overflow_detected = False
        self._rejected_readings = 0
        self._finalization_errors = []
        self._stop_requested = False
        self._finished.clear()
        self._acquisition_thread = threading.Thread(
            target=self._acquisition_loop, name="frequency-acquisition", daemon=True
        )
        self._acquisition_thread.start()

    def stop(self) -> None:
        self._stop_requested = True
        self._stop_reason = "user_requested"
        self.stop_event.set()
        self.analysis_wakeup.set()
        try:
            self.source.cancel_io()
        except Exception as exc:
            self._finalization_errors.append(f"cancel I/O: {exc}")

    def wait(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    def _emit(self, event: SessionEvent) -> None:
        self.events.put(event)

    def _make_session_dir(self) -> Path:
        root = self.settings.output_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        stem = self.started_at_utc.astimezone().strftime("%Y%m%d_%H%M%S")
        candidate = root / stem
        suffix = 1
        while candidate.exists():
            candidate = root / f"{stem}_{suffix:02d}"
            suffix += 1
        candidate.mkdir()
        return candidate

    def _write_metadata(self, status: str, error: str = "") -> None:
        if self.session_dir is None:
            return
        metadata = {
            "application": "Python Real-time Stability Analyzer",
            "status": status,
            "stop_reason": self._stop_reason,
            "error": error,
            "started_at_utc": self.started_at_utc.isoformat(),
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "instrument_identity": self.source.identity,
            "instrument": asdict(self.settings.instrument),
            "actual_gate_time_s": self.actual_gate_time_s,
            "reference_hz": self.settings.reference_hz,
            "reference_mode": "analysis_window_mean"
            if self.settings.reference_hz == 0
            else "fixed",
            "analysis_window_points": self.settings.analysis_window_points,
            "total_samples": self.buffer.total_count,
            "simulated": self.settings.simulated,
            "connection_verified_at_utc": getattr(self.source, "connection_verified_at_utc", None),
            "identity": self._identity_metadata(),
            "acquisition_ready_at_utc": self._acquisition_ready_at_utc,
            "first_reading_at_utc": self._first_reading_at_utc,
            "overflow_detected": self._overflow_detected,
            "rejected_readings": self._rejected_readings,
            "finalization_errors": self._finalization_errors,
            "final_drain_skipped": bool(getattr(self.source, "io_cancelled", False)),
            "setup_errors": getattr(self.source, "setup_errors", []),
            "setup_warnings": getattr(self.source, "setup_warnings", []),
            "parameter_checks": [
                asdict(item) for item in getattr(self.source, "parameter_checks", [])
            ],
            "configuration_verified_at_utc": getattr(
                self.source, "configuration_verified_at_utc", None
            ),
            "input_diagnostics": getattr(self.source, "input_diagnostics", {}),
            "operation_condition": getattr(self.source, "operation_condition", None),
            "command_log": getattr(self.source, "command_log", []),
            "continuity_segment_count": len(self._segment_restarts_utc),
            "continuity_segment_started_at_utc": self._segment_restarts_utc,
            "maximum_samples_per_continuous_segment": self.source.max_samples_per_segment,
            "gap_free_across_segment_boundaries": False
            if self.source.max_samples_per_segment is not None
            else None,
            "time_columns": {
                "continuous_sample_elapsed_s": "global sample index multiplied by the actual gate time; restart gaps are excluded",
                "segment_elapsed_s": "sample index inside one gap-free segment multiplied by the actual gate time",
                "estimated_unix_s": "computer time immediately after INIT plus segment elapsed time",
                "received_unix_s": "computer UTC time when the batch was received",
            },
        }
        temporary = self.session_dir / "metadata.json.tmp"
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.session_dir / "metadata.json")

    def _identity_metadata(self) -> dict[str, str] | None:
        identity = getattr(self.source, "identity_fields", None)
        if identity is None:
            raw = getattr(self.source, "identity", "")
            return {"raw": raw} if raw else None
        return {
            "manufacturer": identity.manufacturer,
            "model": identity.model,
            "serial_number": identity.serial_number,
            "firmware": identity.firmware,
            "raw": identity.raw,
        }

    def _first_read_deadline(self) -> float:
        configured = self.settings.first_read_timeout_s
        return time.monotonic() + (
            configured
            if configured is not None
            else max(5.0, self.actual_gate_time_s * 3.0 + 2.0)
        )

    def _no_data_limit(self) -> float:
        configured = self.settings.no_data_timeout_s
        return configured if configured is not None else max(5.0, self.actual_gate_time_s * 3.0 + 2.0)

    def _validate_readings(self, readings: np.ndarray) -> np.ndarray:
        values = np.asarray(readings, dtype=np.float64)
        if values.ndim != 1:
            self._rejected_readings += int(values.size)
            raise RuntimeError("instrument returned a non-vector reading payload")
        invalid = ~np.isfinite(values) | (np.abs(values) >= 9e36)
        if np.any(invalid):
            self._rejected_readings += int(np.count_nonzero(invalid))
            raise RuntimeError(
                f"instrument returned {int(np.count_nonzero(invalid))} invalid/overload readings"
            )
        return values

    def _append_readings(
        self,
        csv_stream,
        readings: np.ndarray,
        segment_index: int,
        segment_sample_count: int,
        segment_started_unix: float,
    ) -> int:
        values = self._validate_readings(readings)
        if not values.size:
            return segment_sample_count
        first_index = self.buffer.total_count
        received_unix = time.time()
        lines = []
        for offset, reading in enumerate(values):
            index = first_index + offset
            segment_sample_index = segment_sample_count + offset
            continuous_elapsed = (index + 1) * self.actual_gate_time_s
            segment_elapsed = (segment_sample_index + 1) * self.actual_gate_time_s
            lines.append(
                f"{index},{segment_index},{segment_sample_index},"
                f"{continuous_elapsed:.15g},{segment_elapsed:.15g},"
                f"{segment_started_unix + segment_elapsed:.15f},"
                f"{received_unix:.15f},{reading:.15g}\n"
            )
        csv_stream.writelines(lines)
        csv_stream.flush()
        self.buffer.append(values)
        self.analysis_wakeup.set()
        return segment_sample_count + int(values.size)

    def _acquisition_loop(self) -> None:
        csv_stream = None
        error_message = ""
        final_status = "completed"
        segment_index = 0
        segment_sample_count = 0
        segment_started_unix = 0.0
        last_reading_monotonic: float | None = None
        try:
            self.session_dir = self._make_session_dir()
            identity = self.source.connect()
            self.actual_gate_time_s = self.source.configure()
            self._write_metadata("running")
            csv_path = self.session_dir / "measurements.csv"
            csv_stream = csv_path.open("w", encoding="ascii", newline="", buffering=1024 * 1024)
            csv_stream.write(
                "sample_index,continuity_segment,segment_sample_index,"
                "continuous_sample_elapsed_s,segment_elapsed_s,estimated_unix_s,"
                "received_unix_s,frequency_hz\n"
            )
            self.source.start()
            segment_started_unix = time.time()
            self._segment_restarts_utc.append(
                datetime.fromtimestamp(segment_started_unix, timezone.utc).isoformat()
            )
            self._analysis_thread = threading.Thread(
                target=self._analysis_loop, name="stability-analysis", daemon=True
            )
            self._analysis_thread.start()
            self._emit(SessionEvent("connected", message=identity, session_dir=str(self.session_dir)))

            last_status_time = 0.0
            # Check immediately after INIT, then continue polling at the interval.
            last_overflow_check = time.monotonic() - 2.0
            first_read_deadline = self._first_read_deadline()
            no_data_limit = self._no_data_limit()
            while not self.stop_event.is_set():
                readings = self.source.read_available()
                if readings.size:
                    segment_sample_count = self._append_readings(
                        csv_stream,
                        readings,
                        segment_index,
                        segment_sample_count,
                        segment_started_unix,
                    )
                    if self._first_reading_at_utc is None:
                        self._first_reading_at_utc = datetime.now(timezone.utc).isoformat()
                        self._acquisition_ready_at_utc = self._first_reading_at_utc
                        last_reading_monotonic = time.monotonic()
                        self._emit(
                            SessionEvent(
                                "started",
                                message=identity,
                                session_dir=str(self.session_dir),
                            )
                        )
                    else:
                        last_reading_monotonic = time.monotonic()

                    now = time.monotonic()
                    if now - last_status_time >= 0.25:
                        total = self.buffer.total_count
                        self._emit(
                            SessionEvent(
                                "progress",
                                sample_count=total,
                                latest_hz=float(readings[-1]),
                                elapsed_s=total * self.actual_gate_time_s,
                                segment_index=segment_index,
                                session_dir=str(self.session_dir),
                            )
                        )
                        last_status_time = now
                elif self._first_reading_at_utc is None:
                    if time.monotonic() >= first_read_deadline:
                        raise RuntimeError(
                            "measurement readiness timeout: no valid first reading received"
                        )
                    time.sleep(min(0.05, max(0.002, self.actual_gate_time_s / 4.0)))
                else:
                    if (
                        last_reading_monotonic is not None
                        and time.monotonic() - last_reading_monotonic >= no_data_limit
                    ):
                        raise RuntimeError(
                            f"no-data watchdog expired after {no_data_limit:.3g} seconds"
                        )
                    time.sleep(min(0.05, max(0.002, self.actual_gate_time_s / 4.0)))

                now = time.monotonic()
                if now - last_overflow_check >= 2.0:
                    if self.source.read_memory_overflow_event():
                        self._overflow_detected = True
                        raise RuntimeError(
                            "53230A reading memory overflowed; data continuity is no longer valid"
                        )
                    last_overflow_check = now

                segment_limit = self.source.max_samples_per_segment
                if (
                    segment_limit is not None
                    and segment_sample_count >= segment_limit
                    and not self.stop_event.is_set()
                ):
                    if segment_sample_count != segment_limit:
                        raise RuntimeError("continuous segment exceeded the instrument sample limit")
                    segment_index += 1
                    segment_sample_count = 0
                    self.buffer.start_new_segment()
                    latest_deviations = self.session_dir / "deviations_latest.csv"
                    latest_deviations.unlink(missing_ok=True)
                    self.source.start()
                    segment_started_unix = time.time()
                    self._segment_restarts_utc.append(
                        datetime.fromtimestamp(segment_started_unix, timezone.utc).isoformat()
                    )
                    self._emit(
                        SessionEvent(
                            "segment",
                            message="instrument started a new continuous segment",
                            sample_count=self.buffer.total_count,
                            segment_index=segment_index,
                            session_dir=str(self.session_dir),
                        )
                    )

            self.source.abort()
            if not getattr(self.source, "io_cancelled", False):
                while True:
                    readings = self.source.read_available()
                    if not readings.size:
                        break
                    segment_sample_count = self._append_readings(
                        csv_stream,
                        readings,
                        segment_index,
                        segment_sample_count,
                        segment_started_unix,
                    )
                csv_stream.flush()
        except Exception as exc:
            final_status = "completed" if (
                self._stop_requested and isinstance(exc, InstrumentCancelled)
            ) else "error"
            error_message = str(exc)
            if isinstance(exc, InstrumentCancelled) and self._stop_requested:
                error_message = ""
            elif not self._stop_requested:
                self._stop_reason = "error"
            if not isinstance(exc, InstrumentCancelled):
                self._emit(SessionEvent("error", message=error_message))
        finally:
            self.stop_event.set()
            self.analysis_wakeup.set()
            if self._analysis_thread is not None:
                try:
                    self._analysis_thread.join(timeout=max(5.0, self.settings.analysis_interval_s + 1.0))
                except Exception as exc:
                    self._finalization_errors.append(f"analysis thread join: {exc}")
            if csv_stream is not None:
                try:
                    csv_stream.close()
                except Exception as exc:
                    self._finalization_errors.append(f"close measurements.csv: {exc}")
            try:
                self.source.abort()
            except Exception as exc:
                self._finalization_errors.append(f"abort instrument: {exc}")
            try:
                self.source.close()
            except Exception as exc:
                self._finalization_errors.append(f"close instrument: {exc}")
            try:
                self._write_metadata(final_status, error_message)
            except Exception as exc:
                self._finalization_errors.append(f"write metadata: {exc}")
                try:
                    if self.session_dir is not None:
                        (self.session_dir / "finalization_errors.txt").write_text(
                            "\n".join(self._finalization_errors), encoding="utf-8"
                        )
                except Exception:
                    pass
            total = self.buffer.total_count
            try:
                self._emit(
                    SessionEvent(
                        "stopped",
                        message=error_message or "; ".join(self._finalization_errors),
                        sample_count=total,
                        elapsed_s=total * self.actual_gate_time_s,
                        segment_index=segment_index,
                        session_dir=str(self.session_dir or ""),
                    )
                )
            finally:
                self._finished.set()

    def _analysis_loop(self) -> None:
        last_analyzed_count = -1
        while True:
            self.analysis_wakeup.wait(self.settings.analysis_interval_s)
            self.analysis_wakeup.clear()
            if not self.stop_event.is_set():
                # Coalesce frequent acquisition notifications into one analysis interval.
                time.sleep(self.settings.analysis_interval_s)
            values, start_index, total_count, segment_index = self.buffer.snapshot()
            if values.size >= 10 and total_count != last_analyzed_count:
                try:
                    result = overlapping_deviations(
                        values,
                        self.actual_gate_time_s,
                        self.settings.reference_hz,
                        window_start_index=start_index,
                        continuity_segment=segment_index,
                    )
                    if self.buffer.segment_index != segment_index:
                        continue
                    if self.session_dir is not None:
                        result_path = self.session_dir / "deviations_latest.csv"
                        write_latest_deviations(result_path, result)
                        if self.buffer.segment_index != segment_index:
                            result_path.unlink(missing_ok=True)
                            continue
                    self._emit(SessionEvent("analysis", result=result))
                    last_analyzed_count = total_count
                except Exception as exc:
                    self._emit(SessionEvent("analysis_error", message=str(exc)))
            if self.stop_event.is_set():
                return
