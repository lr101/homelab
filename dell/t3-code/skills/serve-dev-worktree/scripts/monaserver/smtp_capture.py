#!/usr/bin/env python3
"""Capture preview SMTP messages locally without relaying them."""

import argparse
import os
import socketserver
import time
import uuid
from pathlib import Path


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 monaserver-preview ESMTP\r\n")
        self.wfile.flush()
        message = []
        awaiting_auth_response = False
        while True:
            raw_line = self.rfile.readline(65536)
            if not raw_line:
                return
            line = raw_line.rstrip(b"\r\n")
            command = line.decode("ascii", errors="replace")
            if awaiting_auth_response:
                awaiting_auth_response = False
                self.wfile.write(b"235 2.7.0 Authentication accepted\r\n")
                self.wfile.flush()
                continue
            verb, _, argument = command.partition(" ")
            verb = verb.upper()

            if verb in ("EHLO", "HELO"):
                self.wfile.write(b"250-monaserver-preview\r\n250 AUTH PLAIN\r\n")
            elif verb == "AUTH":
                # This service is bound to loopback and does not relay mail.
                if argument.upper() == "PLAIN":
                    awaiting_auth_response = True
                    self.wfile.write(b"334 \r\n")
                elif argument.upper().startswith("PLAIN "):
                    self.wfile.write(b"235 2.7.0 Authentication accepted\r\n")
                else:
                    self.wfile.write(b"504 5.5.4 authentication mechanism not supported\r\n")
            elif verb == "RCPT":
                recipient = argument.split(":", 1)[-1].strip().split()[0].strip("<>").lower()
                domain = recipient.rpartition("@")[2]
                if "@" in recipient and domain.endswith(".invalid"):
                    self.wfile.write(b"250 2.0.0 accepted\r\n")
                else:
                    self.wfile.write(b"550 5.7.1 preview capture only accepts .invalid recipients\r\n")
            elif verb in ("MAIL", "RSET", "NOOP"):
                self.wfile.write(b"250 2.0.0 accepted\r\n")
            elif verb == "DATA":
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                self.wfile.flush()
                message = []
                while True:
                    data_line = self.rfile.readline(65536)
                    if not data_line:
                        return
                    normalized = data_line.rstrip(b"\r\n")
                    if normalized == b".":
                        break
                    if normalized.startswith(b".."):
                        normalized = normalized[1:]
                    message.append(normalized)
                self._store_message(b"\r\n".join(message) + b"\r\n")
                self.wfile.write(b"250 2.0.0 captured locally\r\n")
            elif verb == "QUIT":
                self.wfile.write(b"221 2.0.0 bye\r\n")
                self.wfile.flush()
                return
            else:
                self.wfile.write(b"502 5.5.1 command not supported\r\n")
            self.wfile.flush()

    def _store_message(self, payload):
        spool = Path(self.server.spool_dir)
        name = f"{time.time_ns()}-{uuid.uuid4().hex}.eml"
        temporary = spool / f".{name}.tmp"
        target = spool / name
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, target)


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, spool_dir):
        self.spool_dir = spool_dir
        super().__init__(address, handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--spool-dir", required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    spool = Path(args.spool_dir)
    if spool.is_symlink():
        parser.error("spool directory must not be a symlink")
    spool.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(spool, 0o700)
    server = SMTPServer((args.host, args.port), SMTPHandler, str(spool))
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
