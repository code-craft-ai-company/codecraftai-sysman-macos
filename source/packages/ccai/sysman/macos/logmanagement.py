"""
    macOS log-management feature.

    Implements the cross-OS log-management contract for macOS using
    ``log show`` / ``log stream`` for the unified logging system, plus
    ``newsyslog`` drop-ins under ``/etc/newsyslog.d/`` for plain-file
    rotation policies.

    File I/O policy:
        Every file read / write / probe / delete is performed through
        the injected :class:`ICommandRunner` (``cat``, ``tee``, ``rm``,
        ``mv``, ``test -e``, ``ls -1``, ``mkdir -p``).  This module does
        NOT consume an :class:`IFileOperator` — it follows the macOS
        pilot pattern of routing all I/O through the runner.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads and
        idempotency probes never elevate.  ``log show`` / ``log stream``
        on the unified subsystem do not require elevation for most
        public predicates.

    Surface choices:
        * ``get_log`` issues
          ``log show --predicate 'subsystem == "<name>"' --last <duration>
          --style ndjson`` and parses one JSON object per stdout line
          into a :class:`MacosLogEntry`.
        * ``tail_log`` is implemented as a generator that consumes the
          stdout of ``log stream --predicate 'subsystem == "<name>"'
          --style ndjson`` line-by-line.  Tests bound the iterator by
          seeding a finite ``stdout`` payload; in production the
          ``log stream`` process yields entries until the runner is
          closed.
        * ``rotate_log`` invokes
          ``newsyslog -F -f /etc/newsyslog.d/<name>.conf`` when a
          per-name drop-in exists, or falls back to ``newsyslog -F``
          (which forces rotation on the global config).
        * ``truncate_log`` cannot truncate the unified log on macOS
          (``log erase`` requires Recovery and is intentionally out of
          scope).  When a plain-file path is encoded in the
          configured drop-in, the verb dispatches ``truncate -s 0
          <path>``; otherwise it returns ``FAILED`` with a descriptive
          ``message``.
        * ``configure_rotation`` writes
          ``/etc/newsyslog.d/<name>.conf`` with the rendered policy
          via ``tee`` over stdin.  Idempotent: skipped when the
          existing on-disk content matches the rendered desired body.
        * ``list_logs`` enumerates loaded subsystems via
          ``log config --list`` plus the drop-ins under
          :attr:`MacosLogManagerConfig.newsyslog_d`.

    Limitations (documented in tasks.md follow-ups):
        * The ``log show`` / ``log stream`` predicate is currently
          fixed to ``subsystem == "<name>"``; richer predicates
          (process, category, eventMessage matching) are out of scope
          for the pilot.
        * ``log erase`` requires Recovery mode and is not exposed.
        * ``newsyslog`` size thresholds are kilobyte-only on macOS;
          the rendered ``size_kb`` field is wired through but callers
          must convert from bytes / megabytes themselves.
"""

import json
import os

from dataclasses import dataclass, field
from typing import Iterator, Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_LOG = "log"
_NEWSYSLOG = "newsyslog"
_TRUNCATE = "truncate"

_DEFAULT_NEWSYSLOG_D = "/etc/newsyslog.d"

# ``log show --last`` accepts duration tokens such as ``1h``, ``5m``,
# ``2d``.  The default window matches the cross-OS contract's
# "recent entries" semantics without reading the full unified store.
_DEFAULT_LOG_WINDOW = "1h"

# Source kinds reported on :class:`MacosLogInfo`.
_SOURCE_UNIFIED = "unified"
_SOURCE_NEWSYSLOG = "newsyslog"


MacosLogActionStatus = ActionStatus


