# Design

## Overview

The precision-time-management feature is implemented as a single
module,
`automojo.sysman.macos.precisiontimemanagement`, exposing a
`MacosPrecisionTimeManager` class that wraps `systemsetup` (the
canonical persistent surface for network-time configuration), `sntp`
(the user-space SNTP client), and `defaults read
/Library/Preferences/com.apple.timed timeServiceConfig` (the
contemporary `timed` defaults domain) through the `ICommandRunner`
abstraction.  Legacy `/etc/ntp.conf` (where present) is read via
`cat` through the runner.

The manager follows the locked-in pattern of every Layer-3 manager
in this package:

* A `BaseFeatureManager` subclass that ferries the canonical fields
  (`runner`, `config`) onto `self`.
* A feature-prefixed status enum, action result, and config
  dataclass vendored on top of the package's `featurebase`.
* OS-prefixed types throughout (`Macos*`).
* No live filesystem access — every external call flows through the
  runner.

**See Also:**
- `3-network-configuration` — companion infrastructure feature.
- `automojo-sysman-linux-redhat/specs/3-precision-time-management/`
  — the RHEL reference, which targets the IEEE 1588 PTP surface and
  is not applicable to macOS.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosPrecisionTimeManager                        │
│                                                             │
│  Reads:                                                     │
│    get_time_status(), get_time_sources(), get_drift()       │
│  Writes:                                                    │
│    add_time_source(), remove_time_source(),                 │
│    set_time_servers(), force_sync_now(),                    │
│    enable_sync(), disable_sync()                            │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
       ┌─────────────────────┐
       │   ICommandRunner    │
       └─────────────────────┘
                 │
   ┌─────────────┼──────────────────┐
   ▼             ▼                  ▼
systemsetup    sntp           defaults / cat
(persistent)   (one-shot)     (read fallback)
```

---

## Configuration

```python
@dataclass
class MacosPrecisionTimeManagerConfig(FeatureConfig):
    pass
```

`dry_run` and `elevated` are inherited from `FeatureConfig`.  macOS
does not expose tunable backend selection or service unit names; the
manager binds to `systemsetup` / `sntp` / `defaults` unconditionally.

---

## Verb-to-Tool Mapping

| Verb | Command |
|------|---------|
| `get_time_status` | `systemsetup -getusingnetworktime` + `systemsetup -getnetworktimeserver` |
| `get_time_sources` | `defaults read /Library/Preferences/com.apple.timed timeServiceConfig` (preferred) → fallback `systemsetup -getnetworktimeserver` → fallback `cat /etc/ntp.conf` |
| `add_time_source` | recompute list, then `systemsetup -setnetworktimeserver "<list>"` |
| `remove_time_source` | recompute list, then `systemsetup -setnetworktimeserver "<list>"` |
| `set_time_servers` | `systemsetup -setnetworktimeserver "<list>"` |
| `force_sync_now` | read current server, then `sntp -sS <server>` |
| `get_drift` | read current server, then `sntp <server>` |
| `enable_sync` | `systemsetup -setusingnetworktime on` |
| `disable_sync` | `systemsetup -setusingnetworktime off` |

The manager never invokes a subprocess directly; every external call
routes through the runner.

---

## Data Models

```python
MacosPrecisionTimeActionStatus = ActionStatus  # alias


@dataclass
class MacosPrecisionTimeActionResult(FeatureActionResult):
    server: Optional[str] = None


@dataclass
class MacosTimeStatus:
    enabled: bool
    synchronized: bool
    current_server: Optional[str]
    stratum: Optional[int]
    last_sync: Optional[str]
    offset_seconds: Optional[float]


@dataclass
class MacosTimeSourceInfo:
    server: str
    state: str                          # 'selected' | 'candidate' | 'unreachable'
    stratum: Optional[int]
    offset_seconds: Optional[float]
    last_response: Optional[str]


@dataclass
class MacosTimeDrift:
    offset_seconds: float
    frequency_ppm: Optional[float]
    jitter_seconds: Optional[float]
    stratum: Optional[int]


