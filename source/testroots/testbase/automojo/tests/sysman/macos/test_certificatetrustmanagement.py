"""
    Tests for :class:`MacosCertificateTrustManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv tuple.  No live keychain or filesystem is touched; ``cat`` /
    ``security`` / ``openssl`` are all faked.
"""

import base64
import hashlib

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.certificatetrustmanagement import (
    MacosCertificateTrustManager,
    MacosCertTrustActionStatus,
    MacosCertTrustManagerConfig,
    MacosCertTrustQueryError,
)


# ----------------------------------------------------------------------
# Test helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.
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
    """
        Compute the macOS canonical uppercase no-colon SHA-256
        fingerprint.
    """
    rtnval = hashlib.sha256(der_bytes).hexdigest().upper()
    return rtnval


_DER_FOO = b"DER-FOO-BYTES-1234567890"
_DER_BAR = b"DER-BAR-BYTES-ABCDEFGHIJ"

_SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"
_USER_KEYCHAIN = "~/Library/Keychains/login.keychain-db"


def _foo_pem() -> str:
    rtnval = _make_pem(der_bytes=_DER_FOO)
    return rtnval


def _bar_pem() -> str:
    rtnval = _make_pem(der_bytes=_DER_BAR)
    return rtnval


def _foo_fp() -> str:
    rtnval = _expected_fingerprint(der_bytes=_DER_FOO)
    return rtnval


def _bar_fp() -> str:
    rtnval = _expected_fingerprint(der_bytes=_DER_BAR)
    return rtnval


def _seed_openssl_text(
    *,
    runner: _MockCommandRunner,
    pem_stdin: str,
) -> None:
    """
        Pre-seed the runner with a canned ``openssl x509 -text`` payload
        for *any* call whose stdin equals ``pem_stdin``.

        The runner's ``results`` dict is keyed by argv tuple, so we set
        the default result here in addition to a specific argv match.
    """
    canned_stdout = "\n".join([
        "subject=CN = Foo CA, O = Foo Inc",
        "issuer=CN = Foo Root, O = Foo Inc",
        "notBefore=Jan 01 00:00:00 2024 GMT",
        "notAfter=Jan 01 00:00:00 2034 GMT",
        "serial=01:23:45",
        "Certificate:",
        "    X509v3 extensions:",
        "        X509v3 Extended Key Usage:",
        "            TLS Web Server Authentication, TLS Web Client Authentication",
        "    Signature Algorithm: sha256WithRSAEncryption",
    ])
    key = (
        "openssl", "x509", "-noout", "-subject", "-issuer",
        "-startdate", "-enddate", "-serial", "-text",
    )
    runner.results[key] = RunResult(
        exit_code=0,
        stdout=canned_stdout,
        stderr="",
    )
    return


# ----------------------------------------------------------------------
# add_trusted_cert
# ----------------------------------------------------------------------


def test_add_trusted_cert_invokes_security():
    """
        add_trusted_cert dispatches ``security add-trusted-cert``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    # Existence probe -> not present
    runner.results[(
        "security", "find-certificate", "-Z", _foo_fp(), _SYSTEM_KEYCHAIN,
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = MacosCertificateTrustManager(
        runner=runner,
        config=MacosCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/foo.pem",
    )

    testbase.assert_equal(result.status, MacosCertTrustActionStatus.SUCCESS)
    testbase.assert_equal(result.fingerprint, _foo_fp())
    add_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2
        and c["args"][0] == "security"
        and c["args"][1] == "add-trusted-cert"
    ]
    testbase.assert_equal(len(add_calls), 1)
    expected_argv = [
        "security", "add-trusted-cert", "-d", "-r", "trustRoot",
        "-k", _SYSTEM_KEYCHAIN, "/tmp/foo.pem",
    ]
    testbase.assert_equal(add_calls[0]["args"], expected_argv)
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_add_trusted_cert_skipped_when_fingerprint_present():
    """
        add_trusted_cert returns SKIPPED when the keychain already
        contains a cert with the same fingerprint.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    # find-certificate probe -> present (exit 0)
    runner.results[(
        "security", "find-certificate", "-Z", _foo_fp(), _SYSTEM_KEYCHAIN,
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")

    manager = MacosCertificateTrustManager(
        runner=runner,
        config=MacosCertTrustManagerConfig(),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/foo.pem",
    )

    testbase.assert_equal(result.status, MacosCertTrustActionStatus.SKIPPED)
    add_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2
        and c["args"][0] == "security"
        and c["args"][1] == "add-trusted-cert"
    ]
    testbase.assert_equal(add_calls, [])
    return


def test_add_trusted_cert_dry_run_short_circuits():
    """
        add_trusted_cert with dry_run=True returns DRY_RUN and does NOT
        invoke security add-trusted-cert.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "security", "find-certificate", "-Z", _foo_fp(), _SYSTEM_KEYCHAIN,
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = MacosCertificateTrustManager(
        runner=runner,
        config=MacosCertTrustManagerConfig(dry_run=True, elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/foo.pem",
    )

    testbase.assert_equal(result.status, MacosCertTrustActionStatus.DRY_RUN)
    add_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2
        and c["args"][0] == "security"
        and c["args"][1] == "add-trusted-cert"
    ]
    testbase.assert_equal(add_calls, [])
    return


def test_add_trusted_cert_invalid_pem_returns_failed():
    """
        Invalid PEM input returns FAILED without invoking security.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/tmp/garbage.pem",
    )] = RunResult(exit_code=0, stdout="not a cert", stderr="")

    manager = MacosCertificateTrustManager(
        runner=runner, config=MacosCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/garbage.pem",
    )

    testbase.assert_equal(result.status, MacosCertTrustActionStatus.FAILED)
    add_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2
        and c["args"][0] == "security"
        and c["args"][1] == "add-trusted-cert"
    ]
    testbase.assert_equal(add_calls, [])
    return


