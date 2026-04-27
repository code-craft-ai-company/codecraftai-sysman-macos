# Design

## Overview

The macOS nfs-server-management feature is implemented as a single
module, ``automojo.sysman.macos.nfsservermanagement``, exposing a
``MacosNfsServerManager`` class that wraps macOS's ``nfsd`` server
surface (``nfsd update``, ``showmount``, the on-disk ``/etc/exports``
file in Darwin syntax) through the ``ICommandRunner`` abstraction.

Follows the standard manager pattern; delegates daemon lifecycle to
``3-service-management``.

**See Also:**
- ``automojo-sysman-common/specs/contracts/nfs-server-management/contract.md``
  — the cross-OS abstract contract.
- ``automojo-sysman-linux-ubuntu/specs/3-nfs-server-management/``
  — Ubuntu's implementation, which uses the Linux-canonical export
  syntax that Darwin is **not** wire-compatible with.
- ``3-service-management`` — ``com.apple.nfsd`` LaunchDaemon
  lifecycle.
- ``3-firewall-management`` — opening RPC ports through pf /
  socketfilterfw.
- ``3-filesystem-management`` — NFS client-side mount points and
  ``/etc/auto_*`` automounter maps.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosNfsServerManager                            │
│                                                             │
│  Writes:                                                    │
│    add_export(), remove_export(),                           │
│    reload_exports(), set_export_options()                   │
│  Reads:                                                     │
│    list_exports(), get_export_status()                      │
│                                                             │
│  Helpers:                                                   │
│    _read_exports_file()                                     │
│    _invoke_nfsd_update()                                    │
│    _invoke_showmount_e()                                    │
└────────────────┬──────────────────────────┬─────────────────┘
                 │                          │
                 ▼                          ▼
      ┌─────────────────────┐   ┌─────────────────────────┐
      │   ICommandRunner    │   │  /etc/exports (file)    │
      └─────────────────────┘   └─────────────────────────┘
```

Every command — including reads — flows through the injected
``ICommandRunner``.  The manager holds no ``subprocess``, ``open``,
or ``pathlib`` references.

---

## Cross-OS Exports-Syntax Concept Map

The cross-OS contract intentionally generalises the verb shape but
not the file format.  This table summarises the divergence — every
spec in the ``nfs-server-management`` family includes it for cross-
reference.

| Concept | Ubuntu / Linux | macOS (Darwin) | Windows (Server for NFS) |
|---------|----------------|----------------|--------------------------|
| File path | ``/etc/exports`` | ``/etc/exports`` | n/a — share table is in the registry |
| Line shape | ``<path> <client>(<opts>) <client>(<opts>)…`` | ``<path> -<opt> -<opt> <client> [<client>…]`` | n/a — ``New-NfsShare -Path <path> -Name <name>`` |
| Options separator | parenthesised, comma-separated | dash-prefixed, space-separated | named cmdlet parameters |
| Per-client options | yes (each client carries its own opts) | shared across the whole line | yes (per-permission rules attached to the share) |
| Wildcard host | literal ``*`` | not accepted — use ``-network 0.0.0.0 -mask 0.0.0.0`` | ``-AllowRootAccess`` / ``-EnableUnmappedAccess`` |
| Reload command | ``exportfs -ra`` | ``nfsd update`` | ``Restart-Service NfsService`` |
| Per-export reload | yes (re-export all) | yes (whole-table refresh) | no — service-wide restart only |

**Critical Darwin-syntax detail.** macOS uses BSD's exports grammar
inherited from FreeBSD, *not* Linux's parenthesised form.  A Linux-
shaped line such as

```
/srv/share *(rw,sync,no_subtree_check)
```

is **rejected** by macOS ``nfsd`` with a syntax error.  The
canonical Darwin shape is

```
/srv/share -ro -alldirs -mapall=nobody -network 0.0.0.0 -mask 0.0.0.0
```

A line with no options and a single host is also valid:

```
/srv/share 10.0.0.0 -mask 255.255.255.0
```

This Darwin-specific shape is the **single most-load-bearing
difference** between the macOS and Ubuntu NFS managers.  Every
parser, renderer, and idempotency-comparison helper in this module
operates on the Darwin grammar exclusively.

---

## Data Models

```python
MacosNfsActionStatus = ActionStatus  # alias from featurebase


@dataclass
class MacosNfsManagerConfig(FeatureConfig):
    exports_file: str = "/etc/exports"
    service_label: str = "com.apple.nfsd"


@dataclass
class MacosNfsActionResult(FeatureActionResult):
    path: str | None = None
    share_name: str | None = None
    message: str | None = None


