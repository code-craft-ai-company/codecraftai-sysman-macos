# Requirements

## Introduction

The nfs-server-management feature provides the
`automojo-sysman-macos` package with the ability to manage a local
NFS server on macOS hosts.  macOS ships its own ``nfsd`` daemon as
part of the base operating system, exposed under launchd as
``com.apple.nfsd`` (LaunchDaemon plist at
``/System/Library/LaunchDaemons/com.apple.nfsd.plist``).  This spec
implements the cross-OS nfs-server-management contract defined at
``automojo-sysman-common/specs/contracts/nfs-server-management/contract.md``
for macOS, scoped to ``nfsd`` and the on-disk ``/etc/exports`` file
in its Darwin-specific syntax.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *what* exports policy the server enforces —
it provides the capability to *apply* policy reliably, idempotently,
and with full auditability.  All system commands (``nfsd``,
``showmount``, ``cat``, ``tee``) are executed exclusively through the
``automojo-interfaces`` command-runner abstraction.  No
``subprocess``, no direct ``open``, and no direct ``pathlib`` calls
are made inside this module.

The macOS ``/etc/exports`` file is **NOT wire-compatible** with the
Linux canonical form.  Darwin uses the BSD-derived export grammar
inherited from FreeBSD: a single line carries a path, a sequence of
dash-prefixed shared options, and one or more client tokens at the
end.  Reload occurs through ``nfsd update`` (or ``nfsd checkexports``
to validate without committing).  The macOS exports-syntax divergence
from Linux is documented prominently in design.md and called out in
every parser / renderer header.

This feature complements — and does not duplicate — the
``com.apple.nfsd`` lifecycle owned by ``3-service-management``, the
RPC-port firewall surface owned by ``3-firewall-management``, and the
client-side mounting surface owned by ``3-filesystem-management``.

---

## Requirements

### Requirement 1: Add an Export

**User Story:** As a maintenance coordinator, I want to add an NFS
export to ``/etc/exports``, so that the declared share set matches
the desired state and the daemon advertises the new export
immediately.

#### Acceptance Criteria

1. WHEN the coordinator calls ``add_export(spec)`` THE SYSTEM SHALL
   read ``/etc/exports`` via ``cat``, parse the existing exports,
   render the requested spec to its canonical Darwin line, and write
   the result via ``tee /etc/exports`` with the rendered content on
   stdin.
2. WHEN the file already contains an export at ``spec.path`` whose
   client list and option list match exactly THE SYSTEM SHALL return
   ``status=SKIPPED`` without invoking ``tee``.
3. WHEN the file contains an export at ``spec.path`` with a different
   client / option configuration THE SYSTEM SHALL replace the
   existing line in the rendered content.
4. WHEN the write succeeds THE SYSTEM SHALL invoke ``nfsd update`` to
   reload the kernel export table.
5. WHEN ``nfsd update`` fails THE SYSTEM SHALL map the verb to
   ``status=FAILED`` even when the ``tee`` write succeeded.
6. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   without invoking the runner.
7. WHEN the write fails THE SYSTEM SHALL return ``status=FAILED``
   with ``exit_code``, ``stdout``, ``stderr``, and ``command``
   populated.

---

### Requirement 2: Remove an Export

**User Story:** As a maintenance coordinator, I want to remove an
export from ``/etc/exports``, so that the daemon stops advertising
the export and the declared share set matches the desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls ``remove_export(path)`` and the file
   does not exist THE SYSTEM SHALL return ``status=SKIPPED``.
2. WHEN the file does not contain a line whose first token equals
   ``path`` THE SYSTEM SHALL return ``status=SKIPPED``.
3. WHEN one or more lines match ``path`` THE SYSTEM SHALL drop every
   matching line, re-render, and write via ``tee /etc/exports``.
4. WHEN the write succeeds THE SYSTEM SHALL invoke ``nfsd update``.
5. WHEN ``nfsd update`` fails THE SYSTEM SHALL map the verb to
   ``status=FAILED``.
6. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.

---

### Requirement 3: List Exports

