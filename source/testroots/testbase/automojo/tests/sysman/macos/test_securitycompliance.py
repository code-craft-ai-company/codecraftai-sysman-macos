"""
    Tests for :class:`MacosSecurityComplianceManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv tuple.  No live ``csrutil`` / ``spctl`` / ``fdesetup`` /
    ``pfctl`` / ``bputil`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.securitycompliance import (
    MacosBaselineInfo,
    MacosSecurityComplianceActionStatus,
    MacosSecurityComplianceManager,
    MacosSecurityComplianceManagerConfig,
    MacosSecurityComplianceQueryError,
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


def _seed_compliant_host(*, runner: _MockCommandRunner) -> None:
    """
        Seed runner results so every primitive reports compliant.
    """
    runner.results[("csrutil", "status")] = RunResult(
        exit_code=0,
        stdout="System Integrity Protection status: enabled.\n",
        stderr="",
    )
    runner.results[("spctl", "--status")] = RunResult(
        exit_code=0,
        stdout="assessments enabled\n",
        stderr="",
    )
    runner.results[("fdesetup", "status")] = RunResult(
        exit_code=0,
        stdout="FileVault is On.\n",
        stderr="",
    )
    runner.results[("pfctl", "-si")] = RunResult(
        exit_code=0,
        stdout="Status: Enabled for 1 days 02:03:04                Debug: Urgent\n",
        stderr="",
    )
    runner.results[("bputil", "--display")] = RunResult(
        exit_code=0,
        stdout="Boot Policy: Full Security\n",
        stderr="",
    )
    return


# ----------------------------------------------------------------------
# list_profiles
# ----------------------------------------------------------------------


def test_list_profiles_returns_bundled_catalogue():
    """
        list_profiles returns the bundled CIS + Apple profile rows
        without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    profiles = manager.list_profiles()
    profile_ids = [p.id for p in profiles]
    testbase.assert_true("cis_macos_baseline" in profile_ids)
    testbase.assert_true("apple_security_baseline" in profile_ids)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# run_compliance_profile
# ----------------------------------------------------------------------


def test_run_compliance_profile_dispatches_each_primitive():
    """
        run_compliance_profile invokes every check's primitive and
        emits one finding per check.
    """
    runner = _MockCommandRunner()
    _seed_compliant_host(runner=runner)
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    report = manager.run_compliance_profile(
        profile_name="apple_security_baseline",
    )
    testbase.assert_equal(report.profile, "apple_security_baseline")
    testbase.assert_equal(len(report.findings), 5)
    rule_ids = [f.rule_id for f in report.findings]
    testbase.assert_true("sip_enabled" in rule_ids)
    testbase.assert_true("gatekeeper_enabled" in rule_ids)
    testbase.assert_true("filevault_enabled" in rule_ids)
    testbase.assert_true("pf_running" in rule_ids)
    testbase.assert_true("boot_policy_full_security" in rule_ids)
    # Every finding should be pass on the seeded compliant host.
    for finding in report.findings:
        testbase.assert_equal(finding.status, "pass")
    return


def test_run_compliance_profile_marks_failures_when_primitive_disabled():
    """
        Each primitive that reports its feature off produces a
        ``fail`` finding.
    """
    runner = _MockCommandRunner()
    runner.results[("csrutil", "status")] = RunResult(
        exit_code=0,
        stdout="System Integrity Protection status: disabled.\n",
        stderr="",
    )
    runner.results[("spctl", "--status")] = RunResult(
        exit_code=0, stdout="assessments disabled\n", stderr="",
    )
    runner.results[("fdesetup", "status")] = RunResult(
        exit_code=0, stdout="FileVault is Off.\n", stderr="",
    )
    runner.results[("pfctl", "-si")] = RunResult(
        exit_code=0, stdout="Status: Disabled\n", stderr="",
    )
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    report = manager.run_compliance_profile(
        profile_name="cis_macos_baseline",
    )
    statuses = [f.status for f in report.findings]
    testbase.assert_equal(statuses, ["fail", "fail", "fail", "fail"])
    testbase.assert_equal(report.counts.get("fail", 0), 4)
    return


