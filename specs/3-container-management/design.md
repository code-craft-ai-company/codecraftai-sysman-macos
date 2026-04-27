# Design

## Concept Mapping (macOS ↔ Ubuntu / RHEL ↔ Windows)

The cross-OS container-management contract talks about *images* and
*containers*.  macOS does not have a native Linux container runtime;
both Docker Desktop and colima ship a small Linux VM that runs the
actual `containerd` / `crun` daemon, and Podman on macOS likewise
runs a managed VM via `podman machine`.  The host-side CLI in every
case is whichever binary the user installed on PATH; this manager
only sees that binary.

| Contract concept | macOS (this spec) | Ubuntu | Windows |
|---|---|---|---|
| Engine | `docker` (Docker Desktop or colima) or `podman` (Podman Desktop / Homebrew) on PATH | `docker` (default) or `podman` | `docker.exe` (Docker Desktop) or `podman.exe` / `wsl podman` |
| Engine implementation | Linux VM hosted by Docker Desktop / colima / `podman machine` | Native Linux daemon (Docker `containerd` or rootless `podman` user-space stack) | Linux VM via WSL2 (Docker Desktop default) or Windows Containers (containerd backend) |
| Image storage | Inside the VM disk | `/var/lib/docker` or `~/.local/share/containers` | Inside the WSL2 distro |
| Volume mount syntax | POSIX paths (`-v /Users/x:/data`); the host portion is interpreted by macOS, the container portion by Linux | identical | Windows paths quoted with backslashes (`-v 'C:\\srv:/data'`) |
| Port mapping syntax | `<host>:<container>[/proto]` | identical | identical |
| Network selection | `--network <name>` | identical | identical |
| Restart policy | `--restart=<policy>` | identical | identical |
| Elevation | None — Docker Desktop / Podman Desktop run as the desktop user.  `elevated` flag exists only for rootful socket configurations. | `sudo` for rootful Docker; rootless Podman needs no sudo. | None — Docker Desktop runs as the desktop user. |

The macOS implementation is the closest sibling to Ubuntu — same CLI
verbs, same argv shape — and contrasts with Windows where the binary
suffix changes (`docker.exe`) and the Windows-path quoting rules
matter for volume mounts.

---

## Overview

The container-management feature is implemented as a single module,
`automojo.sysman.macos.containermanagement`, that exposes a
`MacosContainerManager` class.  The coordinator instantiates
`MacosContainerManager` and calls its methods to perform discrete
container engine actions: image pull / remove / list, container
create / start / stop / remove, container list / inspect, log fetch,
and exec.

This feature **complements — and does not duplicate** — package
installation of the engine (`package-management`), container engine
VM lifecycle (per-OS extension), or registry trust configuration
(per-OS extension).  Everything outside the `MacosContainerManager`
verb set lives in another feature.

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.  The `MacosContainerManager` never
directly calls `subprocess`, shell commands, or `open()`; it only
constructs argument lists and hands them to the injected runner.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Selects engine (DOCKER or PODMAN)                        │
│  - Injects runner into MacosContainerManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosContainerManager                            │
│  automojo.sysman.macos.containermanagement                  │
│                                                             │
│  pull_image()           remove_image()                      │
│  list_images()                                              │
│  create_container()     start_container()                   │
│  stop_container()       remove_container()                  │
│  list_containers()      inspect_container()                 │
│  get_container_logs()   exec_in_container()                 │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner
          ▼
