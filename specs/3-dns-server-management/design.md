# Design

## Overview

The macOS dns-server-management feature is implemented as a
single module,
`ccai.sysman.macos.dnsservermanagement`, exposing a
`MacosDnsServerManager` class that wraps the BIND 9 daemon
(`named`) shipping with macOS through the `ICommandRunner`
abstraction.  It manages the content of `/etc/named.conf` and
per-zone data files under `/var/named/db.<name>`.

The module follows the standard manager pattern shared with
every other Layer 3 feature in this package: it consumes the
`BaseFeatureManager` mixin from
`ccai.sysman.macos.featurebase`, accepts an injected
runner plus a `MacosDnsManagerConfig`, and dispatches every
shell command through the runner.

The cross-OS contract for this feature lives in
`codecraftai-sysman-common/specs/contracts/dns-server-management/contract.md`.
The RHEL canonical implementation lives in
`codecraftai-sysman-linux-redhat/specs/3-dns-server-management/`.

**See Also:**
- `3-network-configuration` — owns DNS *client* (stub-resolver)
  configuration on macOS via `scutil` / `networksetup`.
- `3-service-management` — `org.isc.named` launchd lifecycle.
- `3-firewall-management` — opening UDP/TCP 53 through `pfctl`
  and the macOS application firewall; out of scope here.
- `3-certificate-issuance` — TLS material for DoT / DoH; out of
  scope for this pilot.

---

## Architecture

```
+-------------------------------------------------------------+
|            Maintenance Coordinator                          |
+-----------------------+-------------------------------------+
                        |
                        v
+-------------------------------------------------------------+
|       MacosDnsServerManager                                 |
|                                                             |
|  Write verbs (idempotent + dry-run aware):                  |
|    add_zone(name, spec)                                     |
|    remove_zone(name)                                        |
|    add_record(zone, record)                                 |
|    remove_record(zone, record)                              |
|    reload_zone(name)                                        |
|                                                             |
|  Read verbs:                                                |
|    list_zones()                                             |
|    list_records(zone)                                       |
|    get_zone_status(name)                                    |
+--------------------------+----------------------------------+
                           |
                           v
                +----------+-----------+
                |     ICommandRunner   |
                +----------+-----------+
                           |
                ___________|___________
               |                       |
               v                       v
   +-------------------+    +---------------------+
   | LocalCommandRunner|    | SSHCommandRunner    |
   +---------+---------+    +-----------+---------+
             |                          |
             |     aspects compose here |
             +------------+-------------+
                          |
                          v
        +-----------------------------------+
        | PersistenceAspect, RetryAspect,   |
        | TimeoutAspect (transparent)       |
        +-----------------------------------+
```

---

## Cross-OS DNS-Server-Model Concept Mapping

The cross-OS contract uses an abstract
`(zone, record, serial)` triple plus a backend-agnostic
`zone_type` enum.  Each OS maps these abstractions onto its
native tooling:

| Abstract concept   | macOS (BIND ships with macOS)                | Ubuntu (BIND 9)                              | Windows (DnsServer module)                  | RHEL (BIND, canonical)            |
|--------------------|----------------------------------------------|----------------------------------------------|---------------------------------------------|-----------------------------------|
| Zone declaration   | `zone "<name>" {}` block in                  | `zone "<name>" {}` block in                  | `Add-DnsServerPrimaryZone -Name '<zone>'`   | `zone "<name>" {}` block in       |
|                    | `/etc/named.conf` (single-file convention)   | `/etc/bind/named.conf.local`                 | `-ZoneFile '<zone>.dns'`                    | `/etc/named.conf` /               |
|                    | (sentinel-bracketed)                         | (sentinel-bracketed)                         |                                             | `/etc/named/zones.d/`             |
| Zone-data file     | `/var/named/db.<name>`                       | `/etc/bind/zones/db.<name>`                  | `%SystemRoot%\System32\dns\<name>.dns`      | `/var/named/db.<name>`            |
| Add record         | edit zone file + bump SOA + `rndc reload`    | edit zone file + bump SOA + `rndc reload`    | `Add-DnsServerResourceRecordA` /            | edit zone file + bump SOA +       |
|                    |                                              |                                              | `-AAAA` / `-CName` / `-MX` / `-TXT` ...     | `rndc reload`                     |
| Remove record      | strip line + bump SOA + `rndc reload`        | strip line + bump SOA + `rndc reload`        | `Remove-DnsServerResourceRecord`            | strip line + bump SOA +           |
|                    |                                              |                                              |                                             | `rndc reload`                     |
| List zones         | parse `named.conf` +                         | parse `named.conf.local` +                   | `Get-DnsServerZone`                         | parse `named.conf` +              |
|                    | `named-checkconf -p`                         | `named-checkconf -p`                         |                                             | `named-checkconf -p`              |
| List records       | parse zone-data file                         | parse zone-data file                         | `Get-DnsServerResourceRecord -ZoneName ...` | parse zone-data file              |
| Reload zone        | `rndc reload <zone>`                         | `rndc reload <zone>`                         | `Sync-DnsServerZone -Name <zone>`           | `rndc reload <zone>`              |
| Zone status        | `rndc zonestatus <zone>`                     | `rndc zonestatus <zone>`                     | `Get-DnsServerZone -Name <zone>`            | `rndc zonestatus <zone>`          |
| Service identity   | launchd label `org.isc.named` in domain      | systemd unit `bind9.service`                 | DNS Server role (no systemd unit)           | systemd unit `named.service`      |
|                    | `system`                                     |                                              |                                             |                                   |

