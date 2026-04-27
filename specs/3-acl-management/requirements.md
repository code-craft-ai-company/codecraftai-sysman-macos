# Requirements

## Introduction

The ACL management feature provides the `automojo-sysman-macos`
package with the ability to query and mutate macOS extended Access
Control Lists (ACLs) on individual filesystem paths.  This spec
implements the ACL-management contract defined at
`automojo-sysman-common/specs/contracts/acl-management/contract.md`
for macOS using the BSD ``chmod`` extended-ACL surface (`chmod +a`,
`chmod -a`, `chmod -N`) for writes and `ls -le` for reads — the
canonical NFSv4-style ACL toolchain shipped with macOS.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public verbs.  The feature
does not decide which ACL a path should carry — it provides the
capability to apply, observe, and idempotently re-apply ACLs reliably
and with full auditability.  Every system command is executed
exclusively through the `automojo-interfaces` command-runner
abstraction; this module does not import `subprocess`, does not call
`open`, and does not consume an `IFileOperator`.  Persistence is
owned by `chmod` itself (the kernel-level NFSv4-style ACL stored
on the inode for HFS+ or the per-file extended-attribute set for
APFS); the manager relies on `chmod` to write the on-disk state.

The OS-prefixed types in this module are `MacosAclManager`,
`MacosAclManagerConfig`, `MacosAclActionStatus` (alias of the
package-shared `ActionStatus`), `MacosAclActionResult`,
`MacosAceInfo`, `MacosAceSpec`, `MacosAclInfo`,
`MacosAcePrincipalKind`, `MacosAcePermission`, and
`MacosAclQueryError`.

This feature complements — and does not duplicate — the basic-
permission surface owned by `3-filesystem-management` (`chmod` mode
bits, `chown`).  macOS extended ACLs are conceptually a superset of
the classic owner / group / other model and orthogonal to the BSD
file flags surfaced by `chflags`; this module operates exclusively on
the NFSv4-style ACE list, leaving mode bits and file flags to their
respective sibling features.

---

## Requirements

### Requirement 1: Query the Full ACL of a Path

**User Story:** As a maintenance coordinator, I want to read the
current macOS extended ACL of a file or directory, so that
compliance checks and diffs are possible.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_acl(path)` THE SYSTEM SHALL
   invoke `ls -lde <path>` through the command runner with
   `elevated=False` and parse the output into a `MacosAclInfo`.
2. THE SYSTEM SHALL populate `MacosAclInfo.owner` and
   `MacosAclInfo.group` from the third and fourth whitespace-
   delimited fields of the ``ls -l`` long-form line.
3. THE SYSTEM SHALL populate `MacosAclInfo.aces` with every
   subsequent ACE line, in source order, as `MacosAceInfo` records,
   each carrying the ``index``, ``principal_kind``, ``principal``,
   ``permission``, ``rights``, and derived ``inheritable`` boolean.
4. THE SYSTEM SHALL populate `MacosAclInfo.default_aces` with an
   empty list — macOS has no default-ACL partition.
5. WHEN `ls -lde` exits non-zero THE SYSTEM SHALL raise
   `MacosAclQueryError` carrying the failed argv, exit code, and
   captured stderr.
6. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 2: Replace the Full ACL on a Path

**User Story:** As a maintenance coordinator, I want to replace the
entire extended ACL on a path with a declared specification, so that
access aligns deterministically with the desired posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_acl(path, acl_spec)` THE SYSTEM
   SHALL first invoke `chmod -N <path>` to clear all extended
   entries, then invoke `chmod +a "<rendered>" <path>` per ACE in
   `acl_spec`.
2. WHEN the parsed current ACL is tuple-equal to `acl_spec` THE
   SYSTEM SHALL return `status=SKIPPED` without invoking either
   `chmod` step.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner at all and SHALL populate the
   would-have-been argv on `command`.
4. WHEN the runner reports a non-zero exit on either step THE
   SYSTEM SHALL return `status=FAILED` with `exit_code`, `stdout`,
   `stderr`, and `command` populated.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for every `chmod` step.
6. WHEN a per-ACE write step fails THE SYSTEM SHALL short-circuit
   and return that step's `FAILED` result; subsequent ACEs SHALL
   NOT be dispatched.

---

### Requirement 3: Add a Single ACE

