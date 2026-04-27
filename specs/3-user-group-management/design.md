# Design

## Overview

The user and group management feature is implemented as a single module,
`automojo.sysman.macos.usergroupmanagement`, that exposes a `MacosUserGroupManager`
class.  This spec implements the user-group-management contract defined at
`automojo-sysman-common/specs/contracts/user-group-management/contract.md` for
macOS using the native Directory Services tooling — `sysadminctl` for user lifecycle
and password reset, `dscl` for granular property updates and directory enumeration,
`dseditgroup` for group lifecycle and membership, and `pwpolicy` for lock-state
management — plus the POSIX identity utility `id` where a POSIX view is required.

All read paths used for idempotency guards go through `dscl . -read`,
`dscl . -list`, or `dseditgroup -o read`; output is parsed in pure Python into
`MacosUserInfo` / `MacosGroupInfo` records.  All system interaction is delegated to a
command runner implementing the `ICommandRunner` interface.  No direct file system
access or `subprocess` calls are made inside the module.

Passwords are included in the argv of `sysadminctl` (because the tool requires them
that way), but the manager stores a sanitised argv in `MacosUserGroupActionResult.command`
with the password tokens replaced by `"<redacted>"`.  Persistence aspects therefore
never record plaintext credentials.

The feature works identically whether commands execute locally (via `LocalCommandRunner`)
or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosUserGroupManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosUserGroupManager                            │
│  automojo.sysman.macos.usergroupmanagement                  │
│                                                             │
│  create_user()    delete_user()    modify_user()            │
│  get_user()       list_users()     set_password()           │
│  lock_account()   unlock_account()                          │
│  create_group()   delete_group()                            │
│  get_group()      list_groups()                             │
│  add_to_group()   remove_from_group()                       │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, stdin, capture_output) → RunResult     │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - sudo for elevation │      │ - reliability metrics│
│ - local execution    │      │ - connection retry   │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosUserGroupManager`

**Location:** `source/packages/automojo/sysman/macos/usergroupmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `create_user` | `(spec) → MacosUserGroupActionResult` | `sysadminctl -addUser` |
| `delete_user` | `(name, remove_home) → MacosUserGroupActionResult` | `sysadminctl -deleteUser [-secure/-keepHome]` |
| `modify_user` | `(name, changes) → MacosUserGroupActionResult` | One `dscl . -change` per changed property |
| `get_user` | `(name) → MacosUserInfo \| None` | `dscl . -read /Users/<name>` + `pwpolicy` |
| `list_users` | `() → list[MacosUserInfo]` | `dscl . -list /Users UniqueID` filtered by UID range |
| `set_password` | `(name, password) → MacosUserGroupActionResult` | `sysadminctl -resetPasswordFor` |
| `lock_account` | `(name) → MacosUserGroupActionResult` | `pwpolicy -u <name> -disableuser` |
| `unlock_account` | `(name) → MacosUserGroupActionResult` | `pwpolicy -u <name> -enableuser` |
| `create_group` | `(spec) → MacosUserGroupActionResult` | `dseditgroup -o create -n /Local/Default` |
| `delete_group` | `(name) → MacosUserGroupActionResult` | `dseditgroup -o delete -n /Local/Default` |
| `get_group` | `(name) → MacosGroupInfo \| None` | `dseditgroup -o read -n /Local/Default` |
| `list_groups` | `() → list[MacosGroupInfo]` | `dscl . -list /Groups PrimaryGroupID` |
| `add_to_group` | `(user, group) → MacosUserGroupActionResult` | `dseditgroup -o edit -a <user> -t user` |
| `remove_from_group` | `(user, group) → MacosUserGroupActionResult` | `dseditgroup -o edit -d <user> -t user` |

### `MacosUserGroupManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `uid_min` | `int` | `500` | Lower bound for "local" UID range in `list_users` |
| `admin_user` | `str \| None` | `None` | Admin account used as `-adminUser` for `sysadminctl` |
| `admin_password` | `str \| None` | `None` | Admin password used as `-adminPassword`; redacted in `command` |
| `ds_node` | `str` | `/Local/Default` | Directory Services node path |

---

## Data Models

### Result Types

