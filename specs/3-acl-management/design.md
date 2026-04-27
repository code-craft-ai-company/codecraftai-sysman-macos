# Design

## Overview

The macOS ACL manager wraps the BSD ``chmod`` extended-ACL surface
(`chmod +a`, `chmod -a`, `chmod -N`) for writes and ``ls -le`` for
reads, and exposes the cross-OS ACL-management contract through a
single class, `MacosAclManager`, dispatched entirely through the
injected `ICommandRunner`.

This design document covers:

1. The cross-OS ACL-model divergence that drives the per-OS naming
   and per-OS spec divergence.
2. The macOS-specific verb-to-command mapping table.
3. The principal-kind / permission / rights model.
4. Idempotency, dry-run, elevation, and error-surface behaviour.
5. Internal helpers (parsing `ls -le`, rendering `chmod` tokens).

---

## Cross-OS ACL-Model Divergence

ACLs are nominally a single concept ("who can do what to a path"),
but the three OS families this product targets carry deeply
divergent ACL models.  Pretending the divergence does not exist
produces leaky abstractions; making the divergence explicit lets
each per-OS implementation stay honest to its native model.

| Dimension | POSIX (Ubuntu / RHEL) | macOS (NFSv4-style) | Windows (NTFS / NT DACL) |
|-----------|-----------------------|---------------------|--------------------------|
| Permission set | 3 bits per principal: `r`, `w`, `x`. | ~20 fine-grained tokens (`read`, `write`, `execute`, `delete`, `append`, `readattr`, `writeattr`, `readextattr`, `writeextattr`, `readsecurity`, `writesecurity`, `chown`, `list`, `search`, `add_file`, `add_subdirectory`, `delete_child`, `file_inherit`, `directory_inherit`, `limit_inherit`, `only_inherit`). | `[FileSystemRights]` enum: `FullControl`, `Modify`, `ReadAndExecute`, `Read`, `Write`, `ListDirectory`, `ReadData`, `WriteData`, `Delete`, `Synchronize`, `ExecuteFile`, `ReadAttributes`, `WriteAttributes`, `ReadPermissions`, `ChangePermissions`, `TakeOwnership`. |
| ACE polarity | Always allow.  POSIX has no deny ACEs. | Allow or deny per ACE. | Allow or deny per ACE. |
| Inheritance | Default ACL on directories, applied on creation; no per-ACE inheritance flags. | Per-ACE flags: `file_inherit`, `directory_inherit`, `limit_inherit`, `only_inherit`. | Per-ACE flags: `OBJECT_INHERIT`, `CONTAINER_INHERIT`, plus a `NO_PROPAGATE_INHERIT` flag and a propagation flag set. |
| Effective-rights mask | A `mask::` row that clamps every named entry's effective rights. | None. | None. |
| Principal model | POSIX user / group name resolved via NSS; plus `other` and `mask` pseudo-principals. | Native user / group resolved via Directory Services; `everyone` keyword. | SID resolved via LSA, with `DOMAIN\name` and well-known SIDs (`S-1-1-0` Everyone, `S-1-5-18` SYSTEM). |
| Native tooling | `setfacl` / `getfacl`. | `chmod +a` / `chmod -a` / `chmod -N` / `ls -le`. | `Get-Acl` / `Set-Acl` (PowerShell, `[System.Security.AccessControl.FileSystemSecurity]`); `icacls.exe` (legacy CLI). |
| Default-ACL equivalent | First-class (the `default:` prefix). | None on HFS+ / APFS — inheritance is per-ACE flag-driven. | None — inheritance is per-ACE flag-driven. |

The contract papers over this divergence by reducing the ACE shape
to `principal + rights + allow + inherited + is_default`.  Each per-
OS implementation MAY (and on macOS, does) carry additional fields
that have no cross-OS analogue.

This macOS spec exposes the NFSv4-style native subset directly.
Where the contract's abstract `default_entries` field exists for
POSIX parity, the macOS manager always returns an empty list — the
canonical macOS inheritance idiom is per-ACE inheritance flags
(`file_inherit` / `directory_inherit`) that travel as part of the
rights vocabulary, not a partition.

---

## Module Layout

```
source/packages/automojo/sysman/macos/aclmanagement.py
```

Contents (top-to-bottom):

1. Module docstring (verb catalogue, file-I/O policy, elevation
   policy, idempotency policy, ACL-model commentary).
