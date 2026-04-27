"""
    macOS firewall-management feature.

    Implements the cross-OS firewall-management contract for macOS
    using ``pfctl`` and per-anchor file management under
    ``/etc/pf.anchors/``.

    Zone mapping:
        macOS pf has no first-class zone concept, but it does have
        anchor namespaces.  This manager maps each contract ``zone``
        to a pf anchor named ``automojo.<zone>``; the anchor's rules
        live in ``/etc/pf.anchors/automojo.<zone>``.  Loading an
        anchor requires that ``/etc/pf.conf`` declare and load it;
        :meth:`set_default_zone` ensures that declaration is in
        place.

    File I/O policy:
        Every file read / write / probe is performed through the
        injected :class:`ICommandRunner` (``cat``, ``tee``, ``rm``,
        ``test -e``, ``mkdir -p``).  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``query_zone``, ``query_all_zones``, ``query_state``) and
        idempotency probes never elevate.

    Surface choices:
        * Each ``add_*`` / ``remove_*`` verb reads the current anchor
          file via ``cat``, computes the desired post-state, and
          either rewrites the file via ``tee`` (followed by a
          per-anchor ``pfctl -a`` reload) or returns ``SKIPPED`` when
          the post-state already matches.
        * Service-to-port resolution uses a small bundled table
          (ssh / http / https / smtp / dns / ftp / telnet / pop3 /
          imap / submission / mysql / postgresql).  Unknown service
          names yield ``FAILED``.  Callers needing a richer table can
          override
          :attr:`MacosFirewallManagerConfig.services_table`.
        * ``set_default_zone`` ensures ``/etc/pf.conf`` declares and
          loads the requested anchor as the FIRST automojo anchor;
          the file is rewritten via ``cat`` + ``tee`` and the master
          ruleset is reloaded via ``pfctl -f``.
        * ``reload`` reloads ``/etc/pf.conf`` via ``pfctl -f``.
        * ``query_zone`` runs ``pfctl -a automojo.<zone> -sr`` and
          parses the rule list.
        * ``query_all_zones`` runs ``pfctl -sA``, filters to anchors
          whose name starts with ``automojo.``, and queries each.
        * ``query_state`` runs ``pfctl -si`` and reports
          ``running=True`` iff the ``Status:`` line reports
          ``Enabled``.

    Limitations (documented in tasks.md follow-ups):
        * The bundled services table covers ~12 entries.  Unknown
          service names FAIL eagerly; a future enhancement could read
          ``/etc/services`` via ``cat`` to resolve dynamically.
        * pf has no per-anchor "interfaces" or "sources" concept the
          way firewalld does; the corresponding fields on
          :class:`MacosZoneInfo` are always empty lists.
        * Rich-rule comparison normalises whitespace; pf's
          quick-match semantics mean two rules can be functionally
          equivalent yet textually distinct (e.g. ``pass in quick
          proto tcp ...`` vs ``pass quick in proto tcp ...``).  This
          pilot only matches on whitespace-normalised string equality.
"""

import os
import re

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_PFCTL = "pfctl"

_DEFAULT_ANCHOR_PREFIX = "/etc/pf.anchors/automojo."
_DEFAULT_PF_CONF_PATH = "/etc/pf.conf"
_AUTOMOJO_ANCHOR_NAME_PREFIX = "automojo."

# Bundled services table: name -> (port, protocol).  Callers can
# override this by passing a non-None ``services_table`` on the
# config; the override completely replaces the bundled table.
_BUNDLED_SERVICES_TABLE: dict = {
    "ssh":        (22, "tcp"),
    "http":       (80, "tcp"),
    "https":      (443, "tcp"),
    "smtp":       (25, "tcp"),
    "dns":        (53, "udp"),
    "ftp":        (21, "tcp"),
    "telnet":     (23, "tcp"),
    "pop3":       (110, "tcp"),
    "imap":       (143, "tcp"),
    "submission": (587, "tcp"),
    "mysql":      (3306, "tcp"),
    "postgresql": (5432, "tcp"),
}

_VALID_PROTOCOLS = frozenset({"tcp", "udp"})

_VALID_RULE_VERBS = frozenset({"pass", "block", "match"})

_RULE_PORT_PATTERN = re.compile(
    r"\bproto\s+(tcp|udp)\b.*\bport\s+(\d+)\b",
)