┌─────────────────────────────────────────────────────────────┐
│               LocalCommandRunner / SSHCommandRunner         │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosContainerManager`

**Location:**
`source/packages/automojo/sysman/macos/containermanagement.py`

| Method | Signature | Description |
|---|---|---|
| `pull_image` | `(ref) → MacosContainerActionResult` | `<engine> pull <ref>`; idempotent via `<engine> images --filter` |
| `remove_image` | `(ref) → MacosContainerActionResult` | `<engine> rmi <ref>`; idempotent via `<engine> images --filter` |
| `list_images` | `() → list[MacosImageInfo]` | `<engine> images --format '{{json .}}'` |
| `create_container` | `(spec) → MacosContainerActionResult` | `<engine> create --name <name> [<flags...>] <image> [<argv...>]` |
| `start_container` | `(container_id) → MacosContainerActionResult` | `<engine> start <id>` |
| `stop_container` | `(container_id) → MacosContainerActionResult` | `<engine> stop -t <timeout> <id>` |
| `remove_container` | `(container_id) → MacosContainerActionResult` | `<engine> rm -f <id>` |
| `list_containers` | `(all=False) → list[MacosContainerInfo]` | `<engine> ps [-a] --format '{{json .}}'` |
| `inspect_container` | `(container_id) → MacosContainerInfo \| None` | `<engine> inspect <id>` (JSON array; element 0) |
| `get_container_logs` | `(container_id, lines=100) → MacosContainerLogs` | `<engine> logs --tail <lines> <id>` |
| `exec_in_container` | `(container_id, argv) → MacosContainerExecResult` | `<engine> exec <id> <argv...>` |

### `MacosContainerManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no command is dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `engine` | `MacosContainerEngine` | `DOCKER` | Engine to invoke |
| `force_pull` | `bool` | `False` | When `True`, `pull_image` always invokes `pull` |
| `default_stop_timeout` | `int` | `10` | Default `-t` value passed to `<engine> stop` |

---

## Data Models

### Result Types

```python
class MacosContainerActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

class MacosContainerEngine(Enum):
    DOCKER = "docker"
    PODMAN = "podman"

@dataclass
class MacosContainerActionResult:
    status: MacosContainerActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    engine: MacosContainerEngine | None
    container_id: str | None
    image: str | None
    target: str | None
    message: str | None

@dataclass
class MacosContainerExecResult:
    status: MacosContainerActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    engine: MacosContainerEngine | None
    container_id: str | None
    command_argv: list[str] | None
    output: str | None
```

### Image / Container Types

```python
@dataclass
class MacosImageInfo:
    id: str
    repository: str | None
    tag: str | None
    digest: str | None
    size_bytes: int | None
    created: str

@dataclass
class MacosContainerSpec:
    name: str
    image: str
    argv: list[str] | None = None
    env: dict[str, str] | None = None
    volumes: list[str] | None = None
    ports: list[str] | None = None
    networks: list[str] | None = None
    restart_policy: str | None = None
    working_dir: str | None = None
    user: str | None = None
    detach: bool = True

@dataclass
class MacosContainerInfo:
    id: str
    name: str
    image: str
    status: str
    state: str
    created: str
    started: str | None
    ports: list[str]
    mounts: list[str]
    command: list[str]
    engine: MacosContainerEngine | None

@dataclass
class MacosContainerLogs:
    container_id: str
    lines: list[str]
    truncated: bool
```

### Exceptions

```python
class MacosContainerQueryError(Exception):
    """Raised when an inspect / list query fails unexpectedly."""

class MacosContainerEngineNotFoundError(Exception):
    """Raised when the configured engine binary is not on PATH."""

class MacosContainerNotFoundError(Exception):
    """Reserved for cross-OS-contract parity."""
```

---

## Engine Mapping

| `MacosContainerEngine` | CLI binary | Notes |
|---|---|---|
| `DOCKER` | `docker` | Default.  Installed by Docker Desktop or colima.  Both expose the same `docker` binary on PATH; both target a Linux VM.  The active "context" (`docker context use ...`) determines which VM the CLI talks to. |
| `PODMAN` | `podman` | Podman Desktop or `brew install podman`.  Drives a Linux VM via `podman machine init` / `podman machine start`. |

The cross-OS contract additionally enumerates `DOCKER_DESKTOP` and
`COLIMA` as engine identifiers.  This implementation treats them as
"flavors of `DOCKER`" because the host-side CLI is identical and the
manager has no need to distinguish — both ship `docker` on PATH and
both accept the same argv.  A future enhancement could surface the
distinction by probing `docker context inspect` if a coordinator ever
needs it.

Engine binary resolution is purely PATH-based.  The manager passes
the binary name as argv[0] and lets the runner / OS resolve.  The
manager never probes the engine on construction; binary-missing
errors surface on the first verb call as a `FAILED` result with the
runner's stderr.

### Volume Mount Quoting on macOS

Volume mounts use POSIX paths (`-v /Users/me/srv:/data`).  The host
portion is a macOS path, the container portion is a Linux path; the
engine VM bridges them.  No special quoting is required from the
manager because the runner passes argv directly to the engine
binary — the shell never expands the `:` separator.  Spaces in the
host path are preserved by the runner's argv-pass-through.

