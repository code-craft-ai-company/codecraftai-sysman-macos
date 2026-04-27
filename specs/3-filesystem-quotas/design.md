# Design

## Overview

The filesystem-quotas feature is implemented as a single
module, `automojo.sysman.macos.filesystemquotas`, exposing a
`MacosFilesystemQuotasManager` class that wraps the BSD
`quota` family — `quotacheck`, `quotaon`, `quotaoff`,
`setquota`, `quota`, `repquota` — through the `ICommandRunner`
abstraction.  It also issues a `mount` probe per call so the
manager can decide whether the target filesystem is quota-aware
(HFS+ / UFS) or APFS (no quota subsystem).

This module exists primarily to give the cross-OS contract a
working macOS surface even though the underlying capability is
deeply degraded: APFS — the default macOS filesystem since 10.13
High Sierra — has no per-user / per-group quota subsystem.  The
manager surfaces this gap as a typed FAILED result with
`detail="apfs_no_quota_support"` rather than allowing a kernel
error to bubble through.  Reads against APFS return empty
results because "no quotas defined" is a truthful statement
about an APFS volume.

The feature follows the standard `ActionStatus` / `Config` /
`ActionResult` / `_dispatch` pattern shared with every other
macOS feature.

| Verb | Kind | Returns |
|------|------|---------|
| `enable_quotas` | Write | `MacosQuotaActionResult` |
| `disable_quotas` | Write | `MacosQuotaActionResult` |
| `set_user_quota` | Write | `MacosQuotaActionResult` |
| `set_group_quota` | Write | `MacosQuotaActionResult` |
| `get_user_quota` | Read | `MacosQuotaInfo \| None` |
| `get_group_quota` | Read | `MacosQuotaInfo \| None` |
| `list_quotas` | Read | `list[MacosQuotaInfo]` |
| `get_quota_status` | Read | `MacosQuotaStatus` |

**See Also:**
- `automojo-sysman-common/specs/contracts/filesystem-quotas/contract.md`
  — cross-OS contract this implementation satisfies.
- `automojo-sysman-linux-redhat/specs/3-filesystem-quotas/`
  — canonical reference; covers the full quota surface
  including XFS / project quotas / grace-period verbs.
- `automojo-sysman-linux-ubuntu/specs/3-filesystem-quotas/`
  — closest sibling pilot; shares the BSD `quota` tooling
  shape modulo the APFS gap.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
│  - Declares quota limits per user / per group               │
│  - Toggles enforcement; reads current posture               │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│          MacosFilesystemQuotasManager                       │
│                                                             │
│  Filesystem detection:                                      │
│    _detect_fstype(mountpoint) →                             │
│        parses "mount" output for the fstype token           │
│                                                             │
│  APFS gate:                                                 │
│    _is_quota_aware(fstype) →                                │
│        in config.quota_aware_filesystems list               │
│    Writes against APFS short-circuit FAILED                 │
│    Reads against APFS return empty / disabled status        │
│                                                             │
│  Enforcement state (writes, HFS+ only):                     │
│    enable_quotas (quotacheck + quotaon)                     │
│    disable_quotas (quotaoff)                                │
│                                                             │
│  Limits (writes, HFS+ only):                                │
│    set_user_quota (setquota -u)                             │
│    set_group_quota (setquota -g)                            │
│                                                             │
│  Queries (reads, no elevation):                             │
│    get_user_quota (quota -u)                                │
│    get_group_quota (quota -g)                               │
│    list_quotas (repquota -ug)                               │
│    get_quota_status (quotaon -p + mount fstype)             │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Configuration

```python
@dataclass
class MacosFilesystemQuotasManagerConfig(FeatureConfig):
    """
        Inherits ``dry_run`` and ``elevated`` from FeatureConfig.

        :param quota_aware_filesystems: List of filesystem types
            that the manager treats as quota-aware.  Defaults to
            ``["hfs", "ufs"]``.  ``apfs`` is intentionally absent
            because APFS has no quota subsystem.  Operators
            running on a custom kernel module may extend this
            list as needed.
    """
    quota_aware_filesystems: list[str] = None  # init via field(default_factory=...)
```

`quota_aware_filesystems` defaults to `["hfs", "ufs"]` via
`field(default_factory=lambda: ["hfs", "ufs"])`.

