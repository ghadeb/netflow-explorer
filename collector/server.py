"""Run both GoFlow2 receivers, one SQLite writer and the read-only dashboard API."""
from collections import Counter
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import json
import logging
from mimetypes import guess_type
import os
from pathlib import Path
from queue import Empty, Queue
import signal
import sqlite3
from subprocess import PIPE, Popen, TimeoutExpired
from threading import BoundedSemaphore, Event, Thread
from time import monotonic, time
from urllib.parse import parse_qs, urlsplit

import dns.exception
import dns.resolver

from collector.storage import connect, dashboard, ingest, open_db, prune

LOG = logging.getLogger("fluxo")
STATIC = Path(os.getenv("WEB_ROOT", "/app/web/dist"))
DB = Path(os.getenv("FLOW_DB", "/data/flows.sqlite3"))
PORT_SERVICES = json.loads(Path(__file__).with_name("port-services.json").read_text())
DASHBOARD_SLOTS = BoundedSemaphore(4)
PTR_SLOTS = BoundedSemaphore(8)


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *_args):
        # URL filters can contain network addresses. Keep them out of the logs.
        pass

    def send_payload(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, value):
        self.send_payload(status, "application/json; charset=utf-8", json.dumps(value).encode())

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/healthz":
            return self.send_json(200, {"ok": True})
        if url.path == "/api/dashboard":
            return self.get_dashboard(parse_qs(url.query))
        if url.path == "/":
            file = STATIC / "index.html"
        elif url.path.startswith("/assets/") and "/" not in url.path[len("/assets/"):]:
            file = STATIC / "assets" / url.path.rsplit("/", 1)[1]
        else:
            return self.send_json(404, {"error": "Recurso inexistente"})
        try:
            data = file.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            return self.send_json(404, {"error": "Recurso inexistente"})
        self.send_payload(200, guess_type(file)[0] or "application/octet-stream", data)

    def get_dashboard(self, query):
        if not DASHBOARD_SLOTS.acquire(blocking=False):
            return self.send_json(503, {"error": "O painel está ocupado. Tenta novamente."})
        try:
            minutes = int(query.get("minutes", ["60"])[0])
            router = query.get("router", [None])[0]
            prefix = int(query["prefix"][0]) if "prefix" in query else None
            detail = query.get("ports", ["0"])[0] == "1"
            now = int(time())
            deadline = monotonic() + 8
            with closing(connect(DB)) as db:
                db.set_progress_handler(lambda: monotonic() >= deadline, 10_000)
                data = dashboard(db, now, minutes, router, detail=detail, prefix=prefix)
            data["generated_at"] = now
            data["routers"] = sorted(set(data["routers"]) | self.server.exporters)
            if detail:
                for row in data["talkers"]:
                    row["service"] = PORT_SERVICES.get(f"{row['protocol']}/{row['port']}")
        except ValueError:
            return self.send_json(400, {"error": "Filtro inválido"})
        except sqlite3.Error:
            LOG.exception("Falha na consulta do painel")
            return self.send_json(503, {"error": "Consulta indisponível. Experimenta um intervalo menor."})
        finally:
            DASHBOARD_SLOTS.release()
        self.send_json(200, data)

    def do_POST(self):
        if self.path != "/api/ptr":
            return self.send_json(404, {"error": "Recurso inexistente"})
        if self.headers.get_content_type() != "application/json":
            return self.send_json(415, {"error": "Pedido inválido"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 128:
                raise ValueError
            ip = str(IPv4Address(json.loads(self.rfile.read(length))["ip"]))
        except (ValueError, TypeError, KeyError, TimeoutError):
            return self.send_json(400, {"error": "Endereço inválido"})
        if not PTR_SLOTS.acquire(blocking=False):
            return self.send_json(429, {"error": "O DNS está ocupado. Tenta novamente."})
        try:
            answer = self.server.resolver.resolve_address(ip, lifetime=2)
            return self.send_json(200, {"name": str(answer[0].target).rstrip(".")})
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return self.send_json(200, {"name": None})
        except dns.exception.DNSException:
            return self.send_json(502, {"error": "Não foi possível consultar o DNS."})
        finally:
            PTR_SLOTS.release()


def reader(proc, direction, events):
    with proc.stdout:
        for line in proc.stdout:
            events.put((direction, line))


def get_batch(events, limit=2000, interval=0.5):
    batch = []
    deadline = monotonic() + interval
    while len(batch) < limit:
        wait = deadline - monotonic()
        if wait <= 0:
            break
        try:
            batch.append(events.get(timeout=wait))
        except Empty:
            break
    return batch


def run(storage=None):
    exporters = {str(IPv4Address(ip.strip())) for ip in os.environ["FLOW_EXPORTERS"].split(",")}
    days = int(os.getenv("RETENTION_DAYS", "30"))
    if not 1 <= days <= 365:
        raise ValueError("RETENTION_DAYS deve estar entre 1 e 365")
    dns_servers = [ip.strip() for ip in os.getenv("PTR_DNS", "").split(",") if ip.strip()]
    resolver = dns.resolver.Resolver(configure=not dns_servers)
    if dns_servers:
        resolver.nameservers = dns_servers
    resolver.cache = dns.resolver.LRUCache(2048)
    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())

    events = Queue(maxsize=16_384)
    processes = []
    http = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    http.exporters, http.resolver = exporters, resolver
    stats = Counter()
    with closing(open_db(DB) if storage is None else storage) as db:
        if storage is None:
            def write_batch(batch):
                return ingest(db, batch, exporters)
            batch_options = {}
            engine = "SQLite"
        else:
            def write_batch(batch):
                return storage.ingest(batch, exporters, stopped)
            batch_options = {"limit": 10000, "interval": 1}
            engine = "ClickHouse"
        try:
            for direction, port, metrics in (("up", 2055, 8081), ("down", 2056, 8082)):
                decoder = "goflow2-nbar" if storage is not None else "goflow2"
                proc = Popen([
                    decoder, "-listen", f"netflow://0.0.0.0:{port}",
                    "-addr", f"127.0.0.1:{metrics}", "-format", "json",
                    "-transport", "file", "-loglevel", "warn",
                ], stdout=PIPE, text=True)
                thread = Thread(target=reader, args=(proc, direction, events), daemon=True)
                thread.start()
                processes.append((proc, thread))
            Thread(target=http.serve_forever, daemon=True).start()
            maintenance = 0
            while not stopped.is_set():
                if any(proc.poll() is not None or not thread.is_alive() for proc, thread in processes):
                    raise RuntimeError("Um receptor GoFlow2 terminou.")
                stats["queue_peak"] = max(stats["queue_peak"], events.qsize())
                batch = get_batch(events, **batch_options)
                if batch:
                    stats["accepted"] += write_batch(batch)
                    stats["seen"] += len(batch)
                if monotonic() >= maintenance:
                    if storage is None:
                        prune(db, (int(time()) // 60) * 60 - days * 86400)
                    if stats["seen"]:
                        LOG.info("%s: %d registos guardados, %d ignorados, fila_max=%d/%d",
                                 engine, stats["accepted"], stats["seen"] - stats["accepted"],
                                 stats["queue_peak"], events.maxsize)
                    stats.clear()
                    maintenance = monotonic() + 60
        finally:
            for proc, _ in processes:
                if proc.poll() is None:
                    proc.terminate()
            if stopped.is_set():
                while any(thread.is_alive() for _, thread in processes) or not events.empty():
                    batch = get_batch(events, **batch_options)
                    if batch:
                        write_batch(batch)
                    if any(proc.poll() is None for proc, _ in processes):
                        # In the absence of further output, do not let a stuck child delay shutdown.
                        for proc, _ in processes:
                            if proc.poll() is None and not batch:
                                proc.kill()
            for proc, _ in processes:
                try:
                    proc.wait(timeout=2)
                except TimeoutExpired:
                    proc.kill()
                    proc.wait()
            http.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