2. Module-level constants (binary names, flag tokens).
3. `MacosAclActionStatus` (alias of the package-shared
   `ActionStatus`).
4. `MacosAcePrincipalKind` enum (`USER`, `GROUP`).
5. `MacosAcePermission` enum (`ALLOW`, `DENY`).
6. `MacosAclManagerConfig` dataclass.
7. `MacosAclActionResult` dataclass.
8. `MacosAceInfo` / `MacosAceSpec` / `MacosAclInfo` dataclasses.
9. `MacosAclQueryError` exception.
10. `MacosAclManager` class.
11. Module-level helpers: token rendering, parsing, comparison.

---

## Verb-to-Command Mapping (macOS)

| Verb | Tool invocation | Notes |
|------|-----------------|-------|
| `get_acl(path)` | `ls -lde <path>` | First line is `ls -l`; subsequent lines are `<index>: <principal> <permission> <rights>`. |
| `list_aces(path)` | `ls -lde <path>` (re-uses `get_acl`) | Returns the parsed `aces` list (no default partition on macOS). |
| `set_acl(path, acl_spec)` | `chmod -N <path>` then `chmod +a "<rendered>" <path>` per ACE | Two-step "clear then set"; per-ACE write fan-out is unavoidable because `chmod` accepts only one ACE per invocation. |
| `add_ace(path, principal, rights)` | `chmod +a "<rendered>" <path>` | Token: `<kind>:<name> <permission> <comma-rights>`. |
| `remove_ace(path, principal)` | `chmod -a "<rendered>" <path>` per matching ACE | `chmod -a` requires an exact ACE token match; we re-render from the parsed `MacosAceInfo`. |
| `clear_acl(path)` | `chmod -N <path>` | Removes all ACEs while preserving the classic mode bits. |
| `copy_acl(src, dst)` | `ls -lde <src>` then `chmod -N <dst>` then `chmod +a "<rendered>" <dst>` per source ACE | macOS has no native `getfacl`-style stream representation; we re-render each parsed ACE. |

The "fan-out one chmod call per ACE" pattern is the central macOS-
specific choice.  POSIX ACL specs travel through `setfacl` as a
textual stream that mirrors `getfacl` output (so a single
`setfacl --set-file=-` invocation suffices), but macOS `chmod` has
no equivalent stream input — every ACE is its own argument.  This
is honest to the underlying tool and keeps the dispatch logic
debuggable: each ACE corresponds to exactly one runner call, and a
per-ACE failure surfaces immediately with the offending ACE token
preserved on `command`.

---

## Token Rendering Rules

Every `chmod +a` and `chmod -a` token follows the canonical macOS
form:

```
<kind>:<name> <permission> <comma-joined rights>
```

| Field | Render |
|-------|--------|
| `<kind>` | `user` or `group` (the `MacosAcePrincipalKind.value`). |
| `<name>` | The principal name verbatim. |
| `<permission>` | `allow` or `deny` (the `MacosAcePermission.value`). |
| `<rights>` | Comma-joined rights tokens, in order; when `MacosAceSpec.inheritable=True`, the canonical inheritance pair (`file_inherit,directory_inherit`) is appended if not already present. |

Examples:

```python
MacosAceSpec("alice", USER, ALLOW, ["read", "write"])
    -> "user:alice allow read,write"

MacosAceSpec("admin", GROUP, DENY, ["delete"])
    -> "group:admin deny delete"

MacosAceSpec("alice", USER, ALLOW, ["read"], inheritable=True)
    -> "user:alice allow read,file_inherit,directory_inherit"
```

---

## Parsing `ls -le` Output

`ls -le` emits one ACE per line, prefixed by an index marker:

```
drwxr-xr-x+ 1 alice staff 64 Jan  1 12:00 /srv/data
 0: user:alice allow read,write,execute
 1: group:wheel allow read,execute
 2: user:bob deny delete,file_inherit,directory_inherit
```

The parser:

1. Treats the first line as the standard `ls -l` long-form line.
   The third whitespace-delimited field is the owner; the fourth is
   the group.
2. For each subsequent line: strip leading whitespace, split on the
   first colon to extract the index integer.
3. The remainder is split into three tokens by whitespace: the
   `<kind>:<name>` principal token, the `<permission>` token
   (`allow` / `deny`), and the comma-joined `<rights>` token.