class MacosPrecisionTimeQueryError(Exception):
    ...
```

`MacosPrecisionTimeActionResult` extends the canonical
`FeatureActionResult` with a single new field, `server`.  Verbs that
do not target a single server populate `server=None`.

The contract's abstract `TimeStatus.sync_service_active` is collapsed
into `enabled`; both names denote the same property on macOS — Apple
exposes a single boolean ("network time on / off") rather than a
distinct service-active concept.

`stratum`, `last_sync`, and `frequency_ppm` are always `None` on
macOS because Apple's user-space tooling does not surface those
values.  The fields exist to keep cross-OS-contract type shape
parity.

---

## Multi-Server Limitation

`systemsetup -setnetworktimeserver` accepts a single
whitespace-delimited string and persists it through the System
Configuration framework.  Apple's tooling is single-server-oriented;
multi-server lists are accepted by `systemsetup` but are not natively
distributed across the platform's clock-sync daemons.  Operators
should treat the first entry as the effective primary upstream and
expect Apple's `timed` service to consult others only as fallbacks.
This implementation honours the contract verbs uniformly even when
the underlying surface is single-server-leaning.

---

## Key Method Designs

### `get_time_status()`

1. Dispatch `systemsetup -getusingnetworktime` (no elevation).  Raise
   `MacosPrecisionTimeQueryError` on non-zero.
2. Dispatch `systemsetup -getnetworktimeserver` (no elevation).
3. Build the status from the parsed `Network Time: On|Off` and
   `Network Time Server: <server>` payloads.

### `get_time_sources()`

1. Try
   `defaults read /Library/Preferences/com.apple.timed
   timeServiceConfig` first (no elevation).  On exit code 0, parse
   the `TimeServers = ( ... );` block and return one
   `MacosTimeSourceInfo` per entry, marking the first entry
   `"selected"`.
2. On non-zero exit, dispatch `systemsetup -getnetworktimeserver`.
   On non-zero exit there, raise.  On success, emit a single
   `MacosTimeSourceInfo` for the parsed primary.
3. When neither source reports any servers, fall back further to
   reading `/etc/ntp.conf` via `cat` for legacy installations.

### `add_time_source(server)`

1. Dry-run short-circuit.
2. Read the current server list via the same fallback chain as
   `get_time_sources` (without raising on missing legacy).
3. If `server` is already in the list, return `SKIPPED`.
4. Render the new list as a space-joined string.
5. Dispatch
   `systemsetup -setnetworktimeserver "<rendered>"` honouring
   `config.elevated`.

### `remove_time_source(server)` and `set_time_servers(servers)`

Same shape as `add_time_source` with the appropriate idempotency
guard.

### `force_sync_now()`

1. Dry-run short-circuit returns the placeholder argv
   `["sntp", "-sS", "<current_server>"]` on `result.command`.
2. Read the current server.  If absent, return `FAILED` with a
   stderr message explaining the state.
3. Dispatch `sntp -sS <server>` honouring `config.elevated`.  `-S`
   instructs `sntp` to step the system clock immediately.

### `get_drift()`

1. Read the current server.  If absent, return a zero-value
   `MacosTimeDrift` rather than raise.
2. Dispatch `sntp <server>` (no elevation) and parse
   `±offset +/- error<unit>` from the first matching line.
3. Normalise the error to seconds (``s`` → 1.0, ``ms`` → 1e-3,
   ``us`` → 1e-6, ``ns`` → 1e-9).

### `enable_sync()` / `disable_sync()`

1. Dry-run short-circuit.
2. Probe `systemsetup -getusingnetworktime`.  If state already
   matches, return `SKIPPED`.
3. Dispatch the lifecycle command honouring `config.elevated`.

---

## File-I/O Discipline

* All file I/O routes through the runner via `cat`.  This module
  does not consume an `IFileOperator`.  The cross-OS contract
  permits an `IFileOperator` collaborator; this implementation
  follows the locked-in macOS pattern of routing every external
  call through the runner.
* This module never writes a configuration file directly.
  `systemsetup` owns its own persistence layer.

---

## Dry-Run Behaviour

Every write verb honours `config.dry_run`.  Dry-run returns
`status=DRY_RUN` and records the would-have-been command on
`result.command` without invoking the runner.  Dry-run does NOT
short-circuit reads.

---

## Elevation Behaviour

* Reads (`get_time_status`, `get_time_sources`, `get_drift`, the
  `defaults`/`systemsetup -get*` probes) MUST always pass
  `elevated=False`.
* Writes (`systemsetup -set*`, `sntp -sS <server>`) MUST honour
  `config.elevated`.

---

## Correctness Properties

1. **Idempotency.** `add_time_source` / `remove_time_source` /
   `set_time_servers` / `enable_sync` / `disable_sync` SKIP when
   the live state already matches the requested state.
2. **`force_sync_now` is operational.** No idempotency guard.
3. **Reads never raise on "no server configured".** `get_drift`
   returns a zero-value snapshot; `get_time_sources` returns the
   empty list.
4. **No subprocess.** The manager never constructs its own
   subprocess; every external call routes through the runner.
5. **`stratum` / `last_sync` / `frequency_ppm` honesty.** Always
   `None` — Apple's tools do not surface these values.

---

## Testing Strategy

- A `_MockCommandRunner` records every `run` call and returns
  pre-seeded `RunResult` values keyed by argv tuple.
- Parser tests for the `systemsetup` payloads, the `defaults`
  payload, and the `sntp` offset payload.
- Idempotency tests for `add_time_source` / `remove_time_source` /
  `set_time_servers` driven by pre-seeded `defaults` output.
- Idempotency tests for `enable_sync` / `disable_sync` driven by
  pre-seeded `systemsetup -getusingnetworktime` output.
- Dry-run tests proving the runner is never invoked.
- Elevation policy tests proving every write call passes
  `elevated=True` and every read call passes `elevated=False`.
- A "failed command surfaces stderr" test proving the captured
  exit code, stderr, and command appear on the result.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| Timezone | `3-localization-management` (future) | Disjoint surface |
| Per-source PPS hardware | Out of scope | Not addressed |
| `xntpd` / `ntpd` legacy | Out of scope | Not addressed |

---

## Spec-vs-Contract Drift

* **`sync_service_active` collapsed.** The contract's
  `TimeStatus.sync_service_active` is collapsed into `enabled` on
  `MacosTimeStatus`; macOS exposes a single boolean ("network time
  on / off") rather than a distinct service-active concept.
* **`last_sync_time` shape.** The contract types
  `last_sync_time: datetime`.  This implementation ships
  `last_sync: Optional[str]` and always sets it to `None` because
  Apple's tools do not report a last-sync timestamp through
  user-space surfaces.
* **`stratum` and `offset_seconds` on `MacosTimeStatus`.** The
  contract does not name `offset_seconds` on the status record; the
  macOS type retains it for cross-OS shape parity but always sets it
  to `None`.
* **`MacosTimeSourceInfo` extends the contract.** Adds `stratum` and
  `last_response` for cross-OS shape parity, both always `None`.
* **`MacosTimeDrift` field naming.** The contract's
  `drift_ppm`/`rms_offset_seconds` are renamed to
  `frequency_ppm`/`jitter_seconds`; an `offset_seconds` field is the
  primary drift measure.  `frequency_ppm` is always `None` because
  Apple's tools do not expose it.
* **No `IFileOperator`.** The contract permits an `IFileOperator`
  collaborator; this implementation routes file I/O (legacy ntp.conf
  fallback) through the runner via `cat`.
* **macOS multi-server limitation.** The contract assumes a
  multi-server-natural backend.  macOS's `systemsetup` is
  single-server-oriented; this implementation honours the contract
  verbs uniformly while documenting the underlying limitation.
