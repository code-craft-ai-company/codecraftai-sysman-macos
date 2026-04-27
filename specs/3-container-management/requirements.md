# Requirements

## Introduction

The container management feature provides the
`automojo-sysman-macos` package with the ability to pull / remove
images, create / start / stop / remove containers, list and inspect
containers, retrieve container logs, and execute commands inside
running containers on macOS systems.  This spec implements the
container-management contract defined at
`automojo-sysman-common/specs/contracts/container-management/contract.md`
for macOS using either the `docker` CLI (default — provided by Docker
Desktop or colima) or the `podman` CLI (Podman Desktop / Homebrew),
selected at construction time via
`MacosContainerManagerConfig.engine`.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* containers should exist, *which*
images should be pulled, or *what* environment / volume / port
configuration a container should have — it provides the capability to
*manipulate container engine state* reliably, idempotently, and with
full auditability.  All container engine commands are executed
exclusively through the `automojo-interfaces` command-runner
abstraction.

macOS does not have a native Linux container runtime; both Docker
Desktop and Podman ship a small Linux VM that runs the actual daemon.
This feature only sees the host-side CLI — VM lifecycle (Docker
Desktop start / colima start / podman machine init) is NOT in scope
and is a per-OS extension.  Container engine installation (`brew
install docker` / `brew install --cask docker` / `brew install
podman`) is also NOT in scope; that lives in `package-management`.

---

## Requirements

### Requirement 1: Pull an Image

**User Story:** As a maintenance coordinator, I want to pull a
container image by reference, so that a specific image version is
present locally before any container creation.

#### Acceptance Criteria

1. WHEN the coordinator calls `pull_image(ref)` THE SYSTEM SHALL
   dispatch `<engine> pull <ref>` through the command runner with
   `elevated=True` when configured, where `<engine>` is `docker` or
   `podman` per `MacosContainerManagerConfig.engine`.
2. WHEN the reference is already present locally (probed via
   `<engine> images --filter 'reference=<ref>' --format '{{json .}}'`)
   THE SYSTEM SHALL skip execution and return
   `MacosContainerActionResult(status=SKIPPED)` UNLESS
   `config.force_pull` is True.
3. WHEN `config.force_pull` is True THE SYSTEM SHALL always invoke
   `<engine> pull` without consulting the idempotency probe.
4. WHEN the pull succeeds THE SYSTEM SHALL return
   `MacosContainerActionResult(status=SUCCESS)` populated with
   `target=<ref>`, `image=<ref>`, and `engine=<engine>`.
5. WHEN the pull fails THE SYSTEM SHALL return
   `MacosContainerActionResult(status=FAILED)` carrying the runner's
   exit code, stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosContainerActionResult(status=DRY_RUN)`.

---

### Requirement 2: Remove an Image

**User Story:** As a maintenance coordinator, I want to remove an
image by reference.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_image(ref)` THE SYSTEM SHALL
   dispatch `<engine> rmi <ref>` through the command runner with
   `elevated=True` when configured.
2. WHEN the reference is absent locally THE SYSTEM SHALL skip
   execution and return
   `MacosContainerActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosContainerActionResult(status=SUCCESS)`.
4. WHEN the operation fails THE SYSTEM SHALL return
   `MacosContainerActionResult(status=FAILED)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosContainerActionResult(status=DRY_RUN)`.

---

### Requirement 3: List Images

**User Story:** As a maintenance coordinator, I want to enumerate
every image present locally.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_images()` THE SYSTEM SHALL
   dispatch `<engine> images --format '{{json .}}'` through the
   command runner WITHOUT elevation.
2. THE SYSTEM SHALL parse the response as one JSON object per line
   and emit one `MacosImageInfo` per entry.
3. THE SYSTEM SHALL surface `<none>` repository / tag values as
   `None`.
4. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   list.

---

### Requirement 4: Create a Container

**User Story:** As a maintenance coordinator, I want to create a
container from an image with a declared spec.

#### Acceptance Criteria

1. WHEN the coordinator provides a `MacosContainerSpec` with `name`
   and `image` THE SYSTEM SHALL dispatch
   `<engine> create --name <name> [<flags...>] <image> [<argv...>]`.
2. THE SYSTEM SHALL translate `spec.env`, `spec.volumes`,
   `spec.ports`, `spec.networks`, `spec.restart_policy`,
   `spec.working_dir`, and `spec.user` into the corresponding `-e`,
   `-v`, `-p`, `--network`, `--restart`, `--workdir`, and `--user`
   flags.
3. WHEN a container of the same `name` already exists with the same
   `image` (probed via `<engine> inspect <name>`) THE SYSTEM SHALL
   skip execution and return
   `MacosContainerActionResult(status=SKIPPED)`.
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosContainerActionResult(status=SUCCESS)` populated with
   `container_id` taken from stdout.
