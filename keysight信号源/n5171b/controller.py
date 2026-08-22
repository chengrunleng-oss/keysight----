"""Unified N5171B controller composed from independent modules."""

from __future__ import annotations

from .connection import ScpiConnection
from .configs import ext_hardware
from .list_sweep import ListSweepController
from .output import OutputController


Keysight_ip = ext_hardware["Keysight N5171B"]["ip_address"]


class N5171B:
    """Convenient entry point for connection, output and sweep control."""

    def __init__(self, host: str = Keysight_ip, port: int = 5025, timeout: float = 5.0) -> None:
        self.scpi = ScpiConnection(host, port, timeout)
        self.output = OutputController(self.scpi)
        self.list_sweep = ListSweepController(self.scpi)

    def connect(self) -> str:
        return self.scpi.connect()

    def close(self) -> None:
        self.scpi.close()

    def __enter__(self) -> N5171B:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
