import os
import runpy
import subprocess
import sys

sys.path.insert(0, "/app")
from collector import server as collector
import daily_archive

original_run = collector.run


def run():
    load_applications = daily_archive.panel.ApplicationNames
    if os.environ.get("CLICKHOUSE_URL"):
        from collector.clickhouse import ClickHouseError, Client, Writer
        import clickhouse_view

        def load_applications():
            try:
                return clickhouse_view.load_application_names(Client(timeout=3))
            except ClickHouseError as exc:
                clickhouse_view.LOG.warning("Application names unavailable for archive: %s", exc)
                return daily_archive.panel.ApplicationNames()

        storage = Writer()
        base = clickhouse_view.handler(collector.Handler, storage)
        script = "clickhouse_archive.py"
        # Archived and live queries share one dashboard slot on the pilot host.
        daily_archive.panel.QUERIES = clickhouse_view.QUERIES
    else:
        storage = None
        base = collector.Handler
        script = "daily_archive.py"
        # Daily archival owns expiry. The original time-only purge is disabled.
        collector.prune = lambda *_: None

    collector.Handler = daily_archive.handler(base, load_applications)
    worker = subprocess.Popen([sys.executable, "-u", f"/painel/{script}"])
    try:
        original_run(storage)
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()


collector.run = run
runpy.run_path("/painel/start-collector.py", run_name="__main__")
