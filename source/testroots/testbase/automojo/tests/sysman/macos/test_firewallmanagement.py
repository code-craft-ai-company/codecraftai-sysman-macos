"""
    Tests for :class:`MacosFirewallManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv tuple.  No live ``pfctl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.firewallmanagement import (
    MacosFirewallActionStatus,
    MacosFirewallManager,
    MacosFirewallManagerConfig,
    MacosFirewallQueryError,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call
        and returns pre-seeded :class:`RunResult` values keyed by
        argv tuple.
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


def _anchor_path(zone: str) -> str:
    rtnval = "/etc/pf.anchors/ccai.{}".format(zone)
    return rtnval


def _seed_anchor_body(
    *,
    runner: _MockCommandRunner,
    zone: str,
    body: str,
) -> None:
    """
        Seed ``cat <anchor_path>`` with ``body``.
    """
    runner.results[("cat", _anchor_path(zone))] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )
    return


# ----------------------------------------------------------------------
# add_service
# ----------------------------------------------------------------------


def test_add_service_writes_pass_rule_and_reloads_anchor():
    """
        add_service rewrites the per-zone anchor file with the
        resolved ``pass in quick proto tcp ... port 22`` line and
        reloads the anchor via ``pfctl -a -f``.
    """
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body="")
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.add_service(service="ssh", zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SUCCESS)

    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", _anchor_path("trusted")],
    )
    testbase.assert_true(
        "pass in quick proto tcp from any to any port 22" in tee_calls[0]["stdin"],
    )

    reload_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["pfctl", "-a", "ccai.trusted"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    testbase.assert_equal(
        reload_calls[0]["args"],
        [
            "pfctl", "-a", "ccai.trusted",
            "-f", _anchor_path("trusted"),
        ],
    )
    return


def test_add_service_skipped_when_rule_already_present():
    """
        add_service returns SKIPPED when the resolved pass rule is
        already in the anchor file.
    """
    existing_body = (
        "pass in quick proto tcp from any to any port 22\n"
    )
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body=existing_body)
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.add_service(service="ssh", zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SKIPPED)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


def test_add_service_unknown_service_fails():
    """
        Unknown service names map to FAILED without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.add_service(service="nonexistent", zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "unknown_service")
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# remove_service
# ----------------------------------------------------------------------


def test_remove_service_removes_rule_and_reloads():
    """
        remove_service rewrites the anchor without the rule and
        reloads the anchor.
    """
    existing_body = (
        "pass in quick proto tcp from any to any port 22\n"
        "pass in quick proto tcp from any to any port 80\n"
    )
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body=existing_body)
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.remove_service(service="ssh", zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SUCCESS)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_true("port 22" not in written)
    testbase.assert_true("port 80" in written)
    return


def test_remove_service_skipped_when_rule_absent():
    """
        remove_service returns SKIPPED when the rule is not present.
    """
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body="")
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.remove_service(service="ssh", zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# add_port / remove_port
# ----------------------------------------------------------------------


def test_add_port_appends_rule():
    """
        add_port writes a pass rule against the supplied port.
    """
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body="")
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.add_port(port=8443, protocol="tcp", zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SUCCESS)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_true(
        "pass in quick proto tcp from any to any port 8443"
        in tee_calls[0]["stdin"],
    )
    return


def test_add_port_invalid_protocol_fails():
    """
        Invalid protocols map to FAILED without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.add_port(port=22, protocol="sctp", zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "invalid_protocol")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_remove_port_removes_rule_and_reloads():
    """
        remove_port strips the matching pass rule and reloads.
    """
    existing_body = (
        "pass in quick proto udp from any to any port 53\n"
    )
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body=existing_body)
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.remove_port(port=53, protocol="udp", zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SUCCESS)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_true("port 53" not in tee_calls[0]["stdin"])
    return


# ----------------------------------------------------------------------
# add_rich_rule / remove_rich_rule
# ----------------------------------------------------------------------


def test_add_rich_rule_appends_normalised_line():
    """
        add_rich_rule appends a whitespace-normalised line and
        reloads the anchor.
    """
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body="")
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.add_rich_rule(
        rule="block in   quick   from 10.0.0.5",
        zone="trusted",
    )
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SUCCESS)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_true(
        "block in quick from 10.0.0.5" in tee_calls[0]["stdin"],
    )
    return


def test_add_rich_rule_invalid_verb_fails():
    """
        Rules whose first token is not a valid pf verb FAIL without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.add_rich_rule(
        rule="reject in quick from any",
        zone="trusted",
    )
    testbase.assert_equal(result.status, MacosFirewallActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "invalid_rich_rule_verb")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_remove_rich_rule_skipped_when_absent():
    """
        remove_rich_rule returns SKIPPED when the rule is not in
        the anchor.
    """
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body="")
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.remove_rich_rule(
        rule="block in quick from 10.0.0.5",
        zone="trusted",
    )
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SKIPPED)
    return


