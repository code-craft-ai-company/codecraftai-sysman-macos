"""
    Tests for :class:`MacosVPNClientManager`.

    Every test uses a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``brew``, ``curl``, ``installer``,
    ``launchctl``, ``tailscale``, or ``twingate`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from hypothesis import given, settings, strategies as st

from automojo import testbase

from automojo.aspects.aspects import Aspects

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.common.vpnprovider import VPNProvider

from automojo.sysman.macos.vpnclientmanagement import (
    InstallSource,
    MacosVPNActionStatus,
    MacosVPNClientManager,
    MacosVPNClientManagerConfig,
    MacosVPNClientQueryError,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call
        and returns pre-seeded :class:`RunResult` values keyed by an
        argv-prefix tuple.

        Seeding works as a longest-prefix match against the tuple of
        argv tokens; this keeps test setup compact while still letting
        tests pin specific commands (``brew list --versions tailscale``)
        and fall through to a default ``RunResult`` for everything
        else.
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

        argv_tuple = tuple(args)
        if argv_tuple in self.results:
            rtnval = self.results[argv_tuple]
            return rtnval

        # Longest-prefix fallback.
        candidate_keys = [
            key for key in self.results.keys()
            if isinstance(key, tuple) and len(key) <= len(argv_tuple)
            and argv_tuple[: len(key)] == key
        ]
        if len(candidate_keys) > 0:
            candidate_keys.sort(key=len, reverse=True)
            rtnval = self.results[candidate_keys[0]]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _aspects() -> Aspects:
    """
        Build an empty :class:`Aspects` instance for use in tests.

        :returns: A default-constructed :class:`Aspects`.
    """
    rtnval = Aspects()
    return rtnval


def _seed_tailscale_absent(*, runner: _MockCommandRunner) -> None:
    """
        Seed ``brew list --versions tailscale`` to indicate absence.
    """
    runner.results[("brew", "list", "--versions", "tailscale")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[("pkgutil", "--pkg-info", "io.tailscale.tailscaled")] = (
        RunResult(exit_code=1, stdout="", stderr="No receipt")
    )
    return


def _seed_tailscale_present_brew(
    *,
    runner: _MockCommandRunner,
    version: str,
) -> None:
    """
        Seed ``brew list --versions tailscale`` to show ``version``.
    """
    runner.results[("brew", "list", "--versions", "tailscale")] = RunResult(
        exit_code=0, stdout="tailscale {}\n".format(version), stderr="",
    )
    return


def _seed_tailscale_present_pkg(
    *,
    runner: _MockCommandRunner,
    version: str,
) -> None:
    """
        Seed ``pkgutil --pkg-info`` to show Tailscale installed.
    """
    runner.results[("pkgutil", "--pkg-info", "io.tailscale.tailscaled")] = (
        RunResult(
            exit_code=0,
            stdout="package-id: io.tailscale.tailscaled\nversion: {}\n".format(version),
            stderr="",
        )
    )
    return


def _seed_twingate_absent(*, runner: _MockCommandRunner) -> None:
    """
        Seed ``pkgutil --pkg-info`` to report Twingate absent.
    """
    runner.results[("pkgutil", "--pkg-info", "com.twingate.connector")] = (
        RunResult(exit_code=1, stdout="", stderr="No receipt")
    )
    return


def _seed_twingate_present(
    *,
    runner: _MockCommandRunner,
    version: str,
) -> None:
    """
        Seed ``pkgutil --pkg-info`` to show Twingate installed.
    """
    runner.results[("pkgutil", "--pkg-info", "com.twingate.connector")] = (
        RunResult(
            exit_code=0,
            stdout="package-id: com.twingate.connector\nversion: {}\n".format(version),
            stderr="",
        )
    )
    return


def _seed_tailscale_status_running(
    *,
    runner: _MockCommandRunner,
    hostname: str = "test-host",
) -> None:
    """
        Seed ``tailscale status --json`` for a running tailnet.
    """
    payload = {
        "BackendState": "Running",
        "Version": "1.60.0",
        "MagicDNSSuffix": "tailnet-test.ts.net",
        "Self": {
            "HostName": hostname,
            "TailscaleIPs": ["100.64.0.1"],
            "DNSName": "{}.tailnet-test.ts.net.".format(hostname),
        },
        "Peer": {
            "node-A": {
                "HostName": "peer-a",
                "TailscaleIPs": ["100.64.0.2"],
            },
        },
    }
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=json.dumps(payload), stderr="",
    )
    return


def _seed_tailscale_status_stopped(*, runner: _MockCommandRunner) -> None:
    """
        Seed ``tailscale status --json`` for a stopped daemon.
    """
    payload = {"BackendState": "Stopped"}
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=json.dumps(payload), stderr="",
    )
    return


def _seed_tailscale_status_missing(*, runner: _MockCommandRunner) -> None:
    """
        Seed ``tailscale status --json`` as if the CLI is missing.
    """
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=127, stdout="", stderr="command not found: tailscale",
    )
    return


def _seed_twingate_status_offline(*, runner: _MockCommandRunner) -> None:
    """
        Seed ``twingate status`` with an offline payload.
    """
    runner.results[("twingate", "status")] = RunResult(
        exit_code=0, stdout="connector is offline\n", stderr="",
    )
    return


# ----------------------------------------------------------------------
# install_tailscale
# ----------------------------------------------------------------------


def test_install_tailscale_brew_runs_brew_install():
    """
        Homebrew install path invokes ``brew install tailscale``
        unelevated.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_absent(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    config = MacosVPNClientManagerConfig(
        install_source=InstallSource.HOMEBREW, elevated=True,
    )
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.install_tailscale(
        version=None, channel="stable", aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)
    brew_install_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "install"]
    ]
    testbase.assert_equal(len(brew_install_calls), 1)
    testbase.assert_equal(
        brew_install_calls[0]["args"], ["brew", "install", "tailscale"],
    )
    testbase.assert_equal(brew_install_calls[0]["elevated"], False)
    return