**User Story:** As a maintenance coordinator, I want to add a single
ACE for a named principal with a specific rights set, so that I can
grant access incrementally without reasserting the full ACL.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_ace(path, principal, rights)` THE
   SYSTEM SHALL invoke `chmod +a "<rendered>" <path>` where
   `<rendered>` is `<kind>:<name> <permission> <comma-joined
   rights>`.
2. WHEN the parsed ACL already contains an ACE with the same
   principal-kind / principal / permission / rights-set /
   inheritable-flag THE SYSTEM SHALL return `status=SKIPPED` without
   invoking `chmod`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED`.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner.
6. THE SYSTEM SHALL populate `result.principal` with the spec's
   `principal` value on every returned result.

---

### Requirement 4: Remove ACEs by Principal

**User Story:** As a maintenance coordinator, I want to remove every
ACE belonging to a named principal, so that access can be revoked
without enumerating each ACE individually.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_ace(path, principal)` THE
   SYSTEM SHALL parse the current ACL and identify every ACE whose
   `principal_kind` and `principal` match the spec.
2. THE SYSTEM SHALL dispatch one `chmod -a "<rendered>" <path>`
   per matching ACE, re-rendering the full ACE token from the
   parsed `MacosAceInfo` so that the ``chmod -a`` exact-match
   requirement is honoured.
3. WHEN the parsed ACL contains no matching ACE THE SYSTEM SHALL
   return `status=SKIPPED` without invoking `chmod`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN any per-match `chmod -a` step fails THE SYSTEM SHALL
   short-circuit and return that step's `FAILED` result.
6. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner.

---

### Requirement 5: Clear All ACEs

**User Story:** As a maintenance coordinator, I want to remove every
ACE from a path, so that access reverts to the classic mode-bit
model.

#### Acceptance Criteria

1. WHEN the coordinator calls `clear_acl(path)` THE SYSTEM SHALL
   invoke `chmod -N <path>`.
2. WHEN the parsed ACL has zero ACEs THE SYSTEM SHALL return
   `status=SKIPPED` without invoking `chmod`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED`.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner.
6. THE SYSTEM SHALL NOT remove the classic POSIX mode bits; those
   are owned by the basic-permission surface.

---

### Requirement 6: Copy ACL Between Paths

**User Story:** As a maintenance coordinator, I want to copy the ACL
from one path onto another, so that I can replicate access posture
between siblings without re-rendering the full spec.

#### Acceptance Criteria

1. WHEN the coordinator calls `copy_acl(src, dst)` THE SYSTEM SHALL
   first invoke `ls -lde <src>` (with `elevated=False`) and parse
   the output into a `MacosAclInfo`.
2. WHEN `ls -lde` on `src` exits non-zero THE SYSTEM SHALL return
   `status=FAILED` populated with the read step's runner state.
3. WHEN the parsed source and destination ACLs are tuple-equal THE
   SYSTEM SHALL return `status=SKIPPED`.
4. WHEN the copy proceeds THE SYSTEM SHALL invoke `chmod -N <dst>`
   to clear, then dispatch `chmod +a "<rendered>" <dst>` per
   parsed source ACE.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for every `chmod` step.  The
   `ls -lde` reads never elevate.

---

### Requirement 7: Enumerate ACEs for a Path

