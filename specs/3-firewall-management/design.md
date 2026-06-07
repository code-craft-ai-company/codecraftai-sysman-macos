# Design

## Overview

The firewall management feature is implemented as a single module,
`ccai.sysman.macos.firewallmanagement`, that exposes a `MacosFirewallManager` class.
This spec implements the firewall-management contract defined at
`codecraftai-sysman-common/specs/contracts/firewall-management/contract.md` for macOS
using `pf` (Packet Filter) via the `pfctl` CLI together with an `IFileOperator` for
managing anchor rule files at `/etc/pf.anchors/ccai.<zone>` and, where needed,
`/etc/pf.conf`.  The coordinator instantiates `MacosFirewallManager` and calls its
methods to perform discrete maintenance actions against pf.

All state queries used for idempotency guards go through `pfctl -a ccai.<zone> -sr`
and are parsed in pure Python into a `MacosZoneInfo`.  Anchor file content changes go
through the `IFileOperator`.  System command invocations go through an `ICommandRunner`.
No direct `subprocess` calls and no direct filesystem writes are made inside the
module.

pf rules are declarative: the anchor files on disk are the durable state.  After a
batch of anchor-file changes the manager invokes `pfctl -a ccai.<zone> -f
/etc/pf.anchors/ccai.<zone>` to reload that anchor into the running packet filter;
the coordinator may additionally call `reload()` which re-reads `/etc/pf.conf`.

The feature works identically whether commands execute locally (via `LocalCommandRunner`)
or remotely over SSH (via `SSHCommandRunner`).  When using SSH, the coordinator can
track reliability metrics and apply execution aspects (persistence, retry, timeout)
without any changes to the `MacosFirewallManager` code.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Instantiates file operator (Local or SSH)                │
│  - Configures aspects (optional)                            │
│  - Injects both into MacosFirewallManager                   │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosFirewallManager                             │
│  ccai.sysman.macos.firewallmanagement                   │
│                                                             │
│  add_service()      remove_service()                        │
│  add_port()         remove_port()                           │
│  add_rich_rule()    remove_rich_rule()                      │
│  set_default_zone() reload()                                │
│  query_zone()       query_all_zones()                       │
│  query_state()                                              │
└────────┬────────────────────────┬───────────────────────────┘
         │                        │
         │ delegates to           │ delegates to
         ▼                        ▼
┌──────────────────────┐    ┌──────────────────────┐
│   ICommandRunner     │    │    IFileOperator     │
│ run(args, elevated,  │    │ read_text / write_   │
│  capture_output)     │    │ text / append_text   │
└────────┬─────────────┘    └──────────┬───────────┘
         │                             │
┌────────┴──────┬──────────────┐       │
▼               ▼              ▼       ▼
Local…       SSH…         LocalFileOp / SSHFileOp
```

---

## Components

### `MacosFirewallManager`

**Location:** `source/packages/ccai/sysman/macos/firewallmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_service` | `(service, zone) → MacosFirewallActionResult` | Resolve service to `(port, proto)` via bundled table, append pass rule to anchor file, reload anchor |
| `remove_service` | `(service, zone) → MacosFirewallActionResult` | Resolve service, remove matching rule from anchor file, reload anchor |
| `add_port` | `(port, protocol, zone) → MacosFirewallActionResult` | Append pass rule to anchor file, reload anchor |
| `remove_port` | `(port, protocol, zone) → MacosFirewallActionResult` | Remove pass rule from anchor file, reload anchor |
| `add_rich_rule` | `(rule, zone) → MacosFirewallActionResult` | Append raw pf rule to anchor file, reload anchor |
| `remove_rich_rule` | `(rule, zone) → MacosFirewallActionResult` | Remove matching line from anchor file, reload anchor |
| `set_default_zone` | `(zone) → MacosFirewallActionResult` | Ensure anchor directive ordering in `/etc/pf.conf`, then `pfctl -f /etc/pf.conf` |
| `reload` | `() → MacosFirewallActionResult` | `pfctl -f /etc/pf.conf` |
| `query_zone` | `(zone) → MacosZoneInfo` | Parse `pfctl -a ccai.<zone> -sr` |
| `query_all_zones` | `() → list[MacosZoneInfo]` | `pfctl -sA`, filter `ccai.*`, per-anchor `query_zone` |
| `query_state` | `() → MacosFirewallState` | Parse `pfctl -si` + `pfctl -sA` |

### `MacosFirewallManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands or file writes are executed |
| `elevated` | `bool` | `False` | When `True`, write commands AND file writes pass `elevated=True` |
| `anchor_file_prefix` | `str` | `"/etc/pf.anchors/ccai."` | Path prefix where per-zone anchor files live |
| `pf_conf_path` | `str` | `"/etc/pf.conf"` | Location of the main pf configuration |