4. The principal token is split on `:` to separate kind from name.
5. The rights token is split on `,` and stripped per element.
6. The `inheritable` boolean is set when any of `file_inherit`,
   `directory_inherit`, `limit_inherit`, or `only_inherit` appears
   in the rights list — but the verbatim tokens are also retained
   on `MacosAceInfo.rights` for round-trip fidelity.

The parser intentionally does NOT attempt to recover from truly
malformed `ls -le` output — `ls` on macOS emits the canonical format
unconditionally, and any deviation indicates a real bug we want to
surface (not paper over).

---

## Idempotency

Each write verb runs an idempotency probe BEFORE dispatching:

| Verb | Probe behaviour |
|------|----------------|
| `set_acl` | Compares the parsed current ACL against `acl_spec` on a tuple ordered by `(kind, principal, permission, rights_set)`.  Source order does not matter; `rights_set` is compared as a `frozenset` so ordering of tokens within an ACE does not matter either. |
| `add_ace` | Returns `SKIPPED` when an ACE matching `(kind, principal, permission, rights_set, inheritable)` is already present. |
| `remove_ace` | Returns `SKIPPED` when no ACE matches the spec's `(kind, principal)` pair, regardless of permission / rights. |
| `clear_acl` | Returns `SKIPPED` when the parsed ACL has zero ACEs. |
| `copy_acl` | Returns `SKIPPED` when the source and destination ACLs are tuple-equal.  Owner / group are intentionally NOT compared — `copy_acl` is about ACE state, not file ownership. |

When a probe `ls -le` invocation itself fails, the manager falls
through and dispatches the write anyway (where possible).  The
write step's failure mode then reports the underlying condition,
and the caller sees a clean `FAILED` result.

---

## Dry-Run Model

`config.dry_run=True` short-circuits every write verb at the very
top of the method, BEFORE any probe runs.  This means a dry-run
invocation does NOT consult `ls -le`, does NOT touch the runner,
and reports the would-have-been argv on `command`.

For `set_acl` (which fans out into multiple calls), the dry-run
result populates `command` with a flattened pseudo-argv that
encodes every step joined by literal `&&` markers — this gives the
caller a faithful preview of the full sequence without committing
to any one ordering of dispatch.

---

## Elevation Model

| Verb | Elevation |
|------|-----------|
| `get_acl`, `list_aces` | Never elevate. |
| Idempotency probes (`ls -le` calls inside write verbs) | Never elevate. |
| Source-side read of `copy_acl` | Never elevate. |
| `chmod -N`, `chmod +a`, `chmod -a` | `config.elevated`. |

NFSv4-style ACL writes typically require root or path-ownership.
The elevation flag is the caller's contract for "I want this
manager to run mutating commands as root via the runner's
elevation mechanism".  Reads are deliberately excluded — `ls -le`
against a world-readable path does not need elevation.

---

## Error Surface

| Surface | Trigger | Behaviour |
|---------|---------|-----------|
| `MacosAclQueryError` | `ls -le` exits non-zero on a read (`get_acl` / `list_aces`). | Raised with the failed argv, exit code, and stderr captured on the exception. |
| `MacosAclActionResult(status=FAILED)` | `chmod` exits non-zero on any write step. | Returned (NOT raised) with `exit_code`, `stdout`, `stderr`, `command` populated.  Subsequent ACE writes within a fan-out are NOT dispatched. |
| Source-read failure inside `copy_acl` | `ls -le <src>` exits non-zero. | Returned as a `FAILED` result populated from the read step's runner state. |
| Idempotency-probe failures | `ls -le` exits non-zero inside a write verb. | The probe is silently treated as "no current state known"; the write proceeds.  The write's failure surface reports the underlying condition cleanly. |

This split mirrors the rest of the package: read errors raise
typed exceptions, write errors return typed results.

---

## Configuration

`MacosAclManagerConfig` extends `FeatureConfig` with two
PATH-overridable binary names:

| Field | Default | Purpose |
|-------|---------|---------|
| `ls_path` | `"ls"` | PATH-resolved BSD binary; tests can inject a wrapper. |
| `chmod_path` | `"chmod"` | PATH-resolved BSD binary; tests can inject a wrapper. |
| `dry_run` (inherited) | `False` | When `True`, write verbs short-circuit. |
| `elevated` (inherited) | `False` | When `True`, write steps run elevated. |

