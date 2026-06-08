"""
    macOS container-management feature.

    Implements the cross-OS container-management contract for macOS by
    wrapping the ``docker`` (default) or ``podman`` CLI through the
    injected :class:`ICommandRunner`.  The engine is selected at
    construction time via :class:`MacosContainerManagerConfig.engine`.

    Concept mapping:
        macOS does not have a native Linux container runtime.  Both
        Docker Desktop and colima ship a small Linux VM that runs the
        actual ``containerd`` / ``crun`` daemon.  The user-facing
        ``docker`` CLI is what this manager invokes; the daemon
        socket is whichever the active context points at.  Podman on
        macOS likewise runs containers inside a managed VM
        (``podman machine``).  In both cases the manager only sees
        the CLI — the VM and daemon are out of scope.

        * **Image** — pulled and removed via ``<engine> pull`` and
          ``<engine> rmi``; listed via
          ``<engine> images --format '{{json .}}'``.
        * **Container** — created via ``<engine> create``, started /
          stopped via ``<engine> start`` / ``<engine> stop``,
          removed via ``<engine> rm -f``, listed via
          ``<engine> ps --format '{{json .}}'``, inspected via
          ``<engine> inspect`` (returns a JSON ARRAY — element 0 is
          the desired record), logged via ``<engine> logs --tail``,
          and exec'd into via ``<engine> exec``.

        See ``specs/3-container-management/design.md`` for the full
        engine-mapping table.

    Engine selection:
        :class:`MacosContainerEngine` carries ``DOCKER`` (default —
        Docker Desktop or colima both expose a ``docker`` CLI) and
        ``PODMAN`` (Podman Desktop / Homebrew podman).

    Idempotency:
        Same as the Ubuntu sibling.  Every write verb except
        ``exec_in_container`` consults a read probe before invoking
        the runner.

    Elevation policy:
        macOS Docker Desktop and Podman Desktop both run as the
        desktop user, so elevation is rarely needed.  The
        ``elevated`` flag on the config exists for cross-OS-contract
        parity and for the rare case where the daemon socket is owned
        by ``root`` and the runner needs ``sudo``.  Reads never
        elevate.
"""

import json
import os

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants and engine binaries
# ----------------------------------------------------------------------


_DOCKER = "docker"
_PODMAN = "podman"


# Default tail count when the caller does not pass one.
_DEFAULT_LOG_LINES = 100


# Default stop timeout in seconds, mirroring the Docker default.
_DEFAULT_STOP_TIMEOUT = 10


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


MacosContainerActionStatus = ActionStatus


class MacosContainerEngine(Enum):
    """
        Supported container engines on macOS.

        ``DOCKER`` invokes the ``docker`` CLI, which on macOS is
        installed by either Docker Desktop or colima — both expose
        the same binary on PATH and target a Linux VM.  ``PODMAN``
        invokes ``podman`` (Podman Desktop / Homebrew), which
        likewise drives a Linux VM via ``podman machine``.
    """
    DOCKER = "docker"
    PODMAN = "podman"


