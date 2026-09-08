#!/usr/bin/env python3
"""Small loopback-only TCP forwarder for a trusted LAN model server."""

from __future__ import annotations

import argparse
import ipaddress
import select
import socket
import socketserver


class ForwardHandler(socketserver.BaseRequestHandler):
    remote_host = "127.0.0.1"
    remote_port = 1234

    def handle(self) -> None:
        with socket.create_connection((self.remote_host, self.remote_port), timeout=15) as upstream:
            peers = (self.request, upstream)
            while True:
                readable, _, _ = select.select(peers, [], [], 60)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.request else self.request).sendall(data)


class LoopbackServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-port", type=int, default=1234)
    parser.add_argument("--remote-host", required=True)
    parser.add_argument("--remote-port", type=int, default=1234)
    args = parser.parse_args()
    remote = ipaddress.ip_address(args.remote_host)
    if not (remote.is_private or remote.is_loopback):
        raise SystemExit("Remote model server must use a private or loopback IP address")
    ForwardHandler.remote_host = args.remote_host
    ForwardHandler.remote_port = args.remote_port
    with LoopbackServer(("127.0.0.1", args.listen_port), ForwardHandler) as server:
        print(f"Model proxy: 127.0.0.1:{args.listen_port} -> {args.remote_host}:{args.remote_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