---

## Verb / Command Mappings

### Write Commands (elevation honoured)

| Operation | Argv |
|---|---|
| `pull_image` | `<engine> pull <ref>` |
| `remove_image` | `<engine> rmi <ref>` |
| `create_container` | `<engine> create --name <name> [--restart=<policy>] [--user <u>] [--workdir <d>] [-e <kv> ...] [-v <vol> ...] [-p <port> ...] [--network <n> ...] <image> [<argv...>]` |
| `start_container` | `<engine> start <id>` |
| `stop_container` | `<engine> stop -t <timeout> <id>` |
| `remove_container` | `<engine> rm -f <id>` |
| `exec_in_container` | `<engine> exec <id> <argv...>` |

### Read Commands (no elevation)

| Operation | Argv |
|---|---|
| `list_images` | `<engine> images --format '{{json .}}'` |
| `list_containers` | `<engine> ps [-a] --format '{{json .}}'` |
| `inspect_container` | `<engine> inspect <id>` |
| `get_container_logs` | `<engine> logs --tail <lines> <id>` |
| `image_present` probe | `<engine> images --filter 'reference=<ref>' --format '{{json .}}'` |
| `inspect_raw` probe | `<engine> inspect <id>` |

### Argv Construction Order for `create_container`

Same as Ubuntu — the `_build_create_argv` helper enforces this
canonical order:

1. `<engine> create --name <name>`
2. `--restart=<policy>` (when set)
3. `--user <user>` (when set)
4. `--workdir <dir>` (when set)
5. `-e KEY=VALUE` (one per env entry)
6. `-v <volume_token>` (one per volume)
7. `-p <port_token>` (one per port)
8. `--network <name>` (one per network)
9. `<image>`
10. `<argv...>`

---

## Output Parsing

### `<engine> images --format '{{json .}}'`

```text
{"ID":"sha256:abc","Repository":"nginx","Tag":"1.25","Digest":"sha256:def","Size":12345678,"CreatedAt":"2025-01-01T00:00:00Z"}
{"ID":"sha256:xyz","Repository":"<none>","Tag":"<none>","Digest":"<none>","Size":99,"CreatedAt":"2025-01-02T00:00:00Z"}
```

* One JSON object per line; the parser splits on newlines and
  decodes each line independently.
* `<none>` repository / tag / digest values are coerced to `None`.
* `Size` is reported as int (Podman) or string (Docker) — both
  tolerated.

### `<engine> ps --format '{{json .}}'`

```text
{"ID":"abc","Names":["web"],"Image":"nginx:1.25","Status":"Up 2 hours","State":"running","Ports":"0.0.0.0:8080->80/tcp","Mounts":"","Command":"nginx -g daemon off;"}
```

* `Names` may be either a list (Podman) or a string (older Docker).
* `Ports` and `Mounts` may be either a list or a comma-separated
  string.
* `Command` may be either a list or a string.

### `<engine> inspect <id>`

```json
[
  {
    "Id": "abc",
    "Name": "/web",
    "Config": {"Image": "nginx:1.25", "Cmd": ["nginx"]},
    "State": {"Running": true, "Status": "running", "StartedAt": "2025-02-01T12:00:00Z"},
    "Created": "2025-02-01T11:55:00Z",
    "NetworkSettings": {"Ports": {"80/tcp": []}},
    "Mounts": [{"Destination": "/data"}]
  }
]
```

* The response is a JSON ARRAY; parser picks element 0.
* `Name` carries a leading `/`; the parser strips it.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `pull_image` | Reference already locally present | `<engine> images --filter 'reference=<ref>'` |
| `remove_image` | Reference not present | `<engine> images --filter 'reference=<ref>'` |
| `create_container` | Container of same name + image exists | `<engine> inspect <name>` |
| `start_container` | `State.Running == True` | `<engine> inspect <id>` |
| `stop_container` | `State.Running == False` (or absent) | `<engine> inspect <id>` |
| `remove_container` | Container absent | `<engine> inspect <id>` |
| `exec_in_container` | None — operational, always runs | n/a |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `MacosContainerQueryError` | Reserved for stricter read paths. |
| `MacosContainerEngineNotFoundError` | Reserved for engine-availability probe. |
| `MacosContainerNotFoundError` | Reserved for cross-OS-contract parity. |

