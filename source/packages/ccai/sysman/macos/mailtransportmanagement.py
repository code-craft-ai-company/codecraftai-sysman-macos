"""
    macOS mail-transport-management feature.

    Implements the cross-OS mail-transport-management contract for
    macOS.  macOS ships **Postfix** as part of the base operating
    system; the configuration files live at the same paths as Linux
    (`/etc/postfix/main.cf`, `/etc/aliases`) and the command suite
    (`postconf`, `postqueue`, `mailq`, `newaliases`, `sendmail`) is
    available out of the box.  The daemon, however, is **disabled by
    default** on consumer macOS — the LaunchDaemon
    `org.postfix.master` ships in
    `/System/Library/LaunchDaemons/org.postfix.master.plist` but is
    not loaded.  Enabling Postfix requires
    ``launchctl load -w /System/Library/LaunchDaemons/org.postfix.master.plist``
    (or ``launchctl bootstrap system <plist>`` on Big Sur+); enabling
    the daemon is delegated to the service-management feature and is
    out of scope for this module.  This pilot manages the **Postfix
    configuration** assuming a daemon-management story exists
    elsewhere.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | macOS tool                        |
    +=============================+===================================+
    | ``get_mta_config``          | ``postconf -n``                   |
    +-----------------------------+-----------------------------------+
    | ``set_mta_config``          | ``postconf -e <key>=<value>``     |
    |                             | per setting + ``launchctl         |
    |                             | kickstart -k system/<label>``     |
    +-----------------------------+-----------------------------------+
    | ``add_alias``               | ``cat`` + render + ``tee          |
    |                             | /etc/aliases`` + ``newaliases``   |
    +-----------------------------+-----------------------------------+
    | ``remove_alias``            | ``cat`` + render + ``tee          |
    |                             | /etc/aliases`` + ``newaliases``   |
    +-----------------------------+-----------------------------------+
    | ``list_aliases``            | ``cat /etc/aliases`` + parse      |
    +-----------------------------+-----------------------------------+
    | ``test_mail_delivery``      | ``sendmail -t`` with payload on   |
    |                             | stdin                             |
    +-----------------------------+-----------------------------------+
    | ``get_queue_status``        | ``mailq`` + parse                 |
    +-----------------------------+-----------------------------------+
    | ``flush_queue``             | ``postqueue -f``                  |
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content on
        stdin.  This module does NOT consume an :class:`IFileOperator`.

    Reload mechanism on macOS:
        macOS has no `systemctl reload` equivalent.  ``launchctl
        kickstart -k system/<label>`` is the canonical way to
        re-execute a launchd-managed daemon — the ``-k`` flag stops a
        running instance before re-launching it.  This is closer to a
        full restart than a true `reload`, but for Postfix's master
        process it is the standard signal-handler hook on macOS.  When
        the daemon is not loaded (the consumer-default), the kickstart
        will fail; the manager surfaces that failure on the
        ``set_mta_config`` result.

    Service-disabled-by-default caveat:
        On consumer macOS the Postfix master is NOT loaded by default.
        ``set_mta_config`` will still write directives via ``postconf
        -e`` successfully, but the trailing ``launchctl kickstart``
        will fail with a "Could not find service" error.  Callers that
        want to apply directives without running a daemon can pass
        ``reload_after_set=False`` on the manager config.

    Idempotency model:
        Same as the Ubuntu pilot — per-directive ``SKIPPED`` recorded
        in the audit trail; aggregate ``SKIPPED`` when nothing
        changes.  ``add_alias`` / ``remove_alias`` skip when the
        desired state already matches and chain ``newaliases`` after
        successful writes.

    Elevation policy:
        Write verbs honour ``config.elevated``.  Reads
        (``get_mta_config``, ``list_aliases``, ``get_queue_status``)
        never elevate.
"""

import os
import re

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

_CAT = "cat"
_TEE = "tee"
_POSTCONF = "postconf"
_POSTQUEUE = "postqueue"
_NEWALIASES = "newaliases"
_MAILQ = "mailq"
_SENDMAIL = "sendmail"
_LAUNCHCTL = "launchctl"

