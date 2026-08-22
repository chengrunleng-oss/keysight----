"""TCP connection and raw SCPI communication."""

from __future__ import annotations

import socket


class ScpiConnection:
    """Send and receive SCPI messages through the instrument's TCP port."""

    def __init__(self, host: str, port: int = 5025, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._buffer = bytearray()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> str:
        """Connect and return the instrument identification string."""
        self.close()
        self._socket = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        self._socket.settimeout(self.timeout)
        try:
            return self.query("*IDN?")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
        self._socket = None
        self._buffer.clear()

    def write(self, command: str) -> None:
        """Send one SCPI command."""
        command = command.strip()
        if not command:
            raise ValueError("SCPI command cannot be empty")
        self._require_socket().sendall((command + "\n").encode("ascii"))

    def write_many(self, *commands: str) -> None:
        """Send several SCPI commands in one TCP packet."""
        cleaned = [command.strip() for command in commands if command.strip()]
        if not cleaned:
            return
        payload = "\n".join(cleaned) + "\n"
        self._require_socket().sendall(payload.encode("ascii"))

    def query(self, command: str, timeout: float | None = None) -> str:
        """Send one SCPI query and return one response line."""
        instrument_socket = self._require_socket()
        previous_timeout = instrument_socket.gettimeout()
        if timeout is not None:
            if timeout <= 0:
                raise ValueError("timeout must be greater than 0")
            instrument_socket.settimeout(timeout)
        try:
            self.write(command)
            return self._readline()
        except TimeoutError:
            self.close()
            raise
        finally:
            if self._socket is instrument_socket:
                instrument_socket.settimeout(previous_timeout)

    def _readline(self) -> str:
        instrument_socket = self._require_socket()
        while b"\n" not in self._buffer:
            chunk = instrument_socket.recv(4096)
            if not chunk:
                self.close()
                raise ConnectionError("instrument closed the connection")
            self._buffer.extend(chunk)

        line, _, remainder = self._buffer.partition(b"\n")
        self._buffer = bytearray(remainder)
        return line.rstrip(b"\r").decode("ascii")

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise RuntimeError("instrument is not connected")
        return self._socket