MacosFirewallActionStatus = ActionStatus


@dataclass
class MacosFirewallManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosFirewallManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param anchor_file_prefix: Path prefix used to build per-zone
                                   anchor file paths.  The full path
                                   for zone ``trusted`` is
                                   ``<prefix><zone>``, e.g.
                                   ``/etc/pf.anchors/automojo.trusted``.
        :param pf_conf_path: Absolute path of the master pf
                             configuration file.  Defaults to
                             ``/etc/pf.conf``.
        :param services_table: Optional override for the bundled
                               services table.  ``None`` selects the
                               bundled defaults; passing a dict
                               replaces them.
    """
    anchor_file_prefix: str = _DEFAULT_ANCHOR_PREFIX
    pf_conf_path: str = _DEFAULT_PF_CONF_PATH
    services_table: Optional[dict] = None


@dataclass
class MacosFirewallActionResult(FeatureActionResult):
    """
        Result of a firewall-management write operation.

        :param zone: The zone targeted.  Echoed back even on failure
                     so callers can see what they passed in.
        :param detail_name: Secondary identifier echoed onto the
                            result (service name, ``port/proto``
                            string, or rule string).
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    zone: Optional[str] = None
    detail_name: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosZoneInfo:
    """
        Parsed view of a per-zone pf anchor.

        :param zone: The zone identifier (without the ``automojo.``
                     prefix).
        :param services: Service names whose ``(port, proto)``
                         resolves to a port currently allowed in the
                         anchor.
        :param ports: ``"<port>/<proto>"`` strings parsed from the
                      anchor's pass-rule lines.
        :param rich_rules: Anchor-rule lines that did not match the
                           service / port pattern.  Preserved
                           verbatim (whitespace-normalised) so
                           callers can round-trip them.
        :param interfaces: Always empty — pf has no per-anchor
                           interface concept the way firewalld does.
        :param sources: Always empty — sources are folded into rich
                        rules.
        :param is_default: True when ``/etc/pf.conf`` declares this
                           anchor as the first automojo anchor.
    """
    zone: str
    services: list[str] = field(default_factory=list)
    ports: list[str] = field(default_factory=list)
    rich_rules: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    is_default: bool = False


@dataclass
class MacosFirewallState:
    """
        Current pf running state.

        :param running: True when ``pfctl -si`` reports
                        ``Status: Enabled``.
        :param default_zone: The zone identifier that
                             ``/etc/pf.conf`` lists first among the
                             ``automojo.*`` anchors; empty string
                             when none is declared.
    """
    running: bool
    default_zone: str