def test_install_tailscale_pkg_runs_curl_then_installer():
    """
        PKG install path runs ``mkdir -p`` + ``curl`` + ``installer``.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_absent(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    config = MacosVPNClientManagerConfig(
        install_source=InstallSource.PKG, elevated=True,
    )
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.install_tailscale(
        version="1.60.0", channel="stable", aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)

    curl_calls = [c for c in runner.calls if c["args"][:1] == ["curl"]]
    testbase.assert_equal(len(curl_calls), 1)
    testbase.assert_true(
        "Tailscale-1.60.0.pkg" in " ".join(curl_calls[0]["args"]),
    )

    installer_calls = [
        c for c in runner.calls if c["args"][:1] == ["installer"]
    ]
    testbase.assert_equal(len(installer_calls), 1)
    testbase.assert_equal(installer_calls[0]["args"][-2:], ["-target", "/"])
    return


def test_install_tailscale_skipped_when_present_brew():
    """
        Homebrew install path returns SKIPPED when the formula is
        already installed at the requested version.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_present_brew(runner=runner, version="1.60.0")

    config = MacosVPNClientManagerConfig(
        install_source=InstallSource.HOMEBREW,
    )
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.install_tailscale(
        version=None, channel="stable", aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.SKIPPED)
    brew_install_calls = [
        c for c in runner.calls if c["args"][:2] == ["brew", "install"]
    ]
    testbase.assert_equal(brew_install_calls, [])
    return