```python
class MacosUserGroupActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class MacosUserGroupActionResult:
    status: MacosUserGroupActionStatus
    target: str                         # user or group name
    kind: str                           # "user" or "group"
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None           # sanitised — passwords are "<redacted>"
```

### Spec / Info Types

```python
@dataclass
class MacosUserSpec:
    name: str
    uid: int | None
    home: str | None
    shell: str | None
    primary_group: str | None           # group name; resolved to PrimaryGroupID at runtime
    comment: str | None                 # RealName
    system_account: bool                # documented-only — macOS uses UID < 500

@dataclass
class MacosUserModSpec:
    shell: str | None
    home: str | None
    move_home: bool
    primary_group: str | None
    comment: str | None
    uid: int | None

@dataclass
class MacosUserInfo:
    name: str
    uid: int
    gid: int
    comment: str
    home: str
    shell: str
    locked: bool

@dataclass
class MacosGroupSpec:
    name: str
    gid: int | None
    system_group: bool                  # documented-only — macOS uses GID < 500

@dataclass
class MacosGroupInfo:
    name: str
    gid: int
    members: list[str]                  # usernames, UUID members resolved
```

### Exceptions

```python
class MacosUserNotFoundError(Exception):
    """Raised when a user-targeting verb cannot locate the user in Directory Services."""

class MacosGroupNotFoundError(Exception):
    """Raised when a group-targeting verb cannot locate the group in Directory Services."""

class MacosUserGroupQueryError(Exception):
    """Raised when a dscl / dseditgroup / pwpolicy query fails unexpectedly."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        stdin: str | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

No `IFileOperator` is required: every read and write path flows through the native
macOS CLIs.

---

## User-vs-Group Mapping

macOS stores users and groups in the local Directory Services node
(`/Local/Default`, backed by `/var/db/dslocal/nodes/Default`).  UIDs and GIDs are
integers in the Directory Services `UniqueID` / `PrimaryGroupID` properties.  The
module does not read the `dslocal` plist files directly — it always goes through
`dscl` / `dseditgroup` / `sysadminctl` so any Directory Services middleware (Open
Directory, MDM, configuration profiles) participates without code changes.

The default "local" UID boundary for `list_users` is `500`; callers may override via
`uid_min`.  UIDs below the boundary are treated as macOS system accounts and excluded
by default, matching Apple's own guidance for user-facing tools.

Group membership may be expressed in two forms on macOS:

- **Shortname**: a `GroupMembership` entry whose value is a literal username.
- **UUID**: a `GroupMembers` entry whose value is the target user's `GeneratedUID`.

The parser resolves UUID members back to usernames by issuing
`dscl . -search /Users GeneratedUID <uuid>` through the runner so downstream code only
sees usernames in `MacosGroupInfo.members`.

---

## Command Mappings

### User Write Commands

| Operation | Command |
|---|---|
| create_user | `sysadminctl -addUser <name> [-fullName <comment>] [-UID <uid>] [-shell <shell>] [-home <home>] [-password <pw>] [-adminUser <admin> -adminPassword <pw>]` |
| delete_user (secure) | `sysadminctl -deleteUser <name> -secure [-adminUser <admin> -adminPassword <pw>]` |
| delete_user (keep home) | `sysadminctl -deleteUser <name> -keepHome [-adminUser <admin> -adminPassword <pw>]` |
| modify_user | one or more `dscl . -change /Users/<name> <Property> <oldValue> <newValue>` invocations |
| set_password | `sysadminctl -resetPasswordFor <name> -newPassword <pw> -adminUser <admin> -adminPassword <adminpw>` |
| lock_account | `pwpolicy -u <name> -disableuser` |
| unlock_account | `pwpolicy -u <name> -enableuser` |

`dscl` properties written by `modify_user`:

| `MacosUserModSpec` field | `dscl` property |
|---|---|
| `shell` | `UserShell` |
| `home` | `NFSHomeDirectory` |
| `primary_group` (group name) | `PrimaryGroupID` (integer looked up via `dseditgroup -o read`) |
| `comment` | `RealName` |
| `uid` | `UniqueID` |

When `move_home=True`, the manager additionally dispatches `ditto <old_home> <new_home>`
and `rm -rf <old_home>` in sequence through the runner.

### Group Write Commands

| Operation | Command |
|---|---|
| create_group | `dseditgroup -o create -n /Local/Default [-i <gid>] <name>` |
| delete_group | `dseditgroup -o delete -n /Local/Default <name>` |
| add_to_group | `dseditgroup -o edit -a <user> -t user -n /Local/Default <group>` |
| remove_from_group | `dseditgroup -o edit -d <user> -t user -n /Local/Default <group>` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| get_user | `dscl . -read /Users/<name>` + `pwpolicy -u <name> -getpolicy` |
| list_users | `dscl . -list /Users UniqueID` |
| get_group | `dseditgroup -o read -n /Local/Default <name>` |
| list_groups | `dscl . -list /Groups PrimaryGroupID` |
| UUID → name | `dscl . -search /Users GeneratedUID <uuid>` |

All write commands pass `elevated=True` when the config enables it.  Read commands never
elevate regardless of configuration.

---

## Output Parsing

### `dscl . -read /Users/<name>`

Example output:

```
RecordName: alice
RealName:
 Alice Example