The macOS implementation shares the BIND tooling (`rndc`,
`named-checkconf`, `named-checkzone`) with Ubuntu and RHEL —
the only divergences are the `named.conf` location, the
zone-data file directory, and the service-identity tokens
(`org.isc.named` launchd label vs. systemd unit names).

---

## macOS BIND-vs-Server.app Path Divergence

Two BIND deployments are common on macOS:

| Layout                | named.conf                            | Zone-data dir              | launchd label   | service_domain                   |
|-----------------------|---------------------------------------|----------------------------|-----------------|----------------------------------|
| Apple base-OS BIND    | `/etc/named.conf`                     | `/var/named/`              | `org.isc.named` | `system`                         |
| macOS Server.app      | `/Library/Server/named/named.conf`    | `/Library/Server/named/`   | `org.isc.named` | `system/com.apple.serverd`       |

The `MacosDnsManagerConfig` defaults target the base-OS BIND.
Operators running Server.app must construct the config
explicitly:

```python
cfg = MacosDnsManagerConfig(
    named_conf="/Library/Server/named/named.conf",
    zone_dir="/Library/Server/named",
    service_label="org.isc.named",
    service_domain="system/com.apple.serverd",
)
```

The manager does NOT auto-detect.  When both layouts are
present on the same host (rare but possible), the operator
chooses which one this manager controls.  The other layout is
out of scope.

Note: the `service_label` and `service_domain` fields are
informational — this manager dispatches `rndc reconfig` /
`rndc reload`, never `launchctl bootstrap` or
`launchctl kickstart`.  Daemon-level lifecycle is delegated
to `3-service-management`.  The fields are recorded on the
config so coordinators can correlate the manager's view with
the service-manager's view.

---

## Configuration

```python
@dataclass
class MacosDnsManagerConfig(FeatureConfig):
    named_conf:      str = "/etc/named.conf"
    zone_dir:        str = "/var/named"
    service_label:   str = "org.isc.named"
    service_domain:  str = "system"
```

`dry_run` and `elevated` are inherited from `FeatureConfig`.

---

## Data Models

```python
class MacosDnsActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class MacosDnsActionResult(FeatureActionResult):
    zone:        str | None = None
    record_name: str | None = None
    message:     str | None = None


@dataclass
class MacosDnsZoneSpec:
    name:              str
    zone_type:         str        = "primary"
    zone_file:         str | None = None
    allow_update:      bool       = False
    replication_scope: str | None = None  # AD-only; ignored


@dataclass
class MacosDnsZoneInfo:
    name:         str
    zone_type:    str
    zone_file:    str | None
    record_count: int | None
    is_active:    bool


@dataclass
class MacosDnsRecordSpec:
    name:        str
    record_type: str
    value:       str
    ttl:         int | None = None
    priority:    int | None = None


@dataclass
class MacosDnsRecordInfo:
    name:        str
    record_type: str
    value:       str
    ttl:         int | None = None
    priority:    int | None = None
    serial:      int | None = None


@dataclass
class MacosDnsZoneStatus:
    name:              str
    is_loaded:         bool
    serial:            int | None = None
    last_loaded:       str | None = None
    transferred_from:  str | None = None


class MacosDnsQueryError(Exception):
    """Read-side failure that is not 'absent'."""
```

