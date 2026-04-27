"""
    Tests for :class:`MacosCertificateIssuanceManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live filesystem is touched; ``security`` / ``openssl`` /
    ``cat`` / ``cmp`` are all faked.
"""

import base64
import hashlib

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.certificateissuance import (
    MacosCertIssuanceActionStatus,
    MacosCertIssuanceManagerConfig,
    MacosCertIssuanceQueryError,
    MacosCertificateIssuanceManager,
    MacosCertificateRequestSpec,
    MacosRevocationReason,
)


# ----------------------------------------------------------------------
# Test helpers
# ----------------------------------------------------------------------


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
            "env": env,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _make_pem(*, der_bytes: bytes) -> str:
    encoded = base64.b64encode(der_bytes).decode("ascii")
    body_lines: list = []
    cursor = 0
    while cursor < len(encoded):
        body_lines.append(encoded[cursor:cursor + 64])
        cursor += 64
    rtnval_lines = ["-----BEGIN CERTIFICATE-----"]
    rtnval_lines.extend(body_lines)
    rtnval_lines.append("-----END CERTIFICATE-----")
    rtnval = "\n".join(rtnval_lines) + "\n"
    return rtnval


def _expected_fingerprint(*, der_bytes: bytes) -> str:
    digest = hashlib.sha256(der_bytes).hexdigest().upper()
    rtnval = digest
    return rtnval


_DER_FOO = b"DER-FOO-BYTES-1234567890"
_DER_BAR = b"DER-BAR-BYTES-ABCDEFGHIJ"


def _foo_pem() -> str:
    rtnval = _make_pem(der_bytes=_DER_FOO)
    return rtnval


def _bar_pem() -> str:
    rtnval = _make_pem(der_bytes=_DER_BAR)
    return rtnval


def _foo_fp() -> str:
    rtnval = _expected_fingerprint(der_bytes=_DER_FOO)
    return rtnval


def _far_future_not_after() -> str:
    target = datetime.now(tz=timezone.utc) + timedelta(days=3650)
    rtnval = target.strftime("%b %d %H:%M:%S %Y GMT")
    return rtnval


def _near_expiry_not_after() -> str:
    target = datetime.now(tz=timezone.utc) + timedelta(days=5)
    rtnval = target.strftime("%b %d %H:%M:%S %Y GMT")
    return rtnval


def _seed_openssl_text(
    *,
    runner: _MockCommandRunner,
    pem: str,
    not_after: str,
    subject: str = "CN = example.com",
    issuer: str = "CN = Foo CA",
    sans_text: Optional[str] = None,
) -> None:
    """
        Pre-seed the runner with a canned ``openssl x509 -text`` output
        for any invocation that pipes ``pem`` on stdin.
    """
    body_lines = [
        "subject={}".format(subject),
        "issuer={}".format(issuer),
        "notBefore=Jan 01 00:00:00 2024 GMT",
        "notAfter={}".format(not_after),
        "serial=01:23:45",
        "Certificate:",
        "    Data:",
        "        Subject Public Key Info:",
        "            Public Key Algorithm: rsaEncryption",
        "                Public-Key: (2048 bit)",
        "    X509v3 extensions:",
    ]
    if sans_text is not None:
        body_lines.append("        X509v3 Subject Alternative Name: ")
        body_lines.append("            {}".format(sans_text))
    body_lines.append("    Signature Algorithm: sha256WithRSAEncryption")
    canned = "\n".join(body_lines)
    key = (
        "openssl", "x509", "-noout",
        "-text",
        "-fingerprint", "-sha256",
        "-dates",
        "-subject", "-issuer", "-serial",
    )
    # The runner seeds by argv tuple; the stdin (the PEM) is irrelevant
    # to the mock's lookup, so any invocation matching the argv returns
    # this same canned output.  Tests that need different cert texts
    # would re-seed with a different runner instance.
    runner.results[key] = RunResult(
        exit_code=0, stdout=canned, stderr="",
    )
    return


def _seed_find_certificate(
    *,
    runner: _MockCommandRunner,
    alias: str,
    pem: Optional[str],
    keychain: str = "/Library/Keychains/System.keychain",
) -> None:
    """
        Pre-seed ``security find-certificate -c <alias> -p <keychain>``.
        Pass ``pem=None`` to simulate "alias not found".
    """
    key = (
        "security", "find-certificate",
        "-c", alias, "-p", keychain,
    )
    if pem is None:
        runner.results[key] = RunResult(
            exit_code=44, stdout="", stderr="not found",
        )
    else:
        runner.results[key] = RunResult(
            exit_code=0, stdout=pem, stderr="",
        )
    return


