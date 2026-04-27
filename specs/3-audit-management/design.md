# Design

## Overview

The audit-management feature is implemented as a single module,
`automojo.sysman.macos.auditmanagement`, that exposes a
`MacosAuditManager` class.  This spec implements the
audit-management contract defined at
`automojo-sysman-common/specs/contracts/audit-management/contract.md`
for macOS using `audit` (control), `praudit` (decode), and
`launchctl` (daemon lifecycle).  Rule storage is the
`/etc/security/audit_control` file (the `flags` line) and
`/etc/security/audit_user` (per-user overrides).

All system commands go through the `ICommandRunner` interface;
file I/O is performed via `cat` and `tee` against that same
runner rather than a separate `IFileOperator`.  No `subprocess`,
no direct `open`, and no direct `pathlib` calls are made inside
the module.

The feature works identically whether commands execute locally
(via `LocalCommandRunner`) or remotely over SSH (via
`SSHCommandRunner`).  When using SSH, the coordinator can track
reliability metrics and apply execution aspects (persistence,
retry, timeout) without any changes to the `MacosAuditManager`
code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/SSH execution, reliability tracking, and
aspect configuration.

---

## Cross-OS Audit-Model Concept Mapping

The three Wave-2 OSes diverge sharply in how their audit
subsystems are addressed.  The table below captures the model
divergence so that one cross-OS contract can be honoured by
three very different toolchains.

| Concept | Linux (Ubuntu / RHEL) | macOS (BSM) | Windows |
|---|---|---|---|
| Tool name | `auditctl` / `augenrules` / `ausearch` | **`audit` / `praudit` / `auditreduce`** | `auditpol` / `wevtutil` / `Get-WinEvent` |
| Rule shape | Line-based; one argv body per rule (`-w /etc/passwd -p wa -k passwd_changes`) | **Class-based; a comma-separated list of audit-class abbreviations in `/etc/security/audit_control` (`flags:lo,fa,pc`)** | Subcategory-based; a tuple `(subcategory, success, failure)` |
| Persistence | Drop-in file under `/etc/audit/rules.d/`, reloaded with `augenrules --load` | **Edit `/etc/security/audit_control`, then `audit -s` to reload** | `auditpol /set` is itself the persistent write |
| Identity | `-k <key>` token | **The class abbreviation itself (e.g. `lo`)** | The subcategory name |
| Status | `auditctl -s` (multi-line key/value) | **`audit -s` (textual blob)** | `auditpol /get /category:* /r` (CSV) |
| Search | `ausearch -k <key> -m <type> -ts <ts>` | **`praudit -l <log>` text grep** | `Get-WinEvent` hashtable filter |

macOS sits in the **class-based** column.  There is no per-path
or per-syscall granularity comparable to `auditctl -w` / `-a`.
Adding a "rule" means enabling a class flag; removing a rule
means dropping the flag.  This single-axis policy maps cleanly
onto the cross-OS contract via the OS-prefixed types (the
abstract `rule_id` becomes the class abbreviation; the abstract
`subsystem` is `"class"`).

The set of recognised abbreviations (per `audit_control(5)`):

| Token | Class |
|-------|-------|
| `fr` | file read |
| `fw` | file write |
| `fa` | file attribute access |
| `fm` | file attribute modify |
| `fc` | file create |
| `fd` | file delete |
| `cl` | file close |
| `pc` | process |
| `nt` | network |
| `ip` | ipc |
| `na` | non-attributable |
| `ad` | administrative |
| `lo` | login / logout |
| `aa` | authentication and authorisation |
| `ap` | application |
| `io` | ioctl |
| `ex` | exec |
| `ot` | other |
| `all` | all classes |
| `no` | invalid class (sentinel) |

The manager does not gate input on this list (callers may pass
any token), but the table is exposed via the module's
`_KNOWN_CLASSES` constant for tools that want to validate.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner                                      │
│  - Configures aspects (optional)                            │
│  - Injects runner + MacosAuditManagerConfig                 │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosAuditManager                                │
│  automojo.sysman.macos.auditmanagement                      │
│                                                             │
│  add_rule()             remove_rule()                       │
│  list_rules()           enable_auditing()                   │
│  disable_auditing()     get_audit_status()                  │
│  search_events()                                            │
└───────────┬─────────────────────────────────────────────────┘
            │ delegates to
            ▼
┌──────────────────────────┐
│   ICommandRunner         │
│   run(args, elevated)    │
│   → RunResult            │
└──────────┬───────────────┘
           │
           ▼
  ┌──────────────────────────────────┐
  │   LocalCommandRunner   or        │
  │   SSHCommandRunner               │
  └─────────────┬────────────────────┘
                │ optional
                ▼
  ┌──────────────────────────────────┐
  │   PersistenceAspect /            │
  │   RetryAspect / TimeoutAspect    │
  └──────────────────────────────────┘
