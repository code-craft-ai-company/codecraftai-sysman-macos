"""
    Tests for :class:`MacosNetworkConfigurationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``networksetup`` / ``ifconfig`` / ``route`` /
    ``scutil`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.networkconfiguration import (
    MacosInterfaceInfo,
    MacosNetworkActionStatus,
    MacosNetworkConfigurationManager,
    MacosNetworkConfigurationManagerConfig,
    MacosNetworkQueryError,
    MacosRouteInfo,
    _hex_netmask_to_prefix,
    _parse_getdnsservers,
    _parse_getinfo,
    _parse_hardware_ports,
    _parse_ifconfig,
    _parse_netstat_routes,
    _parse_route_get_gateway,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


_HARDWARE_PORTS_OUTPUT = (
    "Hardware Port: Wi-Fi\n"
    "Device: en0\n"
    "Ethernet Address: aa:bb:cc:dd:ee:ff\n"
    "\n"
    "Hardware Port: Ethernet\n"
    "Device: en1\n"
    "Ethernet Address: 11:22:33:44:55:66\n"
    "\n"
    "VLAN Configurations\n"
    "===================\n"
)


def _seed_hardware_ports(*, runner: _MockCommandRunner) -> None:
    """
        Seed the standard ``networksetup -listallhardwareports``
        output: en0 -> Wi-Fi, en1 -> Ethernet.
    """
    runner.results[("networksetup", "-listallhardwareports")] = RunResult(
        exit_code=0, stdout=_HARDWARE_PORTS_OUTPUT, stderr="",
    )
    return


# ----------------------------------------------------------------------
# Parser tests
# ----------------------------------------------------------------------


def test_parse_hardware_ports_extracts_device_to_service_map():
    """
        ``_parse_hardware_ports`` parses the canonical
        ``-listallhardwareports`` output into a device -> service-name
        dict.
    """
    parsed = _parse_hardware_ports(text=_HARDWARE_PORTS_OUTPUT)
    testbase.assert_equal(parsed.get("en0"), "Wi-Fi")
    testbase.assert_equal(parsed.get("en1"), "Ethernet")
    return


def test_parse_ifconfig_converts_hex_netmask_to_prefix():
    """
        ``_parse_ifconfig`` converts BSD hex netmasks into prefix
        lengths and joins ``inet`` rows into CIDR strings.
    """
    text = (
        "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
        "\tether aa:bb:cc:dd:ee:ff\n"
        "\tinet 10.0.1.50 netmask 0xffffff00 broadcast 10.0.1.255\n"
        "\tinet6 fe80::1%en0 prefixlen 64 scopeid 0x4\n"
    )
    info = _parse_ifconfig(text=text, device="en0", service_name="Wi-Fi")
    testbase.assert_true(info is not None)
    testbase.assert_equal(info.device, "en0")
    testbase.assert_equal(info.service_name, "Wi-Fi")
    testbase.assert_equal(info.oper_state, "up")
    testbase.assert_equal(info.mac_address, "aa:bb:cc:dd:ee:ff")
    testbase.assert_equal(info.mtu, 1500)
    testbase.assert_equal(info.ipv4_addresses, ["10.0.1.50/24"])
    testbase.assert_equal(info.ipv6_addresses, ["fe80::1/64"])
    return


def test_hex_netmask_to_prefix_handles_common_cases():
    """
        ``_hex_netmask_to_prefix`` returns the correct numeric prefix
        for a representative set of netmasks.
    """
    testbase.assert_equal(
        _hex_netmask_to_prefix(hex_mask="ffffff00"), 24,
    )
    testbase.assert_equal(
        _hex_netmask_to_prefix(hex_mask="ffff0000"), 16,
    )
    testbase.assert_equal(
        _hex_netmask_to_prefix(hex_mask="00000000"), 0,
    )
    return


def test_parse_getinfo_extracts_canonical_keys():
    """
        ``_parse_getinfo`` extracts ``IP address``, ``Subnet mask``,
        and ``Router`` from ``networksetup -getinfo`` output.
    """
    text = (
        "Manual Configuration\n"
        "IP address: 10.0.1.50\n"
        "Subnet mask: 255.255.255.0\n"
        "Router: 10.0.1.1\n"
        "IPv6: Automatic\n"
    )
    parsed = _parse_getinfo(text=text)
    testbase.assert_equal(parsed.get("IP address"), "10.0.1.50")
    testbase.assert_equal(parsed.get("Subnet mask"), "255.255.255.0")
    testbase.assert_equal(parsed.get("Router"), "10.0.1.1")
    return


def test_parse_getdnsservers_returns_empty_for_placeholder_line():
    """
        ``_parse_getdnsservers`` recognises the literal placeholder
        sentence as the empty list.
    """
    text = "There aren't any DNS Servers set on Wi-Fi.\n"
    parsed = _parse_getdnsservers(text=text)
    testbase.assert_equal(parsed, [])
    return


def test_parse_getdnsservers_returns_servers_in_order():
    """
        ``_parse_getdnsservers`` preserves declaration order when
        servers are present.
    """
    text = "1.1.1.1\n8.8.8.8\n"
    parsed = _parse_getdnsservers(text=text)
    testbase.assert_equal(parsed, ["1.1.1.1", "8.8.8.8"])
    return


def test_parse_route_get_gateway_extracts_field():
    """
        ``_parse_route_get_gateway`` extracts the ``gateway:`` line
        from ``route -n get`` output.
    """
    text = (
        "   route to: 10.0.1.0\n"
        "destination: 10.0.1.0\n"
        "       mask: 255.255.255.0\n"
        "    gateway: 10.0.1.1\n"
        "  interface: en0\n"
    )
    gateway = _parse_route_get_gateway(text=text)
    testbase.assert_equal(gateway, "10.0.1.1")
    return


def test_parse_netstat_routes_normalises_default_and_link_gateways():
    """
        ``_parse_netstat_routes`` normalises ``default`` to
        ``0.0.0.0/0`` and collapses ``link#N`` gateways to ``None``.
    """
    text = (
        "Routing tables\n"
        "\n"
        "Internet:\n"
        "Destination        Gateway            Flags        Netif\n"
        "default            10.0.1.1           UGScg        en0\n"
        "10.0.1/24          link#4             UCS          en0\n"
        "\n"
        "Internet6:\n"
        "Destination                             Gateway                         Flags        Netif\n"
        "default                                 fe80::1%en0                     UGc          en0\n"
    )
    routes = _parse_netstat_routes(text=text)
    by_dest = {r.destination: r for r in routes}
    testbase.assert_true("0.0.0.0/0" in by_dest)
    testbase.assert_equal(by_dest["0.0.0.0/0"].gateway, "10.0.1.1")
    testbase.assert_equal(by_dest["0.0.0.0/0"].interface, "en0")
    testbase.assert_true("10.0.1/24" in by_dest)
    testbase.assert_equal(by_dest["10.0.1/24"].gateway, None)
    testbase.assert_true("::/0" in by_dest)
    return


# ----------------------------------------------------------------------
# get_interfaces
# ----------------------------------------------------------------------


def test_get_interfaces_builds_map_from_listallhardwareports():
    """
        ``get_interfaces`` calls ``-listallhardwareports`` and
        ``ifconfig`` per device, then assembles
        :class:`MacosInterfaceInfo` rows including the resolved
        service name.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[("ifconfig", "-l")] = RunResult(
        exit_code=0, stdout="en0 en1\n", stderr="",
    )
    runner.results[("ifconfig", "en0")] = RunResult(
        exit_code=0,
        stdout=(
            "en0: flags=8863<UP,BROADCAST,SMART,RUNNING> mtu 1500\n"
            "\tether aa:bb:cc:dd:ee:ff\n"
            "\tinet 10.0.1.50 netmask 0xffffff00 broadcast 10.0.1.255\n"
        ),
        stderr="",
    )
    runner.results[("ifconfig", "en1")] = RunResult(
        exit_code=0,
        stdout=(
            "en1: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500\n"
            "\tether 11:22:33:44:55:66\n"
        ),
        stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    interfaces = manager.get_interfaces()
    testbase.assert_equal(len(interfaces), 2)

    by_device = {info.device: info for info in interfaces}
    testbase.assert_equal(by_device["en0"].service_name, "Wi-Fi")
    testbase.assert_equal(by_device["en0"].oper_state, "up")
    testbase.assert_equal(by_device["en0"].ipv4_addresses, ["10.0.1.50/24"])
    testbase.assert_equal(by_device["en1"].service_name, "Ethernet")
    testbase.assert_equal(by_device["en1"].oper_state, "down")
    return


# ----------------------------------------------------------------------
# configure_interface
# ----------------------------------------------------------------------


def test_configure_interface_resolves_bsd_to_service_name():
    """
        ``configure_interface`` resolves the BSD device name to its
        service name and dispatches the canonical
        ``-setmanual`` argv.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[("networksetup", "-getinfo", "Wi-Fi")] = RunResult(
        exit_code=0,
        stdout=(
            "DHCP Configuration\nIP address: 0.0.0.0\nSubnet mask: 0.0.0.0\nRouter: 0.0.0.0\n"
        ),
        stderr="",
    )
    runner.results[(
        "networksetup", "-setmanual", "Wi-Fi",
        "10.0.1.50", "255.255.255.0", "10.0.1.1",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.configure_interface(
        name="en0",
        address="10.0.1.50",
        netmask="255.255.255.0",
        gateway="10.0.1.1",
    )
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SUCCESS)
    testbase.assert_equal(result.service_name, "Wi-Fi")
    expected = [
        "networksetup", "-setmanual", "Wi-Fi",
        "10.0.1.50", "255.255.255.0", "10.0.1.1",
    ]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_configure_interface_empty_string_for_null_gateway():
    """
        ``configure_interface`` passes an empty string to
        ``networksetup -setmanual`` when ``gateway`` is None.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[("networksetup", "-getinfo", "Wi-Fi")] = RunResult(
        exit_code=0,
        stdout=(
            "DHCP Configuration\nIP address: 0.0.0.0\nSubnet mask: 0.0.0.0\nRouter: 0.0.0.0\n"
        ),
        stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.configure_interface(
        name="en0",
        address="10.0.1.50",
        netmask="255.255.255.0",
        gateway=None,
    )
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SUCCESS)
    expected = [
        "networksetup", "-setmanual", "Wi-Fi",
        "10.0.1.50", "255.255.255.0", "",
    ]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_configure_interface_skipped_when_getinfo_matches():
    """
        ``configure_interface`` returns SKIPPED when
        ``networksetup -getinfo`` already reports the desired
        triple.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[("networksetup", "-getinfo", "Wi-Fi")] = RunResult(
        exit_code=0,
        stdout=(
            "Manual Configuration\n"
            "IP address: 10.0.1.50\n"
            "Subnet mask: 255.255.255.0\n"
            "Router: 10.0.1.1\n"
        ),
        stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.configure_interface(
        name="en0",
        address="10.0.1.50",
        netmask="255.255.255.0",
        gateway="10.0.1.1",
    )
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SKIPPED)
    setmanual_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["networksetup", "-setmanual"]
    ]
    testbase.assert_equal(setmanual_calls, [])
    return


def test_configure_interface_dry_run_does_not_invoke_runner():
    """
        Dry-run short-circuits ``configure_interface`` without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(dry_run=True),
    )
    result = manager.configure_interface(
        name="en0",
        address="10.0.1.50",
        netmask="255.255.255.0",
        gateway=None,
    )
    testbase.assert_equal(result.status, MacosNetworkActionStatus.DRY_RUN)
    setmanual_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["networksetup", "-setmanual"]
    ]
    testbase.assert_equal(setmanual_calls, [])
    return