def test_install_tailscale_skipped_when_present_pkg():
    """
        PKG install path returns SKIPPED when ``pkgutil`` reports the
        package already installed.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_present_pkg(runner=runner, version="1.60.0")

    config = MacosVPNClientManagerConfig(install_source=InstallSource.PKG)
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.install_tailscale(
        version="1.60.0", channel="stable", aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.SKIPPED)
    installer_calls = [
        c for c in runner.calls if c["args"][:1] == ["installer"]
    ]
    testbase.assert_equal(installer_calls, [])
    return


def test_install_tailscale_failed_on_installer_error():
    """
        Non-zero exit from ``installer`` maps to FAILED.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_absent(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    runner.results[
        ("installer", "-pkg",
         "/var/tmp/automojo/vpn-pkgs/Tailscale.pkg",
         "-target", "/")
    ] = RunResult(exit_code=1, stdout="", stderr="installer failed")

    config = MacosVPNClientManagerConfig(install_source=InstallSource.PKG)
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.install_tailscale(
        version=None, channel="stable", aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "installer failed")
    return


# ----------------------------------------------------------------------
# install_twingate_connector
# ----------------------------------------------------------------------


def test_install_twingate_connector_runs_curl_then_installer():
    """
        Twingate install path runs ``curl`` + ``installer`` with
        elevation when configured.
    """
    runner = _MockCommandRunner()
    _seed_twingate_absent(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    config = MacosVPNClientManagerConfig(elevated=True)
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.install_twingate_connector(
        version="1.0.0", aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)

    curl_calls = [c for c in runner.calls if c["args"][:1] == ["curl"]]
    testbase.assert_equal(len(curl_calls), 1)

    installer_calls = [
        c for c in runner.calls if c["args"][:1] == ["installer"]
    ]
    testbase.assert_equal(len(installer_calls), 1)
    testbase.assert_equal(installer_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# configure
# ----------------------------------------------------------------------


def test_configure_tailscale_returns_skipped():
    """
        ``configure(provider=TAILSCALE, ...)`` always returns SKIPPED.
    """
    runner = _MockCommandRunner()
    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(),
    )
    result = manager.configure(
        provider=VPNProvider.TAILSCALE,
        settings={},
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SKIPPED)
    testbase.assert_equal(runner.calls, [])
    return


def test_configure_twingate_writes_conf_via_tee():
    """
        ``configure(provider=TWINGATE, ...)`` renders the connector
        config and pipes it through ``tee``.
    """
    runner = _MockCommandRunner()
    # ``cat`` of the config file returns missing-file.
    runner.results[("cat", "/Library/Application Support/Twingate/connector.conf")] = (
        RunResult(exit_code=1, stdout="", stderr="No such file")
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    config = MacosVPNClientManagerConfig(elevated=True)
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.configure(
        provider=VPNProvider.TWINGATE,
        settings={"network": "example", "token": "abc123"},
        aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][:1] == ["tee"]]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_true("network=example" in tee_calls[0]["stdin"])
    testbase.assert_true("token=abc123" in tee_calls[0]["stdin"])
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_configure_twingate_skipped_when_content_matches():
    """
        ``configure`` returns SKIPPED when the existing on-disk
        content already matches the rendered body.
    """
    existing = "network=example\ntoken=abc123\n"
    runner = _MockCommandRunner()
    runner.results[("cat", "/Library/Application Support/Twingate/connector.conf")] = (
        RunResult(exit_code=0, stdout=existing, stderr="")
    )
    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(),
    )
    result = manager.configure(
        provider=VPNProvider.TWINGATE,
        settings={"network": "example", "token": "abc123"},
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][:1] == ["tee"]]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# bring_up
# ----------------------------------------------------------------------


def test_bring_up_tailscale_loads_plist_then_runs_up():
    """
        Tailscale bring-up runs ``launchctl bootstrap`` first, then
        ``tailscale up`` with the supplied parameters.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_status_stopped(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    config = MacosVPNClientManagerConfig(elevated=True)
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey-abcd",
        hostname="test-host",
        advertise_tags=["tag:test"],
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)

    bootstrap_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["launchctl", "bootstrap", "system"]
    ]
    testbase.assert_equal(len(bootstrap_calls), 1)
    testbase.assert_equal(bootstrap_calls[0]["elevated"], True)

    up_calls = [c for c in runner.calls if c["args"][:2] == ["tailscale", "up"]]
    testbase.assert_equal(len(up_calls), 1)
    testbase.assert_true("--authkey=tskey-abcd" in up_calls[0]["args"])
    testbase.assert_true("--hostname=test-host" in up_calls[0]["args"])
    testbase.assert_true("--advertise-tags=tag:test" in up_calls[0]["args"])
    testbase.assert_equal(up_calls[0]["elevated"], True)
    return


def test_bring_up_tailscale_skipped_when_running_with_matching_state():
    """
        Tailscale bring-up returns SKIPPED when status already
        reports running with the requested hostname.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_status_running(runner=runner, hostname="test-host")

    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(),
    )
    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey-abcd",
        hostname="test-host",
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.SKIPPED)
    up_calls = [c for c in runner.calls if c["args"][:2] == ["tailscale", "up"]]
    testbase.assert_equal(up_calls, [])
    return