class MacosFirewallQueryError(Exception):
    """
        Raised when a firewall read fails for a reason other than
        the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.  May be empty when
                            the error is raised pre-dispatch.
            :param exit_code: The runner's exit code, or ``None``
                              when raised pre-dispatch.
            :param stderr: The runner's captured stderr, or a
                           descriptive string when raised
                           pre-dispatch.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Firewall query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosFirewallManager(BaseFeatureManager):
    """
        macOS pf firewall manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`; per-anchor file I/O is
        performed via ``cat`` / ``tee`` / ``test -e`` rather than a
        separate file operator.
    """

    result_class = MacosFirewallActionResult
    status_class = MacosFirewallActionStatus
    feature_name = "firewall-management"

    def __init__(
        self,
        runner,
        config: Optional[MacosFirewallManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every operation.
            :param config: Manager configuration.  Defaults to a
                           fresh :class:`MacosFirewallManagerConfig`
                           when omitted.
        """
        if config is None:
            config = MacosFirewallManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs — services
    # ------------------------------------------------------------------

    def add_service(
        self,
        service: str,
        zone: str,
    ) -> MacosFirewallActionResult:
        """
            Allow ``service`` (resolved to a port via the bundled
            services table) in ``zone``'s anchor.

            Idempotent: ``SKIPPED`` when the resolved pass rule is
            already present in the anchor file.

            :param service: Service name (``ssh``, ``http``, ...).
            :param zone: Zone identifier; mapped to anchor
                         ``automojo.<zone>``.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        resolved = self._resolve_service(service=service)
        if resolved is None:
            failed = self._build_result(
                status=MacosFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=service,
                command=[],
                message=(
                    "service {!r} is not in the bundled services "
                    "table".format(service)
                ),
                stderr="unknown_service",
            )
            return failed

        port_value, protocol_value = resolved
        rule_line = _build_pass_rule(
            port=port_value, protocol=protocol_value,
        )
        rtnval = self._mutate_anchor(
            zone=zone,
            rule_line=rule_line,
            detail_name=service,
            add=True,
            skip_message="service rule already present",
        )
        return rtnval

    def remove_service(
        self,
        service: str,
        zone: str,
    ) -> MacosFirewallActionResult:
        """
            Remove ``service`` from ``zone``'s anchor.

            Idempotent: ``SKIPPED`` when the resolved pass rule is
            not present.

            :param service: Service name.
            :param zone: Zone identifier.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        resolved = self._resolve_service(service=service)
        if resolved is None:
            failed = self._build_result(
                status=MacosFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=service,
                command=[],
                message=(
                    "service {!r} is not in the bundled services "
                    "table".format(service)
                ),
                stderr="unknown_service",
            )
            return failed

        port_value, protocol_value = resolved
        rule_line = _build_pass_rule(
            port=port_value, protocol=protocol_value,
        )
        rtnval = self._mutate_anchor(
            zone=zone,
            rule_line=rule_line,
            detail_name=service,
            add=False,
            skip_message="service rule not present",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — ports
    # ------------------------------------------------------------------

    def add_port(
        self,
        port: int,
        protocol: str,
        zone: str,
    ) -> MacosFirewallActionResult:
        """
            Open ``port/protocol`` in ``zone``'s anchor.

            Idempotent: ``SKIPPED`` when the resolved pass rule is
            already present.

            :param port: Port number.
            :param protocol: ``"tcp"`` or ``"udp"``.
            :param zone: Zone identifier.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        port_spec = "{}/{}".format(port, protocol)

        protocol_lower = protocol.strip().lower()
        if protocol_lower not in _VALID_PROTOCOLS:
            failed = self._build_result(
                status=MacosFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=port_spec,
                command=[],
                message=(
                    "protocol must be one of: {}".format(
                        ", ".join(sorted(_VALID_PROTOCOLS)),
                    )
                ),
                stderr="invalid_protocol",
            )
            return failed

        rule_line = _build_pass_rule(port=port, protocol=protocol_lower)
        rtnval = self._mutate_anchor(
            zone=zone,
            rule_line=rule_line,
            detail_name=port_spec,
            add=True,
            skip_message="port rule already present",
        )
        return rtnval

    def remove_port(
        self,
        port: int,
        protocol: str,
        zone: str,
    ) -> MacosFirewallActionResult:
        """
            Close ``port/protocol`` in ``zone``'s anchor.

            Idempotent: ``SKIPPED`` when the resolved pass rule is
            not present.

            :param port: Port number.
            :param protocol: ``"tcp"`` or ``"udp"``.
            :param zone: Zone identifier.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        port_spec = "{}/{}".format(port, protocol)

        protocol_lower = protocol.strip().lower()
        if protocol_lower not in _VALID_PROTOCOLS:
            failed = self._build_result(
                status=MacosFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=port_spec,
                command=[],
                message=(
                    "protocol must be one of: {}".format(
                        ", ".join(sorted(_VALID_PROTOCOLS)),
                    )
                ),
                stderr="invalid_protocol",
            )
            return failed

        rule_line = _build_pass_rule(port=port, protocol=protocol_lower)
        rtnval = self._mutate_anchor(
            zone=zone,
            rule_line=rule_line,
            detail_name=port_spec,
            add=False,
            skip_message="port rule not present",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — rich rules
    # ------------------------------------------------------------------

    def add_rich_rule(
        self,
        rule: str,
        zone: str,
    ) -> MacosFirewallActionResult:
        """
            Append a raw pf-syntax rule to ``zone``'s anchor.

            ``rule`` must begin with one of the pf rule verbs
            (``pass``, ``block``, ``match``).  Idempotent:
            ``SKIPPED`` when the normalised rule already appears in
            the anchor file.

            :param rule: pf rule string (without anchor wrapping).
            :param zone: Zone identifier.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        normalised = _normalise_rule(rule=rule)
        verb = _extract_verb(rule=normalised)
        if verb is None:
            failed = self._build_result(
                status=MacosFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=rule,
                command=[],
                message=(
                    "rich rule must begin with one of: {}".format(
                        ", ".join(sorted(_VALID_RULE_VERBS)),
                    )
                ),
                stderr="invalid_rich_rule_verb",
            )
            return failed

        rtnval = self._mutate_anchor(
            zone=zone,
            rule_line=normalised,
            detail_name=rule,
            add=True,
            skip_message="rich rule already present",
        )
        return rtnval

    def remove_rich_rule(
        self,
        rule: str,
        zone: str,
    ) -> MacosFirewallActionResult:
        """
            Delete a raw pf-syntax rule from ``zone``'s anchor.

            Idempotent: ``SKIPPED`` when the rule is not currently
            present.

            :param rule: pf rule string.
            :param zone: Zone identifier.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        normalised = _normalise_rule(rule=rule)
        rtnval = self._mutate_anchor(
            zone=zone,
            rule_line=normalised,
            detail_name=rule,
            add=False,
            skip_message="rich rule not present",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — default zone / reload
    # ------------------------------------------------------------------

    def set_default_zone(
        self,
        zone: str,
    ) -> MacosFirewallActionResult:
        """
            Ensure ``/etc/pf.conf`` declares ``automojo.<zone>`` as
            the FIRST automojo anchor and reload pf.

            The pf-conf body is rewritten via ``cat`` + ``tee`` and
            the master ruleset is reloaded via ``pfctl -f``.
            Idempotent: ``SKIPPED`` when pf-conf already lists this
            zone first among the automojo anchors.

            :param zone: Zone identifier.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        anchor_name = self._anchor_name(zone=zone)
        anchor_path = self._anchor_path(zone=zone)
        pf_conf_path = self._typed_config().pf_conf_path

        write_command = ["tee", pf_conf_path]
        reload_command = [_PFCTL, "-f", pf_conf_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=zone,
                command=write_command,
            )
            return dry

        existing_conf = self._read_file(path=pf_conf_path)
        if existing_conf is None:
            existing_conf = ""

        # Idempotency: if the first automojo anchor declared in the
        # existing pf.conf already matches the requested zone, no
        # rewrite is needed.  We deliberately do NOT compare full
        # rendered bodies because formatting (blank-line separators,
        # comment ordering) is not load-bearing.
        first_zone = _first_automojo_zone_in_pfconf(body=existing_conf)
        if first_zone == zone:
            skipped = self._build_result(
                status=MacosFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=zone,
                command=write_command,
                message="pf.conf already lists anchor first",
            )
            return skipped

        desired_conf = _ensure_anchor_first(
            existing=existing_conf,
            anchor_name=anchor_name,
            anchor_path=anchor_path,
        )

        write_run = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired_conf,
        )
        if write_run.exit_code != 0:
            rtnval = MacosFirewallActionResult(
                status=MacosFirewallActionStatus.FAILED,
                detail=zone,
                exit_code=write_run.exit_code,
                stdout=write_run.stdout,
                stderr=write_run.stderr,
                command=list(write_command),
                zone=zone,
                detail_name=zone,
                message="pf.conf rewrite failed",
            )
            return rtnval

        reload_run = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_run.exit_code != 0:
            rtnval = MacosFirewallActionResult(
                status=MacosFirewallActionStatus.FAILED,
                detail=zone,
                exit_code=reload_run.exit_code,
                stdout=reload_run.stdout,
                stderr=reload_run.stderr,
                command=list(reload_command),
                zone=zone,
                detail_name=zone,
                message="pfctl -f reload failed",
            )
            return rtnval

        rtnval = MacosFirewallActionResult(
            status=MacosFirewallActionStatus.SUCCESS,
            detail=zone,
            exit_code=reload_run.exit_code,
            stdout=reload_run.stdout,
            stderr=reload_run.stderr,
            command=list(reload_command),
            zone=zone,
            detail_name=zone,
        )
        return rtnval

    def reload(self) -> MacosFirewallActionResult:
        """
            Reload ``/etc/pf.conf`` via ``pfctl -f``.  Never skips.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        pf_conf_path = self._typed_config().pf_conf_path
        command = [_PFCTL, "-f", pf_conf_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosFirewallActionStatus.DRY_RUN,
                zone=None,
                detail_name=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosFirewallActionStatus.SUCCESS
        else:
            mapped_status = MacosFirewallActionStatus.FAILED

        rtnval = MacosFirewallActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            zone=None,
            detail_name=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def query_zone(self, zone: str) -> MacosZoneInfo:
        """
            Parse ``pfctl -a automojo.<zone> -sr`` into a
            :class:`MacosZoneInfo`.

            :param zone: Zone identifier.

            :returns: The parsed :class:`MacosZoneInfo`.
            :raises MacosFirewallQueryError: When the runner returns
                a non-zero exit with a non-empty stderr.
        """
        anchor_name = self._anchor_name(zone=zone)
        command = [_PFCTL, "-a", anchor_name, "-sr"]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise MacosFirewallQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        services_table = self._effective_services_table()
        is_default_flag = self._zone_is_default(zone=zone)

        rtnval = _parse_anchor_rules(
            zone=zone,
            text=run_result.stdout,
            services_table=services_table,
            is_default=is_default_flag,
        )
        return rtnval

    def query_all_zones(self) -> list[MacosZoneInfo]:
        """
            Enumerate every ``automojo.<zone>`` anchor declared in
            ``pfctl -sA`` and parse each one.

            :returns: One :class:`MacosZoneInfo` per discovered
                      anchor.
            :raises MacosFirewallQueryError: When ``pfctl -sA`` exits
                non-zero with a non-empty stderr.
        """
        list_command = [_PFCTL, "-sA"]
        list_result = self._runner.run(list(list_command), elevated=False)
        if list_result.exit_code != 0 and len(list_result.stderr.strip()) > 0:
            raise MacosFirewallQueryError(
                command=list_command,
                exit_code=list_result.exit_code,
                stderr=list_result.stderr,
            )

        zones = _extract_automojo_zones(text=list_result.stdout)

        rtnval: list[MacosZoneInfo] = []
        for zone in zones:
            try:
                info = self.query_zone(zone=zone)
            except MacosFirewallQueryError:
                continue
            rtnval.append(info)
        return rtnval

    def query_state(self) -> MacosFirewallState:
        """
            Query running state via ``pfctl -si``.

            ``pfctl -si`` returning non-zero is treated as pf being
            inactive (``running=False``) rather than as an error.

            :returns: The :class:`MacosFirewallState`.
        """
        command = [_PFCTL, "-si"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = MacosFirewallState(running=False, default_zone="")
            return rtnval

        running_flag = _pfctl_si_is_enabled(text=run_result.stdout)

        if running_flag:
            default_zone = self._read_default_zone()
        else:
            default_zone = ""

        rtnval = MacosFirewallState(
            running=running_flag,
            default_zone=default_zone,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — anchor mutation
    # ------------------------------------------------------------------

    def _mutate_anchor(
        self,
        *,
        zone: str,
        rule_line: str,
        detail_name: Optional[str],
        add: bool,
        skip_message: str,
    ) -> MacosFirewallActionResult:
        """
            Idempotency-aware anchor mutator.

            Reads the anchor file, computes the desired post-state
            (with or without ``rule_line``), and either rewrites the
            file via ``tee`` + reloads via ``pfctl -a -f`` or returns
            ``SKIPPED``.

            :param zone: Zone identifier.
            :param rule_line: Whitespace-normalised rule line.
            :param detail_name: Secondary identifier echoed onto the
                                result.
            :param add: True for add-style verbs; False for
                        remove-style.
            :param skip_message: Human-readable reason to surface on
                                 SKIPPED.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        anchor_name = self._anchor_name(zone=zone)
        anchor_path = self._anchor_path(zone=zone)

        write_command = ["tee", anchor_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=detail_name,
                command=write_command,
            )
            return dry

        existing_body = self._read_file(path=anchor_path)
        if existing_body is None:
            existing_body = ""
        existing_lines = _normalise_lines(text=existing_body)

        if add:
            if rule_line in existing_lines:
                skipped = self._build_result(
                    status=MacosFirewallActionStatus.SKIPPED,
                    zone=zone,
                    detail_name=detail_name,
                    command=write_command,
                    message=skip_message,
                )
                return skipped
            desired_lines = list(existing_lines)
            desired_lines.append(rule_line)
        else:
            if rule_line not in existing_lines:
                skipped = self._build_result(
                    status=MacosFirewallActionStatus.SKIPPED,
                    zone=zone,
                    detail_name=detail_name,
                    command=write_command,
                    message=skip_message,
                )
                return skipped
            desired_lines = [
                line for line in existing_lines if line != rule_line
            ]

        desired_body = _join_anchor_body(lines=desired_lines)

        write_run = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired_body,
        )
        if write_run.exit_code != 0:
            rtnval = MacosFirewallActionResult(
                status=MacosFirewallActionStatus.FAILED,
                detail=detail_name,
                exit_code=write_run.exit_code,
                stdout=write_run.stdout,
                stderr=write_run.stderr,
                command=list(write_command),
                zone=zone,
                detail_name=detail_name,
                message="anchor file rewrite failed",
            )
            return rtnval

        reload_command = [_PFCTL, "-a", anchor_name, "-f", anchor_path]
        reload_run = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_run.exit_code != 0:
            rtnval = MacosFirewallActionResult(
                status=MacosFirewallActionStatus.FAILED,
                detail=detail_name,
                exit_code=reload_run.exit_code,
                stdout=reload_run.stdout,
                stderr=reload_run.stderr,
                command=list(reload_command),
                zone=zone,
                detail_name=detail_name,
                message="pfctl -a reload failed",
            )
            return rtnval

        rtnval = MacosFirewallActionResult(
            status=MacosFirewallActionStatus.SUCCESS,
            detail=detail_name,
            exit_code=reload_run.exit_code,
            stdout=reload_run.stdout,
            stderr=reload_run.stderr,
            command=list(reload_command),
            zone=zone,
            detail_name=detail_name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — paths / config / probes
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosFirewallManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosFirewallManagerConfig`.

            :returns: The active config typed as
                      :class:`MacosFirewallManagerConfig`.
        """
        if isinstance(self._config, MacosFirewallManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosFirewallManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _anchor_path(self, *, zone: str) -> str:
        """
            Build the absolute path to the per-zone anchor file.

            :param zone: Zone identifier.

            :returns: ``<anchor_file_prefix><zone>``.
        """
        rtnval = "{}{}".format(
            self._typed_config().anchor_file_prefix, zone,
        )
        return rtnval

    def _anchor_name(self, *, zone: str) -> str:
        """
            Build the pf anchor name (sans path) for the given
            zone.

            :param zone: Zone identifier.

            :returns: ``automojo.<zone>``.
        """
        rtnval = "{}{}".format(_AUTOMOJO_ANCHOR_NAME_PREFIX, zone)
        return rtnval

    def _effective_services_table(self) -> dict:
        """
            Return the active services table.

            :returns: The configured override when non-None;
                      otherwise the bundled defaults.
        """
        override = self._typed_config().services_table
        if override is None:
            rtnval = dict(_BUNDLED_SERVICES_TABLE)
        else:
            rtnval = dict(override)
        return rtnval

    def _resolve_service(
        self,
        *,
        service: str,
    ) -> Optional[tuple]:
        """
            Resolve a service name to a ``(port, protocol)`` tuple.

            :param service: Service name.

            :returns: A ``(port, protocol)`` tuple, or ``None`` when
                      the service is not in the active table.
        """
        table = self._effective_services_table()
        if service not in table:
            rtnval: Optional[tuple] = None
            return rtnval
        rtnval = table[service]
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

    def _read_default_zone(self) -> str:
        """
            Read ``/etc/pf.conf`` and report the first
            ``automojo.<zone>`` anchor declared in it.

            :returns: The zone identifier (without the ``automojo.``
                      prefix), or empty string when none is present.
        """
        pf_conf_path = self._typed_config().pf_conf_path
        body = self._read_file(path=pf_conf_path)
        if body is None:
            rtnval = ""
            return rtnval
        rtnval = _first_automojo_zone_in_pfconf(body=body)
        return rtnval

    def _zone_is_default(self, *, zone: str) -> bool:
        """
            Probe whether ``zone`` is currently the default
            (first-listed) automojo anchor.

            :param zone: Zone identifier.

            :returns: True when ``zone`` is the first
                      ``automojo.*`` anchor in pf-conf.
        """
        rtnval = self._read_default_zone() == zone
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosFirewallActionStatus,
        zone: Optional[str],
        detail_name: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosFirewallActionResult:
        """
            Construct a :class:`MacosFirewallActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param zone: Zone to echo onto the result.
            :param detail_name: Secondary identifier to echo onto
                                the result.
            :param command: The would-have-been argv (copied for
                            capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`MacosFirewallActionResult`.
        """
        rtnval = MacosFirewallActionResult(
            status=status,
            detail=detail_name,
            command=list(command),
            zone=zone,
            detail_name=detail_name,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level rule helpers
# ----------------------------------------------------------------------


def _build_pass_rule(*, port: int, protocol: str) -> str:
    """
        Build the canonical pf pass-rule line for a port / protocol
        pair.

        :param port: Port number.
        :param protocol: ``"tcp"`` or ``"udp"``.

        :returns: ``"pass in quick proto <protocol> from any to any
                  port <port>"``.
    """
    rtnval = (
        "pass in quick proto {} from any to any port {}".format(
            protocol, port,
        )
    )
    return rtnval


def _normalise_rule(*, rule: str) -> str:
    """
        Collapse internal whitespace runs and trim ends so coordinator-
        supplied rules match the parser's reconstructed form.

        :param rule: The input rule string.

        :returns: The whitespace-normalised rule string.
    """
    tokens = rule.split()
    if len(tokens) == 0:
        rtnval = ""
        return rtnval
    rtnval = " ".join(tokens)
    return rtnval


def _extract_verb(*, rule: str) -> Optional[str]:
    """
        Return the leading rule verb of ``rule`` when it is one of
        the accepted pf verbs; otherwise ``None``.

        :param rule: A normalised rule string.

        :returns: The verb, or ``None``.
    """
    tokens = rule.split()
    if len(tokens) == 0:
        rtnval: Optional[str] = None
        return rtnval
    head = tokens[0].lower()
    if head in _VALID_RULE_VERBS:
        rtnval = head
        return rtnval
    rtnval = None
    return rtnval


def _normalise_lines(*, text: str) -> list[str]:
    """
        Split a body into normalised, non-comment, non-blank lines.

        :param text: Raw file content.

        :returns: The whitespace-normalised lines that should be
                  considered part of the anchor's logical content.
    """
    rtnval: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        normalised = _normalise_rule(rule=stripped)
        rtnval.append(normalised)
    return rtnval


def _join_anchor_body(*, lines: list[str]) -> str:
    """
        Reassemble normalised lines into an anchor file body.

        Adds a trailing newline so subsequent ``cat`` reads are
        byte-stable.

        :param lines: Normalised rule lines.

        :returns: The full file body.
    """
    if len(lines) == 0:
        rtnval = ""
        return rtnval
    rtnval = "\n".join(lines) + "\n"
    return rtnval


def _ensure_anchor_first(
    *,
    existing: str,
    anchor_name: str,
    anchor_path: str,
) -> str:
    """
        Return a pf-conf body in which ``anchor_name`` appears as
        the FIRST automojo anchor, with both the ``anchor`` and
        ``load anchor`` directives present.

        Existing automojo anchor declarations are removed; non-
        automojo content is preserved verbatim.

        :param existing: Current pf-conf content.
        :param anchor_name: Anchor name to install
                            (e.g. ``automojo.trusted``).
        :param anchor_path: Anchor file path.

        :returns: The desired pf-conf body, terminated by ``\\n``.
    """
    preserved_lines: list[str] = []
    for raw_line in existing.splitlines():
        stripped = raw_line.strip()
        if _is_automojo_anchor_line(line=stripped):
            continue
        preserved_lines.append(raw_line.rstrip())

    new_lines = list(preserved_lines)
    if len(new_lines) > 0 and len(new_lines[-1].strip()) > 0:
        new_lines.append("")
    new_lines.append('anchor "{}"'.format(anchor_name))
    new_lines.append(
        'load anchor "{}" from "{}"'.format(anchor_name, anchor_path),
    )

    rtnval = "\n".join(new_lines) + "\n"
    return rtnval


def _is_automojo_anchor_line(*, line: str) -> bool:
    """
        Return True for any pf-conf line that declares or loads an
        ``automojo.*`` anchor.

        :param line: A trimmed pf-conf line.

        :returns: True when the line is an automojo anchor /
                  load-anchor directive.
    """
    if not line.startswith("anchor ") and not line.startswith("load anchor "):
        rtnval = False
        return rtnval
    if "\"automojo." in line:
        rtnval = True
        return rtnval
    if "'automojo." in line:
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval


def _first_automojo_zone_in_pfconf(*, body: str) -> str:
    """
        Return the zone identifier of the first ``automojo.<zone>``
        ``anchor "..."`` directive in pf-conf, or empty string when
        none is present.

        :param body: pf-conf content.

        :returns: The zone identifier, or empty string.
    """
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("anchor "):
            continue
        if "\"automojo." not in stripped:
            continue
        # Extract the value between the first pair of double quotes.
        first_quote = stripped.find("\"")
        if first_quote < 0:
            continue
        last_quote = stripped.find("\"", first_quote + 1)
        if last_quote < 0:
            continue
        anchor_name = stripped[first_quote + 1: last_quote]
        if not anchor_name.startswith(_AUTOMOJO_ANCHOR_NAME_PREFIX):
            continue
        rtnval = anchor_name[len(_AUTOMOJO_ANCHOR_NAME_PREFIX):]
        return rtnval

    rtnval = ""
    return rtnval


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _parse_anchor_rules(
    *,
    zone: str,
    text: str,
    services_table: dict,
    is_default: bool,
) -> MacosZoneInfo:
    """
        Parse ``pfctl -a <anchor> -sr`` output into a
        :class:`MacosZoneInfo`.

        Each non-empty, non-comment line is interpreted as a rule.
        Lines that match the canonical pass-rule shape with a single
        port / protocol are added to ``ports`` and (when matched
        against the services table) ``services``.  All other lines
        are preserved verbatim in ``rich_rules``.

        :param zone: Zone identifier (used to stamp on the result).
        :param text: Raw stdout text from ``pfctl -a ... -sr``.
        :param services_table: Services-table map used for
                               port-to-service back-resolution.
        :param is_default: Pre-computed ``is_default`` flag.

        :returns: The parsed :class:`MacosZoneInfo`.
    """
    info = MacosZoneInfo(zone=zone, is_default=is_default)

    # Build a reverse map for back-resolution: (port, protocol) -> name.
    reverse_map: dict = {}
    for service_name, port_proto in services_table.items():
        if not isinstance(port_proto, tuple):
            continue
        if len(port_proto) != 2:
            continue
        port_value = port_proto[0]
        proto_value = port_proto[1]
        reverse_map[(int(port_value), str(proto_value))] = service_name

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        normalised = _normalise_rule(rule=stripped)

        match = _RULE_PORT_PATTERN.search(normalised)
        if match is not None and normalised.startswith("pass"):
            proto_value = match.group(1).lower()
            try:
                port_value = int(match.group(2))
            except ValueError:
                port_value = -1
            if port_value > 0:
                port_spec = "{}/{}".format(port_value, proto_value)
                if port_spec not in info.ports:
                    info.ports.append(port_spec)
                key = (port_value, proto_value)
                if key in reverse_map:
                    service_name = reverse_map[key]
                    if service_name not in info.services:
                        info.services.append(service_name)
                continue

        if normalised not in info.rich_rules:
            info.rich_rules.append(normalised)

    return info


def _extract_automojo_zones(*, text: str) -> list[str]:
    """
        Filter ``pfctl -sA`` output to anchor names whose stem starts
        with ``automojo.`` and return the trailing zone identifiers.

        ``pfctl -sA`` emits one anchor name per line, each indented
        with leading whitespace.

        :param text: Raw stdout text.

        :returns: The discovered zone identifiers in declaration
                  order, deduplicated.
    """
    rtnval: list[str] = []
    seen: set = set()
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if not stripped.startswith(_AUTOMOJO_ANCHOR_NAME_PREFIX):
            continue
        zone_value = stripped[len(_AUTOMOJO_ANCHOR_NAME_PREFIX):]
        if len(zone_value) == 0:
            continue
        if zone_value in seen:
            continue
        seen.add(zone_value)
        rtnval.append(zone_value)
    return rtnval


def _pfctl_si_is_enabled(*, text: str) -> bool:
    """
        Detect pf-enabled in ``pfctl -si`` output.

        ``pfctl -si`` emits a ``Status:`` line whose value is
        ``Enabled`` when pf is running.

        :param text: Raw stdout.

        :returns: True when the status line reports ``Enabled``.
    """
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.lower().startswith("status:"):
            continue
        _, _, value = line.partition(":")
        value_lower = value.strip().lower()
        if value_lower.startswith("enabled"):
            rtnval = True
            return rtnval
        rtnval = False
        return rtnval

    rtnval = False
    return rtnval