---

## Data Models

```python
class MacosQuotaActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class MacosQuotaActionResult(FeatureActionResult):
    """
        Adds quota-scope fields on top of FeatureActionResult.

        :param mountpoint: The mountpoint acted upon (or None).
        :param principal: The user or group acted upon (or None).
    """
    mountpoint: str | None = None
    principal: str | None = None


class MacosQuotaPrincipalKind(Enum):
    USER = "user"
    GROUP = "group"


@dataclass
class MacosQuotaInfo:
    mountpoint: str
    principal: str
    principal_kind: MacosQuotaPrincipalKind
    used_bytes: int | None
    soft_bytes: int | None
    hard_bytes: int | None
    used_inodes: int | None
    soft_inodes: int | None
    hard_inodes: int | None
    grace_period_seconds: int | None


@dataclass
class MacosQuotaStatus:
    mountpoint: str
    enabled: bool
    enforcing: bool
    user_quotas_active: bool
    group_quotas_active: bool
    filesystem_type: str | None
```

### Exceptions

```python
class MacosQuotaQueryError(Exception):
    """Raised when a read fails unexpectedly."""


class MacosQuotaFilesystemNotMountedError(Exception):
    """
        Raised by writes when the supplied mountpoint is not
        currently mounted (per ``mount`` parsing).
    """
```

---

## Quota-Model Concept Mapping (Cross-OS)

The cross-OS contract speaks in `block_*` and `inode_*` units
expressed as bytes.  macOS's BSD quota tooling speaks in 1-KiB
blocks (same as Ubuntu).  The manager normalises to bytes.

| Concept | Cross-OS contract | macOS native | Notes |
|---------|-------------------|--------------|-------|
| Soft block limit | `block_soft` (bytes) | `setquota` arg in 1-KiB blocks | Multiply by 1024 on read; divide by 1024 on write. |
| Hard block limit | `block_hard` (bytes) | `setquota` arg in 1-KiB blocks | Same as above. |
| Block usage | `block_used` (bytes) | `quota -u` reports KiB | Multiply by 1024 on read. |
| Inode soft / hard | `inode_*` (count) | `setquota` arg in counts | 0 = no inode enforcement. |
| Grace period | `grace_*` (str) | `quota -u` reports seconds | Mapped to `grace_period_seconds`. |
| Filesystem type | `fstype` | `mount` output token | `"hfs"`, `"apfs"`, `"ufs"`, ... |

### APFS gap (load-bearing design choice)

Modern macOS (10.13+) uses APFS by default.  APFS removed the
classic per-user / per-group quota subsystem entirely.  Apple's
guidance is to use per-volume container management
(`diskutil apfs` reserve / quota properties), which is a
fundamentally different surface — per-volume, not
per-principal — and therefore does NOT satisfy the cross-OS
contract's per-user / per-group semantics.

Rather than exposing a degraded APFS-flavoured surface that
silently does nothing, the macOS pilot:

1. Detects the mountpoint's filesystem via the `mount` output
   parser.
2. On writes against APFS: returns `FAILED` with
   `detail="apfs_no_quota_support"` and a fully-populated
   `MacosQuotaActionResult.command` field carrying the
   would-have-been argv.  This makes the gap auditable: every
   APFS-targeted write call leaves a clean failure trail
   without dispatching a command that would fail mysteriously.
3. On reads against APFS: returns the empty / disabled
   variant of the read type.  `get_user_quota` /
   `get_group_quota` return `None`.  `list_quotas` returns
   `[]`.  `get_quota_status` returns
   `MacosQuotaStatus(enabled=False, enforcing=False,
   user_quotas_active=False, group_quotas_active=False,
   filesystem_type="apfs")`.  This matches the truth: APFS has
   no quota records, so reporting "none" is correct.

The list of quota-aware filesystem types is configurable so
that operators running on a custom kernel module (or a
hypothetical future Apple filesystem with a re-introduced
quota surface) can extend the surface without code changes.

---

## Key Method Designs

### `_detect_fstype(mountpoint)`

1. Dispatch `["mount"]` without elevation.
2. Parse each line: `<device> on <mountpoint> (<fstype>,
   <options>)`.  Anchor on the LAST occurrence of `" on "` so
   that mountpoints containing the literal `" on "` parse
   correctly (this is a known macOS pitfall).