All write failures are surfaced as
`MacosContainerActionResult(status=FAILED, ...)` carrying the
runner's exit code, stdout, and stderr.

---

## Correctness Properties

### Property 1: pull_image Argv

*For any* `pull_image(ref=R)` call where `R` is not present, the
runner sees `["<engine>", "pull", R]`.

**Validates: Requirements 1.1**

### Property 2: pull_image Idempotency

*For any* `pull_image(ref=R)` where `R` is present and
`force_pull=False`, the manager MUST return SKIPPED.

**Validates: Requirements 1.2**

### Property 3: remove_image Argv

*For any* `remove_image(ref=R)`, the runner sees
`["<engine>", "rmi", R]`.

**Validates: Requirements 2.1**

### Property 4: list_images Parses JSON Lines

*For any* engine output of N JSON objects (one per line),
`list_images()` MUST return N rows.

**Validates: Requirements 3.2**

### Property 5: create_container Argv Skeleton

*For any* `MacosContainerSpec(name=N, image=I)`,
`create_container()` MUST dispatch an argv that begins
`["<engine>", "create", "--name", N, ...]`.

**Validates: Requirements 4.1, 4.2**

### Property 6: create_container Idempotency

*For any* `create_container(spec)` where a container of `spec.name`
exists with `Config.Image == spec.image`, the manager MUST return
SKIPPED.

**Validates: Requirements 4.3**

### Property 7: start_container Idempotency

*For any* `start_container(id)` where `State.Running == True`, the
manager MUST return SKIPPED.

**Validates: Requirements 5.2**

### Property 8: stop_container Argv

*For any* `stop_container(id=I)`, the runner sees
`["<engine>", "stop", "-t", str(default_stop_timeout), I]`.

**Validates: Requirements 6.1**

### Property 9: stop_container Idempotency

*For any* `stop_container(id)` where `State.Running == False` or
absent, the manager MUST return SKIPPED.

**Validates: Requirements 6.2, 6.3**

### Property 10: remove_container Argv

*For any* `remove_container(id=I)`, the runner sees
`["<engine>", "rm", "-f", I]`.

**Validates: Requirements 7.1**

### Property 11: list_containers Includes Stopped When all=True

*For any* `list_containers(all=True)`, the argv contains `"-a"`.

**Validates: Requirements 8.1, 8.2**

### Property 12: inspect_container Returns Element Zero

*For any* successful inspect, the manager returns an info derived
from element 0.

**Validates: Requirements 9.2**

### Property 13: inspect_container Returns None on Absence

*For any* inspect with non-zero exit, the manager returns `None`.

**Validates: Requirements 9.4**

### Property 14: get_container_logs Sets truncated

*For any* `get_container_logs(id, lines)`, `truncated` MUST be True.

**Validates: Requirements 10.2**

### Property 15: exec_in_container Never Skips

*For any* `exec_in_container` outside dry-run, the manager MUST
dispatch `<engine> exec`.

**Validates: Requirements 11.3**

### Property 16: Engine Selection

*For any* config `engine=PODMAN`, every verb's argv[0] MUST equal
`"podman"`.

**Validates: Requirements 12.1, 12.2**

### Property 17: Dry-Run Safety

*For any* write verb invoked with `dry_run=True`, the runner MUST
NOT be invoked and the result status MUST be `DRY_RUN`.

**Validates: Requirements 1.6, 2.5, 4.6, 5.4, 6.5, 7.4, 11.4**

### Property 18: Elevation for Writes

*For any* write verb with `elevated=True` in the config, every
runner invocation MUST receive `elevated=True`.

**Validates: Requirements 12.3**

### Property 19: No Elevation for Reads

*For any* read verb (and any idempotency probe), the runner MUST
NEVER be invoked with `elevated=True`.

**Validates: Requirements 12.4**

### Property 20: Error Surface

*For any* non-zero exit from a write command, the result MUST have
`status=FAILED` and carry the runner's exit code and stderr.

**Validates: Requirements 1.5, 2.4, 4.5**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
├── featurebase.py
├── containermanagement.py    ← MacosContainerManager + types
└── storagemanagement.py

