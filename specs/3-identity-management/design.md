# Design

## Overview

The identity management feature is implemented as a single module,
`automojo.sysman.macos.identitymanagement`, that exposes a
`MacosIdentityManager` class.  This spec implements the cross-OS
identity-management contract defined at
`automojo-sysman-common/specs/contracts/identity-management/contract.md`
for macOS using `dsconfigad` (AD bind / unbind / status), `dscl`
(Directory Services reads through the canonical `/Search` node),
`dscacheutil` (NSS cache flush), and `killall -HUP mDNSResponder`
(MDNS-side cache kick).

All read paths go through `dsconfigad -show` and `dscl /Search -read`
with the output parsed in pure Python.  All system interaction is
delegated to a command runner implementing the `ICommandRunner`
interface.  No direct file-system access or `subprocess` calls are
made inside the module.

Administrator passwords for `join_realm` and `leave_realm` land on
the runner-bound argv (because `dsconfigad` accepts them on argv) but
the recorded `command` field on the
`MacosIdentityActionResult` replaces every password value with the
literal `<redacted>` token.  This mirrors the redaction pattern
established by `MacosUserGroupManager.set_password`, where the same
sanitiser walks the argv linearly and replaces the value following
each password-bearing flag.

The feature works identically whether commands execute locally or
remotely over SSH.

**See Also:**
- `3-user-group-management` for local-account operations.
- `3-certificate-trust-management` for the enterprise-CA anchors
  required by LDAPS / Kerberos-over-TLS setups.
- `automojo-sysman-linux-redhat/specs/3-identity-management/` for
  the FreeIPA / SSSD / realmd reference implementation that
  motivated this cross-OS contract.
- `automojo-sysman-linux-ubuntu/specs/3-identity-management/` for
  the Ubuntu sibling implementation using `realm` + `sssctl`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosIdentityManager                 │
│  - Supplies MacosRealmJoinSpec with admin credentials       │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosIdentityManager                             │
│  automojo.sysman.macos.identitymanagement                   │
│                                                             │
│  Writes:                                                    │
│    join_realm()    leave_realm()    sync_users()            │
│  Reads:                                                     │
│    get_realm_status()    list_joined_realms()               │
│    resolve_user()        resolve_group()                    │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - sudo for elevation │      │ - reliability metrics│
└──────────────────────┘      └──────────────────────┘
```

---

## Components

### `MacosIdentityManager`

**Location:** `source/packages/automojo/sysman/macos/identitymanagement.py`

| Method | Signature | Description |
|---|---|---|
| `join_realm` | `(spec) → MacosIdentityActionResult` | `dsconfigad -add <domain> -username <admin> -password <pw> [-computer …] [-ou …] [-preferred …] [-force]` |
| `leave_realm` | `(admin_user, admin_password, realm=None) → MacosIdentityActionResult` | `dsconfigad -remove -username <admin> -password <pw> [-force]` |
| `get_realm_status` | `() → MacosRealmStatus` | `dsconfigad -show` parsed |
| `list_joined_realms` | `() → list[MacosRealmInfo]` | List of zero or one based on `dsconfigad -show` |
| `sync_users` | `() → MacosIdentityActionResult` | `dscacheutil -flushcache` then `killall -HUP mDNSResponder` |
| `resolve_user` | `(username) → MacosUserResolveResult` | `dscl /Search -read /Users/<u>` parsed |
| `resolve_group` | `(groupname) → MacosGroupResolveResult` | `dscl /Search -read /Groups/<g>` parsed |

### `MacosIdentityManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `dsconfigad_executable` | `str` | `"dsconfigad"` | Path / name of the `dsconfigad` binary |
| `dscl_executable` | `str` | `"dscl"` | Path / name of the `dscl` binary |
| `dscacheutil_executable` | `str` | `"dscacheutil"` | Path / name of the `dscacheutil` binary |
| `killall_executable` | `str` | `"killall"` | Path / name of the `killall` binary |
| `default_ou` | `str \| None` | `None` | Fallback OU when `MacosRealmJoinSpec.ou_path` is `None` |
| `force` | `bool` | `True` | When `True`, append `-force` to bind / unbind argv |

---

## Data Models

### Outcome / status

```python
class MacosIdentityActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class MacosIdentityActionResult(FeatureActionResult):
    realm: Optional[str] = None
    username: Optional[str] = None
    message: Optional[str] = None
```