NFSHomeDirectory: /Users/alice
UserShell: /bin/zsh
UniqueID: 501
PrimaryGroupID: 20
GeneratedUID: C8A8D8C6-2F2A-4F54-A5F8-0123456789AB
```

Parser rules:

- Keys are `^[A-Za-z]+:` lines.  Trailing whitespace is stripped.
- Multi-line values are indented by one space; the parser concatenates them.
- `RealName` → `comment`, `NFSHomeDirectory` → `home`, `UserShell` → `shell`,
  `UniqueID` → `uid`, `PrimaryGroupID` → `gid`, `RecordName` → `name`.
- `locked` is filled in by parsing `pwpolicy -u <name> -getpolicy` — see below.

### `pwpolicy -u <name> -getpolicy`

Example output:

```
isDisabled=0 newPasswordRequired=0 canModifyPasswordforSelf=1 ...
```

The parser finds the `isDisabled=<0|1>` token — `1` ⇒ `locked=True`, `0` ⇒
`locked=False`.  If the token is absent, the parser defaults to `locked=False`.

### `dseditgroup -o read -n /Local/Default <group>`

Example output:

```
dsAttrTypeStandard:RecordName -
 staff
dsAttrTypeStandard:PrimaryGroupID -
 20
dsAttrTypeStandard:GroupMembership -
 alice bob
dsAttrTypeStandard:GroupMembers -
 C8A8D8C6-2F2A-4F54-A5F8-0123456789AB
