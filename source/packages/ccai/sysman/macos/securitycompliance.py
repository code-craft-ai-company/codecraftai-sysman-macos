"""
    macOS security-compliance feature.

    macOS lacks a unified compliance framework comparable to OpenSCAP.
    This module provides a primitive-aggregating skeleton that
    interrogates a curated set of macOS-native security primitives
    (``csrutil``, ``bputil``, ``spctl``, ``fdesetup``, ``pfctl``) and
    emits a tolerant :class:`MacosComplianceReport` whose
    :class:`MacosComplianceFinding` rows are derived from those
    primitives' textual output.

    A future enhancement could integrate
    `mSCP <https://github.com/usnistgov/macos_security>`_ (the macOS
    Security Compliance Project) baselines.  That work is documented
    in ``specs/3-security-compliance/tasks.md``.

    File I/O policy:
        Every file read / write is performed through the injected
        :class:`ICommandRunner` (``cat`` / ``tee`` / ``mkdir -p``).
        This module does NOT consume an :class:`IFileOperator`.

    Elevation policy:
        ``run_compliance_profile`` and ``set_baseline`` honour
        ``config.elevated``.  ``list_profiles`` / ``get_baseline`` /
        ``remediate`` never elevate (``remediate`` is a synchronous
        FAILED stub for the pilot — see :meth:`remediate`).

    Verb surface:
        * ``run_compliance_profile`` runs each registered primitive,
          maps its output to one or more :class:`MacosComplianceFinding`
          rows, and assembles the report in pure Python.  The
          "profile" is interpreted as a config-driven set of checks;
          the bundled profiles are ``cis_macos_baseline`` and
          ``apple_security_baseline``.
        * ``list_profiles`` returns the bundled profile catalogue.
        * ``remediate`` is a synchronous FAILED stub.  macOS automated
          remediation requires user-interactive Recovery mode for
          several settings (e.g., reduced-security boot policy, SIP
          toggling) and is intentionally out of scope for the pilot.
        * ``get_baseline`` reads
          ``<output_dir>/current_baseline.json`` via ``cat`` and
          decodes the JSON into a :class:`MacosBaselineInfo`.
        * ``set_baseline`` writes that file via ``tee`` over stdin
          when its existing JSON does not match the requested spec.
"""

import json
import os
import time

from dataclasses import dataclass, field
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_CSRUTIL = "csrutil"
_BPUTIL = "bputil"
_SPCTL = "spctl"
_FDESETUP = "fdesetup"
_PFCTL = "pfctl"

_DEFAULT_OUTPUT_DIR = "/var/lib/ccai/compliance"
_BASELINE_FILENAME = "current_baseline.json"

# Bundled profile catalogue.  Each profile owns a sequence of "checks"
# — primitive identifiers consumed by :meth:`_run_check`.
_PROFILE_CIS = "cis_macos_baseline"
_PROFILE_APPLE = "apple_security_baseline"

_PROFILE_CATALOGUE = {
    _PROFILE_CIS: {
        "title": "CIS macOS Baseline (pilot)",
        "description": (
            "Pilot subset of CIS macOS controls expressed via "
            "csrutil / spctl / fdesetup / pfctl primitives."
        ),
        "checks": [
            "sip_enabled",
            "gatekeeper_enabled",
            "filevault_enabled",
            "pf_running",
        ],
    },
    _PROFILE_APPLE: {
        "title": "Apple Security Baseline (pilot)",
        "description": (
            "Apple-Silicon-aware baseline that augments the CIS "
            "subset with bputil-derived boot-policy findings."
        ),
        "checks": [
            "sip_enabled",
            "gatekeeper_enabled",
            "filevault_enabled",
            "pf_running",
            "boot_policy_full_security",
        ],
    },
}


MacosSecurityComplianceActionStatus = ActionStatus


