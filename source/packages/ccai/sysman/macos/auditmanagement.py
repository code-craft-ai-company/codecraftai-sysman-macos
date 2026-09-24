"""
    macOS audit-management feature.

    macOS ships the BSM-derived audit subsystem inherited from
    OpenBSM.  The relevant tools are ``audit`` (control), ``auditd``
    (daemon, managed via launchd), ``praudit`` (decode binary records
    into text), and ``auditreduce`` (filter records).  The kernel-side
    policy is configured by editing
    ``/etc/security/audit_control``: a colon-separated key/value file
    whose ``flags`` line carries a comma-separated list of audit-class
    abbreviations (``lo`` = login/logout, ``fa`` = file attribute, ``pc``
    = process, ``ad`` = administrative, etc.).

    Audit-model divergence from Linux:
        macOS audit is **class-based**, not line-based.  A "rule" in
        the cross-OS contract maps to enabling a single audit-class
        flag in ``/etc/security/audit_control``.  Adding a rule means
        appending the class abbreviation to the ``flags`` list and
        reloading the daemon; removing a rule means dropping the
        abbreviation.  There is no per-path or per-syscall granularity
        comparable to ``auditctl -w`` / ``auditctl -a``.

    File I/O policy:
        Every file read / write / probe is performed through the
        injected :class:`ICommandRunner` (``cat`` / ``tee`` /
        ``test -e``).  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        Every write verb (``add_rule``, ``remove_rule``,
        ``enable_auditing``, ``disable_auditing``) honours
        ``config.elevated``.  Reads (``list_rules``,
        ``get_audit_status``, ``search_events``) never elevate.

    Audit-class validation:
        :data:`KNOWN_AUDIT_CLASSES` is the public table of the audit-class
        abbreviations documented in ``audit_control(5)`` and
        :func:`is_known_audit_class` checks a token against it (accepting
        an ``audit_control(5)`` ``+`` / ``-`` / ``^`` / ``^+`` / ``^-``
        prefix).  Tools use them to validate input.  ``add_rule`` and
        ``remove_rule`` gate their class token on the table only when
        ``MacosAuditManagerConfig.validate_event_class`` is True; by
        default any token is passed through, because hosts may define
        site-specific classes in ``/etc/security/audit_class``.

    Surface choices:
        * ``add_rule(spec)`` rewrites the ``flags`` line of
          ``/etc/security/audit_control`` so that ``spec.event_class``
          is present, then issues ``audit -s`` to reload the kernel
          policy.
        * ``remove_rule(rule_id)`` rewrites the ``flags`` line so
          that ``rule_id`` is absent, then ``audit -s`` reloads.
        * ``list_rules()`` parses the ``flags`` line of
          ``/etc/security/audit_control`` and ALSO includes any
          rule-style entries in ``/etc/security/audit_user`` (one
          per non-comment line).
        * ``enable_auditing()`` issues ``audit -i`` (initialize) and
          then ``launchctl bootstrap system
          /System/Library/LaunchDaemons/com.apple.auditd.plist`` to
          start the daemon under launchd.
        * ``disable_auditing()`` issues ``launchctl bootout
          system/com.apple.auditd``.
        * ``get_audit_status()`` parses ``audit -s`` text into a
          :class:`MacosAuditStatus`.
        * ``search_events(query)`` invokes ``praudit -l <log_path>``
          and filters the line stream by the query's substring fields
          (or ``raw_filter`` when supplied).
"""

import os

from dataclasses import dataclass
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_AUDIT = "audit"
_PRAUDIT = "praudit"
_LAUNCHCTL = "launchctl"

_DEFAULT_AUDIT_CONTROL = "/etc/security/audit_control"
_DEFAULT_AUDIT_USER = "/etc/security/audit_user"
_DEFAULT_AUDIT_LOG_DIR = "/var/audit"
_DEFAULT_AUDITD_PLIST = (
    "/System/Library/LaunchDaemons/com.apple.auditd.plist"
)
_DEFAULT_AUDITD_LABEL = "system/com.apple.auditd"

# Documented audit-class abbreviations from audit_control(5).  The
# table is public so tools can validate input (see
# is_known_audit_class()).  The manager gates add_rule / remove_rule
# on it only when MacosAuditManagerConfig.validate_event_class is True;
# by default callers may pass any token.
KNOWN_AUDIT_CLASSES = (
    "fr",  # file read
    "fw",  # file write
    "fa",  # file attribute access
    "fm",  # file attribute modify
    "fc",  # file create
    "fd",  # file delete
    "cl",  # file close
    "pc",  # process
    "nt",  # network
    "ip",  # ipc
    "na",  # non-attributable
    "ad",  # administrative
    "lo",  # login / logout
    "aa",  # authentication and authorization
    "ap",  # application
    "io",  # ioctl
    "ex",  # exec
    "ot",  # other
    "all",  # all classes
    "no",  # invalid class
)