@dataclass
class MacosContainerManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosContainerManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param engine: The container engine to invoke.  Defaults to
                       :attr:`MacosContainerEngine.DOCKER`.
        :param force_pull: When True, ``pull_image`` always invokes
                           ``<engine> pull`` even when the reference
                           is already present locally.
        :param default_stop_timeout: Default ``-t`` value passed to
                                     ``<engine> stop``.  Defaults to
                                     :data:`_DEFAULT_STOP_TIMEOUT`
                                     (10 s).
    """
    engine: MacosContainerEngine = MacosContainerEngine.DOCKER
    force_pull: bool = False
    default_stop_timeout: int = _DEFAULT_STOP_TIMEOUT


@dataclass
class MacosContainerActionResult(FeatureActionResult):
    """
        Result of a container-management write operation.

        :param engine: The engine the manager used for this call.
        :param container_id: Container id, when applicable.
        :param image: Image reference, when applicable.
        :param target: Generic target identifier (image ref or
                       container id).
        :param message: Human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes.
    """
    engine: Optional[MacosContainerEngine] = None
    container_id: Optional[str] = None
    image: Optional[str] = None
    target: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosContainerExecResult(FeatureActionResult):
    """
        Result of :meth:`MacosContainerManager.exec_in_container`.

        :param engine: The engine the manager used for this call.
        :param container_id: The container the command ran in.
        :param command_argv: The argv passed inside the container
                             (distinct from the outer ``command``
                             field which records the engine
                             invocation).
        :param output: Captured stdout from the inner command.
    """
    engine: Optional[MacosContainerEngine] = None
    container_id: Optional[str] = None
    command_argv: Optional[list] = None
    output: Optional[str] = None


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class MacosImageInfo:
    """
        Information about a single image reported by
        ``<engine> images --format '{{json .}}'``.

        :param id: Image id.
        :param repository: Repository portion of the reference, or
                           ``None`` for dangling images.
        :param tag: Tag portion, or ``None`` for ``<none>``.
        :param digest: Image digest when reported, else ``None``.
        :param size_bytes: On-disk size in bytes when parseable, else
                           ``None``.
        :param created: Free-form creation timestamp string.
    """
    id: str
    repository: Optional[str] = None
    tag: Optional[str] = None
    digest: Optional[str] = None
    size_bytes: Optional[int] = None
    created: str = ""


@dataclass
class MacosContainerSpec:
    """
        Specification for :meth:`MacosContainerManager.create_container`.

        :param name: Container name (the ``--name`` argument).
        :param image: Image reference.
        :param argv: Optional command override, appended after the
                     image positional.
        :param env: Environment-variable overrides, applied as
                    ``-e KEY=VALUE`` flags.
        :param volumes: Volume mount strings, applied as ``-v
                        <token>``.  Volume strings use POSIX-style
                        paths on macOS (``/Users/x:/data``); only
                        the host side is interpreted by the host
                        OS.
        :param ports: Port-publication strings (``host:container``),
                      applied as ``-p <token>``.
        :param networks: Network names, applied as ``--network
                         <name>``.
        :param restart_policy: Optional restart-policy token.
        :param working_dir: Optional ``--workdir`` value.
        :param user: Optional ``--user`` value.
        :param detach: Recorded for cross-OS-contract parity; ``create``
                       is non-blocking on every engine.
    """
    name: str
    image: str
    argv: Optional[list] = None
    env: Optional[dict] = None
    volumes: Optional[list] = None
    ports: Optional[list] = None
    networks: Optional[list] = None
    restart_policy: Optional[str] = None
    working_dir: Optional[str] = None
    user: Optional[str] = None
    detach: bool = True


@dataclass
class MacosContainerInfo:
    """
        Summary of a single container reported by
        ``<engine> ps --format '{{json .}}'`` or
        ``<engine> inspect``.

        :param id: Container id.
        :param name: Container name.
        :param image: Image reference the container was created from.
        :param status: Native human-readable status string.
        :param state: Lifecycle state token (``running``, ``exited``,
                      ``created``, ``paused``, ``restarting``,
                      ``dead``).
        :param created: ISO-8601 creation time when reported, else
                        the engine's free-form string.
        :param started: ISO-8601 start time when running, else
                        ``None``.
        :param ports: Published port mapping strings.
        :param mounts: Mount-target strings.
        :param command: The container's command argv.
        :param engine: The engine that reported the container.
    """
    id: str
    name: str
    image: str
    status: str = ""
    state: str = ""
    created: str = ""
    started: Optional[str] = None
    ports: list = field(default_factory=list)
    mounts: list = field(default_factory=list)
    command: list = field(default_factory=list)
    engine: Optional[MacosContainerEngine] = None


@dataclass
class MacosContainerLogs:
    """
        Captured log output from a container.

        :param container_id: The source container id.
        :param lines: One entry per log line.
        :param truncated: True when the manager asked the engine for
                          a line-limited tail.
    """
    container_id: str
    lines: list = field(default_factory=list)
    truncated: bool = False


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class MacosContainerQueryError(Exception):
    """
        Raised when a container read fails for a reason other than
        the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command: list = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Container query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosContainerEngineNotFoundError(Exception):
    """
        Raised when the configured engine binary is not available on
        the host.  The manager raises this lazily on the first verb
        that invokes the engine.
    """

    def __init__(
        self,
        *,
        engine: MacosContainerEngine,
        stderr: str,
    ) -> None:
        """
            :param engine: The engine that could not be invoked.
            :param stderr: The runner's captured stderr.
        """
        self.engine = engine
        self.stderr = stderr

        err_msg_lines = ["Container engine not found on host."]
        err_msg_lines.append("    ENGINE: {}".format(engine.value))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosContainerNotFoundError(Exception):
    """
        Reserved for cross-OS-contract parity.  Not raised by the
        public verbs today (absence is treated as ``SKIPPED``).
    """