@dataclass
class MacosSecurityComplianceManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosSecurityComplianceManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param output_dir: Directory holding the persisted baseline
                           JSON.  Defaults to
                           ``/var/lib/ccai/compliance``.
    """
    output_dir: str = _DEFAULT_OUTPUT_DIR


@dataclass
class MacosSecurityComplianceActionResult(FeatureActionResult):
    """
        Result of a security-compliance write operation.

        :param profile: The profile id targeted, when applicable.
        :param finding_id: The rule / finding id targeted, when
                           applicable.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
        :param target: Optional secondary identifier (typically the
                       baseline file path for ``set_baseline``).
    """
    profile: Optional[str] = None
    finding_id: Optional[str] = None
    message: Optional[str] = None
    target: Optional[str] = None


@dataclass
class MacosComplianceProfile:
    """
        One entry in the bundled profile catalogue.

        :param id: Canonical profile id (e.g. ``cis_macos_baseline``).
        :param title: Human-readable title.
        :param description: Optional long-form description; ``None``
                            when not surfaced.
    """
    id: str
    title: str
    description: Optional[str] = None


@dataclass
class MacosComplianceFinding:
    """
        Single finding emitted by :meth:`MacosSecurityComplianceManager.run_compliance_profile`.

        :param rule_id: The rule identifier (one of the check names
                        defined in :data:`_PROFILE_CATALOGUE`).
        :param severity: The rule severity (``low``/``medium``/
                         ``high``/``critical``).
        :param title: Human-readable rule title.
        :param description: Optional long-form description; ``None``
                            when not parsed.
        :param status: Result of the rule evaluation.  One of
                       ``pass``, ``fail``, ``notapplicable``,
                       ``error``, ``unknown``.
        :param remediation_available: Pilot-time flag — always False
                                      because automated remediation is
                                      out of scope.
        :param remediation_automated: Pilot-time flag — always False.
    """
    rule_id: str
    severity: str
    title: str
    description: Optional[str]
    status: str
    remediation_available: bool = False
    remediation_automated: bool = False


@dataclass
class MacosComplianceReport:
    """
        Aggregated outcome of a profile run.

        :param profile: The profile id that was evaluated.
        :param generated_at: ISO-8601 UTC timestamp captured at the
                             moment the report was assembled.
        :param findings: One :class:`MacosComplianceFinding` per
                         primitive evaluated.
        :param counts: Mapping of result-state name to frequency.
    """
    profile: str
    generated_at: str
    findings: list[MacosComplianceFinding]
    counts: dict = field(default_factory=dict)


@dataclass
class MacosBaselineInfo:
    """
        Currently-persisted compliance baseline.

        :param profile: The profile id captured by the baseline.
        :param applied_at: ISO-8601 timestamp at which the baseline
                           was last applied.
    """
    profile: str
    applied_at: str


class MacosSecurityComplianceQueryError(Exception):
    """
        Raised when a security-compliance read fails for a reason
        other than a target simply being absent.
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

        err_msg_lines = ["Security compliance query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosSecurityComplianceManager(BaseFeatureManager):
    """
        macOS security-compliance manager — a primitive-aggregating
        skeleton, not a full compliance framework.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`; baseline JSON I/O is performed via
        ``cat`` / ``tee`` rather than a separate file operator.
    """

    result_class = MacosSecurityComplianceActionResult
    status_class = MacosSecurityComplianceActionStatus
    feature_name = "security-compliance"

    def __init__(
        self,
        runner,
        config: Optional[MacosSecurityComplianceManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           operation.
            :param config: Manager configuration.  Defaults to a fresh
                           :class:`MacosSecurityComplianceManagerConfig`
                           when omitted.
        """
        if config is None:
            config = MacosSecurityComplianceManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[MacosComplianceProfile]:
        """
            Return the bundled profile catalogue.

            macOS lacks a runtime profile-discovery mechanism; the
            pilot ships a static catalogue.  Reads never elevate.

            :returns: One :class:`MacosComplianceProfile` per
                      catalogue entry, in catalogue order.
        """
        rtnval: list[MacosComplianceProfile] = []
        for profile_id in (_PROFILE_CIS, _PROFILE_APPLE):
            entry = _PROFILE_CATALOGUE[profile_id]
            profile = MacosComplianceProfile(
                id=profile_id,
                title=entry["title"],
                description=entry["description"],
            )
            rtnval.append(profile)
        return rtnval

    def get_baseline(self) -> Optional[MacosBaselineInfo]:
        """
            Read the persisted baseline JSON via ``cat``.

            :returns: The decoded :class:`MacosBaselineInfo`, or
                      ``None`` when the baseline file does not exist
                      or contains an unparseable payload.
        """
        path = self._baseline_path()
        body = self._read_file(path=path)
        if body is None:
            rtnval: Optional[MacosBaselineInfo] = None
            return rtnval

        try:
            decoded = json.loads(body)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        if not isinstance(decoded, dict):
            rtnval = None
            return rtnval

        profile_value = decoded.get("profile", "")
        applied_value = decoded.get("applied_at", "")

        if not isinstance(profile_value, str):
            profile_value = str(profile_value)
        if not isinstance(applied_value, str):
            applied_value = str(applied_value)

        rtnval = MacosBaselineInfo(
            profile=profile_value,
            applied_at=applied_value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def run_compliance_profile(
        self,
        profile_name: str,
    ) -> MacosComplianceReport:
        """
            Run a profile against the host and assemble a tolerant
            report.

            For each check declared in the profile, dispatches the
            corresponding macOS primitive and maps the output to a
            :class:`MacosComplianceFinding`.  Non-zero exits from a
            primitive map to a finding with ``status="error"``.

            :param profile_name: The profile id to evaluate.

            :returns: The :class:`MacosComplianceReport`.
            :raises MacosSecurityComplianceQueryError: When
                ``profile_name`` is not in the bundled catalogue.
        """
        if profile_name not in _PROFILE_CATALOGUE:
            err_msg_lines = [
                "Unknown compliance profile: {!r}.".format(profile_name),
            ]
            err_msg_lines.append("    KNOWN PROFILES:")
            for known in sorted(_PROFILE_CATALOGUE.keys()):
                err_msg_lines.append("        {}".format(known))
            err_msg = os.linesep.join(err_msg_lines)
            raise MacosSecurityComplianceQueryError(
                command=[],
                exit_code=None,
                stderr=err_msg,
            )

        profile_entry = _PROFILE_CATALOGUE[profile_name]
        findings: list[MacosComplianceFinding] = []
        for check_id in profile_entry["checks"]:
            finding = self._run_check(check_id=check_id)
            findings.append(finding)

        counts: dict = {}
        for finding in findings:
            current = counts.get(finding.status, 0)
            counts[finding.status] = current + 1

        rtnval = MacosComplianceReport(
            profile=profile_name,
            generated_at=_iso_utc_now(),
            findings=findings,
            counts=counts,
        )
        return rtnval

    def remediate(
        self,
        finding_id: str,
    ) -> MacosSecurityComplianceActionResult:
        """
            Synchronous FAILED stub.

            macOS automated remediation is out of scope for the
            pilot.  Several settings (reduced-security boot policy,
            SIP toggling) require user-interactive Recovery mode;
            others (Gatekeeper, FileVault) are user-policy decisions
            that should not be silently flipped by automation.

            :param finding_id: The finding identifier the caller asked
                               to remediate.

            :returns: A :class:`MacosSecurityComplianceActionResult`
                      with status ``FAILED`` and a descriptive
                      ``message``.
        """
        message = (
            "macOS automated remediation is out of scope; "
            "{} requires user-interactive remediation".format(
                finding_id,
            )
        )
        rtnval = MacosSecurityComplianceActionResult(
            status=MacosSecurityComplianceActionStatus.FAILED,
            detail=finding_id,
            command=None,
            profile=None,
            finding_id=finding_id,
            message=message,
            stderr="remediation_unsupported",
        )
        return rtnval

    def set_baseline(
        self,
        spec: MacosBaselineInfo,
    ) -> MacosSecurityComplianceActionResult:
        """
            Persist ``spec`` as the current baseline JSON.

            Skipped when the existing on-disk JSON decodes to a
            byte-identical payload.

            :param spec: The :class:`MacosBaselineInfo` to persist.

            :returns: The :class:`MacosSecurityComplianceActionResult`.
        """
        path = self._baseline_path()
        desired = _render_baseline(spec=spec)

        write_command = ["tee", path]

        if self._config.dry_run:
            dry = MacosSecurityComplianceActionResult(
                status=MacosSecurityComplianceActionStatus.DRY_RUN,
                detail=spec.profile,
                command=list(write_command),
                profile=spec.profile,
                finding_id=None,
                target=path,
            )
            return dry

        existing = self._read_file(path=path)
        if existing is not None and existing == desired:
            skipped = MacosSecurityComplianceActionResult(
                status=MacosSecurityComplianceActionStatus.SKIPPED,
                detail=spec.profile,
                command=list(write_command),
                profile=spec.profile,
                finding_id=None,
                target=path,
                message="baseline already matches desired content",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosSecurityComplianceActionStatus.SUCCESS
        else:
            mapped_status = MacosSecurityComplianceActionStatus.FAILED

        rtnval = MacosSecurityComplianceActionResult(
            status=mapped_status,
            detail=spec.profile,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            profile=spec.profile,
            finding_id=None,
            target=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — checks
    # ------------------------------------------------------------------

    def _run_check(self, *, check_id: str) -> MacosComplianceFinding:
        """
            Dispatch a single check by id and return the parsed
            finding.

            :param check_id: Check identifier (one of the values in a
                             profile's ``checks`` list).

            :returns: The :class:`MacosComplianceFinding`.
        """
        if check_id == "sip_enabled":
            rtnval = self._check_sip()
        elif check_id == "gatekeeper_enabled":
            rtnval = self._check_gatekeeper()
        elif check_id == "filevault_enabled":
            rtnval = self._check_filevault()
        elif check_id == "pf_running":
            rtnval = self._check_pf()
        elif check_id == "boot_policy_full_security":
            rtnval = self._check_boot_policy()
        else:
            rtnval = MacosComplianceFinding(
                rule_id=check_id,
                severity="medium",
                title="unknown_check_{}".format(check_id),
                description="Unknown check id; profile catalogue out of sync.",
                status="error",
            )
        return rtnval

    def _check_sip(self) -> MacosComplianceFinding:
        """
            Evaluate ``csrutil status``.

            ``csrutil status`` reports
            ``System Integrity Protection status: enabled.`` when SIP
            is on.  Anything else is treated as fail.

            :returns: The :class:`MacosComplianceFinding`.
        """
        command = [_CSRUTIL, "status"]
        run_result = self._runner.run(list(command), elevated=False)

        title = "System Integrity Protection enabled"
        description = (
            "csrutil reports SIP as enabled, blocking modification of "
            "protected system files."
        )

        if run_result.exit_code != 0:
            status = "error"
        elif _csrutil_status_is_enabled(text=run_result.stdout):
            status = "pass"
        else:
            status = "fail"

        rtnval = MacosComplianceFinding(
            rule_id="sip_enabled",
            severity="high",
            title=title,
            description=description,
            status=status,
        )
        return rtnval

    def _check_gatekeeper(self) -> MacosComplianceFinding:
        """
            Evaluate ``spctl --status``.

            ``spctl --status`` reports
            ``assessments enabled`` when Gatekeeper is on.

            :returns: The :class:`MacosComplianceFinding`.
        """
        command = [_SPCTL, "--status"]
        run_result = self._runner.run(list(command), elevated=False)

        title = "Gatekeeper assessments enabled"
        description = (
            "spctl reports developer-id assessments as enabled."
        )

        if run_result.exit_code != 0:
            status = "error"
        elif _spctl_status_is_enabled(text=run_result.stdout):
            status = "pass"
        else:
            status = "fail"

        rtnval = MacosComplianceFinding(
            rule_id="gatekeeper_enabled",
            severity="medium",
            title=title,
            description=description,
            status=status,
        )
        return rtnval

    def _check_filevault(self) -> MacosComplianceFinding:
        """
            Evaluate ``fdesetup status``.

            ``fdesetup status`` reports
            ``FileVault is On.`` when full-disk encryption is
            enabled.

            :returns: The :class:`MacosComplianceFinding`.
        """
        command = [_FDESETUP, "status"]
        run_result = self._runner.run(list(command), elevated=False)

        title = "FileVault enabled"
        description = (
            "fdesetup reports the boot volume as encrypted with "
            "FileVault."
        )

        if run_result.exit_code != 0:
            status = "error"
        elif _fdesetup_status_is_on(text=run_result.stdout):
            status = "pass"
        else:
            status = "fail"

        rtnval = MacosComplianceFinding(
            rule_id="filevault_enabled",
            severity="high",
            title=title,
            description=description,
            status=status,
        )
        return rtnval

    def _check_pf(self) -> MacosComplianceFinding:
        """
            Evaluate ``pfctl -si``.

            ``pfctl -si`` reports a ``Status:`` line whose value is
            ``Enabled`` when pf is running.

            :returns: The :class:`MacosComplianceFinding`.
        """
        command = [_PFCTL, "-si"]
        run_result = self._runner.run(list(command), elevated=False)

        title = "Packet filter (pf) enabled"
        description = (
            "pfctl reports the packet filter as enabled (cross-"
            "reference firewall-management feature)."
        )

        if run_result.exit_code != 0:
            status = "error"
        elif _pfctl_status_is_enabled(text=run_result.stdout):
            status = "pass"
        else:
            status = "fail"

        rtnval = MacosComplianceFinding(
            rule_id="pf_running",
            severity="medium",
            title=title,
            description=description,
            status=status,
        )
        return rtnval

    def _check_boot_policy(self) -> MacosComplianceFinding:
        """
            Evaluate ``bputil --display`` (Apple Silicon only).

            On Intel Macs ``bputil`` is absent; the check returns a
            ``notapplicable`` finding rather than ``error`` so the
            profile counts reflect platform reality.

            :returns: The :class:`MacosComplianceFinding`.
        """
        command = [_BPUTIL, "--display"]
        run_result = self._runner.run(list(command), elevated=False)

        title = "Boot policy: Full Security"
        description = (
            "bputil reports the boot policy mode as Full Security "
            "(Apple Silicon required)."
        )

        if run_result.exit_code != 0:
            stderr_lower = run_result.stderr.lower()
            looks_unsupported = (
                "command not found" in stderr_lower
                or "unsupported" in stderr_lower
                or "not supported" in stderr_lower
            )
            if looks_unsupported:
                status = "notapplicable"
            else:
                status = "error"
        elif _bputil_is_full_security(text=run_result.stdout):
            status = "pass"
        else:
            status = "fail"

        rtnval = MacosComplianceFinding(
            rule_id="boot_policy_full_security",
            severity="high",
            title=title,
            description=description,
            status=status,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — file I/O
    # ------------------------------------------------------------------

    def _baseline_path(self) -> str:
        """
            Build the absolute path to the persisted baseline JSON.

            :returns: ``<output_dir>/current_baseline.json``.
        """
        rtnval = "{}/{}".format(
            self._config.output_dir, _BASELINE_FILENAME,
        )
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


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _iso_utc_now() -> str:
    """
        Build an ISO-8601 UTC timestamp at second precision.

        :returns: ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    rtnval = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return rtnval


def _render_baseline(*, spec: MacosBaselineInfo) -> str:
    """
        Render a :class:`MacosBaselineInfo` into the canonical
        baseline JSON body.

        Output is deterministic (sorted keys, two-space indent,
        trailing newline) so that
        :meth:`MacosSecurityComplianceManager.set_baseline` can
        compare against existing on-disk content for idempotency.

        :param spec: The :class:`MacosBaselineInfo` to render.

        :returns: The full file body as a string ending in ``\\n``.
    """
    payload = {
        "profile": spec.profile,
        "applied_at": spec.applied_at,
    }
    body = json.dumps(payload, sort_keys=True, indent=2)
    rtnval = body + "\n"
    return rtnval


def _csrutil_status_is_enabled(*, text: str) -> bool:
    """
        Detect SIP-enabled in ``csrutil status`` output.

        Output forms::

            System Integrity Protection status: enabled.
            System Integrity Protection status: disabled.

        Some macOS versions append a per-feature breakdown after the
        status line; we only consult the first ``status:`` line.

        :param text: Raw stdout.

        :returns: True when the status line reports ``enabled``.
    """
    for raw_line in text.splitlines():
        line = raw_line.strip().lower()
        if "system integrity protection status:" not in line:
            continue
        if "enabled" in line:
            rtnval = True
            return rtnval
        rtnval = False
        return rtnval

    rtnval = False
    return rtnval


def _spctl_status_is_enabled(*, text: str) -> bool:
    """
        Detect Gatekeeper-enabled in ``spctl --status`` output.

        Output forms::

            assessments enabled
            assessments disabled

        :param text: Raw stdout.

        :returns: True when the output reports ``assessments
                  enabled``.
    """
    lowered = text.lower()
    if "assessments enabled" in lowered:
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval


def _fdesetup_status_is_on(*, text: str) -> bool:
    """
        Detect FileVault-on in ``fdesetup status`` output.

        Output forms::

            FileVault is On.
            FileVault is Off.

        :param text: Raw stdout.

        :returns: True when the output reports ``FileVault is On``.
    """
    lowered = text.lower()
    if "filevault is on" in lowered:
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval


def _pfctl_status_is_enabled(*, text: str) -> bool:
    """
        Detect pf-enabled in ``pfctl -si`` output.

        Output forms::

            Status: Enabled for ...
            Status: Disabled

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


def _bputil_is_full_security(*, text: str) -> bool:
    """
        Detect Full-Security boot policy in ``bputil --display``
        output.

        ``bputil --display`` prints a multi-line property list whose
        ``Security Mode:`` (or ``Boot Policy:``) line contains
        ``Full Security`` when the host is in the strictest mode.

        :param text: Raw stdout.

        :returns: True when ``Full Security`` is present.
    """
    lowered = text.lower()
    if "full security" in lowered:
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval
