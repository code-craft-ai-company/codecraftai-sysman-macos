"""
    macOS process-management feature.

    Implements the cross-OS process-management contract for macOS using
    BSD ``ps``, ``kill``, ``pkill``, ``pgrep``, ``renice``, and
    ``nohup``.  Every verb is dispatched through an
    :class:`ICommandRunner` so the manager works identically under
    local and SSH execution.

    Verb / tool mapping:

    +------------------------+-----------------------------------------+
    | Verb                   | macOS tool                              |
    +========================+=========================================+
    | ``list_processes``     | ``ps -A -o pid,ppid,user,stat,pri,ni,`` |
    |                        | ``vsz,rss,%cpu,%mem,etime,comm,args``   |
    +------------------------+-----------------------------------------+
    | ``get_process``        | ``ps -p <pid> -o ...`` with the same    |
    |                        | format spec                             |
    +------------------------+-----------------------------------------+
    | ``kill_process``       | ``kill -<signal> <pid>``                |
    +------------------------+-----------------------------------------+
    | ``kill_process_tree``  | ``pkill -<signal> -P <pid>`` then       |
    |                        | ``kill -<signal> <pid>``                |
    +------------------------+-----------------------------------------+
    | ``start_process``      | ``nohup sh -c '<command> &'`` via the   |
    |                        | runner (pilot blocks until completion;  |
    |                        | macOS lacks ``setsid``)                 |
    +------------------------+-----------------------------------------+
    | ``set_process_priority`` | ``renice <priority> -p <pid>``        |
    +------------------------+-----------------------------------------+
    | ``wait_for_process``   | poll ``kill -0 <pid>`` with sleep       |
    +------------------------+-----------------------------------------+
    | ``find_processes_by_name`` | ``pgrep -l <name>``                 |
    +------------------------+-----------------------------------------+

    Idempotency / dry-run / elevation:

    - ``kill_process`` / ``kill_process_tree`` SKIP when the target PID
      no longer exists (verified through ``get_process``).
    - ``set_process_priority`` SKIPs when the current ``ni`` value
      already matches the requested priority.
    - ``start_process`` is non-idempotent and never skips.
    - All writes honour ``config.dry_run`` and short-circuit to
      :attr:`MacosProcessActionStatus.DRY_RUN` without invoking the
      runner.
    - All writes pass ``elevated=config.elevated`` to the runner; the
      runner is responsible for applying ``sudo`` when ``elevated=True``.
    - All reads pass ``elevated=False`` regardless of config — reads
      never elevate per contract.

    Differences from the Ubuntu implementation:

    - ``ps`` on macOS uses ``-A`` for "all processes" (the Linux
      ``-e`` shortcut also works but ``-A`` is the documented BSD
      switch).  ``%cpu`` / ``%mem`` are the BSD column names where
      Linux uses ``pcpu`` / ``pmem``.
    - macOS does not ship ``setsid``.  ``start_process`` uses
      ``nohup sh -c '<command> &'`` to detach the spawned process from
      the controlling terminal.  Pilot still blocks until the wrapping
      shell exits — same caveat as Ubuntu.
    - ``pgrep -l`` on macOS prints ``<pid> <command-name>`` (no full
      argv); the Ubuntu pilot uses ``-a`` which is not available on
      macOS' BSD ``pgrep``.
"""

import os
import time

from dataclasses import dataclass, field
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_PS = "ps"
_KILL = "kill"
_PKILL = "pkill"
_PGREP = "pgrep"
_RENICE = "renice"
_NOHUP = "nohup"
_SH = "sh"

# BSD ``ps`` accepts the same column-list shape as Linux ``ps`` but the
# ``%cpu`` / ``%mem`` column names differ from the Linux ``pcpu`` /
# ``pmem`` aliases.
_PS_FORMAT = (
    "pid,ppid,user,stat,pri,ni,vsz,rss,%cpu,%mem,etime,comm,args"
)

_DEFAULT_SIGNAL = "TERM"

_KNOWN_SIGNALS = frozenset({
    "TERM", "KILL", "HUP", "INT", "QUIT", "USR1", "USR2",
})