# ----------------------------------------------------------------------
# remove_trusted_cert
# ----------------------------------------------------------------------


def test_remove_trusted_cert_invokes_security():
    """
        remove_trusted_cert dispatches ``security delete-certificate``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "security", "find-certificate", "-Z", _foo_fp(), _SYSTEM_KEYCHAIN,
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")

    manager = MacosCertificateTrustManager(
        runner=runner,
        config=MacosCertTrustManagerConfig(elevated=True),
    )
    result = manager.remove_trusted_cert(
        store="system-ca", fingerprint=_foo_fp(),
    )

    testbase.assert_equal(result.status, MacosCertTrustActionStatus.SUCCESS)
    delete_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2
        and c["args"][0] == "security"
        and c["args"][1] == "delete-certificate"
    ]
    testbase.assert_equal(len(delete_calls), 1)
    expected_argv = [
        "security", "delete-certificate", "-Z", _foo_fp(), _SYSTEM_KEYCHAIN,
    ]
    testbase.assert_equal(delete_calls[0]["args"], expected_argv)
    testbase.assert_equal(delete_calls[0]["elevated"], True)
    return


def test_remove_trusted_cert_skipped_when_absent():
    """
        remove_trusted_cert returns SKIPPED when no cert with the
        requested fingerprint exists in the keychain.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=1, stdout="", stderr=""),
    )

    manager = MacosCertificateTrustManager(
        runner=runner, config=MacosCertTrustManagerConfig(),
    )
    result = manager.remove_trusted_cert(
        store="system-ca", fingerprint=_foo_fp(),
    )

    testbase.assert_equal(result.status, MacosCertTrustActionStatus.SKIPPED)
    delete_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2
        and c["args"][0] == "security"
        and c["args"][1] == "delete-certificate"
    ]
    testbase.assert_equal(delete_calls, [])
    return


# ----------------------------------------------------------------------
# list_trusted_certs / get_cert_details
# ----------------------------------------------------------------------