def test_bring_up_twingate_runs_launchctl_bootstrap():
    """
        Twingate bring-up dispatches ``launchctl bootstrap system``
        on the configured plist.
    """
    runner = _MockCommandRunner()
    _seed_twingate_status_offline(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    config = MacosVPNClientManagerConfig(elevated=True)
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.bring_up(
        provider=VPNProvider.TWINGATE,
        authkey="ignored",
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)
    bootstrap_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["launchctl", "bootstrap", "system"]
    ]
    testbase.assert_equal(len(bootstrap_calls), 1)
    testbase.assert_equal(
        bootstrap_calls[0]["args"][3],
        "/Library/LaunchDaemons/com.twingate.connector.plist",
    )
    testbase.assert_equal(bootstrap_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# bring_down
# ----------------------------------------------------------------------


def test_bring_down_tailscale_runs_tailscale_down():
    """
        Tailscale bring-down dispatches ``tailscale down``.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_status_running(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(elevated=True),
    )
    result = manager.bring_down(
        provider=VPNProvider.TAILSCALE, aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)

    down_calls = [
        c for c in runner.calls
        if c["args"] == ["tailscale", "down"]
    ]
    testbase.assert_equal(len(down_calls), 1)
    testbase.assert_equal(down_calls[0]["elevated"], True)
    return


def test_bring_down_tailscale_skipped_when_already_down():
    """
        bring_down returns SKIPPED when status reports not running.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_status_stopped(runner=runner)

    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(),
    )
    result = manager.bring_down(
        provider=VPNProvider.TAILSCALE, aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SKIPPED)
    down_calls = [
        c for c in runner.calls if c["args"] == ["tailscale", "down"]
    ]
    testbase.assert_equal(down_calls, [])
    return


def test_bring_down_twingate_runs_launchctl_bootout():
    """
        Twingate bring-down dispatches ``launchctl bootout system``.
    """
    runner = _MockCommandRunner()
    # Twingate status says online so we don't short-circuit to SKIPPED.
    runner.results[("twingate", "status")] = RunResult(
        exit_code=0, stdout="connector is online\n", stderr="",
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(elevated=True),
    )
    result = manager.bring_down(
        provider=VPNProvider.TWINGATE, aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)
    bootout_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["launchctl", "bootout", "system"]
    ]
    testbase.assert_equal(len(bootout_calls), 1)
    return


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------


def test_status_tailscale_parses_running_json():
    """
        Tailscale status parses the JSON payload into running state,
        self_address, hostname, peers, and version.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_status_running(runner=runner, hostname="host-x")
    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(),
    )
    snapshot = manager.status(
        provider=VPNProvider.TAILSCALE, aspects=_aspects(),
    )
    testbase.assert_equal(snapshot.running, True)
    testbase.assert_equal(snapshot.installed, True)
    testbase.assert_equal(snapshot.self_address, "100.64.0.1")
    testbase.assert_equal(snapshot.hostname, "host-x")
    testbase.assert_equal(snapshot.version, "1.60.0")
    testbase.assert_equal(snapshot.tailnet, "tailnet-test.ts.net")
    testbase.assert_equal(snapshot.peer_addresses.get("peer-a"), "100.64.0.2")

    # Reads never elevate.
    status_calls = [
        c for c in runner.calls
        if c["args"] == ["tailscale", "status", "--json"]
    ]
    testbase.assert_equal(len(status_calls), 1)
    testbase.assert_equal(status_calls[0]["elevated"], False)
    return


def test_status_tailscale_returns_installed_false_when_missing():
    """
        Tailscale status returns ``installed=False`` when the CLI is
        missing.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_status_missing(runner=runner)
    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(),
    )
    snapshot = manager.status(
        provider=VPNProvider.TAILSCALE, aspects=_aspects(),
    )
    testbase.assert_equal(snapshot.installed, False)
    testbase.assert_equal(snapshot.running, False)
    return


def test_status_twingate_parses_text():
    """
        Twingate status detects the ``online`` substring as running.
    """
    runner = _MockCommandRunner()
    runner.results[("twingate", "status")] = RunResult(
        exit_code=0, stdout="connector is online\n", stderr="",
    )
    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(),
    )
    snapshot = manager.status(
        provider=VPNProvider.TWINGATE, aspects=_aspects(),
    )
    testbase.assert_equal(snapshot.running, True)
    testbase.assert_equal(snapshot.installed, True)
    testbase.assert_equal(snapshot.self_address, None)
    testbase.assert_equal(snapshot.peer_addresses, {})
    return


