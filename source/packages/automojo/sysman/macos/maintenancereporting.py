"""
    macOS maintenance reporter.

    :class:`MacosMaintenanceReporter` collects action and query
    records produced by the other macOS sysman features, computes a
    summary, and emits a report (JSON, YAML, Markdown, or plain text)
    to disk.  All file writes go through an
    :class:`automojo.interfaces.icommandrunner.ICommandRunner` so that
    the reporter does not need a dedicated file-operator dependency
    (per the batch contract caveat -- file I/O via ``mkdir -p`` and
    ``tee`` is acceptable for small reports).

    The macOS default output base is
    ``/var/log/automojo/maintenance/<session_id>/`` -- shared with
    Ubuntu since both are Unix systems.

    The module is pure Python (no subprocess calls of its own) except
    for :meth:`emit_report`, which delegates to the runner.  All read
    helpers (:meth:`summary_counts`, :meth:`actions_by_status`,
    :meth:`actions_by_feature`, :meth:`to_json`, :meth:`to_text`,
    :meth:`to_markdown`, :meth:`to_yaml`) are I/O free.
"""

import json
import os
import socket
import traceback

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from automojo.interfaces.icommandrunner import ICommandRunner


__all__ = [
    "MacosActionRecord",
    "MacosActionStatus",
    "MacosMaintenanceReport",
    "MacosMaintenanceReporter",
    "MacosMaintenanceReporterConfig",
    "MacosMaintenanceSession",
    "MacosReportFormat",
    "MacosReportSummary",
    "MacosReportWriteResult",
]


_DEFAULT_OUTPUT_BASE = "/var/log/automojo/maintenance"


class MacosActionStatus(Enum):
    """
        Outcome categories recognised by the report writer.
    """
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


class MacosReportFormat(Enum):
    """
        Supported output formats.  ``JSON`` is canonical; ``YAML`` and
        ``MARKDOWN`` are best-effort renderings derived from the same
        underlying dict.
    """
    JSON     = "json"
    TEXT     = "text"
    YAML     = "yaml"
    MARKDOWN = "markdown"


@dataclass
class MacosActionRecord:
    """
        One recorded action or query within a maintenance session.

        :param feature: Feature name (e.g. ``"package-management"``).
        :param operation: Short name of the operation performed.
        :param status: Outcome status.
        :param detail: Free-form human-readable description.
        :param error: Error message captured on failure.
        :param duration_seconds: Wall-clock duration in seconds.
        :param timestamp: Event timestamp; auto-stamped at construction
                          when ``None`` is supplied.
    """
    feature: str
    operation: str
    status: MacosActionStatus
    detail: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: Optional[float] = None
    timestamp: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)
        return


@dataclass
class MacosReportSummary:
    """
        Counters tallied by status across a session.
    """
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    dry_run: int = 0


@dataclass
class MacosReportWriteResult:
    """
        Outcome of :meth:`MacosMaintenanceReporter.emit_report`.

        :param success: True when the report was written or skipped in
                        dry-run mode.
        :param path: Path of the resulting report file (or the path
                     that would have been used in dry-run).
        :param dry_run: True when the call was a dry-run.
        :param error: Error message when ``success`` is False.
    """
    success: bool
    path: str
    dry_run: bool = False
    error: Optional[str] = None