# ----------------------------------------------------------------------
# request_certificate
# ----------------------------------------------------------------------


def test_request_certificate_invokes_openssl_with_subject():
    """
        request_certificate dispatches openssl req with the configured
        subject CN and key algorithm.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=None)

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    spec = MacosCertificateRequestSpec(
        alias="web",
        common_name="example.com",
        key_length=2048,
        validity_days=90,
        is_self_signed=True,
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SUCCESS)
    openssl_calls = [c for c in runner.calls if c["args"][0] == "openssl"]
    testbase.assert_equal(len(openssl_calls), 2)
    req_call = openssl_calls[0]
    testbase.assert_equal(req_call["args"][1], "req")
    testbase.assert_equal(req_call["args"][2], "-newkey")
    testbase.assert_equal(req_call["args"][3], "rsa:2048")
    testbase.assert_equal(req_call["args"][4], "-nodes")
    # verify subj is ours
    subj_idx = req_call["args"].index("-subj")
    testbase.assert_equal(req_call["args"][subj_idx + 1], "/CN=example.com")
    return


def test_request_certificate_skipped_when_present_and_not_near_expiry():
    """
        request_certificate returns SKIPPED when the alias is already
        present and the existing cert is well clear of the renew
        threshold.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=_foo_pem())
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
    )

    config = MacosCertIssuanceManagerConfig(
        elevated=True, renew_threshold_days=30,
    )
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    spec = MacosCertificateRequestSpec(
        alias="web", common_name="example.com",
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SKIPPED)
    openssl_calls = [
        c for c in runner.calls
        if c["args"][0] == "openssl" and c["args"][1] == "req"
    ]
    testbase.assert_equal(openssl_calls, [])
    return


def test_request_certificate_dry_run_short_circuits():
    """
        request_certificate with dry_run=True returns DRY_RUN and the
        runner is never invoked with openssl req.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=None)

    config = MacosCertIssuanceManagerConfig(
        dry_run=True, elevated=True,
    )
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    spec = MacosCertificateRequestSpec(
        alias="web", common_name="example.com",
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.DRY_RUN)
    openssl_calls = [
        c for c in runner.calls
        if c["args"][0] == "openssl" and c["args"][1] == "req"
    ]
    testbase.assert_equal(openssl_calls, [])
    testbase.assert_equal(result.command[1], "req")
    return


def test_request_certificate_self_signed_uses_openssl():
    """
        Default self-signed dispatch uses the openssl branch on macOS.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="dev", pem=None)

    config = MacosCertIssuanceManagerConfig()
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    spec = MacosCertificateRequestSpec(
        alias="dev",
        common_name="dev.local",
        is_self_signed=True,
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SUCCESS)
    sign_calls = [
        c for c in runner.calls
        if c["args"][0] == "openssl"
        and len(c["args"]) > 1 and c["args"][1] == "x509"
    ]
    testbase.assert_equal(len(sign_calls), 1)
    testbase.assert_equal(sign_calls[0]["args"][2], "-req")
    return


# ----------------------------------------------------------------------
# renew_certificate
# ----------------------------------------------------------------------


def test_renew_certificate_invokes_openssl():
    """
        renew_certificate dispatches openssl x509 -req against the
        existing CSR + key when the existing cert is near expiry.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=_foo_pem())
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_near_expiry_not_after(),
    )

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    result = manager.renew_certificate(alias="web")

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SUCCESS)
    sign_calls = [
        c for c in runner.calls
        if c["args"][0] == "openssl" and "x509" in c["args"]
        and "-req" in c["args"] and "-signkey" in c["args"]
    ]
    testbase.assert_equal(len(sign_calls), 1)
    return


def test_renew_certificate_skipped_when_not_near_expiry():
    """
        renew_certificate returns SKIPPED when the existing cert is
        well clear of the renew threshold.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=_foo_pem())
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
    )

    config = MacosCertIssuanceManagerConfig(renew_threshold_days=30)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    result = manager.renew_certificate(alias="web")

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SKIPPED)
    sign_calls = [
        c for c in runner.calls
        if c["args"][0] == "openssl" and "x509" in c["args"]
        and "-req" in c["args"] and "-signkey" in c["args"]
    ]
    testbase.assert_equal(sign_calls, [])
    return


# ----------------------------------------------------------------------
# revoke_certificate
# ----------------------------------------------------------------------