# ----------------------------------------------------------------------
# enable_interface / disable_interface
# ----------------------------------------------------------------------


def test_enable_interface_skipped_when_already_enabled():
    """
        ``enable_interface`` returns SKIPPED when
        ``-getnetworkserviceenabled`` already reports ``Enabled``.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[(
        "networksetup", "-getnetworkserviceenabled", "Wi-Fi",
    )] = RunResult(exit_code=0, stdout="Enabled\n", stderr="")

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.enable_interface(name="en0")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SKIPPED)
    enable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["networksetup", "-setnetworkserviceenabled"]
    ]
    testbase.assert_equal(enable_calls, [])
    return


def test_disable_interface_invokes_setnetworkserviceenabled_off():
    """
        ``disable_interface`` dispatches the canonical
        ``-setnetworkserviceenabled <service> off`` argv when the
        service is currently enabled.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[(
        "networksetup", "-getnetworkserviceenabled", "Wi-Fi",
    )] = RunResult(exit_code=0, stdout="Enabled\n", stderr="")

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.disable_interface(name="en0")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SUCCESS)
    expected = [
        "networksetup", "-setnetworkserviceenabled", "Wi-Fi", "off",
    ]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


def test_add_route_dispatches_bsd_route_add():
    """
        ``add_route`` dispatches ``route -n add <dest> <gateway>``,
        not the linux ``ip route ... via ...`` form.
    """
    runner = _MockCommandRunner()
    runner.results[("route", "-n", "get", "192.168.1.0/24")] = RunResult(
        exit_code=1, stdout="", stderr="route: writing to routing socket: not in table\n",
    )
    runner.results[(
        "route", "-n", "add", "192.168.1.0/24", "10.0.1.1",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.add_route(dest="192.168.1.0/24", gateway="10.0.1.1")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SUCCESS)
    expected = ["route", "-n", "add", "192.168.1.0/24", "10.0.1.1"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    testbase.assert_true(
        "runtime-only" in (result.message or ""),
    )
    return


def test_add_route_skipped_when_present():
    """
        ``add_route`` returns SKIPPED when ``route -n get`` already
        reports a matching gateway.
    """
    runner = _MockCommandRunner()
    runner.results[("route", "-n", "get", "192.168.1.0/24")] = RunResult(
        exit_code=0,
        stdout=(
            "   route to: 192.168.1.0\n"
            "    gateway: 10.0.1.1\n"
            "  interface: en0\n"
        ),
        stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.add_route(dest="192.168.1.0/24", gateway="10.0.1.1")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SKIPPED)
    add_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["route", "-n", "add"]
    ]
    testbase.assert_equal(add_calls, [])
    return


def test_remove_route_skipped_when_absent():
    """
        ``remove_route`` returns SKIPPED when ``route -n get``
        reports no matching route.
    """
    runner = _MockCommandRunner()
    runner.results[("route", "-n", "get", "192.168.1.0/24")] = RunResult(
        exit_code=1, stdout="", stderr="not in table\n",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.remove_route(dest="192.168.1.0/24")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SKIPPED)
    delete_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["route", "-n", "delete"]
    ]
    testbase.assert_equal(delete_calls, [])
    return