5. WHEN the operation fails THE SYSTEM SHALL return
   `MacosContainerActionResult(status=FAILED)`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosContainerActionResult(status=DRY_RUN)`.

---

### Requirement 5: Start a Container

**User Story:** As a maintenance coordinator, I want to start an
existing container by id or name.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_container(container_id)` THE
   SYSTEM SHALL dispatch `<engine> start <container_id>`.
2. WHEN the container is already running THE SYSTEM SHALL skip
   execution and return
   `MacosContainerActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosContainerActionResult(status=SUCCESS)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosContainerActionResult(status=DRY_RUN)`.

---

### Requirement 6: Stop a Container

**User Story:** As a maintenance coordinator, I want to stop a
running container with a configurable timeout.

#### Acceptance Criteria

1. WHEN the coordinator calls `stop_container(container_id)` THE
   SYSTEM SHALL dispatch
   `<engine> stop -t <default_stop_timeout> <container_id>`.
2. WHEN the container is already stopped THE SYSTEM SHALL skip
   execution and return
   `MacosContainerActionResult(status=SKIPPED)`.
3. WHEN the container does not exist THE SYSTEM SHALL skip execution
   and return `MacosContainerActionResult(status=SKIPPED)`.
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosContainerActionResult(status=SUCCESS)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosContainerActionResult(status=DRY_RUN)`.

---

### Requirement 7: Remove a Container

**User Story:** As a maintenance coordinator, I want to remove a
container by id or name.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_container(container_id)` THE
   SYSTEM SHALL dispatch `<engine> rm -f <container_id>`.  The `-f`
   flag is always passed.
2. WHEN no container of that id / name exists THE SYSTEM SHALL skip
   execution and return
   `MacosContainerActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosContainerActionResult(status=SUCCESS)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosContainerActionResult(status=DRY_RUN)`.

---

### Requirement 8: List Containers

**User Story:** As a maintenance coordinator, I want to enumerate
every container.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_containers(all=True)` THE SYSTEM
   SHALL dispatch `<engine> ps -a --format '{{json .}}'`.
2. WHEN the coordinator calls `list_containers(all=False)` THE
   SYSTEM SHALL dispatch `<engine> ps --format '{{json .}}'`.
3. THE SYSTEM SHALL parse the response as one JSON object per line
   and emit one `MacosContainerInfo` per entry.
4. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   list.

---

### Requirement 9: Inspect a Container

**User Story:** As a maintenance coordinator, I want to inspect a
single container by id or name.

#### Acceptance Criteria

1. WHEN the coordinator calls `inspect_container(container_id)` THE
   SYSTEM SHALL dispatch `<engine> inspect <container_id>` WITHOUT
   elevation.
2. THE SYSTEM SHALL parse the JSON ARRAY response and pick element
   0.
3. THE SYSTEM SHALL return a `MacosContainerInfo`.
4. WHEN the container does not exist THE SYSTEM SHALL return `None`.

---

### Requirement 10: Get Container Logs

**User Story:** As a maintenance coordinator, I want to fetch the
tail of a container's log.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `get_container_logs(container_id, lines)` THE SYSTEM SHALL
   dispatch `<engine> logs --tail <lines> <container_id>` WITHOUT
   elevation.
2. THE SYSTEM SHALL return a `MacosContainerLogs` populated with
   `container_id`, `lines`, and `truncated=True`.
3. WHEN `lines` is omitted THE SYSTEM SHALL default to 100.
4. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   `MacosContainerLogs` rather than raise.

---

### Requirement 11: Execute a Command in a Container

**User Story:** As a maintenance coordinator, I want to run a command
inside a running container.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `exec_in_container(container_id, argv)` THE SYSTEM SHALL dispatch
   `<engine> exec <container_id> <argv...>`.
2. THE SYSTEM SHALL return a `MacosContainerExecResult` populated
   with `container_id`, `command_argv`, `output`, `exit_code`, and
   the standard `stdout` / `stderr` fields.
3. THE SYSTEM SHALL NOT skip — `exec_in_container` is operational.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosContainerExecResult(status=DRY_RUN)`.

---

### Requirement 12: Engine Selection and Elevation Policy

**User Story:** As a maintenance coordinator, I want a single
`MacosContainerManager` API regardless of whether the host runs
Docker Desktop, colima, or Podman Desktop.

#### Acceptance Criteria

1. WHEN `config.engine` is `MacosContainerEngine.DOCKER` THE SYSTEM
   SHALL invoke the `docker` binary for every verb.
2. WHEN `config.engine` is `MacosContainerEngine.PODMAN` THE SYSTEM
   SHALL invoke the `podman` binary for every verb.
3. WHEN `config.elevated` is True THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write verb.  macOS Docker
   Desktop / Podman Desktop normally run as the desktop user; the
   `elevated` flag is reserved for the rare case where the daemon
   socket is owned by `root`.
4. WHEN performing read-only operations THE SYSTEM SHALL NEVER pass
   `elevated=True`.
5. WHEN `config.elevated` is False THE SYSTEM SHALL NEVER pass
   `elevated=True`.