`MacosIdentityActionStatus` is aliased to the shared
`featurebase.ActionStatus`; the per-feature alias preserves the
contract's prefixed-naming requirement without forking the enum.

### Realm models

```python
class MacosRealmKind(Enum):
    ACTIVE_DIRECTORY = "active_directory"
    OPEN_DIRECTORY   = "open_directory"
    UNKNOWN          = "unknown"


@dataclass
class MacosRealmJoinSpec:
    realm: str
    admin_user: str
    admin_password: str
    computer_name: Optional[str] = None
    ou_path: Optional[str] = None
    domain_controller: Optional[str] = None


@dataclass
class MacosRealmInfo:
    realm: str
    realm_kind: MacosRealmKind
    is_default: bool = False
    domain_controller: Optional[str] = None
    last_sync: Optional[str] = None


@dataclass
class MacosRealmStatus:
    joined: bool
    current_realm: Optional[str] = None
    realm_kind: Optional[MacosRealmKind] = None
    computer_name: Optional[str] = None
```

### Resolve models

```python
@dataclass
class MacosUserResolveResult:
    username: str
    uid: Optional[int] = None
    gid: Optional[int] = None
    home: Optional[str] = None
    shell: Optional[str] = None
    realm: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    found: bool = False


@dataclass
class MacosGroupResolveResult:
    groupname: str
    gid: Optional[int] = None
    members: list[str] = field(default_factory=list)
    realm: Optional[str] = None
    found: bool = False
```

### Exceptions

```python
class MacosIdentityQueryError(Exception): ...
```

`MacosIdentityQueryError` is raised by `get_realm_status` (and
transitively by `list_joined_realms` and `_is_realm_joined`) when
`dsconfigad -show` exits non-zero.  `resolve_user` / `resolve_group`
do NOT raise on `dscl` non-zero exit codes; they return a
`found=False` result instead, so callers can branch on existence
without try/except scaffolding.

---

## Realm-Kind Mapping

The cross-OS contract defines four `RealmKind` values:
`ACTIVE_DIRECTORY`, `FREEIPA`, `OPEN_DIRECTORY`, `KERBEROS_ONLY`.
macOS supports only `ACTIVE_DIRECTORY` (via `dsconfigad`) and
`OPEN_DIRECTORY` (via `dscl /Search` augmentation):

| Cross-OS `RealmKind` | macOS support | Notes |
|---|---|---|
| `ACTIVE_DIRECTORY` | Yes | `dsconfigad -add` / `-remove` |
| `OPEN_DIRECTORY` | Out of Wave 4.5 | Reserved enum value; `list_joined_realms` does not currently enumerate OD nodes |
| `FREEIPA` | Not supported | Per contract, `join_realm` returns FAILED when an `OS does not support` kind is requested; this implementation does not gate on kind because `MacosRealmJoinSpec` does not carry a kind field |
| `KERBEROS_ONLY` | Not supported | Same |

### AD vs Open Directory divergence

macOS's directory layer is intentionally heterogeneous:

- **Active Directory** binds via `dsconfigad` register a single
  computer-account on the domain controller and configure the host
  to fetch user / group records from the AD plug-in for Directory
  Services.  There can be exactly one AD bind at a time.
- **Open Directory** uses `dscl /Search` augmentation: the
  `/Search` node is a meta-node that aggregates the local node
  (`/Local/Default`) with arbitrary additional nodes (typically
  LDAP-backed Open Directory nodes).  There can be many.

This divergence drives the single-realm reality of
`list_joined_realms`: the AD bind is the only reasonable thing
to call a "realm" on macOS.  Open Directory enumeration is a
follow-up batch — its right shape is `dscl /Search -read /
SearchPolicy`, which surfaces a list of bound nodes that we would
materialise as `MacosRealmInfo` records with kind
`OPEN_DIRECTORY`.

### vs FreeIPA

macOS has no first-class FreeIPA bind path.  Hosts can be joined to
a FreeIPA realm only by running `realmd` / `sssd` outside the
shipping macOS toolchain, which is out of scope.  Coordinators that
need IPA-on-Mac should fall through to a Linux peer; the cross-OS
contract is satisfied for macOS by the AD path alone.

---

## Key Method Designs

### `join_realm(spec)`

1. Build the argv:
   `["dsconfigad", "-add", spec.realm,
     "-username", spec.admin_user,
     "-password", spec.admin_password,
     ... optional placement flags ...,
     "-force" if config.force]`.
2. Sanitise the argv (replace `-password` value with
   `<redacted>`); the recorded copy is used in the result and the
   plaintext copy is the one dispatched to the runner.