def test_rich_rule_idempotency_uses_normalised_match():
    """
        A rule with extra whitespace is treated as already-present
        when the anchor stores the normalised form.
    """
    existing_body = "block in quick from 10.0.0.5\n"
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body=existing_body)
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.add_rich_rule(
        rule="block   in   quick   from   10.0.0.5",
        zone="trusted",
    )
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# set_default_zone / reload
# ----------------------------------------------------------------------


def test_set_default_zone_rewrites_pf_conf_and_reloads():
    """
        set_default_zone ensures pf.conf declares the zone's anchor
        as the first ccai anchor, rewrites the file, and reloads
        pf.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/pf.conf")] = RunResult(
        exit_code=0,
        stdout=(
            "# pf.conf\n"
            "scrub-anchor \"com.apple/*\"\n"
        ),
        stderr="",
    )
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.set_default_zone(zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SUCCESS)

    tee_calls = [
        c for c in runner.calls
        if c["args"] == ["tee", "/etc/pf.conf"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_true('anchor "ccai.trusted"' in written)
    testbase.assert_true(
        'load anchor "ccai.trusted" from "/etc/pf.anchors/ccai.trusted"'
        in written,
    )

    reload_calls = [
        c for c in runner.calls
        if c["args"] == ["pfctl", "-f", "/etc/pf.conf"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


def test_set_default_zone_skipped_when_already_first():
    """
        set_default_zone returns SKIPPED when pf.conf already lists
        the zone's anchor first.
    """
    existing_body = (
        "# pf.conf\n"
        'anchor "ccai.trusted"\n'
        'load anchor "ccai.trusted" from "/etc/pf.anchors/ccai.trusted"\n'
    )
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/pf.conf")] = RunResult(
        exit_code=0, stdout=existing_body, stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.set_default_zone(zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SKIPPED)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


def test_reload_dispatches_pfctl_f():
    """
        reload dispatches ``pfctl -f /etc/pf.conf``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    result = manager.reload()
    testbase.assert_equal(result.status, MacosFirewallActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"], ["pfctl", "-f", "/etc/pf.conf"],
    )
    return


# ----------------------------------------------------------------------
# query_zone / query_all_zones / query_state
# ----------------------------------------------------------------------