```

---

## Components

### `MacosAuditManager`

**Location:**
`source/packages/automojo/sysman/macos/auditmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_rule` | `(spec: MacosAuditRuleSpec) → MacosAuditActionResult` | Append `event_class` to `flags`, reload via `audit -s`. |
| `remove_rule` | `(rule_id: str) → MacosAuditActionResult` | Drop `rule_id` from `flags`, reload. |
| `list_rules` | `() → list[MacosAuditRuleInfo]` | Parse `audit_control` flags + `audit_user` rows. |
| `enable_auditing` | `() → MacosAuditActionResult` | `audit -i` + `launchctl bootstrap system <plist>`. |
| `disable_auditing` | `() → MacosAuditActionResult` | `launchctl bootout <label>`. |
| `get_audit_status` | `() → MacosAuditStatus` | Parse `audit -s`. |
| `search_events` | `(query: MacosAuditEventQuery) → list[MacosAuditEvent]` | Decode + filter `praudit -l <log>`. |

### `MacosAuditManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit. |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True`. |
| `audit_control` | `str` | `/etc/security/audit_control` | The audit-control file. |
| `audit_user` | `str` | `/etc/security/audit_user` | The per-user rules file. |
| `audit_log_dir` | `str` | `/var/audit` | Directory holding rotated audit logs. |
| `auditd_plist` | `str` | `/System/Library/LaunchDaemons/com.apple.auditd.plist` | LaunchDaemon plist. |
| `auditd_label` | `str` | `system/com.apple.auditd` | launchctl label. |

---

## Data Models

### Enum

```python
class MacosAuditActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
```

### `MacosAuditRuleSpec`

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Logical rule identifier (acts as the abstract `rule_id`). |
| `event_class` | `str` | BSM audit-class abbreviation. |
| `condition` | `str \| None` | Reserved for the audit_control `naflags` line counterpart. |
| `enabled` | `bool` | When `False`, the rule is recorded but not applied. |

### `MacosAuditRuleInfo`

| Field | Type | Description |
|---|---|---|
| `rule_id` | `str` | The class token for `flags` rows; the user name for `audit_user` rows. |
| `name` | `str` | Mirrors `rule_id`. |
| `event_class` | `str` | Class token. |
| `expression` | `str` | Token text in source form. |
| `subsystem` | `str` | `"class"` for `audit_control` rows, `"user"` for `audit_user` rows. |
| `source` | `str` | Origin file path. |

### `MacosAuditStatus`

| Field | Type | Description |
|---|---|---|
| `enabled` | `bool` | True when `audit -s` reports auditing. |
| `running` | `bool` | True when daemon `pid` is non-zero. |
| `pid` | `int \| None` | Daemon PID. |
| `failure_mode` | `str \| None` | Translated `policy` line (`continue` / `halt`). |
| `rate_limit` | `int \| None` | Reserved. Always `None`. |
| `backlog_limit` | `int \| None` | Reserved. Always `None`. |
| `backlog` | `int \| None` | Reserved. Always `None`. |
| `lost_events` | `int \| None` | Cumulative dropped-record count. |
| `locked` | `bool` | True when `audit -s` reports `locked:yes`. |
| `raw` | `str` | Raw stdout for debuggability. |

### `MacosAuditEventQuery`

| Field | Type | Description |
|---|---|---|
| `key` | `str \| None` | Substring filter against each praudit line. |
| `event_class` | `str \| None` | Substring filter against the second comma-separated field. |
| `since` | `str \| None` | Reserved. |
| `until` | `str \| None` | Reserved. |
| `user` | `str \| None` | Substring filter. |
| `raw_filter` | `str \| None` | Verbatim tokens. |
| `limit` | `int \| None` | Cap on parsed records. |

### `MacosAuditEvent`

| Field | Type | Description |
|---|---|---|
| `timestamp` | `str` | First whitespace token of the decoded line. |
| `event_id` | `int \| None` | Reserved. |
| `category` | `str \| None` | Second comma-separated field. |
| `severity` | `str \| None` | Reserved. |
| `message` | `str` | Short summary. |
| `raw` | `str` | Full decoded line. |

### `MacosAuditActionResult`

Inherits from `FeatureActionResult` and adds `rule_id`,
`rule_key`, `message`.

### Exceptions

| Exception | Raised When |
|---|---|
| `MacosAuditQueryError` | A read fails unexpectedly. |
| `MacosAuditImmutableError` | Reserved for callers who want a typed exception when the subsystem is locked; the manager itself returns `FAILED` per the contract. |

---

## Verb Behaviors

### `add_rule(spec)`

1. **Dry-run guard:** when `config.dry_run` is True, return
   `DRY_RUN`.
2. **Disabled-spec guard:** when `spec.enabled` is False, return
   `SKIPPED` with a descriptive message.
3. Read `audit_control` via `cat`.  When the file is missing,
   start from the empty string.
4. Compute `(new_body, changed)` via `_add_class_to_flags`.
   When `changed` is False, return `SKIPPED`.
5. Dispatch the `tee` write with `elevated=config.elevated` and
   `stdin=new_body`.
6. Map `tee` exit code 0 → `SUCCESS`, non-zero → `FAILED`.
7. Dispatch `audit -s`.  When this reload fails, downgrade to
   `FAILED`.

### `remove_rule(rule_id)`

1. **Dry-run guard.**
2. Read `audit_control`.  When the file is missing, return
   `SKIPPED`.
3. Compute `(new_body, changed)` via `_remove_class_from_flags`.
   When unchanged, return `SKIPPED`.
4. Dispatch `tee` and `audit -s` as for `add_rule`.

### `list_rules()`

1. Read `audit_control` via `cat`.  When present, parse via
   `_parse_audit_control_flags`.
2. Read `audit_user` via `cat`.  When present, parse via
   `_parse_audit_user`.
3. Return the concatenated list.  Missing files are omitted.

### `enable_auditing()`

1. Dry-run guard returns `DRY_RUN`.
2. Read status via `_safe_status()`.  When already enabled and
   running, return `SKIPPED`.  When `locked` is True, return
   `FAILED` with the immutable-mode message.
3. Dispatch `audit -i`.  Map exit code 0 → `SUCCESS`, non-zero
   → `FAILED`.
4. Dispatch `launchctl bootstrap system <auditd_plist>`.  When
   this step fails, downgrade to `FAILED`.

### `disable_auditing()`

1. Dry-run guard.
2. Read status via `_safe_status()`.  When already disabled and
   not running, return `SKIPPED`.  When `locked`, return
   `FAILED`.
3. Dispatch `launchctl bootout <auditd_label>`.

### `get_audit_status()`

1. Dispatch `audit -s` without elevation.
2. Raise `MacosAuditQueryError` on non-zero exit with non-empty
   stderr.
3. Parse via `_parse_audit_status`.  The parser tolerates both
   `<key>:<value>` and `<key> = <value>` formats.

### `search_events(query)`

1. When `query.raw_filter` is non-empty, build
   `["praudit"] + raw_filter.split()`.  Otherwise build
   `["praudit", "-l", "<audit_log_dir>/current"]`.
2. Dispatch without elevation.
3. Raise `MacosAuditQueryError` on non-zero exit with non-empty
   stderr.
4. Parse output via `_parse_praudit_lines`, applying
   substring-match filters from `query.key`, `query.event_class`,
   and `query.user`.  Cap at `query.limit` when set.

---

## Idempotency Model

| Verb | Probe | Skip condition |
|---|---|---|
| `add_rule` | `cat audit_control` | The class is already in the flags line. |
| `remove_rule` | `cat audit_control` | The class is not in the flags line, or the file is missing. |
| `enable_auditing` | `audit -s` | `enabled` is True and `running` is True. |
| `disable_auditing` | `audit -s` | `enabled` is False and `running` is False. |

Probes always run unelevated.  Probe failures fall through to
attempting the operation rather than masking it as a no-op.

---

## Persistence Model

`add_rule` and `remove_rule` are themselves persistent: they
rewrite `/etc/security/audit_control` directly.  After every
write the manager dispatches `audit -s` so the kernel policy is
in sync with the on-disk file.  There is no separate "drop-in"
folder model on macOS — Apple's audit subsystem reads only the
single `audit_control` file plus the per-user `audit_user`.

When the audit subsystem is in immutable mode, the kernel
rejects the `audit -s` reload.  The manager surfaces this as
`status=FAILED` (the file write itself succeeds because
`audit_control` is just a regular file; only the reload fails).

---

## Elevation Policy

| Operation | `elevated` |
|---|---|
| `tee /etc/security/audit_control` | `config.elevated` |
| `audit -i` | `config.elevated` |
| `audit -s` (reload after write) | `config.elevated` |
| `launchctl bootstrap system <plist>` | `config.elevated` |
| `launchctl bootout <label>` | `config.elevated` |
| `cat <audit_control>` | `False` |
| `cat <audit_user>` | `False` |
| `audit -s` (status read) | `False` |
| `praudit -l <log>` | `False` |

Reads never elevate.  Writes elevate when the config opts in.
On macOS Sequoia and later, `praudit` against `/var/audit/current`
may require Full Disk Access for the runner process; this is a
runner-side concern and the manager does not attempt to mask it.

---

## Parsers

### `_parse_audit_control_flags`

Walks the file body looking for the first line that starts with
`flags:`.  Splits the value on commas, trims each token, and
emits one `MacosAuditRuleInfo` per non-empty token.  Subsequent
`flags:` lines are ignored — `audit_control` only honours the
first.

### `_parse_audit_user`

Walks the file body skipping blank lines and `#`-commented
lines.  Each remaining line is split on `:` to extract the
user name (field 0) and always-mask (field 1).  Emits one
`MacosAuditRuleInfo` per parsed line with `subsystem="user"`.