def test_revoke_certificate_invokes_security():
    """
        revoke_certificate dispatches security delete-certificate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=_foo_pem())
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
    )

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    result = manager.revoke_certificate(alias="web")

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SUCCESS)
    delete_calls = [
        c for c in runner.calls
        if c["args"][0] == "security" and len(c["args"]) > 1
        and c["args"][1] == "delete-certificate"
    ]
    testbase.assert_equal(len(delete_calls), 1)
    testbase.assert_equal(delete_calls[0]["args"][2], "-c")
    testbase.assert_equal(delete_calls[0]["args"][3], "web")
    testbase.assert_equal(
        delete_calls[0]["args"][4], "/Library/Keychains/System.keychain",
    )
    testbase.assert_equal(delete_calls[0]["elevated"], True)
    return


def test_revoke_certificate_with_reason():
    """
        revoke_certificate records the reason on the result detail
        even though macOS does not change argv based on it.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=_foo_pem())
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
    )

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    result = manager.revoke_certificate(
        alias="web",
        reason=MacosRevocationReason.KEY_COMPROMISE,
    )

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SUCCESS)
    testbase.assert_not_equal(result.detail, None)
    testbase.assert_equal("keycompromise" in result.detail, True)
    return


# ----------------------------------------------------------------------
# get_certificate / list_certificates
# ----------------------------------------------------------------------


def test_get_certificate_parses_x509_output():
    """
        get_certificate returns a populated MacosCertificateInfo when
        the alias is known.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=_foo_pem())
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
        sans_text="DNS:example.com, DNS:www.example.com",
    )

    config = MacosCertIssuanceManagerConfig()
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    info = manager.get_certificate(alias="web")

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.alias, "web")
    testbase.assert_equal(info.fingerprint, _foo_fp())
    testbase.assert_equal(info.serial_number, "01:23:45")
    testbase.assert_equal(info.key_algorithm, "RSA")
    testbase.assert_equal(info.key_length, 2048)
    testbase.assert_equal(
        sorted(info.sans), sorted(["example.com", "www.example.com"]),
    )
    return


def test_list_certificates_returns_all():
    """
        list_certificates dispatches security find-certificate -a -p
        and parses every PEM block.
    """
    multi_pem = "{}\n{}".format(_foo_pem(), _bar_pem())
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "security", "find-certificate", "-a", "-p",
        "/Library/Keychains/System.keychain",
    )] = RunResult(exit_code=0, stdout=multi_pem, stderr="")
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
    )

    config = MacosCertIssuanceManagerConfig()
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    infos = manager.list_certificates()

    testbase.assert_equal(len(infos), 2)
    return


# ----------------------------------------------------------------------
# export_certificate / import_certificate
# ----------------------------------------------------------------------


def test_export_certificate_writes_to_path():
    """
        export_certificate dispatches security export to the requested
        path.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=_foo_pem())
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
    )

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    result = manager.export_certificate(
        alias="web", path="/tmp/web.pem", include_private_key=False,
    )

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SUCCESS)
    export_calls = [
        c for c in runner.calls
        if c["args"][0] == "security" and len(c["args"]) > 1
        and c["args"][1] == "export"
    ]
    testbase.assert_equal(len(export_calls), 1)
    testbase.assert_equal(export_calls[0]["args"][2], "-k")
    testbase.assert_equal(
        export_calls[0]["args"][3], "/Library/Keychains/System.keychain",
    )
    testbase.assert_equal(export_calls[0]["args"][4], "-t")
    testbase.assert_equal(export_calls[0]["args"][5], "certs")
    testbase.assert_equal(export_calls[0]["args"][6], "-f")
    testbase.assert_equal(export_calls[0]["args"][7], "pemseq")
    testbase.assert_equal(export_calls[0]["args"][8], "-o")
    testbase.assert_equal(export_calls[0]["args"][9], "/tmp/web.pem")
    testbase.assert_equal(export_calls[0]["elevated"], True)
    return