---

## Sentinel-Bracketed Block Convention

Every managed `zone "<name>" {}` declaration is wrapped between
sentinel comment lines so the parser can locate it without
disturbing operator-authored declarations:

```
# >>> codecraftai-zone:example.com >>>
zone "example.com" {
    type master;
    file "/var/named/db.example.com";
};
# <<< codecraftai-zone:example.com <<<
```

This convention matches the Ubuntu manager exactly so cross-OS
playbook authors see identical sentinel formatting on both
platforms.

---

## Zone-Data File Rendering

The initial zone-data file body emitted by `add_zone` is:

```
; Generated by ccai MacosDnsServerManager
$TTL    3600
@       IN      SOA     ns1.<name>. hostmaster.<name>. (
                <YYYYMMDD00>        ; Serial
                3600                ; Refresh
                600                 ; Retry
                1209600             ; Expire
                86400 )             ; Minimum TTL
;
@       IN      NS      ns1.<name>.
```

Identical to the Ubuntu manager modulo the comment header —
this consistency is intentional so cross-OS playbooks see
identical zone-file shapes regardless of platform.

---

## SOA Serial Bumping

Same algorithm as the Ubuntu manager:

1. Find the first line whose text contains `Serial` or
   `serial`.
2. Replace the first integer token: when the existing token
   is `>=` today's `YYYYMMDD00`, the new value is
   `existing + 1`; otherwise the new value is today's date.
3. Stop after the first replacement.

---

## Validate-Before-Apply

The contract requires every write verb to validate the
rendered configuration before dispatching the reload / apply
command.  macOS ships `named-checkconf` and
`named-checkzone` with the base-OS BIND.

The `_validate_configuration` helper invokes both validators
in order: `named-checkconf` first, then (when both arguments
are supplied) `named-checkzone`.  A non-zero exit from either
flips the verb's status to `FAILED`, the reload command is
suppressed, and the file mutations remain on disk for
forensic inspection.

---

## Key Method Designs

### `add_zone(name, spec)`

1. Resolve the zone-data file path.
2. When `config.dry_run` → `DRY_RUN`.
3. Read `named.conf`; probe the zone file via `test -e`.
4. When the sentinel block is present AND the zone file
   exists → `SKIPPED`.
5. Render the zone block and zone-data file body.
6. Dispatch `tee /etc/named.conf` → on failure, return
   `FAILED`.
7. Dispatch `tee <zone_file>` → on failure, return
   `FAILED`.
8. Validate via `named-checkconf` then `named-checkzone`.
9. Dispatch `rndc reconfig`.

### `remove_zone(name)`

1. When `config.dry_run` → `DRY_RUN`.
2. Read `named.conf`; probe the zone-data file.
3. When neither is present → `SKIPPED`.
4. When the sentinel block is present, dispatch `tee` with
   the stripped content.
5. When the zone-data file is present, dispatch `rm -f`.
6. Validate via `named-checkconf`.
7. Dispatch `rndc reconfig`.

### `add_record(zone, record)` / `remove_record(zone, record)`

Identical shape to the Ubuntu manager: read, parse, mutate,
bump serial, write, validate, reload.

### `reload_zone(name)`

`rndc reload <name>`, no idempotency, honours dry-run.

### `list_zones()`

1. `cat /etc/named.conf`; parse every sentinel-bracketed
   managed block.
2. `named-checkconf -p`; fold in any operator-authored
   zones the manager does not own.

### `list_records(zone)`

`cat /var/named/db.<zone>` and parse.

### `get_zone_status(name)`

`rndc zonestatus <name>`; parse the indented key-colon-value
report.

---

## Dry-Run Behaviour

Dry-run disables every mutating dispatch (`tee`, `rm`,
`named-checkconf`, `named-checkzone`, `rndc reconfig`,
`rndc reload`).  Reads (`cat`, `test -e`,
`named-checkconf -p`, `rndc zonestatus`) run unconditionally.

---

## Idempotency

| Verb              | Idempotency rule                                         |
|-------------------|----------------------------------------------------------|
| `add_zone`        | SKIP when sentinel block AND zone-data file are present. |
| `remove_zone`     | SKIP when neither sentinel block nor zone file exists.   |
| `add_record`      | SKIP when an identical (name, type, value) is present.   |
| `remove_record`   | SKIP when no matching record is on disk.                 |
| `reload_zone`     | No idempotency guard.                                    |
| `list_zones`      | n/a (read).                                              |
| `list_records`    | n/a (read).                                              |
| `get_zone_status` | n/a (read).                                              |

