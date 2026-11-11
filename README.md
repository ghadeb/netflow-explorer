<p align="center">
  <img src="explorer/logo.png" alt="NetFlow Explorer logo" width="112" height="112">
</p>
<h1 align="center">NetFlow Explorer</h1>
<p align="center"><strong>Self-hosted traffic analytics and interactive weathermaps for IPv4 NetFlow v9.</strong></p>

[Weathermaps](#weathermaps) · [Software stack](#software-stack) · [Quick start](#quick-start) · [Exporter configuration](#exporter-configuration) · [Operations](docs/operations.md) · [MIT License](LICENSE)

NetFlow Explorer collects flow records from your network exporters, stores them locally, and serves live and historical traffic views from one Docker Compose service.

## Highlights

- **Interactive weathermaps** for individual flows, the peers of an IPv4 host, or the destinations reached from a source subnet.
- **Traffic analytics** with top talkers, port and service mix, a UTC activity heatmap, peak intervals, and separate sent and received totals.
- **Flow explorer** with search, aggregation by host, conversation, subnet, port or ASN, and an editable page selector. View source and destination endpoints, packet and byte counts, and exported BGP ASNs. Resolve reverse DNS on demand and export filtered results to CSV.
- **Cisco NBAR labels** on live UP flows, individual weathermaps and CSV when the exporter supplies the application ID and name table.
- **Daily archive browser** with 30 days of live ClickHouse queries by default. Select a verified daily `.sqlite3.xz` from the server or upload one. Historical queries run against a separate read-only copy.
- **Direct HTTPS dashboard** with English and Portuguese, automatic refresh every ten minutes in Live, and a manual refresh control.

## Weathermaps

| Start with | What the map shows |
| --- | --- |
| **View map** on an individual flow | Source and destination endpoints, the observation point, and interfaces and next-hop information when the exporter supplies them. Dashed links mark gaps in NetFlow visibility. |
| An **IPv4 host** | Its observed peers, ranked by volume, with bytes shown in both directions. |
| A **source CIDR** such as `192.0.2.0/24` | Traffic from that subnet to each observed destination. Link thickness represents volume and destinations are paginated. |

Maps use the active dashboard filters and work with both live data and daily archives.

## Software stack

| Software | Role |
| --- | --- |
| **GoFlow2** | Decodes template-based NetFlow exports from UDP into structured flow records. [GoFlow2](https://github.com/netsampler/goflow2) |
| **Python 3.13 on Alpine Linux** | Runs ingestion, the HTTPS API, retention, and archive verification. [Python](https://docs.python.org/3.13/) · [Alpine Linux](https://alpinelinux.org/) |
| **ClickHouse** | Stores live NetFlow records in daily MergeTree partitions, compresses raw payloads with ZSTD, and powers the analytics and maps. [ClickHouse MergeTree](https://clickhouse.com/docs/engines/table-engines/mergetree-family/mergetree) |
| **SQLite** | Provides portable daily archives and the legacy rollback deployment. [SQLite](https://sqlite.org/) |
| **Docker Compose** | Runs the collector and dashboard on Linux AMD64 with persistent data volumes. [Docker Compose](https://docs.docker.com/compose/) |
| **dnspython and IANA port data** | Resolve PTR records on demand and label registered services by protocol and port. [dnspython](https://dnspython.readthedocs.io/en/stable/) · [IANA port registry](https://www.iana.org/assignments/service-names-port-numbers/) |
| **XZ and SHA-256** | Compress each daily SQLite export and verify archive integrity. [Python `lzma`](https://docs.python.org/3.13/library/lzma.html) |
| **HTML, CSS and JavaScript** | Render the dashboard, charts and maps directly in the browser. |

Cisco's NetFlow v9 export format is documented in an Informational RFC. IPFIX is the related IETF Internet Standard. NetFlow Explorer currently ingests IPv4 NetFlow v9 records and requires the exporter to send the corresponding templates. [RFC 3954](https://www.rfc-editor.org/rfc/rfc3954.html) · [RFC 7011](https://www.rfc-editor.org/rfc/rfc7011.html)

## Quick start

Use a **Linux AMD64** server with x86-64-v3 CPU support, Docker Engine, the Compose plugin and OpenSSL. Run these steps from the repository directory.

1. Copy `.env.example` to `.env`. Set `FLOW_EXPORTERS` to the comma-separated source IPv4 addresses of your exporters. Set `NETFLOW_UDP_BIND` to the server IPv4 address they can reach and `NETFLOW_WEB_BIND` to the private dashboard address. Replace the documentation addresses in the example before starting the collector. Optionally set `PTR_DNS` to comma-separated resolver addresses. Set `CLICKHOUSE_PASSWORD` to a strong password generated locally with `openssl rand -hex 24`. Keep `.env` private.
2. Prepare HTTPS before starting the collector. Replace the example with the IPv4 address or DNS name you will use to open the dashboard.

   ```sh
   ./scripts/tls.sh 192.0.2.20
   ```

   With an empty TLS directory, the script creates a self-signed certificate. It also verifies an existing certificate's identity, validity and key match. To use a private CA, place the server certificate and intermediates in `tls-fluxo/cert.pem`, the unencrypted PEM key in `tls-fluxo/key.pem`, and the root CA in `tls-fluxo/ca.pem`. The script verifies that chain. For a DNS identity, also set `TLS_VERIFY_HOST` in `.env`.

3. Build and start the stack. Compose starts the collector when ClickHouse is healthy.

   ```sh
   docker compose up -d --build --wait --wait-timeout 180
   ```

4. Open `https://<dashboard-address>:8001`. The default dashboard has HTTPS without login and is intended for a trusted private network.
5. Send IPv4 NetFlow v9 data and templates from the allowed exporters. Use a single exporter filter if two devices observe the same traffic. [Exporter configuration](#exporter-configuration)

If you are upgrading from the SQLite deployment, follow the [one-time cutover and rollback guide](docs/clickhouse-pilot.md).

## Exporter configuration

NetFlow Explorer labels records on **UDP 2055** as sent and records on **UDP 2056** as received. On a router interface facing the monitored network, `input` is traffic leaving that network and `output` is traffic returning to it. Choose the monitor directions according to where the interface sits in your topology.

### Cisco Flexible NetFlow v9

The reference platforms for these examples are an **ISR4300 with IOS XE Fuji 16.9.5** and an **ASR1000 RP1 with IOS XE 3.2.0S (IOS 15.1(1)S)**. Check field and monitor support against your platform, software release and interface type. Confirm NBAR export separately on each router. [IOS XE 16.9 Flexible NetFlow guide](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/fnetflow/configuration/xe-16-9/fnf-xe-16-9-book/fnf-fnetflow.html) · [ASR1000 IOS XE 3S NetFlow guide](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/netflow/configuration/xe-3s/asr1000/nf-xe-3s-asr1000-book.pdf)

For a new configuration, replace `<COLLECTOR-IP>` with the address in `NETFLOW_UDP_BIND`, `<EXPORT-SOURCE>` with the router's source interface and `<MONITORED-INTERFACE>` with the Layer 3 interface facing the monitored network. Add the exporter source IPv4 address to `FLOW_EXPORTERS`. For an existing configuration, inspect `show flow interface`, `show flow monitor` and `show flow exporter` and reuse the appropriate objects.

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
 exit
flow monitor TOP-IPV4
 record netflow ipv4 original-input
 exporter FLUXO-UP
 cache timeout active 60
 cache timeout inactive 15
 exit
flow monitor TOP-IPV4-DOWN
 record netflow ipv4 original-output
 exporter FLUXO-DOWN
 cache timeout active 60
 cache timeout inactive 15
 exit
interface <MONITORED-INTERFACE>
 ip flow monitor TOP-IPV4 input
 ip flow monitor TOP-IPV4-DOWN output
end
show flow interface <MONITORED-INTERFACE>
show flow exporter FLUXO-UP statistics
show flow exporter FLUXO-DOWN statistics
```

### Cisco NBAR on the UP exporter

Check `show ip nbar version` and the platform's AVC licensing. On the ISR4300 reference image, `show ip nbar protocol-pack active` identified Standard Protocol Pack 38.0. Available classifications depend on the active pack and licence. CLI acceptance is the first check. A flow with an ID and a corresponding name in the collector confirms export. [Cisco NBAR for IOS XE 16.9](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/fnetflow/configuration/xe-16-9/fnf-xe-16-9-book/fnf-nbar.html) · [Cisco NBAR for IOS XE 3S](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/fnetflow/configuration/xe-3s/fnf-xe-3s-book/fnf-nbar.pdf) · [Cisco AVC licensing](https://www.cisco.com/c/en/us/td/docs/ios/solutions_docs/avc/guide/avc-user-guide/avc_activation-legacy.html)

Create an input record based on the existing one. The template below uses the IPv4 original-input fields for an unsampled monitor. The application field is a non-key field. The exporter sends the ID-to-name table every 60 seconds. [Cisco record commands](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/fnetflow/command/fnf-cr-book/fnf-c1.html) · [Cisco exporter options](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/fnetflow/command/fnf-cr-book/fnf-m1.html)

```text
configure terminal
flow record TOP-IPV4-NBAR
 match ipv4 tos
 match ipv4 protocol
 match ipv4 source address
 match ipv4 destination address
 match transport source-port
 match transport destination-port
 match interface input
 collect routing source as
 collect routing destination as
 collect routing next-hop address ipv4
 collect ipv4 source mask
 collect ipv4 destination mask
 collect transport tcp flags
 collect interface output
 collect counter bytes
 collect counter packets
 collect timestamp sys-uptime first
 collect timestamp sys-uptime last
 collect application name
 exit
flow exporter FLUXO-UP
 option application-table timeout 60
end
show flow record TOP-IPV4-NBAR
show flow exporter FLUXO-UP
```

After the router accepts the new fields and application table option, replace the record on the UP monitor. This example assumes the monitor has one input attachment. Check all attachments with `show flow interface` first. Detaching the monitor interrupts input flow collection while its cache is recreated.

```text
configure terminal
interface <MONITORED-INTERFACE>
 no ip flow monitor TOP-IPV4 input
 exit
flow monitor TOP-IPV4
 no record
 record TOP-IPV4-NBAR
 exit
interface <MONITORED-INTERFACE>
 ip flow monitor TOP-IPV4 input
end
show flow interface <MONITORED-INTERFACE>
show flow exporter FLUXO-UP statistics
```

Generate traffic through the input interface and check the **Application** column in Live. The dashboard uses the exporter’s application name when available. For IANA protocol and service IDs, it can also look up the name in its local tables. Flow details and CSV include the resolved name. Daily archives retain the original ID. To roll back, use the same detach and reattach sequence with the previous record, `record netflow ipv4 original-input` for this example, and remove the option with `no option application-table` on the exporter if it was added for this test.

### Other NetFlow v9 exporters

For IPv4 flow collection, configure a version 9 template and use the UDP port that matches the monitored direction. Sending the same records to both ports will count the traffic twice. These links cover documented NetFlow v9 export capabilities.

- **Juniper Junos OS** supports version 9 templates with inline active flow monitoring on supported platforms. Check the model and line-card requirements. [Juniper IPv4 version 9 example](https://www.juniper.net/documentation/us/en/software/junos/flow-monitoring/topics/concept/services-ipfix-flow-template-flow-aggregation-configuring.html)
- **MikroTik RouterOS** provides version 9 via Traffic Flow targets. Choose the monitoring interfaces and set the target address and UDP port. [MikroTik Traffic Flow commands](https://manual.mikrotik.com/docs/diagnostics-monitoring-and-troubleshooting/traffic-flow/)

## Storage and retention

| Container path | Purpose |
| --- | --- |
| `/var/lib/clickhouse` (ClickHouse) | Live flow data in `fluxo-clickhouse_clickhouse-data`. `RETENTION_DAYS` defaults to 30. |
| `/archives` | One `fluxo-YYYY-MM-DD.sqlite3.xz` per completed UTC day with records, plus a SHA-256 checksum. The default volume is `fluxo-clickhouse_archive-data`. |
| `/archives/.imports` | Separate read-only working copy for historical queries. Opening another day replaces the previous working copy. |

The collector starts checking for completed UTC days after 00:15 UTC. It expires a whole day only after that day falls outside the retention window and the archive has been verified against the records. Archived files have no automatic expiry. Imports and compression require free space for a temporary SQLite copy. [Volume settings, evidence exports and troubleshooting](docs/operations.md)

## License

NetFlow Explorer is distributed under the [MIT License](LICENSE). The GoFlow2 binary included in the collector image retains its [BSD 3-Clause license](licenses/goflow2.txt).

---

Created by [Takesignal](https://pt.linkedin.com/company/takesignal) 2026 · [MIT License](LICENSE)