**User Story:** As a maintenance coordinator, I want to enumerate
both the configured exports (file content) and the active exports
(``showmount -e localhost``), so that I can compare against the
declared desired state and detect drift.

#### Acceptance Criteria

1. WHEN the coordinator calls ``list_exports()`` and ``/etc/exports``
   does not exist THE SYSTEM SHALL return an empty list.
2. WHEN the file exists THE SYSTEM SHALL parse each non-comment,
   non-blank line into a ``MacosNfsExportInfo`` with ``path``,
   ``clients``, and ``is_active`` populated.
3. WHEN parsing the configured exports THE SYSTEM SHALL also invoke
   ``showmount -e localhost`` to read the active export table.
4. WHEN an export's path appears in both the configured file and the
   ``showmount -e`` output THE SYSTEM SHALL set ``is_active=True``;
   otherwise ``is_active=False``.
5. WHEN ``list_exports`` is invoked THE SYSTEM SHALL pass
   ``elevated=False`` to every runner call.
6. WHEN ``showmount -e localhost`` exits non-zero AND ``stderr`` is
   non-empty THE SYSTEM SHALL raise ``MacosNfsQueryError``.

---

### Requirement 4: Reload Exports

**User Story:** As a maintenance coordinator, I want to re-read every
export from the configuration file into the daemon, so that
configuration changes that bypassed ``add_export`` /
``remove_export`` (e.g., manual edits) take effect.

#### Acceptance Criteria

1. WHEN the coordinator calls ``reload_exports()`` THE SYSTEM SHALL
   invoke ``nfsd update`` via the runner.
2. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   without invoking the runner.
3. WHEN the runner exits 0 THE SYSTEM SHALL return ``status=SUCCESS``.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   ``status=FAILED``.
5. ``reload_exports`` MUST NOT skip — it is operational and
   unconditional.

---

### Requirement 5: Get Export Status

**User Story:** As a maintenance coordinator, I want to query whether
a specific path is currently advertised by the daemon, so that
post-write checks can confirm the export is live before downstream
mounts proceed.

#### Acceptance Criteria

1. WHEN the coordinator calls ``get_export_status(path)`` THE SYSTEM
   SHALL invoke ``showmount -e localhost`` via the runner with
   ``elevated=False``.
2. WHEN the path appears in the ``showmount -e`` output THE SYSTEM
   SHALL return a ``MacosNfsExportStatus`` with ``is_active=True``,
   ``client_count`` set to the number of distinct clients seen, and
   ``flags`` populated from the parsed shared-option list.
3. WHEN the path does NOT appear in the output THE SYSTEM SHALL
   return a ``MacosNfsExportStatus`` with ``is_active=False``,
   ``client_count=0``, and ``flags=[]``.
4. WHEN ``showmount -e localhost`` exits non-zero AND ``stderr`` is
   non-empty THE SYSTEM SHALL raise ``MacosNfsQueryError``.

---

### Requirement 6: Set Export Options

**User Story:** As a maintenance coordinator, I want to update the
shared options portion of an existing export, so that the share's
permissions and behaviour can be tightened or relaxed without
removing and re-adding the export.

#### Acceptance Criteria

1. WHEN the coordinator calls ``set_export_options(path, options)``
   THE SYSTEM SHALL read ``/etc/exports``, locate the line whose
   first token equals ``path``, and rewrite the line with the
   shared-option list replaced by ``options`` (preserving the
   client list).
2. WHEN the existing line already carries exactly ``options`` THE
   SYSTEM SHALL return ``status=SKIPPED`` without invoking ``tee``.
3. WHEN no line matches ``path`` THE SYSTEM SHALL return
   ``status=SKIPPED``.
4. WHEN the write succeeds THE SYSTEM SHALL invoke ``nfsd update``.
5. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.

---

### Requirement 7: Canonical Darwin Export-Line Rendering

**User Story:** As a maintenance coordinator, I want the rendered
export lines in ``/etc/exports`` to follow the canonical Darwin
syntax, so that downstream ``nfsd`` invocations parse the file
without warnings.

#### Acceptance Criteria

1. THE SYSTEM SHALL render an export with one or more clients as
   ``<path> -<opt1> -<opt2>… <client1> [<client2>…]``.
