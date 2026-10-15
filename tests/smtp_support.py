"""Loopback TCP SMTP fixture: accepts bytes and can deliberately lose the final ACK."""

import socketserver
import threading
from collections import deque
from contextlib import contextmanager

from app.config import Settings


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        server = self.server
        with server.lock:
            mode = server.modes.popleft() if server.modes else "accept"
            server.connections += 1
        self.wfile.write(b"220 fixture ESMTP\r\n")
        while line := self.rfile.readline(8192):
            command = line.split(b" ", 1)[0].strip().upper()
            if command in {b"EHLO", b"HELO", b"MAIL", b"RCPT", b"RSET", b"NOOP"}:
                if command == b"RCPT" and server.before_data:
                    server.before_data()
                if command == b"MAIL" and mode in {"temporary", "reject", "drop_before"}:
                    if mode == "drop_before":
                        return
                    self.wfile.write(
                        b"451 fixture temporary\r\n"
                        if mode == "temporary"
                        else b"550 fixture rejected\r\n"
                    )
                else:
                    self.wfile.write(b"250 fixture OK\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 send data\r\n")
                parts = []
                while chunk := self.rfile.readline(2_000_001):
                    if chunk == b".\r\n":
                        break
                    parts.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                with server.lock:
                    server.messages.append(b"".join(parts))
                if mode == "drop_after":
                    return
                self.wfile.write(b"250 fixture accepted\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 goodbye\r\n")
                return
            else:
                self.wfile.write(b"500 unsupported\r\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def smtp_server(*modes, before_data=None):
    with Server(("127.0.0.1", 0), Handler) as server:
        server.lock = threading.Lock()
        server.modes = deque(modes)
        server.messages = []
        server.connections = 0
        server.before_data = before_data
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


def smtp_settings(directory, port, **changes):
    return Settings(
        _env_file=None,
        data_dir=directory,
        session_secret="fixture-session-secret-" * 3,
        smtp_enabled=True,
        smtp_host="127.0.0.1",
        smtp_port=port,
        smtp_tls_mode="none",
        smtp_from="sender@example.com",
        smtp_test_recipient="reader@example.com",
        mail_retry_seconds=1,
        **changes,
    )