@dataclass
class MacosMaintenanceSession:
    """
        Metadata describing a single maintenance session.

        :param run_id: Unique identifier for the run.
        :param host: Hostname or system identifier.
        :param description: Optional free-form description.
        :param started_at: UTC start timestamp.
        :param ended_at: UTC end timestamp; ``None`` until
                         :meth:`MacosMaintenanceReporter.end_session`.
        :param status: Aggregated status; ``None`` until ``end_session``.
        :param metadata: Free-form caller-supplied bag.
    """
    run_id: str
    host: str
    description: Optional[str]
    started_at: datetime
    ended_at: Optional[datetime] = None
    status: Optional[MacosActionStatus] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MacosMaintenanceReport:
    """
        Snapshot of a session in serialisable form, used internally by
        the reporter to assemble payloads.

        :param session: The session metadata.
        :param actions: The action records.
        :param queries: The query records.
        :param errors: Recorded error events.
        :param counts: Summary counters keyed by status name.
    """
    session: MacosMaintenanceSession
    actions: list[MacosActionRecord] = field(default_factory=list)
    queries: list[MacosActionRecord] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class MacosMaintenanceReporterConfig:
    """
        Configuration for :class:`MacosMaintenanceReporter`.

        :param dry_run: When True, ``emit_report`` reports success
                        without invoking the runner.
        :param output_dir: Base directory for default session paths.
        :param default_format: Default emit format.
        :param json_indent: Indent level used by JSON serialisation.
    """
    dry_run: bool = False
    output_dir: str = _DEFAULT_OUTPUT_BASE
    default_format: MacosReportFormat = MacosReportFormat.JSON
    json_indent: int = 2


