import http.client
import os
import socket
import ssl
from pathlib import Path


def check():
    host = os.environ.get("TLS_VERIFY_HOST") or os.environ.get("NETFLOW_WEB_BIND") or "127.0.0.1"
    ca = Path("/tls/ca.pem")
    context = ssl.create_default_context(cafile=str(ca if ca.is_file() else Path("/tls/cert.pem")))
    with socket.create_connection(("127.0.0.1", 8000), timeout=2) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
            with http.client.HTTPResponse(tls) as response:
                response.begin()
                if response.status != 200:
                    raise SystemExit(f"HTTPS health check returned HTTP {response.status}")


if __name__ == "__main__":
    check()
