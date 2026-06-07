"""
    macOS network-configuration feature.

    Implements the cross-OS network-configuration contract for macOS using
    ``networksetup`` (the canonical persistent surface, which writes through
    System Configuration / ``configd``), ``ifconfig`` for runtime interface
    detail, BSD ``route`` for routes, and ``scutil`` for the macOS hostname
    triple (``HostName`` / ``ComputerName`` / ``LocalHostName``).

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner` (``ifconfig``, ``networksetup``, ``route``,
        ``scutil``).  This module does NOT consume an
        :class:`IFileOperator`.  ``networksetup`` owns its own persistence
        layer; we never write configuration files directly.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads (``get_*``,
        ``_resolve_service_name``, idempotency probes) never elevate.

    Surface choices:
        * Persistent IPv4 addressing goes through
          ``networksetup -setmanual`` rather than runtime ``ifconfig``,
          because ``configd`` reconciles ``networksetup`` writes back into
          the running interface, which gives us both runtime and
          persistent state from a single command.
        * Routes use BSD ``route -n add`` / ``route -n delete``, which
          are runtime-only on macOS.  The result's ``message`` field
          notes that persistent routes are out of scope (a future
          extension would author a launchd plist).
        * DNS uses ``networksetup -setdnsservers``, which is also
          persistent.  Setting the literal ``empty`` clears the list.
        * The hostname is set across all three macOS surfaces in order
          (``HostName``, ``ComputerName``, ``LocalHostName``); the first
          failing call short-circuits the rest and surfaces the failing
          surface in ``message``.

    Interface-name mapping:
        ``networksetup`` operates on service names ("Wi-Fi", "Ethernet",
        ...); ``ifconfig`` / ``route`` operate on BSD device names
        ("en0", "en1", ...).  This manager accepts the BSD device name
        as the canonical ``name`` argument and resolves it to a service
        name via :func:`_resolve_service_name`, which scans
        ``networksetup -listallhardwareports`` once and caches the map
        for the manager's lifetime.
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


_NETWORKSETUP = "networksetup"
_IFCONFIG = "ifconfig"
_ROUTE = "route"
_SCUTIL = "scutil"


# ``networksetup -getdnsservers`` returns this literal sentence when no DNS
# servers are configured for the queried service.  We compare a stripped
# prefix because the service name is interpolated at the end of the line.
_DNS_EMPTY_PREFIX = "There aren't any DNS Servers set"


# Regular-expression patterns for the various ``ifconfig`` lines we care
# about.  These are module-level constants so they compile once.
_IFCONFIG_FIRST_LINE = re.compile(
    r"^([A-Za-z0-9_]+):\s+flags=\d+<([^>]*)>(?:\s+mtu\s+(\d+))?",
)
_IFCONFIG_ETHER = re.compile(r"^\s*ether\s+([0-9a-fA-F:]+)")
_IFCONFIG_INET = re.compile(
    r"^\s*inet\s+([0-9.]+)\s+netmask\s+0x([0-9a-fA-F]+)",
)
_IFCONFIG_INET6 = re.compile(
    r"^\s*inet6\s+([0-9a-fA-F:]+)(?:%[A-Za-z0-9_]+)?\s+prefixlen\s+(\d+)",
)


MacosNetworkActionStatus = ActionStatus


@dataclass
class MacosNetworkConfigurationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosNetworkConfigurationManager`.
        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.
    """
    pass


@dataclass
class MacosNetworkActionResult(FeatureActionResult):
    """
        Result of a network-configuration write operation.

        :param interface: BSD device name when the operation is
                          interface-scoped, else ``None``.
        :param service_name: ``networksetup`` service name resolved
                             from ``interface``, when applicable.
        :param destination: Route destination, for the route verbs.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes (or a contract-mandated
                        note for ``add_route`` / ``remove_route``).
    """
    interface: Optional[str] = None
    service_name: Optional[str] = None
    destination: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosInterfaceInfo:
    """
        Summary of a single network interface.

        :param device: BSD ifname (e.g. ``en0``).
        :param service_name: The ``networksetup`` service name mapped
                             to ``device``, or empty when no mapping
                             exists (e.g. ``lo0``).
        :param oper_state: ``"up"`` when the ``ifconfig`` first-line
                           flags include ``UP``; otherwise ``"down"``.
        :param mac_address: Hardware address from ``ether`` line, or
                            ``None`` when the interface has none.
        :param ipv4_addresses: ``addr/prefix`` strings parsed from
                               ``inet`` lines (hex netmask converted to
                               numeric prefix).
        :param ipv6_addresses: ``addr/prefix`` strings parsed from
                               ``inet6`` lines.
        :param mtu: MTU from the first-line ``mtu`` field, or ``None``
                    when not reported.
    """
    device: str
    service_name: str
    oper_state: str
    mac_address: Optional[str]
    ipv4_addresses: list[str] = field(default_factory=list)
    ipv6_addresses: list[str] = field(default_factory=list)
    mtu: Optional[int] = None


