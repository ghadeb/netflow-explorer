"""Isolated UDP and SQLite integration check using the collector's legacy HTTP API."""
from contextlib import closing
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
from time import monotonic, sleep, time
from urllib.request import urlopen

from collector.storage import connect


def packet(sequence, source, destination, port, size):
    fields = [(8, 4), (12, 4), (4, 1), (7, 2), (11, 2), (1, 8), (2, 8)]
    template = struct.pack("!HHHH", 0, 8 + 4 * len(fields), 256, len(fields))
    template += b"".join(struct.pack("!HH", *field) for field in fields)
    record = struct.pack("!4s4sBHHQQ", IPv4Address(source).packed, IPv4Address(destination).packed,
                         6, 45000, port, size, 1)
    record += bytes((-len(record)) % 4)
    data = struct.pack("!HH", 256, len(record) + 4) + record
    return struct.pack("!HHIIII", 9, 2, 60000, int(time()), sequence, 1) + template + data


def main():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "smoke.sqlite3"
        env = dict(os.environ, FLOW_DB=str(path), FLOW_EXPORTERS="127.0.0.1,127.0.0.2")
        proc = subprocess.Popen([sys.executable, "-m", "collector.server"], env=env)
        try:
            deadline = monotonic() + 12
            while monotonic() < deadline:
                try:
                    for port in (8000, 8081, 8082):
                        endpoint = "healthz" if port == 8000 else "metrics"
                        with urlopen(f"http://127.0.0.1:{port}/{endpoint}", timeout=1):
                            pass
                    break
                except OSError:
                    if proc.poll() is not None:
                        raise RuntimeError(f"collector exited with {proc.returncode}")
                    sleep(0.1)
            else:
                raise RuntimeError("collector did not become ready")
            pairs = [
                ("127.0.0.1", 2055, "203.0.113.25", "198.51.100.9", 443, 4000),
                ("127.0.0.2", 2056, "198.51.100.9", "203.0.113.25", 55000, 30000),
            ]
            for sender, port, source, destination, dst_port, size in pairs:
                with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as udp:
                    udp.bind((sender, 0))
                    udp.sendto(packet(1, source, destination, dst_port, size), ("127.0.0.1", port))
            deadline = monotonic() + 10
            with closing(connect(path)) as db:
                while monotonic() < deadline:
                    rows = db.execute("SELECT router, direction, client, bytes FROM records ORDER BY router").fetchall()
                    if len(rows) >= 2:
                        break
                    sleep(0.1)
                assert [tuple(row) for row in rows] == [
                    ("127.0.0.1", "up", "203.0.113.25", 4000),
                    ("127.0.0.2", "down", "203.0.113.25", 30000),
                ], [tuple(row) for row in rows]
            with urlopen("http://127.0.0.1:8000/api/dashboard?ports=1", timeout=2) as response:
                view = json.load(response)
            assert view["totals"] == {"up": 4000, "down": 30000, "flows": 2}, view
            assert {(row["port"], row["service"]) for row in view["talkers"]} == {(443, "https"), (55000, None)}
            print("OK: NetFlow v9 UDP → GoFlow2 → SQLite → API")
        finally:
            proc.terminate()
            proc.wait(timeout=8)


if __name__ == "__main__":
    main()