# ----------------------------------------------------------------------
# MacosContainerManager
# ----------------------------------------------------------------------


class MacosContainerManager(BaseFeatureManager):
    """
        macOS container manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`.  See the module
        docstring for the engine selection model and elevation
        policy.
    """

    result_class = MacosContainerActionResult
    status_class = MacosContainerActionStatus
    feature_name = "container-management"

    # ------------------------------------------------------------------
    # Read verbs — images
    # ------------------------------------------------------------------

    def list_images(self) -> list:
        """
            Enumerate images via
            ``<engine> images --format '{{json .}}'``.

            :returns: One :class:`MacosImageInfo` per JSON line.
                      Returns an empty list when the engine exits
                      non-zero.
        """
        engine_binary = self._engine_binary()
        command = [engine_binary, "images", "--format", "{{json .}}"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _parse_image_lines(output=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs — containers
    # ------------------------------------------------------------------

    def list_containers(
        self,
        *,
        all: bool = False,
    ) -> list:
        """
            Enumerate containers via
            ``<engine> ps --format '{{json .}}'``.

            :param all: When True, ``-a`` is added so stopped
                        containers are included.

            :returns: One :class:`MacosContainerInfo` per JSON line.
        """
        engine_binary = self._engine_binary()
        engine_value = self._typed_config().engine
        command = [engine_binary, "ps"]
        if all:
            command.append("-a")
        command.extend(["--format", "{{json .}}"])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _parse_container_ps_lines(
            output=run_result.stdout, engine=engine_value,
        )
        return rtnval

    def inspect_container(
        self,
        container_id: str,
    ) -> Optional[MacosContainerInfo]:
        """
            Inspect a single container via ``<engine> inspect``.  The
            engine returns a JSON ARRAY; element 0 is the inspected
            container.

            :param container_id: Container id or name.

            :returns: The :class:`MacosContainerInfo`, or ``None``
                      when no container of that id / name exists.
        """
        record = self._inspect_raw(target=container_id)
        if record is None:
            rtnval: Optional[MacosContainerInfo] = None
            return rtnval
        engine_value = self._typed_config().engine
        rtnval = _build_info_from_inspect(record=record, engine=engine_value)
        return rtnval

    def get_container_logs(
        self,
        container_id: str,
        lines: int = _DEFAULT_LOG_LINES,
    ) -> MacosContainerLogs:
        """
            Fetch up to ``lines`` log lines via
            ``<engine> logs --tail <lines> <id>``.

            :param container_id: Container id or name.
            :param lines: Maximum number of tail lines requested.

            :returns: The :class:`MacosContainerLogs`.
        """
        engine_binary = self._engine_binary()
        command = [
            engine_binary, "logs",
            "--tail", str(lines),
            container_id,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            empty = MacosContainerLogs(
                container_id=container_id,
                lines=[],
                truncated=True,
            )
            return empty
        log_lines = _split_log_lines(output=run_result.stdout)
        rtnval = MacosContainerLogs(
            container_id=container_id,
            lines=log_lines,
            truncated=True,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — images
    # ------------------------------------------------------------------

    def pull_image(self, ref: str) -> MacosContainerActionResult:
        """
            Pull ``ref`` via ``<engine> pull <ref>``.

            :param ref: Image reference.

            :returns: The :class:`MacosContainerActionResult`.
        """
        engine_binary = self._engine_binary()
        engine_value = self._typed_config().engine
        command = [engine_binary, "pull", ref]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosContainerActionStatus.DRY_RUN,
                command=command,
                target=ref,
                image=ref,
                engine=engine_value,
            )
            return dry

        force = self._typed_config().force_pull
        if not force:
            already_present = self._image_present(ref=ref)
            if already_present:
                skipped = self._build_result(
                    status=MacosContainerActionStatus.SKIPPED,
                    command=command,
                    target=ref,
                    image=ref,
                    engine=engine_value,
                    message="image {} already present".format(ref),
                )
                return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=ref,
            image=ref,
            engine=engine_value,
        )
        return rtnval

    def remove_image(self, ref: str) -> MacosContainerActionResult:
        """
            Remove ``ref`` via ``<engine> rmi <ref>``.

            :param ref: Image reference.

            :returns: The :class:`MacosContainerActionResult`.
        """
        engine_binary = self._engine_binary()
        engine_value = self._typed_config().engine
        command = [engine_binary, "rmi", ref]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosContainerActionStatus.DRY_RUN,
                command=command,
                target=ref,
                image=ref,
                engine=engine_value,
            )
            return dry

        already_present = self._image_present(ref=ref)
        if not already_present:
            skipped = self._build_result(
                status=MacosContainerActionStatus.SKIPPED,
                command=command,
                target=ref,
                image=ref,
                engine=engine_value,
                message="image {} not present".format(ref),
            )
            return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=ref,
            image=ref,
            engine=engine_value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — containers
    # ------------------------------------------------------------------

    def create_container(
        self,
        spec: MacosContainerSpec,
    ) -> MacosContainerActionResult:
        """
            Create a container via ``<engine> create --name <name>
            [<flags...>] <image> [<argv...>]``.

            :param spec: The :class:`MacosContainerSpec`.

            :returns: The :class:`MacosContainerActionResult`.
        """
        engine_binary = self._engine_binary()
        engine_value = self._typed_config().engine
        command = _build_create_argv(
            engine_binary=engine_binary, spec=spec,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosContainerActionStatus.DRY_RUN,
                command=command,
                target=spec.name,
                image=spec.image,
                engine=engine_value,
            )
            return dry

        existing = self._inspect_raw(target=spec.name)
        if existing is not None:
            existing_image = _extract_image_from_inspect(record=existing)
            if existing_image == spec.image:
                skipped = self._build_result(
                    status=MacosContainerActionStatus.SKIPPED,
                    command=command,
                    target=spec.name,
                    image=spec.image,
                    engine=engine_value,
                    container_id=existing.get("Id"),
                    message=(
                        "container {} already exists with image {}".format(
                            spec.name, spec.image,
                        )
                    ),
                )
                return skipped

        result = self._dispatch_container_write(
            command=command,
            target=spec.name,
            image=spec.image,
            engine=engine_value,
        )
        if result.status == MacosContainerActionStatus.SUCCESS:
            new_id = result.stdout.strip() if result.stdout is not None else ""
            if len(new_id) > 0:
                result.container_id = new_id
        return result

    def start_container(
        self,
        container_id: str,
    ) -> MacosContainerActionResult:
        """
            Start a container via ``<engine> start <id>``.

            :param container_id: Container id or name.

            :returns: The :class:`MacosContainerActionResult`.
        """
        engine_binary = self._engine_binary()
        engine_value = self._typed_config().engine
        command = [engine_binary, "start", container_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosContainerActionStatus.DRY_RUN,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
            )
            return dry

        existing = self._inspect_raw(target=container_id)
        if existing is not None:
            running = _extract_running(record=existing)
            if running:
                skipped = self._build_result(
                    status=MacosContainerActionStatus.SKIPPED,
                    command=command,
                    target=container_id,
                    container_id=container_id,
                    engine=engine_value,
                    message=(
                        "container {} is already running".format(
                            container_id,
                        )
                    ),
                )
                return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=container_id,
            container_id=container_id,
            engine=engine_value,
        )
        return rtnval

    def stop_container(
        self,
        container_id: str,
    ) -> MacosContainerActionResult:
        """
            Stop a container via ``<engine> stop -t <timeout> <id>``.

            :param container_id: Container id or name.

            :returns: The :class:`MacosContainerActionResult`.
        """
        engine_binary = self._engine_binary()
        engine_value = self._typed_config().engine
        timeout = self._typed_config().default_stop_timeout
        command = [
            engine_binary, "stop",
            "-t", str(timeout),
            container_id,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosContainerActionStatus.DRY_RUN,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
            )
            return dry

        existing = self._inspect_raw(target=container_id)
        if existing is None:
            skipped = self._build_result(
                status=MacosContainerActionStatus.SKIPPED,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
                message="container {} does not exist".format(container_id),
            )
            return skipped

        running = _extract_running(record=existing)
        if not running:
            skipped = self._build_result(
                status=MacosContainerActionStatus.SKIPPED,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
                message=(
                    "container {} is already stopped".format(container_id)
                ),
            )
            return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=container_id,
            container_id=container_id,
            engine=engine_value,
        )
        return rtnval

    def remove_container(
        self,
        container_id: str,
    ) -> MacosContainerActionResult:
        """
            Remove a container via ``<engine> rm -f <id>``.

            :param container_id: Container id or name.

            :returns: The :class:`MacosContainerActionResult`.
        """
        engine_binary = self._engine_binary()
        engine_value = self._typed_config().engine
        command = [engine_binary, "rm", "-f", container_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosContainerActionStatus.DRY_RUN,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
            )
            return dry

        existing = self._inspect_raw(target=container_id)
        if existing is None:
            skipped = self._build_result(
                status=MacosContainerActionStatus.SKIPPED,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
                message="container {} does not exist".format(container_id),
            )
            return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=container_id,
            container_id=container_id,
            engine=engine_value,
        )
        return rtnval

    def exec_in_container(
        self,
        container_id: str,
        argv: list,
    ) -> MacosContainerExecResult:
        """
            Run ``argv`` inside the container via
            ``<engine> exec <id> <argv...>``.  Never skips.

            :param container_id: Container id or name.
            :param argv: The argv to run inside the container.

            :returns: The :class:`MacosContainerExecResult`.
        """
        engine_binary = self._engine_binary()
        engine_value = self._typed_config().engine
        command = [engine_binary, "exec", container_id] + list(argv)

        if self._config.dry_run:
            dry = MacosContainerExecResult(
                status=MacosContainerActionStatus.DRY_RUN,
                command=list(command),
                detail=container_id,
                engine=engine_value,
                container_id=container_id,
                command_argv=list(argv),
                output=None,
            )
            return dry

        run_result = self._runner.run(
            list(command), elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosContainerActionStatus.SUCCESS
        else:
            mapped_status = MacosContainerActionStatus.FAILED

        rtnval = MacosContainerExecResult(
            status=mapped_status,
            command=list(command),
            detail=container_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            engine=engine_value,
            container_id=container_id,
            command_argv=list(argv),
            output=run_result.stdout,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosContainerManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`MacosContainerManagerConfig`.
        """
        if isinstance(self._config, MacosContainerManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosContainerManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _engine_binary(self) -> str:
        """
            Return the CLI binary name for the configured engine.
        """
        engine_value = self._typed_config().engine
        if engine_value == MacosContainerEngine.PODMAN:
            rtnval = _PODMAN
        else:
            rtnval = _DOCKER
        return rtnval

    def _image_present(self, *, ref: str) -> bool:
        """
            Probe ``<engine> images --filter 'reference=<ref>'``.
        """
        engine_binary = self._engine_binary()
        filter_token = "reference={}".format(ref)
        command = [
            engine_binary, "images",
            "--filter", filter_token,
            "--format", "{{json .}}",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        stripped = run_result.stdout.strip()
        rtnval = len(stripped) > 0
        return rtnval

    def _inspect_raw(self, *, target: str) -> Optional[dict]:
        """
            Run ``<engine> inspect <target>`` and return the first
            element of the JSON-array response, or ``None``.
        """
        engine_binary = self._engine_binary()
        command = [engine_binary, "inspect", target]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval
        rtnval = _parse_inspect_first(output=run_result.stdout)
        return rtnval

    def _dispatch_container_write(
        self,
        *,
        command: list,
        target: Optional[str] = None,
        image: Optional[str] = None,
        container_id: Optional[str] = None,
        engine: Optional[MacosContainerEngine] = None,
    ) -> MacosContainerActionResult:
        """
            Dispatch a write command and wrap the result with the
            relevant container-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="container_write",
            target=target,
            image=image,
            container_id=container_id,
            engine=engine,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosContainerActionStatus,
        command: list,
        target: Optional[str] = None,
        image: Optional[str] = None,
        container_id: Optional[str] = None,
        engine: Optional[MacosContainerEngine] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosContainerActionResult:
        """
            Construct a :class:`MacosContainerActionResult` without
            invoking the runner.
        """
        if message is not None:
            detail_value: Optional[str] = message
        else:
            detail_value = target
        rtnval = MacosContainerActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            target=target,
            image=image,
            container_id=container_id,
            engine=engine,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level argv builders
# ----------------------------------------------------------------------


def _build_create_argv(
    *,
    engine_binary: str,
    spec: MacosContainerSpec,
) -> list:
    """
        Build the argv for ``<engine> create``.
    """
    command = [engine_binary, "create", "--name", spec.name]

    if spec.restart_policy is not None:
        restart_token = "--restart={}".format(spec.restart_policy)
        command.append(restart_token)

    if spec.user is not None:
        command.extend(["--user", spec.user])

    if spec.working_dir is not None:
        command.extend(["--workdir", spec.working_dir])

    if spec.env is not None:
        for key in spec.env:
            value = spec.env[key]
            kv_token = "{}={}".format(key, value)
            command.extend(["-e", kv_token])

    if spec.volumes is not None:
        for volume_token in spec.volumes:
            command.extend(["-v", volume_token])

    if spec.ports is not None:
        for port_token in spec.ports:
            command.extend(["-p", port_token])

    if spec.networks is not None:
        for network_name in spec.networks:
            command.extend(["--network", network_name])

    command.append(spec.image)

    if spec.argv is not None and len(spec.argv) > 0:
        command.extend(list(spec.argv))

    return command


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _parse_image_lines(*, output: str) -> list:
    """
        Parse one JSON object per line into :class:`MacosImageInfo`
        rows.
    """
    rtnval: list = []
    stripped = output.strip()
    if len(stripped) == 0:
        return rtnval
    for raw_line in stripped.splitlines():
        line_text = raw_line.strip()
        if len(line_text) == 0:
            continue
        try:
            entry = json.loads(line_text)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        image_id = entry.get("ID", entry.get("Id", ""))
        repo_value = entry.get("Repository")
        if isinstance(repo_value, str) and repo_value not in ("", "<none>"):
            repository: Optional[str] = repo_value
        else:
            repository = None
        tag_value = entry.get("Tag")
        if isinstance(tag_value, str) and tag_value not in ("", "<none>"):
            tag: Optional[str] = tag_value
        else:
            tag = None
        digest_value = entry.get("Digest")
        if (
            isinstance(digest_value, str)
            and digest_value not in ("", "<none>")
        ):
            digest: Optional[str] = digest_value
        else:
            digest = None
        size_bytes = _parse_image_size(raw_value=entry.get("Size"))
        created_value = entry.get("CreatedAt", entry.get("Created", ""))
        if not isinstance(created_value, str):
            created_value = str(created_value)
        info = MacosImageInfo(
            id=image_id,
            repository=repository,
            tag=tag,
            digest=digest,
            size_bytes=size_bytes,
            created=created_value,
        )
        rtnval.append(info)
    return rtnval


def _parse_image_size(*, raw_value) -> Optional[int]:
    """
        Parse the engine's ``Size`` field tolerating int and string
        variants.
    """
    if raw_value is None:
        rtnval: Optional[int] = None
        return rtnval
    if isinstance(raw_value, int):
        rtnval = raw_value
        return rtnval
    if not isinstance(raw_value, str):
        rtnval = None
        return rtnval
    text = raw_value.strip()
    if len(text) == 0:
        rtnval = None
        return rtnval
    try:
        rtnval = int(text)
    except ValueError:
        rtnval = None
    return rtnval


def _parse_container_ps_lines(
    *,
    output: str,
    engine: MacosContainerEngine,
) -> list:
    """
        Parse ``<engine> ps --format '{{json .}}'`` output.
    """
    rtnval: list = []
    stripped = output.strip()
    if len(stripped) == 0:
        return rtnval
    for raw_line in stripped.splitlines():
        line_text = raw_line.strip()
        if len(line_text) == 0:
            continue
        try:
            entry = json.loads(line_text)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        container_id = entry.get("ID", entry.get("Id", ""))
        names_field = entry.get("Names")
        if isinstance(names_field, list) and len(names_field) > 0:
            name = names_field[0]
        elif isinstance(names_field, str) and len(names_field) > 0:
            name = names_field
        else:
            name = entry.get("Name", "")
        image = entry.get("Image", "")
        status = entry.get("Status", "")
        state = entry.get("State", "")
        created = entry.get("CreatedAt", entry.get("Created", ""))
        if not isinstance(created, str):
            created = str(created)
        ports_value = entry.get("Ports")
        if isinstance(ports_value, list):
            ports_list = list(ports_value)
        elif isinstance(ports_value, str) and len(ports_value) > 0:
            ports_list = [token.strip() for token in ports_value.split(",")]
        else:
            ports_list = []
        mounts_value = entry.get("Mounts")
        if isinstance(mounts_value, list):
            mounts_list = list(mounts_value)
        elif isinstance(mounts_value, str) and len(mounts_value) > 0:
            mounts_list = [token.strip() for token in mounts_value.split(",")]
        else:
            mounts_list = []
        command_value = entry.get("Command")
        if isinstance(command_value, list):
            command_list = list(command_value)
        elif isinstance(command_value, str) and len(command_value) > 0:
            command_list = [command_value]
        else:
            command_list = []
        info = MacosContainerInfo(
            id=container_id,
            name=name,
            image=image,
            status=status,
            state=state,
            created=created,
            started=None,
            ports=ports_list,
            mounts=mounts_list,
            command=command_list,
            engine=engine,
        )
        rtnval.append(info)
    return rtnval


def _parse_inspect_first(*, output: str) -> Optional[dict]:
    """
        Parse ``<engine> inspect`` output (a JSON array) and return
        element 0.
    """
    stripped = output.strip()
    if len(stripped) == 0:
        rtnval: Optional[dict] = None
        return rtnval
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = None
        return rtnval
    if not isinstance(data, list):
        rtnval = None
        return rtnval
    if len(data) == 0:
        rtnval = None
        return rtnval
    head = data[0]
    if not isinstance(head, dict):
        rtnval = None
        return rtnval
    rtnval = head
    return rtnval


def _build_info_from_inspect(
    *,
    record: dict,
    engine: MacosContainerEngine,
) -> MacosContainerInfo:
    """
        Build a :class:`MacosContainerInfo` from a single inspect
        record.
    """
    container_id = record.get("Id", record.get("ID", ""))
    raw_name = record.get("Name", "")
    if isinstance(raw_name, str) and raw_name.startswith("/"):
        name = raw_name[1:]
    else:
        name = raw_name
    config_section = record.get("Config")
    if isinstance(config_section, dict):
        image = config_section.get("Image", "")
        cmd_value = config_section.get("Cmd")
        if isinstance(cmd_value, list):
            command_list = list(cmd_value)
        else:
            command_list = []
    else:
        image = ""
        command_list = []
    state_section = record.get("State")
    if isinstance(state_section, dict):
        state_status = state_section.get("Status", "")
        running_flag = bool(state_section.get("Running", False))
        started_at = state_section.get("StartedAt")
        if isinstance(started_at, str) and len(started_at) > 0:
            started_value: Optional[str] = started_at
        else:
            started_value = None
    else:
        state_status = ""
        running_flag = False
        started_value = None
    if running_flag:
        status_text = "Up"
    else:
        status_text = state_status
    created = record.get("Created", "")
    if not isinstance(created, str):
        created = str(created)
    network_section = record.get("NetworkSettings")
    ports_list: list = []
    if isinstance(network_section, dict):
        ports_section = network_section.get("Ports")
        if isinstance(ports_section, dict):
            for port_key in ports_section:
                ports_list.append(port_key)
    mounts_section = record.get("Mounts")
    mounts_list: list = []
    if isinstance(mounts_section, list):
        for mount_entry in mounts_section:
            if not isinstance(mount_entry, dict):
                continue
            destination = mount_entry.get("Destination", "")
            if isinstance(destination, str) and len(destination) > 0:
                mounts_list.append(destination)
    rtnval = MacosContainerInfo(
        id=container_id,
        name=name,
        image=image,
        status=status_text,
        state=state_status,
        created=created,
        started=started_value,
        ports=ports_list,
        mounts=mounts_list,
        command=command_list,
        engine=engine,
    )
    return rtnval


def _extract_image_from_inspect(*, record: dict) -> str:
    """
        Pull the configured image reference from an inspect record.
    """
    config_section = record.get("Config")
    if isinstance(config_section, dict):
        image_value = config_section.get("Image")
        if isinstance(image_value, str) and len(image_value) > 0:
            rtnval = image_value
            return rtnval
    fallback = record.get("Image", "")
    if isinstance(fallback, str):
        rtnval = fallback
    else:
        rtnval = ""
    return rtnval


def _extract_running(*, record: dict) -> bool:
    """
        Return True when an inspect record's ``State.Running`` is True.
    """
    state_section = record.get("State")
    if not isinstance(state_section, dict):
        rtnval = False
        return rtnval
    rtnval = bool(state_section.get("Running", False))
    return rtnval


def _split_log_lines(*, output: str) -> list:
    """
        Split engine log output into one entry per line.
    """
    rtnval: list = []
    if output is None:
        return rtnval
    text = output
    if text.endswith(os.linesep):
        text = text[:-len(os.linesep)]
    elif text.endswith("\n"):
        text = text[:-1]
    if len(text) == 0:
        return rtnval
    rtnval = text.split("\n")
    return rtnval