# Backward-compatible alias for the private name under which the table
# was documented before it was made public.
_KNOWN_CLASSES = KNOWN_AUDIT_CLASSES

# Prefixes audit_control(5) accepts in front of a class abbreviation in
# a flags list.  Longest first so "^+" / "^-" are stripped before "^".
_AUDIT_CLASS_PREFIXES = ("^+", "^-", "^", "+", "-")


MacosAuditActionStatus = ActionStatus


@dataclass
class MacosAuditManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosAuditManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param audit_control: Absolute path to the audit-control
                              file.  Defaults to
                              ``/etc/security/audit_control``.
        :param audit_user: Absolute path to the per-user audit
                           rules file.  Defaults to
                           ``/etc/security/audit_user``.
        :param audit_log_dir: Directory containing audit log files.
                              Defaults to ``/var/audit``.
        :param auditd_plist: Path to the ``com.apple.auditd``
                             LaunchDaemon plist.  Defaults to the
                             stock system path.
        :param auditd_label: launchctl label of the auditd service.
                             Defaults to ``system/com.apple.auditd``.
        :param validate_event_class: When True, ``add_rule`` and
                                     ``remove_rule`` return ``FAILED``
                                     without reading or writing
                                     ``audit_control`` when their class
                                     token is not in
                                     :data:`KNOWN_AUDIT_CLASSES` (see
                                     :func:`is_known_audit_class`).
                                     Defaults to False: any token is
                                     accepted, including site-specific
                                     classes defined in
                                     ``/etc/security/audit_class``.
    """
    audit_control: str = _DEFAULT_AUDIT_CONTROL
    audit_user: str = _DEFAULT_AUDIT_USER
    audit_log_dir: str = _DEFAULT_AUDIT_LOG_DIR
    auditd_plist: str = _DEFAULT_AUDITD_PLIST
    auditd_label: str = _DEFAULT_AUDITD_LABEL
    validate_event_class: bool = False


@dataclass
class MacosAuditActionResult(FeatureActionResult):
    """
        Result of an audit-management write operation.

        :param rule_id: The rule identifier targeted, when applicable.
        :param rule_key: The rule's audit-class token (``"lo"``,
                         ``"fa"``, …) when applicable.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    rule_id: Optional[str] = None
    rule_key: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosAuditRuleSpec:
    """
        Structured input for :meth:`MacosAuditManager.add_rule` /
        :meth:`MacosAuditManager.remove_rule`.

        macOS audit rules are class-based: the ``event_class`` is one
        of the documented BSM class abbreviations (``"lo"``, ``"fa"``,
        ``"pc"``, …).  ``name`` is a logical identifier that mirrors
        ``event_class`` for the pilot but may diverge in the future
        if multiple classes collapse onto a single rule_id.

        :param name: Logical rule identifier (acts as the abstract
                     ``rule_id``).
        :param event_class: BSM audit-class abbreviation.
        :param condition: Reserved for the audit_control ``naflags``
                          line counterpart; ``None`` for the pilot.
        :param enabled: When False the rule is recorded but not
                        applied.  The pilot honours ``False`` by
                        emitting ``SKIPPED``.
    """
    name: str
    event_class: str
    condition: Optional[str] = None
    enabled: bool = True


@dataclass
class MacosAuditRuleInfo:
    """
        One audit rule observed in the ``flags`` line of
        ``audit_control`` or in ``audit_user``.

        :param rule_id: The rule identifier.  Mirrors the class token
                        for ``audit_control`` rows.
        :param name: Logical name (mirrors ``rule_id``).
        :param event_class: The BSM class abbreviation.
        :param expression: The token as it appears in the source
                           file.  For ``audit_user`` rows this is
                           the entire rule line.
        :param subsystem: ``"class"`` for ``audit_control`` flags,
                          ``"user"`` for ``audit_user`` rows.
        :param source: Origin file path.
    """
    rule_id: str
    name: str
    event_class: str
    expression: str
    subsystem: str
    source: str