3. Return the matched `<fstype>` string, or raise
   :class:`MacosQuotaFilesystemNotMountedError` when no row
   matches.

### `_is_quota_aware(fstype)`

Returns True when `fstype` is in
`config.quota_aware_filesystems`.

### `enable_quotas(mountpoint)`

1. Dry-run guard.
2. `_detect_fstype(mountpoint)` — raises if not mounted.
3. If not `_is_quota_aware(fstype)` → return `FAILED` with
   `detail="apfs_no_quota_support"` (the detail token is fixed
   regardless of the actual fstype; APFS is overwhelmingly the
   non-supported case in practice).
4. Probe `get_quota_status(mountpoint)`. If both kinds active
   → `SKIPPED`.
5. Dispatch `["quotacheck", "-ug", mountpoint]`.
6. On failure return its result.
7. Dispatch `["quotaon", "-ug", mountpoint]`.

### `disable_quotas(mountpoint)`

1. Dry-run guard.
2. `_detect_fstype` mount validation.
3. APFS check → `FAILED detail="apfs_no_quota_support"`.
4. Probe state. If both off → `SKIPPED`.
5. Dispatch `["quotaoff", "-ug", mountpoint]`.

### `set_user_quota(mountpoint, user, soft_bytes, hard_bytes)`

1. Dry-run guard.
2. `_detect_fstype` mount validation.
3. APFS check → `FAILED detail="apfs_no_quota_support"`.
4. Probe `get_user_quota`; SKIPPED on match.
5. Dispatch `["setquota", "-u", user, str(soft_kb),
   str(hard_kb), "0", "0", mountpoint]`.

### `set_group_quota(...)`

Symmetric to `set_user_quota` with `-g`.

### `get_user_quota(mountpoint, user)` / `get_group_quota(...)`

1. `_detect_fstype` (best-effort; returns None on failure).
2. If APFS / unknown → return None.
3. Dispatch `["quota", "-u", user]`.
4. Exit code 0/1/2 → parse; anything else → raise
   :class:`MacosQuotaQueryError`.
5. Match the row whose `Filesystem` token is the mountpoint or
   ends with the mountpoint.

### `list_quotas(mountpoint)`

1. `_detect_fstype` (best-effort).
2. APFS / unknown → return [].
3. Dispatch `["repquota", "-ug", mountpoint]`.
4. Non-zero → raise :class:`MacosQuotaQueryError`.
5. Parse the two-section output.

### `get_quota_status(mountpoint)`

1. `_detect_fstype` (best-effort).
2. APFS → return the disabled status with
   `filesystem_type="apfs"`.
3. Dispatch `["quotaon", "-p", mountpoint]`.
4. Non-zero → raise :class:`MacosQuotaQueryError`.
5. Parse the user/group on/off lines.

---

## Parser Contracts

### `mount` parser

Sample output:

```
/dev/disk1s1 on / (apfs, sealed, local, read-only, ...)
/dev/disk2s1 on /Volumes/Backup (hfs, local, journaled)
```

Parse rules:
- Anchor on the LAST `" on "` (mountpoints can contain the
  literal text).
- The right-hand side `(<fstype>, ...)` is split on the first
  comma; the fstype is the leading lowercased token.

### `quota -u <user>` parser

Same shape as Ubuntu: the BSD `quota` binary on macOS produces
near-identical output to GNU `quota`.  Block columns are 1-KiB.
A trailing `*` on the used column indicates a soft-limit
breach; the parser strips it.

### `repquota -ug <mountpoint>` parser

Same shape as Ubuntu; the BSD `repquota` produces a two-section
report keyed on `*** Report for user/group quotas`.

### `quotaon -p <mountpoint>` parser

Same as Ubuntu: lines of the form
`user quota on <mountpoint> (<dev>) is on/off`.

---

## Dry-Run Behaviour

Dry-run short-circuits each write verb at the top of the method
body, before mount validation.  The `command` field on the
returned :class:`MacosQuotaActionResult` records the would-
have-been argv.

---

## Elevation Policy