There is no other configuration: ACL state lives entirely on the
target paths, and both tools are stateless wrappers on the kernel
ACL syscalls.

---

## Aspect Composition

`MacosAclManager` is safe under any aspect composition on the
runner because:

1. The manager never spawns a subprocess directly — every command
   flows through `self._runner.run(...)`.
2. The manager re-queries ACL state per write rather than caching;
   retry-aspect re-dispatches do not see stale state.
3. The manager's only state is the injected runner and config; per-
   call state lives in local variables.
4. Persistence aspects on the runner cannot mutate
   `MacosAclActionResult` shape; the manager always builds the
   result from the captured `RunResult`.

---

## Implementation Walkthrough

This section walks each verb end-to-end, showing the exact dispatch
sequence and the data flow.

### `get_acl(path)`

```
caller -> manager.get_acl(path)
    -> runner.run(["ls", "-lde", path], elevated=False)
        -> RunResult(exit_code=0, stdout=<ls -le payload>, stderr="")
    -> _parse_ls_le_output(output, path)
        -> MacosAclInfo(path, owner, group, aces, default_aces=[])
    -> caller receives MacosAclInfo
```

Failure path: if `RunResult.exit_code != 0`, the manager raises
`MacosAclQueryError(command, exit_code, stderr)`.

### `list_aces(path)`

```
caller -> manager.list_aces(path)
    -> manager.get_acl(path) (same as above)
    -> flat list[MacosAceInfo] = info.aces (default_aces always [])
    -> caller receives list[MacosAceInfo]
```

### `set_acl(path, acl_spec)`

```
caller -> manager.set_acl(path, acl_spec)
    -> render every ACE in acl_spec into chmod tokens

    -- Dry-run gate --
    if config.dry_run:
        return MacosAclActionResult(status=DRY_RUN, command=<preview>)

    -- Idempotency probe --
    current = manager.get_acl(path)  # may raise; we swallow
    if _acl_matches_spec(current, acl_spec):
        return SKIPPED

    -- Clear step --
    runner.run(["chmod", "-N", path], elevated=config.elevated)
    if exit_code != 0:
        return FAILED

    -- Per-ACE write fan-out --
    for token in rendered_aces:
        runner.run(["chmod", "+a", token, path], elevated=config.elevated)
        if exit_code != 0:
            return FAILED  # short-circuit
    return last SUCCESS
```

### `add_ace(path, principal, rights=None)`

```
caller -> manager.add_ace(path, MacosAceSpec(...), rights=None)
    -> effective = _materialise_ace_spec(principal, rights)
    -> rendered = _render_chmod_ace_token(spec=effective)
    -> command = ["chmod", "+a", rendered, path]

    -- Dry-run gate --
    if config.dry_run:
        return DRY_RUN

    -- Idempotency probe --
    current = manager.get_acl(path)
    if _ace_already_present(current, effective):
        return SKIPPED

    -- Dispatch --
    return manager._dispatch(command=command, elevated=config.elevated)
```

### `remove_ace(path, principal)`

```
caller -> manager.remove_ace(path, MacosAceSpec(...))
    -- Dry-run gate --
    if config.dry_run:
        return DRY_RUN

    -- Probe --
    current = manager.get_acl(path)
    matches = _find_matching_aces(current, kind, name)
    if len(matches) == 0:
        return SKIPPED

    -- Per-match removal fan-out --
    for ace in matches:
        rendered = _render_chmod_ace_token(spec=ace_as_spec)
        runner.run(["chmod", "-a", rendered, path], elevated=...)
        if exit_code != 0:
            return FAILED
    return last SUCCESS
```

### `clear_acl(path)`

```
caller -> manager.clear_acl(path)
    -> command = ["chmod", "-N", path]

    -- Dry-run gate --
    if config.dry_run:
        return DRY_RUN

    -- Idempotency probe --
    current = manager.get_acl(path)
    if len(current.aces) == 0:
        return SKIPPED

    -- Dispatch --
    return manager._dispatch(command=command, elevated=config.elevated)
```

### `copy_acl(src, dst)`

