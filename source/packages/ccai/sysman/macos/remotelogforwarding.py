"""
    macOS remote-log-forwarding feature.

    Implements the cross-OS remote-log-forwarding contract for
    macOS using the legacy ``/etc/syslog.conf`` mechanism that
    ``syslogd(8)`` still honours on modern macOS releases.  Every
    verb is dispatched through an :class:`ICommandRunner` so the
    manager works identically under local and SSH execution.

    macOS unified-logging limitation:
        macOS unified logging (the ``log show`` / ``log stream``
        / ``os_log`` surface) does NOT have a native remote-syslog
        forwarder.  Apple does not provide a supported mechanism
        for shipping unified-logging messages off-host.  The only
        forwarding pipeline the operating system exposes is
        ``/etc/syslog.conf`` plus ``syslogd``, and that pipeline
        carries only the messages that are explicitly routed
        through the legacy ``syslog(3)`` / ``asl_log_message``
        facility — a strict subset of unified-logging messages.

        Operators who need full unified-logging coverage MUST
        deploy a third-party shipper (Filebeat, NXLog, or Fluent
        Bit) alongside this module.  This module exists for the
        legacy-syslog use cases (small deployments, integration
        testing, cross-OS-contract parity) and does not attempt
        to bridge the unified-logging gap.

    File I/O policy:
        Every file read / write is performed through the injected
        :class:`ICommandRunner` (``cat``, ``tee``, ``test -e``).
        This module does NOT consume an :class:`IFileOperator`.

    Drop-in naming:
        Every managed forwarder lives inside a
        sentinel-bracketed block in ``/etc/syslog.conf``.  The
        sentinel header / footer pair carries the configured
        ``prefix`` plus the forwarder identifier so that the
        parser can locate the block to update without disturbing
        operator-authored entries.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads,
        idempotency probes, and ``test_forwarder`` never elevate.

    Service reload:
        After every successful mutation the manager dispatches
        ``launchctl kickstart -k <syslogd_label>`` to terminate
        and respawn ``syslogd`` so that the new configuration
        takes effect immediately.

    Documented limitations:
        * Apple ``syslogd`` is UDP-only.  Specs that request
          ``protocol="tcp"`` or ``protocol="tls"`` still render a
          UDP action line; the result's ``message`` field flags
          the downgrade.
        * Disk-spool, queue tuning, and structured output are not
          implemented.
        * ``asl_log_message`` predicate filters are not
          implemented.
"""

import os
import time

from dataclasses import dataclass
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

_TEE = "tee"
_CAT = "cat"
_LOGGER = "logger"
_LAUNCHCTL = "launchctl"

_DEFAULT_SYSLOG_CONF = "/etc/syslog.conf"
_DEFAULT_SYSLOGD_LABEL = "system/com.apple.syslogd"
_DEFAULT_PREFIX = "codecraftai-fwd"

_PROTOCOL_UDP = "udp"
_PROTOCOL_TCP = "tcp"
_PROTOCOL_TLS = "tls"

_VALID_PROTOCOLS = frozenset({_PROTOCOL_UDP, _PROTOCOL_TCP, _PROTOCOL_TLS})


MacosForwarderActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class MacosForwarderManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosRemoteLogForwardingManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param syslog_conf: Path to the syslog config file.
                            Defaults to ``/etc/syslog.conf``.
        :param syslogd_label: ``launchctl`` service label used
                              for the post-write kickstart.
                              Defaults to
                              ``system/com.apple.syslogd``.
        :param prefix: Sentinel prefix used in the bracketed
                       block headers (``# >>> <prefix>:<id> >>>``).
                       Defaults to ``codecraftai-fwd``.
    """
    syslog_conf: str = _DEFAULT_SYSLOG_CONF
    syslogd_label: str = _DEFAULT_SYSLOGD_LABEL
    prefix: str = _DEFAULT_PREFIX


@dataclass
class MacosForwarderActionResult(FeatureActionResult):
    """
        Result of a :class:`MacosRemoteLogForwardingManager`
        write verb.

        :param forwarder_id: The forwarder identifier targeted.
        :param path: The configured syslog.conf path the action
                     targeted, when applicable.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` / ``FAILED`` outcomes (and
                        the TCP / TLS downgrade notice).
    """
    forwarder_id: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosForwarderSpec:
    """
        Declarative specification for a single legacy syslog
        forwarder.

        :param id: File-name-friendly identifier embedded in the
                   sentinel block header.
        :param host: Collector hostname or IP.
        :param port: Collector port.  Defaults to 514.
        :param protocol: ``"udp"``, ``"tcp"``, or ``"tls"``.
                         Apple ``syslogd`` only honours UDP; the
                         other tokens render a UDP action line
                         and flag the downgrade on the result.
                         Defaults to ``"udp"``.
        :param selector: syslog facility.severity selector.
                         Defaults to ``"*.*"``.
        :param tls_ca_cert: Accepted for cross-OS parity but
                            unused — Apple ``syslogd`` does not
                            support TLS forwarding.
        :param enabled: When ``False``, the rendered block has
                        every action line commented out so that
                        ``syslogd`` ignores it.  Defaults to
                        ``True``.
    """
    id: str
    host: str
    port: int = 514
    protocol: str = _PROTOCOL_UDP
    selector: str = "*.*"
    tls_ca_cert: Optional[str] = None
    enabled: bool = True


@dataclass
class MacosForwarderInfo:
    """
        One row from
        :meth:`MacosRemoteLogForwardingManager.list_forwarders`.

        :param id: Forwarder identifier (extracted from the
                   sentinel block header).
        :param host: Collector hostname or IP parsed from the
                     action line.
        :param port: Collector port parsed from the action line.
        :param protocol: ``"udp"`` (Apple ``syslogd`` is
                         UDP-only).
        :param enabled: True when at least one action line in
                        the block is uncommented.
        :param path: Path of the syslog.conf file the block
                     lives in.
        :param selector_or_query: syslog selector parsed from
                                  the action line.  ``None``
                                  when the parser could not
                                  extract it.
    """
    id: str
    host: str
    port: int
    protocol: str
    enabled: bool
    path: Optional[str]
    selector_or_query: Optional[str]


@dataclass
class MacosForwarderStatus:
    """
        Result of
        :meth:`MacosRemoteLogForwardingManager.get_forwarder_status`.

        :param id: The forwarder identifier queried.
        :param enabled: True when the matching block is present
                        and contains at least one uncommented
                        action line.
        :param last_event_time: Reserved for future ``os_log``
                                integration.  ``None`` for the
                                pilot.
        :param error_count: Reserved for future ``os_log``
                            integration.  ``None`` for the pilot.
        :param bytes_sent: Reserved for future ``os_log``
                           integration.  ``None`` for the pilot.
    """
    id: str
    enabled: bool
    last_event_time: Optional[str] = None
    error_count: Optional[int] = None
    bytes_sent: Optional[int] = None


@dataclass
class MacosForwarderTestResult(FeatureActionResult):
    """
        Result of
        :meth:`MacosRemoteLogForwardingManager.test_forwarder`.

        :param forwarder_id: The forwarder identifier the probe
                             targeted.
        :param test_message_id: The unique identifier embedded
                                in the dispatched ``logger``
                                message.
    """
    forwarder_id: str = ""
    test_message_id: Optional[str] = None


class MacosForwarderQueryError(Exception):
    """
        Raised when a remote-log-forwarding read fails for a
        reason other than the syslog.conf file simply being
        missing.
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

        err_msg_lines = ["Forwarder query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# MacosRemoteLogForwardingManager
# ----------------------------------------------------------------------


class MacosRemoteLogForwardingManager(BaseFeatureManager):
    """
        macOS legacy-syslog forwarder manager.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.  See the module docstring for the
        prominent unified-logging limitation: this manager covers
        only legacy ``syslog(3)`` traffic, NOT unified logging.
    """

    result_class = MacosForwarderActionResult
    status_class = MacosForwarderActionStatus
    feature_name = "remote-log-forwarding"

    def __init__(
        self,
        runner,
        config: Optional[MacosForwarderManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`MacosForwarderManagerConfig`
                           when omitted.
        """
        if config is None:
            config = MacosForwarderManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_forwarder(
        self,
        spec: MacosForwarderSpec,
    ) -> MacosForwarderActionResult:
        """
            Upsert a sentinel-bracketed forwarder block in
            ``/etc/syslog.conf``.

            Idempotent: returns ``SKIPPED`` when the merged file
            body matches the on-disk body byte-for-byte.

            :param spec: The :class:`MacosForwarderSpec` to
                         render.

            :returns: The :class:`MacosForwarderActionResult`.
        """
        validation_failure = _validate_spec(spec=spec)
        if validation_failure is not None:
            failed = self._build_result(
                status=MacosForwarderActionStatus.FAILED,
                forwarder_id=spec.id,
                path=self._typed_config().syslog_conf,
                command=[_TEE, self._typed_config().syslog_conf],
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        cfg = self._typed_config()
        existing_body = self._read_file(path=cfg.syslog_conf)
        if existing_body is None:
            existing_body = ""

        block_body = _render_block(spec=spec, prefix=cfg.prefix)
        merged_body = _upsert_block(
            body=existing_body,
            block_body=block_body,
            forwarder_id=spec.id,
            prefix=cfg.prefix,
        )

        message: Optional[str] = None
        if spec.protocol != _PROTOCOL_UDP:
            message = (
                "Apple syslogd does not implement TCP/TLS "
                "forwarding; downgraded to UDP. For TCP/TLS "
                "coverage use Filebeat / NXLog / Fluent Bit."
            )

        write_command = [_TEE, cfg.syslog_conf]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosForwarderActionStatus.DRY_RUN,
                forwarder_id=spec.id,
                path=cfg.syslog_conf,
                command=write_command,
                message=message,
            )
            return dry

        if merged_body == existing_body:
            skipped = self._build_result(
                status=MacosForwarderActionStatus.SKIPPED,
                forwarder_id=spec.id,
                path=cfg.syslog_conf,
                command=write_command,
                message="forwarder already matches desired content",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=cfg.elevated,
            stdin=merged_body,
        )

        if run_result.exit_code != 0:
            failed = MacosForwarderActionResult(
                status=MacosForwarderActionStatus.FAILED,
                detail=spec.id,
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                forwarder_id=spec.id,
                path=cfg.syslog_conf,
                message="tee failed; syslog.conf not written",
            )
            return failed

        reload_result = self._kickstart_syslogd()
        if reload_result.exit_code != 0:
            failed = MacosForwarderActionResult(
                status=MacosForwarderActionStatus.FAILED,
                detail=spec.id,
                exit_code=reload_result.exit_code,
                stdout=reload_result.stdout,
                stderr=reload_result.stderr,
                command=list(write_command),
                forwarder_id=spec.id,
                path=cfg.syslog_conf,
                message="syslogd kickstart failed after write",
            )
            return failed

        rtnval = MacosForwarderActionResult(
            status=MacosForwarderActionStatus.SUCCESS,
            detail=spec.id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            forwarder_id=spec.id,
            path=cfg.syslog_conf,
            message=message,
        )
        return rtnval

    def remove_forwarder(self, id: str) -> MacosForwarderActionResult:
        """
            Strip the matching sentinel-bracketed forwarder block
            from ``/etc/syslog.conf``.

            Idempotent: returns ``SKIPPED`` when no matching
            block is present.

            :param id: The forwarder identifier.

            :returns: The :class:`MacosForwarderActionResult`.
        """
        cfg = self._typed_config()
        existing_body = self._read_file(path=cfg.syslog_conf)
        if existing_body is None:
            existing_body = ""

        new_body = _remove_block(
            body=existing_body, forwarder_id=id, prefix=cfg.prefix,
        )
        write_command = [_TEE, cfg.syslog_conf]

        if new_body == existing_body:
            skipped = self._build_result(
                status=MacosForwarderActionStatus.SKIPPED,
                forwarder_id=id,
                path=cfg.syslog_conf,
                command=write_command,
                message="forwarder block already absent",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosForwarderActionStatus.DRY_RUN,
                forwarder_id=id,
                path=cfg.syslog_conf,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=cfg.elevated,
            stdin=new_body,
        )

        if run_result.exit_code != 0:
            failed = MacosForwarderActionResult(
                status=MacosForwarderActionStatus.FAILED,
                detail=id,
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                forwarder_id=id,
                path=cfg.syslog_conf,
                message="tee failed; syslog.conf not written",
            )
            return failed

        reload_result = self._kickstart_syslogd()
        if reload_result.exit_code != 0:
            failed = MacosForwarderActionResult(
                status=MacosForwarderActionStatus.FAILED,
                detail=id,
                exit_code=reload_result.exit_code,
                stdout=reload_result.stdout,
                stderr=reload_result.stderr,
                command=list(write_command),
                forwarder_id=id,
                path=cfg.syslog_conf,
                message="syslogd kickstart failed after removal",
            )
            return failed

        rtnval = MacosForwarderActionResult(
            status=MacosForwarderActionStatus.SUCCESS,
            detail=id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            forwarder_id=id,
            path=cfg.syslog_conf,
        )
        return rtnval

    def enable_forwarder(self, id: str) -> MacosForwarderActionResult:
        """
            Uncomment every action line in the matching forwarder
            block.

            Idempotent: returns ``SKIPPED`` when the block's
            action lines are already uncommented.

            :param id: The forwarder identifier.

            :returns: The :class:`MacosForwarderActionResult`.
        """
        rtnval = self._toggle_block_state(forwarder_id=id, enabled=True)
        return rtnval

    def disable_forwarder(self, id: str) -> MacosForwarderActionResult:
        """
            Comment out every action line in the matching
            forwarder block.

            Idempotent: returns ``SKIPPED`` when the block's
            action lines are already commented.

            :param id: The forwarder identifier.

            :returns: The :class:`MacosForwarderActionResult`.
        """
        rtnval = self._toggle_block_state(forwarder_id=id, enabled=False)
        return rtnval

    def test_forwarder(self, id: str) -> MacosForwarderTestResult:
        """
            Send a test message into the legacy syslog pipeline
            so that downstream forwarders pick it up.

            Dispatches ``logger -t codecraftai-test
            'forwarder-test-<timestamp>'`` without elevation.

            :param id: The forwarder identifier the test is
                       targeted at.  Apple ``syslogd`` does not
                       address forwarders individually; the
                       dispatched message is routed to every
                       configured forwarder via the legacy
                       facility.

            :returns: The :class:`MacosForwarderTestResult`.
        """
        timestamp = int(time.time())
        message_id = "forwarder-test-{}".format(timestamp)
        command = [_LOGGER, "-t", "codecraftai-test", message_id]

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 0:
            mapped_status = MacosForwarderActionStatus.SUCCESS
        else:
            mapped_status = MacosForwarderActionStatus.FAILED

        rtnval = MacosForwarderTestResult(
            status=mapped_status,
            detail=id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            forwarder_id=id,
            test_message_id=message_id,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_forwarders(self) -> list[MacosForwarderInfo]:
        """
            Enumerate every managed forwarder by parsing
            ``/etc/syslog.conf``.

            :returns: One :class:`MacosForwarderInfo` per
                      sentinel-bracketed managed block.

            :raises MacosForwarderQueryError: When ``cat`` fails
                                              with a non-zero
                                              exit and a non-empty
                                              stderr.
        """
        cfg = self._typed_config()
        command = [_CAT, cfg.syslog_conf]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            stderr_text = run_result.stderr.strip()
            if len(stderr_text) > 0 and "no such file" not in stderr_text.lower():
                raise MacosForwarderQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval: list[MacosForwarderInfo] = []
            return rtnval

        blocks = _extract_blocks(
            body=run_result.stdout, prefix=cfg.prefix,
        )
        rtnval = []
        for block in blocks:
            host, port, protocol, selector, enabled = _parse_block(
                block_body=block.body,
            )
            info = MacosForwarderInfo(
                id=block.forwarder_id,
                host=host,
                port=port,
                protocol=protocol,
                enabled=enabled,
                path=cfg.syslog_conf,
                selector_or_query=selector,
            )
            rtnval.append(info)
        return rtnval

    def get_forwarder_status(self, id: str) -> MacosForwarderStatus:
        """
            Report a forwarder's current enabled / disabled
            state.

            :param id: The forwarder identifier.

            :returns: The :class:`MacosForwarderStatus`.
                      ``enabled`` is ``False`` when the block is
                      not present.
        """
        cfg = self._typed_config()
        existing_body = self._read_file(path=cfg.syslog_conf)
        if existing_body is None:
            existing_body = ""

        blocks = _extract_blocks(body=existing_body, prefix=cfg.prefix)
        match = None
        for block in blocks:
            if block.forwarder_id == id:
                match = block
                break

        if match is None:
            rtnval = MacosForwarderStatus(
                id=id,
                enabled=False,
                last_event_time=None,
                error_count=None,
                bytes_sent=None,
            )
            return rtnval

        _, _, _, _, enabled = _parse_block(block_body=match.body)
        rtnval = MacosForwarderStatus(
            id=id,
            enabled=enabled,
            last_event_time=None,
            error_count=None,
            bytes_sent=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _toggle_block_state(
        self,
        *,
        forwarder_id: str,
        enabled: bool,
    ) -> MacosForwarderActionResult:
        """
            Flip the enabled / disabled state of a managed block.

            :param forwarder_id: The forwarder identifier.
            :param enabled: True to enable; False to disable.

            :returns: The :class:`MacosForwarderActionResult`.
        """
        cfg = self._typed_config()
        existing_body = self._read_file(path=cfg.syslog_conf)
        if existing_body is None:
            existing_body = ""

        new_body, found = _toggle_block_enabled(
            body=existing_body,
            forwarder_id=forwarder_id,
            prefix=cfg.prefix,
            enabled=enabled,
        )

        write_command = [_TEE, cfg.syslog_conf]

        if not found:
            failed = self._build_result(
                status=MacosForwarderActionStatus.FAILED,
                forwarder_id=forwarder_id,
                path=cfg.syslog_conf,
                command=write_command,
                message="forwarder block not found",
                stderr="forwarder_block_missing",
            )
            return failed

        if new_body == existing_body:
            if enabled:
                msg = "forwarder already enabled"
            else:
                msg = "forwarder already disabled"
            skipped = self._build_result(
                status=MacosForwarderActionStatus.SKIPPED,
                forwarder_id=forwarder_id,
                path=cfg.syslog_conf,
                command=write_command,
                message=msg,
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosForwarderActionStatus.DRY_RUN,
                forwarder_id=forwarder_id,
                path=cfg.syslog_conf,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=cfg.elevated,
            stdin=new_body,
        )

        if run_result.exit_code != 0:
            failed = MacosForwarderActionResult(
                status=MacosForwarderActionStatus.FAILED,
                detail=forwarder_id,
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                forwarder_id=forwarder_id,
                path=cfg.syslog_conf,
                message="tee failed; syslog.conf not written",
            )
            return failed

        reload_result = self._kickstart_syslogd()
        if reload_result.exit_code != 0:
            failed = MacosForwarderActionResult(
                status=MacosForwarderActionStatus.FAILED,
                detail=forwarder_id,
                exit_code=reload_result.exit_code,
                stdout=reload_result.stdout,
                stderr=reload_result.stderr,
                command=list(write_command),
                forwarder_id=forwarder_id,
                path=cfg.syslog_conf,
                message="syslogd kickstart failed after toggle",
            )
            return failed

        rtnval = MacosForwarderActionResult(
            status=MacosForwarderActionStatus.SUCCESS,
            detail=forwarder_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            forwarder_id=forwarder_id,
            path=cfg.syslog_conf,
        )
        return rtnval

    def _typed_config(self) -> MacosForwarderManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosForwarderManagerConfig`.

            :returns: The active config typed.
        """
        if isinstance(self._config, MacosForwarderManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosForwarderManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      the file is missing or unreadable.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _kickstart_syslogd(self):
        """
            Dispatch ``launchctl kickstart -k <syslogd_label>``.

            :returns: The runner's :class:`RunResult`.
        """
        cfg = self._typed_config()
        command = [_LAUNCHCTL, "kickstart", "-k", cfg.syslogd_label]
        rtnval = self._runner.run(
            list(command), elevated=cfg.elevated,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosForwarderActionStatus,
        forwarder_id: Optional[str],
        path: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosForwarderActionResult:
        """
            Construct a :class:`MacosForwarderActionResult`
            without invoking the runner.

            :param status: The outcome status.
            :param forwarder_id: The forwarder identifier to echo
                                 onto the result.
            :param path: The syslog.conf path to echo onto the
                         result.
            :param command: The would-have-been argv (copied for
                            capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`MacosForwarderActionResult`.
        """
        rtnval = MacosForwarderActionResult(
            status=status,
            detail=forwarder_id,
            command=list(command),
            forwarder_id=forwarder_id,
            path=path,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


@dataclass
class _ManagedBlock:
    """
        A discovered sentinel-bracketed managed block.
    """
    forwarder_id: str
    body: str          # full block body including sentinel lines
    start_index: int   # 0-based line index of the header
    end_index: int     # 0-based line index of the footer


def _validate_spec(*, spec: MacosForwarderSpec) -> Optional[str]:
    """
        Validate a spec before the renderer runs.

        :param spec: The :class:`MacosForwarderSpec` to validate.

        :returns: ``None`` when the spec is valid; an error
                  message string when it is not.
    """
    if spec.id is None or len(spec.id) == 0:
        rtnval: Optional[str] = "spec.id is required"
        return rtnval
    if spec.host is None or len(spec.host) == 0:
        rtnval = "spec.host is required"
        return rtnval
    if spec.port is None:
        rtnval = "spec.port is required"
        return rtnval
    if spec.port <= 0:
        rtnval = "spec.port must be positive"
        return rtnval
    if spec.protocol not in _VALID_PROTOCOLS:
        rtnval = "spec.protocol must be one of {}".format(
            sorted(_VALID_PROTOCOLS),
        )
        return rtnval

    rtnval = None
    return rtnval


def _render_block(
    *,
    spec: MacosForwarderSpec,
    prefix: str,
) -> str:
    """
        Render a :class:`MacosForwarderSpec` into a
        sentinel-bracketed block body.

        :param spec: The :class:`MacosForwarderSpec` to render.
        :param prefix: The configured sentinel prefix.

        :returns: The full block body as a string ending in
                  ``\\n``.
    """
    action_target = "@{}:{}".format(spec.host, spec.port)
    action_line = "{} {}".format(spec.selector, action_target)
    if spec.enabled:
        active_action = action_line
    else:
        active_action = "# " + action_line

    header = "# >>> {}:{} >>>".format(prefix, spec.id)
    footer = "# <<< {}:{} <<<".format(prefix, spec.id)
    # The descriptive comment line uses a "#@ " marker so the
    # toggle helper can distinguish it from a commented-out
    # action line (which begins with "# <selector> @host:port").
    comment = "#@ render: {} {}".format(spec.selector, action_target)

    body_lines: list[str] = []
    body_lines.append(header)
    body_lines.append(comment)
    body_lines.append(active_action)
    body_lines.append(footer)

    rtnval = "\n".join(body_lines) + "\n"
    return rtnval


def _block_header(*, prefix: str, forwarder_id: str) -> str:
    """
        Build the sentinel header line for a forwarder block.
    """
    rtnval = "# >>> {}:{} >>>".format(prefix, forwarder_id)
    return rtnval


def _block_footer(*, prefix: str, forwarder_id: str) -> str:
    """
        Build the sentinel footer line for a forwarder block.
    """
    rtnval = "# <<< {}:{} <<<".format(prefix, forwarder_id)
    return rtnval


def _extract_blocks(*, body: str, prefix: str) -> list:
    """
        Walk ``body`` and return every managed block.

        :param body: Raw syslog.conf body.
        :param prefix: The configured sentinel prefix.

        :returns: A list of :class:`_ManagedBlock`.
    """
    rtnval: list = []
    if body is None or len(body) == 0:
        return rtnval

    lines = body.splitlines()
    header_marker = "# >>> {}:".format(prefix)
    footer_marker = "# <<< {}:".format(prefix)

    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if line.startswith(header_marker) and line.endswith(" >>>"):
            after_marker = line[len(header_marker):]
            forwarder_id = after_marker[: -len(" >>>")]
            footer_index = -1
            for inner in range(index + 1, total):
                inner_line = lines[inner]
                if inner_line.startswith(footer_marker) and inner_line.endswith(" <<<"):
                    inner_id = inner_line[
                        len(footer_marker): -len(" <<<")
                    ]
                    if inner_id == forwarder_id:
                        footer_index = inner
                        break
            if footer_index == -1:
                index = index + 1
                continue
            block_lines = lines[index: footer_index + 1]
            block_body = "\n".join(block_lines) + "\n"
            block = _ManagedBlock(
                forwarder_id=forwarder_id,
                body=block_body,
                start_index=index,
                end_index=footer_index,
            )
            rtnval.append(block)
            index = footer_index + 1
            continue
        index = index + 1

    return rtnval


def _upsert_block(
    *,
    body: str,
    block_body: str,
    forwarder_id: str,
    prefix: str,
) -> str:
    """
        Insert or replace ``block_body`` inside ``body``.

        :param body: The current syslog.conf body.
        :param block_body: The rendered block body to insert.
        :param forwarder_id: The forwarder identifier.
        :param prefix: The configured sentinel prefix.

        :returns: The merged body.
    """
    if body is None:
        body = ""

    blocks = _extract_blocks(body=body, prefix=prefix)
    match = None
    for block in blocks:
        if block.forwarder_id == forwarder_id:
            match = block
            break

    if match is None:
        if len(body) > 0 and not body.endswith("\n"):
            rtnval = body + "\n" + block_body
            return rtnval
        rtnval = body + block_body
        return rtnval

    lines = body.splitlines()
    before = lines[: match.start_index]
    after = lines[match.end_index + 1:]
    new_block_lines = block_body.rstrip("\n").splitlines()

    merged_lines: list[str] = []
    merged_lines.extend(before)
    merged_lines.extend(new_block_lines)
    merged_lines.extend(after)

    rtnval = "\n".join(merged_lines)
    if body.endswith("\n"):
        rtnval = rtnval + "\n"
    return rtnval


def _remove_block(
    *,
    body: str,
    forwarder_id: str,
    prefix: str,
) -> str:
    """
        Remove the matching block from ``body`` if present.

        :param body: The current syslog.conf body.
        :param forwarder_id: The forwarder identifier.
        :param prefix: The configured sentinel prefix.

        :returns: The body with the block stripped.  Returns
                  ``body`` unchanged when no match is found.
    """
    if body is None or len(body) == 0:
        return body

    blocks = _extract_blocks(body=body, prefix=prefix)
    match = None
    for block in blocks:
        if block.forwarder_id == forwarder_id:
            match = block
            break

    if match is None:
        return body

    lines = body.splitlines()
    before = lines[: match.start_index]
    after = lines[match.end_index + 1:]
    merged: list[str] = []
    merged.extend(before)
    merged.extend(after)

    rtnval = "\n".join(merged)
    if body.endswith("\n") and len(rtnval) > 0:
        rtnval = rtnval + "\n"
    return rtnval


def _toggle_block_enabled(
    *,
    body: str,
    forwarder_id: str,
    prefix: str,
    enabled: bool,
) -> tuple:
    """
        Toggle the comment status of every action line within
        the matching block.

        Comment lines that begin with ``#`` followed by a
        rendered action expression (i.e. the descriptive
        comment line, not user-authored comments) are detected
        by inspecting the ``# <selector> @host:port`` shape.

        :param body: The current syslog.conf body.
        :param forwarder_id: The forwarder identifier.
        :param prefix: The configured sentinel prefix.
        :param enabled: True to uncomment; False to comment.

        :returns: A two-tuple ``(new_body, found)``.
    """
    if body is None or len(body) == 0:
        rtnval = (body, False)
        return rtnval

    blocks = _extract_blocks(body=body, prefix=prefix)
    match = None
    for block in blocks:
        if block.forwarder_id == forwarder_id:
            match = block
            break

    if match is None:
        rtnval = (body, False)
        return rtnval

    lines = body.splitlines()
    new_lines: list[str] = []
    for index, line in enumerate(lines):
        if index <= match.start_index or index >= match.end_index:
            new_lines.append(line)
            continue

        # Lines between header and footer.  The renderer emits
        # exactly one descriptive comment line (starting with
        # "#@") and one action line.  The action line starts
        # with the selector token directly when enabled, or
        # with "# " plus the selector when disabled.  The "#@"
        # descriptive comment is preserved verbatim so the
        # toggle helper does not mistake it for a disabled
        # action line.
        stripped = line.strip()
        if stripped.startswith("#@"):
            new_lines.append(line)
            continue

        if stripped.startswith("#") and " @" in stripped:
            # Disabled action line: "# <selector> @host:port".
            without_hash = stripped.lstrip("#").lstrip()
            if "@" in without_hash and " " in without_hash:
                tokens = without_hash.split(None, 1)
                if len(tokens) == 2 and tokens[1].startswith("@"):
                    if enabled:
                        new_lines.append(without_hash)
                        continue
                    new_lines.append("# " + without_hash)
                    continue
            new_lines.append(line)
            continue

        if " @" in stripped or stripped.startswith("@"):
            tokens = stripped.split(None, 1)
            if len(tokens) == 2 and tokens[1].startswith("@"):
                if enabled:
                    new_lines.append(stripped)
                    continue
                new_lines.append("# " + stripped)
                continue

        new_lines.append(line)

    rtnval_body = "\n".join(new_lines)
    if body.endswith("\n"):
        rtnval_body = rtnval_body + "\n"
    rtnval = (rtnval_body, True)
    return rtnval


def _parse_block(*, block_body: str) -> tuple:
    """
        Parse a managed block body back into
        ``(host, port, protocol, selector, enabled)``.

        :param block_body: The block body (header / footer
                           inclusive).

        :returns: A five-tuple
                  ``(host, port, protocol, selector, enabled)``.
                  Missing values surface as empty strings or
                  ``0``.
    """
    host = ""
    port = 0
    protocol = _PROTOCOL_UDP
    selector = ""
    enabled = False

    for raw_line in block_body.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("# >>>"):
            continue
        if stripped.startswith("# <<<"):
            continue
        if stripped.startswith("#@"):
            # Descriptive comment — ignored by the parser.
            continue

        if stripped.startswith("#"):
            # Disabled action line.  Strip the leading "#" /
            # spaces and try to parse.
            without_hash = stripped.lstrip("#").lstrip()
            tokens = without_hash.split(None, 1)
            if len(tokens) == 2 and tokens[1].startswith("@"):
                # Disabled action line — use it for the host /
                # port / selector but mark enabled=False.
                if len(host) == 0:
                    host_value, port_value = _split_host_port(
                        host_port=tokens[1].lstrip("@"),
                    )
                    if host_value is not None:
                        host = host_value
                        port = port_value
                        selector = tokens[0]
            continue

        # Active action line.
        tokens = stripped.split(None, 1)
        if len(tokens) < 2:
            continue
        target = tokens[1]
        if not target.startswith("@"):
            continue
        host_value, port_value = _split_host_port(
            host_port=target.lstrip("@"),
        )
        if host_value is None:
            continue
        host = host_value
        port = port_value
        selector = tokens[0]
        enabled = True

    rtnval = (host, port, protocol, selector, enabled)
    return rtnval


def _split_host_port(*, host_port: str) -> tuple:
    """
        Split a ``host:port`` string into its components.

        :param host_port: The combined token.

        :returns: A two-tuple ``(host, port)``.  ``host`` is
                  ``None`` when the token cannot be parsed.
    """
    if host_port is None or len(host_port) == 0:
        rtnval = (None, 0)
        return rtnval

    if ":" not in host_port:
        rtnval = (host_port, 0)
        return rtnval

    last_colon = host_port.rfind(":")
    host_value = host_port[:last_colon]
    port_text = host_port[last_colon + 1:]
    try:
        port_value = int(port_text)
    except ValueError:
        rtnval = (None, 0)
        return rtnval

    rtnval = (host_value, port_value)
    return rtnval