2. THE SYSTEM SHALL prefix every option with a single ``-`` character
   even when the option carries an embedded ``=`` (e.g.,
   ``-mapall=nobody``).
3. THE SYSTEM SHALL place a single space between adjacent tokens
   (path / option / client).
4. THE SYSTEM SHALL NOT emit any leading or trailing whitespace on
   the rendered line.
5. THE SYSTEM SHALL preserve the order in which options and clients
   appear in the spec (no alphabetisation).
6. THE SYSTEM SHALL render `host="*"` as `-network 0.0.0.0 -mask 0.0.0.0`
   when the spec carries the wildcard host, since Darwin's exports
   grammar does NOT accept literal ``*`` as a client matcher.

---

### Requirement 8: Elevation Policy

**User Story:** As a maintenance coordinator, I want every write that
mutates server state to elevate, so that operations succeed on
standard macOS hosts where the process does not run as root.

#### Acceptance Criteria

1. WHEN ``elevated=True`` THE SYSTEM SHALL pass ``elevated=True`` for
   every write verb (``add_export``, ``remove_export``,
   ``reload_exports``, ``set_export_options``) and any chained
   ``nfsd update`` dispatches.
2. WHEN performing reads (``list_exports``, ``get_export_status``)
   THE SYSTEM SHALL pass ``elevated=False`` regardless of the config.
3. WHEN ``elevated=False`` THE SYSTEM SHALL pass ``elevated=False``
   on every path.

---

### Requirement 9: Dry-Run

**User Story:** As a maintenance coordinator, I want to preview every
mutation without dispatching, so that diff-vs-state reports can be
generated before changes go live.

#### Acceptance Criteria

1. WHEN ``dry_run=True`` THE SYSTEM SHALL NOT dispatch any mutating
   command (``tee /etc/exports``, ``nfsd update``).
2. WHEN ``dry_run=True`` THE SYSTEM SHALL still allow read-only
   probes that drive idempotency decisions (``cat /etc/exports``,
   ``showmount -e localhost``).
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   for every mutating verb with ``command`` populated to the
   would-have-been argv.

---

### Requirement 10: Failure Reporting

**User Story:** As a maintenance coordinator, I want every failed
write to surface a structured result, so that downstream automation
can categorise failures without scraping logs.

#### Acceptance Criteria

1. WHEN any write verb's underlying runner call exits non-zero THE
   SYSTEM SHALL return ``status=FAILED`` with ``exit_code``,
   ``stdout``, ``stderr``, and ``command`` populated from the
   runner's ``RunResult``.
2. WHEN a chained command fails (e.g., ``nfsd update`` after a
   successful ``tee``) THE SYSTEM SHALL surface the chained command's
   ``command``, ``exit_code``, and ``stderr`` on the returned result.
3. WHEN a read verb's underlying runner call exits non-zero with
   non-empty ``stderr`` THE SYSTEM SHALL raise ``MacosNfsQueryError``
   carrying ``command``, ``exit_code``, and ``stderr``.

---

### Requirement 11: Idempotency Across Verbs

**User Story:** As a maintenance coordinator, I want every
configuration-mutating verb to be safe to call repeatedly, so that
declarative reconciliation loops do not produce duplicate exports.

#### Acceptance Criteria

1. WHEN ``add_export(spec)`` is called with a spec already present in
   ``/etc/exports`` (same path, same clients, same options) THE
   SYSTEM SHALL return ``status=SKIPPED`` and SHALL NOT invoke
   ``tee`` or ``nfsd update``.
2. WHEN ``remove_export(path)`` is called with no matching line THE
   SYSTEM SHALL return ``status=SKIPPED`` and SHALL NOT invoke
   ``tee`` or ``nfsd update``.
3. WHEN ``set_export_options(path, options)`` is called and the
   existing line already carries exactly ``options`` THE SYSTEM SHALL
   return ``status=SKIPPED`` and SHALL NOT invoke ``tee`` or
   ``nfsd update``.
4. WHEN ``reload_exports()`` is called THE SYSTEM SHALL NOT short-
   circuit; it is the only write verb that always dispatches.