```

Parser rules:

- Each `dsAttrTypeStandard:<Name> -` line starts a block; values are the lines indented
  by a single space below it, whitespace-split.
- `RecordName` → `name`, `PrimaryGroupID` → `gid`.
- `GroupMembership` values are appended to `members` as-is.
- `GroupMembers` values are resolved via `dscl . -search /Users GeneratedUID <uuid>` and
  the resolved `RecordName` is appended to `members`.  If resolution fails, the raw
  UUID is appended verbatim.

### `dscl . -list /Users UniqueID` / `dscl . -list /Groups PrimaryGroupID`

Example output:

```
_amavisd           83
_analyticsd        263
alice              501
bob                502
```

Parser rule: split each line on whitespace; the first token is the name, the last
token is the integer UID / GID.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `create_user` | user already exists | `get_user(name) is not None` |
| `delete_user` | user does not exist | `get_user(name) is None` |
| `modify_user` | every non-`None` field already matches | compare `MacosUserInfo` |
| `set_password` | no idempotency guard (always runs when user exists) | — |
| `lock_account` | already locked | `get_user(name).locked is True` |
| `unlock_account` | already unlocked | `get_user(name).locked is False` |
| `create_group` | group already exists | `get_group(name) is not None` |
| `delete_group` | group does not exist | `get_group(name) is None` |
| `add_to_group` | user already in `members` | `user in get_group(group).members` |
| `remove_from_group` | user not in `members` | `user not in get_group(group).members` |

Preconditions raised before any idempotency check:

- `modify_user`, `set_password`, `lock_account`, `unlock_account`, `add_to_group`,
  `remove_from_group` raise `MacosUserNotFoundError` when `get_user(name)` returns
  `None`.
- `add_to_group`, `remove_from_group` raise `MacosGroupNotFoundError` when
  `get_group(group)` returns `None`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `MacosUserNotFoundError` | A user-targeting write is called with a non-existent user |
| `MacosGroupNotFoundError` | A group-targeting write is called with a non-existent group |
| `MacosUserGroupQueryError` | `dscl`, `dseditgroup`, or `pwpolicy` exits with an unexpected non-zero code (not the "record not found" pattern) |

All other failures are surfaced as `MacosUserGroupActionResult(status=FAILED, ...)`
carrying the runner's exit code, stdout, and stderr.

### Password Confidentiality

`sysadminctl` accepts passwords only via argv, so the manager:

1. Assembles the full argv with the plaintext password(s) for dispatch to the runner.
2. Constructs a *sanitised copy* of the argv with the plaintext password tokens
   replaced by the literal string `"<redacted>"`.
3. Stores the sanitised argv in `MacosUserGroupActionResult.command`.

Tests assert that none of `command`, `stdout`, `stderr` contains the plaintext
password.  Persistence aspects therefore only ever record the sanitised argv.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: create_user Command Construction

*For any* `MacosUserSpec`, `create_user()` should dispatch `sysadminctl -addUser <name>` augmented with `-fullName`, `-UID`, `-shell`, `-home`, `-password`, `-adminUser`, `-adminPassword` flags reflecting the non-`None` spec and config fields.

**Validates: Requirements 1.1**

### Property 2: create_user System Account Detail

*For any* `MacosUserSpec` with `system_account=True`, `create_user()` should not alter the argv but should record a detail noting that macOS uses UID < 500 as the system marker.

**Validates: Requirements 1.2**

### Property 3: create_user Idempotency

*For any* user already present in Directory Services, `create_user()` should return `SKIPPED` without invoking `sysadminctl`.

**Validates: Requirements 1.3**

### Property 4: delete_user Secure Flag

*For any* `delete_user(name, remove_home=True)` call, the argv should include `-secure`; for `remove_home=False`, it should include `-keepHome`.

**Validates: Requirements 2.1, 2.2**

### Property 5: delete_user Idempotency

*For any* user not present in Directory Services, `delete_user()` should return `SKIPPED`.

**Validates: Requirements 2.3**

### Property 6: modify_user Property-Granular Dispatch

*For any* `MacosUserModSpec`, `modify_user()` should issue exactly one `dscl . -change` invocation per non-`None` field (plus the `ditto`+`rm` pair when `move_home=True`).

**Validates: Requirements 3.1, 3.7**

### Property 7: modify_user Precondition

*For any* non-existent user, `modify_user()` should raise `MacosUserNotFoundError` before any command dispatch.

**Validates: Requirements 3.2**

### Property 8: modify_user Idempotency

*For any* `MacosUserModSpec` whose non-`None` fields all match the current `MacosUserInfo`, `modify_user()` should return `SKIPPED`.

**Validates: Requirements 3.3**

### Property 9: modify_user Short-Circuits on First Failure

*For any* `MacosUserModSpec` whose first `dscl . -change` invocation fails, `modify_user()` should not dispatch subsequent changes and should report `FAILED` with a `detail` naming the failing property.

**Validates: Requirements 3.5**

### Property 10: get_user Parsing

*For any* `dscl . -read /Users/<name>` block, `get_user()` should return a `MacosUserInfo` with `name`, `uid`, `gid`, `comment`, `home`, and `shell` populated and `locked` derived from `pwpolicy`.

**Validates: Requirements 4.1, 4.2**

### Property 11: get_user Missing Returns None

*For any* name that yields `eDSRecordNotFound` on `dscl`, `get_user()` should return `None`.

**Validates: Requirements 4.3**

### Property 12: list_users Range Filter

*For any* `dscl . -list /Users UniqueID` output, `list_users()` should return only entries with UID `>= uid_min`.

**Validates: Requirements 5.2**

### Property 13: set_password Argv Sanitisation

*For any* `set_password(name, password)` call, the argv dispatched to the runner MUST contain the plaintext but `MacosUserGroupActionResult.command` MUST have the plaintext replaced by `"<redacted>"`.

**Validates: Requirements 6.1, 6.2**

### Property 14: set_password Precondition

*For any* non-existent user, `set_password()` should raise `MacosUserNotFoundError` before any command dispatch.

**Validates: Requirements 6.3**

### Property 15: lock_account Idempotency

*For any* user whose `MacosUserInfo.locked` is `True`, `lock_account()` should return `SKIPPED`.

**Validates: Requirements 7.2**

### Property 16: unlock_account Idempotency

*For any* user whose `MacosUserInfo.locked` is `False`, `unlock_account()` should return `SKIPPED`.

**Validates: Requirements 8.2**

### Property 17: create_group Command Construction

*For any* `MacosGroupSpec`, `create_group()` should dispatch `dseditgroup -o create -n /Local/Default [-i <gid>] <name>`.

**Validates: Requirements 9.1**

### Property 18: create_group Idempotency

*For any* group already present in Directory Services, `create_group()` should return `SKIPPED`.

**Validates: Requirements 9.2**

### Property 19: delete_group Idempotency

*For any* group not present, `delete_group()` should return `SKIPPED`.

**Validates: Requirements 10.2**

### Property 20: get_group Parsing With UUID Resolution

*For any* `dseditgroup -o read` output, `get_group()` should return a `MacosGroupInfo` whose `members` list contains usernames — with UUID entries resolved to their `RecordName` via `dscl . -search /Users GeneratedUID`.

**Validates: Requirements 11.1, 11.2**

### Property 21: list_groups Parsing

*For any* successful `dscl . -list /Groups PrimaryGroupID` output, `list_groups()` should materialise every group through `get_group`.

**Validates: Requirements 12.1, 12.2**

### Property 22: add_to_group Command Construction

*For any* existing user and group, `add_to_group(user, group)` should dispatch `dseditgroup -o edit -a <user> -t user -n /Local/Default <group>`.

**Validates: Requirements 13.1**

### Property 23: add_to_group Preconditions

*For any* non-existent user, `add_to_group()` should raise `MacosUserNotFoundError`; for any non-existent group, it should raise `MacosGroupNotFoundError`.

**Validates: Requirements 13.2, 13.3**

### Property 24: add_to_group Membership Idempotency

*For any* user already listed in `MacosGroupInfo.members`, `add_to_group()` should return `SKIPPED`.

**Validates: Requirements 13.4**

### Property 25: remove_from_group Command Construction

*For any* existing user and group with the user as a member, `remove_from_group()` should dispatch `dseditgroup -o edit -d <user> -t user -n /Local/Default <group>`.

**Validates: Requirements 14.1**

### Property 26: remove_from_group Idempotency

*For any* user not listed in `MacosGroupInfo.members`, `remove_from_group()` should return `SKIPPED`.

**Validates: Requirements 14.4**

### Property 27: Dry-Run Safety

*For any* write operation with `dry_run=True`, the runner MUST NOT be invoked and the result status MUST be `DRY_RUN`.

**Validates: Requirements 1.6, 2.6, 3.6, 6.5, 7.6, 8.6, 9.5, 10.5, 13.7, 14.7**

### Property 28: Elevation for Writes

*For any* write with `elevated=True` in the config, the runner MUST be invoked with `elevated=True`.

**Validates: Requirements 15.1**

### Property 29: No Elevation for Reads

*For any* read (`get_user`, `list_users`, `get_group`, `list_groups`), the runner MUST NEVER be invoked with `elevated=True`.

**Validates: Requirements 15.2**

### Property 30: Error Result Consistency

*For any* non-zero exit from a write command, the returned `MacosUserGroupActionResult` should have `status=FAILED`, and carry the runner's exit code and stderr.

**Validates: Requirements 1.5, 2.5, 3.5, 6.4, 7.5, 8.5, 9.4, 10.4, 13.6, 14.6**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── usergroupmanagement.py        ← MacosUserGroupManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_usergroupmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.macos.usergroupmanagement import (
    MacosUserGroupManager,
    MacosUserGroupManagerConfig,
    MacosUserSpec,
    MacosGroupSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())

config = MacosUserGroupManagerConfig(
    elevated=True,
    dry_run=False,
    admin_user="admin",
    admin_password="from-vault",
)
mgr = MacosUserGroupManager(runner=runner, config=config)

user_spec = MacosUserSpec(
    name="svc-build",
    uid=1500,
    home="/Users/svc-build",
    shell="/usr/bin/false",
    primary_group="staff",
    comment="Build Service",
    system_account=False,
)
result = mgr.create_user(spec=user_spec)
print(f"create_user: {result.status}")

group_spec = MacosGroupSpec(name="build-ops", gid=None, system_group=False)
mgr.create_group(spec=group_spec)

mgr.add_to_group(user="svc-build", group="build-ops")

mgr.set_password(name="svc-build", password="rotated-by-vault")

mgr.lock_account(name="svc-build")

user_info = mgr.get_user(name="svc-build")
print(f"locked: {user_info.locked}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.macos.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.macos.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from automojo.sysman.macos.storage import SQLiteStorage
from automojo.sysman.macos.usergroupmanagement import (
    MacosUserGroupManager, MacosUserGroupManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-mac", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=30.0)))

mgr = MacosUserGroupManager(
    runner=runner,
    config=MacosUserGroupManagerConfig(
        elevated=True, admin_user="admin", admin_password="from-vault",
    ),
)

for user in mgr.list_users():
    print(f"{user.name}\t{user.uid}\t{user.locked}")
```

