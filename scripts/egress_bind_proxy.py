"""HTTP CONNECT proxy that binds outbound sockets to a fixed source IP.

Run on the Docker *host* (not inside a container) so secondary Elastic IPs
on eth0 are reachable:

  python3 scripts/egress_bind_proxy.py --bind 65.109.255.239 --listen 0.0.0.0:18901

Containers reach it via the docker bridge gateway, e.g.:
  AK07_EGRESS_PROXY=http://172.19.0.1:18901
"""

from __future__ import annotations

import argparse
import select
import socket
import socketserver


class _ConnectHandler(socketserver.StreamRequestHandler):
    bind_ip: str = "0.0.0.0"

    def handle(self) -> None:
        try:
            first = self.rfile.readline(65537)
        except OSError:
            return
        if not first:
            return
        line = first.decode("latin-1", errors="replace").strip()
        parts = line.split()
        if len(parts) < 3 or parts[0].upper() != "CONNECT":
            self._reply(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host_port = parts[1]
        if ":" not in host_port:
            self._reply(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        host, _, port_s = host_port.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            self._reply(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        # Drain proxy headers
        while True:
            hdr = self.rfile.readline(65537)
            if not hdr or hdr in (b"\r\n", b"\n"):
                break

        remote: socket.socket | None = None
        try:
            remote = socket.create_connection((host, port), timeout=30, source_address=(self.bind_ip, 0))
        except OSError as exc:
            self._reply(f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(str(exc))}\r\n\r\n{exc}".encode())
            return

        self._reply(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        assert remote is not None
        self._pipe(self.connection, remote)
        try:
            remote.close()
        except OSError:
            pass

    def _reply(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            pass

    @staticmethod
    def _pipe(client: socket.socket, remote: socket.socket) -> None:
        sockets = [client, remote]
        try:
            while True:
                readable, _, errored = select.select(sockets, [], sockets, 120)
                if errored or not readable:
                    break
                for sock in readable:
                    other = remote if sock is client else client
                    try:
                        data = sock.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        except (OSError, ValueError):
            return


class _ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="SEBI egress CONNECT proxy with source IP bind")
    parser.add_argument("--bind", required=True, help="Outbound source IP (secondary static IP)")
    parser.add_argument("--listen", default="0.0.0.0:18901", help="Listen host:port on the host")
    args = parser.parse_args()
    listen_host, _, listen_port_s = args.listen.rpartition(":")
    listen_host = listen_host or "0.0.0.0"
    listen_port = int(listen_port_s)

    class Handler(_ConnectHandler):
        bind_ip = args.bind

    server = _ThreadedServer((listen_host, listen_port), Handler)
    print(
        f"egress proxy listening on {listen_host}:{listen_port} "
        f"outbound bind={args.bind} (Ctrl+C to stop)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", flush=True)


if __name__ == "__main__":
    main()
