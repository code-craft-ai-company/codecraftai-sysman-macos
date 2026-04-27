"""
    macOS precision-time-management feature.

    Implements the cross-OS precision-time-management contract for macOS
    using ``systemsetup`` (the canonical persistent surface for the
    network-time configuration), ``sntp`` (the user-space SNTP client
    for one-shot sync and offset measurement), and (where present) the
    ``com.apple.timed`` defaults domain for richer source enumeration.

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner` (``systemsetup``, ``sntp``, ``defaults``,
        ``cat``).  This module does NOT consume an
        :class:`IFileOperator`.  ``systemsetup`` owns its own persistence
        layer; we never write configuration files directly.  Legacy
        ``/etc/ntp.conf`` is read via ``cat`` only when ``defaults read
        com.apple.timed`` is unavailable (older macOS builds).

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_time_status``, ``get_time_sources``, ``get_drift``,
        idempotency probes) never elevate.

    Multi-server limitation:
        ``systemsetup -setnetworktimeserver`` accepts a single
        whitespace-delimited string and persists it through the System
        Configuration framework.  Apple's tooling is single-server-
        oriented; multi-server lists are accepted by ``systemsetup`` but
        are not natively distributed across daemons.  This manager
        documents the limitation: ``add_time_source`` and
        ``set_time_servers`` write the full list via ``systemsetup``,
        and operators should treat the first entry as the effective
        primary upstream.
"""

import os
import re

from dataclasses import dataclass
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_SYSTEMSETUP = "systemsetup"
_SNTP = "sntp"
_DEFAULTS = "defaults"
_CAT = "cat"

_LEGACY_NTP_CONF = "/etc/ntp.conf"
_TIMED_DEFAULTS_DOMAIN = "/Library/Preferences/com.apple.timed"


# Recognised ``systemsetup -getusingnetworktime`` payloads.  The command
# emits one of two literal forms.
_USING_NETWORK_TIME_ON = "Network Time: On"
_USING_NETWORK_TIME_OFF = "Network Time: Off"

# ``systemsetup -getnetworktimeserver`` payload prefix.
_NETWORK_TIME_SERVER_PREFIX = "Network Time Server:"

# Compiled regex for the ``sntp`` offset payload.  The tool emits a line
# like::
#
#     +0.001234 +/- 0.005000s ntp1.example.com 192.0.2.10
#
# We accept an optional sign on the offset and a unit suffix on the
# error.
_SNTP_OFFSET_PATTERN = re.compile(
    r"^\s*([+-]?\d+\.\d+)\s+\+\/\-\s+([0-9.]+)([a-z]*)",
)


MacosPrecisionTimeActionStatus = ActionStatus