@dataclass
class MacosAuditStatus:
    """
        Parsed ``audit -s`` status block.

        :param enabled: True when the audit subsystem is enabled.
                        Inferred from ``AUC_AUDITING`` in the
                        ``audit -s`` text.
        :param running: True when the auditd daemon is running.
                        Inferred from a non-zero ``pid`` in the
                        ``audit -s`` text or from
                        ``launchctl print system/com.apple.auditd``.
        :param pid: Daemon PID when surfaced, ``None`` otherwise.
        :param failure_mode: Reserved for future surfacing of the
                             audit-control ``policy`` line; ``None``
                             in the pilot.
        :param rate_limit: Reserved.  Always ``None``.
        :param backlog_limit: Reserved.  Always ``None``.
        :param backlog: Reserved.  Always ``None``.
        :param lost_events: Cumulative dropped-record count when
                            surfaced; ``None`` otherwise.
        :param locked: True when ``audit -s`` reports the kernel as
                       locked / immutable.  macOS exposes this via
                       the ``"locked"`` keyword in the status text.
        :param raw: The raw stdout text for debuggability.
    """
    enabled: bool
    running: bool
    pid: Optional[int]
    failure_mode: Optional[str]
    rate_limit: Optional[int]
    backlog_limit: Optional[int]
    backlog: Optional[int]
    lost_events: Optional[int]
    locked: bool
    raw: str


@dataclass
class MacosAuditEventQuery:
    """
        Structured query for :meth:`MacosAuditManager.search_events`.

        macOS does not expose a structured filter API comparable to
        Linux's ``ausearch``.  The manager builds a ``praudit -l``
        invocation against the configured log file and applies the
        query as a substring filter against the decoded text stream.

        :param key: Substring filter applied against each praudit
                    line.
        :param event_class: When set, lines whose decoded ``event``
                            field does not contain this token are
                            dropped.
        :param since: Reserved for future ``-a <yyyymmdd[hhmmss]>``
                      argument plumbing; today the field is ignored.
        :param until: Reserved for future ``-d <yyyymmdd[hhmmss]>``;
                      today the field is ignored.
        :param user: When set, lines whose decoded ``subject``
                     field does not contain this token are dropped.
        :param raw_filter: When set, replaces the structured argv
                           with verbatim tokens passed to ``praudit``.
        :param limit: Cap on the number of events returned.
    """
    key: Optional[str] = None
    event_class: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None
    user: Optional[str] = None
    raw_filter: Optional[str] = None
    limit: Optional[int] = None


@dataclass
class MacosAuditEvent:
    """
        One praudit-decoded audit event.

        :param timestamp: The first whitespace-separated token of the
                          decoded line; empty when not surfaced.
        :param event_id: Reserved; always ``None`` in the pilot.
        :param category: First ``,header,`` field of the praudit
                         line, or the first comma-separated token
                         when no header field is present.
        :param severity: Reserved; always ``None``.
        :param message: A short summary built from the decoded line.
        :param raw: The full decoded line.
    """
    timestamp: str
    event_id: Optional[int]
    category: Optional[str]
    severity: Optional[str]
    message: str
    raw: str


