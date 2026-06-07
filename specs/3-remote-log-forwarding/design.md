# Design

## Overview

The macOS remote-log-forwarding feature is implemented as a
single module,
`ccai.sysman.macos.remotelogforwarding`, exposing a
`MacosRemoteLogForwardingManager` class that manages
sentinel-bracketed forwarder blocks inside the legacy
`/etc/syslog.conf` file.  Every verb is dispatched through the
`ICommandRunner` abstraction.

The cross-OS contract for this feature lives in
`codecraftai-sysman-common/specs/contracts/remote-log-forwarding/contract.md`.
The RHEL canonical implementation lives in
`codecraftai-sysman-linux-redhat/specs/3-remote-log-forwarding/`.

---

## macOS Native Logging — The Limitation You Must Read First

macOS shipped a unified-logging system in 10.12 (Sierra)
implemented around `os_log`, `logd`, and the ``log show`` / ``log
stream`` CLI.  That subsystem replaced the BSD-style `syslogd`
pipeline as the primary log surface for the operating system.

Apple's unified logging system has no native, supported way to
forward messages off-host.  There is no equivalent of rsyslog
`omfwd`, no `syslog-ng` backend, and no Apple-blessed shipper.
The only forwarding mechanism the OS exposes is the legacy
`/etc/syslog.conf` file plus the `syslogd(8)` daemon, which is
still loaded by `com.apple.syslogd.plist` for backwards
compatibility.

The legacy pipeline can forward only the messages that are
explicitly routed through the legacy syslog facility — a small
subset of unified-logging messages that do not go through
`os_log`.  In practice that means:

- Messages from BSD-style `syslog(3)` callers.
- Messages explicitly routed via `asl_log_message`.
- The ``logger`` CLI's emissions.

Anything that goes through `os_log` (which is most of the
operating system) does NOT appear in the legacy pipeline and
therefore does NOT reach a forwarder configured via this module.

**Recommended alternatives for full coverage:**

| Tool       | Approach                                              |
|------------|-------------------------------------------------------|
| Filebeat   | OSQuery-style log shipper with an `os_log` predicate. |
| NXLog      | Native macOS shipper with `im_oslog` input module.    |
| Fluent Bit | `tail` against `/var/log/system.log` plus an
              `os_log`-aware shipper.                                |

This module exists for the legacy-syslog use cases (small
deployments where BSD syslog still carries the operationally
interesting messages, integration testing of collectors, or
parity with the cross-OS contract).  The module docstring
repeats this limitation so that callers see it in `help()`
output.

---

## Architecture

```
+-------------------------------------------------------------+
|            Maintenance Coordinator                          |
+-----------------------+-------------------------------------+
                        |
                        v
+-------------------------------------------------------------+
|       MacosRemoteLogForwardingManager                       |
|                                                             |
|  Write verbs (idempotent + dry-run aware):                  |
|    add_forwarder(spec)                                      |
|    remove_forwarder(id)                                     |
|    enable_forwarder(id)                                     |
|    disable_forwarder(id)                                    |
|                                                             |
|  Probe verbs:                                               |
|    test_forwarder(id)                                       |
|                                                             |
|  Read verbs:                                                |
|    list_forwarders()                                        |
|    get_forwarder_status(id)                                 |
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

## Forwarding Model — Concept Mapping

The cross-OS contract uses an abstract
`(destination_host, destination_port, transport, selector)`
tuple.  macOS maps it to a syslog.conf action line:

| Abstract field          | syslog.conf rendering                |
|-------------------------|--------------------------------------|
| `destination_host`      | `<host>` portion of the action line  |
| `destination_port`      | `<port>` portion of the action line  |
| `transport`             | Always UDP — Apple's `syslogd` does  |
|                         | not implement TCP / TLS forwarding.  |
|                         | Specs that request TCP / TLS still   |
|                         | render a UDP action line and the     |
|                         | result's `message` field flags the   |
|                         | downgrade.                           |
| `selector`              | Standard syslog facility.severity    |

Compared with the Ubuntu and Windows implementations:

| OS      | Native concept                           | Notes                                   |
|---------|------------------------------------------|-----------------------------------------|
| Ubuntu  | rsyslog drop-in under `/etc/rsyslog.d/`  | Full TCP / UDP / TLS support.           |
| macOS   | `/etc/syslog.conf` legacy syslogd        | UDP-only, legacy syslog facility only.  |
| Windows | Windows Event Forwarding (`wecutil`)     | XML subscription documents.             |
| RHEL    | rsyslog (canonical)                      | Reference implementation.               |

---

## Sentinel-Bracketed Block Layout

Every managed block lives inside a sentinel header / footer pair
so that the parser can find the block to update without
disturbing operator-authored entries:

```
# >>> codecraftai-fwd:<id> >>>
# <selector> @<host>:<port>
*.* @host.example.com:514
# <<< codecraftai-fwd:<id> <<<
```

The first comment line carries the rendered selector / target as
a self-documenting reference; the second non-comment line is the
actionable directive.  When the block is disabled, every action
line is prefixed with ``# `` and the leading comment header is
left intact.

The sentinel prefix (`codecraftai-fwd`) is configurable through
`MacosForwarderManagerConfig.prefix` so that operators can run
multiple managed instances side-by-side; in practice the default
is sufficient.

---

## Configuration

```python
@dataclass
class MacosForwarderManagerConfig(FeatureConfig):
    syslog_conf: str = "/etc/syslog.conf"
    syslogd_label: str = "system/com.apple.syslogd"
    prefix: str = "codecraftai-fwd"