### Dry-Run Mode

```python
config = MacosUserGroupManagerConfig(elevated=True, dry_run=True,
                                     admin_user="admin", admin_password="x")
mgr = MacosUserGroupManager(runner=runner, config=config)

result = mgr.set_password(name="alice", password="NewSecret!")
assert result.status == MacosUserGroupActionStatus.DRY_RUN
assert "NewSecret!" not in (result.command or [])
assert "<redacted>" in (result.command or [])
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on the argv prefix.
- `dscl . -read /Users/<name>` mock responses drive every user-level idempotency guard.
- `dseditgroup -o read -n /Local/Default <name>` mock responses drive every group-level
  idempotency guard and supply the `members` list for `add_to_group` /
  `remove_from_group` guards.
- `pwpolicy -u <name> -getpolicy` mock responses drive lock-state detection.
- A dedicated test verifies that `set_password` sends the plaintext to the runner but
  the returned `MacosUserGroupActionResult.command` has the plaintext replaced with
  `"<redacted>"`.
- Precondition tests assert `MacosUserNotFoundError` / `MacosGroupNotFoundError`
  raise before any runner call.
- Dry-run tests prove that every write short-circuits to `DRY_RUN` without invoking
  the runner.
- Elevation tests prove writes receive `elevated=True` when configured and reads
  never do.
- Parser tests cover `dscl . -read`, `dscl . -list`, `dseditgroup -o read`, and
  `pwpolicy -u -getpolicy` outputs, including the UUID-member resolution path.
- No test requires root access or a live Directory Services node.

---

## Scope Mapping

| Contract verb | macOS implementation |
|---|---|
| `create_user` | `sysadminctl -addUser` |
| `delete_user` | `sysadminctl -deleteUser` |
| `modify_user` | per-property `dscl . -change` |
| `get_user` | `dscl . -read` + `pwpolicy -u -getpolicy` |
| `list_users` | `dscl . -list /Users UniqueID` (filtered) |
| `set_password` | `sysadminctl -resetPasswordFor` |
| `lock_account` | `pwpolicy -u -disableuser` |
| `unlock_account` | `pwpolicy -u -enableuser` |
| `create_group` | `dseditgroup -o create` |
| `delete_group` | `dseditgroup -o delete` |
| `get_group` | `dseditgroup -o read` |
| `list_groups` | `dscl . -list /Groups PrimaryGroupID` |
| `add_to_group` | `dseditgroup -o edit -a` |
| `remove_from_group` | `dseditgroup -o edit -d` |

---

## Extensions Beyond the Contract

None in this macOS implementation.  Extensions such as admin / standard role
assignment, FileVault enrolment, and mobile-account migration are intentionally
deferred; when added they will live behind their own feature flags so a coordinator
writing against the contract does not have to special-case macOS.