def test_query_zone_parses_pass_rules_and_back_resolves_services():
    """
        query_zone parses ``pfctl -a <anchor> -sr`` output, extracts
        ports, and back-resolves services via the bundled table.
    """
    rules_stdout = (
        "pass in quick proto tcp from any to any port 22\n"
        "pass in quick proto tcp from any to any port 8443\n"
        "block in quick from 10.0.0.5\n"
    )
    runner = _MockCommandRunner()
    runner.results[("pfctl", "-a", "ccai.trusted", "-sr")] = RunResult(
        exit_code=0, stdout=rules_stdout, stderr="",
    )
    runner.results[("cat", "/etc/pf.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    info = manager.query_zone(zone="trusted")
    testbase.assert_equal(info.zone, "trusted")
    testbase.assert_true("22/tcp" in info.ports)
    testbase.assert_true("8443/tcp" in info.ports)
    testbase.assert_true("ssh" in info.services)
    testbase.assert_true("block in quick from 10.0.0.5" in info.rich_rules)
    testbase.assert_equal(info.is_default, False)
    return


def test_query_zone_raises_query_error_on_failure():
    """
        query_zone raises :class:`MacosFirewallQueryError` when
        ``pfctl`` exits non-zero with non-empty stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("pfctl", "-a", "ccai.trusted", "-sr")] = RunResult(
        exit_code=1, stdout="", stderr="permission denied",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    raised = False
    try:
        manager.query_zone(zone="trusted")
    except MacosFirewallQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_query_all_zones_filters_to_ccai_anchors():
    """
        query_all_zones reads ``pfctl -sA``, filters to
        ``ccai.*`` anchors, and parses each.
    """
    list_stdout = (
        "  com.apple/something\n"
        "  ccai.trusted\n"
        "  ccai.public\n"
        "  other-thing\n"
    )
    runner = _MockCommandRunner()
    runner.results[("pfctl", "-sA")] = RunResult(
        exit_code=0, stdout=list_stdout, stderr="",
    )
    runner.results[("pfctl", "-a", "ccai.trusted", "-sr")] = RunResult(
        exit_code=0,
        stdout="pass in quick proto tcp from any to any port 22\n",
        stderr="",
    )
    runner.results[("pfctl", "-a", "ccai.public", "-sr")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("cat", "/etc/pf.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    zones = manager.query_all_zones()
    zone_names = [info.zone for info in zones]
    testbase.assert_equal(zone_names, ["trusted", "public"])
    return


def test_query_state_reports_running_when_pfctl_si_enabled():
    """
        query_state reports ``running=True`` for ``Status: Enabled``.
    """
    runner = _MockCommandRunner()
    runner.results[("pfctl", "-si")] = RunResult(
        exit_code=0,
        stdout=(
            "Status: Enabled for 0 days 00:01:23                Debug: Urgent\n"
            "Hostid: 0x12345678\n"
        ),
        stderr="",
    )
    runner.results[("cat", "/etc/pf.conf")] = RunResult(
        exit_code=0,
        stdout='anchor "ccai.trusted"\n',
        stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    state = manager.query_state()
    testbase.assert_equal(state.running, True)
    testbase.assert_equal(state.default_zone, "trusted")
    return


def test_query_state_reports_inactive_when_status_disabled():
    """
        query_state reports ``running=False`` when ``pfctl -si``
        reports ``Status: Disabled``.
    """
    runner = _MockCommandRunner()
    runner.results[("pfctl", "-si")] = RunResult(
        exit_code=0,
        stdout="Status: Disabled\n",
        stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner, config=MacosFirewallManagerConfig(),
    )
    state = manager.query_state()
    testbase.assert_equal(state.running, False)
    testbase.assert_equal(state.default_zone, "")
    return


# ----------------------------------------------------------------------
# Dry-run
# ----------------------------------------------------------------------


def test_add_service_dry_run_short_circuits():
    """
        add_service in dry-run mode does not invoke the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosFirewallManager(
        runner=runner,
        config=MacosFirewallManagerConfig(dry_run=True),
    )
    result = manager.add_service(service="ssh", zone="trusted")
    testbase.assert_equal(result.status, MacosFirewallActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate_when_configured_reads_do_not():
    """
        Mutating commands pass ``elevated=True`` when the config
        requests it; reads / idempotency probes do not.
    """
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body="")
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosFirewallManager(
        runner=runner,
        config=MacosFirewallManagerConfig(elevated=True),
    )
    manager.add_service(service="ssh", zone="trusted")
    manager.query_state()

    write_calls = [
        c for c in runner.calls
        if c["args"][:1] in (["tee"], ["pfctl"]) and c["elevated"]
    ]
    testbase.assert_true(len(write_calls) >= 1)

    read_cat_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["cat"]
    ]
    elevated_reads = [c for c in read_cat_calls if c["elevated"]]
    testbase.assert_equal(elevated_reads, [])
    return


# ----------------------------------------------------------------------
# Custom services table override
# ----------------------------------------------------------------------


def test_services_table_override_replaces_bundled_defaults():
    """
        A non-None ``services_table`` on the config replaces the
        bundled defaults.
    """
    runner = _MockCommandRunner()
    _seed_anchor_body(runner=runner, zone="trusted", body="")
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    custom_table = {"app1": (9000, "tcp")}
    manager = MacosFirewallManager(
        runner=runner,
        config=MacosFirewallManagerConfig(
            services_table=custom_table,
        ),
    )
    # ssh is in the bundled table but NOT in the override.
    result_ssh = manager.add_service(service="ssh", zone="trusted")
    testbase.assert_equal(result_ssh.status, MacosFirewallActionStatus.FAILED)
    # The custom service should resolve.
    result_app = manager.add_service(service="app1", zone="trusted")
    testbase.assert_equal(result_app.status, MacosFirewallActionStatus.SUCCESS)
    return
