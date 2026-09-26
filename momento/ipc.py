"""Daemon control socket: newline-delimited JSON over a Unix stream socket.

One request per line, one reply per line. A connection may carry several
requests in sequence. Replies may be produced asynchronously by the handler.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger(__name__)

MAX_LINE = 1 << 20


class DaemonNotRunning(ConnectionError):
    pass


class IPCError(RuntimeError):
    pass


def _read_line(f) -> bytes:
    line = f.readline(MAX_LINE)
    return line


def request(msg: dict, timeout: float = 120, path: str | Path | None = None) -> dict:
    """Send one request, wait for the reply."""
    path = str(path or config.SOCKET_PATH)
    if not os.path.exists(path):
        raise DaemonNotRunning(f"no daemon socket at {path}")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(path)
        except (ConnectionRefusedError, FileNotFoundError) as e:
            raise DaemonNotRunning(f"daemon not running ({e})") from e
        sock.sendall(json.dumps(msg).encode() + b"\n")
        with sock.makefile("rb") as f:
            line = _read_line(f)
        if not line:
            raise IPCError("daemon closed the connection without replying")
        return json.loads(line)
    except socket.timeout as e:
        raise IPCError("timed out waiting for the daemon") from e
    finally:
        sock.close()


Handler = Callable[[dict, Callable[[dict], None]], None]


class Server:
    """Accepts connections in background threads; handler runs in the GLib main loop.

    handler(msg, reply) is invoked via GLib.idle_add; reply(dict) may be called
    at any later time (from any thread) exactly once.
    """

    def __init__(self, path: str | Path, handler: Handler):
        self.path = str(path)
        self.handler = handler
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    def start(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._remove_stale()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(self.path)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        sock.listen(16)
        self._sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name="ipc-accept", daemon=True)
        self._thread.start()
        log.debug("listening on %s", self.path)

    def _remove_stale(self) -> None:
        if not os.path.exists(self.path):
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(self.path)
        except OSError:
            os.unlink(self.path)  # stale
            return
        finally:
            probe.close()
        raise RuntimeError(f"another daemon is already listening on {self.path}")

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), name="ipc-conn", daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        from gi.repository import GLib

        try:
            with conn, conn.makefile("rb") as f:
                while not self._closed.is_set():
                    line = _read_line(f)
                    if not line:
                        return
                    try:
                        msg = json.loads(line)
                        if not isinstance(msg, dict):
                            raise ValueError("request must be a JSON object")
                    except ValueError as e:
                        self._send(conn, {"ok": False, "error": f"bad request: {e}"})
                        continue
                    done = threading.Event()
                    box: list[dict] = []

                    def reply(result: dict, _box=box, _done=done) -> None:
                        if not _done.is_set():
                            _box.append(result)
                            _done.set()

                    def dispatch(_msg=msg, _reply=reply) -> bool:
                        try:
                            self.handler(_msg, _reply)
                        except Exception as e:  # noqa: BLE001
                            log.exception("ipc handler failed")
                            _reply({"ok": False, "error": str(e)})
                        return False

                    GLib.idle_add(dispatch)
                    while not done.wait(0.5):
                        if self._closed.is_set():
                            return
                    self._send(conn, box[0])
        except OSError as e:
            log.debug("ipc connection error: %s", e)

    @staticmethod
    def _send(conn: socket.socket, result: dict) -> None:
        conn.sendall(json.dumps(result).encode() + b"\n")
