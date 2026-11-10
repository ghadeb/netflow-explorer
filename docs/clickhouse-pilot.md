# ClickHouse pilot

The public branch `ghadeb/clickhouse-pilot` runs GoFlow2 and the existing HTTPS dashboard against a single ClickHouse container. It targets an AMD64 Linux host with SSD storage and uses a starting ClickHouse limit of 1.5 vCPU and 2 GiB of container memory. The selected ClickHouse image requires an x86-64-v3 CPU. Check that the VM exposes these CPU features before cutover.

The dashboard still uses HTTPS on TCP 8001 and the collectors still listen on UDP 2055 and 2056. The ClickHouse port is internal to the Compose network. The pilot project is `fluxo-clickhouse` and has its own database and archive volumes. Live queries start with data ingested after cutover. You can import existing verified `.sqlite3.xz` files through **Archives**.

## Start on the testing server

Run from the existing working SQLite checkout, which contains `.env` and `tls-fluxo/`. The following Bash block prepares a sibling checkout and the ClickHouse service while SQLite is still receiving traffic. It stops the old collector just before starting the new one so the pilot can reuse the same ports. Keep the original checkout for rollback.

```sh
(
  set -eu
  old="$PWD"
  pilot="$old-clickhouse"
  test -f "$old/.env"
  test -d "$old/tls-fluxo"
  timeout 120s env GIT_TERMINAL_PROMPT=0 \
    git -c credential.helper= -c http.version=HTTP/1.1 clone \
      --single-branch --branch ghadeb/clickhouse-pilot \
      https://github.com/ghadeb/netflow-explorer.git "$pilot"
  install -m 600 "$old/.env" "$pilot/.env"
  sudo cp -a "$old/tls-fluxo" "$pilot/tls-fluxo"
  cd "$pilot"
  printf '\nCLICKHOUSE_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> .env

  sudo docker compose -f compose.clickhouse.yaml config -q
  sudo docker compose -f compose.clickhouse.yaml up -d --wait --wait-timeout 150 clickhouse
  sudo docker compose -f compose.clickhouse.yaml build collector
  (cd "$old" && sudo docker compose stop collector)
  if ! sudo docker compose -f compose.clickhouse.yaml up -d --wait --wait-timeout 90 collector; then
    sudo docker compose -f compose.clickhouse.yaml logs --tail=50 collector || true
    sudo docker compose -f compose.clickhouse.yaml stop collector || true
    (cd "$old" && sudo docker compose up -d collector)
    exit 1
  fi
)
```

Open the same HTTPS address as before. Check that the selected router and both directions show traffic.

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

Stop the pilot collector from the pilot checkout, then start the original collector from the original checkout.

```sh
sudo docker compose -f compose.clickhouse.yaml stop collector
cd /path/to/original-checkout
sudo docker compose up -d collector
```

Both projects retain their separate volumes. The pilot collects its own data, so records received during the test remain in its ClickHouse volume and archives.