@dataclass
class MacosRouteInfo:
    """
        Parsed routing-table entry.

        :param destination: Destination prefix.  ``"default"`` is
                            normalised to ``"0.0.0.0/0"`` (or ``"::/0"``
                            for IPv6 rows).
        :param gateway: Gateway IP, or ``None`` when the table reports
                        ``link#N`` / a MAC-shaped value.
        :param interface: Outgoing interface (the ``Netif`` column).
        :param metric: Always ``None`` on macOS — ``netstat -rn`` does
                       not surface a metric column.
    """
    destination: str
    gateway: Optional[str]
    interface: Optional[str]
    metric: Optional[int] = None


class MacosNetworkQueryError(Exception):
    """
        Raised when a network read fails for a reason other than the
        target simply being absent.
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

        err_msg_lines = ["Network query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosNetworkConfigurationManager(BaseFeatureManager):
    """
        macOS network-configuration manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`; ``networksetup``
        owns its own persistence so this manager performs no direct
        file I/O.
    """

    result_class = MacosNetworkActionResult
    status_class = MacosNetworkActionStatus
    feature_name = "network-configuration"

    def __init__(
        self,
        runner,
        config: Optional[MacosNetworkConfigurationManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every operation.
            :param config: Manager configuration.  Defaults to a fresh
                           :class:`MacosNetworkConfigurationManagerConfig`
                           when omitted.
        """
        if config is None:
            config = MacosNetworkConfigurationManagerConfig()

        super().__init__(runner=runner, config=config)

        # Cache of BSD-device -> networksetup-service-name, populated
        # lazily by :meth:`_resolve_service_name` so we do not repeat
        # the listing on every verb.  ``None`` means uninitialised.
        self._service_name_cache: Optional[dict[str, str]] = None
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_interfaces(self) -> list[MacosInterfaceInfo]:
        """
            Enumerate network interfaces.

            Calls ``networksetup -listallhardwareports`` to map BSD
            device names to service names, then ``ifconfig <device>``
            for each device to extract IP / MAC / MTU detail.  Devices
            that have no service-name mapping (e.g. ``lo0``,
            ``utun0``) still appear in the result with an empty
            ``service_name``.  Never elevates.

            :returns: One :class:`MacosInterfaceInfo` per discovered
                      device.
            :raises MacosNetworkQueryError: When
                                            ``-listallhardwareports``
                                            exits non-zero.
        """
        port_map = self._load_hardware_port_map()
        # ``ifconfig -l`` is the cheapest way to enumerate every BSD
        # device on the host (including non-networksetup devices like
        # ``lo0`` and tunnel interfaces).  Fall back to the keys of the
        # hardware-port map when ``-l`` is unavailable.
        list_command = [_IFCONFIG, "-l"]
        list_run = self._runner.run(list(list_command), elevated=False)
        if list_run.exit_code == 0:
            devices = list_run.stdout.split()
        else:
            devices = list(port_map.keys())

        rtnval: list[MacosInterfaceInfo] = []
        for device in devices:
            stripped = device.strip()
            if len(stripped) == 0:
                continue
            try:
                info = self._read_interface(
                    device=stripped, port_map=port_map,
                )
            except MacosNetworkQueryError:
                continue
            if info is None:
                continue
            rtnval.append(info)

        return rtnval

    def configure_interface(
        self,
        name: str,
        address: str,
        netmask: str,
        gateway: Optional[str],
    ) -> MacosNetworkActionResult:
        """
            Configure persistent IPv4 addressing for ``name`` via
            ``networksetup -setmanual``.

            Idempotent: when ``networksetup -getinfo`` already reports
            a matching ``IP address`` / ``Subnet mask`` / ``Router``
            triple, returns ``SKIPPED``.

            :param name: BSD device name (e.g. ``en0``).
            :param address: IPv4 address in dotted-quad form.
            :param netmask: IPv4 netmask in dotted-quad form (e.g.
                            ``"255.255.255.0"``).
            :param gateway: Default gateway, or ``None`` to pass an
                            empty string to ``networksetup``.

            :returns: The :class:`MacosNetworkActionResult`.
        """
        try:
            service_name = self._resolve_service_name(device=name)
        except MacosNetworkQueryError as exc:
            failed = self._build_result(
                status=MacosNetworkActionStatus.FAILED,
                interface=name,
                service_name=None,
                detail=name,
                command=[_NETWORKSETUP, "-listallhardwareports"],
                message="service_name_resolution_failed",
                stderr=str(exc),
            )
            return failed

        if gateway is None:
            gateway_arg = ""
        else:
            gateway_arg = gateway

        command = [
            _NETWORKSETUP, "-setmanual", service_name,
            address, netmask, gateway_arg,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNetworkActionStatus.DRY_RUN,
                interface=name,
                service_name=service_name,
                detail=name,
                command=command,
            )
            return dry

        if self._getinfo_matches(
            service_name=service_name,
            address=address,
            netmask=netmask,
            gateway=gateway,
        ):
            skipped = self._build_result(
                status=MacosNetworkActionStatus.SKIPPED,
                interface=name,
                service_name=service_name,
                detail=name,
                command=command,
                message="interface configuration already matches",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=name,
            service_name=service_name,
            detail=name,
        )
        return rtnval

    def enable_interface(self, name: str) -> MacosNetworkActionResult:
        """
            Enable a network service via
            ``networksetup -setnetworkserviceenabled <service> on``.

            Idempotent: when
            ``networksetup -getnetworkserviceenabled`` already reports
            ``Enabled``, returns ``SKIPPED``.

            :param name: BSD device name.

            :returns: The :class:`MacosNetworkActionResult`.
        """
        try:
            service_name = self._resolve_service_name(device=name)
        except MacosNetworkQueryError as exc:
            failed = self._build_result(
                status=MacosNetworkActionStatus.FAILED,
                interface=name,
                service_name=None,
                detail=name,
                command=[_NETWORKSETUP, "-listallhardwareports"],
                message="service_name_resolution_failed",
                stderr=str(exc),
            )
            return failed

        command = [
            _NETWORKSETUP, "-setnetworkserviceenabled", service_name, "on",
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNetworkActionStatus.DRY_RUN,
                interface=name,
                service_name=service_name,
                detail=name,
                command=command,
            )
            return dry

        if self._service_is_enabled(service_name=service_name):
            skipped = self._build_result(
                status=MacosNetworkActionStatus.SKIPPED,
                interface=name,
                service_name=service_name,
                detail=name,
                command=command,
                message="service is already enabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=name,
            service_name=service_name,
            detail=name,
        )
        return rtnval

    def disable_interface(self, name: str) -> MacosNetworkActionResult:
        """
            Disable a network service via
            ``networksetup -setnetworkserviceenabled <service> off``.

            Idempotent: when
            ``networksetup -getnetworkserviceenabled`` already reports
            ``Disabled``, returns ``SKIPPED``.

            :param name: BSD device name.

            :returns: The :class:`MacosNetworkActionResult`.
        """
        try:
            service_name = self._resolve_service_name(device=name)
        except MacosNetworkQueryError as exc:
            failed = self._build_result(
                status=MacosNetworkActionStatus.FAILED,
                interface=name,
                service_name=None,
                detail=name,
                command=[_NETWORKSETUP, "-listallhardwareports"],
                message="service_name_resolution_failed",
                stderr=str(exc),
            )
            return failed

        command = [
            _NETWORKSETUP, "-setnetworkserviceenabled", service_name, "off",
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNetworkActionStatus.DRY_RUN,
                interface=name,
                service_name=service_name,
                detail=name,
                command=command,
            )
            return dry

        if not self._service_is_enabled(service_name=service_name):
            skipped = self._build_result(
                status=MacosNetworkActionStatus.SKIPPED,
                interface=name,
                service_name=service_name,
                detail=name,
                command=command,
                message="service is already disabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=name,
            service_name=service_name,
            detail=name,
        )
        return rtnval

    def add_route(
        self,
        dest: str,
        gateway: str,
    ) -> MacosNetworkActionResult:
        """
            Add a route via ``route -n add <dest> <gateway>``.

            macOS BSD ``route`` is positional (no ``via`` keyword).
            Routes added this way are runtime-only; the result's
            ``message`` notes the limitation per contract.

            Idempotent: when ``route -n get <dest>`` already reports a
            matching gateway, returns ``SKIPPED``.

            :param dest: Destination prefix.
            :param gateway: Gateway IP.

            :returns: The :class:`MacosNetworkActionResult`.
        """
        command = [_ROUTE, "-n", "add", dest, gateway]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNetworkActionStatus.DRY_RUN,
                interface=None,
                service_name=None,
                detail=dest,
                destination=dest,
                command=command,
                message="macOS routes are runtime-only via BSD route",
            )
            return dry

        existing_gateway = self._route_get_gateway(dest=dest)
        if existing_gateway is not None and existing_gateway == gateway:
            skipped = self._build_result(
                status=MacosNetworkActionStatus.SKIPPED,
                interface=None,
                service_name=None,
                detail=dest,
                destination=dest,
                command=command,
                message="route already present with matching gateway",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=None,
            service_name=None,
            detail=dest,
            destination=dest,
            success_message="macOS routes are runtime-only via BSD route",
        )
        return rtnval

    def remove_route(self, dest: str) -> MacosNetworkActionResult:
        """
            Remove a route via ``route -n delete <dest>``.

            Idempotent: when ``route -n get <dest>`` reports no
            matching route, returns ``SKIPPED``.

            :param dest: Destination prefix.

            :returns: The :class:`MacosNetworkActionResult`.
        """
        command = [_ROUTE, "-n", "delete", dest]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNetworkActionStatus.DRY_RUN,
                interface=None,
                service_name=None,
                detail=dest,
                destination=dest,
                command=command,
            )
            return dry

        existing_gateway = self._route_get_gateway(dest=dest)
        if existing_gateway is None:
            skipped = self._build_result(
                status=MacosNetworkActionStatus.SKIPPED,
                interface=None,
                service_name=None,
                detail=dest,
                destination=dest,
                command=command,
                message="route is not present",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=None,
            service_name=None,
            detail=dest,
            destination=dest,
        )
        return rtnval

    def set_dns_servers(
        self,
        interface: str,
        servers: list[str],
    ) -> MacosNetworkActionResult:
        """
            Set per-service DNS servers via
            ``networksetup -setdnsservers <service> <servers...>``.

            When ``servers`` is empty the literal ``empty`` is passed,
            which is the way ``networksetup`` clears the DNS list.

            Idempotent: when the sorted current list matches the
            sorted desired list, returns ``SKIPPED``.

            :param interface: BSD device name.
            :param servers: Ordered list of DNS server IPs.  An empty
                            list clears the configuration.

            :returns: The :class:`MacosNetworkActionResult`.
        """
        try:
            service_name = self._resolve_service_name(device=interface)
        except MacosNetworkQueryError as exc:
            failed = self._build_result(
                status=MacosNetworkActionStatus.FAILED,
                interface=interface,
                service_name=None,
                detail=interface,
                command=[_NETWORKSETUP, "-listallhardwareports"],
                message="service_name_resolution_failed",
                stderr=str(exc),
            )
            return failed

        if len(servers) == 0:
            command = [_NETWORKSETUP, "-setdnsservers", service_name, "empty"]
        else:
            command = (
                [_NETWORKSETUP, "-setdnsservers", service_name]
                + list(servers)
            )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNetworkActionStatus.DRY_RUN,
                interface=interface,
                service_name=service_name,
                detail=interface,
                command=command,
            )
            return dry

        try:
            current = self.get_dns_servers(interface=interface)
        except MacosNetworkQueryError:
            current = []

        if sorted(current) == sorted(servers):
            skipped = self._build_result(
                status=MacosNetworkActionStatus.SKIPPED,
                interface=interface,
                service_name=service_name,
                detail=interface,
                command=command,
                message="DNS servers already match desired list",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=interface,
            service_name=service_name,
            detail=interface,
        )
        return rtnval

    def get_dns_servers(self, interface: str) -> list[str]:
        """
            Read DNS servers configured on ``interface`` via
            ``networksetup -getdnsservers <service>``.

            ``networksetup`` emits one IP per line when servers are
            configured, or the literal sentence "There aren't any DNS
            Servers set on <service>." when empty.  Reads do not
            elevate.

            :param interface: BSD device name.

            :returns: List of DNS servers in declaration order.  Empty
                      when the placeholder line is reported.
            :raises MacosNetworkQueryError: When the service-name
                                            resolution fails or when
                                            ``networksetup`` exits
                                            non-zero.
        """
        service_name = self._resolve_service_name(device=interface)
        command = [_NETWORKSETUP, "-getdnsservers", service_name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosNetworkQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_getdnsservers(text=run_result.stdout)
        return rtnval

    def get_hostname(self) -> str:
        """
            Read the current Unix hostname via ``scutil --get HostName``.

            :returns: The hostname (whitespace stripped), or an empty
                      string when ``scutil`` reports a non-zero exit.
        """
        rtnval = self._scutil_get(field_name="HostName")
        return rtnval

    def set_hostname(self, name: str) -> MacosNetworkActionResult:
        """
            Set the macOS hostname triple via three sequential
            ``scutil --set`` calls.

            macOS maintains three independent hostname fields:
            ``HostName`` (the Unix hostname), ``ComputerName`` (the
            display name shown in System Settings), and
            ``LocalHostName`` (the Bonjour name; dots are normalised
            to dashes by macOS).  This method writes all three so a
            cross-OS coordinator using ``set_hostname`` can rely on
            the same single-call semantics that
            ``hostnamectl set-hostname`` provides on Linux.

            Idempotent: when all three ``scutil --get`` reads already
            match (with ``LocalHostName`` compared after normalising
            ``.`` -> ``-``), returns ``SKIPPED``.  Stops at the first
            failing surface and surfaces it in ``message``.

            :param name: Desired hostname.

            :returns: The :class:`MacosNetworkActionResult`.
        """
        # ``LocalHostName`` cannot contain dots — macOS replaces them
        # with dashes for Bonjour purposes.  Normalise eagerly so the
        # idempotency guard compares apples to apples.
        local_name = name.replace(".", "-")

        # The argv we record on the result is the FIRST scutil --set
        # call (the ``HostName`` write).  This mirrors how the Linux
        # spec records the canonical hostname-write command.
        primary_command = [_SCUTIL, "--set", "HostName", name]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNetworkActionStatus.DRY_RUN,
                interface=None,
                service_name=None,
                detail=name,
                command=primary_command,
            )
            return dry

        current_host = self._scutil_get(field_name="HostName")
        current_computer = self._scutil_get(field_name="ComputerName")
        current_local = self._scutil_get(field_name="LocalHostName")

        if (
            current_host == name
            and current_computer == name
            and current_local == local_name
        ):
            skipped = self._build_result(
                status=MacosNetworkActionStatus.SKIPPED,
                interface=None,
                service_name=None,
                detail=name,
                command=primary_command,
                message="hostname triple is already set",
            )
            return skipped

        # Write order: HostName -> ComputerName -> LocalHostName.  Stop
        # at the first failing surface and report it in ``message``.
        write_plan = [
            ("HostName", name),
            ("ComputerName", name),
            ("LocalHostName", local_name),
        ]
        last_run = None
        last_command = primary_command
        for field_name, field_value in write_plan:
            this_command = [_SCUTIL, "--set", field_name, field_value]
            this_run = self._runner.run(
                list(this_command),
                elevated=self._config.elevated,
            )
            last_run = this_run
            last_command = this_command
            if this_run.exit_code != 0:
                failed = MacosNetworkActionResult(
                    status=MacosNetworkActionStatus.FAILED,
                    detail=name,
                    exit_code=this_run.exit_code,
                    stdout=this_run.stdout,
                    stderr=this_run.stderr,
                    command=list(this_command),
                    interface=None,
                    service_name=None,
                    message="scutil_set_{}_failed".format(
                        field_name.lower(),
                    ),
                )
                return failed

        success = MacosNetworkActionResult(
            status=MacosNetworkActionStatus.SUCCESS,
            detail=name,
            exit_code=last_run.exit_code,
            stdout=last_run.stdout,
            stderr=last_run.stderr,
            command=list(last_command),
            interface=None,
            service_name=None,
        )
        return success

    # ------------------------------------------------------------------
    # Internal helpers — reads / probes / dispatch / result construction
    # ------------------------------------------------------------------

    def _load_hardware_port_map(self) -> dict[str, str]:
        """
            Populate (and cache) the BSD-device -> service-name map.

            The map is computed once per manager instance and reused
            on every subsequent call.  Reads do not elevate.

            :returns: ``dict[str, str]`` keyed by BSD device name.
            :raises MacosNetworkQueryError: When
                                            ``-listallhardwareports``
                                            exits non-zero.
        """
        if self._service_name_cache is not None:
            return self._service_name_cache

        command = [_NETWORKSETUP, "-listallhardwareports"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosNetworkQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        port_map = _parse_hardware_ports(text=run_result.stdout)
        self._service_name_cache = port_map
        return port_map

    def _resolve_service_name(self, *, device: str) -> str:
        """
            Resolve a BSD device name to its ``networksetup`` service
            name.

            :param device: BSD device name (e.g. ``en0``).

            :returns: The matching service name.
            :raises MacosNetworkQueryError: When the device has no
                                            service mapping or when
                                            the listing call fails.
        """
        port_map = self._load_hardware_port_map()
        if device in port_map:
            rtnval = port_map[device]
            return rtnval

        err_msg_lines = [
            "No networksetup service maps to BSD device {!r}.".format(device),
        ]
        err_msg_lines.append("    KNOWN DEVICES:")
        for known in sorted(port_map.keys()):
            err_msg_lines.append(
                "        {} -> {}".format(known, port_map[known]),
            )
        err_msg = os.linesep.join(err_msg_lines)
        raise MacosNetworkQueryError(
            command=[_NETWORKSETUP, "-listallhardwareports"],
            exit_code=0,
            stderr=err_msg,
        )

    def _read_interface(
        self,
        *,
        device: str,
        port_map: dict[str, str],
    ) -> Optional[MacosInterfaceInfo]:
        """
            Read a single interface's state via ``ifconfig <device>``.

            :param device: BSD device name.
            :param port_map: Pre-built device -> service-name map.

            :returns: The :class:`MacosInterfaceInfo`, or ``None`` when
                      ``ifconfig`` reports the device does not exist.
            :raises MacosNetworkQueryError: When ``ifconfig`` exits
                                            non-zero for another
                                            reason.
        """
        command = [_IFCONFIG, device]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_lower = run_result.stderr.lower()
            if "does not exist" in stderr_lower:
                rtnval: Optional[MacosInterfaceInfo] = None
                return rtnval
            raise MacosNetworkQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        if device in port_map:
            service_name = port_map[device]
        else:
            service_name = ""

        rtnval = _parse_ifconfig(
            text=run_result.stdout,
            device=device,
            service_name=service_name,
        )
        return rtnval

    def _getinfo_matches(
        self,
        *,
        service_name: str,
        address: str,
        netmask: str,
        gateway: Optional[str],
    ) -> bool:
        """
            Probe ``networksetup -getinfo <service>`` and report whether
            the existing IP / mask / router triple matches the desired
            one.

            :param service_name: Service name.
            :param address: Desired IPv4 address.
            :param netmask: Desired netmask.
            :param gateway: Desired gateway, or ``None``.

            :returns: True when the existing configuration matches.
        """
        command = [_NETWORKSETUP, "-getinfo", service_name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        existing = _parse_getinfo(text=run_result.stdout)
        existing_address = existing.get("IP address", "")
        existing_netmask = existing.get("Subnet mask", "")
        existing_router = existing.get("Router", "")

        if gateway is None:
            wanted_router = ""
        else:
            wanted_router = gateway

        rtnval = (
            existing_address == address
            and existing_netmask == netmask
            and existing_router == wanted_router
        )
        return rtnval

    def _service_is_enabled(self, *, service_name: str) -> bool:
        """
            Probe ``networksetup -getnetworkserviceenabled <service>``.

            :param service_name: Service name.

            :returns: True when the command's stdout contains
                      ``"Enabled"``; False otherwise (including when
                      the call exits non-zero).
        """
        command = [
            _NETWORKSETUP, "-getnetworkserviceenabled", service_name,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        text = run_result.stdout.strip()
        rtnval = (text == "Enabled")
        return rtnval

    def _route_get_gateway(self, *, dest: str) -> Optional[str]:
        """
            Probe ``route -n get <dest>`` and extract the gateway.

            ``route get`` emits human-readable lines such as
            ``  gateway: 10.0.1.1``.  When no matching route exists,
            ``route`` exits non-zero and stderr contains
            ``"not in table"``.

            :param dest: Destination prefix.

            :returns: The gateway IP, or ``None`` when no matching
                      route is reported.
        """
        command = [_ROUTE, "-n", "get", dest]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        rtnval = _parse_route_get_gateway(text=run_result.stdout)
        return rtnval

    def _scutil_get(self, *, field_name: str) -> str:
        """
            Read a single ``scutil --get <FieldName>`` value.

            Reads do not elevate.

            :param field_name: One of ``"HostName"``, ``"ComputerName"``,
                               ``"LocalHostName"``.

            :returns: Stripped stdout, or an empty string on failure.
        """
        command = [_SCUTIL, "--get", field_name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout.strip()
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        interface: Optional[str],
        service_name: Optional[str],
        detail: Optional[str],
        destination: Optional[str] = None,
        success_message: Optional[str] = None,
    ) -> MacosNetworkActionResult:
        """
            Dispatch a write command, honouring elevation, and map the
            runner result onto a :class:`MacosNetworkActionResult`.

            :param command: Full argv.
            :param interface: BSD device name, when interface-scoped.
            :param service_name: Service name, when interface-scoped.
            :param detail: Result ``detail`` echo.
            :param destination: Route destination, when applicable.
            :param success_message: Optional human-readable note that
                                    is preserved on a SUCCESS result
                                    (e.g. the runtime-only note for
                                    ``add_route``).

            :returns: The :class:`MacosNetworkActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosNetworkActionStatus.SUCCESS
            mapped_message = success_message
        else:
            mapped_status = MacosNetworkActionStatus.FAILED
            mapped_message = None

        rtnval = MacosNetworkActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            interface=interface,
            service_name=service_name,
            destination=destination,
            message=mapped_message,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosNetworkActionStatus,
        interface: Optional[str],
        service_name: Optional[str],
        detail: Optional[str],
        command: list[str],
        destination: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosNetworkActionResult:
        """
            Construct a :class:`MacosNetworkActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param interface: BSD device name.
            :param service_name: Resolved service name.
            :param detail: Result ``detail`` value.
            :param command: Would-have-been argv (copied for capture).
            :param destination: Optional route destination.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`MacosNetworkActionResult`.
        """
        rtnval = MacosNetworkActionResult(
            status=status,
            detail=detail,
            command=list(command),
            interface=interface,
            service_name=service_name,
            destination=destination,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _parse_hardware_ports(*, text: str) -> dict[str, str]:
    """
        Parse ``networksetup -listallhardwareports`` output into a
        BSD-device -> service-name dict.

        Output shape (blank-line separated blocks)::

            Hardware Port: Wi-Fi
            Device: en0
            Ethernet Address: aa:bb:cc:dd:ee:ff

            Hardware Port: Ethernet
            Device: en1
            Ethernet Address: 11:22:33:44:55:66

        :param text: Raw stdout.

        :returns: ``dict[str, str]`` keyed by BSD device name.
    """
    rtnval: dict[str, str] = {}
    current_port: Optional[str] = None
    current_device: Optional[str] = None

    def flush() -> None:
        if current_port is not None and current_device is not None:
            rtnval[current_device] = current_port
        return

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if len(stripped) == 0:
            flush()
            current_port = None
            current_device = None
            continue
        if stripped.startswith("Hardware Port:"):
            # New block — flush any previous one in case the input had
            # no blank-line separators.
            flush()
            current_port = stripped[len("Hardware Port:"):].strip()
            current_device = None
            continue
        if stripped.startswith("Device:"):
            current_device = stripped[len("Device:"):].strip()
            continue

    flush()
    return rtnval


def _parse_ifconfig(
    *,
    text: str,
    device: str,
    service_name: str,
) -> Optional[MacosInterfaceInfo]:
    """
        Parse ``ifconfig <device>`` output into a
        :class:`MacosInterfaceInfo`.

        :param text: Raw stdout from ``ifconfig <device>``.
        :param device: BSD device name expected on the first line.
        :param service_name: Pre-resolved service name (``""`` when
                             the device has no networksetup mapping).

        :returns: The :class:`MacosInterfaceInfo`, or ``None`` when no
                  parseable first line is found.
    """
    oper_state = "down"
    mtu: Optional[int] = None
    mac_address: Optional[str] = None
    ipv4_addresses: list[str] = []
    ipv6_addresses: list[str] = []

    found_first = False
    for raw in text.splitlines():
        if not found_first:
            match = _IFCONFIG_FIRST_LINE.match(raw)
            if match is not None:
                flags_text = match.group(2)
                mtu_text = match.group(3)
                if "UP" in flags_text.split(","):
                    oper_state = "up"
                else:
                    oper_state = "down"
                if mtu_text is not None and len(mtu_text) > 0:
                    try:
                        mtu = int(mtu_text)
                    except ValueError:
                        mtu = None
                found_first = True
                continue

        ether_match = _IFCONFIG_ETHER.match(raw)
        if ether_match is not None:
            mac_address = ether_match.group(1)
            continue

        inet_match = _IFCONFIG_INET.match(raw)
        if inet_match is not None:
            address = inet_match.group(1)
            mask_hex = inet_match.group(2)
            prefix_len = _hex_netmask_to_prefix(hex_mask=mask_hex)
            cidr = "{}/{}".format(address, prefix_len)
            ipv4_addresses.append(cidr)
            continue

        inet6_match = _IFCONFIG_INET6.match(raw)
        if inet6_match is not None:
            address6 = inet6_match.group(1)
            prefix6_text = inet6_match.group(2)
            try:
                prefix6 = int(prefix6_text)
            except ValueError:
                prefix6 = 0
            cidr6 = "{}/{}".format(address6, prefix6)
            ipv6_addresses.append(cidr6)
            continue

    if not found_first:
        rtnval: Optional[MacosInterfaceInfo] = None
        return rtnval

    rtnval = MacosInterfaceInfo(
        device=device,
        service_name=service_name,
        oper_state=oper_state,
        mac_address=mac_address,
        ipv4_addresses=ipv4_addresses,
        ipv6_addresses=ipv6_addresses,
        mtu=mtu,
    )
    return rtnval


def _hex_netmask_to_prefix(*, hex_mask: str) -> int:
    """
        Convert a hex IPv4 netmask (the ``ifconfig`` ``netmask 0x...``
        form) into a numeric prefix length.

        :param hex_mask: Hex digits without the ``0x`` prefix.

        :returns: Prefix length in [0, 32].
    """
    cleaned = hex_mask.strip()
    if len(cleaned) == 0:
        rtnval = 0
        return rtnval
    try:
        mask_value = int(cleaned, 16)
    except ValueError:
        rtnval = 0
        return rtnval
    bits = 0
    for shift in range(32):
        if (mask_value >> shift) & 1:
            bits += 1
    rtnval = bits
    return rtnval


def _parse_getinfo(*, text: str) -> dict[str, str]:
    """
        Parse ``networksetup -getinfo <service>`` output into a
        ``key -> value`` dict.

        Each non-blank line is split on the first ``": "``.  Lines
        without a colon are skipped.

        :param text: Raw stdout.

        :returns: ``dict[str, str]`` of parsed entries.
    """
    rtnval: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        rtnval[key.strip()] = value.strip()
    return rtnval


def _parse_getdnsservers(*, text: str) -> list[str]:
    """
        Parse ``networksetup -getdnsservers <service>`` output.

        ``networksetup`` emits one IP per line when servers are
        configured, or the literal sentence "There aren't any DNS
        Servers set on <service>." when none are configured.

        :param text: Raw stdout.

        :returns: List of DNS servers in declaration order, or an
                  empty list when the placeholder line is reported.
    """
    rtnval: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith(_DNS_EMPTY_PREFIX):
            empty: list[str] = []
            return empty
        rtnval.append(line)
    return rtnval


def _parse_route_get_gateway(*, text: str) -> Optional[str]:
    """
        Parse ``route -n get <dest>`` output and extract the
        ``gateway`` field.

        :param text: Raw stdout.

        :returns: Gateway IP, or ``None`` when the field is absent or
                  reports a link-style value.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("gateway:"):
            continue
        _, _, value = line.partition(":")
        gateway = value.strip()
        if len(gateway) == 0:
            rtnval: Optional[str] = None
            return rtnval
        if gateway.startswith("link#"):
            rtnval = None
            return rtnval
        rtnval = gateway
        return rtnval

    rtnval = None
    return rtnval


def _parse_netstat_routes(*, text: str) -> list[MacosRouteInfo]:
    """
        Parse ``netstat -rn`` output into :class:`MacosRouteInfo`
        rows.  Provided as a module-level helper for tests; the
        manager prefers ``route -n get`` for idempotency probes.

        ``default`` is normalised to ``0.0.0.0/0`` (or ``::/0`` in the
        IPv6 section).  ``link#N`` and MAC-shaped gateways collapse to
        ``None``.

        :param text: Raw stdout from ``netstat -rn``.

        :returns: List of parsed routes.
    """
    rtnval: list[MacosRouteInfo] = []

    in_table = False
    is_ipv6_section = False
    for raw in text.splitlines():
        stripped = raw.rstrip()
        if len(stripped.strip()) == 0:
            in_table = False
            continue

        # Section headers reset the parser.
        if stripped.startswith("Internet:"):
            in_table = False
            is_ipv6_section = False
            continue
        if stripped.startswith("Internet6:"):
            in_table = False
            is_ipv6_section = True
            continue

        # The header row that introduces table data.
        if stripped.lstrip().startswith("Destination"):
            in_table = True
            continue

        if not in_table:
            continue

        tokens = stripped.split()
        if len(tokens) < 4:
            continue

        destination = tokens[0]
        gateway_token = tokens[1]
        # The Netif column is consistently the fourth column on macOS
        # (``Destination Gateway Flags Netif``); BSD adds ``Refs`` and
        # ``Use`` columns in some builds, so we walk to the last token
        # whose form matches a BSD device name.
        interface_token: Optional[str] = None
        for candidate in tokens[3:]:
            if any(ch.isalpha() for ch in candidate):
                # Most BSD devices are alpha+digit; ``Expire`` columns
                # are pure digits or ``-``.  The first alpha token in
                # the trailing portion is the Netif column on macOS.
                interface_token = candidate
                break

        if destination == "default":
            if is_ipv6_section:
                normalised_dest = "::/0"
            else:
                normalised_dest = "0.0.0.0/0"
        else:
            normalised_dest = destination

        if (
            gateway_token.startswith("link#")
            or _looks_like_mac(token=gateway_token)
        ):
            gateway: Optional[str] = None
        else:
            gateway = gateway_token

        rtnval.append(
            MacosRouteInfo(
                destination=normalised_dest,
                gateway=gateway,
                interface=interface_token,
                metric=None,
            ),
        )

    return rtnval


def _looks_like_mac(*, token: str) -> bool:
    """
        Heuristic for whether ``token`` is a MAC-shaped address (BSD
        ``netstat -rn`` sometimes shows the next-hop MAC in the
        Gateway column for directly connected hosts).

        :param token: Candidate token.

        :returns: True when ``token`` contains at least three colons
                  and otherwise consists of hex digits.
    """
    if token.count(":") < 3:
        rtnval = False
        return rtnval
    for ch in token:
        if ch == ":":
            continue
        if ch in "0123456789abcdefABCDEF":
            continue
        rtnval = False
        return rtnval
    rtnval = True
    return rtnval