# ----------------------------------------------------------------------
# DNS
# ----------------------------------------------------------------------


def test_set_dns_servers_uses_empty_literal_for_empty_list():
    """
        ``set_dns_servers`` passes the literal ``empty`` to
        ``networksetup`` when ``servers`` is the empty list.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[("networksetup", "-getdnsservers", "Wi-Fi")] = RunResult(
        exit_code=0, stdout="1.1.1.1\n", stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.set_dns_servers(interface="en0", servers=[])
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SUCCESS)
    expected = ["networksetup", "-setdnsservers", "Wi-Fi", "empty"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_set_dns_servers_skipped_when_current_matches():
    """
        ``set_dns_servers`` returns SKIPPED when the current sorted
        list matches the desired sorted list.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[("networksetup", "-getdnsservers", "Wi-Fi")] = RunResult(
        exit_code=0, stdout="1.1.1.1\n8.8.8.8\n", stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.set_dns_servers(
        interface="en0", servers=["8.8.8.8", "1.1.1.1"],
    )
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SKIPPED)
    setdns_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["networksetup", "-setdnsservers"]
    ]
    testbase.assert_equal(setdns_calls, [])
    return


def test_get_dns_servers_returns_empty_for_placeholder():
    """
        ``get_dns_servers`` returns an empty list when ``networksetup``
        reports the placeholder line.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[("networksetup", "-getdnsservers", "Wi-Fi")] = RunResult(
        exit_code=0,
        stdout="There aren't any DNS Servers set on Wi-Fi.\n",
        stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    servers = manager.get_dns_servers(interface="en0")
    testbase.assert_equal(servers, [])
    return


# ----------------------------------------------------------------------
# Hostname triple
# ----------------------------------------------------------------------


def test_set_hostname_dispatches_three_scutil_calls():
    """
        ``set_hostname`` dispatches three ``scutil --set`` calls in
        order: ``HostName`` -> ``ComputerName`` -> ``LocalHostName``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("scutil", "--get", "HostName")] = RunResult(
        exit_code=0, stdout="oldhost\n", stderr="",
    )
    runner.results[("scutil", "--get", "ComputerName")] = RunResult(
        exit_code=0, stdout="oldhost\n", stderr="",
    )
    runner.results[("scutil", "--get", "LocalHostName")] = RunResult(
        exit_code=0, stdout="oldhost\n", stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.set_hostname(name="newhost")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SUCCESS)

    scutil_set_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["scutil", "--set"]
    ]
    testbase.assert_equal(len(scutil_set_calls), 3)
    testbase.assert_equal(
        scutil_set_calls[0]["args"],
        ["scutil", "--set", "HostName", "newhost"],
    )
    testbase.assert_equal(
        scutil_set_calls[1]["args"],
        ["scutil", "--set", "ComputerName", "newhost"],
    )
    testbase.assert_equal(
        scutil_set_calls[2]["args"],
        ["scutil", "--set", "LocalHostName", "newhost"],
    )
    return