### `_parse_audit_status`

Walks the file body looking for `<key>:<value>`,
`<key> = <value>`, or `<key> <value>` lines.  Recognised keys:

| Key | Maps to |
|-----|---------|
| `auc_auditing` / `audit condition` | `enabled` (True for `auditing`, False for `no audit`/`0`/`off`) |
| `pid` | `pid` (`running=True` when `pid > 0`) |
| `policy` | `failure_mode` (`halt` for `ahlt`, `continue` for `cnt`) |
| `lost` | `lost_events` |
| `locked` / `audit locked` | `locked` (True for `yes`/`true`/`1`) |

Unrecognised keys are tolerated and ignored.

### `_parse_praudit_lines`

`praudit -l` flattens each multi-line record into a single comma-
separated line.  The parser walks every non-empty line, applies
the substring-match filters from the query, and builds a
`MacosAuditEvent` row with `timestamp` taken from field 0 and
`category` taken from field 1.  When `limit` is set the loop
terminates after collecting the first `limit` matches.

---

## Renderers

### `_add_class_to_flags`

1. Splits the input into lines while preserving line endings.
2. Walks every line; when a `flags:` line is encountered, splits
   the value on commas and appends `event_class` if absent.
3. When no `flags:` line is present, appends a new
   `flags:<event_class>` line at the end of the file.
