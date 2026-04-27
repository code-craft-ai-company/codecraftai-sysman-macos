# Design

## Overview

The mail-transport-management feature is implemented as a single
module,
`automojo.sysman.macos.mailtransportmanagement`, exposing a
`MacosMailTransportManager` class that wraps `postconf`,
`postqueue`, `mailq`, `newaliases`, and `sendmail` through the
`ICommandRunner` abstraction.  It also reads and writes
`/etc/aliases` via `cat` / `tee` and re-executes the Postfix master
daemon via `launchctl kickstart -k system/org.postfix.master`.

macOS ships **Postfix** as part of the base operating system.  The
configuration files (`/etc/postfix/main.cf`, `/etc/aliases`) and the
command suite (`postconf`, `postqueue`, `mailq`, `newaliases`,
`sendmail`) are identical to a standard Linux Postfix install — this
is by design, since Apple imports the upstream Postfix tree.  The
divergence is in **daemon management**: launchd, not systemd, owns
the daemon lifecycle, and the daemon is **disabled by default** on
consumer macOS.

The Postfix master LaunchDaemon plist ships at
`/System/Library/LaunchDaemons/org.postfix.master.plist`.  On
consumer macOS it is **not loaded** until an operator explicitly
bootstraps it.  Enabling the daemon (`launchctl bootstrap system
<plist>` or the legacy `launchctl load -w <plist>`) is the
responsibility of the service-management feature.  This module
manages the *configuration* assuming a daemon-management story
exists elsewhere.  When the LaunchDaemon is not loaded, the
trailing `launchctl kickstart -k` after `set_mta_config` will fail
with a `Could not find service` error; the manager surfaces that
failure on the result.

This feature does NOT own the `org.postfix.master` lifecycle —
that lives in `3-service-management`.  The reload after
`set_mta_config` is the sole exception: it is the canonical "apply
directive change" signal on macOS and is idiomatic to this feature.

**See Also:**
- `3-service-management` — `org.postfix.master` lifecycle.
- `3-firewall-management` — SMTP / submission / smtps ports.
- `3-certificate-trust-management` / `3-certificate-issuance` — TLS
  key/cert material for SMTP+TLS.

---

## Architecture

```
+-----------------------------------------------------------------+
|            Maintenance Coordinator                              |
+-----------------------------------------------------------------+
                       |
                       v
+-----------------------------------------------------------------+
|          MacosMailTransportManager                              |
|                                                                 |
|  Reads:                                                         |
|     get_mta_config()    -> postconf -n  -> parse                |
|     list_aliases()      -> cat /etc/aliases -> parse            |
|     get_queue_status()  -> mailq -> parse                       |
|                                                                 |
|  Writes:                                                        |
|     set_mta_config()    -> postconf -e per setting +            |
|                            launchctl kickstart -k               |
|                              system/org.postfix.master          |
|     add_alias()         -> cat + render + tee + newaliases      |
|     remove_alias()      -> cat + render + tee + newaliases      |
|     test_mail_delivery()-> sendmail -t (stdin payload)          |
|     flush_queue()       -> postqueue -f                         |
+-----------------------------------------------------------------+
                       |
                       v
+-----------------------------------------------------------------+
|                ICommandRunner                                   |
+-----------------------------------------------------------------+
```

---

## Configuration

```python
@dataclass
class MacosMtaManagerConfig(FeatureConfig):
    aliases_file: str = "/etc/aliases"
    service_label: str = "org.postfix.master"
    service_domain: str = "system"
    reload_after_set: bool = True
```

Inherited from `FeatureConfig`:

- `dry_run: bool = False` — when True, mutations short-circuit to
  `DRY_RUN`.
- `elevated: bool = False` — when True, writes pass `elevated=True`
  to the runner.

`reload_after_set` controls whether `set_mta_config` issues the
trailing `launchctl kickstart -k <domain>/<label>` after a
successful change.  On consumer macOS where the LaunchDaemon is not
loaded, callers SHOULD set this to `False` to avoid the
`Could not find service` failure surface.

---

## Data Models

