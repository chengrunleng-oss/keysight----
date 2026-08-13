from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionFingerprint:
    """The settings that determine which endpoint a connection test approved."""

    simulated: bool
    host: str
    port: str


@dataclass(frozen=True)
class ParameterFingerprint:
    connection: ConnectionFingerprint
    channel: str
    gate_time_s: str
    impedance_ohm: str


def make_fingerprint(simulated: bool, host: str, port: str) -> ConnectionFingerprint:
    return ConnectionFingerprint(
        simulated=bool(simulated),
        host=host.strip().lower(),
        port=port.strip(),
    )


def make_parameter_fingerprint(
    connection: ConnectionFingerprint,
    channel: str,
    gate_time_s: str,
    impedance_ohm: str,
) -> ParameterFingerprint:
    try:
        normalized_gate = format(float(gate_time_s.strip()), ".15g")
    except ValueError:
        normalized_gate = gate_time_s.strip()
    return ParameterFingerprint(
        connection=connection,
        channel=channel.strip(),
        gate_time_s=normalized_gate,
        impedance_ohm=impedance_ohm.strip().lower().replace(" ", ""),
    )


class ConnectionGate:
    """Tracks whether the current connection settings passed a test."""

    def __init__(self) -> None:
        self._verified: ConnectionFingerprint | None = None
        self._testing = False
        self._test_id = 0
        self._pending: ConnectionFingerprint | None = None
        self._parameters_verified: ParameterFingerprint | None = None
        self._parameters_testing = False
        self._parameter_test_id = 0
        self._pending_parameters: ParameterFingerprint | None = None

    @property
    def testing(self) -> bool:
        return self._testing

    @property
    def parameters_testing(self) -> bool:
        return self._parameters_testing

    @property
    def verified_fingerprint(self) -> ConnectionFingerprint | None:
        return self._verified

    @property
    def verified_parameter_fingerprint(self) -> ParameterFingerprint | None:
        return self._parameters_verified

    def invalidate(self) -> None:
        self._verified = None
        self._testing = False
        self._test_id += 1
        self._pending = None
        self.invalidate_parameters()

    def invalidate_parameters(self) -> None:
        self._parameters_verified = None
        self._parameters_testing = False
        self._parameter_test_id += 1
        self._pending_parameters = None

    def begin_test(self, fingerprint: ConnectionFingerprint) -> int:
        self.invalidate_parameters()
        self._test_id += 1
        self._testing = True
        self._pending = fingerprint
        # A test result is only valid for the exact settings it was run with.
        if self._verified != fingerprint:
            self._verified = None
        return self._test_id

    def complete_success(self, fingerprint: ConnectionFingerprint, test_id: int) -> bool:
        if test_id != self._test_id or fingerprint != self._pending:
            return False
        self._testing = False
        self._verified = fingerprint
        self._pending = None
        return True

    def complete_failure(self, test_id: int) -> bool:
        if test_id != self._test_id:
            return False
        self._testing = False
        self._verified = None
        self._pending = None
        self.invalidate_parameters()
        return True

    def is_verified(self, fingerprint: ConnectionFingerprint) -> bool:
        return not self._testing and self._verified == fingerprint

    def begin_parameter_test(self, fingerprint: ParameterFingerprint) -> int:
        self._parameter_test_id += 1
        self._parameters_testing = True
        self._pending_parameters = fingerprint
        self._parameters_verified = None
        return self._parameter_test_id

    def complete_parameter_success(
        self, fingerprint: ParameterFingerprint, test_id: int
    ) -> bool:
        if (
            test_id != self._parameter_test_id
            or fingerprint != self._pending_parameters
            or not self.is_verified(fingerprint.connection)
        ):
            return False
        self._parameters_testing = False
        self._parameters_verified = fingerprint
        self._pending_parameters = None
        return True

    def complete_parameter_failure(self, test_id: int) -> bool:
        if test_id != self._parameter_test_id:
            return False
        self._parameters_testing = False
        self._parameters_verified = None
        self._pending_parameters = None
        return True

    def is_parameter_verified(
        self, connection: ConnectionFingerprint, fingerprint: ParameterFingerprint
    ) -> bool:
        return (
            not self._parameters_testing
            and self.is_verified(connection)
            and self._parameters_verified == fingerprint
        )
