# ClickHouse deployment and SQLite cutover

The default Compose file runs GoFlow2 and the HTTPS dashboard against a single ClickHouse container. It targets an AMD64 Linux host with SSD storage and uses a starting ClickHouse limit of 1.5 vCPU and 2 GiB of container memory. The selected ClickHouse image requires an x86-64-v3 CPU. Check that the VM exposes these CPU features before cutover.

The dashboard still uses HTTPS on TCP 8001 and the collectors still listen on UDP 2055 and 2056. The ClickHouse port is internal to the Compose network. The Compose project is `fluxo-clickhouse` and has its own database and archive volumes. Live queries start with data ingested after cutover. You can import existing verified `.sqlite3.xz` files through **Archives**.

## Start or update ClickHouse

Run from the `netflow-explorer` directory after preparing `.env` and `tls-fluxo/` as described in the [quick start](../README.md#quick-start).

```sh
sudo docker compose up -d --build --wait --wait-timeout 180
```

The default Compose file uses the same project and volumes as the original ClickHouse pilot. Its previous filename, `compose.clickhouse.yaml`, remains available as an alias.

## Cut over from the legacy SQLite deployment

The two collectors bind the same UDP and HTTPS ports. Use this block once when the legacy SQLite collector still owns those ports. Run it from the same `netflow-explorer` directory with the existing `.env` and certificates.

```sh
(
  set -eu
  if ! grep -q '^CLICKHOUSE_PASSWORD=.' .env; then
    chmod 600 .env
    db_password="$(openssl rand -hex 24)"
    printf '\nCLICKHOUSE_PASSWORD=%s\n' "$db_password" >> .env
    unset db_password
  fi

  sudo docker compose config -q
  sudo docker compose -f compose.sqlite.yaml stop collector
  if ! sudo docker compose up -d --build --wait --wait-timeout 180; then
    sudo docker compose logs --tail=50 collector clickhouse || true
    sudo docker compose stop collector
    sudo docker compose -f compose.sqlite.yaml up -d collector
    exit 1
  fi
)
```

Open the same HTTPS address and check that flows appear for the expected exporters and directions.

## Storage and compression

The raw flow JSON is stored losslessly with ZSTD level 1. The collector updates the column codec on startup if it finds an older pilot table. It leaves existing parts available and does not force a table rewrite. Measure the actual bytes per record from active parts after a representative interval.

```sh
sudo docker compose exec -T clickhouse sh -c \
  'exec clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --database netflow' <<'SQL'
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
sudo docker compose logs --since 15m collector
sudo docker compose exec -T collector grep '^Udp:' /proc/net/snmp
sudo docker stats --no-stream $(sudo docker compose ps -q)
```

Compare saved records and the sampled queue high-water mark in the collector logs. Compare `RcvbufErrors` and `InErrors` deltas inside the collector with the exporter counters. The collector counts decoded records, while UDP counters count datagrams. Test the dashboard during the peak and note query time and whether ingestion catches up. An outage of ClickHouse keeps the pending batch in memory while the collector retries. The pilot has no durable message queue.

The worker writes verified daily `.sqlite3.xz` files and only drops a whole UTC partition after the retention cutoff and a final consistency check. The ClickHouse stack defaults to XZ level 3 to limit CPU use. You can set `CLICKHOUSE_ARCHIVE_XZ_LEVEL` from 0 to 9. The archive worker reserves disk headroom and can defer archiving when storage is tight. Check available space for ClickHouse merges and for a temporary daily SQLite file during archival. Archive production and long-range query performance are part of the pilot.

## Roll back

From the same `netflow-explorer` directory, stop the ClickHouse stack and start the legacy SQLite collector.

```sh
sudo docker compose stop
sudo docker compose -f compose.sqlite.yaml up -d collector
```

Each deployment uses separate volumes. Records captured by ClickHouse stay in its volume and archives during rollback.
