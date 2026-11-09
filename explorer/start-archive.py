import runpy
import subprocess
import sys

sys.path.insert(0, "/app")
from collector import server as collector
import daily_archive

original_run = collector.run


def run():
    collector.Handler = daily_archive.handler(collector.Handler)
    # Daily archival owns expiry. The original time-only purge is disabled.
    collector.prune = lambda *_: None
    worker = subprocess.Popen([sys.executable, "-u", "/painel/daily_archive.py"])
    try:
        original_run()
    finally:
        worker.terminate()
        worker.wait(timeout=10)


collector.run = run
runpy.run_path("/painel/start-collector.py", run_name="__main__")