source/testroots/testbase/automojo/tests/sysman/macos/
└── test_containermanagement.py
```

---

## Usage Examples

### Local Execution (Default — Docker Desktop)

```python
from automojo.sysman.macos.runners import (
    LocalCommandRunner,
    LocalRunnerConfig,
)
from automojo.sysman.macos.containermanagement import (
    MacosContainerEngine,
    MacosContainerManager,
    MacosContainerManagerConfig,
    MacosContainerSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = MacosContainerManagerConfig(
    engine=MacosContainerEngine.DOCKER,
)
containers = MacosContainerManager(runner=runner, config=config)

# Pull, create, start
containers.pull_image(ref="nginx:1.25")
spec = MacosContainerSpec(
    name="web",
    image="nginx:1.25",
    ports=["8080:80"],
    volumes=["/Users/me/srv:/data"],
)
containers.create_container(spec=spec)
containers.start_container(container_id="web")

info = containers.inspect_container(container_id="web")
print(info.state)
```

### Switching to Podman Desktop

```python
config = MacosContainerManagerConfig(
    engine=MacosContainerEngine.PODMAN,
)
containers = MacosContainerManager(runner=runner, config=config)
# Same verbs; argv[0] becomes "podman".  Note that podman machine
# must already be running; this manager does not start the VM.
```

### Dry-Run Mode

```python
config = MacosContainerManagerConfig(dry_run=True)
containers = MacosContainerManager(runner=runner, config=config)
result = containers.remove_container(container_id="web")
assert result.status == MacosContainerActionStatus.DRY_RUN
print(result.command)   # ["docker", "rm", "-f", "web"]
```

### SSH Execution with Aspects

```python
runner = SSHCommandRunner(SSHRunnerConfig(host="mac-ci-1"))
runner.add_aspect(aspect=PersistenceAspect(...))
runner.add_aspect(aspect=RetryAspect(...))
containers = MacosContainerManager(
    runner=runner,
    config=MacosContainerManagerConfig(),
)
for c in containers.list_containers(all=True):
    print(c.id, c.name, c.state)
```

---

## Testing Strategy

* All tests use a `MockCommandRunner` keyed on argv tuples.
* Argv-construction tests assert the exact argv shape for every
  write verb.
* Idempotency tests cover SKIPPED paths (image present / absent,
  container present / absent / running / stopped).
* Dry-run tests prove the runner is not invoked.
* Engine-selection tests prove `PODMAN` flips argv[0] for every
  verb.
* Elevation tests cover write/read elevation policy.
* Parser tests cover `images`, `ps`, and `inspect` JSON shapes.
* Failure surface is tested by seeding non-zero exits.
* No test requires a live engine or a running VM.

---

## Scope Mapping

| Contract verb | macOS implementation |
|---|---|
| `pull_image` | `<engine> pull <ref>` |
| `remove_image` | `<engine> rmi <ref>` |
| `list_images` | `<engine> images --format '{{json .}}'` |
| `create_container` | `<engine> create --name <name> [<flags...>] <image> [<argv...>]` |
| `start_container` | `<engine> start <id>` |
| `stop_container` | `<engine> stop -t <timeout> <id>` |
| `remove_container` | `<engine> rm -f <id>` |
| `list_containers` | `<engine> ps [-a] --format '{{json .}}'` |
| `inspect_container` | `<engine> inspect <id>` |
| `get_container_logs` | `<engine> logs --tail <lines> <id>` |
| `exec_in_container` | `<engine> exec <id> <argv...>` |

---

## Extensions Beyond the Contract

None in this macOS implementation.  Image builds, Compose
orchestration, registry auth (`docker login`), Docker Desktop / colima
/ podman-machine VM lifecycle, and Kubernetes integration are
intentionally deferred.

---

## Future Considerations

* Surface the `DOCKER_DESKTOP` / `COLIMA` engine identifiers from the
  cross-OS contract so coordinators can tell which Docker flavor is
  active (probe via `docker context inspect`).
* Add a strict-read mode that raises `MacosContainerQueryError`.
* Add an engine-availability probe so a missing binary raises
  `MacosContainerEngineNotFoundError` instead of returning `FAILED`.
* Add a Docker Desktop / colima / podman-machine VM-status verb for
  coordinators that want to ensure the VM is running before any
  container verb.
* Add a registry-login verb that streams the password on stdin.