```
caller -> manager.copy_acl(src, dst)
    -- Dry-run gate --
    if config.dry_run:
        return DRY_RUN

    -- Source read --
    src_info = manager.get_acl(src)  # raises -> FAILED return

    -- Idempotency probe --
    dst_info = manager.get_acl(dst)
    if _acl_infos_equal(src_info, dst_info):
        return SKIPPED

    -- Clear + per-ACE write fan-out --
    runner.run(["chmod", "-N", dst], elevated=...)
    if exit_code != 0:
        return FAILED
    for ace in src_info.aces:
        rendered = _render_chmod_ace_token(spec=ace_as_spec)
        runner.run(["chmod", "+a", rendered, dst], elevated=...)
        if exit_code != 0:
            return FAILED
    return last SUCCESS
```

---

## Spec-vs-Contract Drift

The cross-OS contract uses the names `AclSet` (for the per-path
snapshot), `AclEntry` (for an ACE), and `AclPrincipal` (for a
principal record carrying `kind` / `name` / `sid`).  This macOS spec
exposes:

* `MacosAclInfo` instead of `MacosAclSet` — `Info` aligns with the
  rest of this package's read-shape types.
* `MacosAceInfo` and `MacosAceSpec` instead of `MacosAclEntry` — the
  `Info` / `Spec` split distinguishes parsed read shapes from
  caller-supplied write shapes.
* `MacosAcePrincipalKind` (just `USER` and `GROUP`) instead of an
  `MacosAclPrincipal` dataclass — macOS principals resolve through
  Directory Services and have no SID concept; the principal name is
  carried directly on `MacosAceInfo` / `MacosAceSpec`.
* `MacosAcePermission` (`ALLOW` / `DENY`) — the contract folds
  permission polarity into a single `allow: bool` boolean on
  `AclEntry`; this spec exposes the equivalent as a typed enum for
  symmetry with the other macOS sysman enums.
* `inheritable: bool` on `MacosAceInfo` / `MacosAceSpec` — the
  contract pushes inheritance into a generic `is_default` /
  `inherited` pair; this spec exposes the macOS-native flag set
  collapsed into a single boolean (with the verbatim tokens
  retained on `rights` for round-trip fidelity).
* The `recursive: bool` parameter on every write verb in the
  contract is intentionally NOT exposed on this OS; macOS `chmod`
  carries an `-R` flag, but recursive ACL walks demand a richer
  reporting shape than the per-path verbs provide and will be
  delivered by a follow-up `3-acl-management-recursive` spec.

Cross-OS callers that need the contract's names will see them in
the `automojo-interfaces` adapter layer when that layer
materialises; the per-OS managers favour names that align with
the rest of their package.

---

## Performance Notes

`ls -le` typically completes in under 5 ms; `chmod -N` and `chmod
+a` similarly cheap.  The per-ACE fan-out in `set_acl` and
`copy_acl` means N ACEs becomes N runner calls — for a typical
host-management workload (single-digit ACEs per path), this is
well under 100 ms total.  No caching is performed across calls;
the per-call cost is too low to warrant cache-invalidation
complexity.

For callers that need to batch the same ACL across many paths, the
recommended pattern is to render the ACL once and call `set_acl`
per path.  A future `apply_acl_template` verb may collapse those
into a single multi-target invocation behind a richer reporting
shape.

---

## Tests

The test module `test_aclmanagement.py` covers:

* Parsing `ls -le` output including allow / deny ACEs.
* `set_acl` dispatching the canonical clear-then-fan-out sequence.
* `set_acl` `SKIPPED` when the parsed ACL already matches.
* `set_acl` `DRY_RUN` short-circuiting the runner.
* `add_ace` dispatching the rendered `chmod +a` token.
* `add_ace` `SKIPPED` when the ACE is already present.
* `remove_ace` dispatching `chmod -a` per matching ACE.
* `remove_ace` `SKIPPED` when no ACE matches the principal.
* `clear_acl` dispatching `chmod -N` and skipping when empty.
* `copy_acl` reading source and dispatching clear + per-ACE writes.
* `copy_acl` skipping when source and destination match.
* `list_aces` returning the full flat list.
* Writes elevating, reads never elevating.
* Failed commands surfacing stderr on the result.

Total: 14+ tests, all using a `_MockCommandRunner` that records
every call and returns pre-seeded `RunResult` values keyed by argv
tuple.  No live `ls` / `chmod` is invoked.

---

## Backwards Compatibility

This is a new feature; there are no backwards-compatibility
constraints.  The cross-OS contract pins the public verb names; we
follow them.  The OS-prefixed type names are pinned by this spec;
future additions go through new specs.