class MacosMaintenanceReporter:
    """
        Aggregator for macOS sysman action / query / error events
        with a single file-emit egress.
    """

    def __init__(
        self,
        runner: ICommandRunner,
        config: Optional[MacosMaintenanceReporterConfig] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        """
            :param runner: Command runner used by :meth:`emit_report`
                           to create directories and write files.
            :param config: Reporter configuration.  Defaults are used
                           when ``None`` is supplied.
            :param clock: Optional callable returning ``datetime`` used
                          to stamp session and record times.  Defaults
                          to :func:`datetime.now` with UTC.
        """
        if config is None:
            config = MacosMaintenanceReporterConfig()
        if clock is None:
            clock = _utc_now

        self._runner: ICommandRunner = runner
        self._config: MacosMaintenanceReporterConfig = config
        self._clock: Callable[[], datetime] = clock
        self._session: Optional[MacosMaintenanceSession] = None
        self._actions: list[MacosActionRecord] = []
        self._queries: list[MacosActionRecord] = []
        self._errors: list[dict[str, Any]] = []
        return

    @property
    def runner(self) -> ICommandRunner:
        """
            The injected command runner.
        """
        return self._runner

    @property
    def config(self) -> MacosMaintenanceReporterConfig:
        """
            The active :class:`MacosMaintenanceReporterConfig`.
        """
        return self._config

    @property
    def session(self) -> Optional[MacosMaintenanceSession]:
        """
            The currently active session, or ``None``.
        """
        return self._session

    @property
    def actions(self) -> list[MacosActionRecord]:
        """
            Snapshot of recorded action records in insertion order.
        """
        rtnval = list(self._actions)
        return rtnval

    @property
    def queries(self) -> list[MacosActionRecord]:
        """
            Snapshot of recorded query records in insertion order.
        """
        rtnval = list(self._queries)
        return rtnval

    @property
    def errors(self) -> list[dict[str, Any]]:
        """
            Snapshot of recorded error events in insertion order.
        """
        rtnval = list(self._errors)
        return rtnval

    def begin_session(
        self,
        session_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> MacosMaintenanceSession:
        """
            Start a new session, replacing any prior session state.

            :param session_id: Unique identifier for the run.  Empty
                               strings are rejected.
            :param metadata: Optional free-form metadata.

            :returns: The newly created
                      :class:`MacosMaintenanceSession`.
            :raises: ValueError when ``session_id`` is empty.
        """
        if session_id is None or len(session_id) == 0:
            raise ValueError("session_id must be a non-empty string")

        try:
            host = socket.gethostname()
        except Exception:
            host = "unknown"

        if metadata is None:
            session_metadata: dict[str, Any] = {}
        else:
            session_metadata = dict(metadata)

        description = None
        if "description" in session_metadata:
            description = session_metadata["description"]

        new_session = MacosMaintenanceSession(
            run_id=session_id,
            host=host,
            description=description,
            started_at=self._clock(),
            ended_at=None,
            status=None,
            metadata=session_metadata,
        )

        self._session = new_session
        self._actions = []
        self._queries = []
        self._errors = []
        return new_session

    def record_action(self, result: Any) -> MacosActionRecord:
        """
            Record an action result.  Accepts either an existing
            :class:`MacosActionRecord` or any object that can be
            adapted via :func:`_coerce_record`.

            :param result: The action-result dataclass instance.

            :returns: The stored :class:`MacosActionRecord`.
            :raises: RuntimeError when no session is active.
        """
        self._require_session(operation="record_action")
        record = _coerce_record(value=result, default_status=MacosActionStatus.SUCCESS)
        if record.timestamp is None:
            record.timestamp = self._clock()
        self._actions.append(record)
        return record

    def record_query(self, result: Any) -> MacosActionRecord:
        """
            Record a query result.  Identical mutation path to
            :meth:`record_action`, but stored on the queries list.

            :param result: The query-result dataclass instance.

            :returns: The stored :class:`MacosActionRecord`.
            :raises: RuntimeError when no session is active.
        """
        self._require_session(operation="record_query")
        record = _coerce_record(value=result, default_status=MacosActionStatus.SUCCESS)
        if record.timestamp is None:
            record.timestamp = self._clock()
        self._queries.append(record)
        return record

    def record_error(
        self,
        error: Exception,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
            Record an exception with its traceback and an optional
            context dict.

            :param error: The exception to record.
            :param context: Optional caller-supplied context.

            :returns: The stored error event dict.
            :raises: RuntimeError when no session is active.
        """
        self._require_session(operation="record_error")

        if context is None:
            stored_context: dict[str, Any] = {}
        else:
            stored_context = dict(context)

        traceback_text = "".join(
            traceback.format_exception(type(error), error, error.__traceback__),
        )

        feature = stored_context.get("feature", "unknown")
        operation = stored_context.get("operation", "unknown")

        error_event: dict[str, Any] = {
            "feature": feature,
            "operation": operation,
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback_text,
            "context": stored_context,
            "timestamp": self._clock().isoformat(),
        }
        self._errors.append(error_event)

        failure_record = MacosActionRecord(
            feature=feature,
            operation=operation,
            status=MacosActionStatus.FAILED,
            detail=stored_context.get("detail"),
            error=str(error),
            timestamp=self._clock(),
        )
        self._actions.append(failure_record)
        return error_event

    def end_session(
        self,
        status: Optional[str] = None,
    ) -> MacosMaintenanceSession:
        """
            Finalise the active session.

            :param status: Optional explicit status string.  When
                           ``None``, the status is computed from the
                           recorded actions.

            :returns: The finalised
                      :class:`MacosMaintenanceSession`.
            :raises: RuntimeError when no session is active.
        """
        self._require_session(operation="end_session")

        active_session = self._session
        if active_session is None:
            raise RuntimeError("end_session called without active session")

        active_session.ended_at = self._clock()
        if status is None:
            active_session.status = self._compute_overall_status()
        else:
            active_session.status = self._coerce_status_string(value=status)
        return active_session

    def summary_counts(self) -> MacosReportSummary:
        """
            Tally action records by status.
        """
        summary = MacosReportSummary(total=len(self._actions))
        for record in self._actions:
            if record.status == MacosActionStatus.SUCCESS:
                summary.succeeded = summary.succeeded + 1
            elif record.status == MacosActionStatus.FAILED:
                summary.failed = summary.failed + 1
            elif record.status == MacosActionStatus.SKIPPED:
                summary.skipped = summary.skipped + 1
            elif record.status == MacosActionStatus.DRY_RUN:
                summary.dry_run = summary.dry_run + 1
        return summary

    def actions_by_status(
        self,
        *statuses: MacosActionStatus,
    ) -> list[MacosActionRecord]:
        """
            Return action records whose status is in ``statuses``.
        """
        if len(statuses) == 0:
            empty_list: list[MacosActionRecord] = []
            return empty_list
        target = set(statuses)
        rtnval: list[MacosActionRecord] = []
        for record in self._actions:
            if record.status in target:
                rtnval.append(record)
        return rtnval

    def actions_by_feature(self, feature: str) -> list[MacosActionRecord]:
        """
            Return action records emitted by a specific feature.
        """
        rtnval: list[MacosActionRecord] = []
        for record in self._actions:
            if record.feature == feature:
                rtnval.append(record)
        return rtnval

    def default_session_path(self, session_id: str) -> str:
        """
            Compute the default per-session output directory.

            :param session_id: Session identifier.

            :returns: ``<output_dir>/<session_id>/``.
            :raises: ValueError when ``session_id`` is empty.
        """
        if session_id is None or len(session_id) == 0:
            raise ValueError("session_id must be a non-empty string")
        rtnval = "{}/{}/".format(self._config.output_dir.rstrip("/"), session_id)
        return rtnval

    def to_json(self, indent: Optional[int] = None) -> str:
        """
            Serialise the current report state to a JSON string.
        """
        if indent is None:
            indent_value = self._config.json_indent
        else:
            indent_value = indent
        payload = self._to_dict()
        rendered = json.dumps(payload, indent=indent_value, default=str)
        return rendered

    def to_text(self) -> str:
        """
            Serialise the current report state to plain text.
        """
        active_session = self._session

        lines: list[str] = []
        lines.append("=" * 51)
        lines.append("MACOS MAINTENANCE REPORT")
        lines.append("=" * 51)
        if active_session is not None:
            lines.append("Run ID    : {}".format(active_session.run_id))
            lines.append("Host      : {}".format(active_session.host))
            description = active_session.description
            if description is None:
                description = "(none)"
            lines.append("Desc      : {}".format(description))
            lines.append("Started   : {}".format(active_session.started_at.isoformat()))
            ended_at = active_session.ended_at
            if ended_at is None:
                ended_label = "in progress"
            else:
                ended_label = ended_at.isoformat()
            lines.append("Finished  : {}".format(ended_label))
            status = active_session.status
            if status is None:
                status_label = "in progress"
            else:
                status_label = status.value
            lines.append("Status    : {}".format(status_label))
        else:
            lines.append("(no active session)")

        summary = self.summary_counts()
        lines.append("")
        lines.append(
            "Summary   : {} total  {} ok  {} failed  {} skipped  {} dry-run".format(
                summary.total,
                summary.succeeded,
                summary.failed,
                summary.skipped,
                summary.dry_run,
            )
        )
        lines.append("-" * 51)
        lines.append("Actions")
        lines.append("-" * 51)
        if len(self._actions) == 0:
            lines.append("(no actions recorded)")
        else:
            for record in self._actions:
                lines.append(
                    "[{:8s}] {} / {}".format(
                        record.status.value.upper(),
                        record.feature,
                        record.operation,
                    )
                )
                if record.detail is not None:
                    lines.append("           detail: {}".format(record.detail))
                if record.error is not None:
                    lines.append("           Error: {}".format(record.error))

        body = "\n".join(lines) + "\n"
        return body

    def to_yaml(self) -> str:
        """
            Serialise the current report state to YAML.

            When PyYAML is not importable, returns a JSON dump (which
            is valid YAML for the data shapes produced by this
            module).  This keeps the surface lossless without adding a
            runtime dependency.
        """
        payload = self._to_dict()
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            fallback = json.dumps(payload, indent=2, default=str)
            return fallback

        rendered = yaml.safe_dump(  # type: ignore[no-any-return]
            payload,
            default_flow_style=False,
            sort_keys=False,
        )
        return rendered

    def to_markdown(self) -> str:
        """
            Serialise the current report state to Markdown.
        """
        active_session = self._session
        summary = self.summary_counts()

        lines: list[str] = []
        lines.append("# macOS Maintenance Report")
        lines.append("")

        lines.append("| Field | Value |")
        lines.append("|---|---|")
        if active_session is not None:
            lines.append("| Run ID | {} |".format(active_session.run_id))
            lines.append("| Host | {} |".format(active_session.host))
            description = active_session.description
            if description is None:
                description = "(none)"
            lines.append("| Description | {} |".format(description))
            lines.append("| Started | {} |".format(active_session.started_at.isoformat()))
            ended_at = active_session.ended_at
            if ended_at is None:
                ended_label = "in progress"
            else:
                ended_label = ended_at.isoformat()
            lines.append("| Finished | {} |".format(ended_label))
            status = active_session.status
            if status is None:
                status_label = "in progress"
            else:
                status_label = status.value
            lines.append("| Status | {} |".format(status_label))
        else:
            lines.append("| Run ID | (no active session) |")
        lines.append("")

        lines.append("## Summary")
        lines.append("")
        lines.append("| Total | Succeeded | Failed | Skipped | Dry-Run |")
        lines.append("|---|---|---|---|---|")
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                summary.total,
                summary.succeeded,
                summary.failed,
                summary.skipped,
                summary.dry_run,
            )
        )
        lines.append("")

        lines.append("## Actions")
        lines.append("")
        if len(self._actions) == 0:
            lines.append("(no actions recorded)")
        else:
            for index, record in enumerate(self._actions):
                heading_index = index + 1
                lines.append(
                    "### {}. {} / {} - {}".format(
                        heading_index,
                        record.feature,
                        record.operation,
                        record.status.value.upper(),
                    )
                )
                if record.detail is not None:
                    lines.append("- Detail: {}".format(record.detail))
                if record.duration_seconds is not None:
                    lines.append("- Duration: {:.2f}s".format(record.duration_seconds))
                if record.error is not None:
                    lines.append("- Error: {}".format(record.error))
                lines.append("")

        body = "\n".join(lines)
        return body

    def emit_report(
        self,
        format: MacosReportFormat,
        path: Optional[str] = None,
    ) -> MacosReportWriteResult:
        """
            Serialise and write the report.

            :param format: Output format.
            :param path: Destination path.  When ``None``, defaults to
                         ``<output_dir>/<session_id>/report.<ext>``.

            :returns: A :class:`MacosReportWriteResult`.
            :raises: RuntimeError when called before
                     :meth:`end_session`.
        """
        active_session = self._session
        if active_session is None:
            raise RuntimeError("emit_report called without active session")
        if active_session.ended_at is None:
            raise RuntimeError("emit_report called before end_session")

        if not isinstance(format, MacosReportFormat):
            error_result = MacosReportWriteResult(
                success=False,
                path=path if path is not None else "",
                error="unsupported report format: {!r}".format(format),
            )
            return error_result

        if path is None:
            destination_path = self._default_emit_path(format=format)
        else:
            destination_path = path

        if len(destination_path) == 0:
            empty_path_result = MacosReportWriteResult(
                success=False,
                path=destination_path,
                error="path must be a non-empty string",
            )
            return empty_path_result

        if self._config.dry_run is True:
            dry_run_result = MacosReportWriteResult(
                success=True,
                path=destination_path,
                dry_run=True,
            )
            return dry_run_result

        body = self._render_body(format=format)

        try:
            self._ensure_parent_directory(path=destination_path)
            self._write_file_contents(path=destination_path, content=body)
        except Exception as ex:
            failure_result = MacosReportWriteResult(
                success=False,
                path=destination_path,
                error=str(ex),
            )
            return failure_result

        success_result = MacosReportWriteResult(
            success=True,
            path=destination_path,
        )
        return success_result

    def _default_emit_path(self, *, format: MacosReportFormat) -> str:
        """
            Compute the default output path for the active session and
            requested format.
        """
        active_session = self._session
        if active_session is None:
            return ""

        extension_by_format = {
            MacosReportFormat.JSON: "json",
            MacosReportFormat.TEXT: "txt",
            MacosReportFormat.YAML: "yaml",
            MacosReportFormat.MARKDOWN: "md",
        }
        extension = extension_by_format[format]
        directory = self.default_session_path(session_id=active_session.run_id)
        rtnval = "{}report.{}".format(directory, extension)
        return rtnval

    def _render_body(self, *, format: MacosReportFormat) -> str:
        """
            Render the report body for the requested format.
        """
        if format == MacosReportFormat.JSON:
            return self.to_json()
        if format == MacosReportFormat.TEXT:
            return self.to_text()
        if format == MacosReportFormat.YAML:
            return self.to_yaml()
        if format == MacosReportFormat.MARKDOWN:
            return self.to_markdown()
        err_msg = "unsupported report format: {!r}".format(format)
        raise ValueError(err_msg)

    def _ensure_parent_directory(self, *, path: str) -> None:
        """
            Create the parent directory of ``path`` via ``mkdir -p``
            dispatched through the runner.
        """
        parent = os.path.dirname(path)
        if len(parent) == 0:
            return

        mkdir_result = self._runner.run(
            ["mkdir", "-p", parent],
            elevated=False,
        )
        if mkdir_result.exit_code != 0:
            err_msg_lines = ["Failed to create report directory."]
            err_msg_lines.append("    PARENT: {}".format(parent))
            err_msg_lines.append("    EXIT_CODE: {}".format(mkdir_result.exit_code))
            err_msg_lines.append("    STDERR: {}".format(mkdir_result.stderr))
            err_msg = "\n".join(err_msg_lines)
            raise RuntimeError(err_msg)
        return

    def _write_file_contents(self, *, path: str, content: str) -> None:
        """
            Write ``content`` to ``path`` by piping it through ``tee``
            via the runner's stdin support.
        """
        tee_result = self._runner.run(
            ["tee", path],
            stdin=content,
            elevated=False,
        )
        if tee_result.exit_code != 0:
            err_msg_lines = ["Failed to write report file."]
            err_msg_lines.append("    PATH: {}".format(path))
            err_msg_lines.append("    EXIT_CODE: {}".format(tee_result.exit_code))
            err_msg_lines.append("    STDERR: {}".format(tee_result.stderr))
            err_msg = "\n".join(err_msg_lines)
            raise RuntimeError(err_msg)
        return

    def _compute_overall_status(self) -> MacosActionStatus:
        """
            Aggregate per-action statuses into a session-level status.
        """
        if len(self._actions) == 0:
            return MacosActionStatus.SUCCESS

        any_failed = False
        all_skipped = True
        all_dry_run = True
        for record in self._actions:
            if record.status == MacosActionStatus.FAILED:
                any_failed = True
            if record.status != MacosActionStatus.SKIPPED:
                all_skipped = False
            if record.status != MacosActionStatus.DRY_RUN:
                all_dry_run = False

        if any_failed is True:
            return MacosActionStatus.FAILED
        if all_dry_run is True:
            return MacosActionStatus.DRY_RUN
        if all_skipped is True:
            return MacosActionStatus.SKIPPED
        return MacosActionStatus.SUCCESS

    def _coerce_status_string(self, *, value: str) -> MacosActionStatus:
        """
            Map a caller-supplied status string to a
            :class:`MacosActionStatus`.  Unknown strings map to
            ``SUCCESS`` so that callers never crash on a typo.
        """
        normalized = value.lower()
        for status in MacosActionStatus:
            if status.value == normalized:
                return status
        if normalized == "completed":
            return MacosActionStatus.SUCCESS
        return MacosActionStatus.SUCCESS

    def _require_session(self, *, operation: str) -> None:
        """
            Raise ``RuntimeError`` when no session is active.
        """
        if self._session is None:
            err_msg = "{}: no active session; call begin_session first".format(operation)
            raise RuntimeError(err_msg)
        return

    def _to_dict(self) -> dict[str, Any]:
        """
            Build the serialisable dict used by all emit paths.
        """
        active_session = self._session
        summary = self.summary_counts()

        if active_session is None:
            session_payload: dict[str, Any] = {
                "run_id": None,
                "host": None,
                "description": None,
                "started_at": None,
                "ended_at": None,
                "status": None,
                "metadata": {},
            }
        else:
            ended_at_value: Optional[str] = None
            if active_session.ended_at is not None:
                ended_at_value = active_session.ended_at.isoformat()
            status_value: Optional[str] = None
            if active_session.status is not None:
                status_value = active_session.status.value
            session_payload = {
                "run_id": active_session.run_id,
                "host": active_session.host,
                "description": active_session.description,
                "started_at": active_session.started_at.isoformat(),
                "ended_at": ended_at_value,
                "status": status_value,
                "metadata": active_session.metadata,
            }

        actions_payload: list[dict[str, Any]] = []
        for record in self._actions:
            actions_payload.append(_record_to_dict(record=record))

        queries_payload: list[dict[str, Any]] = []
        for record in self._queries:
            queries_payload.append(_record_to_dict(record=record))

        counts_payload = {
            "total": summary.total,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "skipped": summary.skipped,
            "dry_run": summary.dry_run,
        }

        rtnval = {
            "session": session_payload,
            "actions": actions_payload,
            "queries": queries_payload,
            "errors": list(self._errors),
            "counts": counts_payload,
        }
        return rtnval


def _utc_now() -> datetime:
    """
        Default clock callable.  Returns a UTC-aware datetime.
    """
    rtnval = datetime.now(timezone.utc)
    return rtnval


def _record_to_dict(*, record: MacosActionRecord) -> dict[str, Any]:
    """
        Convert one :class:`MacosActionRecord` to a serialisable dict.
    """
    timestamp_value: Optional[str] = None
    if record.timestamp is not None:
        timestamp_value = record.timestamp.isoformat()
    rtnval = {
        "feature": record.feature,
        "operation": record.operation,
        "status": record.status.value,
        "detail": record.detail,
        "error": record.error,
        "duration_seconds": record.duration_seconds,
        "timestamp": timestamp_value,
    }
    return rtnval


def _coerce_record(
    *,
    value: Any,
    default_status: MacosActionStatus,
) -> MacosActionRecord:
    """
        Adapt an arbitrary value into a :class:`MacosActionRecord`.

        Preference order:
          1. ``value`` is already a :class:`MacosActionRecord` -> return as-is.
          2. ``value`` exposes ``feature`` / ``operation`` / ``status``
             attributes -> harvest them.
          3. ``value`` is a dict -> read keys.
          4. Fallback -> store the repr() under feature ``"unknown"``.
    """
    if isinstance(value, MacosActionRecord):
        return value

    feature_value: Optional[str] = None
    operation_value: Optional[str] = None
    status_value: Any = None
    detail_value: Optional[str] = None
    error_value: Optional[str] = None
    duration_value: Optional[float] = None
    timestamp_value: Optional[datetime] = None

    if isinstance(value, dict):
        feature_value = value.get("feature")
        operation_value = value.get("operation")
        status_value = value.get("status")
        detail_value = value.get("detail")
        error_value = value.get("error")
        duration_value = value.get("duration_seconds")
        timestamp_value = value.get("timestamp")
    else:
        feature_value = getattr(value, "feature", None)
        operation_value = getattr(value, "operation", None)
        status_value = getattr(value, "status", None)
        detail_value = getattr(value, "detail", None)
        error_value = getattr(value, "stderr", None)
        if error_value is None or len(error_value) == 0:
            error_value = getattr(value, "error", None)
        duration_value = getattr(value, "duration_seconds", None)
        timestamp_value = getattr(value, "timestamp", None)

    if feature_value is None:
        feature_value = "unknown"
    if operation_value is None:
        operation_value = "unknown"

    coerced_status = _coerce_status_value(value=status_value, default=default_status)

    record = MacosActionRecord(
        feature=feature_value,
        operation=operation_value,
        status=coerced_status,
        detail=detail_value,
        error=error_value,
        duration_seconds=duration_value,
        timestamp=timestamp_value,
    )
    return record


def _coerce_status_value(
    *,
    value: Any,
    default: MacosActionStatus,
) -> MacosActionStatus:
    """
        Coerce a status-shaped value to a :class:`MacosActionStatus`.
    """
    if isinstance(value, MacosActionStatus):
        return value

    if value is None:
        return default

    raw_value = getattr(value, "value", value)
    if isinstance(raw_value, str):
        normalized = raw_value.lower()
        for status in MacosActionStatus:
            if status.value == normalized:
                return status
    return default
