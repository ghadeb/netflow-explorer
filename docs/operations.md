# NetFlow Explorer operations

[Back to NetFlow Explorer](../README.md)

## Configure an exporter

Send IPv4 NetFlow v9 data records and templates from an address allowed by `FLOW_EXPORTERS` to the collector's UDP bind address. This deployment uses UDP 2055 for traffic sent from the monitored network and UDP 2056 for traffic received by it. If the exporter uses a VRF, check the route to the collector from that VRF. Allow the two UDP ports from the expected source addresses.

### Cisco Flexible NetFlow example

Set `<COLLECTOR-IP>` to the IPv4 address in `NETFLOW_UDP_BIND`, `<EXPORT-SOURCE>` to an existing source interface, and `<MONITORED-INTERFACE>` to the Layer 3 interface facing the monitored network. Verify that the exporter, record and monitor commands are available on the device.

```text
configure terminal
 flow exporter FLUXO-UP
  destination <COLLECTOR-IP>
  source <EXPORT-SOURCE>
  transport udp 2055
  export-protocol netflow-v9
  template data timeout 60
 exit
 flow exporter FLUXO-DOWN
  destination <COLLECTOR-IP>
  source <EXPORT-SOURCE>
  transport udp 2056
  export-protocol netflow-v9
  template data timeout 60
 end
```

If the input and output monitors already exist, add the relevant exporter to each existing monitor. Check its name and direction first. If the device requires a monitor to be detached before changing it, identify every attached interface before proceeding.

```text
configure terminal
 flow monitor TOP-IPV4
  exporter FLUXO-UP
 exit
 flow monitor TOP-IPV4-DOWN
  exporter FLUXO-DOWN
 end
```

For new monitors, use the following configuration after creating the exporters. An active timeout of 60 seconds helps report long-lived flows during testing.

```text
configure terminal
 flow monitor TOP-IPV4
  record netflow ipv4 original-input
  exporter FLUXO-UP
  cache timeout active 60
 exit
 flow monitor TOP-IPV4-DOWN
  record netflow ipv4 original-output
  exporter FLUXO-DOWN
  cache timeout active 60
 exit
 interface <MONITORED-INTERFACE>
  ip flow monitor TOP-IPV4 input
  ip flow monitor TOP-IPV4-DOWN output
 end
```

Generate test traffic through the monitored interface and check the exporter counters. A flow can appear after the active or inactive timeout.

```text
show flow interface <MONITORED-INTERFACE>
show flow exporter FLUXO-UP statistics
show flow exporter FLUXO-DOWN statistics
show flow monitor TOP-IPV4
show flow monitor TOP-IPV4-DOWN
```

To roll back additions to existing monitors, remove the exporter associations first and then the exporters. For monitors created specifically for this setup, detach them from the interface before removing them. Verify with `show flow interface <MONITORED-INTERFACE>`.

```text
configure terminal
 flow monitor TOP-IPV4
  no exporter FLUXO-UP
 exit
 flow monitor TOP-IPV4-DOWN
  no exporter FLUXO-DOWN
 exit
 no flow exporter FLUXO-UP
 no flow exporter FLUXO-DOWN
end
```

## Verify ingestion and capture evidence

If the database is empty, check datagram arrival, exporter source addresses and template transmission. Replace `eth0` with the server interface that receives the exports.

```sh
sudo tcpdump -ni eth0 -c 20 'udp dst port 2055 or udp dst port 2056'
docker compose logs --tail=100 collector
```

Query records from the last 15 minutes using a read-only connection.

```sh
docker compose exec -T collector python - <<'PY'
import sqlite3
db = sqlite3.connect("file:/data/flows.sqlite3?mode=ro", uri=True)
for row in db.execute("""
    SELECT router, direction, count(*), sum(bytes),
           datetime(min(received), 'unixepoch'), datetime(max(received), 'unixepoch')
    FROM records WHERE received >= unixepoch('now', '-15 minutes')
    GROUP BY router, direction ORDER BY router, direction
"""):
    print(*row, sep="\t")
PY
```

The output gives the exporter, direction, record count, byte count, and first and last receipt times in UTC. Keep the output with the device and interface details and exporter counters when collecting evidence.

Use the SQLite backup API for a consistent database copy during ingestion. Reserve disk space and protect the snapshot because it contains traffic metadata.

```sh
umask 077
snapshot="fluxo-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
docker compose exec -T collector python - > "$snapshot" <<'PY'
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import sqlite3
import sys

with TemporaryDirectory() as directory:
    path = Path(directory) / "snapshot.sqlite3"
    with closing(sqlite3.connect("file:/data/flows.sqlite3?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(path)) as target:
            source.backup(target)
    with path.open("rb") as stream:
        shutil.copyfileobj(stream, sys.stdout.buffer)
PY
status=$?
if [ "$status" -eq 0 ]; then
  sha256sum "$snapshot" > "$snapshot.sha256"
else
  printf 'Snapshot failed. The output file may be incomplete.\n' >&2
fi
```

## Check HTTPS and PTR

Use `tls-fluxo/cert.pem` as the trust anchor for a self-signed certificate and `tls-fluxo/ca.pem` for a private CA. The example uses a self-signed certificate.

```sh
curl --cacert tls-fluxo/cert.pem https://192.0.2.20:8001/healthz
docker compose exec -T collector python /painel/ptr.py 192.0.2.11
```

For the PTR test, use an address with a known reverse record. The container must be able to reach the configured `PTR_DNS` resolvers on port 53.

## Manage archives and capacity

Set `FLOW_ARCHIVE_VOLUME` to override the archive volume name. To use an existing external Docker volume, run Compose with `-f compose.yaml -f compose.archives.yaml` and confirm write access for UID/GID 10001.

```sh
docker compose cp collector:/archives/fluxo-YYYY-MM-DD.sqlite3.xz .
docker compose exec collector du -h /data /archives
docker compose logs --tail=100 collector
```

Replace the date with the archive you want to copy. SQLite reuses space as records expire. The archive volume keeps growing as new files are written. A broad 30-day query can exceed the query time limit on busy deployments. Use a shorter range or filter by exporter, endpoint or service. Use `docker compose stop` to stop the collector and keep both volumes.