| Verb | `elevated` |
|------|-----------|
| `enable_quotas` | `config.elevated` |
| `disable_quotas` | `config.elevated` |
| `set_user_quota` | `config.elevated` |
| `set_group_quota` | `config.elevated` |
| `get_user_quota` | `False` |
| `get_group_quota` | `False` |
| `list_quotas` | `False` |
| `get_quota_status` | `False` |
| `_detect_fstype` (mount probe) | `False` |

---

## Idempotency

| Verb | Probe | Skip condition |
|------|-------|----------------|
| `enable_quotas` | `get_quota_status` | both active |
| `disable_quotas` | `get_quota_status` | both off |
| `set_user_quota` | `get_user_quota` | soft and hard match |
| `set_group_quota` | `get_group_quota` | soft and hard match |

The probe runs after the dry-run guard but before the actual
mutation.  The APFS gate runs even before the probe so that an
APFS write returns FAILED without ever calling `quotaon -p`.

---

## Filesystem-Mapping Table

| (fstype, target) | Supported? | Notes |
|------------------|------------|-------|
| `(hfs, USER)` | Yes | Canonical legacy surface. |
| `(hfs, GROUP)` | Yes | Canonical legacy surface. |
| `(ufs, USER)` | Yes (legacy) | Same BSD tooling; rarely seen in production. |
| `(ufs, GROUP)` | Yes (legacy) | Same. |
| `(apfs, *)` | **No** | APFS has no quota subsystem.  Writes return FAILED with `detail="apfs_no_quota_support"`.  Reads return empty / disabled. |
| `(*, PROJECT)` | No | macOS has no project-quota analogue. |

---

## Correctness Properties

1. **APFS writes never dispatch.** Every write verb gates on
   `_is_quota_aware(fstype)` before invoking the underlying
   command.
2. **APFS reads never raise.** Every read verb returns the
   empty / disabled variant when the fstype is APFS (or when
   `_detect_fstype` could not resolve a fstype at all).
3. **Mount validation precedes mutation.** Every write verb
   calls `_detect_fstype(mountpoint)` before any quota command.
4. **Idempotent enable / disable.** Repeated invocations on a
   mountpoint already in the requested state return `SKIPPED`.
5. **Idempotent set.** Setting limits that already match is a
   `SKIPPED` no-op.
6. **No elevation on reads.** Every read verb passes
   `elevated=False`.
7. **Dry-run never dispatches.**

---

## Testing Strategy

- `_MockCommandRunner` recording every call and returning
  pre-seeded `RunResult` values keyed by argv tuple.
- Per-verb command-construction tests asserting the exact argv.
- Per-verb idempotency tests on HFS+.
- Per-verb dry-run tests asserting no runner call.
- Parser tests for `mount`, `quota`, `repquota`,
  `quotaon -p`.
- Unknown-principal test for `get_user_quota`.
- Elevation-on-writes test.
- No-elevation-on-reads test.
- Mount-validation test (`mount` row missing → raises
  `MacosQuotaFilesystemNotMountedError`).
- APFS write returns FAILED with
  `detail="apfs_no_quota_support"`.
- Failure-surface test (mutation non-zero → FAILED with
  stderr captured).

No live `quota` / `repquota` / `quotaon` is invoked.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|------|-------|--------------|
| Mount options | `3-filesystem-management` | Quota mount options are HFS+ legacy and largely manual on macOS; this feature assumes they are active when applicable. |
| Target accounts / groups | `3-user-group-management` | Caller ensures names are valid. |
| APFS volume size limits | macOS-native (out of scope) | `diskutil apfs` properties are not surfaced here. |

---

## Out of Scope (Documented Drift From Contract)

The macOS pilot intentionally narrows scope to:

- USER + GROUP only on quota-aware filesystems; PROJECT is
  unsupported (macOS has no project-quota analogue).
- No grace-period setter; the read path surfaces
  `grace_period_seconds` for reporting.
- `enable_quotas` / `disable_quotas` toggle BOTH user and
  group enforcement together.
- APFS writes return `FAILED detail="apfs_no_quota_support"`
  rather than raising, mirroring the contract's requirement
  that an unsupported (fstype, target) combination returns
  FAILED with descriptive detail.

These deviations are purposeful: they keep the macOS pilot
small and aligned with the Ubuntu / Windows peers.