_DEFAULT_ALIASES_FILE = "/etc/aliases"
_DEFAULT_SERVICE_LABEL = "org.postfix.master"
_DEFAULT_SERVICE_DOMAIN = "system"

_KNOWN_MAIN_KEYS = (
    "myhostname",
    "mydomain",
    "myorigin",
    "relayhost",
    "mynetworks",
    "inet_interfaces",
    "inet_protocols",
    "mailbox_size_limit",
    "message_size_limit",
)

_INT_KEYS = (
    "mailbox_size_limit",
    "message_size_limit",
)


MacosMtaActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class MacosMtaManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosMailTransportManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param aliases_file: Absolute path to the ``aliases`` file.
                             Default is ``/etc/aliases``.
        :param service_label: launchd service identifier used to
                              re-execute the Postfix master daemon
                              after a successful ``set_mta_config``.
                              Default is ``"org.postfix.master"``.
        :param service_domain: launchd domain qualifier (e.g.
                               ``"system"``).  Default is
                               ``"system"`` — the Postfix master is
                               operated as a system-domain daemon.
        :param reload_after_set: When True, ``set_mta_config``
                                 dispatches ``launchctl kickstart -k
                                 <domain>/<label>`` after at least
                                 one ``postconf -e`` succeeds.
                                 Default is True.  Set to False on
                                 consumer macOS where the Postfix
                                 LaunchDaemon is not loaded.
    """
    aliases_file: str = _DEFAULT_ALIASES_FILE
    service_label: str = _DEFAULT_SERVICE_LABEL
    service_domain: str = _DEFAULT_SERVICE_DOMAIN
    reload_after_set: bool = True


@dataclass
class MacosMtaActionResult(FeatureActionResult):
    """
        Result of a mail-transport write operation.

        :param setting: The Postfix directive targeted, when applicable.
        :param local: The local alias name targeted, when applicable.
    """
    setting: Optional[str] = None
    local: Optional[str] = None


@dataclass
class MacosMtaConfig:
    """
        Parsed Postfix ``main.cf`` directive snapshot.

        :param myhostname: ``myhostname`` directive.
        :param mydomain: ``mydomain`` directive.
        :param myorigin: ``myorigin`` directive.
        :param relayhost: ``relayhost`` directive.
        :param mynetworks: ``mynetworks`` directive.
        :param inet_interfaces: ``inet_interfaces`` directive.
        :param inet_protocols: ``inet_protocols`` directive.
        :param mailbox_size_limit: ``mailbox_size_limit`` directive
                                   (bytes).
        :param message_size_limit: ``message_size_limit`` directive
                                   (bytes).
        :param extras: Every directive that is NOT mapped to a named
                       field, preserving original key/value text.
    """
    myhostname: Optional[str] = None
    mydomain: Optional[str] = None
    myorigin: Optional[str] = None
    relayhost: Optional[str] = None
    mynetworks: Optional[str] = None
    inet_interfaces: Optional[str] = None
    inet_protocols: Optional[str] = None
    mailbox_size_limit: Optional[int] = None
    message_size_limit: Optional[int] = None
    extras: dict = field(default_factory=dict)


@dataclass
class MacosMtaAliasInfo:
    """
        A single ``/etc/aliases`` entry.

        :param local: The local alias (left-hand side).
        :param remote: The remote target list (right-hand side).
    """
    local: str
    remote: str


@dataclass
class MacosMtaQueueStatus:
    """
        Aggregated Postfix mail-queue status.

        :param pending_count: Number of active / pending messages.
        :param deferred_count: Number of deferred messages.
        :param total_size_bytes: Aggregate queue size in bytes.
        :param queue_age_oldest: Arrival-time text of the oldest
                                 message.
    """
    pending_count: int = 0
    deferred_count: int = 0
    total_size_bytes: Optional[int] = None
    queue_age_oldest: Optional[str] = None


class MacosMtaQueryError(Exception):
    """
        Raised when a mail-transport read fails for an unrecoverable
        reason.
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

        err_msg_lines = ["Mail transport query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosMailTransportManager(BaseFeatureManager):
    """
        macOS mail-transport-management manager.  Reads, mutates, and
        operates the Postfix MTA through the injected
        :class:`ICommandRunner`.
    """

    result_class = MacosMtaActionResult
    status_class = MacosMtaActionStatus
    feature_name = "mail-transport-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_mta_config(self) -> MacosMtaConfig:
        """
            Read the live Postfix configuration via ``postconf -n``.

            :returns: The :class:`MacosMtaConfig`.
            :raises MacosMtaQueryError: When ``postconf`` exits
                                        non-zero with non-empty stderr.
        """
        command = [_POSTCONF, "-n"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise MacosMtaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_postconf_n_output(text=run_result.stdout)
        return rtnval

    def list_aliases(self) -> list:
        """
            Enumerate every entry in the aliases file.

            :returns: A ``list[MacosMtaAliasInfo]`` in file order.
                      Empty when the file is missing or contains no
                      entries.
        """
        path = self._typed_config().aliases_file
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        rtnval = _parse_aliases_file(content=content)
        return rtnval

    def get_queue_status(self) -> MacosMtaQueueStatus:
        """
            Read the current Postfix mail-queue status via ``mailq``.

            :returns: The :class:`MacosMtaQueueStatus`.
            :raises MacosMtaQueryError: When ``mailq`` exits non-zero
                                        with non-empty stderr.
        """
        command = [_MAILQ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise MacosMtaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_mailq_output(text=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_mta_config(
        self,
        spec: MacosMtaConfig,
    ) -> MacosMtaActionResult:
        """
            Apply the directive snapshot in ``spec`` to live Postfix.

            For each named field on :class:`MacosMtaConfig` that is
            not None, the manager dispatches ``postconf -e
            <key>=<value>`` when the live value differs.  Settings
            already at the desired value are skipped silently.  After
            all per-setting dispatches complete, ``launchctl kickstart
            -k <domain>/<label>`` is invoked when at least one
            directive changed AND ``config.reload_after_set`` is True.

            :param spec: The :class:`MacosMtaConfig` snapshot.

            :returns: The :class:`MacosMtaActionResult` summarising
                      the per-setting outcomes.
        """
        aggregate_command: list = [_POSTCONF, "-e"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosMtaActionStatus.DRY_RUN,
                command=aggregate_command,
                detail="set_mta_config",
                local=None,
                setting=None,
            )
            return dry

        try:
            existing = self.get_mta_config()
        except MacosMtaQueryError as raised:
            failed = MacosMtaActionResult(
                status=MacosMtaActionStatus.FAILED,
                detail="set_mta_config",
                command=list(aggregate_command),
                local=None,
                setting=None,
                stderr=raised.stderr,
                exit_code=raised.exit_code,
            )
            return failed

        desired_pairs = _config_to_directive_pairs(config=spec)
        existing_pairs = _config_to_directive_pairs(config=existing)

        per_setting_audit: list = []
        any_changed = False
        first_failure: Optional[MacosMtaActionResult] = None

        for setting_key, desired_value in desired_pairs:
            existing_value = existing_pairs.get_first_value_for(
                key=setting_key,
            )
            if existing_value is not None:
                if existing_value == desired_value:
                    per_setting_audit.append(
                        "SKIPPED {}={}".format(setting_key, desired_value),
                    )
                    continue
            per_command = [
                _POSTCONF,
                "-e",
                "{}={}".format(setting_key, desired_value),
            ]
            per_run = self._runner.run(
                list(per_command),
                elevated=self._config.elevated,
            )
            if per_run.exit_code == 0:
                any_changed = True
                per_setting_audit.append(
                    "CHANGED {}={}".format(setting_key, desired_value),
                )
                continue

            failure = MacosMtaActionResult(
                status=MacosMtaActionStatus.FAILED,
                detail="set_mta_config",
                exit_code=per_run.exit_code,
                stdout=per_run.stdout,
                stderr=per_run.stderr,
                command=list(per_command),
                setting=setting_key,
                local=None,
            )
            if first_failure is None:
                first_failure = failure
            per_setting_audit.append(
                "FAILED {}={}".format(setting_key, desired_value),
            )

        if first_failure is not None:
            first_failure.stdout = "\n".join(per_setting_audit)
            return first_failure

        if any_changed and self._typed_config().reload_after_set:
            kickstart_target = "{}/{}".format(
                self._typed_config().service_domain,
                self._typed_config().service_label,
            )
            kickstart_command = [
                _LAUNCHCTL,
                "kickstart",
                "-k",
                kickstart_target,
            ]
            kickstart_run = self._runner.run(
                list(kickstart_command),
                elevated=self._config.elevated,
            )
            per_setting_audit.append(
                "KICKSTART exit={}".format(kickstart_run.exit_code),
            )
            if kickstart_run.exit_code != 0:
                failed = MacosMtaActionResult(
                    status=MacosMtaActionStatus.FAILED,
                    detail="set_mta_config",
                    exit_code=kickstart_run.exit_code,
                    stdout="\n".join(per_setting_audit),
                    stderr=kickstart_run.stderr,
                    command=list(kickstart_command),
                    setting=None,
                    local=None,
                )
                return failed

        if not any_changed:
            skipped = MacosMtaActionResult(
                status=MacosMtaActionStatus.SKIPPED,
                detail="set_mta_config",
                exit_code=0,
                stdout="\n".join(per_setting_audit),
                stderr=None,
                command=aggregate_command,
                setting=None,
                local=None,
            )
            return skipped

        rtnval = MacosMtaActionResult(
            status=MacosMtaActionStatus.SUCCESS,
            detail="set_mta_config",
            exit_code=0,
            stdout="\n".join(per_setting_audit),
            stderr=None,
            command=aggregate_command,
            setting=None,
            local=None,
        )
        return rtnval

    def add_alias(
        self,
        local: str,
        remote: str,
    ) -> MacosMtaActionResult:
        """
            Append (or update) an entry in the aliases file.

            Idempotent: returns ``SKIPPED`` when an entry for ``local``
            with the same ``remote`` already exists.

            :param local: The local alias name.
            :param remote: The remote target list.

            :returns: The :class:`MacosMtaActionResult`.
        """
        path = self._typed_config().aliases_file
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        existing_aliases = _parse_aliases_file(content=existing_content)

        for entry in existing_aliases:
            if entry.local == local:
                if entry.remote == remote:
                    skipped = self._build_result(
                        status=MacosMtaActionStatus.SKIPPED,
                        detail="add_alias:{}".format(local),
                        command=write_command,
                        local=local,
                        setting=None,
                        message_text="alias already at desired value",
                    )
                    return skipped
                break

        new_content = _upsert_alias(
            content=existing_content, local=local, remote=remote,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosMtaActionStatus.DRY_RUN,
                detail="add_alias:{}".format(local),
                command=write_command,
                local=local,
                setting=None,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = MacosMtaActionResult(
                status=MacosMtaActionStatus.FAILED,
                detail="add_alias:{}".format(local),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                local=local,
                setting=None,
            )
            return failed

        rtnval = self._invoke_newaliases(
            base_action="add_alias:{}".format(local),
            local=local,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def remove_alias(
        self,
        local: str,
    ) -> MacosMtaActionResult:
        """
            Strip the entry whose left-hand side equals ``local`` from
            the aliases file.

            Idempotent: returns ``SKIPPED`` when the file is missing or
            no entry matches.

            :param local: The local alias name to remove.

            :returns: The :class:`MacosMtaActionResult`.
        """
        path = self._typed_config().aliases_file
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=MacosMtaActionStatus.SKIPPED,
                detail="remove_alias:{}".format(local),
                command=write_command,
                local=local,
                setting=None,
                message_text="aliases file does not exist",
            )
            return skipped

        new_content = _remove_alias(content=existing, local=local)
        if new_content == existing:
            skipped = self._build_result(
                status=MacosMtaActionStatus.SKIPPED,
                detail="remove_alias:{}".format(local),
                command=write_command,
                local=local,
                setting=None,
                message_text="alias not present",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosMtaActionStatus.DRY_RUN,
                detail="remove_alias:{}".format(local),
                command=write_command,
                local=local,
                setting=None,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = MacosMtaActionResult(
                status=MacosMtaActionStatus.FAILED,
                detail="remove_alias:{}".format(local),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                local=local,
                setting=None,
            )
            return failed

        rtnval = self._invoke_newaliases(
            base_action="remove_alias:{}".format(local),
            local=local,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def test_mail_delivery(
        self,
        to: str,
        from_addr: str,
        subject: str,
    ) -> MacosMtaActionResult:
        """
            Hand a probe message to ``sendmail -t`` for delivery.

            :param to: The recipient address.
            :param from_addr: The envelope-from address.
            :param subject: The message subject.

            :returns: The :class:`MacosMtaActionResult`.
        """
        command = [_SENDMAIL, "-t"]
        payload = _build_probe_message(
            to=to, from_addr=from_addr, subject=subject,
        )
        detail_text = "test_mail_delivery:{}".format(to)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosMtaActionStatus.DRY_RUN,
                detail=detail_text,
                command=command,
                local=None,
                setting=None,
            )
            dry.stdout = payload
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
            stdin=payload,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            detail=detail_text,
            local=None,
            setting=None,
        )
        return rtnval

    def flush_queue(self) -> MacosMtaActionResult:
        """
            Attempt immediate delivery of every queued message via
            ``postqueue -f``.

            :returns: The :class:`MacosMtaActionResult`.
        """
        command = [_POSTQUEUE, "-f"]
        detail_text = "flush_queue"

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosMtaActionStatus.DRY_RUN,
                detail=detail_text,
                command=command,
                local=None,
                setting=None,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            detail=detail_text,
            local=None,
            setting=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosMtaManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed :class:`MacosMtaManagerConfig`.
        """
        rtnval = self._config
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosMtaActionStatus,
        detail: Optional[str],
        command: list,
        local: Optional[str],
        setting: Optional[str],
        message_text: Optional[str] = None,
    ) -> MacosMtaActionResult:
        """
            Build a result without invoking the runner.

            :param status: Outcome status.
            :param detail: Short identifier.
            :param command: Would-have-been argv.
            :param local: Alias local name, when applicable.
            :param setting: Postfix directive name, when applicable.
            :param message_text: Optional human-readable detail.

            :returns: The :class:`MacosMtaActionResult`.
        """
        rtnval = MacosMtaActionResult(
            status=status,
            detail=detail,
            command=list(command),
            local=local,
            setting=setting,
        )
        if message_text is not None:
            rtnval.stderr = message_text
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        detail: Optional[str],
        local: Optional[str],
        setting: Optional[str],
    ) -> MacosMtaActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`MacosMtaActionResult`.

            :param run_result: The runner's :class:`RunResult`.
            :param command: The argv that ran.
            :param detail: Short identifier.
            :param local: Alias local name, when applicable.
            :param setting: Postfix directive name, when applicable.

            :returns: The :class:`MacosMtaActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = MacosMtaActionStatus.SUCCESS
        else:
            mapped_status = MacosMtaActionStatus.FAILED

        rtnval = MacosMtaActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            local=local,
            setting=setting,
        )
        return rtnval

    def _invoke_newaliases(
        self,
        *,
        base_action: str,
        local: str,
        primary_run,
        primary_command: list,
    ) -> MacosMtaActionResult:
        """
            Rebuild the aliases database via ``newaliases`` after a
            successful aliases-file mutation.

            :param base_action: The detail string to embed.
            :param local: The alias local name.
            :param primary_run: The :class:`RunResult` from the
                                preceding ``tee`` call.
            :param primary_command: The argv that ran first.

            :returns: A :class:`MacosMtaActionResult`.
        """
        newaliases_command = [_NEWALIASES]
        newaliases_run = self._runner.run(
            list(newaliases_command),
            elevated=self._config.elevated,
        )
        if newaliases_run.exit_code != 0:
            rtnval = MacosMtaActionResult(
                status=MacosMtaActionStatus.FAILED,
                detail=base_action,
                exit_code=newaliases_run.exit_code,
                stdout=newaliases_run.stdout,
                stderr=newaliases_run.stderr,
                command=list(newaliases_command),
                local=local,
                setting=None,
            )
            return rtnval

        rtnval = MacosMtaActionResult(
            status=MacosMtaActionStatus.SUCCESS,
            detail=base_action,
            exit_code=primary_run.exit_code,
            stdout=primary_run.stdout,
            stderr=primary_run.stderr,
            command=list(primary_command),
            local=local,
            setting=None,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — directive pairs & parsing
# ----------------------------------------------------------------------


class _DirectivePairs:
    """
        Ordered list of ``(key, value)`` directive pairs with a tiny
        ``get_first_value_for`` helper.
    """

    def __init__(self, *, pairs: list) -> None:
        """
            :param pairs: Ordered list of ``(key, value)`` tuples.
        """
        self._pairs: list = list(pairs)
        return

    def get_first_value_for(self, *, key: str) -> Optional[str]:
        """
            Return the value of the first pair whose key equals ``key``.

            :param key: The directive name.

            :returns: The value, or ``None`` when no such pair exists.
        """
        for pair_key, pair_value in self._pairs:
            if pair_key == key:
                rtnval: Optional[str] = pair_value
                return rtnval
        rtnval = None
        return rtnval

    def __iter__(self):
        return iter(self._pairs)

    def __len__(self) -> int:
        rtnval = len(self._pairs)
        return rtnval


def _config_to_directive_pairs(
    *,
    config: MacosMtaConfig,
) -> _DirectivePairs:
    """
        Flatten a :class:`MacosMtaConfig` into ``(key, value)`` pairs.

        :param config: The config snapshot.

        :returns: A :class:`_DirectivePairs`.
    """
    flattened: list = []
    for key in _KNOWN_MAIN_KEYS:
        attr_value = getattr(config, key)
        if attr_value is None:
            continue
        if key in _INT_KEYS:
            text_value = str(int(attr_value))
        else:
            text_value = str(attr_value)
        flattened.append((key, text_value))

    for extra_key, extra_value in config.extras.items():
        flattened.append((extra_key, str(extra_value)))

    rtnval = _DirectivePairs(pairs=flattened)
    return rtnval


def _parse_postconf_n_output(*, text: str) -> MacosMtaConfig:
    """
        Parse ``postconf -n`` output into a :class:`MacosMtaConfig`.

        :param text: The captured stdout.

        :returns: The :class:`MacosMtaConfig`.
    """
    rtnval = MacosMtaConfig()
    for raw in text.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        before, after = stripped.split("=", 1)
        key = before.strip()
        value = after.strip()
        _apply_directive(config=rtnval, key=key, value=value)

    return rtnval


def _apply_directive(
    *,
    config: MacosMtaConfig,
    key: str,
    value: str,
) -> None:
    """
        Place a single directive into the named field or ``extras``.

        :param config: The config to mutate.
        :param key: The directive name.
        :param value: The directive value.
    """
    if key in _INT_KEYS:
        try:
            int_value = int(value)
        except ValueError:
            int_value = None
        if int_value is not None:
            setattr(config, key, int_value)
            return
        config.extras[key] = value
        return

    if key in _KNOWN_MAIN_KEYS:
        setattr(config, key, value)
        return

    config.extras[key] = value
    return


# ----------------------------------------------------------------------
# Module-level helpers — aliases parser / renderer
# ----------------------------------------------------------------------


def _parse_aliases_file(*, content: str) -> list:
    """
        Parse the aliases file content into entries.

        :param content: Raw file content.

        :returns: A ``list[MacosMtaAliasInfo]`` in file order.
    """
    rtnval: list = []
    for raw in content.splitlines():
        if len(raw.strip()) == 0:
            continue
        if raw.strip().startswith("#"):
            continue
        if raw[0] in (" ", "\t") and len(rtnval) > 0:
            previous = rtnval[-1]
            previous.remote = "{} {}".format(
                previous.remote, raw.strip(),
            )
            continue
        if ":" not in raw:
            continue
        before, after = raw.split(":", 1)
        local = before.strip()
        remote = after.strip()
        if len(local) == 0:
            continue
        rtnval.append(MacosMtaAliasInfo(local=local, remote=remote))
    return rtnval


def _upsert_alias(
    *,
    content: str,
    local: str,
    remote: str,
) -> str:
    """
        Return content with ``local: remote`` upserted.

        :param content: Existing file content.
        :param local: Local alias name.
        :param remote: Remote target list.

        :returns: The updated content.
    """
    new_lines: list = []
    replaced = False
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            new_lines.append(raw)
            continue
        if ":" not in raw:
            new_lines.append(raw)
            continue
        before, _ = raw.split(":", 1)
        existing_local = before.strip()
        if existing_local == local:
            new_lines.append("{}: {}".format(local, remote))
            replaced = True
            continue
        new_lines.append(raw)

    if not replaced:
        new_lines.append("{}: {}".format(local, remote))

    body = "\n".join(new_lines)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_alias(
    *,
    content: str,
    local: str,
) -> str:
    """
        Return content with every entry whose left-hand side matches
        ``local`` stripped.

        :param content: Existing file content.
        :param local: Local alias name to remove.

        :returns: The updated content.
    """
    new_lines: list = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            new_lines.append(raw)
            continue
        if ":" not in raw:
            new_lines.append(raw)
            continue
        before, _ = raw.split(":", 1)
        existing_local = before.strip()
        if existing_local == local:
            continue
        new_lines.append(raw)

    body = "\n".join(new_lines)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


# ----------------------------------------------------------------------
# Module-level helpers — mailq parser
# ----------------------------------------------------------------------


_MAILQ_FOOTER_PATTERN = re.compile(
    r"--\s*(?P<bytes>\d+)\s+Kbytes\s+in\s+(?P<count>\d+)\s+Request",
    re.IGNORECASE,
)
_MAILQ_HEAD_PATTERN = re.compile(
    r"^(?P<qid>[A-Za-z0-9]+)(?P<marker>[*!]?)\s+(?P<size>\d+)\s+(?P<arrival>\S.*?\S)\s+\S+@\S+",
)


def _parse_mailq_output(*, text: str) -> MacosMtaQueueStatus:
    """
        Parse ``mailq`` output into a :class:`MacosMtaQueueStatus`.

        :param text: The captured stdout.

        :returns: The :class:`MacosMtaQueueStatus`.
    """
    rtnval = MacosMtaQueueStatus()

    if "Mail queue is empty" in text:
        return rtnval

    pending = 0
    deferred = 0
    oldest_arrival: Optional[str] = None

    for raw in text.splitlines():
        match = _MAILQ_HEAD_PATTERN.match(raw)
        if match is None:
            continue
        marker = match.group("marker")
        if marker == "*":
            pending = pending + 1
        else:
            deferred = deferred + 1
        if oldest_arrival is None:
            oldest_arrival = match.group("arrival")

    rtnval.pending_count = pending
    rtnval.deferred_count = deferred
    rtnval.queue_age_oldest = oldest_arrival

    footer_match = _MAILQ_FOOTER_PATTERN.search(text)
    if footer_match is not None:
        kbytes = int(footer_match.group("bytes"))
        rtnval.total_size_bytes = kbytes * 1024

    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — probe message
# ----------------------------------------------------------------------


def _build_probe_message(
    *,
    to: str,
    from_addr: str,
    subject: str,
) -> str:
    """
        Build the RFC 5322 payload handed to ``sendmail -t`` on stdin.

        :param to: Recipient address.
        :param from_addr: Sender address.
        :param subject: Subject text.

        :returns: The fully rendered message.
    """
    lines = [
        "Subject: {}".format(subject),
        "From: {}".format(from_addr),
        "To: {}".format(to),
        "",
        "codecraftai-sysman mail-transport probe",
        "",
    ]
    rtnval = "\n".join(lines)
    return rtnval