```python
class MacosMtaActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class MacosMtaActionResult(FeatureActionResult):
    setting: str | None = None
    local: str | None = None


@dataclass
class MacosMtaConfig:
    myhostname: str | None = None
    mydomain: str | None = None
    myorigin: str | None = None
    relayhost: str | None = None
    mynetworks: str | None = None
    inet_interfaces: str | None = None
    inet_protocols: str | None = None
    mailbox_size_limit: int | None = None
    message_size_limit: int | None = None
    extras: dict[str, str] = field(default_factory=dict)


@dataclass
class MacosMtaAliasInfo:
    local: str
    remote: str


@dataclass
class MacosMtaQueueStatus:
    pending_count: int = 0
    deferred_count: int = 0
    total_size_bytes: int | None = None
    queue_age_oldest: str | None = None


class MacosMtaQueryError(Exception):
    ...
```

---

## MTA Mapping

The cross-OS contract requires every implementation to document the
default MTA, the configuration paths it manages, and the
"no-MTA-installed" detection procedure.

| Concept                      | macOS                                  |
|------------------------------|----------------------------------------|
| Default MTA                  | Postfix (system-shipped)               |
| Service unit                 | `org.postfix.master` (launchd          |
|                              | system-domain LaunchDaemon)            |
| Plist                        | `/System/Library/LaunchDaemons/`       |
|                              | `org.postfix.master.plist`             |
| Daemon default state         | NOT LOADED (consumer macOS)            |
| Primary config               | `/etc/postfix/main.cf` (managed via    |
|                              | `postconf` rather than direct edits)   |
| Aliases file                 | `/etc/aliases`                         |
| Compiled aliases DB          | `/etc/aliases.db` (rebuilt by          |
|                              | `newaliases`)                          |
| Reload signal                | `launchctl kickstart -k`               |
|                              | `system/org.postfix.master`            |
| Detection                    | If `postconf -n` exits 127 / "command  |
|                              | not found", the host has no Postfix    |
|                              | tooling.  If the kickstart fails with  |
|                              | "Could not find service", the daemon   |
|                              | is not loaded.  The pilot raises       |
|                              | `MacosMtaQueryError` on read failure   |
|                              | and surfaces FAILED on write failure.  |

### Cross-OS MTA Concept Mapping

The cross-OS contract abstracts the same conceptual operations
across very different MTA management stories:

| Concept              | RHEL / Ubuntu (systemd)              | macOS (launchd)                                 | Windows (no built-in MTA)            |
|----------------------|--------------------------------------|-------------------------------------------------|--------------------------------------|
| Daemon lifecycle     | `systemctl start/stop postfix`       | `launchctl bootstrap/bootout system <plist>`    | N/A                                  |
| Daemon reload        | `systemctl reload postfix`           | `launchctl kickstart -k system/org.postfix.master` | N/A (or `Restart-Service SMTPSVC`) |
| Default state        | Enabled (or installable)             | Present-but-unloaded                            | Absent                               |
| Directive read       | `postconf -n`                        | `postconf -n`                                   | `Get-WmiObject IIsSmtpServer` (legacy) |
| Directive write      | `postconf -e <key>=<value>`          | `postconf -e <key>=<value>`                     | WMI / N/A                            |
| Aliases file         | `/etc/aliases`                       | `/etc/aliases`                                  | N/A                                  |
| Queue read           | `mailq` / `postqueue -p`             | `mailq` / `postqueue -p`                        | IIS SMTP queue dir                   |
| Queue flush          | `postqueue -f`                       | `postqueue -f`                                  | `Restart-Service SMTPSVC`            |
| Probe                | `sendmail -t`                        | `sendmail -t`                                   | `Send-MailMessage` (PowerShell)      |

Ubuntu and macOS share the Postfix command suite verbatim — the
divergence is entirely in the lifecycle / reload story.  Windows
diverges on every axis (no built-in MTA at all on default Windows
SKUs after the IIS SMTP deprecation).

---

## Key Method Designs

### `get_mta_config()`

1. Dispatch `["postconf", "-n"]` with `elevated=False`.
2. If exit non-zero AND stderr non-empty, raise
   `MacosMtaQueryError`.
3. Walk each line, split on `=`, place into the named field or
   `extras`.
4. Coerce integer-typed directives via `int()`.

### `set_mta_config(spec)`

1. Honour dry-run: return `DRY_RUN`.
2. Call `get_mta_config()` to load the live directive set.  If it
   raises, surface a `FAILED` result and stop.
3. For each named field on `spec` (skipping `None`s) and each entry
   in `spec.extras`, compare against the live value:
   - If equal: record `SKIPPED <key>=<value>` in the audit trail.
   - If different: dispatch `["postconf", "-e",
     "<key>=<value>"]` with `elevated`.  On non-zero, surface
     `FAILED` and stop the loop.
