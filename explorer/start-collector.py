import logging
import ssl
import sys
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, "/app")
from collector import server as collector
import server as dashboard


def tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain("/tls/cert.pem", "/tls/key.pem")
    return context


class HTTPS(ThreadingHTTPServer):
    def __init__(self, address, handler):
        self.context = tls_context()
        super().__init__(address, handler)

    def finish_request(self, request, address):
        request.settimeout(10)
        try:
            with self.context.wrap_socket(request, server_side=True) as connection:
                super().finish_request(connection, address)
        except (ssl.SSLError, TimeoutError):
            pass


class Handler(dashboard.Handler):
    def do_GET(self):
        if urlsplit(self.path).path == "/healthz":
            return self.send_json(200, {"ok": True})
        return super().do_GET()


if "--check" in sys.argv:
    tls_context()
    dashboard.check()
else:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    collector.Handler = Handler
    collector.ThreadingHTTPServer = HTTPS
    collector.run()