3. If `config.dry_run` → return `DRY_RUN` with the sanitised argv.
4. Probe `_is_realm_joined(realm=spec.realm)` via
   `dsconfigad -show`; when the bound realm matches → `SKIPPED`.
5. Dispatch the plaintext argv with `elevated=config.elevated`.
6. Map the runner result to `MacosIdentityActionResult` with
   `realm=spec.realm`, `username=spec.admin_user`, and the
   sanitised argv on `command`.

### `leave_realm(admin_user, admin_password, realm=None)`

1. Build the argv:
   `["dsconfigad", "-remove",
     "-username", admin_user,
     "-password", admin_password,
     "-force" if config.force]`.
2. Sanitise the argv.
3. If `config.dry_run` → return `DRY_RUN`.
4. Probe `get_realm_status()`; when not bound → `SKIPPED`.  When a
   non-`None` `realm` argument is supplied and the bound realm
   differs (case-insensitive) → `SKIPPED` (we do not unbind from a
   different realm than the caller asked for).
5. Dispatch and map.

### `sync_users()`

1. Build two argvs:
   - `["dscacheutil", "-flushcache"]`
   - `["killall", "-HUP", "mDNSResponder"]`
2. If `config.dry_run` → return `DRY_RUN`.
3. Run the flush; on non-zero exit return `FAILED` immediately
   without touching `mDNSResponder`.
4. Run the kick; map its exit to the final `status`.  The recorded
   `command` is the flush argv (the kick is auxiliary).

### `get_realm_status()` / `list_joined_realms()`

1. `_dsconfigad_show()` runs `dsconfigad -show` (no elevation),
   parses `Key = Value` lines, and returns a flat dict.  Non-zero
   exit raises `MacosIdentityQueryError`.
2. `_materialise_realm_status` inspects the `Active Directory
   Domain` (preferred) or `Active Directory Forest` field — when
   either is non-empty and not `-`, the host is bound; the kind is
   `ACTIVE_DIRECTORY`; the `computer_name` is the parsed
   `Computer Account` line (or `None` when dashed).
3. `list_joined_realms` calls `get_realm_status` and returns a
   single-element list when `joined=True`, otherwise an empty list.

### `resolve_user(username)` / `resolve_group(groupname)`

1. Build the argv:
   `["dscl", "/Search", "-read", "/Users/<username>"]` (or
   `"/Groups/<groupname>"`).
2. Dispatch without elevation.
3. On non-zero exit → return `found=False`.
4. Parse the multi-line `Attribute: value` output via
   `_parse_dscl_read_output` (continuation lines fold into the
   previous attribute with single-space joins).
5. Materialise the result: `UniqueID` → uid, `PrimaryGroupID` →
   gid, `NFSHomeDirectory` → home, `UserShell` → shell,
   `GroupMembership` → members.  Detect the realm by inspecting
   the `OriginalNodeName` line: when it starts with
   `/Active Directory/<realm>/` → the realm name.

---

## Dry-Run Behaviour

- `dry_run=True` disables: `join_realm`, `leave_realm`, `sync_users`.
- `dry_run=True` allows: `get_realm_status`, `list_joined_realms`,
  `resolve_user`, `resolve_group`.

When `dry_run=True`, the manager returns `DRY_RUN` without
dispatching even the read-side `dsconfigad -show` probe normally
performed by `_is_realm_joined`.  This prevents stalls on
unreachable directory servers.

---

## Correctness Properties

1. **Password sanitisation in recorded argv.** Every `-password
   <pw>` pair in the recorded `command` field is replaced with
   `-password <redacted>` — verified by a dedicated test.
2. **Idempotent join / leave.** `join_realm` returns `SKIPPED` when
   already bound to the requested realm; `leave_realm` returns
   `SKIPPED` when not bound or bound to a different realm.
3. **Dry-run blocks remote contact.** When `config.dry_run=True`,
   no `dsconfigad` / `dscacheutil` / `killall` command is
   dispatched.
4. **Reads do not elevate.** `get_realm_status`,
   `list_joined_realms`, `resolve_user`, and `resolve_group`
   always dispatch with `elevated=False`, even when
   `config.elevated=True`.
5. **Query-error surface is typed.** `MacosIdentityQueryError`
   carries the failing argv, exit code, and stderr.

---

## Testing Strategy

- `_MockCommandRunner` recording every `run` call and returning
  pre-seeded `RunResult` values keyed by argv tuple.