4. If at least one directive changed AND `config.reload_after_set`,
   dispatch `["launchctl", "kickstart", "-k",
   "<domain>/<label>"]`.  On non-zero, surface `FAILED`.
5. If no directives changed, return `SKIPPED`; otherwise return
   `SUCCESS`.

### `add_alias` / `remove_alias`

Same flow as the Ubuntu pilot — the command suite is identical:

1. Read `/etc/aliases` via `cat`.
2. Skip when the desired state already matches.
3. Build new content via `_upsert_alias` / `_remove_alias`.
4. Honour dry-run.
5. Dispatch `["tee", "/etc/aliases"]` with new content on stdin.
6. Chain `["newaliases"]` to rebuild the compiled aliases
   database.  On chained failure, return `FAILED`.

### `list_aliases`, `get_queue_status`, `flush_queue`,
### `test_mail_delivery`

Identical to the Ubuntu pilot.  See `_parse_aliases_file`,
`_parse_mailq_output`, `_build_probe_message`.

---

## Idempotency Model

| Verb                    | Idempotency probe                      | Skip when                                  |
|-------------------------|----------------------------------------|--------------------------------------------|
| `set_mta_config`        | `postconf -n` per-directive compare    | Live value already matches spec            |
| `add_alias`             | `cat /etc/aliases` + parse             | (local, remote) pair already in file       |
| `remove_alias`          | `cat /etc/aliases` + parse             | No entry for local (or file missing)       |
| `test_mail_delivery`    | none — operational                     | Never                                      |
| `flush_queue`           | none — operational                     | Never                                      |

---

## Aliases-Database Coherence

Every successful mutation of `/etc/aliases` is followed by
`newaliases`.  Failure of `newaliases` maps the verb to `FAILED`
even when the `tee` write succeeded, mirroring the Ubuntu pilot.

---

## Reload-After-Set on macOS

`set_mta_config` issues `launchctl kickstart -k <domain>/<label>`
after at least one `postconf -e` succeeds.  This is gated on
`config.reload_after_set` (default `True`).

The `-k` flag instructs launchctl to STOP the running instance
before re-launching it under the same plist.  This is closer to a
full restart than a true "reload" — Postfix's master process does
not honour SIGHUP-driven re-read on macOS the same way it does on
Linux's systemd.  The kickstart restart approach is the canonical
"apply directive change" signal.

When the LaunchDaemon is **not loaded** (the consumer-macOS
default), the kickstart will fail with `Could not find service
"org.postfix.master"`.  Callers should either:

1. Bootstrap the daemon first via the service-management feature.
2. Set `reload_after_set=False` and rely on a separate "load and
   start" flow handled by service-management.

---

## Dry-Run Behaviour

Identical to Ubuntu — disables every mutating dispatch
(`postconf -e`, `tee`, `newaliases`, `sendmail -t`,
`postqueue -f`, `launchctl kickstart`).  Idempotency probes
continue to run.

---

## Elevation Policy

| Verb                    | Elevated when `config.elevated=True`     | Notes                                      |
|-------------------------|------------------------------------------|--------------------------------------------|
| `get_mta_config`        | NO — read                                |                                            |
| `set_mta_config`        | YES                                      | Per-directive `postconf -e` + kickstart    |
| `add_alias`             | YES                                      | `tee` + `newaliases`                       |
| `remove_alias`          | YES                                      | `tee` + `newaliases`                       |
| `list_aliases`          | NO — read                                |                                            |
| `test_mail_delivery`    | YES                                      | `sendmail` enqueues on behalf of root      |
| `get_queue_status`      | NO — read                                | `mailq` does not require elevation         |
| `flush_queue`           | YES                                      | `postqueue -f` requires write to spool     |

---

## Correctness Properties

1. **Idempotent directive sets.** `set_mta_config` SKIPs when every
   directive in `spec` already equals the live value.
2. **Alias-database coherence.** Every successful mutation of
   `/etc/aliases` triggers `newaliases`; `newaliases` failure maps
   the verb to `FAILED`.
3. **Reload coherence.** Successful `set_mta_config` runs trigger
   `launchctl kickstart` exactly once at the end (when changes
   occurred AND `reload_after_set=True`).
4. **Read isolation.** No read verb passes `elevated=True`; no
   read verb mutates state.
5. **Dry-run safety.** No mutating dispatch occurs when
   `dry_run=True`; idempotency probes still run.
