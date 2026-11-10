# ClickHouse pilot

The public branch `ghadeb/clickhouse-pilot` runs GoFlow2 and the existing HTTPS dashboard against a single ClickHouse container. It targets an AMD64 Linux host with SSD storage and uses a starting ClickHouse limit of 1.5 vCPU and 2 GiB of container memory. The selected ClickHouse image requires an x86-64-v3 CPU. Check that the VM exposes these CPU features before cutover.

The dashboard still uses HTTPS on TCP 8001 and the collectors still listen on UDP 2055 and 2056. The ClickHouse port is internal to the Compose network. The pilot project is `fluxo-clickhouse` and has its own database and archive volumes. Live queries start with data ingested after cutover. You can import existing verified `.sqlite3.xz` files through **Archives**.

## Start on the testing server

Run from the existing working SQLite checkout, which contains `.env` and `tls-fluxo/`. Use a single working directory named `netflow-explorer` on the pilot branch. Both Compose files use the same configuration and certificates. Prepare ClickHouse before stopping the SQLite collector so the pilot can reuse the same ports.

```sh
(
  set -eu
  test -f .env
  test -d tls-fluxo
  test -f compose.clickhouse.yaml
  if ! grep -q '^CLICKHOUSE_PASSWORD=' .env; then
    chmod 600 .env
    printf '\nCLICKHOUSE_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> .env
  fi

  sudo docker compose -f compose.clickhouse.yaml config -q
  sudo docker compose -f compose.clickhouse.yaml up -d --wait --wait-timeout 150 clickhouse
  sudo docker compose -f compose.clickhouse.yaml build collector
  sudo docker compose -f compose.yaml stop collector
  if ! sudo docker compose -f compose.clickhouse.yaml up -d --wait --wait-timeout 90 collector; then
    sudo docker compose -f compose.clickhouse.yaml logs --tail=50 collector || true
    sudo docker compose -f compose.clickhouse.yaml stop collector || true
    sudo docker compose -f compose.yaml up -d collector
    exit 1
  fi
)
```

Open the same HTTPS address as before. Check that the selected router and both directions show traffic.

## Storage and compression

The raw flow JSON is stored losslessly with ZSTD level 1. The collector updates the column codec on startup if it finds an older pilot table. It leaves existing parts available and does not force a table rewrite. Measure the actual bytes per record from active parts after a representative interval.

```sh
sudo docker compose -f compose.clickhouse.yaml exec -T clickhouse sh -c   'exec clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --database netflow' <<'SQL'
SELECT sum(rows) AS records,
       formatReadableSize(sum(bytes_on_disk)) AS disk,
       round(sum(bytes_on_disk) / greatest(sum(rows), 1), 1) AS bytes_per_record
FROM system.parts
WHERE database = 'netflow' AND table = 'flows' AND active;
SQL
```

For the original SQLite volume, first stop every container that mounts it. The backup script streams the database and any WAL files to XZ on a separate filesystem, verifies the compressed copy against the source and saves its SHA-256. This is a whole-volume backup for recovery. Restore it to a separate location and check SQLite integrity before removing the original.

```sh
sudo ./scripts/backup-sqlite-volume.sh /mnt/backup
```

## Measure the peak

From the pilot directory, capture a baseline and repeat during and after the peak:

```sh
sudo docker compose -f compose.clickhouse.yaml logs --since 15m collector
sudo docker compose -f compose.clickhouse.yaml exec -T collector grep '^Udp:' /proc/net/snmp
sudo docker stats --no-stream $(sudo docker compose -f compose.clickhouse.yaml ps -q)
```

Compare saved records and the sampled queue high-water mark in the collector logs. Compare `RcvbufErrors` and `InErrors` deltas inside the collector with the exporter counters. The collector counts decoded records, while UDP counters count datagrams. Test the dashboard during the peak and note query time and whether ingestion catches up. An outage of ClickHouse keeps the pending batch in memory while the collector retries. The pilot has no durable message queue.

The worker writes verified daily `.sqlite3.xz` files and only drops a whole UTC partition after the retention cutoff and a final consistency check. The pilot defaults to XZ level 3 to limit CPU use. You can set `CLICKHOUSE_ARCHIVE_XZ_LEVEL` from 0 to 9. The archive worker reserves disk headroom and can defer archiving when storage is tight. Check available space for ClickHouse merges and for a temporary daily SQLite file during archival. Archive production and long-range query performance are part of the pilot.

## Roll back

From the same `netflow-explorer` directory, stop the pilot collector and start the SQLite collector.

```sh
sudo docker compose -f compose.clickhouse.yaml stop collector
sudo docker compose -f compose.yaml up -d collector
```

Both projects retain their separate volumes. The pilot collects its own data, so records received during the test remain in its ClickHouse volume and archives.