4. Returns the rebuilt body and a `changed` flag indicating
   whether the body differs from the input.

### `_remove_class_from_flags`

Mirror of `_add_class_to_flags`: drops `event_class` from the
existing flags line and reports `changed=False` when the class
was already absent.

---

## Failure Mapping

| Runner outcome | Mapped status |
|---|---|
| Zero exit code (every step) | `SUCCESS` |
| Non-zero `tee` write or `audit -s` reload | `FAILED` |
| Non-zero `audit -i` or `launchctl` | `FAILED` |
| Pre-flight skip | `SKIPPED` |
| Dry-run | `DRY_RUN` |

`MacosAuditQueryError` carries `command`, `exit_code`, and
`stderr` for read-path debuggability.

---

## Testing Strategy

Tests use a `_MockCommandRunner` that records every `run` call
and returns pre-seeded `RunResult` values keyed by argv tuple.
Coverage targets:

1. Each write verb dispatches the canonical argv (`tee`,
   `audit -s`, `audit -i`, `launchctl bootstrap`, `launchctl
   bootout`).
2. Each write verb honours the idempotency guard.
3. Each write verb honours dry-run.
4. `list_rules` parses both `audit_control` flags and
   `audit_user` rows.
5. `get_audit_status` parses representative `audit -s` text
   including the `locked:yes` line.
6. `search_events` builds the expected praudit argv and
   filters lines by substring.
7. Writes elevate when configured; reads never elevate.
8. Failed `tee` exits surface as `FAILED` results with stderr
   intact.
9. Pure-function tests cover the renderers (`_add_class_to_flags`,
   `_remove_class_from_flags`) and parsers
   (`_parse_audit_control_flags`, `_parse_audit_status`,
   `_parse_praudit_lines`) in isolation.

---

## Spec Deviations

- **`IFileOperator` collaborator.** The cross-OS contract names
  an `IFileOperator` as the route for rule-file I/O.  This
  implementation routes every read / write through the
  `ICommandRunner` (`cat` / `tee`).

- **Class-based vs line-based addressing.** The contract's
  abstract `AuditRuleSpec` carries `subsystem`, `expression`,
  and `enabled` fields.  macOS's class-based model collapses
  `subsystem` onto the literal string `"class"` and
  `expression` onto the class token.  This is a documented
  divergence; downstream callers that compare `expression`
  across OSes must be aware of the difference.

- **`reload_rules` verb.** The contract enumerates a
  `reload_rules` verb.  This implementation folds the reload
  into `add_rule` / `remove_rule` (each is followed by
  `audit -s`).

- **`AuditImmutableError` raising.** This manager returns
  `status=FAILED` with `message="audit subsystem is in
  immutable mode"` instead of raising.  The exception type is
  exposed for callers who want to raise it themselves.

- **`disable_auditing` mechanism.** macOS does not expose a
  kernel-side disable toggle equivalent to Linux's
  `auditctl -e 0`.  Disabling means unloading the daemon via
  `launchctl bootout`; the kernel-side audit syscalls still
  exist but no daemon consumes the records.  This is documented
  in the verb's docstring.