def test_status_raises_query_error_on_unexpected_failure():
    """
        Unexpected non-zero exits from ``tailscale status`` raise
        :class:`MacosVPNClientQueryError`.
    """
    runner = _MockCommandRunner()
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=1, stdout="", stderr="permission denied",
    )
    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(),
    )
    raised = False
    try:
        manager.status(provider=VPNProvider.TAILSCALE, aspects=_aspects())
    except MacosVPNClientQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# uninstall
# ----------------------------------------------------------------------


def test_uninstall_tailscale_brew_runs_brew_uninstall():
    """
        Homebrew uninstall path runs ``brew uninstall tailscale``.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_present_brew(runner=runner, version="1.60.0")
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    config = MacosVPNClientManagerConfig(install_source=InstallSource.HOMEBREW)
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.uninstall(
        provider=VPNProvider.TAILSCALE, purge=False, aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)
    uninstall_calls = [
        c for c in runner.calls
        if c["args"] == ["brew", "uninstall", "tailscale"]
    ]
    testbase.assert_equal(len(uninstall_calls), 1)
    testbase.assert_equal(uninstall_calls[0]["elevated"], False)
    return


def test_uninstall_tailscale_pkg_runs_bundled_uninstaller():
    """
        PKG uninstall path runs the bundled uninstall script.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_present_pkg(runner=runner, version="1.60.0")
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    config = MacosVPNClientManagerConfig(
        install_source=InstallSource.PKG, elevated=True,
    )
    manager = MacosVPNClientManager(runner=runner, config=config)
    result = manager.uninstall(
        provider=VPNProvider.TAILSCALE, purge=False, aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SUCCESS)
    bundled_calls = [
        c for c in runner.calls
        if c["args"] == ["/Library/Application Support/Tailscale/uninstall.sh"]
    ]
    testbase.assert_equal(len(bundled_calls), 1)
    testbase.assert_equal(bundled_calls[0]["elevated"], True)
    return