@dataclass
class MacosNfsClientSpec:
    host: str = "*"
    options: list[str] = field(default_factory=lambda: [
        "ro", "alldirs", "mapall=nobody",
    ])


@dataclass
class MacosNfsExportSpec:
    path: str
    clients: list[MacosNfsClientSpec]
    share_name: str | None = None  # unused on macOS


@dataclass
class MacosNfsExportInfo:
    path: str
    share_name: str | None
    clients: list[MacosNfsClientSpec]
    is_active: bool


@dataclass
class MacosNfsExportStatus:
    path: str
    is_active: bool
    client_count: int
    flags: list[str]


class MacosNfsQueryError(Exception):
    """Raised when a read fails for a non-recoverable reason."""
```

Note: Even though Darwin scopes options across the entire line
rather than per-client, this module retains the per-client
``options`` field for cross-OS API parity.  When rendering a Darwin
line the manager uses **the options from the first client only**;
the option lists on subsequent clients are ignored.  Callers who
need per-client option scoping must either (a) emit one export per
client, or (b) accept the shared-option semantics.

---

## Verb / Tool Mapping

| Verb | macOS tool |
|------|------------|
| ``add_export`` | ``cat /etc/exports`` + parse + render + ``tee /etc/exports`` + ``nfsd update`` |
| ``remove_export`` | ``cat /etc/exports`` + parse + render + ``tee /etc/exports`` + ``nfsd update`` |
| ``list_exports`` | ``cat /etc/exports`` + parse + ``showmount -e localhost`` for active state |
| ``reload_exports`` | ``nfsd update`` |
| ``get_export_status`` | ``showmount -e localhost`` parse for the path |
| ``set_export_options`` | ``cat`` + parse + render-with-new-options + ``tee`` + ``nfsd update`` |

Reads (``cat /etc/exports``, ``showmount -e localhost``) never
elevate.  Writes (``tee /etc/exports``, ``nfsd update``) elevate per
``config.elevated``.

---

## Export-Path Mapping

The abstract ``path`` parameter maps to a single line inside
``/etc/exports``.  Darwin does NOT support drop-in directories
(``/etc/exports.d/`` is not consulted by the macOS ``nfsd``); the
primary file is the only configuration surface.

| Field | Source |
|-------|--------|
| Configuration location | ``/etc/exports`` |
| Reload mechanism | ``nfsd update`` |
| Path-to-name normalisation | None — Darwin identifies exports by path. |

``MacosNfsExportSpec.share_name`` is preserved for parity with the
Windows implementation (where shares are addressed by share-name)
but is ignored by every macOS verb.

---

## Configuration

```python
@dataclass
class MacosNfsManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosNfsServerManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param exports_file: Absolute path to the primary exports
                             file.  Default ``/etc/exports``.
        :param service_label: launchd label used by callers who wish
                              to restart the daemon.  This module
                              does NOT call launchctl directly.
                              Default ``"com.apple.nfsd"``.
    """
    exports_file: str = "/etc/exports"
    service_label: str = "com.apple.nfsd"
```

---

## Key Method Designs

### `add_export(spec)`

```
1. Build the canonical write argv: ["tee", config.exports_file].
2. Read the current file via ``cat`` (no elevation).
   None when missing.
3. Parse the existing file into list[MacosNfsExportInfo] using the
   Darwin parser.
4. If an existing export at spec.path has identical client list AND
   identical option list → SKIPPED.
5. Render a new content string with the spec's line upserted.
6. Dry-run? → DRY_RUN.
7. Dispatch ``tee``.  FAILED on non-zero exit.
8. Dispatch ``nfsd update``.  FAILED if non-zero — surface the
   reload's argv / exit / stderr.
9. SUCCESS.
```

### `remove_export(path)`

```
1. Build the write argv.
2. Read the file via ``cat``.  When missing → SKIPPED.
3. Strip every line whose first token equals ``path``.
4. Compare to the original; identical → SKIPPED.
5. Dry-run? → DRY_RUN.
6. Dispatch ``tee``; chain ``nfsd update``.
```

### `list_exports()`

```
1. Read /etc/exports via ``cat`` (no elevation).
   Empty list when missing.
2. Parse each non-comment, non-blank line into
   MacosNfsExportInfo.
3. Read active state via ``showmount -e localhost`` (no elevation).
   Failure with stderr → MacosNfsQueryError.
4. For each parsed export, set is_active=True iff the path appears
   in the showmount output.
5. Return the list.
```

### `reload_exports()`

```
1. Build ["nfsd", "update"].
2. Dry-run? → DRY_RUN.
3. Dispatch (elevated per config).
4. Map exit code to SUCCESS / FAILED.
```

### `get_export_status(path)`

```
1. Dispatch ``showmount -e localhost`` (no elevation).
2. Parse output.  When ``path`` is absent → status with
   is_active=False, client_count=0, flags=[].
3. When present, count distinct clients and pull flags from the
   parsed line.  Note: ``showmount -e`` does not emit option
   lists — flags are reconstructed from the configured /etc/exports
   line as a best-effort.
```

### `set_export_options(path, options)`

```
1. Read the file via ``cat``.  Missing → SKIPPED.
2. Locate the line whose first token equals ``path``.  None →
   SKIPPED.
3. Replace the line's option list with the supplied list, preserving
   the path and client tokens.
4. Compare rendered output to existing.  Identical → SKIPPED.
5. Dry-run? → DRY_RUN.
6. Dispatch ``tee`` and chain ``nfsd update``.
```

---

## Darwin Export-Line Parser / Renderer

Two pure-Python helpers live at module scope:

- ``_parse_exports_file(content) → list[MacosNfsExportInfo]``
- ``_render_export_line(spec) → str``

The parser tokenises each non-comment, non-blank line on whitespace.
The first token is the path.  Subsequent tokens are classified as:

- **Option** — token begins with ``-`` and is not a known network
  qualifier (``-network``, ``-mask``).
- **Network qualifier** — ``-network`` or ``-mask``; the next token
  is consumed as the qualifier value.
- **Client** — any other token (hostname, IP literal).

The classifier reconstructs the per-spec data model: every parsed
client carries the *same* option list (Darwin's shared-option
semantics).

The renderer walks the spec's first client's options list, prefixing
each with ``-``, then emits each client host.  When the spec carries
the wildcard host ``"*"``, the renderer emits ``-network 0.0.0.0
-mask 0.0.0.0`` because Darwin does not accept literal ``*`` as a
client matcher.

A second parser, ``_parse_showmount_e_output(text)``, produces a
``dict[str, list[str]]`` mapping path → client list, used by
``get_export_status`` and the active-state probe inside
``list_exports``.

---

## Worked Example — Adding A New Export

```python
runner = LocalCommandRunner()
config = MacosNfsManagerConfig(elevated=True)
manager = MacosNfsServerManager(runner=runner, config=config)
spec = MacosNfsExportSpec(
    path="/Users/Shared/share",
    clients=[MacosNfsClientSpec(
        host="*",
        options=["ro", "alldirs", "mapall=nobody"],
    )],
)
result = manager.add_export(spec=spec)
```

The manager dispatches:

1. ``["cat", "/etc/exports"]`` (``elevated=False``) — read.
2. ``["tee", "/etc/exports"]`` (``elevated=True``,
   ``stdin=<rendered>``) — write.  Stdin contains
   ``/Users/Shared/share -ro -alldirs -mapall=nobody -network 0.0.0.0 -mask 0.0.0.0\n``.
3. ``["nfsd", "update"]`` (``elevated=True``) — reload.

If the file already contains that exact line, only step 1 runs and
the verb returns ``SKIPPED``.

---

## Worked Example — Active Probe via showmount

``showmount -e localhost`` emits one block per export:

```
Exports list on localhost:
/Users/Shared/share        Everyone
/srv/restricted            10.0.0.0
```

The first column is the path, the second is the client matcher (or
``Everyone`` when the line carried the network-zero wildcard).  This
parser ignores the ``Exports list on…`` header line.

---

## Edge Cases And Their Handling

| Edge case | Handling |
|-----------|----------|
| ``/etc/exports`` is missing | ``add_export`` treats existing content as ``""``; ``remove_export`` and ``set_export_options`` return ``SKIPPED``; ``list_exports`` returns ``[]``. |
| File contains comment lines | Comments and blank lines are preserved verbatim through every write path. |
| Multiple lines with the same path | ``_upsert_export`` collapses duplicates to a single canonical line at the first occurrence; ``_remove_export_lines`` strips every duplicate. |
| Spec carries wildcard host ``"*"`` | Renderer expands to ``-network 0.0.0.0 -mask 0.0.0.0`` (Darwin grammar). |
| Spec carries multiple clients with different option lists | The renderer uses **only the first client's option list**; remaining clients' option lists are ignored.  This is documented behaviour driven by Darwin's shared-option grammar. |
| ``-network`` followed by no value | Parser treats the dangling qualifier as a parse error and skips the line. |
| Comment with ``#`` preceded by whitespace | Comments at any column are skipped (line-level test, not column-level). |

---

## Dry-Run Behaviour

Dry-run disables every mutating entry point (``add_export``,
``remove_export``, ``reload_exports``, ``set_export_options``).
Read-only queries — including the ``cat`` / ``showmount`` probes
that drive idempotency decisions inside the write paths — run
freely.

---

## Correctness Properties

1. **Idempotent exports.** Every write verb SKIPs when the rendered
   file content already matches the requested state.
2. **Reload-after-change.** Every successful mutation that touches
   ``/etc/exports`` is followed by ``nfsd update`` unless the
   mutation was SKIPPED.
3. **Reload-failure visibility.** When the chained ``nfsd update``
   fails after a successful ``tee``, the verb returns FAILED and
   the reload's ``command`` / ``exit_code`` / ``stderr`` is on the
   returned result.
4. **Read isolation.** Read verbs never pass ``elevated=True``;
   the ``cat`` / ``showmount -e`` probes used inside write paths to
   drive idempotency also do not elevate.
5. **Service delegation.** This module never invokes ``launchctl``.
   Daemon restart is the responsibility of ``3-service-management``.

---

## Testing Strategy

- ``_MockCommandRunner`` records every call and returns pre-seeded
  ``RunResult`` values keyed by argv tuple.
- Parser tests exercise ``_parse_exports_file`` (including a test
  proving the wildcard expansion) and ``_parse_showmount_e_output``.
- Renderer tests verify Darwin-canonical line shape (dash-prefixed
  options, single space between tokens, wildcard expansion).
- A dedicated ``test_export_line_uses_dash_options_syntax`` test
  proves that the rendered options carry the leading ``-`` —
  guarding against regressions toward the Linux-canonical
  parenthesised form.
- Idempotency, reload-failure-propagation, elevation, dry-run, and
  failure-mapping tests follow the same shape as the Ubuntu / RHEL
  managers.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| ``com.apple.nfsd`` lifecycle | ``3-service-management`` | Delegated |
| Opening NFS RPC ports | ``3-firewall-management`` | Coordinator opens through pf / socketfilterfw |
| Client-side NFS mounts | ``3-filesystem-management`` | Out of scope |
| ``nfs.conf`` (Darwin's nfsd flags) | Out of scope for the pilot | A future ``configure_daemon`` extension may surface ``-N`` / ``-T`` flags via ``nfsd configure`` |

---

## Follow-Ups

- **Per-client option scoping.** Darwin natively shares options
  across the line; the manager's per-client API surface accepts
  divergent option lists but the renderer drops every client's
  options after the first.  A follow-up could detect divergence
  and emit one export-line per client to preserve the per-client
  scoping at the cost of duplicate lines.
- **``nfsd checkexports`` integration.** A future
  ``test_exports_file()`` verb could invoke ``nfsd checkexports``
  before the ``tee`` write to catch syntax errors pre-commit.
- **NFSv4-only mode.** Darwin's nfsd defaults to v3; toggling v4
  requires ``nvfsd configure -N v4`` flags.  Not in scope for the
  pilot.
- **Pure ``-network`` matchers.** When a caller wants a
  ``10.0.0.0/24`` matcher the manager currently surfaces it as a
  literal client host; a follow-up should detect CIDR strings and
  emit ``-network 10.0.0.0 -mask 255.255.255.0`` automatically.

---

## Spec-vs-Contract Drift

This macOS spec deviates from the cross-OS contract in three
documented places:

1. **Per-client option scoping.** Identical to Ubuntu — the
   contract has parallel ``clients`` / ``options`` lists; this
   spec nests options inside each client.  Ubuntu honours the
   per-client scoping natively; macOS *cannot* honour per-client
   scoping at the line level (Darwin grammar shares options across
   the whole line).  The renderer collapses to first-client
   options; this is documented above.
2. **Wildcard normalisation.** The contract permits ``"*"`` as a
   client; Darwin does not.  The renderer transparently expands
   ``"*"`` to ``-network 0.0.0.0 -mask 0.0.0.0``.  Callers see
   ``"*"`` in the spec round-tripped back as ``"*"`` from the
   parser too — the parser folds the network-zero pair back to
   the wildcard literal.
3. **No drop-in support.** Darwin's nfsd does not consult
   ``/etc/exports.d/``; the manager edits ``/etc/exports``
   exclusively.  This is *not* a contract violation — the contract
   does not require drop-in support — but it is called out here
   for cross-OS-parity readers.