```

`dry_run` and `elevated` are inherited from `FeatureConfig`.

---

## Data Models

```python
class MacosForwarderActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class MacosForwarderActionResult(FeatureActionResult):
    forwarder_id: str | None = None
    path:         str | None = None


@dataclass
class MacosForwarderSpec:
    id:       str
    host:     str
    port:     int    = 514
    protocol: str    = "udp"
    selector: str    = "*.*"
    enabled:  bool   = True
    tls_ca_cert: str | None = None  # accepted for parity but ignored


@dataclass
class MacosForwarderInfo:
    id:                 str
    host:               str
    port:               int
    protocol:           str
    enabled:            bool
    path:               str | None
    selector_or_query:  str | None


@dataclass
class MacosForwarderStatus:
    id:              str
    enabled:         bool
    last_event_time: str | None = None
    error_count:     int | None = None
    bytes_sent:      int | None = None


@dataclass
class MacosForwarderTestResult(FeatureActionResult):
    forwarder_id:    str = ""
    test_message_id: str | None = None


class MacosForwarderQueryError(Exception):
    """Read-side failure that is not 'absent'."""
```

---

## Block Rendering

The renderer is a pure function:
`_render_block(spec)`.  It produces a deterministic block body
so that the idempotency comparison works.

### Plain UDP

```
# >>> codecraftai-fwd:collector1 >>>
# *.* @logs.example.com:514
*.* @logs.example.com:514
# <<< codecraftai-fwd:collector1 <<<
```

### TCP / TLS specs (downgrade)

When the spec requests `protocol="tcp"` or `protocol="tls"` the
renderer still produces the UDP action line above and the
verb's result `message` field carries:

> "Apple syslogd does not implement TCP / TLS forwarding;
> downgraded to UDP.  For TCP / TLS coverage use Filebeat /
> NXLog / Fluent Bit."

This keeps the dispatch path uniform across cross-OS callers
while making the limitation explicit.

---

## Key Method Designs

### `add_forwarder(spec)`

1. Validate the spec.  When `spec.host` or `spec.id` is empty,
   return `FAILED` with a stderr that names the missing field.
2. Read `/etc/syslog.conf` via `cat`.
3. Render the desired block via `_render_block`.
4. Splice the block into the file body via
   `_upsert_block(...)`:
   - Locate the existing sentinel-bracketed block, if any.
   - Replace it with the rendered block.
   - When no existing block is found, append the rendered
     block to the file body.
5. When the resulting file body matches the on-disk body
   byte-for-byte → `SKIPPED`.
6. When `config.dry_run` → `DRY_RUN`.
7. Dispatch `tee /etc/syslog.conf` with the merged body on
   stdin.
8. On non-zero exit → return `FAILED`.
9. Dispatch `launchctl kickstart -k system/com.apple.syslogd`.
10. On non-zero kickstart → return `FAILED` with the kickstart's
    exit code / stderr.

### `remove_forwarder(id)`

1. `cat` the file body.
2. Strip the matching block via `_remove_block(body, id, prefix)`.
3. When the resulting body equals the on-disk body → `SKIPPED`.
4. When `config.dry_run` → `DRY_RUN`.
5. `tee` the new body.
6. Kickstart `syslogd`.

### `enable_forwarder(id)` / `disable_forwarder(id)`

1. `cat` the file body.
2. Locate the matching block.
3. Toggle the comment status of every action line within the
   block via `_toggle_block_enabled(...)`.
4. When the result equals the on-disk body → `SKIPPED`.
5. When the block is missing entirely → `FAILED` with an
   explanatory `message`.
6. When `config.dry_run` → `DRY_RUN`.
7. `tee` the new body.
8. Kickstart `syslogd`.

### `list_forwarders()`

1. `cat` the file body without elevation.
2. Walk the file body extracting every sentinel-bracketed block
   matching `<prefix>:<id>`.
3. For each block, parse the host / port / protocol / selector
   from the (commented or uncommented) action line.
4. Classify the block as enabled when at least one action line
   inside the block is uncommented.
5. Return a list of `MacosForwarderInfo`.

### `test_forwarder(id)`

1. Build the test message ID:
   `forwarder-test-<int(time.time())>`.
2. Dispatch
   `logger -t codecraftai-test 'forwarder-test-<timestamp>'`
   without elevation.
3. Wrap the runner result into `MacosForwarderTestResult`.
4. Never raises on dispatch failure.

### `get_forwarder_status(id)`

1. `cat` the file body.
2. Locate the matching block.
3. Return a `MacosForwarderStatus` describing the on-disk
   state.  `last_event_time`, `error_count`, and `bytes_sent`
   are `None` placeholders.

---

## Dry-Run Behaviour

Dry-run disables every mutating dispatch (`tee`,
`launchctl kickstart`).  Reads (`cat`, `test -e`) and the
`logger`-based probe run unconditionally.  Dry-run results
report the would-have-been argv on the result's `command` field.

---

## Idempotency

| Verb                    | Idempotency rule                                     |
|-------------------------|------------------------------------------------------|
| `add_forwarder`         | SKIP when the merged body matches the on-disk body.  |
| `remove_forwarder`      | SKIP when no matching block is present.              |
| `enable_forwarder`      | SKIP when block action lines are already uncommented.|
| `disable_forwarder`     | SKIP when block action lines are already commented.  |
| `test_forwarder`        | No idempotency guard.                                |
| `list_forwarders`       | n/a (read).                                          |
| `get_forwarder_status`  | n/a (read).                                          |

---

## Elevation

| Situation                                  | Behaviour                          |
|--------------------------------------------|------------------------------------|
| `elevated=True`, write verb                | Pass `elevated=True` to runner.    |
| `elevated=True`, read verb                 | Do NOT elevate.                    |
| `elevated=False`                           | Never elevate.                     |
| `test_forwarder`                           | Never elevates (read-class verb).  |

---

## Service Reload

`launchctl kickstart -k system/com.apple.syslogd` is the
modern incantation that asks `launchd` to terminate and respawn
`syslogd`.  The `-k` flag forces the existing process to be
killed first; `system/...` selects the system-domain service
label.  This replaces the `killall -HUP syslogd` of pre-Sierra
macOS, which still works but is deprecated in newer macOS
releases.

The service label is configurable through
`MacosForwarderManagerConfig.syslogd_label` so that future
Apple-shipped renames (e.g. switching to a different label
under macOS Ventura+) can be accommodated without a code
change.

---

## Correctness Properties

1. **Deterministic rendering.** The renderer is a pure function
   of the spec; identical inputs produce identical outputs.
2. **Sentinel boundaries.** The parser only edits the
   sentinel-bracketed block; operator-authored lines outside
   the block are preserved verbatim.
3. **Reads never elevate.** Every parsing path goes through
   `cat` without `elevated=True`.
4. **Probe never raises.** `test_forwarder` wraps every failure
   in the result record.

---

## Out of Scope (Documented Limitations)

The following are NOT implemented:

- **Unified-logging coverage.** Apple does not provide a
  forwarder for the `os_log` pipeline; this module covers only
  the legacy `syslog(3)` / `asl_log_message` subset.
- **TCP / TLS forwarding.** Apple `syslogd` is UDP-only.
- **Queue tuning, disk-spool, structured output.** Out of
  scope.
- **`asl_log_message` predicate filters.** Out of scope.
- **`launchctl bootstrap` / `bootout` lifecycle for
  syslogd.** This manager only kickstarts the existing service.

For full coverage operators should pair this module with one of
Filebeat, NXLog, or Fluent Bit.

---

## Testing Strategy

- `_MockCommandRunner` records every call and returns
  pre-seeded `RunResult` values keyed by argv tuple.
- One command-construction test per write verb.
- One idempotency test per write verb proving `SKIPPED`.
- One dry-run test per write verb proving the runner is not
  invoked beyond reads.
- One `list_forwarders` parser test covering enabled +
  disabled blocks.
- One reload-failure test proving the mutating verb returns
  `FAILED` when `launchctl kickstart` fails.
- One elevation test proving writes elevate and reads do not.
- One `test_forwarder` test asserting the `logger` argv shape.
- One unified-logging-limitation test asserting the module
  docstring documents the limitation.

---

## Spec-vs-Contract Drift

| Drift point                                | Resolution                                          |
|--------------------------------------------|-----------------------------------------------------|
| Contract uses `ForwarderProbeResult` (reachability + latency) | This pilot returns `MacosForwarderTestResult` because `logger` does NOT round-trip — there is no probe to time. |
| Contract `ForwarderTransport` enum         | The pilot uses a `protocol: str` field for parity with the Ubuntu / Windows implementations; the value is recorded but Apple's `syslogd` always speaks UDP. |
| Contract `last_sent_at` / `queue_depth` on `ForwarderInfo` | Moved onto `MacosForwarderStatus` as `None` placeholders pending future `os_log` integration. |
| Contract `tls_bundle_path`                 | Accepted on `MacosForwarderSpec.tls_ca_cert` for parity but unused (no native TLS support). |

The drift is intentional and documented; the cross-OS contract
recognises that macOS's TLS gap is platform-imposed.

---

## Seams With Other Features

| Seam                                | Owner                                | This Feature        |
|-------------------------------------|--------------------------------------|---------------------|
| Local unified-log surface           | `3-log-management`                   | Out of scope here.  |
| TLS material                        | `3-certificate-issuance` / trust-mgmt| n/a (no TLS).       |
| `syslogd` lifecycle                 | `3-service-management`               | Kickstart dispatched directly via `launchctl`. |
| Third-party shipper installation    | n/a (operator action)                | Out of scope here.  |