6. **Daemon-not-loaded surface.** When the LaunchDaemon is not
   loaded, the kickstart failure is surfaced via FAILED on
   `set_mta_config`; the per-directive writes still went through,
   so callers can recover without re-running the directives.

---

## Failure Surface

Every write verb returns a `MacosMtaActionResult`.  On runner
non-zero:

- `status = FAILED`
- `exit_code` = runner's exit code
- `stdout` / `stderr` = runner's captured streams
- `command` = the argv that ran

Read verbs raise `MacosMtaQueryError` carrying `command`,
`exit_code`, and `stderr`.

---

## Module-Level Helpers

| Helper                          | Role                                          |
|---------------------------------|-----------------------------------------------|
| `_parse_postconf_n_output`      | `postconf -n` -> `MacosMtaConfig`             |
| `_apply_directive`              | Place one directive into a config             |
| `_config_to_directive_pairs`    | Flatten config to ordered key/value pairs    |
| `_parse_aliases_file`           | `/etc/aliases` -> list of entries             |
| `_upsert_alias`                 | Render new content for `add_alias`            |
| `_remove_alias`                 | Render new content for `remove_alias`         |
| `_parse_mailq_output`           | `mailq` -> `MacosMtaQueueStatus`              |
| `_build_probe_message`          | Build the `sendmail -t` payload               |
| `_DirectivePairs`               | Ordered key/value list with first-match       |

---

## Testing Strategy

- `_MockCommandRunner` records every `run` call and returns
  pre-seeded `RunResult` values keyed by argv tuple.
- Parser tests for `postconf -n`, `mailq`, and aliases-file content
  drive the helpers directly.
- Integration tests on the manager verify:
  - `get_mta_config` parses representative output and never
    elevates.
  - `set_mta_config` invokes the per-setting argv, skips matched
    directives, dispatches `launchctl kickstart` once, and
    short-circuits under dry-run.
  - `set_mta_config` surfaces `FAILED` with the `Could not find
    service` stderr when the kickstart fails (consumer-macOS
    scenario).
  - `add_alias` / `remove_alias` invoke `tee` and chain
    `newaliases`, skip when the desired state matches, and
    short-circuit under dry-run.
  - `list_aliases` parses entries and never elevates.
  - `test_mail_delivery` builds the expected RFC 5322 payload and
    invokes `sendmail -t` with stdin.
  - `get_queue_status` parses representative `mailq` output.
  - `flush_queue` invokes `postqueue -f`.
  - Writes elevate when configured; reads never elevate.
  - Failures surface `exit_code` / `stderr`.
  - `add_alias` maps to `FAILED` when `newaliases` fails after a
    successful `tee`.

---

## Seams With Other Features

| Seam                              | Owner                                    | This Feature             |
|-----------------------------------|------------------------------------------|--------------------------|
| `org.postfix.master` lifecycle    | `3-service-management`                   | Reload only              |
| SMTP / submission ports           | `3-firewall-management`                  | Out of scope here        |
| TLS cert / key for SMTP+TLS       | `3-certificate-issuance` / trust         | Caller installs first    |
| User mailbox storage paths        | `3-storage-management`                   | Out of scope here        |
| Mail-queue spool filesystem       | `3-storage-management`                   | Out of scope here        |

---

## Out of Scope

- Loading / unloading the `org.postfix.master` LaunchDaemon —
  delegated to `3-service-management`.
- TLS / SASL configuration directives beyond the `extras` dict.
- Virtual domains and transport maps (`postmap`-managed lookup
  tables).
- Per-message tracing beyond `test_mail_delivery`.
- Mailbox format selection (Maildir vs mbox).
- Greylisting and anti-spam integration.
- `master.cf` per-service directive mutation.

---

## Open Questions

1. Should the pilot probe `launchctl print system/org.postfix.master`
   first to confirm the daemon is loaded before issuing the
   kickstart?  Today we just dispatch and surface the failure on
   the result.  A pre-flight probe would let the manager produce a
   clearer "daemon not loaded" detail string.
2. Should the manager auto-disable `reload_after_set` when the
   daemon is detected as not-loaded?  This would mean
   `set_mta_config` writes the directives without attempting the
   restart and reports `SUCCESS`.  Currently the operator must opt
   in by passing `reload_after_set=False`.
3. Should the macOS pilot emit `launchctl kickstart -k` or the
   newer `launchctl kill -SIGHUP`?  Postfix's master DOES handle
   SIGHUP for re-read; the kickstart approach is heavier-handed
   but more predictable across launchd versions.