def test_set_hostname_local_name_normalises_dots_to_dashes():
    """
        ``set_hostname`` writes the dot-normalised name to
        ``LocalHostName`` because Bonjour names cannot contain dots.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("scutil", "--get", "HostName")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[("scutil", "--get", "ComputerName")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[("scutil", "--get", "LocalHostName")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.set_hostname(name="my.host")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SUCCESS)

    local_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["scutil", "--set", "LocalHostName"]
    ]
    testbase.assert_equal(len(local_calls), 1)
    testbase.assert_equal(
        local_calls[0]["args"],
        ["scutil", "--set", "LocalHostName", "my-host"],
    )
    return


def test_set_hostname_skipped_when_all_three_match():
    """
        ``set_hostname`` returns SKIPPED when all three ``scutil
        --get`` reads already match (with the dot-to-dash
        normalisation applied to ``LocalHostName``).
    """
    runner = _MockCommandRunner()
    runner.results[("scutil", "--get", "HostName")] = RunResult(
        exit_code=0, stdout="my.host\n", stderr="",
    )
    runner.results[("scutil", "--get", "ComputerName")] = RunResult(
        exit_code=0, stdout="my.host\n", stderr="",
    )
    runner.results[("scutil", "--get", "LocalHostName")] = RunResult(
        exit_code=0, stdout="my-host\n", stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.set_hostname(name="my.host")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.SKIPPED)
    set_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["scutil", "--set"]
    ]
    testbase.assert_equal(set_calls, [])
    return


def test_set_hostname_failed_identifies_failing_surface():
    """
        ``set_hostname`` short-circuits at the first failing surface
        and surfaces the failing field name in ``message``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("scutil", "--get", "HostName")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[("scutil", "--get", "ComputerName")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[("scutil", "--get", "LocalHostName")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[(
        "scutil", "--set", "ComputerName", "newhost",
    )] = RunResult(
        exit_code=1, stdout="", stderr="permission denied\n",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.set_hostname(name="newhost")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.FAILED)
    testbase.assert_equal(result.message, "scutil_set_computername_failed")
    # No ``LocalHostName`` write should be attempted after the failure.
    local_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["scutil", "--set", "LocalHostName"]
    ]
    testbase.assert_equal(local_calls, [])
    return


def test_get_hostname_returns_stripped_stdout():
    """
        ``get_hostname`` reads ``scutil --get HostName`` and returns
        the stripped stdout.
    """
    runner = _MockCommandRunner()
    runner.results[("scutil", "--get", "HostName")] = RunResult(
        exit_code=0, stdout="myhost\n", stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    name = manager.get_hostname()
    testbase.assert_equal(name, "myhost")
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_pass_elevated_when_configured():
    """
        Write verbs pass ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_hardware_ports(runner=runner)
    runner.results[(
        "networksetup", "-getnetworkserviceenabled", "Wi-Fi",
    )] = RunResult(exit_code=0, stdout="Disabled\n", stderr="")

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(elevated=True),
    )
    manager.enable_interface(name="en0")
    enable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["networksetup", "-setnetworkserviceenabled"]
    ]
    testbase.assert_equal(len(enable_calls), 1)
    testbase.assert_equal(enable_calls[0]["elevated"], True)
    return


def test_reads_never_elevate_even_with_elevated_config():
    """
        Reads never pass ``elevated=True`` even when the config is
        configured to elevate writes.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_hardware_ports(runner=runner)
    runner.results[("networksetup", "-getdnsservers", "Wi-Fi")] = RunResult(
        exit_code=0, stdout="1.1.1.1\n", stderr="",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(elevated=True),
    )
    manager.get_dns_servers(interface="en0")

    elevated_reads = [
        c for c in runner.calls
        if c["elevated"] and c["args"][:2] == ["networksetup", "-getdnsservers"]
    ]
    testbase.assert_equal(elevated_reads, [])
    elevated_listing = [
        c for c in runner.calls
        if c["elevated"] and c["args"] == [
            "networksetup", "-listallhardwareports",
        ]
    ]
    testbase.assert_equal(elevated_listing, [])
    return


# ----------------------------------------------------------------------
# Failure surface
# ----------------------------------------------------------------------


def test_failed_command_returns_failed_result_with_stderr():
    """
        A non-zero exit from ``-setmanual`` maps to FAILED with the
        captured exit_code / stdout / stderr / command preserved.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[("networksetup", "-getinfo", "Wi-Fi")] = RunResult(
        exit_code=0,
        stdout=(
            "Manual Configuration\nIP address: 0.0.0.0\nSubnet mask: 0.0.0.0\nRouter: 0.0.0.0\n"
        ),
        stderr="",
    )
    runner.results[(
        "networksetup", "-setmanual", "Wi-Fi",
        "10.0.1.50", "255.255.255.0", "10.0.1.1",
    )] = RunResult(
        exit_code=1, stdout="", stderr="Operation not permitted\n",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.configure_interface(
        name="en0",
        address="10.0.1.50",
        netmask="255.255.255.0",
        gateway="10.0.1.1",
    )
    testbase.assert_equal(result.status, MacosNetworkActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "Operation not permitted\n")
    expected = [
        "networksetup", "-setmanual", "Wi-Fi",
        "10.0.1.50", "255.255.255.0", "10.0.1.1",
    ]
    testbase.assert_equal(result.command, expected)
    return


def test_resolve_service_name_failure_surfaces_as_failed_result():
    """
        A device with no service mapping surfaces as FAILED on the
        public verbs (rather than raising) so callers can keep their
        outer try/except simple.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    result = manager.enable_interface(name="utun0")
    testbase.assert_equal(result.status, MacosNetworkActionStatus.FAILED)
    testbase.assert_equal(result.message, "service_name_resolution_failed")
    return


def test_get_dns_servers_raises_query_error_on_unexpected_failure():
    """
        ``get_dns_servers`` raises :class:`MacosNetworkQueryError`
        when ``networksetup`` exits non-zero.
    """
    runner = _MockCommandRunner()
    _seed_hardware_ports(runner=runner)
    runner.results[("networksetup", "-getdnsservers", "Wi-Fi")] = RunResult(
        exit_code=1, stdout="", stderr="boom\n",
    )

    manager = MacosNetworkConfigurationManager(
        runner=runner,
        config=MacosNetworkConfigurationManagerConfig(),
    )
    raised = False
    try:
        manager.get_dns_servers(interface="en0")
    except MacosNetworkQueryError:
        raised = True
    testbase.assert_true(raised)
    return