def test_run_compliance_profile_marks_bputil_notapplicable_on_intel():
    """
        bputil's command-not-found stderr maps to a
        ``notapplicable`` finding rather than ``error``.
    """
    runner = _MockCommandRunner()
    _seed_compliant_host(runner=runner)
    runner.results[("bputil", "--display")] = RunResult(
        exit_code=127,
        stdout="",
        stderr="bputil: command not found",
    )
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    report = manager.run_compliance_profile(
        profile_name="apple_security_baseline",
    )
    boot_finding = None
    for finding in report.findings:
        if finding.rule_id == "boot_policy_full_security":
            boot_finding = finding
            break
    testbase.assert_true(boot_finding is not None)
    testbase.assert_equal(boot_finding.status, "notapplicable")
    return


def test_run_compliance_profile_unknown_profile_raises():
    """
        run_compliance_profile raises
        :class:`MacosSecurityComplianceQueryError` for an unknown
        profile id.
    """
    runner = _MockCommandRunner()
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    raised = False
    try:
        manager.run_compliance_profile(profile_name="ghost_profile")
    except MacosSecurityComplianceQueryError:
        raised = True
    testbase.assert_true(raised)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_run_compliance_profile_marks_error_on_primitive_failure():
    """
        A non-zero exit (without a recognised "unsupported" stderr)
        produces an ``error`` finding.
    """
    runner = _MockCommandRunner()
    _seed_compliant_host(runner=runner)
    runner.results[("csrutil", "status")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="csrutil: I/O error",
    )
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    report = manager.run_compliance_profile(
        profile_name="cis_macos_baseline",
    )
    sip_finding = None
    for finding in report.findings:
        if finding.rule_id == "sip_enabled":
            sip_finding = finding
            break
    testbase.assert_true(sip_finding is not None)
    testbase.assert_equal(sip_finding.status, "error")
    return


# ----------------------------------------------------------------------
# remediate
# ----------------------------------------------------------------------


def test_remediate_returns_failed_unsupported():
    """
        remediate is a synchronous FAILED stub for the pilot.
    """
    runner = _MockCommandRunner()
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    result = manager.remediate(finding_id="sip_enabled")
    testbase.assert_equal(
        result.status, MacosSecurityComplianceActionStatus.FAILED,
    )
    testbase.assert_equal(result.finding_id, "sip_enabled")
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# get_baseline / set_baseline
# ----------------------------------------------------------------------


def test_get_baseline_returns_none_when_file_missing():
    """
        get_baseline returns ``None`` when the baseline file does
        not exist.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=1, stdout="", stderr="No such file")
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    rtn = manager.get_baseline()
    testbase.assert_true(rtn is None)
    return


def test_get_baseline_decodes_existing_json():
    """
        get_baseline decodes the on-disk JSON into a
        :class:`MacosBaselineInfo`.
    """
    body = json.dumps({
        "profile": "cis_macos_baseline",
        "applied_at": "2024-04-01T00:00:00Z",
    }) + "\n"
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=0, stdout=body, stderr="")
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    info = manager.get_baseline()
    testbase.assert_true(info is not None)
    testbase.assert_equal(info.profile, "cis_macos_baseline")
    testbase.assert_equal(info.applied_at, "2024-04-01T00:00:00Z")
    return


def test_set_baseline_writes_via_tee_when_content_differs():
    """
        set_baseline dispatches ``tee <baseline_path>`` with the
        rendered JSON on stdin when the existing file does not
        match.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=1, stdout="", stderr="")
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    spec = MacosBaselineInfo(
        profile="cis_macos_baseline",
        applied_at="2024-04-01T00:00:00Z",
    )
    result = manager.set_baseline(spec=spec)
    testbase.assert_equal(
        result.status, MacosSecurityComplianceActionStatus.SUCCESS,
    )
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    payload = tee_calls[0]["stdin"]
    decoded = json.loads(payload)
    testbase.assert_equal(decoded["profile"], "cis_macos_baseline")
    testbase.assert_equal(decoded["applied_at"], "2024-04-01T00:00:00Z")
    return