def test_uninstall_skipped_when_already_absent():
    """
        Uninstall returns SKIPPED when the package is already absent.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_absent(runner=runner)

    manager = MacosVPNClientManager(
        runner=runner,
        config=MacosVPNClientManagerConfig(install_source=InstallSource.HOMEBREW),
    )
    result = manager.uninstall(
        provider=VPNProvider.TAILSCALE, purge=False, aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SKIPPED)
    uninstall_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "uninstall"]
    ]
    testbase.assert_equal(uninstall_calls, [])
    return


# ----------------------------------------------------------------------
# dry-run
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_every_write_verb():
    """
        With ``dry_run=True`` every write verb returns ``DRY_RUN``
        and the runner is never invoked.
    """
    runner = _MockCommandRunner()
    config = MacosVPNClientManagerConfig(dry_run=True)
    manager = MacosVPNClientManager(runner=runner, config=config)

    install_t = manager.install_tailscale(
        version=None, channel="stable", aspects=_aspects(),
    )
    testbase.assert_equal(install_t.status, MacosVPNActionStatus.DRY_RUN)

    install_tg = manager.install_twingate_connector(
        version=None, aspects=_aspects(),
    )
    testbase.assert_equal(install_tg.status, MacosVPNActionStatus.DRY_RUN)

    configure_t = manager.configure(
        provider=VPNProvider.TWINGATE,
        settings={"k": "v"},
        aspects=_aspects(),
    )
    testbase.assert_equal(configure_t.status, MacosVPNActionStatus.DRY_RUN)

    up_t = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="k",
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(up_t.status, MacosVPNActionStatus.DRY_RUN)

    down_t = manager.bring_down(
        provider=VPNProvider.TAILSCALE, aspects=_aspects(),
    )
    testbase.assert_equal(down_t.status, MacosVPNActionStatus.DRY_RUN)

    uninstall_t = manager.uninstall(
        provider=VPNProvider.TAILSCALE,
        purge=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(uninstall_t.status, MacosVPNActionStatus.DRY_RUN)

    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# elevation
# ----------------------------------------------------------------------


def test_writes_pass_elevated_when_configured_excluding_brew():
    """
        Writes pass ``elevated=True`` to the runner when configured,
        except Homebrew which always runs unelevated.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_absent(runner=runner)
    _seed_twingate_absent(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    config = MacosVPNClientManagerConfig(
        install_source=InstallSource.HOMEBREW, elevated=True,
    )
    manager = MacosVPNClientManager(runner=runner, config=config)
    manager.install_tailscale(
        version=None, channel="stable", aspects=_aspects(),
    )

    brew_install_calls = [
        c for c in runner.calls if c["args"][:2] == ["brew", "install"]
    ]
    testbase.assert_equal(len(brew_install_calls), 1)
    testbase.assert_equal(brew_install_calls[0]["elevated"], False)

    # Now Twingate installer must elevate.
    pkg_runner = _MockCommandRunner()
    _seed_twingate_absent(runner=pkg_runner)
    pkg_runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    pkg_manager = MacosVPNClientManager(
        runner=pkg_runner,
        config=MacosVPNClientManagerConfig(elevated=True),
    )
    pkg_manager.install_twingate_connector(
        version=None, aspects=_aspects(),
    )
    installer_calls = [
        c for c in pkg_runner.calls if c["args"][:1] == ["installer"]
    ]
    testbase.assert_equal(len(installer_calls), 1)
    testbase.assert_equal(installer_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Status / probe calls never pass ``elevated=True``.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_status_running(runner=runner)
    manager = MacosVPNClientManager(
        runner=runner,
        config=MacosVPNClientManagerConfig(elevated=True),
    )
    manager.status(provider=VPNProvider.TAILSCALE, aspects=_aspects())

    elevated_reads = [c for c in runner.calls if c["elevated"] is True]
    testbase.assert_equal(elevated_reads, [])
    return


# ----------------------------------------------------------------------
# InstallSource enum behaviour
# ----------------------------------------------------------------------


def test_install_source_switches_install_path():
    """
        Switching :class:`InstallSource` between HOMEBREW and PKG
        changes the command dispatched.
    """
    runner_brew = _MockCommandRunner()
    _seed_tailscale_absent(runner=runner_brew)
    runner_brew.default_result = RunResult(exit_code=0, stdout="", stderr="")

    brew_manager = MacosVPNClientManager(
        runner=runner_brew,
        config=MacosVPNClientManagerConfig(install_source=InstallSource.HOMEBREW),
    )
    brew_manager.install_tailscale(
        version=None, channel="stable", aspects=_aspects(),
    )

    runner_pkg = _MockCommandRunner()
    _seed_tailscale_absent(runner=runner_pkg)
    runner_pkg.default_result = RunResult(exit_code=0, stdout="", stderr="")

    pkg_manager = MacosVPNClientManager(
        runner=runner_pkg,
        config=MacosVPNClientManagerConfig(install_source=InstallSource.PKG),
    )
    pkg_manager.install_tailscale(
        version=None, channel="stable", aspects=_aspects(),
    )

    brew_has_brew_install = any(
        c["args"][:2] == ["brew", "install"] for c in runner_brew.calls
    )
    testbase.assert_true(brew_has_brew_install)

    pkg_has_installer = any(
        c["args"][:1] == ["installer"] for c in runner_pkg.calls
    )
    testbase.assert_true(pkg_has_installer)
    pkg_has_brew_install = any(
        c["args"][:2] == ["brew", "install"] for c in runner_pkg.calls
    )
    testbase.assert_equal(pkg_has_brew_install, False)
    return


# ----------------------------------------------------------------------
# Property tests
# ----------------------------------------------------------------------


_version_strategy = st.one_of(
    st.none(),
    st.from_regex(r"^[0-9]\.[0-9]+\.[0-9]+$", fullmatch=True),
)
_channel_strategy = st.sampled_from(["stable", "unstable"])
_install_source_strategy = st.sampled_from(
    [InstallSource.HOMEBREW, InstallSource.PKG],
)


@settings(max_examples=25, deadline=None)
@given(
    version=_version_strategy,
    channel=_channel_strategy,
    install_source=_install_source_strategy,
)
def test_property_install_tailscale_idempotent(
    version,
    channel,
    install_source,
):
    """
        For any generated input, a second ``install_tailscale`` after
        a successful first invocation must be ``SKIPPED``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    # First call: pretend not installed yet.
    _seed_tailscale_absent(runner=runner)

    config = MacosVPNClientManagerConfig(install_source=install_source)
    manager = MacosVPNClientManager(runner=runner, config=config)
    first = manager.install_tailscale(
        version=version, channel=channel, aspects=_aspects(),
    )
    testbase.assert_true(
        first.status in {
            MacosVPNActionStatus.SUCCESS,
            MacosVPNActionStatus.FAILED,
        },
    )

    # Second call: pretend now installed at the requested version (or
    # any version when none was requested).
    effective_version = version if version is not None else "1.60.0"
    if install_source == InstallSource.HOMEBREW:
        _seed_tailscale_present_brew(runner=runner, version=effective_version)
    else:
        _seed_tailscale_present_pkg(runner=runner, version=effective_version)

    second = manager.install_tailscale(
        version=version, channel=channel, aspects=_aspects(),
    )
    testbase.assert_equal(second.status, MacosVPNActionStatus.SKIPPED)
    return


@settings(max_examples=15, deadline=None)
@given(provider=st.sampled_from([VPNProvider.TAILSCALE, VPNProvider.TWINGATE]))
def test_property_uninstall_idempotent(provider):
    """
        A second ``uninstall`` after the package has been removed
        must be ``SKIPPED``.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_absent(runner=runner)
    _seed_twingate_absent(runner=runner)

    manager = MacosVPNClientManager(
        runner=runner,
        config=MacosVPNClientManagerConfig(install_source=InstallSource.HOMEBREW),
    )
    result = manager.uninstall(
        provider=provider, purge=False, aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SKIPPED)
    return


@settings(max_examples=15, deadline=None)
@given(hostname=st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=10,
))
def test_property_bring_up_idempotent_on_matching_state(hostname):
    """
        When the local status already reports running with the same
        hostname, ``bring_up`` is ``SKIPPED``.
    """
    runner = _MockCommandRunner()
    _seed_tailscale_status_running(runner=runner, hostname=hostname)

    manager = MacosVPNClientManager(
        runner=runner, config=MacosVPNClientManagerConfig(),
    )
    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="k",
        hostname=hostname,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )
    testbase.assert_equal(result.status, MacosVPNActionStatus.SKIPPED)
    return