- Canonical `dsconfigad -show` output samples for bound and
  unbound states.
- Canonical `dscl read` output samples for AD-backed and local
  user / group records.
- Assertions for:
  - The exact argv shape for each verb.
  - `elevated` flag propagation through writes.
  - Non-elevation of reads.
  - Recorded argv carrying `<redacted>` in `-password` slots.
  - Idempotency-skip paths for `join_realm` (already bound) and
    `leave_realm` (not bound).
  - Dry-run paths returning `DRY_RUN` without invoking the runner.
  - `get_realm_status` / `list_joined_realms` parsing fidelity.
  - `resolve_user` / `resolve_group` parsing for AD-backed and
    local records, plus the `found=False` path.
  - `MacosIdentityQueryError` propagation on `dsconfigad -show`
    failure.
  - Captured `exit_code` and `stderr` on a synthetic failure.

No live AD / Open Directory service required.

---

## Module Layout

```
source/packages/automojo/sysman/macos/
└── identitymanagement.py

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_identitymanagement.py
```

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| Local-account creation | `3-user-group-management` | Out of scope; `resolve_user` only reads |
| Enterprise CA trust for LDAPS | `3-certificate-trust-management` | Coordinator installs the anchor first, then this feature binds |
| MDM policy / configuration profiles | Out of scope (cross-OS contract) | This feature only handles `dsconfigad`-driven bind state |
| `opendirectoryd` lifecycle | `3-service-management` (when applicable) | Out of scope; coordinator restarts the service if needed |

---

## Cross-OS Identity-Model Concept Mapping

The cross-OS contract is intentionally lossy.  This table maps the
abstract concepts to the concrete macOS surfaces:

| Cross-OS concept | macOS surface |
|---|---|
| Realm join | `dsconfigad -add <domain> -username <admin> -password <pw>` |
| Realm leave | `dsconfigad -remove -username <admin> -password <pw>` |
| Realm membership probe | `dsconfigad -show` (single-realm reality) |
| Directory cache refresh | `dscacheutil -flushcache` + `killall -HUP mDNSResponder` |
| User principal resolution | `dscl /Search -read /Users/<user>` |
| Group principal resolution | `dscl /Search -read /Groups/<group>` |
| Computer / machine account | `dsconfigad -show` `Computer Account` line |
| Default realm | The single bound AD domain, if any |

Notice the divergence from the FreeIPA / SSSD model:

- macOS's bind is single-realm (one AD domain at a time).
  FreeIPA / SSSD on Linux supports arbitrary multi-realm
  configurations.
- macOS uses `dsconfigad` for AD bind, which writes Directory
  Services plists rather than `/etc/sssd/sssd.conf`.
- macOS's NSS is fronted by `opendirectoryd`, not `nss-sssd`.  The
  cache flush therefore goes through `dscacheutil` plus an MDNS
  kick, rather than `sssctl cache-expire`.

These differences are why the cross-OS contract abstracts the
operations behind verbs rather than tools — every OS realises the
same workflow with different primitives.

---

## Deviations From the Cross-OS Contract

| Contract surface | macOS | Reason |
|---|---|---|
| `discover_realm(domain) → RealmDiscovery` | Not exposed | Out of Wave 4.5 scope; `dsconfigad` has no first-class discovery verb |
| `IdentityCredentialError` | Not raised | `MacosRealmJoinSpec.admin_password` is a required field, so the missing-credential case is structurally impossible; `leave_realm` accepts the credential pair as required positional / keyword arguments |
| `IdentityRealmNotFoundError` | Not raised | `leave_realm` reports `SKIPPED` when the host is not bound or is bound to a different realm; `resolve_*` returns `found=False` on missing principals |
| `RealmKind` enum on join spec | Not modelled | `MacosRealmJoinSpec` does not carry a kind because `dsconfigad` only supports AD; an unsupported-kind path is unreachable |
| `extra_options` on join spec | Not modelled | `dsconfigad` extras (e.g. `-mobile`, `-mobileconfirm`) belong in a follow-up batch |
| Open Directory enumeration in `list_joined_realms` | Returns single-element list only | OD enumeration via `dscl /Search -read / SearchPolicy` is a follow-up batch |

These deviations are intentional and tracked in `tasks.md`.  They
do not break cross-OS contract conformance because the contract's
required verb set — `join_realm`, `leave_realm`,
`get_realm_status`, `list_joined_realms`, `sync_users`,
`resolve_user`, `resolve_group` — is fully implemented.