---

## Elevation

| Situation                                     | Behaviour                          |
|-----------------------------------------------|------------------------------------|
| `elevated=True`, write verb (incl. validators)| Pass `elevated=True` to runner.    |
| `elevated=True`, read verb                    | Do NOT elevate.                    |
| `elevated=False`                              | Never elevate.                     |
| `rndc zonestatus <zone>`                      | Read-class verb; never elevates.   |

---

## Service Reload

Same as the Ubuntu manager: `rndc reconfig` for declaration
changes, `rndc reload <zone>` for record changes.  Daemon
restarts are delegated to `3-service-management`.

---

## Correctness Properties

1. **Sentinel discipline.** Managed zone declarations are
   always wrapped in sentinel comment lines.
2. **Validate-before-reload.** `rndc` never runs unless
   `named-checkconf` (and, where applicable,
   `named-checkzone`) exit zero.
3. **Strict-monotonic SOA serial.** Every record-write
   strictly increases the serial.
4. **Reads never elevate.**
5. **Dry-run honours.** Every mutating dispatch is gated on
   `config.dry_run is False`.

---

## Out of Scope (Documented Limitations)

The following native BIND features are NOT implemented in
this pilot.  They are recorded as follow-ups in `tasks.md`:

- **DNSSEC zone signing.**
- **TSIG / transfer-key management.**
- **RPZ (response-policy zones).**
- **Views / split-horizon.**
- **Dynamic-update ACL beyond the boolean toggle.**
- **DoT / DoH listener configuration.**
- **`get_server_status`.** Daemon-level status is delegated
  to `3-service-management`.
- **mDNSResponder integration.** macOS unified resolution
  goes through `mDNSResponder`, not directly through
  `named`; the manager does NOT touch
  `mDNSResponder`'s configuration.
- **Forward / stub zone payload rendering.**
- **Server.app auto-detection.** Operators on Server.app
  must override the path defaults explicitly.

---

## Testing Strategy

- `_MockCommandRunner` records every call and returns
  pre-seeded `RunResult` values keyed by argv tuple.
- One command-construction test per write verb.
- One idempotency test per write verb.
- One dry-run test per write verb.
- One `list_zones` parser test covers a multi-zone
  `named.conf` plus the `named-checkconf -p` fold-in.
- One `list_records` parser test covers A, AAAA, CNAME,
  TXT records.
- One `get_zone_status` parser test.
- One serial-bump test.
- One elevation test.
- One reads-never-elevate test.
- One failure-surfacing test.

---

## Spec-vs-Contract Drift

| Drift point                                              | Resolution                                                                                           |
|----------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| Contract uses a `DnsServerBackend` enum.                 | macOS pilot targets BIND only.  `dnsmasq` via Homebrew is mentioned in the contract but out of pilot. |
| Contract names `DnsZoneKind`.                            | The pilot uses a `zone_type: str` field that the renderer maps to BIND tokens.                       |
| Contract specifies `DnsRecord` (no priority field).      | The pilot adds `priority: int | None` so MX / SRV records do not have to embed priority into `value`. |
| Contract specifies `DnsServerStatus`.                    | The pilot does NOT implement `get_server_status`; daemon-level status is delegated to `3-service-management`. |
| Contract specifies `DnsBackendMismatchError`.            | macOS pilot is single-backend; the error is reserved for cross-OS parity.                            |
| Contract is silent about Server.app.                     | The pilot documents the divergence and provides config overrides; the manager does not auto-detect.  |

---

## Seams With Other Features

| Seam                                | Owner                                | This Feature        |
|-------------------------------------|--------------------------------------|---------------------|
| Stub-resolver / `scutil --dns`      | `3-network-configuration`            | Out of scope here.  |
| `org.isc.named` launchd lifecycle   | `3-service-management`               | Reload only via `rndc`; restarts delegated. |
| UDP / TCP 53 firewall opening       | `3-firewall-management`              | Out of scope here.  |
| TLS material for DoT / DoH          | `3-certificate-issuance` / trust-mgmt| Out of scope here.  |
| BIND package install                | `3-package-management`               | macOS ships BIND in the base OS. |