def test_export_with_private_key_uses_pem():
    """
        export_certificate with include_private_key=True dispatches
        security export -t identities -f pkcs12 (the macOS native
        cert+key envelope; the corresponding "pem" form on
        Linux/macOS).
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=_foo_pem())
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
    )

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    result = manager.export_certificate(
        alias="web", path="/tmp/web.p12", include_private_key=True,
    )

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SUCCESS)
    export_calls = [
        c for c in runner.calls
        if c["args"][0] == "security" and len(c["args"]) > 1
        and c["args"][1] == "export"
    ]
    testbase.assert_equal(len(export_calls), 1)
    testbase.assert_equal(export_calls[0]["args"][4], "-t")
    testbase.assert_equal(export_calls[0]["args"][5], "identities")
    testbase.assert_equal(export_calls[0]["args"][7], "pkcs12")
    return


def test_import_certificate_skipped_when_fingerprint_present():
    """
        import_certificate returns SKIPPED when a certificate with the
        same fingerprint is already in the keychain.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/tmp/web.pem")] = RunResult(
        exit_code=0, stdout=_foo_pem(), stderr="",
    )
    runner.results[(
        "security", "find-certificate",
        "-Z", _foo_fp(), "/Library/Keychains/System.keychain",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    result = manager.import_certificate(
        path="/tmp/web.pem", alias="web",
    )

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SKIPPED)
    import_calls = [
        c for c in runner.calls
        if c["args"][0] == "security" and len(c["args"]) > 1
        and c["args"][1] == "import"
    ]
    testbase.assert_equal(import_calls, [])
    testbase.assert_equal(result.fingerprint, _foo_fp())
    return


# ----------------------------------------------------------------------
# Passphrase redaction / elevation / failure surfaces
# ----------------------------------------------------------------------


def test_passphrase_redacted_in_recorded_command():
    """
        When export_certificate is called with a passphrase, the
        passphrase flows on stdin only and the recorded command
        never contains it.  Demonstrate the redaction path by
        passing a synthetic argv through the helper directly via the
        manager's PFX export.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=_foo_pem())
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
    )

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    result = manager.export_certificate(
        alias="web",
        path="/tmp/web.p12",
        include_private_key=True,
        passphrase="super-secret",
    )

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.SUCCESS)
    # No call argv may contain the passphrase verbatim.
    for call in runner.calls:
        for token in call["args"]:
            testbase.assert_equal("super-secret" in token, False)
    # The recorded command field on the result is also free of it.
    if result.command is not None:
        for token in result.command:
            testbase.assert_equal("super-secret" in token, False)
    # The stdin path is the carrier.
    export_calls = [
        c for c in runner.calls
        if c["args"][0] == "security" and len(c["args"]) > 1
        and c["args"][1] == "export"
    ]
    testbase.assert_equal(len(export_calls), 1)
    testbase.assert_equal(export_calls[0]["stdin"], "super-secret")
    return


def test_writes_elevate():
    """
        Mutating verbs pass elevated=True when config.elevated=True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=None)

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    spec = MacosCertificateRequestSpec(
        alias="web", common_name="example.com", is_self_signed=True,
    )
    manager.request_certificate(spec=spec)

    mutating_calls = [
        c for c in runner.calls
        if c["args"][0] in ("openssl", "security")
        and c["args"][0] != "security"  # find-certificate is a read
        or (c["args"][0] == "security" and c["args"][1] == "import")
    ]
    testbase.assert_equal(len(mutating_calls) > 0, True)
    for call in mutating_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        list_certificates / get_certificate never pass elevated=True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "security", "find-certificate", "-a", "-p",
        "/Library/Keychains/System.keychain",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(
        runner=runner, pem=_foo_pem(),
        not_after=_far_future_not_after(),
    )

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    manager.list_certificates()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When openssl req exits non-zero, FAILED is returned with stderr
        / exit_code captured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_find_certificate(runner=runner, alias="web", pem=None)
    runner.results[(
        "openssl", "req",
        "-newkey", "rsa:2048",
        "-nodes",
        "-keyout", "/etc/automojo/certs/web.key",
        "-out", "/etc/automojo/certs/web.csr",
        "-subj", "/CN=example.com",
    )] = RunResult(
        exit_code=2, stdout="", stderr="bad subject",
    )

    config = MacosCertIssuanceManagerConfig(elevated=True)
    manager = MacosCertificateIssuanceManager(runner=runner, config=config)
    spec = MacosCertificateRequestSpec(
        alias="web", common_name="example.com", is_self_signed=True,
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, MacosCertIssuanceActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "bad subject")
    return


def test_query_error_exception_is_constructible():
    """
        MacosCertIssuanceQueryError is constructible and stringifies.
    """
    err = MacosCertIssuanceQueryError(
        operation="list_certificates",
        message="parser failed",
    )
    rendered = str(err)
    testbase.assert_equal("list_certificates" in rendered, True)
    testbase.assert_equal("parser failed" in rendered, True)
    return