def test_set_baseline_skipped_when_content_matches():
    """
        set_baseline returns SKIPPED when the on-disk JSON already
        matches the rendered desired content.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    spec = MacosBaselineInfo(
        profile="cis_macos_baseline",
        applied_at="2024-04-01T00:00:00Z",
    )

    # Write once to capture the rendered body.
    runner.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=1, stdout="", stderr="")
    manager_one = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    manager_one.set_baseline(spec=spec)
    rendered = None
    for c in runner.calls:
        if c["args"][:1] == ["tee"]:
            rendered = c["stdin"]
            break
    testbase.assert_true(rendered is not None)

    # Now seed the rendered body and re-run.
    runner_two = _MockCommandRunner()
    runner_two.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=0, stdout=rendered, stderr="")
    manager_two = MacosSecurityComplianceManager(
        runner=runner_two,
        config=MacosSecurityComplianceManagerConfig(),
    )
    result = manager_two.set_baseline(spec=spec)
    testbase.assert_equal(
        result.status, MacosSecurityComplianceActionStatus.SKIPPED,
    )
    tee_calls = [
        c for c in runner_two.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


def test_set_baseline_dry_run_short_circuits():
    """
        set_baseline in dry-run mode does not invoke tee.
    """
    runner = _MockCommandRunner()
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(dry_run=True),
    )
    spec = MacosBaselineInfo(
        profile="cis_macos_baseline",
        applied_at="2024-04-01T00:00:00Z",
    )
    result = manager.set_baseline(spec=spec)
    testbase.assert_equal(
        result.status, MacosSecurityComplianceActionStatus.DRY_RUN,
    )
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_set_baseline_elevates_when_configured():
    """
        set_baseline writes pass ``elevated=True`` when configured.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=1, stdout="", stderr="")
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(elevated=True),
    )
    spec = MacosBaselineInfo(
        profile="cis_macos_baseline",
        applied_at="2024-04-01T00:00:00Z",
    )
    manager.set_baseline(spec=spec)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_run_compliance_reads_never_elevate():
    """
        run_compliance_profile dispatches every primitive without
        elevation, even when the config is configured to elevate
        writes.
    """
    runner = _MockCommandRunner()
    _seed_compliant_host(runner=runner)
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(elevated=True),
    )
    manager.run_compliance_profile(profile_name="cis_macos_baseline")
    elevated_reads = [c for c in runner.calls if c["elevated"]]
    testbase.assert_equal(elevated_reads, [])
    return


# ----------------------------------------------------------------------
# Parser surface
# ----------------------------------------------------------------------


def test_csrutil_parser_handles_extended_status_block():
    """
        The csrutil parser only consults the first ``status:`` line
        even when a per-feature breakdown follows.
    """
    runner = _MockCommandRunner()
    _seed_compliant_host(runner=runner)
    runner.results[("csrutil", "status")] = RunResult(
        exit_code=0,
        stdout=(
            "System Integrity Protection status: enabled.\n"
            "\n"
            "    Configuration:\n"
            "        Apple Internal: disabled\n"
        ),
        stderr="",
    )
    manager = MacosSecurityComplianceManager(
        runner=runner,
        config=MacosSecurityComplianceManagerConfig(),
    )
    report = manager.run_compliance_profile(
        profile_name="cis_macos_baseline",
    )
    sip_finding = None
    for finding in report.findings:
        if finding.rule_id == "sip_enabled":
            sip_finding = finding
            break
    testbase.assert_true(sip_finding is not None)
    testbase.assert_equal(sip_finding.status, "pass")
    return