**User Story:** As a maintenance coordinator, I want to enumerate
every ACE attached to a single path, so that compliance reports can
summarise per-path posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_aces(path)` THE SYSTEM SHALL
   invoke `ls -lde <path>` and return a flat
   `list[MacosAceInfo]`.
2. THE SYSTEM SHALL emit ACEs in the source order reported by
   `ls -le` (their natural index ordering).
3. WHEN `ls -le` exits non-zero THE SYSTEM SHALL raise
   `MacosAclQueryError`.
4. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 8: Allow / Deny Polarity

**User Story:** As a maintenance coordinator, I want allow ACEs and
deny ACEs to be expressible, because macOS NFSv4-style ACLs support
both polarities and a deny ACE is the only way to express "this
principal can NOT do X regardless of group inheritance".

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `MacosAcePermission` with values
   `ALLOW` and `DENY`.
2. THE SYSTEM SHALL render `MacosAcePermission.ALLOW` as the literal
   `allow` token and `MacosAcePermission.DENY` as `deny` in every
   `chmod +a` / `chmod -a` argument.
3. THE SYSTEM SHALL parse `allow` and `deny` from `ls -le` output
   into the corresponding enum value.
4. THE SYSTEM SHALL treat permission as part of the idempotency
   tuple — an `allow` ACE and a `deny` ACE for the same principal
   and rights set are NOT considered identical.

---

### Requirement 9: Inheritance Support

**User Story:** As a maintenance coordinator, I want directory ACEs
to optionally carry inheritance flags, so that newly-created
children inherit declared access automatically.

#### Acceptance Criteria

1. WHEN a `MacosAceSpec` carries `inheritable=True` THE SYSTEM
   SHALL append `file_inherit` and `directory_inherit` to the
   rendered rights list when those tokens are not already present.
2. WHEN parsing an `ls -le` ACE row whose rights list contains any
   of `file_inherit`, `directory_inherit`, `limit_inherit`, or
   `only_inherit` THE SYSTEM SHALL set `MacosAceInfo.inheritable`
   to `True`.
3. THE SYSTEM SHALL retain the verbatim rights tokens on
   `MacosAceInfo.rights`; the `inheritable` boolean is a derived
   summary, not a replacement.
4. THE SYSTEM SHALL include `inheritable` in the idempotency tuple
   for `add_ace` and `set_acl` comparisons.

---

### Requirement 10: Rights-Token Vocabulary

**User Story:** As a maintenance coordinator, I want to express the
full macOS rights vocabulary, so that fine-grained NFSv4-style
permissions can be applied.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept `rights` as a `list[str]` of macOS-
   native rights tokens.  No translation is performed; each token
   is rendered verbatim into the comma-joined `chmod +a` argument.
2. THE SYSTEM SHALL accept any of the canonical macOS rights
   tokens documented by `chmod(1)`: `read`, `write`, `execute`,
   `delete`, `append`, `readattr`, `writeattr`, `readextattr`,
   `writeextattr`, `readsecurity`, `writesecurity`, `chown`,
   `list`, `search`, `add_file`, `add_subdirectory`,
   `delete_child`, `file_inherit`, `directory_inherit`,
   `limit_inherit`, `only_inherit`.
3. THE SYSTEM SHALL NOT validate the token vocabulary in this
   module; invalid tokens are caught by `chmod` itself and surface
   as a `FAILED` result with `chmod`'s native error message.

---

### Requirement 11: Elevation, Dry-Run, and Aspect Composition

**User Story:** As a maintenance coordinator, I want every mutating
verb to honour the manager's elevation policy and to be safe under
arbitrary aspect composition on the runner, so that the manager
behaves identically against a `LocalCommandRunner`,
`SSHCommandRunner`, or any wrapped variant.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner on every write step
   (`chmod -N`, `chmod +a`, `chmod -a`).
2. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL never pass `elevated=True`.
3. THE SYSTEM SHALL never pass `elevated=True` on any read
   (`get_acl`, `list_aces`, every idempotency probe, the source-
   side read of `copy_acl`).
4. WHEN `dry_run=True` THE SYSTEM SHALL never invoke the runner for
   a mutating verb.
5. THE SYSTEM SHALL hold no state across calls beyond the injected
   config and runner.
6. THE SYSTEM SHALL be safe under any retry / persistence /
   timeout aspect composition on the runner.

---

### Requirement 12: Error Reporting and No Direct Filesystem Access

**User Story:** As an operator, I want failed ACL writes to surface
the underlying tool's error output, and I want guarantees that no
direct filesystem access bypasses the runner-driven dispatch model.

#### Acceptance Criteria

1. WHEN any `chmod` invocation exits non-zero THE SYSTEM SHALL
   return `MacosAclActionResult` populated with `status=FAILED`,
   `exit_code`, `stdout`, `stderr`, and `command`.
2. WHEN `ls -le` exits non-zero on a read verb THE SYSTEM SHALL
   raise `MacosAclQueryError`.
3. THE SYSTEM SHALL NOT import `subprocess`, `os.system`, or any
   process-spawning module other than indirectly through the
   command runner.
4. THE SYSTEM SHALL NOT call `open()` to read or write any path.
5. THE SYSTEM SHALL NOT consume an `IFileOperator`.