class MacosAuditQueryError(Exception):
    """
        Raised when an audit read fails for an unrecoverable reason.
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
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Audit query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosAuditImmutableError(Exception):
    """
        Raised when a write is attempted while the audit subsystem
        is in immutable / locked mode.
    """

    def __init__(self, *, detail: str) -> None:
        """
            :param detail: Human-readable description of the locked
                           state and the rejected operation.
        """
        self.detail = detail
        err_msg_lines = ["Audit subsystem is in immutable mode."]
        err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosAuditManager(BaseFeatureManager):
    """
        macOS audit-management manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`; ``audit_control``
        I/O flows through ``cat`` / ``tee`` rather than a separate
        file operator.
    """

    result_class = MacosAuditActionResult
    status_class = MacosAuditActionStatus
    feature_name = "audit-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_rules(self) -> list:
        """
            Enumerate audit rules.

            Reads the ``flags`` line of ``audit_control`` and emits
            one :class:`MacosAuditRuleInfo` per audit-class token.
            Also reads ``audit_user`` and emits one row per
            non-comment, non-blank line so that per-user rules are
            visible.

            :returns: One :class:`MacosAuditRuleInfo` per parsed
                      rule.
            :raises MacosAuditQueryError: When neither file can be
                                          read **and** the audit
                                          subsystem is otherwise
                                          unhealthy.  In practice
                                          missing files surface as
                                          empty lists.
        """
        rtnval: list = []

        control_text = self._read_file(path=self._config.audit_control)
        if control_text is not None:
            class_rows = _parse_audit_control_flags(
                text=control_text, source=self._config.audit_control,
            )
            for row in class_rows:
                rtnval.append(row)

        user_text = self._read_file(path=self._config.audit_user)
        if user_text is not None:
            user_rows = _parse_audit_user(
                text=user_text, source=self._config.audit_user,
            )
            for row in user_rows:
                rtnval.append(row)

        return rtnval

    def get_audit_status(self) -> MacosAuditStatus:
        """
            Run ``audit -s`` and parse the textual status payload.

            :returns: The :class:`MacosAuditStatus`.
            :raises MacosAuditQueryError: When ``audit -s`` exits
                                          non-zero with non-empty
                                          stderr.
        """
        command = [_AUDIT, "-s"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise MacosAuditQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_audit_status(text=run_result.stdout)
        return rtnval

    def search_events(
        self,
        query: MacosAuditEventQuery,
    ) -> list:
        """
            Search audit events by decoding the latest log file with
            ``praudit -l`` and applying the query as a substring
            filter against each line.

            :param query: The :class:`MacosAuditEventQuery` describing
                          the filter.

            :returns: One :class:`MacosAuditEvent` per matching line,
                      capped at ``query.limit`` when set.
            :raises MacosAuditQueryError: When ``praudit`` exits
                                          non-zero with non-empty
                                          stderr.
        """
        if query.raw_filter is not None and len(query.raw_filter) > 0:
            command = [_PRAUDIT] + query.raw_filter.split()
        else:
            log_path = self._latest_log_path()
            command = [_PRAUDIT, "-l", log_path]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise MacosAuditQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        events = _parse_praudit_lines(
            text=run_result.stdout,
            key=query.key,
            event_class=query.event_class,
            user=query.user,
            limit=query.limit,
        )
        return events

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_rule(
        self,
        spec: MacosAuditRuleSpec,
    ) -> MacosAuditActionResult:
        """
            Add an audit-class rule.

            Reads ``audit_control``, ensures the ``flags`` line
            includes ``spec.event_class``, writes the modified file
            via ``tee``, and dispatches ``audit -s`` to reload the
            kernel policy.

            Idempotency: when ``spec.event_class`` is already in the
            ``flags`` line, the verb returns ``SKIPPED`` without
            invoking the writer.

            Validation: when ``config.validate_event_class`` is True and
            ``spec.event_class`` is not a known audit class (see
            :func:`is_known_audit_class`), the verb returns ``FAILED``
            without reading or writing ``audit_control``.

            :param spec: The :class:`MacosAuditRuleSpec`.

            :returns: The :class:`MacosAuditActionResult`.
        """
        write_command = ["tee", self._config.audit_control]

        if self._config.validate_event_class:
            validation_failure = _validate_event_class(
                event_class=spec.event_class,
            )
            if validation_failure is not None:
                failed = self._build_result(
                    status=MacosAuditActionStatus.FAILED,
                    rule_id=spec.name,
                    rule_key=spec.event_class,
                    command=write_command,
                    message=validation_failure,
                    stderr=validation_failure,
                )
                return failed

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAuditActionStatus.DRY_RUN,
                rule_id=spec.name,
                rule_key=spec.event_class,
                command=write_command,
            )
            return dry

        if not spec.enabled:
            skipped = self._build_result(
                status=MacosAuditActionStatus.SKIPPED,
                rule_id=spec.name,
                rule_key=spec.event_class,
                command=write_command,
                message="spec.enabled is False; rule recorded but not applied",
            )
            return skipped

        existing = self._read_file(path=self._config.audit_control)
        if existing is None:
            existing = ""

        new_body, changed = _add_class_to_flags(
            text=existing, event_class=spec.event_class,
        )

        if not changed:
            skipped = self._build_result(
                status=MacosAuditActionStatus.SKIPPED,
                rule_id=spec.name,
                rule_key=spec.event_class,
                command=write_command,
                message="event_class already present in audit_control flags",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_body,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosAuditActionStatus.SUCCESS
        else:
            mapped_status = MacosAuditActionStatus.FAILED

        # Reload the daemon so the modified flags take effect.  When
        # the file write failed we still attempt the reload so that a
        # partial rollback (the file was written but stored under a
        # different inode by tee) does not leave the kernel out of
        # sync; the reload's outcome is folded into mapped_status.
        reload_command = [_AUDIT, "-s"]
        reload_result = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_result.exit_code != 0:
            mapped_status = MacosAuditActionStatus.FAILED

        rtnval = MacosAuditActionResult(
            status=mapped_status,
            detail=spec.name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            rule_id=spec.name,
            rule_key=spec.event_class,
        )
        return rtnval

    def remove_rule(
        self,
        rule_id: str,
    ) -> MacosAuditActionResult:
        """
            Remove an audit-class rule by class abbreviation.

            Reads ``audit_control``, drops ``rule_id`` from the
            ``flags`` line, writes the modified file via ``tee``,
            and dispatches ``audit -s`` to reload.

            Validation: when ``config.validate_event_class`` is True and
            ``rule_id`` is not a known audit class (see
            :func:`is_known_audit_class`), the verb returns ``FAILED``
            without reading or writing ``audit_control``.

            :param rule_id: The audit-class abbreviation to remove
                            (e.g. ``"lo"``).

            :returns: The :class:`MacosAuditActionResult`.
        """
        write_command = ["tee", self._config.audit_control]

        if self._config.validate_event_class:
            validation_failure = _validate_event_class(event_class=rule_id)
            if validation_failure is not None:
                failed = self._build_result(
                    status=MacosAuditActionStatus.FAILED,
                    rule_id=rule_id,
                    rule_key=rule_id,
                    command=write_command,
                    message=validation_failure,
                    stderr=validation_failure,
                )
                return failed

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAuditActionStatus.DRY_RUN,
                rule_id=rule_id,
                rule_key=rule_id,
                command=write_command,
            )
            return dry

        existing = self._read_file(path=self._config.audit_control)
        if existing is None:
            skipped = self._build_result(
                status=MacosAuditActionStatus.SKIPPED,
                rule_id=rule_id,
                rule_key=rule_id,
                command=write_command,
                message="audit_control file not present",
            )
            return skipped

        new_body, changed = _remove_class_from_flags(
            text=existing, event_class=rule_id,
        )

        if not changed:
            skipped = self._build_result(
                status=MacosAuditActionStatus.SKIPPED,
                rule_id=rule_id,
                rule_key=rule_id,
                command=write_command,
                message="event_class not present in audit_control flags",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_body,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosAuditActionStatus.SUCCESS
        else:
            mapped_status = MacosAuditActionStatus.FAILED

        reload_command = [_AUDIT, "-s"]
        reload_result = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_result.exit_code != 0:
            mapped_status = MacosAuditActionStatus.FAILED

        rtnval = MacosAuditActionResult(
            status=mapped_status,
            detail=rule_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            rule_id=rule_id,
            rule_key=rule_id,
        )
        return rtnval

    def enable_auditing(self) -> MacosAuditActionResult:
        """
            Enable the audit subsystem.

            Issues ``audit -i`` to initialise the kernel side and
            ``launchctl bootstrap system <auditd_plist>`` to load the
            daemon under launchd.

            Idempotency: when ``audit -s`` reports the subsystem as
            already enabled and running, the verb returns
            ``SKIPPED``.

            :returns: The :class:`MacosAuditActionResult`.
        """
        primary_command = [_AUDIT, "-i"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAuditActionStatus.DRY_RUN,
                rule_id=None,
                rule_key=None,
                command=primary_command,
            )
            return dry

        status = self._safe_status()
        if status is not None and status.enabled and status.running:
            skipped = self._build_result(
                status=MacosAuditActionStatus.SKIPPED,
                rule_id=None,
                rule_key=None,
                command=primary_command,
                message="audit subsystem already enabled and running",
            )
            return skipped

        if status is not None and status.locked:
            failed = self._build_result(
                status=MacosAuditActionStatus.FAILED,
                rule_id=None,
                rule_key=None,
                command=primary_command,
                message="audit subsystem is in immutable mode",
                stderr="audit subsystem is in immutable mode",
            )
            return failed

        run_result = self._runner.run(
            list(primary_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosAuditActionStatus.SUCCESS
        else:
            mapped_status = MacosAuditActionStatus.FAILED

        bootstrap_command = [
            _LAUNCHCTL, "bootstrap", "system", self._config.auditd_plist,
        ]
        bootstrap_result = self._runner.run(
            list(bootstrap_command),
            elevated=self._config.elevated,
        )
        if bootstrap_result.exit_code != 0:
            mapped_status = MacosAuditActionStatus.FAILED

        rtnval = MacosAuditActionResult(
            status=mapped_status,
            detail="enable",
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(primary_command),
            rule_id=None,
            rule_key=None,
        )
        return rtnval

    def disable_auditing(self) -> MacosAuditActionResult:
        """
            Disable the audit subsystem.

            Issues ``launchctl bootout system/com.apple.auditd`` to
            unload the daemon.  macOS does not expose a kernel-side
            ``-e 0`` toggle; on systems where the daemon is the only
            way to flush audit records, taking the daemon down is
            equivalent to disabling the subsystem.

            Idempotency: when ``audit -s`` reports the daemon as
            already not running, the verb returns ``SKIPPED``.

            :returns: The :class:`MacosAuditActionResult`.
        """
        primary_command = [
            _LAUNCHCTL, "bootout", self._config.auditd_label,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAuditActionStatus.DRY_RUN,
                rule_id=None,
                rule_key=None,
                command=primary_command,
            )
            return dry

        status = self._safe_status()
        if status is not None and (not status.enabled) and (not status.running):
            skipped = self._build_result(
                status=MacosAuditActionStatus.SKIPPED,
                rule_id=None,
                rule_key=None,
                command=primary_command,
                message="audit subsystem already disabled and stopped",
            )
            return skipped

        if status is not None and status.locked:
            failed = self._build_result(
                status=MacosAuditActionStatus.FAILED,
                rule_id=None,
                rule_key=None,
                command=primary_command,
                message="audit subsystem is in immutable mode",
                stderr="audit subsystem is in immutable mode",
            )
            return failed

        run_result = self._runner.run(
            list(primary_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosAuditActionStatus.SUCCESS
        else:
            mapped_status = MacosAuditActionStatus.FAILED

        rtnval = MacosAuditActionResult(
            status=mapped_status,
            detail="disable",
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(primary_command),
            rule_id=None,
            rule_key=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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

    def _safe_status(self) -> Optional[MacosAuditStatus]:
        """
            Return the parsed status, swallowing
            :class:`MacosAuditQueryError` so write paths can fall
            through to attempting the operation.

            :returns: The :class:`MacosAuditStatus`, or ``None``
                      when the read failed.
        """
        try:
            rtnval: Optional[MacosAuditStatus] = self.get_audit_status()
        except MacosAuditQueryError:
            rtnval = None
        return rtnval

    def _latest_log_path(self) -> str:
        """
            Pick the latest audit log file under
            :attr:`MacosAuditManagerConfig.audit_log_dir`.

            macOS rotates audit logs as ``YYYYMMDDHHmmss.<status>``
            files.  The ``current`` symlink in the directory points
            to the active file.  We default to that symlink so
            ``praudit`` reads the live record stream.

            :returns: ``<audit_log_dir>/current`` (a symlink under
                      normal operation).
        """
        rtnval = "{}/current".format(self._config.audit_log_dir)
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosAuditActionStatus,
        rule_id: Optional[str],
        rule_key: Optional[str],
        command: list,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosAuditActionResult:
        """
            Construct a :class:`MacosAuditActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param rule_id: The rule identifier to echo onto the
                            result.
            :param rule_key: The rule key to echo onto the result.
            :param command: The would-have-been argv.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`MacosAuditActionResult`.
        """
        rtnval = MacosAuditActionResult(
            status=status,
            detail=rule_id,
            command=list(command),
            rule_id=rule_id,
            rule_key=rule_key,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Audit-class validation
# ----------------------------------------------------------------------


def is_known_audit_class(event_class: str) -> bool:
    """
        Check whether ``event_class`` names one of the audit classes in
        :data:`KNOWN_AUDIT_CLASSES`.

        One ``audit_control(5)`` prefix (``+``, ``-``, ``^``, ``^+`` or
        ``^-``) is accepted in front of the class abbreviation, so the
        tokens that :meth:`MacosAuditManager.list_rules` reports for a
        ``flags`` line validate as they are.

        :param event_class: The audit-class token to check (for example
                            ``"lo"`` or ``"-fr"``).

        :returns: True when the token names a known audit class,
                  otherwise False.
    """
    if not isinstance(event_class, str):
        rtnval = False
        return rtnval

    abbreviation = event_class
    for prefix in _AUDIT_CLASS_PREFIXES:
        if abbreviation.startswith(prefix):
            abbreviation = abbreviation[len(prefix):]
            break

    rtnval = abbreviation in KNOWN_AUDIT_CLASSES
    return rtnval


def _validate_event_class(*, event_class: str) -> Optional[str]:
    """
        Validate an audit-class token against
        :data:`KNOWN_AUDIT_CLASSES`.

        :param event_class: The audit-class token to validate.

        :returns: ``None`` when the token names a known audit class;
                  an error message string when it does not.
    """
    if is_known_audit_class(event_class):
        rtnval: Optional[str] = None
        return rtnval

    err_msg_lines = [
        "Unknown audit class {!r}.".format(event_class),
        "    KNOWN: {}".format(", ".join(KNOWN_AUDIT_CLASSES)),
        "    PREFIXES: {} (optional, from audit_control(5))".format(
            ", ".join(_AUDIT_CLASS_PREFIXES),
        ),
        "    Set MacosAuditManagerConfig.validate_event_class=False to "
        "pass site-specific classes defined in /etc/security/audit_class.",
    ]
    rtnval = os.linesep.join(err_msg_lines)
    return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _split_flags_value(*, value: str) -> list:
    """
        Split a ``flags`` value into its constituent class tokens.

        macOS audit_control accepts comma-separated tokens.  Each
        token may carry a leading ``+`` (additive) or ``-``
        (subtractive); the parser preserves the prefix on the
        returned token so callers can distinguish.

        :param value: The flags value (everything after ``flags:``).

        :returns: One token per parsed class.
    """
    rtnval: list = []
    for raw_token in value.split(","):
        token = raw_token.strip()
        if len(token) > 0:
            rtnval.append(token)
    return rtnval


def _join_flags_value(*, tokens: list) -> str:
    """
        Join a list of class tokens back into a flags value.

        :param tokens: The tokens to join.

        :returns: The comma-separated flags value.
    """
    rtnval = ",".join(tokens)
    return rtnval


def _split_lines_preserve_endings(*, text: str) -> list:
    """
        Split ``text`` into lines while preserving line endings.

        ``str.splitlines`` strips the trailing newline; this helper
        keeps the original byte sequence so writers can rebuild the
        file without altering unrelated lines' EOL bytes.

        :param text: The input text.

        :returns: One item per line.
    """
    rtnval: list = []
    if len(text) == 0:
        return rtnval

    current = []
    for ch in text:
        current.append(ch)
        if ch == "\n":
            rtnval.append("".join(current))
            current = []
    if len(current) > 0:
        rtnval.append("".join(current))
    return rtnval


def _add_class_to_flags(
    *,
    text: str,
    event_class: str,
) -> tuple:
    """
        Ensure the ``flags`` line of ``audit_control`` contains
        ``event_class``.

        :param text: The current file body.
        :param event_class: The class abbreviation to add.

        :returns: A two-tuple ``(new_body, changed)`` where
                  ``changed`` is True when the body was modified.
    """
    lines = _split_lines_preserve_endings(text=text)
    found_flags = False
    rebuilt: list = []
    changed = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("flags:"):
            found_flags = True
            _, _, value = stripped.partition(":")
            tokens = _split_flags_value(value=value)
            if event_class in tokens:
                rebuilt.append(line)
                continue
            tokens.append(event_class)
            joined = _join_flags_value(tokens=tokens)
            rebuilt.append("flags:{}\n".format(joined))
            changed = True
        else:
            rebuilt.append(line)

    if not found_flags:
        # Append a new flags line.
        new_line = "flags:{}\n".format(event_class)
        if len(rebuilt) > 0 and not rebuilt[-1].endswith("\n"):
            rebuilt[-1] = rebuilt[-1] + "\n"
        rebuilt.append(new_line)
        changed = True

    rtnval = ("".join(rebuilt), changed)
    return rtnval


def _remove_class_from_flags(
    *,
    text: str,
    event_class: str,
) -> tuple:
    """
        Drop ``event_class`` from the ``flags`` line of
        ``audit_control``.

        :param text: The current file body.
        :param event_class: The class abbreviation to drop.

        :returns: A two-tuple ``(new_body, changed)``.
    """
    lines = _split_lines_preserve_endings(text=text)
    rebuilt: list = []
    changed = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("flags:"):
            _, _, value = stripped.partition(":")
            tokens = _split_flags_value(value=value)
            if event_class not in tokens:
                rebuilt.append(line)
                continue
            new_tokens = [t for t in tokens if t != event_class]
            joined = _join_flags_value(tokens=new_tokens)
            rebuilt.append("flags:{}\n".format(joined))
            changed = True
        else:
            rebuilt.append(line)

    rtnval = ("".join(rebuilt), changed)
    return rtnval


def _parse_audit_control_flags(
    *,
    text: str,
    source: str,
) -> list:
    """
        Parse the ``flags`` line of ``audit_control`` into a list of
        :class:`MacosAuditRuleInfo`.

        :param text: The full file body.
        :param source: The on-disk path (echoed onto each row).

        :returns: One row per audit-class token.
    """
    rtnval: list = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("flags:"):
            continue
        _, _, value = stripped.partition(":")
        for token in _split_flags_value(value=value):
            info = MacosAuditRuleInfo(
                rule_id=token,
                name=token,
                event_class=token,
                expression="flags:{}".format(token),
                subsystem="class",
                source=source,
            )
            rtnval.append(info)
        break
    return rtnval


def _parse_audit_user(*, text: str, source: str) -> list:
    """
        Parse ``audit_user`` into a list of
        :class:`MacosAuditRuleInfo`.

        ``audit_user`` is a per-user override file with one rule
        per non-comment line in the form
        ``<user>:<always-mask>:<never-mask>``.

        :param text: The full file body.
        :param source: The on-disk path.

        :returns: One row per parsed line.
    """
    rtnval: list = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 1:
            continue
        user_name = parts[0]
        if len(parts) >= 2:
            always_mask = parts[1]
        else:
            always_mask = ""
        info = MacosAuditRuleInfo(
            rule_id=user_name,
            name=user_name,
            event_class=always_mask,
            expression=line,
            subsystem="user",
            source=source,
        )
        rtnval.append(info)
    return rtnval


def _parse_audit_status(*, text: str) -> MacosAuditStatus:
    """
        Parse the textual ``audit -s`` output.

        ``audit -s`` emits one ``<key>:<value>`` pair per line.
        Recognised keys include ``AUC_AUDITING`` (auditing state),
        ``policy``, ``oncost``, ``filesize``, ``expire-after``,
        ``flags``, ``naflags``, and on some releases ``pid``.

        :param text: Raw stdout text.

        :returns: The :class:`MacosAuditStatus`.
    """
    enabled_bool = False
    running_bool = False
    pid_value: Optional[int] = None
    locked_bool = False
    lost_value: Optional[int] = None
    failure_value: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            continue
        # audit -s output uses both "<key>:<value>" and "<key> = <value>".
        key_token = ""
        value_token = ""
        if ":" in line:
            head, _, tail = line.partition(":")
            key_token = head.strip().lower()
            value_token = tail.strip()
        elif "=" in line:
            head, _, tail = line.partition("=")
            key_token = head.strip().lower()
            value_token = tail.strip()
        else:
            tokens = line.split(None, 1)
            if len(tokens) < 2:
                continue
            key_token = tokens[0].strip().lower()
            value_token = tokens[1].strip()

        if key_token == "auc_auditing" or key_token == "audit condition":
            lowered = value_token.lower()
            if lowered.startswith("auditing") or lowered == "1":
                enabled_bool = True
                running_bool = True
            elif "no" in lowered and "audit" in lowered:
                enabled_bool = False
            elif lowered == "0" or lowered == "off":
                enabled_bool = False
        elif key_token == "pid":
            try:
                pid_value = int(value_token)
                if pid_value > 0:
                    running_bool = True
            except ValueError:
                pid_value = None
        elif key_token == "policy":
            failure_value = value_token
            if "ahlt" in value_token.lower():
                failure_value = "halt"
            if "cnt" in value_token.lower():
                failure_value = "continue"
        elif key_token == "lost":
            try:
                lost_value = int(value_token)
            except ValueError:
                lost_value = None
        elif key_token == "locked" or key_token == "audit locked":
            lowered = value_token.lower()
            if lowered in {"yes", "true", "1"}:
                locked_bool = True

    rtnval = MacosAuditStatus(
        enabled=enabled_bool,
        running=running_bool,
        pid=pid_value,
        failure_mode=failure_value,
        rate_limit=None,
        backlog_limit=None,
        backlog=None,
        lost_events=lost_value,
        locked=locked_bool,
        raw=text,
    )
    return rtnval


def _parse_praudit_lines(
    *,
    text: str,
    key: Optional[str],
    event_class: Optional[str],
    user: Optional[str],
    limit: Optional[int],
) -> list:
    """
        Parse ``praudit -l`` output into :class:`MacosAuditEvent`
        rows, filtering by the supplied substring fields.

        ``praudit -l`` emits one record per line (the ``-l`` flag
        flattens the multi-line default).  Each record is a
        comma-separated body whose first field is the timestamp.

        :param text: Raw stdout text.
        :param key: Optional substring filter.
        :param event_class: Optional substring filter on the first
                            comma-separated token.
        :param user: Optional substring filter.
        :param limit: Cap on the number of events returned.

        :returns: One :class:`MacosAuditEvent` per matching line.
    """
    rtnval: list = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if len(line.strip()) == 0:
            continue

        if key is not None and len(key) > 0:
            if key not in line:
                continue

        if user is not None and len(user) > 0:
            if user not in line:
                continue

        # praudit -l comma-separates fields; the first non-header
        # field is typically the event type.
        fields = line.split(",")
        category: Optional[str] = None
        timestamp = ""
        if len(fields) > 0:
            timestamp = fields[0].strip()
        if len(fields) > 1:
            # ``header`` records use field index 1 as the event type
            # number; subsequent records use it as the event class.
            category = fields[1].strip()
        elif len(fields) > 0:
            category = fields[0].strip()

        if event_class is not None and len(event_class) > 0:
            if category is None or event_class not in category:
                continue

        message_summary = line[:200]
        event = MacosAuditEvent(
            timestamp=timestamp,
            event_id=None,
            category=category,
            severity=None,
            message=message_summary,
            raw=line,
        )
        rtnval.append(event)
        if limit is not None and len(rtnval) >= limit:
            return rtnval

    return rtnval