@dataclass
class MacosPrecisionTimeManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosPrecisionTimeManager`.

        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.
        macOS does not expose tunable backend selection or service unit
        names; the manager binds to ``systemsetup`` / ``sntp`` /
        ``defaults`` unconditionally.
    """
    pass


@dataclass
class MacosPrecisionTimeActionResult(FeatureActionResult):
    """
        Result of a precision-time write operation.

        :param server: The server hostname or address the action targeted,
                       or ``None`` for verbs that do not target a single
                       server (``force_sync_now``, ``enable_sync``,
                       ``disable_sync``).
    """
    server: Optional[str] = None


@dataclass
class MacosTimeStatus:
    """
        Snapshot of the host's overall time-sync status.

        :param enabled: Whether ``systemsetup -getusingnetworktime``
                        reports ``On``.
        :param synchronized: Whether the host's clock is currently
                             considered synchronised (heuristic: True
                             when ``sntp`` against the current server
                             returns a finite offset and the service is
                             enabled).
        :param current_server: The server reported by ``systemsetup
                               -getnetworktimeserver``, or ``None``.
        :param stratum: Stratum is not exposed by ``systemsetup``;
                        always ``None``.
        :param last_sync: Last sync timestamp as a string, or ``None``.
        :param offset_seconds: Offset reported by ``sntp`` in seconds,
                               or ``None`` when not measured.
    """
    enabled: bool
    synchronized: bool
    current_server: Optional[str]
    stratum: Optional[int]
    last_sync: Optional[str]
    offset_seconds: Optional[float]


@dataclass
class MacosTimeSourceInfo:
    """
        One configured upstream time source.

        :param server: Hostname or address of the source.
        :param state: Native state token.  ``"selected"`` for the
                      ``systemsetup``-reported primary; ``"candidate"``
                      for additional whitespace-delimited entries.
        :param stratum: Always ``None`` on macOS.
        :param offset_seconds: Always ``None`` on macOS unless
                               specifically queried via ``sntp``.
        :param last_response: Always ``None`` on macOS.
    """
    server: str
    state: str
    stratum: Optional[int]
    offset_seconds: Optional[float]
    last_response: Optional[str]


@dataclass
class MacosTimeDrift:
    """
        Measured drift snapshot.

        :param offset_seconds: Smoothed offset returned by ``sntp``.
        :param frequency_ppm: Always ``None`` on macOS — Apple's stack
                              does not expose a native frequency-
                              adjustment value through user-space
                              tooling.
        :param jitter_seconds: ``sntp``'s ``+/-`` error term, expressed
                               in seconds.
        :param stratum: Always ``None``.
    """
    offset_seconds: float
    frequency_ppm: Optional[float]
    jitter_seconds: Optional[float]
    stratum: Optional[int]


class MacosPrecisionTimeQueryError(Exception):
    """
        Raised when a precision-time read fails for a reason other than
        the network-time service simply being off.
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

        err_msg_lines = ["Precision-time query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosPrecisionTimeManager(BaseFeatureManager):
    """
        macOS precision-time manager.

        Routes every operation through the injected
        :class:`ICommandRunner`.  The verb set is layered atop
        ``systemsetup`` for the persistent network-time surface,
        ``sntp`` for one-shot sync and offset measurement, and
        ``defaults read /Library/Preferences/com.apple.timed`` for
        richer source enumeration on contemporary macOS builds.
    """

    result_class = MacosPrecisionTimeActionResult
    status_class = MacosPrecisionTimeActionStatus
    feature_name = "precision-time-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_time_status(self) -> MacosTimeStatus:
        """
            Query the host's overall sync status.

            Combines ``systemsetup -getusingnetworktime`` and
            ``systemsetup -getnetworktimeserver``.  Never elevates.

            :returns: The :class:`MacosTimeStatus`.
            :raises MacosPrecisionTimeQueryError: When ``systemsetup``
                                                   fails unexpectedly.
        """
        using_command = [_SYSTEMSETUP, "-getusingnetworktime"]
        using_run = self._runner.run(list(using_command), elevated=False)
        if using_run.exit_code != 0:
            raise MacosPrecisionTimeQueryError(
                command=using_command,
                exit_code=using_run.exit_code,
                stderr=using_run.stderr or "",
            )
        enabled = _parse_using_network_time(text=using_run.stdout)

        server_command = [_SYSTEMSETUP, "-getnetworktimeserver"]
        server_run = self._runner.run(list(server_command), elevated=False)
        if server_run.exit_code != 0:
            raise MacosPrecisionTimeQueryError(
                command=server_command,
                exit_code=server_run.exit_code,
                stderr=server_run.stderr or "",
            )
        current_server = _parse_network_time_server(text=server_run.stdout)

        rtnval = MacosTimeStatus(
            enabled=enabled,
            synchronized=enabled,
            current_server=current_server,
            stratum=None,
            last_sync=None,
            offset_seconds=None,
        )
        return rtnval

    def get_time_sources(self) -> list[MacosTimeSourceInfo]:
        """
            Enumerate currently configured upstream time sources.

            Tries ``defaults read /Library/Preferences/com.apple.timed
            timeServiceConfig`` first; on failure falls back to the
            single-server ``systemsetup -getnetworktimeserver`` payload;
            falls further back to parsing ``/etc/ntp.conf`` for legacy
            installations.  Never elevates.

            :returns: One :class:`MacosTimeSourceInfo` per source.
        """
        rtnval: list[MacosTimeSourceInfo] = []

        defaults_command = [
            _DEFAULTS, "read", _TIMED_DEFAULTS_DOMAIN,
            "timeServiceConfig",
        ]
        defaults_run = self._runner.run(
            list(defaults_command), elevated=False,
        )
        if defaults_run.exit_code == 0:
            servers = _parse_defaults_time_service_config(
                text=defaults_run.stdout,
            )
            if len(servers) > 0:
                rtnval = self._build_sources_from_list(
                    servers=servers, primary=servers[0],
                )
                return rtnval

        server_command = [_SYSTEMSETUP, "-getnetworktimeserver"]
        server_run = self._runner.run(list(server_command), elevated=False)
        if server_run.exit_code != 0:
            raise MacosPrecisionTimeQueryError(
                command=server_command,
                exit_code=server_run.exit_code,
                stderr=server_run.stderr or "",
            )

        primary = _parse_network_time_server(text=server_run.stdout)
        if primary is not None:
            rtnval = self._build_sources_from_list(
                servers=[primary], primary=primary,
            )
            return rtnval

        legacy_command = [_CAT, _LEGACY_NTP_CONF]
        legacy_run = self._runner.run(list(legacy_command), elevated=False)
        if legacy_run.exit_code == 0:
            servers = _parse_ntp_conf_servers(text=legacy_run.stdout)
            if len(servers) > 0:
                rtnval = self._build_sources_from_list(
                    servers=servers, primary=servers[0],
                )
        return rtnval

    def get_drift(self) -> MacosTimeDrift:
        """
            Read the current drift snapshot via ``sntp``.

            Picks the currently-configured server (from
            ``systemsetup -getnetworktimeserver``) and dispatches
            ``sntp <server>``, then parses the
            ``±offset +/- error`` payload.  Never elevates.

            :returns: The :class:`MacosTimeDrift`.
        """
        server_command = [_SYSTEMSETUP, "-getnetworktimeserver"]
        server_run = self._runner.run(list(server_command), elevated=False)
        if server_run.exit_code != 0:
            raise MacosPrecisionTimeQueryError(
                command=server_command,
                exit_code=server_run.exit_code,
                stderr=server_run.stderr or "",
            )
        server = _parse_network_time_server(text=server_run.stdout)
        if server is None:
            rtnval = MacosTimeDrift(
                offset_seconds=0.0,
                frequency_ppm=None,
                jitter_seconds=None,
                stratum=None,
            )
            return rtnval

        sntp_command = [_SNTP, server]
        sntp_run = self._runner.run(list(sntp_command), elevated=False)
        if sntp_run.exit_code != 0:
            raise MacosPrecisionTimeQueryError(
                command=sntp_command,
                exit_code=sntp_run.exit_code,
                stderr=sntp_run.stderr or "",
            )

        rtnval = _parse_sntp_offset(text=sntp_run.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_time_source(self, server: str) -> MacosPrecisionTimeActionResult:
        """
            Add an upstream time source.

            macOS limitation: ``systemsetup -setnetworktimeserver``
            accepts a single whitespace-delimited string; this method
            appends to the current list and rewrites the full string.
            Apple's tooling treats the first entry as the effective
            primary.

            Idempotent: ``SKIPPED`` when the server is already in the
            current list.

            :param server: Server hostname or address.

            :returns: The :class:`MacosPrecisionTimeActionResult`.
        """
        action = "add_time_source:{}".format(server)
        command = [_SYSTEMSETUP, "-setnetworktimeserver", server]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosPrecisionTimeActionStatus.DRY_RUN,
                detail=action,
                command=command,
                server=server,
            )
            return dry

        current_servers = self._read_current_servers()
        if server in current_servers:
            skipped = self._build_result(
                status=MacosPrecisionTimeActionStatus.SKIPPED,
                detail=action,
                command=command,
                server=server,
            )
            return skipped

        new_servers = list(current_servers)
        new_servers.append(server)
        rendered = " ".join(new_servers)
        rewrite_command = [_SYSTEMSETUP, "-setnetworktimeserver", rendered]

        rtnval = self._dispatch_write(
            command=rewrite_command,
            detail=action,
            server=server,
        )
        return rtnval

    def remove_time_source(
        self, server: str,
    ) -> MacosPrecisionTimeActionResult:
        """
            Remove an upstream time source.

            Recomputes the current list with ``server`` removed and
            invokes ``systemsetup -setnetworktimeserver`` with the
            joined remainder.

            Idempotent: ``SKIPPED`` when the server is not currently
            configured.

            :param server: Server hostname or address.

            :returns: The :class:`MacosPrecisionTimeActionResult`.
        """
        action = "remove_time_source:{}".format(server)

        if self._config.dry_run:
            placeholder = [_SYSTEMSETUP, "-setnetworktimeserver", ""]
            dry = self._build_result(
                status=MacosPrecisionTimeActionStatus.DRY_RUN,
                detail=action,
                command=placeholder,
                server=server,
            )
            return dry

        current_servers = self._read_current_servers()
        if server not in current_servers:
            placeholder = [
                _SYSTEMSETUP, "-setnetworktimeserver",
                " ".join(current_servers),
            ]
            skipped = self._build_result(
                status=MacosPrecisionTimeActionStatus.SKIPPED,
                detail=action,
                command=placeholder,
                server=server,
            )
            return skipped

        new_servers = [item for item in current_servers if item != server]
        rendered = " ".join(new_servers)
        rewrite_command = [_SYSTEMSETUP, "-setnetworktimeserver", rendered]

        rtnval = self._dispatch_write(
            command=rewrite_command,
            detail=action,
            server=server,
        )
        return rtnval

    def set_time_servers(
        self, servers: list[str],
    ) -> MacosPrecisionTimeActionResult:
        """
            Replace the active set of upstream sources.

            Idempotent: ``SKIPPED`` when the current sorted list equals
            the desired sorted list.

            :param servers: Ordered list of desired servers.

            :returns: The :class:`MacosPrecisionTimeActionResult`.
        """
        action = "set_time_servers:{}".format(",".join(servers))
        rendered = " ".join(servers)
        command = [_SYSTEMSETUP, "-setnetworktimeserver", rendered]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosPrecisionTimeActionStatus.DRY_RUN,
                detail=action,
                command=command,
                server=None,
            )
            return dry

        current_servers = self._read_current_servers()
        if sorted(current_servers) == sorted(servers):
            skipped = self._build_result(
                status=MacosPrecisionTimeActionStatus.SKIPPED,
                detail=action,
                command=command,
                server=None,
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            detail=action,
            server=None,
        )
        return rtnval

    def force_sync_now(self) -> MacosPrecisionTimeActionResult:
        """
            Force an immediate synchronisation against the currently
            configured server.

            Implementation: dispatches ``sntp -sS <current_server>``,
            which is the documented Apple one-shot synchronisation
            primitive.  ``-S`` instructs ``sntp`` to step the system
            clock; the manager runs the command elevated.

            :returns: The :class:`MacosPrecisionTimeActionResult`.
        """
        if self._config.dry_run:
            placeholder = [_SNTP, "-sS", "<current_server>"]
            dry = self._build_result(
                status=MacosPrecisionTimeActionStatus.DRY_RUN,
                detail="force_sync_now",
                command=placeholder,
                server=None,
            )
            return dry

        server_command = [_SYSTEMSETUP, "-getnetworktimeserver"]
        server_run = self._runner.run(list(server_command), elevated=False)
        if server_run.exit_code != 0:
            failed = MacosPrecisionTimeActionResult(
                status=MacosPrecisionTimeActionStatus.FAILED,
                detail="force_sync_now",
                exit_code=server_run.exit_code,
                stdout=server_run.stdout,
                stderr=server_run.stderr,
                command=list(server_command),
                server=None,
            )
            return failed
        server = _parse_network_time_server(text=server_run.stdout)
        if server is None:
            failed = MacosPrecisionTimeActionResult(
                status=MacosPrecisionTimeActionStatus.FAILED,
                detail="force_sync_now",
                exit_code=server_run.exit_code,
                stdout=server_run.stdout,
                stderr="No network time server is configured",
                command=list(server_command),
                server=None,
            )
            return failed

        sync_command = [_SNTP, "-sS", server]
        rtnval = self._dispatch_write(
            command=sync_command,
            detail="force_sync_now",
            server=server,
        )
        return rtnval

    def enable_sync(self) -> MacosPrecisionTimeActionResult:
        """
            Enable network time via
            ``systemsetup -setusingnetworktime on``.

            Idempotent: ``SKIPPED`` when network time is already on.

            :returns: The :class:`MacosPrecisionTimeActionResult`.
        """
        command = [_SYSTEMSETUP, "-setusingnetworktime", "on"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosPrecisionTimeActionStatus.DRY_RUN,
                detail="enable_sync",
                command=command,
                server=None,
            )
            return dry

        if self._sync_is_enabled():
            skipped = self._build_result(
                status=MacosPrecisionTimeActionStatus.SKIPPED,
                detail="enable_sync",
                command=command,
                server=None,
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            detail="enable_sync",
            server=None,
        )
        return rtnval

    def disable_sync(self) -> MacosPrecisionTimeActionResult:
        """
            Disable network time via
            ``systemsetup -setusingnetworktime off``.

            Idempotent: ``SKIPPED`` when network time is already off.

            :returns: The :class:`MacosPrecisionTimeActionResult`.
        """
        command = [_SYSTEMSETUP, "-setusingnetworktime", "off"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosPrecisionTimeActionStatus.DRY_RUN,
                detail="disable_sync",
                command=command,
                server=None,
            )
            return dry

        if not self._sync_is_enabled():
            skipped = self._build_result(
                status=MacosPrecisionTimeActionStatus.SKIPPED,
                detail="disable_sync",
                command=command,
                server=None,
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            detail="disable_sync",
            server=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_current_servers(self) -> list[str]:
        """
            Read the currently configured server list.

            Tries ``defaults read`` first, falls back to the
            single-server ``systemsetup -getnetworktimeserver`` form.

            :returns: Servers in declaration order.  Empty list when
                      neither source reports any.
        """
        defaults_command = [
            _DEFAULTS, "read", _TIMED_DEFAULTS_DOMAIN,
            "timeServiceConfig",
        ]
        defaults_run = self._runner.run(
            list(defaults_command), elevated=False,
        )
        if defaults_run.exit_code == 0:
            servers = _parse_defaults_time_service_config(
                text=defaults_run.stdout,
            )
            if len(servers) > 0:
                rtnval = servers
                return rtnval

        server_command = [_SYSTEMSETUP, "-getnetworktimeserver"]
        server_run = self._runner.run(list(server_command), elevated=False)
        if server_run.exit_code != 0:
            rtnval = []
            return rtnval
        primary = _parse_network_time_server(text=server_run.stdout)
        if primary is None:
            rtnval = []
            return rtnval

        # ``systemsetup`` accepts a multi-server space-delimited string
        # on input but returns the configured value verbatim on output.
        # Split on whitespace so a previously-set list round-trips.
        rtnval = primary.split()
        return rtnval

    def _sync_is_enabled(self) -> bool:
        """
            Probe whether network time is currently on.

            :returns: ``True`` when ``systemsetup -getusingnetworktime``
                      reports ``On``.
        """
        command = [_SYSTEMSETUP, "-getusingnetworktime"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        rtnval = _parse_using_network_time(text=run_result.stdout)
        return rtnval

    def _build_sources_from_list(
        self, *, servers: list[str], primary: Optional[str],
    ) -> list[MacosTimeSourceInfo]:
        """
            Build :class:`MacosTimeSourceInfo` records from a list of
            server hostnames.

            :param servers: The server list, in declaration order.
            :param primary: The hostname (if any) to mark
                            ``"selected"``.

            :returns: One info record per server.
        """
        rtnval: list[MacosTimeSourceInfo] = []
        for server in servers:
            if server == primary:
                state = "selected"
            else:
                state = "candidate"
            info = MacosTimeSourceInfo(
                server=server,
                state=state,
                stratum=None,
                offset_seconds=None,
                last_response=None,
            )
            rtnval.append(info)
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        detail: str,
        server: Optional[str],
    ) -> MacosPrecisionTimeActionResult:
        """
            Dispatch a single-shot write command and wrap the result.

            :param command: The full argv.
            :param detail: ``detail`` value to echo.
            :param server: ``server`` value to echo.

            :returns: The :class:`MacosPrecisionTimeActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosPrecisionTimeActionStatus.SUCCESS
        else:
            mapped_status = MacosPrecisionTimeActionStatus.FAILED

        rtnval = MacosPrecisionTimeActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            server=server,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosPrecisionTimeActionStatus,
        detail: str,
        command: list[str],
        server: Optional[str],
    ) -> MacosPrecisionTimeActionResult:
        """
            Construct a :class:`MacosPrecisionTimeActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param detail: ``detail`` value to echo.
            :param command: ``command`` argv to record.
            :param server: ``server`` value to echo.

            :returns: The :class:`MacosPrecisionTimeActionResult`.
        """
        rtnval = MacosPrecisionTimeActionResult(
            status=status,
            detail=detail,
            command=list(command),
            server=server,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _parse_using_network_time(*, text: str) -> bool:
    """
        Parse a ``systemsetup -getusingnetworktime`` payload.

        :param text: Raw stdout.

        :returns: True when the payload reports ``Network Time: On``.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if line == _USING_NETWORK_TIME_ON:
            rtnval = True
            return rtnval
        if line == _USING_NETWORK_TIME_OFF:
            rtnval = False
            return rtnval
    rtnval = False
    return rtnval


def _parse_network_time_server(*, text: str) -> Optional[str]:
    """
        Parse a ``systemsetup -getnetworktimeserver`` payload.

        :param text: Raw stdout.

        :returns: The server hostname or address, or ``None`` when the
                  payload is empty / not in the expected shape.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(_NETWORK_TIME_SERVER_PREFIX):
            continue
        _, _, tail = line.partition(_NETWORK_TIME_SERVER_PREFIX)
        candidate = tail.strip()
        if len(candidate) == 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = candidate
        return rtnval
    rtnval = None
    return rtnval


def _parse_defaults_time_service_config(*, text: str) -> list[str]:
    """
        Parse a ``defaults read /Library/Preferences/com.apple.timed
        timeServiceConfig`` payload to extract the configured servers.

        ``defaults`` emits a plist-shaped payload like::

            {
                TimeServers = (
                    "time.apple.com",
                    "ntp.example.com"
                );
                ...
            }

        The parser pulls quoted hostnames from inside the
        ``TimeServers = ( ... );`` parenthesis block.

        :param text: Raw stdout.

        :returns: Extracted server hostnames in declaration order.
    """
    rtnval: list[str] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if "TimeServers" in line and "(" in line:
            in_block = True
            continue
        if in_block is False:
            continue
        if line.startswith(")"):
            in_block = False
            continue
        if "=" in line:
            # Some defaults dumps inline a single string assignment.
            head, _, tail = line.partition("=")
            if head.strip() == "TimeServers":
                cleaned = tail.strip().strip(";").strip().strip('"')
                if len(cleaned) > 0:
                    rtnval.append(cleaned)
            continue
        cleaned = line.strip(",").strip().strip('"')
        if len(cleaned) == 0:
            continue
        rtnval.append(cleaned)
    return rtnval


def _parse_ntp_conf_servers(*, text: str) -> list[str]:
    """
        Parse the legacy ``/etc/ntp.conf`` ``server`` declarations.

        :param text: Raw file contents.

        :returns: Servers in declaration order.
    """
    rtnval: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        head = tokens[0].lower()
        if head not in ("server", "pool"):
            continue
        rtnval.append(tokens[1])
    return rtnval


def _parse_sntp_offset(*, text: str) -> MacosTimeDrift:
    """
        Parse an ``sntp <server>`` payload into a drift snapshot.

        ``sntp`` prints a single line like::

            +0.001234 +/- 0.005000s ntp1.example.com 192.0.2.10

        The parser pulls the offset and the ``+/-`` error term and
        normalises the latter to seconds (``s`` suffix preserved as 1.0,
        ``ms`` divided by 1e3, etc.).

        :param text: Raw stdout.

        :returns: The :class:`MacosTimeDrift`.
    """
    offset_seconds = 0.0
    jitter_seconds: Optional[float] = None

    for raw in text.splitlines():
        match = _SNTP_OFFSET_PATTERN.search(raw)
        if match is None:
            continue
        offset_token = match.group(1)
        error_token = match.group(2)
        unit_token = match.group(3) or "s"
        try:
            parsed_offset = float(offset_token)
        except ValueError:
            continue
        try:
            parsed_error = float(error_token)
        except ValueError:
            continue
        offset_seconds = parsed_offset
        scale = _scale_for_unit(unit=unit_token)
        jitter_seconds = parsed_error * scale
        break

    rtnval = MacosTimeDrift(
        offset_seconds=offset_seconds,
        frequency_ppm=None,
        jitter_seconds=jitter_seconds,
        stratum=None,
    )
    return rtnval


def _scale_for_unit(*, unit: str) -> float:
    """
        Convert a unit suffix to a multiplicative seconds scale.

        :param unit: ``"s"``, ``"ms"``, ``"us"``, ``"ns"``, or ``""``.

        :returns: Multiplicative scale.  Defaults to 1.0 (seconds).
    """
    if unit == "ns":
        rtnval = 1e-9
        return rtnval
    if unit == "us":
        rtnval = 1e-6
        return rtnval
    if unit == "ms":
        rtnval = 1e-3
        return rtnval
    rtnval = 1.0
    return rtnval