@settings(max_examples=20, deadline=None)
@given(
    version=_version_strategy,
    install_source=_install_source_strategy,
    purge=st.booleans(),
)
def test_property_dry_run_never_invokes_runner(version, install_source, purge):
    """
        With ``dry_run=True`` the runner is never invoked, regardless
        of generated inputs.
    """
    runner = _MockCommandRunner()
    config = MacosVPNClientManagerConfig(
        install_source=install_source, dry_run=True,
    )
    manager = MacosVPNClientManager(runner=runner, config=config)

    manager.install_tailscale(
        version=version, channel="stable", aspects=_aspects(),
    )
    manager.install_twingate_connector(
        version=version, aspects=_aspects(),
    )
    manager.uninstall(
        provider=VPNProvider.TAILSCALE,
        purge=purge,
        aspects=_aspects(),
    )

    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# Audit regression tests — C3 (authkey redaction), S-H7 (elevation)
# ----------------------------------------------------------------------


def test_bring_up_redacts_authkey_in_command():
    """
        C3 regression: the Tailscale auth key must be redacted in the
        returned ``MacosVPNActionResult.command``.
    """
    runner = _MockCommandRunner()
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=json.dumps({"BackendState": "Stopped"}), stderr="",
    )
    config = MacosVPNClientManagerConfig(elevated=True)
    manager = MacosVPNClientManager(runner=runner, config=config)

    secret = "tskey-auth-SUPERSECRET"
    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey=secret,
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )

    testbase.assert_true(result.command is not None)
    joined = " ".join(result.command)
    testbase.assert_true(secret not in joined)
    testbase.assert_true("--authkey=***" in joined)
    return


def test_bring_up_tailscale_requires_elevation_when_not_elevated():
    """
        S-H7 regression: ``launchctl bootstrap system`` without
        elevation must return a clear FAILED elevation-required result
        rather than dispatching the command.
    """
    runner = _MockCommandRunner()
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=json.dumps({"BackendState": "Stopped"}), stderr="",
    )
    config = MacosVPNClientManagerConfig(elevated=False)
    manager = MacosVPNClientManager(runner=runner, config=config)

    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey-auth-ABC",
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_aspects(),
    )

    testbase.assert_equal(result.status, MacosVPNActionStatus.FAILED)
    testbase.assert_true("Elevation required" in (result.detail or ""))
    bootstrap_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["launchctl", "bootstrap", "system"]
    ]
    testbase.assert_equal(len(bootstrap_calls), 0)
    return