---

## Data Models

### Result Types

```python
class MacosFirewallActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class MacosFirewallActionResult:
    status: MacosFirewallActionStatus
    zone: str | None
    detail: str | None          # service name, port/proto, rule string, or zone name
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Query Result Types

```python
@dataclass
class MacosZoneInfo:
    zone: str                   # anchor name without the "ccai." prefix
    services: list[str]
    ports: list[str]            # each entry is "<port>/<protocol>"
    rich_rules: list[str]
    interfaces: list[str]       # populated from "on <ifname>" rule clauses
    sources: list[str]          # populated from "from <addr>" rule clauses
    is_default: bool            # True when this is the first ccai.* anchor in /etc/pf.conf

@dataclass
class MacosFirewallState:
    running: bool
    default_zone: str           # first ccai.* anchor name, or ""

class MacosFirewallQueryError(Exception):
    """Raised when a pfctl query fails."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `IFileOperator` (from `codecraftai-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str, elevated: bool = False) -> str: ...
    def write_text(self, path: str, content: str, elevated: bool = False) -> None: ...
    def append_text(self, path: str, content: str, elevated: bool = False) -> None: ...
    def exists(self, path: str, elevated: bool = False) -> bool: ...
```

---

## Extensions beyond the contract

The base contract defines the firewall feature exclusively in terms of `ICommandRunner`.
macOS pf is fundamentally a configuration-file-driven subsystem: rule state is persisted
as text in `/etc/pf.anchors/*` and `/etc/pf.conf`, and `pfctl` is only the loader.
`MacosFirewallManager` therefore requires an additional `IFileOperator` dependency to
read and write anchor files and `/etc/pf.conf`.

This extension is documented here and does not change the abstract verb surface.  Every
public method still returns the same `*FirewallActionResult` / `*ZoneInfo` /
`*FirewallState` shape mandated by the contract.  Callers that drive only the contract
interface do not need to know about the file operator — they inject it once at
construction time and the manager takes care of it internally.

The `MacosFirewallManagerConfig.anchor_file_prefix` and `pf_conf_path` fields are
OS-specific additions permitted by the contract's "Implementations MAY extend with
OS-specific fields" clause.

---

## Zone Mapping

On macOS the abstract `zone` parameter maps to a pf anchor name.  The manager manages
a single anchor file per zone at `<anchor_file_prefix><zone>` (default
`/etc/pf.anchors/ccai.<zone>`).  The main `/etc/pf.conf` is expected to reference
the anchor via the pair of lines:

```
anchor "ccai.<zone>"
load anchor "ccai.<zone>" from "/etc/pf.anchors/ccai.<zone>"
```

`set_default_zone(zone)` ensures that the specified anchor's directive appears first
among codecraftai-prefixed anchors in `/etc/pf.conf`, because pf evaluates rules in
declaration order.

---

## Command Mappings

### Write Commands

| Operation | File operation | pfctl command |
|---|---|---|
| Add service | Append `pass in quick proto <proto> from any to any port <port>` to `/etc/pf.anchors/ccai.<zone>` | `pfctl -a ccai.<zone> -f /etc/pf.anchors/ccai.<zone>` |
| Remove service | Remove matching line from anchor file | `pfctl -a ccai.<zone> -f /etc/pf.anchors/ccai.<zone>` |
| Add port | Append `pass in quick proto <proto> from any to any port <port>` | `pfctl -a ccai.<zone> -f /etc/pf.anchors/ccai.<zone>` |
| Remove port | Remove matching line | `pfctl -a ccai.<zone> -f /etc/pf.anchors/ccai.<zone>` |
| Add rich rule | Append raw rule line verbatim | `pfctl -a ccai.<zone> -f /etc/pf.anchors/ccai.<zone>` |
| Remove rich rule | Remove matching line (whitespace-normalised) | `pfctl -a ccai.<zone> -f /etc/pf.anchors/ccai.<zone>` |
| Set default zone | Reorder anchor directives in `/etc/pf.conf` | `pfctl -f /etc/pf.conf` |
| Reload | (no file op) | `pfctl -f /etc/pf.conf` |

All write commands and file writes pass `elevated=True` when the config enables it.

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Query one zone | `pfctl -a ccai.<zone> -sr` |
| List anchors | `pfctl -sA` |
| Query running state | `pfctl -si` |

Per the contract, reads MUST NOT pass `elevated=True`.  On a non-privileged macOS
account, `pfctl` queries typically fail; the manager translates that into a
`MacosFirewallQueryError` for `query_zone`/`query_all_zones` and into
`MacosFirewallState(running=False, default_zone="")` for `query_state`.

---

## `pfctl -a ccai.<zone> -sr` Output Parsing

`pfctl -a ccai.<zone> -sr` produces output in the following format:

```
pass in quick proto tcp from any to any port = 22
pass in quick proto tcp from any to any port = 80
pass in quick proto tcp from any to any port = 443
pass in quick on en0 proto tcp from 10.0.0.0/8 to any port = 5432
block drop in quick proto udp from any to any port = 53
```

The parser processes this output as follows:

| Rule shape | Maps to `MacosZoneInfo` field |
|---|---|
| `pass in quick proto <proto> from any to any port = <port>` | `ports` gets `"<port>/<proto>"`; services gets the reverse-lookup name if the bundled services table has one for that `(port, proto)` |
| Any `pass` / `block` / `match` rule with non-`any` `from` or `on <iface>` | `rich_rules` gets the rule verbatim |
| `on <ifname>` clause within a rule | `interfaces` gets `<ifname>` (appended once, de-duplicated) |
| `from <addr>` clause with `<addr>` != `any` | `sources` gets `<addr>` |

The parser:

1. Splits the output on newlines, stripping blank lines and comments (`#`).
2. Normalises `port = <n>` and `port <n>` to a uniform `<n>` in order to compare with
   the bundled services table.
3. Tracks whether each rule is "simple" (port-based, source=any, no interface) or
   "rich" (any other shape) and routes the rule accordingly.
4. De-duplicates across service / port / rich_rules lists.

`is_default` is populated externally by `query_all_zones()` (comparing each zone name
against `query_state().default_zone`) or by `query_zone()` reading `/etc/pf.conf`
ordering through the file operator when called directly.

---

## `pfctl -sA` Output Parsing

`pfctl -sA` lists anchors one per line, e.g.:

```
  ccai.public
  ccai.internal
  com.apple/250.ApplicationFirewall
```

The parser filters to lines beginning (after strip) with `ccai.` and strips the
prefix to obtain the zone name.

---

## Idempotency Guards

All idempotency checks read the anchor file via the file operator and compare against
the desired state in pure Python.  `query_zone()` is used only when the caller asks for
a parsed `MacosZoneInfo`; write idempotency compares directly against anchor-file
lines to avoid requiring `pfctl` to be runnable at idempotency-check time.

| Method | Guard | Source of truth |
|---|---|---|
| `add_service()` | Line already in anchor file | anchor file contents |
| `remove_service()` | Line not in anchor file | anchor file contents |
| `add_port()` | Line already in anchor file | anchor file contents |
| `remove_port()` | Line not in anchor file | anchor file contents |
| `add_rich_rule()` | Normalised line already in anchor file | anchor file contents |
| `remove_rich_rule()` | Normalised line not in anchor file | anchor file contents |
| `set_default_zone()` | Target anchor is first ccai directive | `/etc/pf.conf` contents |

`reload()` has no idempotency guard — it is always executed when not in dry-run mode.

---

## Rich Rule Comparison

Rich rule strings are compared after normalising whitespace (collapsing multiple spaces
to one, stripping leading/trailing whitespace) to avoid false mismatches from formatting
differences between the coordinator-supplied rule and the line stored in the anchor
file.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `MacosFirewallQueryError` | `query_zone()` or `query_all_zones()` returns a non-zero exit code from `pfctl`, or when the anchor cannot be enumerated |

`pfctl -si` returning non-zero in `query_state()` is **not** an error — it means pf is
not running, which is surfaced as `MacosFirewallState(running=False, default_zone="")`.

All other failures from write operations (including file-operator failures) are
surfaced as `MacosFirewallActionResult` with `status=FAILED`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Service Resolution

*For any* known service name, `add_service()` and `remove_service()` should consult the bundled services table and operate on the resolved `(port, protocol)` pair.

**Validates: Requirements 1.1, 2.1**

### Property 2: Service Addition File Mutation

*For any* resolved `(port, protocol)` and zone name, `add_service()` should append `pass in quick proto <protocol> from any to any port <port>` to `/etc/pf.anchors/ccai.<zone>` via the file operator.

**Validates: Requirements 1.1**

### Property 3: Service Addition Reload

*For any* successful anchor-file append, `add_service()` should then invoke `pfctl -a ccai.<zone> -f /etc/pf.anchors/ccai.<zone>` via the command runner.

**Validates: Requirements 1.1, 1.4**

### Property 4: Service Addition Idempotency

*For any* service whose resolved rule is already present in the anchor file, `add_service()` should return `SKIPPED` without writing the file or invoking the runner.

**Validates: Requirements 1.3**

### Property 5: Service Removal Command Construction

*For any* service name and zone, `remove_service()` should remove the matching line from the anchor file and reload the anchor via `pfctl -a ccai.<zone> -f <anchor_file>`.

**Validates: Requirements 2.1**

### Property 6: Service Removal Idempotency

*For any* service whose rule is not present in the anchor file, `remove_service()` should return `SKIPPED` without mutating the file.

**Validates: Requirements 2.3**

### Property 7: Port Addition File Mutation

*For any* `(port, protocol, zone)`, `add_port()` should append `pass in quick proto <protocol> from any to any port <port>` to the anchor file.

**Validates: Requirements 3.1**

### Property 8: Port Addition Idempotency

*For any* `(port, protocol)` whose line is already present in the anchor file, `add_port()` should return `SKIPPED` without mutating the file.

**Validates: Requirements 3.2**

### Property 9: Port Removal File Mutation

*For any* `(port, protocol, zone)`, `remove_port()` should remove the matching line from the anchor file and reload the anchor.

**Validates: Requirements 4.1**

### Property 10: Port Removal Idempotency

*For any* `(port, protocol)` whose line is not present in the anchor file, `remove_port()` should return `SKIPPED` without mutating the file.

**Validates: Requirements 4.2**

### Property 11: Rich Rule Addition File Mutation

*For any* raw pf rule string and zone, `add_rich_rule()` should append the rule verbatim as a new line to the anchor file.

**Validates: Requirements 5.1**

### Property 12: Rich Rule Addition Idempotency

*For any* rule already present (after normalisation) in the anchor file, `add_rich_rule()` should return `SKIPPED` without mutating the file.

**Validates: Requirements 5.2**

### Property 13: Rich Rule Removal File Mutation

*For any* raw pf rule string and zone, `remove_rich_rule()` should remove the matching (whitespace-normalised) line from the anchor file and reload the anchor.

**Validates: Requirements 6.1**

### Property 14: Rich Rule Removal Idempotency

*For any* rule not present in the anchor file, `remove_rich_rule()` should return `SKIPPED` without mutating the file.

**Validates: Requirements 6.2**

### Property 15: Default Zone Directive Reordering

*For any* zone name, `set_default_zone()` should ensure `anchor "ccai.<zone>"` and the matching `load anchor` directive appear first among `ccai.*` anchors in `/etc/pf.conf`, rewriting the file if necessary.

**Validates: Requirements 7.1**

### Property 16: Default Zone Idempotency

*For any* zone already first among `ccai.*` anchors in `/etc/pf.conf`, `set_default_zone()` should return `SKIPPED` without mutating the file.

**Validates: Requirements 7.2**

### Property 17: Reload Command Construction

*For any* call to `reload()`, the system should invoke `pfctl -f /etc/pf.conf` via the command runner.

**Validates: Requirements 8.1**

### Property 18: Zone Query Command Construction

*For any* call to `query_zone(zone)`, the system should invoke `pfctl -a ccai.<zone> -sr` via the command runner without elevation.

**Validates: Requirements 9.1**

### Property 19: Zone Query Result Parsing

*For any* successful `pfctl -a ccai.<zone> -sr` output, `query_zone()` should return a `MacosZoneInfo` with correctly parsed services, ports, rich_rules, interfaces, and sources lists.

**Validates: Requirements 9.2**

### Property 20: Non-Existent Anchor Error Handling

*For any* zone whose anchor does not exist, `query_zone()` should raise `MacosFirewallQueryError`.

**Validates: Requirements 9.3**

### Property 21: Query All Zones Enumeration

*For any* call to `query_all_zones()`, the system should call `pfctl -sA`, filter the result to anchors prefixed with `ccai.`, and return one `MacosZoneInfo` per anchor.

**Validates: Requirements 10.1, 10.2**

### Property 22: Firewall State Enabled Parsing

*For any* `pfctl -si` output containing `Status: Enabled`, `query_state()` should set `running=True`.

**Validates: Requirements 11.2**

### Property 23: Firewall State Default Zone Parsing

*For any* `pfctl -sA` output, when `running=True`, `query_state()` should set `default_zone` to the first `ccai.*` anchor with the prefix stripped, or `""` if none exist.

**Validates: Requirements 11.3**

### Property 24: Firewall Not Running Handling

*For any* failed `pfctl -si` command, `query_state()` should return `MacosFirewallState(running=False, default_zone="")` without raising.

**Validates: Requirements 11.4**

### Property 25: Dry-Run Safety

*For any* write operation (`add_service`, `remove_service`, `add_port`, `remove_port`, `add_rich_rule`, `remove_rich_rule`, `set_default_zone`, `reload`) with `dry_run=True`, neither the command runner nor the file operator should be invoked for mutations, and the result status should be `DRY_RUN`.

**Validates: Requirements 1.6, 2.6, 3.5, 4.5, 5.5, 6.5, 7.5, 8.4**

### Property 26: Elevation for Write Operations

*For any* write operation with `elevated=True` in the config, the command runner AND the file operator should both be invoked with `elevated=True`.

**Validates: Requirements 12.1**

### Property 27: No Elevation for Read Operations

*For any* read operation, the command runner and file operator should never be invoked with `elevated=True` regardless of the elevation configuration.

**Validates: Requirements 12.2**

### Property 28: Error Result Consistency

*For any* failed `pfctl` command or failed file-operator call within a write path, the returned `MacosFirewallActionResult` should have `status=FAILED` and contain the exit code (where available) and stderr from the failing component.

**Validates: Requirements 1.5, 2.5, 3.4, 4.4, 5.4, 6.4, 7.4, 8.3**

---

## Module Layout

```
source/packages/ccai/sysman/macos/
├── __init__.py
├── firewallmanagement.py         ← MacosFirewallManager, config, models, exceptions
└── _services_table.py            ← bundled (service → port, protocol) mapping

source/testroots/testbase/
└── ccai/tests/sysman/macos/
    └── test_firewallmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.macos.fileops import LocalFileOperator, LocalFileOperatorConfig
from ccai.sysman.macos.firewallmanagement import (
    MacosFirewallManager,
    MacosFirewallManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_ops = LocalFileOperator(LocalFileOperatorConfig())

config = MacosFirewallManagerConfig(elevated=True, dry_run=False)
fw_mgr = MacosFirewallManager(
    runner=runner,
    file_operator=file_ops,
    config=config,
)

# Add services to a zone (anchor ccai.public)
result = fw_mgr.add_service(service="ssh", zone="public")
print(f"Add service: {result.status}")

result = fw_mgr.add_service(service="http", zone="public")
print(f"Add service: {result.status}")

# Open custom port
port_result = fw_mgr.add_port(port=8080, protocol="tcp", zone="public")
print(f"Add port: {port_result.status}")

# Add raw pf rule for specific source
rich_rule = "pass in quick on en0 proto tcp from 10.0.0.0/8 to any port = 5432"
rule_result = fw_mgr.add_rich_rule(rule=rich_rule, zone="internal")
print(f"Add rich rule: {rule_result.status}")

# Apply changes
reload_result = fw_mgr.reload()
print(f"Reload: {reload_result.status}")

# Query zone state
zone_info = fw_mgr.query_zone(zone="public")
print(f"Services: {zone_info.services}")
print(f"Ports: {zone_info.ports}")
```

### SSH Execution with Aspects

```python
from ccai.sysman.macos.runners import SSHCommandRunner, SSHRunnerConfig
from ccai.sysman.macos.fileops import SSHFileOperator, SSHFileOperatorConfig
from ccai.sysman.macos.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from ccai.sysman.macos.storage import SQLiteStorage
from ccai.sysman.macos.firewallmanagement import (
    MacosFirewallManager,
    MacosFirewallManagerConfig,
)

ssh_runner_config = SSHRunnerConfig(
    host="mac-build-agent",
    username="admin",
    key_filename="/home/user/.ssh/id_rsa",
)
runner = SSHCommandRunner(ssh_runner_config)
file_ops = SSHFileOperator(SSHFileOperatorConfig(session=runner.session))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

fw_mgr = MacosFirewallManager(
    runner=runner,
    file_operator=file_ops,
    config=MacosFirewallManagerConfig(elevated=True),
)

fw_mgr.add_service(service="ssh", zone="public")
fw_mgr.add_port(port=443, protocol="tcp", zone="public")
fw_mgr.set_default_zone(zone="public")
fw_mgr.reload()

state = fw_mgr.query_state()
print(f"pf running: {state.running}")
print(f"Default ccai anchor: {state.default_zone}")
```

### Dry-Run Mode

```python
# Dry-run short-circuits both the runner AND the file operator
config = MacosFirewallManagerConfig(elevated=True, dry_run=True)
fw_mgr = MacosFirewallManager(
    runner=runner,
    file_operator=file_ops,
    config=config,
)

result = fw_mgr.remove_service(service="ssh", zone="public")
assert result.status == MacosFirewallActionStatus.DRY_RUN
print("Would have removed ssh from ccai.public anchor")
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix and a
  `MockFileOperator` keyed on path.
- `pfctl -a ccai.<zone> -sr` mock responses drive all idempotency guards for
  service, port, and rich rule methods — together with anchor-file contents loaded
  into the `MockFileOperator`.
- `pfctl -sA` mock responses drive `query_all_zones()` enumeration tests.
- `pfctl -si` mocks drive `query_state()` tests including the non-running case.
- Rich rule normalisation is tested in pure Python with representative rule strings.
- Parsing of `pfctl -a ... -sr` is tested with multi-line output strings including
  simple port rules, interface-scoped rules, and source-scoped rules.
- The `set_default_zone()` file-mutation logic is tested against representative
  `/etc/pf.conf` contents — including a case where the target zone is already first,
  a case where it is elsewhere in the file, and a case where it is absent.
- No test requires root access or a live `pf` installation.