@dataclass
class MacosLogManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosLogManager`.  Inherits ``dry_run``
        and ``elevated`` from :class:`FeatureConfig`.

        :param newsyslog_d: Directory holding newsyslog drop-ins.
                            Defaults to ``/etc/newsyslog.d``.
        :param default_window: ``log show --last`` duration token used
                               when the caller omits ``since``.
                               Defaults to ``"1h"``.
    """
    newsyslog_d: str = _DEFAULT_NEWSYSLOG_D
    default_window: str = _DEFAULT_LOG_WINDOW


@dataclass
class MacosLogActionResult(FeatureActionResult):
    """
        Result of a log-management write operation.

        :param name: The log source name targeted.
        :param path: The file path the action targeted, when
                     applicable (newsyslog drop-in path or plain-file
                     log path).
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes (notably the
                        unified-log-not-truncatable explanation on
                        :meth:`MacosLogManager.truncate_log`).
    """
    name: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosLogEntry:
    """
        Single parsed entry from ``log show --style ndjson``.

        :param timestamp: ISO-8601 timestamp string from the
                          ``timestamp`` field; ``None`` when absent.
        :param process: Originating process name from
                        ``processImagePath`` (basename) or
                        ``process``; ``None`` when absent.
        :param subsystem: ``subsystem`` field; ``None`` when absent.
        :param message: ``eventMessage`` field; empty string when
                        absent.
    """
    timestamp: Optional[str]
    process: Optional[str]
    subsystem: Optional[str]
    message: str


@dataclass
class MacosLogEntries:
    """
        Wrapper around a list of :class:`MacosLogEntry` plus query
        metadata.

        :param source: The log source identifier the query targeted
                       (typically the subsystem name).
        :param entries: The parsed entries in the order they were
                        emitted by ``log show``.
        :param total_count: ``len(entries)`` recorded explicitly for
                            convenience and to match the contract's
                            metadata shape.
        :param raw: Raw stdout lines preserved for debuggability —
                    populated only when at least one line failed to
                    parse.
    """
    source: str
    entries: list[MacosLogEntry]
    total_count: int
    raw: list[str] = field(default_factory=list)


@dataclass
class MacosLogInfo:
    """
        One row from :meth:`MacosLogManager.list_logs`.

        :param name: Log source name.  For unified subsystems this is
                     the reverse-DNS subsystem identifier
                     (e.g. ``com.apple.network``); for newsyslog
                     drop-ins it is the filename stem under
                     :attr:`MacosLogManagerConfig.newsyslog_d`.
        :param source: ``"unified"`` for subsystems reported by
                       ``log config --list``; ``"newsyslog"`` for
                       drop-in files.
        :param path: For ``"newsyslog"`` rows, the absolute drop-in
                     path; ``None`` for ``"unified"`` rows.
    """
    name: str
    source: str
    path: Optional[str] = None


@dataclass
class MacosRotationPolicy:
    """
        Rotation policy rendered into a newsyslog drop-in.

        :param frequency: ``"daily"`` / ``"weekly"`` / ``"monthly"`` —
                          interpreted as the rotation cadence and
                          mapped to the newsyslog ``when`` column
                          (``$D0``, ``$W0D0``, ``$M1D0`` respectively).
        :param rotate_count: Number of rotated copies to keep.  Maps
                             to the newsyslog ``count`` column.
        :param compress: When True, sets the ``Z`` flag in the
                         newsyslog ``flags`` column.
        :param size_kb: Optional size threshold in kilobytes (the only
                        unit newsyslog accepts).  ``None`` writes an
                        ``*`` to the ``size`` column.
        :param mode: File-mode column (octal string, e.g. ``"640"``).
        :param owner: Owner column (``user:group``); defaults to
                      ``"root:wheel"``.
        :param log_path: The full path of the log file the policy
                         targets.  When ``None``, the renderer
                         defaults to ``/var/log/<name>.log``.
    """
    frequency: str
    rotate_count: int = 7
    compress: bool = True
    size_kb: Optional[int] = None
    mode: str = "640"
    owner: str = "root:wheel"
    log_path: Optional[str] = None


class MacosLogQueryError(Exception):
    """
        Raised when a log read fails for a reason other than the
        source simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Log query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosLogManager(BaseFeatureManager):
    """
        macOS log manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner`; file I/O for newsyslog
        drop-ins is performed via ``cat`` / ``tee`` / ``rm`` /
        ``test -e`` / ``ls -1`` rather than a separate file operator.
    """

    result_class = MacosLogActionResult
    status_class = MacosLogActionStatus
    feature_name = "log-management"

    def __init__(
        self,
        runner,
        config: Optional[MacosLogManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           operation.
            :param config: Manager configuration.  Defaults to a fresh
                           :class:`MacosLogManagerConfig` when omitted.
        """
        if config is None:
            config = MacosLogManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_log(
        self,
        name: str,
        lines: int = 100,
        since: Optional[str] = None,
    ) -> MacosLogEntries:
        """
            Retrieve recent unified-log entries for a subsystem.

            Runs ``log show --predicate 'subsystem == "<name>"' --last
            <duration> --style ndjson`` and parses one JSON object per
            stdout line.  When ``since`` is provided it overrides the
            default window; otherwise
            :attr:`MacosLogManagerConfig.default_window` is used.
            Reads never elevate.

            :param name: The unified-log subsystem identifier.
            :param lines: Maximum number of entries to return.  The
                          ``log show`` command does not natively cap
                          line count, so this parameter trims the
                          parsed list in pure Python.  Defaults to
                          100.
            :param since: Optional ISO-8601 ``--start`` timestamp.
                          When provided, replaces the default
                          ``--last`` duration window.

            :returns: The :class:`MacosLogEntries` describing the
                      result.
            :raises MacosLogQueryError: When ``log show`` exits
                                        non-zero with a non-empty
                                        stderr.
        """
        predicate = self._build_subsystem_predicate(name=name)
        command: list[str] = [_LOG, "show", "--predicate", predicate]

        if since is not None and len(since) > 0:
            command.append("--start")
            command.append(since)
        else:
            command.append("--last")
            command.append(self._typed_config().default_window)

        command.append("--style")
        command.append("ndjson")

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise MacosLogQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        entries, raw = _parse_log_ndjson(text=run_result.stdout)
        if lines is not None and lines >= 0 and len(entries) > lines:
            trimmed = entries[-lines:]
        else:
            trimmed = entries

        rtnval = MacosLogEntries(
            source=name,
            entries=trimmed,
            total_count=len(trimmed),
            raw=raw,
        )
        return rtnval

    def tail_log(
        self,
        name: str,
        follow: bool = True,
    ) -> Iterator[MacosLogEntry]:
        """
            Stream unified-log entries for a subsystem.

            Runs ``log stream --predicate 'subsystem == "<name>"'
            --style ndjson`` and yields one :class:`MacosLogEntry`
            per JSON line emitted by the runner.  Tests bound the
            iterator by seeding a finite ``stdout`` payload; in
            production the ``log stream`` process yields entries
            until the runner is closed.

            ``follow`` is accepted for cross-OS API parity.  ``log
            stream`` always streams.  When ``follow=False`` the
            backlog is yielded without follow semantics; this matches
            the systemd-style contract surface.

            :param name: The unified-log subsystem identifier.
            :param follow: When True, the runner is expected to keep
                           the ``log stream`` process attached.  When
                           False, the same command is dispatched but
                           the iterator terminates when stdout is
                           drained.

            :returns: A generator of :class:`MacosLogEntry`.  Never
                      elevates.
        """
        predicate = self._build_subsystem_predicate(name=name)
        command: list[str] = [_LOG, "stream", "--predicate", predicate]
        command.append("--style")
        command.append("ndjson")

        # ``follow`` does not change the argv on macOS — ``log stream``
        # always streams.  We accept the parameter to honour the
        # cross-OS signature; production callers can interrupt the
        # runner to terminate the stream when ``follow=True``.
        _ = follow

        run_result = self._runner.run(list(command), elevated=False)

        for raw_line in run_result.stdout.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            entry = _parse_one_log_line(line=stripped)
            if entry is not None:
                yield entry

        return

    def list_logs(self) -> list[MacosLogInfo]:
        """
            Enumerate available log sources.

            Combines ``log config --list`` (each subsystem becomes a
            ``"unified"`` source) with an ``ls -1 <newsyslog_d>``
            listing of every file under the configured newsyslog
            drop-in directory (each becomes a ``"newsyslog"`` source).
            Reads never elevate.

            :returns: One :class:`MacosLogInfo` per discovered source.
        """
        rtnval: list[MacosLogInfo] = []

        config_command = [_LOG, "config", "--list"]
        config_result = self._runner.run(
            list(config_command), elevated=False,
        )
        if config_result.exit_code == 0:
            for raw_line in config_result.stdout.splitlines():
                line = raw_line.strip()
                if len(line) == 0:
                    continue
                # ``log config --list`` emits one "subsystem: <id>"
                # row per known subsystem; when the prefix is absent
                # the whole line is treated as the subsystem id.
                if line.lower().startswith("subsystem:"):
                    _, _, value = line.partition(":")
                    subsystem_id = value.strip()
                else:
                    subsystem_id = line
                if len(subsystem_id) == 0:
                    continue
                info = MacosLogInfo(
                    name=subsystem_id,
                    source=_SOURCE_UNIFIED,
                    path=None,
                )
                rtnval.append(info)

        newsyslog_d = self._typed_config().newsyslog_d
        ls_command = ["ls", "-1", newsyslog_d]
        ls_result = self._runner.run(list(ls_command), elevated=False)
        if ls_result.exit_code == 0:
            for raw_entry in ls_result.stdout.splitlines():
                entry = raw_entry.strip()
                if len(entry) == 0:
                    continue
                if entry.endswith(".conf"):
                    base_name = entry[: -len(".conf")]
                else:
                    base_name = entry
                full_path = "{}/{}".format(newsyslog_d, entry)
                info = MacosLogInfo(
                    name=base_name,
                    source=_SOURCE_NEWSYSLOG,
                    path=full_path,
                )
                rtnval.append(info)

        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def rotate_log(self, name: str) -> MacosLogActionResult:
        """
            Force-rotate a log via ``newsyslog``.

            Uses ``newsyslog -F -f <newsyslog_d>/<name>.conf`` when the
            per-name drop-in exists; falls back to ``newsyslog -F``
            (which forces rotation against the global newsyslog
            config) otherwise.  No idempotency guard: rotation is
            always attempted in non-dry-run mode.

            :param name: The log source name (drop-in stem).

            :returns: The :class:`MacosLogActionResult`.
        """
        drop_in_path = self._dropin_path(name=name)

        if self._path_exists(path=drop_in_path):
            target_path = drop_in_path
            command = [_NEWSYSLOG, "-F", "-f", target_path]
        else:
            target_path = None
            command = [_NEWSYSLOG, "-F"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosLogActionStatus.DRY_RUN,
                name=name,
                path=target_path,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosLogActionStatus.SUCCESS
        else:
            mapped_status = MacosLogActionStatus.FAILED

        rtnval = MacosLogActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            path=target_path,
        )
        return rtnval

    def truncate_log(self, name: str) -> MacosLogActionResult:
        """
            Truncate a plain-file log to zero bytes.

            macOS unified logging cannot be truncated by an
            unprivileged or elevated process: ``log erase`` requires
            Recovery mode.  Therefore this verb only supports the
            plain-file path.  When a per-name newsyslog drop-in
            exists, the policy's first column (the log path) is
            extracted and ``truncate -s 0 <path>`` is dispatched
            against it.  When no drop-in exists, the verb returns
            ``FAILED`` with a descriptive ``message``.

            Idempotency: when the file is already 0 bytes (probed via
            ``ls -1`` against an empty file) the verb returns
            ``SKIPPED``.  The probe is best-effort — failure to read
            the size proceeds with the truncate dispatch.

            :param name: The log source name (drop-in stem).

            :returns: The :class:`MacosLogActionResult`.
        """
        drop_in_path = self._dropin_path(name=name)

        if not self._path_exists(path=drop_in_path):
            failed_message = (
                "macOS unified logging cannot be truncated; no newsyslog "
                "drop-in exists for '{}'.".format(name)
            )
            failed = self._build_result(
                status=MacosLogActionStatus.FAILED,
                name=name,
                path=None,
                command=[_TRUNCATE, "-s", "0"],
                message=failed_message,
                stderr="unified_log_not_truncatable",
            )
            return failed

        existing_body = self._read_file(path=drop_in_path)
        if existing_body is None:
            log_path: Optional[str] = None
        else:
            log_path = _extract_log_path_from_newsyslog(
                body=existing_body,
            )

        if log_path is None:
            failed_message = (
                "Could not extract log path from {}; truncate is "
                "unsupported.".format(drop_in_path)
            )
            failed = self._build_result(
                status=MacosLogActionStatus.FAILED,
                name=name,
                path=drop_in_path,
                command=[_TRUNCATE, "-s", "0"],
                message=failed_message,
                stderr="newsyslog_drop_in_unparseable",
            )
            return failed

        command = [_TRUNCATE, "-s", "0", log_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosLogActionStatus.DRY_RUN,
                name=name,
                path=log_path,
                command=command,
            )
            return dry

        if self._file_is_empty(path=log_path):
            skipped = self._build_result(
                status=MacosLogActionStatus.SKIPPED,
                name=name,
                path=log_path,
                command=command,
                message="log file is already empty",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosLogActionStatus.SUCCESS
        else:
            mapped_status = MacosLogActionStatus.FAILED

        rtnval = MacosLogActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            path=log_path,
        )
        return rtnval

    def configure_rotation(
        self,
        name: str,
        policy: MacosRotationPolicy,
    ) -> MacosLogActionResult:
        """
            Write a newsyslog drop-in for ``name`` containing the
            rendered ``policy``.

            Skipped when the existing drop-in's content matches the
            rendered desired body byte-for-byte.

            :param name: The log source name (drop-in stem).
            :param policy: The :class:`MacosRotationPolicy` to render.

            :returns: The :class:`MacosLogActionResult`.
        """
        path = self._dropin_path(name=name)
        desired = _render_newsyslog(name=name, policy=policy)

        write_command = ["tee", path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosLogActionStatus.DRY_RUN,
                name=name,
                path=path,
                command=write_command,
            )
            return dry

        existing = self._read_file(path=path)
        if existing is not None and existing == desired:
            skipped = self._build_result(
                status=MacosLogActionStatus.SKIPPED,
                name=name,
                path=path,
                command=write_command,
                message="rotation policy already matches desired content",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosLogActionStatus.SUCCESS
        else:
            mapped_status = MacosLogActionStatus.FAILED

        rtnval = MacosLogActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            name=name,
            path=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosLogManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosLogManagerConfig`.

            :returns: The active config typed as
                      :class:`MacosLogManagerConfig`.
        """
        if isinstance(self._config, MacosLogManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosLogManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_subsystem_predicate(self, *, name: str) -> str:
        """
            Build the ``log show`` / ``log stream`` predicate for the
            given subsystem name.

            :param name: Subsystem identifier.

            :returns: The predicate string ``subsystem == "<name>"``.
        """
        rtnval = 'subsystem == "{}"'.format(name)
        return rtnval

    def _dropin_path(self, *, name: str) -> str:
        """
            Build the absolute path to the newsyslog drop-in for
            ``name``.

            :param name: The log source name (drop-in stem).

            :returns: ``<newsyslog_d>/<name>.conf``.
        """
        newsyslog_d = self._typed_config().newsyslog_d
        rtnval = "{}/{}.conf".format(newsyslog_d, name)
        return rtnval

    def _path_exists(self, *, path: str) -> bool:
        """
            Probe a path's existence via ``test -e``.

            :param path: The absolute path to probe.

            :returns: True when ``test -e`` exits zero; False
                      otherwise.
        """
        command = ["test", "-e", path]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when the
                      file is missing or unreadable.
        """
        command = ["cat", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _file_is_empty(self, *, path: str) -> bool:
        """
            Best-effort idempotency probe: report whether ``path`` is
            an empty file.

            Uses ``cat <path>`` and treats an empty stdout as
            zero-length.  Errors (missing file, non-zero exit) are
            reported as not-empty so the caller falls through to the
            mutating dispatch and surfaces the real error.

            :param path: The absolute path to probe.

            :returns: True when the probed file appears empty.
        """
        body = self._read_file(path=path)
        if body is None:
            rtnval = False
            return rtnval
        rtnval = len(body) == 0
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosLogActionStatus,
        name: Optional[str],
        path: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosLogActionResult:
        """
            Construct a :class:`MacosLogActionResult` without invoking
            the runner.

            :param status: The outcome status.
            :param name: The log source name to echo onto the result.
            :param path: The file path to echo onto the result.
            :param command: The would-have-been argv (copied for
                            capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`MacosLogActionResult`.
        """
        rtnval = MacosLogActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            path=path,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _frequency_to_when(*, frequency: str) -> str:
    """
        Map a :class:`MacosRotationPolicy.frequency` string to a
        newsyslog ``when`` token.

        ``daily``   -> ``$D0``    (rotate at midnight, every day)
        ``weekly``  -> ``$W0D0``  (rotate every Sunday at midnight)
        ``monthly`` -> ``$M1D0``  (rotate on the first of the month at
                                   midnight)
        Anything else is passed through verbatim, allowing callers to
        supply a literal newsyslog ``when`` token directly.

        :param frequency: The frequency string.

        :returns: The matching ``when`` token.
    """
    lowered = frequency.strip().lower()
    if lowered == "daily":
        rtnval = "$D0"
    elif lowered == "weekly":
        rtnval = "$W0D0"
    elif lowered == "monthly":
        rtnval = "$M1D0"
    else:
        rtnval = frequency.strip()
    return rtnval


def _render_newsyslog(
    *,
    name: str,
    policy: MacosRotationPolicy,
) -> str:
    """
        Render a :class:`MacosRotationPolicy` into a newsyslog drop-in
        body.

        Output is byte-stable for matching inputs so that
        :meth:`MacosLogManager.configure_rotation` can compare against
        the existing file content for idempotency.

        Newsyslog row shape (per ``newsyslog.conf(5)``)::

            <logfile> <owner:group> <mode> <count> <size> <when> <flags>

        :param name: The log source name.  Used to default the log
                     file path when ``policy.log_path`` is ``None``.
        :param policy: The :class:`MacosRotationPolicy` to render.

        :returns: The full file body as a string ending in ``\\n``.
    """
    if policy.log_path is not None and len(policy.log_path) > 0:
        log_path = policy.log_path
    else:
        log_path = "/var/log/{}.log".format(name)

    if policy.size_kb is not None and policy.size_kb > 0:
        size_token = str(policy.size_kb)
    else:
        size_token = "*"

    when_token = _frequency_to_when(frequency=policy.frequency)

    if policy.compress:
        flags_token = "Z"
    else:
        flags_token = "-"

    header_lines = [
        "# Generated by ccai MacosLogManager",
        "# Source: {}".format(name),
    ]
    row = "{} {} {} {} {} {} {}".format(
        log_path,
        policy.owner,
        policy.mode,
        policy.rotate_count,
        size_token,
        when_token,
        flags_token,
    )

    body_lines = list(header_lines)
    body_lines.append(row)
    rtnval = "\n".join(body_lines) + "\n"
    return rtnval


def _extract_log_path_from_newsyslog(*, body: str) -> Optional[str]:
    """
        Extract the log-file path from a newsyslog drop-in body.

        Walks every non-comment, non-blank line and returns the first
        token of the first parseable row.  Comments (``#`` prefix) and
        blank lines are skipped.

        :param body: Raw newsyslog drop-in content.

        :returns: The log-file path, or ``None`` when no parseable
                  row is found.
    """
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if len(tokens) == 0:
            continue
        first_token = tokens[0]
        if len(first_token) == 0:
            continue
        rtnval: Optional[str] = first_token
        return rtnval

    rtnval = None
    return rtnval


def _parse_log_ndjson(*, text: str) -> tuple:
    """
        Parse the line-oriented JSON output of ``log show --style
        ndjson``.

        Walks every non-empty line; lines that fail to decode as a
        JSON object are appended to a ``raw`` collector and skipped
        from the parsed entries.

        :param text: Raw stdout text.

        :returns: A two-tuple ``(entries, raw)`` where ``entries`` is
                  a list of :class:`MacosLogEntry` and ``raw`` is the
                  list of unparseable lines.
    """
    entries: list[MacosLogEntry] = []
    raw: list[str] = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue

        entry = _parse_one_log_line(line=stripped)
        if entry is None:
            raw.append(stripped)
            continue
        entries.append(entry)

    rtnval = (entries, raw)
    return rtnval


def _parse_one_log_line(*, line: str) -> Optional[MacosLogEntry]:
    """
        Parse a single ``log show --style ndjson`` line into a
        :class:`MacosLogEntry`.

        :param line: One JSON line.

        :returns: The :class:`MacosLogEntry`, or ``None`` when the
                  line cannot be decoded as a JSON object.
    """
    try:
        decoded = json.loads(line)
    except (ValueError, TypeError):
        rtnval: Optional[MacosLogEntry] = None
        return rtnval

    if not isinstance(decoded, dict):
        rtnval = None
        return rtnval

    timestamp_raw = decoded.get("timestamp", None)
    if isinstance(timestamp_raw, str) and len(timestamp_raw) > 0:
        timestamp: Optional[str] = timestamp_raw
    else:
        timestamp = None

    process_path = decoded.get("processImagePath", None)
    if isinstance(process_path, str) and len(process_path) > 0:
        # Reduce a full path to its basename for caller convenience.
        process: Optional[str] = process_path.rsplit("/", 1)[-1]
    else:
        process_field = decoded.get("process", None)
        if isinstance(process_field, str) and len(process_field) > 0:
            process = process_field
        else:
            process = None

    subsystem_raw = decoded.get("subsystem", None)
    if isinstance(subsystem_raw, str) and len(subsystem_raw) > 0:
        subsystem: Optional[str] = subsystem_raw
    else:
        subsystem = None

    message_raw = decoded.get("eventMessage", "")
    if isinstance(message_raw, str):
        message = message_raw
    else:
        message = str(message_raw)

    rtnval = MacosLogEntry(
        timestamp=timestamp,
        process=process,
        subsystem=subsystem,
        message=message,
    )
    return rtnval