def test_list_trusted_certs_parses_output():
    """
        list_trusted_certs parses the multi-PEM stream from
        ``security find-certificate -a -p``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    multi_pem = _foo_pem() + _bar_pem()
    runner.results[(
        "security", "find-certificate", "-a", "-p", _SYSTEM_KEYCHAIN,
    )] = RunResult(exit_code=0, stdout=multi_pem, stderr="")
    _seed_openssl_text(runner=runner, pem_stdin=_foo_pem())

    manager = MacosCertificateTrustManager(
        runner=runner, config=MacosCertTrustManagerConfig(),
    )
    info_list = manager.list_trusted_certs(store="system-ca")

    testbase.assert_equal(len(info_list), 2)
    fingerprints = sorted([info.fingerprint for info in info_list])
    expected = sorted([_foo_fp(), _bar_fp()])
    testbase.assert_equal(fingerprints, expected)
    testbase.assert_equal(info_list[0].subject, "CN = Foo CA, O = Foo Inc")
    testbase.assert_equal(
        info_list[0].purposes,
        ["TLS Web Server Authentication", "TLS Web Client Authentication"],
    )
    return


def test_get_cert_details_returns_full_info():
    """
        get_cert_details returns a populated MacosCertTrustInfo when
        the fingerprint is found.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=1, stdout="", stderr=""),
    )
    runner.results[(
        "security", "find-certificate", "-Z", _foo_fp(),
        "-p", _SYSTEM_KEYCHAIN,
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(runner=runner, pem_stdin=_foo_pem())

    manager = MacosCertificateTrustManager(
        runner=runner, config=MacosCertTrustManagerConfig(),
    )
    info = manager.get_cert_details(fingerprint=_foo_fp())

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.fingerprint, _foo_fp())
    testbase.assert_equal(info.serial_number, "01:23:45")
    testbase.assert_equal(info.store, "system-ca")
    return


def test_get_cert_details_unknown_fingerprint_returns_none():
    """
        get_cert_details returns None when no keychain contains the
        fingerprint.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=1, stdout="", stderr=""),
    )

    manager = MacosCertificateTrustManager(
        runner=runner, config=MacosCertTrustManagerConfig(),
    )
    info = manager.get_cert_details(fingerprint=_foo_fp())

    testbase.assert_equal(info, None)
    return


# ----------------------------------------------------------------------
# update_trust_store
# ----------------------------------------------------------------------


def test_update_trust_store_returns_success_noop():
    """
        update_trust_store is a SUCCESS no-op on macOS — the runner is
        NOT invoked.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = MacosCertificateTrustManager(
        runner=runner,
        config=MacosCertTrustManagerConfig(elevated=True),
    )
    result = manager.update_trust_store()

    testbase.assert_equal(result.status, MacosCertTrustActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Store routing / elevation / errors
# ----------------------------------------------------------------------


def test_unsupported_store_returns_failed():
    """
        An unknown store name returns FAILED without dispatching.
    """
    runner = _MockCommandRunner()
    manager = MacosCertificateTrustManager(
        runner=runner, config=MacosCertTrustManagerConfig(),
    )
    result = manager.add_trusted_cert(
        store="bogus", path="/tmp/foo.pem",
    )
    testbase.assert_equal(result.status, MacosCertTrustActionStatus.FAILED)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_unsupported_store_in_list_raises():
    """
        list_trusted_certs raises for an unknown store name.
    """
    runner = _MockCommandRunner()
    manager = MacosCertificateTrustManager(
        runner=runner, config=MacosCertTrustManagerConfig(),
    )
    raised = False
    try:
        manager.list_trusted_certs(store="bogus")
    except MacosCertTrustQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_writes_pass_elevated_true_for_system_store():
    """
        With elevated=True, system-ca writes pass elevated=True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "security", "find-certificate", "-Z", _foo_fp(), _SYSTEM_KEYCHAIN,
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = MacosCertificateTrustManager(
        runner=runner,
        config=MacosCertTrustManagerConfig(elevated=True),
    )
    manager.add_trusted_cert(store="system-ca", path="/tmp/foo.pem")

    add_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2
        and c["args"][0] == "security"
        and c["args"][1] == "add-trusted-cert"
    ]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_user_store_writes_do_not_elevate():
    """
        Even with elevated=True in the config, writes against user-ca
        pass elevated=False (current-user keychain).
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "security", "find-certificate", "-Z", _foo_fp(), _USER_KEYCHAIN,
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = MacosCertificateTrustManager(
        runner=runner,
        config=MacosCertTrustManagerConfig(elevated=True),
    )
    manager.add_trusted_cert(store="user-ca", path="/tmp/foo.pem")

    add_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2
        and c["args"][0] == "security"
        and c["args"][1] == "add-trusted-cert"
    ]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_equal(add_calls[0]["elevated"], False)
    return


def test_reads_never_elevate():
    """
        list_trusted_certs always passes elevated=False, regardless of
        config.elevated.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "security", "find-certificate", "-a", "-p", _SYSTEM_KEYCHAIN,
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(runner=runner, pem_stdin=_foo_pem())

    manager = MacosCertificateTrustManager(
        runner=runner,
        config=MacosCertTrustManagerConfig(elevated=True),
    )
    manager.list_trusted_certs(store="system-ca")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        Non-zero security exit codes surface as FAILED with stderr
        captured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "security", "find-certificate", "-Z", _foo_fp(), _SYSTEM_KEYCHAIN,
    )] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[(
        "security", "add-trusted-cert", "-d", "-r", "trustRoot",
        "-k", _SYSTEM_KEYCHAIN, "/tmp/foo.pem",
    )] = RunResult(
        exit_code=1, stdout="", stderr="user authentication failed",
    )

    manager = MacosCertificateTrustManager(
        runner=runner,
        config=MacosCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/foo.pem",
    )

    testbase.assert_equal(result.status, MacosCertTrustActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "user authentication failed")
    return