# Backwards-compatible alias matching the contract's naming.
MacosProcessActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class MacosProcessManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosProcessManager`.

        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param default_signal: Signal name (without the ``SIG`` prefix)
                               used when ``kill_process`` /
                               ``kill_process_tree`` are called without
                               an explicit ``signal`` argument.
                               Defaults to ``"TERM"``.
        :param wait_poll_interval_seconds: Seconds between successive
                                           ``kill -0`` probes inside
                                           :meth:`MacosProcessManager.wait_for_process`.
    """
    default_signal: str = _DEFAULT_SIGNAL
    wait_poll_interval_seconds: float = 0.2


@dataclass
class MacosProcessInfo:
    """
        Per-process record returned by ``list_processes`` /
        ``get_process`` / ``find_processes_by_name``.
    """
    pid: int
    ppid: Optional[int] = None
    user: Optional[str] = None
    state: Optional[str] = None
    priority: Optional[int] = None
    nice: Optional[int] = None
    vsz: Optional[int] = None
    rss: Optional[int] = None
    cpu_percent: Optional[float] = None
    mem_percent: Optional[float] = None
    elapsed: Optional[str] = None
    command: Optional[str] = None
    argv: list = field(default_factory=list)


@dataclass
class MacosProcessSpec:
    """
        Spec for :meth:`MacosProcessManager.start_process`.
    """
    executable: str
    args: list = field(default_factory=list)
    working_dir: Optional[str] = None
    env: Optional[dict] = None
    user: Optional[str] = None


@dataclass
class MacosProcessActionResult(FeatureActionResult):
    """
        Outcome of a process-management write.

        :param pid: Target PID (``kill`` / ``set_priority`` / spawned
                    PID for ``start_process``).
        :param signal: Signal name dispatched.
    """
    pid: Optional[int] = None
    signal: Optional[str] = None


@dataclass
class MacosProcessWaitResult:
    """
        Outcome of :meth:`MacosProcessManager.wait_for_process`.

        :param pid: Target PID.
        :param exit_code: Exit code of the process when known.  Left
                          ``None`` because only the parent process can
                          recover the exit code on macOS.
        :param timed_out: ``True`` when the wait exceeded the timeout.
    """
    pid: int
    exit_code: Optional[int] = None
    timed_out: bool = False


# ----------------------------------------------------------------------
# Exception
# ----------------------------------------------------------------------


class MacosProcessQueryError(Exception):
    """
        Raised when a process read fails for a reason other than the
        target PID being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param message: Optional override for the exception message.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Process query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosProcessManager(BaseFeatureManager):
    """
        macOS process manager.  Wraps BSD ``ps``, ``kill``, ``pkill``,
        ``pgrep``, ``renice``, and ``nohup`` through the injected
        :class:`ICommandRunner`.
    """

    result_class = MacosProcessActionResult
    status_class = MacosProcessActionStatus
    feature_name = "process-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_processes(
        self,
        *,
        owner: Optional[str] = None,
    ) -> list:
        """
            List running processes, optionally restricted to ``owner``.

            Runs ``ps -A -o <format>`` (or ``ps -U <owner> -o <format>``
            when ``owner`` is supplied — BSD ``ps`` uses ``-U`` rather
            than the Linux ``-u``).

            :param owner: Username to scope the listing to, or ``None``
                          for all users.

            :returns: A list of :class:`MacosProcessInfo`.
            :raises MacosProcessQueryError: When ``ps`` exits non-zero.
        """
        if owner is None:
            command = [_PS, "-A", "-o", _PS_FORMAT]
        else:
            command = [_PS, "-U", owner, "-o", _PS_FORMAT]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosProcessQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_ps_output(stdout=run_result.stdout)
        return records

    def get_process(self, *, pid: int) -> Optional[MacosProcessInfo]:
        """
            Look up a single process by PID.

            Runs ``ps -p <pid> -o <format>``.  An unknown PID surfaces
            as ``None`` rather than an exception, matching the contract.

            :param pid: Target process ID.

            :returns: The :class:`MacosProcessInfo` or ``None`` when
                      the PID is not running.
        """
        command = [_PS, "-p", str(pid), "-o", _PS_FORMAT]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) == 0:
                return None
            raise MacosProcessQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_ps_output(stdout=run_result.stdout)
        if len(records) == 0:
            return None

        first = records[0]
        return first

    def find_processes_by_name(self, *, name: str) -> list:
        """
            Find running processes matching ``name`` via ``pgrep -l``.

            BSD ``pgrep -l`` prints ``<pid> <command>`` per match —
            note that BSD ``pgrep`` does NOT support the Linux ``-a``
            flag that prints the full argv.

            :param name: Pattern matched against process names.

            :returns: A list of :class:`MacosProcessInfo`.  Empty when
                      no processes match.
        """
        command = [_PGREP, "-l", name]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 1:
            return []

        if run_result.exit_code != 0:
            raise MacosProcessQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_pgrep_output(stdout=run_result.stdout)
        return records

    def wait_for_process(
        self,
        *,
        pid: int,
        timeout_seconds: Optional[float] = None,
    ) -> MacosProcessWaitResult:
        """
            Block until ``pid`` exits or ``timeout_seconds`` elapses.

            Polls ``kill -0 <pid>`` at the configured cadence.

            :param pid: Target process ID.
            :param timeout_seconds: Maximum wait in wall-clock seconds.

            :returns: A :class:`MacosProcessWaitResult`.
        """
        start_monotonic = time.monotonic()
        poll_interval = self._typed_config().wait_poll_interval_seconds

        while True:
            probe_command = [_KILL, "-0", str(pid)]
            probe_result = self._runner.run(
                list(probe_command), elevated=False,
            )

            if probe_result.exit_code != 0:
                rtnval = MacosProcessWaitResult(
                    pid=pid, exit_code=None, timed_out=False,
                )
                return rtnval

            if timeout_seconds is not None:
                elapsed = time.monotonic() - start_monotonic
                if elapsed >= timeout_seconds:
                    rtnval = MacosProcessWaitResult(
                        pid=pid, exit_code=None, timed_out=True,
                    )
                    return rtnval

            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def kill_process(
        self,
        *,
        pid: int,
        signal: Optional[str] = None,
    ) -> MacosProcessActionResult:
        """
            Send ``signal`` to ``pid``.  Skipped when the PID is no
            longer present.
        """
        effective_signal = self._resolve_signal(signal=signal)
        command = [_KILL, "-{}".format(effective_signal), str(pid)]

        if self._config.dry_run:
            dry_result = self._build_result(
                status=MacosProcessActionStatus.DRY_RUN,
                command=command,
                pid=pid,
                signal=effective_signal,
                detail=str(pid),
            )
            return dry_result

        existing = self.get_process(pid=pid)
        if existing is None:
            skip_result = self._build_result(
                status=MacosProcessActionStatus.SKIPPED,
                command=command,
                pid=pid,
                signal=effective_signal,
                detail="pid {} not present".format(pid),
            )
            return skip_result

        rtnval = self._dispatch(
            command=command,
            detail=str(pid),
            operation="kill_process",
            pid=pid,
            signal=effective_signal,
        )
        return rtnval

    def kill_process_tree(
        self,
        *,
        pid: int,
        signal: Optional[str] = None,
    ) -> MacosProcessActionResult:
        """
            Send ``signal`` to ``pid`` and all of its direct children.

            Implementation: ``pkill -<signal> -P <pid>`` first, then
            ``kill -<signal> <pid>``.
        """
        effective_signal = self._resolve_signal(signal=signal)
        children_command = [
            _PKILL, "-{}".format(effective_signal), "-P", str(pid),
        ]
        root_command = [_KILL, "-{}".format(effective_signal), str(pid)]

        if self._config.dry_run:
            dry_result = self._build_result(
                status=MacosProcessActionStatus.DRY_RUN,
                command=root_command,
                pid=pid,
                signal=effective_signal,
                detail=str(pid),
            )
            return dry_result

        existing = self.get_process(pid=pid)
        if existing is None:
            skip_result = self._build_result(
                status=MacosProcessActionStatus.SKIPPED,
                command=root_command,
                pid=pid,
                signal=effective_signal,
                detail="pid {} not present".format(pid),
            )
            return skip_result

        children_run = self._runner.run(
            list(children_command),
            elevated=self._config.elevated,
        )
        children_failed = (
            children_run.exit_code is not None
            and children_run.exit_code not in (0, 1)
        )

        rtnval = self._dispatch(
            command=root_command,
            detail=str(pid),
            operation="kill_process_tree",
            pid=pid,
            signal=effective_signal,
        )

        if children_failed is True and rtnval.status == MacosProcessActionStatus.SUCCESS:
            rtnval.detail = "{} (child sweep exit={})".format(
                rtnval.detail, children_run.exit_code,
            )
        return rtnval

    def start_process(
        self,
        *,
        spec: MacosProcessSpec,
    ) -> MacosProcessActionResult:
        """
            Spawn a new process described by ``spec``.

            Implementation: ``nohup sh -c '<argv> &'`` is dispatched
            through the runner so that the spawned process detaches
            from the controlling terminal.  Pilot limitation: the
            runner blocks until the wrapping shell returns.
        """
        argv = [spec.executable] + list(spec.args)
        inner_line = _shell_escape_argv(argv=argv)
        command_line = "{} &".format(inner_line)
        command = [_NOHUP, _SH, "-c", command_line]

        if self._config.dry_run:
            dry_result = self._build_result(
                status=MacosProcessActionStatus.DRY_RUN,
                command=command,
                pid=None,
                signal=None,
                detail=spec.executable,
            )
            return dry_result

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
            cwd=spec.working_dir,
            env=spec.env,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosProcessActionStatus.SUCCESS
        else:
            mapped_status = MacosProcessActionStatus.FAILED

        pid_value: Optional[int] = None
        if mapped_status == MacosProcessActionStatus.SUCCESS:
            pid_value = _best_effort_pid_lookup(
                manager=self, executable=spec.executable,
            )

        rtnval = self._build_result(
            status=mapped_status,
            command=command,
            pid=pid_value,
            signal=None,
            detail=spec.executable,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
        )
        return rtnval

    def set_process_priority(
        self,
        *,
        pid: int,
        priority: int,
    ) -> MacosProcessActionResult:
        """
            Adjust the CPU nice value of ``pid``.

            Runs ``renice <priority> -p <pid>``.  Skipped when the
            current nice value already matches ``priority``.
        """
        command = [_RENICE, str(priority), "-p", str(pid)]

        if self._config.dry_run:
            dry_result = self._build_result(
                status=MacosProcessActionStatus.DRY_RUN,
                command=command,
                pid=pid,
                signal=None,
                detail=str(priority),
            )
            return dry_result

        existing = self.get_process(pid=pid)
        if existing is not None and existing.nice is not None:
            if existing.nice == priority:
                skip_result = self._build_result(
                    status=MacosProcessActionStatus.SKIPPED,
                    command=command,
                    pid=pid,
                    signal=None,
                    detail="nice already {}".format(priority),
                )
                return skip_result

        rtnval = self._dispatch(
            command=command,
            detail=str(priority),
            operation="set_process_priority",
            pid=pid,
            signal=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_signal(self, *, signal: Optional[str]) -> str:
        """
            Validate and resolve a signal name.
        """
        if signal is None:
            candidate = self._typed_config().default_signal
        else:
            candidate = signal

        normalised = candidate.upper()
        if normalised.startswith("SIG"):
            normalised = normalised[3:]

        if normalised not in _KNOWN_SIGNALS:
            err_msg_lines = ["Unsupported POSIX signal."]
            err_msg_lines.append("    REQUESTED: {}".format(candidate))
            err_msg_lines.append(
                "    KNOWN: {}".format(sorted(_KNOWN_SIGNALS)),
            )
            err_msg = os.linesep.join(err_msg_lines)
            raise ValueError(err_msg)

        return normalised

    def _build_result(
        self,
        *,
        status: MacosProcessActionStatus,
        command: list,
        pid: Optional[int],
        signal: Optional[str],
        detail: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosProcessActionResult:
        """
            Construct a :class:`MacosProcessActionResult` without
            dispatching.
        """
        rtnval = MacosProcessActionResult(
            status=status,
            detail=detail,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=list(command),
            pid=pid,
            signal=signal,
        )
        return rtnval

    def _typed_config(self) -> MacosProcessManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosProcessManagerConfig`.
        """
        if isinstance(self._config, MacosProcessManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosProcessManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _parse_ps_output(*, stdout: str) -> list:
    """
        Parse the output of
        ``ps -A -o pid,ppid,user,stat,pri,ni,vsz,rss,%cpu,%mem,etime,
        comm,args``.

        BSD ``ps`` prints a header row by default.  We skip any row
        whose first token is not a non-negative integer to drop the
        header without needing a separate ``--no-headers`` flag (which
        BSD ``ps`` does not have anyway).
    """
    rtnval: list = []
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if len(line.strip()) == 0:
            continue

        tokens = line.split(None, 12)
        if len(tokens) < 12:
            continue

        pid_raw = tokens[0]
        try:
            pid_value = int(pid_raw)
        except ValueError:
            # Header row — first token is the literal "PID".
            continue

        ppid_raw = tokens[1]
        user = tokens[2]
        state = tokens[3]
        pri_raw = tokens[4]
        ni_raw = tokens[5]
        vsz_raw = tokens[6]
        rss_raw = tokens[7]
        pcpu_raw = tokens[8]
        pmem_raw = tokens[9]
        etime = tokens[10]
        comm = tokens[11]

        if len(tokens) >= 13:
            args_text = tokens[12]
        else:
            args_text = comm

        ppid_value = _safe_int(raw=ppid_raw)
        priority_value = _safe_int(raw=pri_raw)
        nice_value = _safe_int(raw=ni_raw)
        vsz_value = _safe_int(raw=vsz_raw)
        rss_value = _safe_int(raw=rss_raw)
        cpu_value = _safe_float(raw=pcpu_raw)
        mem_value = _safe_float(raw=pmem_raw)

        argv_list = args_text.split()

        record = MacosProcessInfo(
            pid=pid_value,
            ppid=ppid_value,
            user=user,
            state=state,
            priority=priority_value,
            nice=nice_value,
            vsz=vsz_value,
            rss=rss_value,
            cpu_percent=cpu_value,
            mem_percent=mem_value,
            elapsed=etime,
            command=comm,
            argv=argv_list,
        )
        rtnval.append(record)

    return rtnval


def _parse_pgrep_output(*, stdout: str) -> list:
    """
        Parse ``pgrep -l`` output.

        Each line has the shape ``<pid> <command>``.
    """
    rtnval: list = []
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if len(line.strip()) == 0:
            continue

        tokens = line.split(None, 1)
        if len(tokens) < 1:
            continue

        try:
            pid_value = int(tokens[0])
        except ValueError:
            continue

        if len(tokens) >= 2:
            command_value = tokens[1]
            argv_list = [command_value]
        else:
            command_value = None
            argv_list = []

        record = MacosProcessInfo(
            pid=pid_value,
            command=command_value,
            argv=argv_list,
        )
        rtnval.append(record)

    return rtnval


def _safe_int(*, raw: str) -> Optional[int]:
    """
        Coerce a ``ps`` token to ``int``, returning ``None`` when the
        token cannot be parsed.
    """
    try:
        rtnval = int(raw)
    except (TypeError, ValueError):
        rtnval = None
    return rtnval


def _safe_float(*, raw: str) -> Optional[float]:
    """
        Coerce a ``ps`` token to ``float``, returning ``None`` when the
        token cannot be parsed.
    """
    try:
        rtnval = float(raw)
    except (TypeError, ValueError):
        rtnval = None
    return rtnval


def _shell_escape_argv(*, argv: list) -> str:
    """
        Build a single shell-quoted command line from an argv list.
    """
    quoted_tokens: list = []
    for token in argv:
        text = str(token)
        escaped = text.replace("'", "'\\''")
        quoted_tokens.append("'{}'".format(escaped))

    rtnval = " ".join(quoted_tokens)
    return rtnval


def _best_effort_pid_lookup(
    *,
    manager: MacosProcessManager,
    executable: str,
) -> Optional[int]:
    """
        Re-query ``pgrep`` for ``executable`` and return the most-recent
        PID, if any.
    """
    try:
        records = manager.find_processes_by_name(name=executable)
    except MacosProcessQueryError:
        return None

    if len(records) == 0:
        return None

    last = records[-1]
    return last.pid
