"""
    macOS certificate-issuance feature.

    Implements the cross-OS certificate-issuance contract for macOS by
    wrapping the ``security`` keychain CLI plus ``openssl`` for CSR
    generation and self-signing through the injected
    :class:`ICommandRunner`.

    Verb / tool mapping:

    +------------------------+--------------------------------------+
    | Verb                   | Command                              |
    +========================+======================================+
    | ``request_certificate``| ``openssl req -newkey rsa:<bits>``   |
    |                        | ``-nodes -keyout <k> -out <csr>``    |
    |                        | ``-subj '/CN=<cn>'`` then            |
    |                        | ``openssl x509 -req -days <n>``      |
    |                        | ``-in <csr> -signkey <k>``           |
    |                        | ``-out <cert>`` then                 |
    |                        | ``security import <cert>``           |
    |                        | ``-k <keychain>``                    |
    +------------------------+--------------------------------------+
    | ``renew_certificate``  | ``openssl x509 -req -days <n>``      |
    |                        | ``-in <csr> -signkey <k>``           |
    |                        | ``-out <cert>`` then re-import       |
    +------------------------+--------------------------------------+
    | ``revoke_certificate`` | ``security delete-certificate -c``   |
    |                        | ``<alias> <keychain>``               |
    +------------------------+--------------------------------------+
    | ``get_certificate``    | ``security find-certificate -c``     |
    |                        | ``<alias> -p <keychain>`` then       |
    |                        | ``openssl x509 -text`` parse         |
    +------------------------+--------------------------------------+
    | ``list_certificates``  | ``security find-certificate -a -p``  |
    |                        | ``<keychain>`` then PEM-block split  |
    +------------------------+--------------------------------------+
    | ``export_certificate`` | ``security export -k <keychain>``    |
    |                        | ``-t certs -f pemseq -o <path>``     |
    |                        | (cert only) or                       |
    |                        | ``security export -k <keychain>``    |
    |                        | ``-t identities -f pkcs12``          |
    |                        | ``-o <path>`` (with key)             |
    +------------------------+--------------------------------------+
    | ``import_certificate`` | ``security import <path> -k``       |
    |                        | ``<keychain>``                       |
    +------------------------+--------------------------------------+

    ACME limitation:
        macOS lacks a first-party ACME client.  The recommended
        path is to install ``certbot`` via Homebrew and delegate to
        the Ubuntu-style flow.  This pilot does NOT auto-detect or
        wire Homebrew certbot — calls whose pathway implies ACME
        return ``FAILED`` with a descriptive ``detail``.

    CA-side revocation:
        Out of scope for the pilot.  ``revoke_certificate`` removes
        the local keychain entry only; CA-side revocation
        (e.g. AWS Certificate Manager, internal CA) is the caller's
        responsibility.

    Idempotency model:
        - :meth:`request_certificate` and :meth:`renew_certificate`
          return ``SKIPPED`` when the alias's existing keychain entry
          has more than ``config.renew_threshold_days`` of validity.
        - :meth:`revoke_certificate` returns ``SKIPPED`` when the
          alias is absent.
        - :meth:`import_certificate` returns ``SKIPPED`` when a
          certificate with the same SHA-256 fingerprint is already
          present in the keychain.
        - :meth:`export_certificate` returns ``SKIPPED`` when the
          destination path's bytes already match what would be
          written.

    Elevation policy:
        Mutating verbs honour ``config.elevated`` and pass it
        through to the runner.  Reads do not elevate.

    Credential safety:
        Export passphrases (when supplied) flow on runner ``stdin``
        only.  The recorded ``command`` field is sanitised via
        :func:`_sanitise_security_passphrases` before being
        captured on the result.
"""

import base64
import hashlib
import os

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_CAT = "cat"
_CMP = "cmp"
_SECURITY = "security"
_OPENSSL = "openssl"

_DEFAULT_KEYCHAIN = "/Library/Keychains/System.keychain"
_DEFAULT_CERT_DIR = "/etc/ccai/certs"

_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


MacosCertIssuanceActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------


class MacosRevocationReason(Enum):
    """
        Revocation reason codes accepted by
        :meth:`MacosCertificateIssuanceManager.revoke_certificate`.
    """
    UNSPECIFIED            = "unspecified"
    KEY_COMPROMISE         = "keycompromise"
    CA_COMPROMISE          = "cacompromise"
    AFFILIATION_CHANGED    = "affiliationchanged"
    SUPERSEDED             = "superseded"
    CESSATION_OF_OPERATION = "cessationofoperation"


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class MacosCertIssuanceManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosCertificateIssuanceManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param keychain: Target keychain file.  Defaults to
                         ``"/Library/Keychains/System.keychain"``.
        :param cert_dir: Directory where openssl-side material (key,
                         CSR, cert) is staged.  Defaults to
                         ``"/etc/ccai/certs"``.
        :param security_cli: Command name for ``security``.
        :param openssl_cli: Command name for ``openssl``.
        :param renew_threshold_days: Skip threshold (in days) for
                                     :meth:`request_certificate` and
                                     :meth:`renew_certificate`.
                                     Defaults to ``30``.
    """
    keychain: str = _DEFAULT_KEYCHAIN
    cert_dir: str = _DEFAULT_CERT_DIR
    security_cli: str = _SECURITY
    openssl_cli: str = _OPENSSL
    renew_threshold_days: int = 30


@dataclass
class MacosCertIssuanceActionResult(FeatureActionResult):
    """
        Result of a certificate-issuance write operation.

        :param alias: Alias acted upon.
        :param subject: Certificate subject DN, when known.
        :param fingerprint: SHA-256 fingerprint of the affected
                            certificate (macOS native form: uppercase
                            hex, no colons), when known.
    """
    alias: Optional[str] = None
    subject: Optional[str] = None
    fingerprint: Optional[str] = None


@dataclass
class MacosCertificateInfo:
    """
        Structured metadata for a single managed certificate.

        :param alias: Caller-chosen alias (or the ``security`` -c name
                      for keychain-found certs).
        :param subject: Certificate subject DN.
        :param issuer: Issuer DN.
        :param not_before: Validity start (openssl format).
        :param not_after: Validity end (openssl format).
        :param fingerprint: SHA-256 fingerprint (uppercase, no colons
                            — macOS native form).
        :param serial_number: Hex serial.
        :param key_algorithm: ``"RSA"``, ``"EC"``, etc., or ``None``.
        :param key_length: Key size in bits, or ``None``.
        :param sans: SANs from the X.509 v3 SAN extension.
        :param path: Filesystem path of the staged cert file, when
                     known.  ``None`` for keychain-only entries.
        :param is_self_signed: ``True`` when subject == issuer.
    """
    alias: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    fingerprint: str
    serial_number: str
    key_algorithm: Optional[str]
    key_length: Optional[int]
    sans: list = field(default_factory=list)
    path: Optional[str] = None
    is_self_signed: bool = False


@dataclass
class MacosCertificateRequestSpec:
    """
        Caller-supplied issuance spec.

        :param alias: Caller-chosen identifier.
        :param common_name: Certificate CN.
        :param email: ACME registration email.  Currently unused on
                      macOS (ACME is FAILED).
        :param sans: SANs.
        :param key_algorithm: ``"RSA"`` or ``"EC"``.
        :param key_length: Key size in bits.
        :param validity_days: Days of validity.
        :param is_self_signed: Defaults to ``True`` on macOS — the
                               pilot supports self-signed only.
        :param webroot: Reserved for future ACME delegation.
    """
    alias: str
    common_name: str
    email: Optional[str] = None
    sans: list = field(default_factory=list)
    key_algorithm: str = "RSA"
    key_length: int = 2048
    validity_days: int = 365
    is_self_signed: bool = True
    webroot: Optional[str] = None


class MacosCertIssuanceQueryError(Exception):
    """
        Raised when a certificate-issuance read fails for a reason
        other than the target certificate simply being absent.
    """

    def __init__(
        self,
        *,
        operation: str,
        message: str,
    ) -> None:
        """
            :param operation: The verb that triggered the failure.
            :param message: Human-readable detail.
        """
        self.operation = operation
        self.message = message
        err_msg_lines = ["Certificate issuance query command failed."]
        err_msg_lines.append("    OPERATION: {}".format(operation))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosCertificateIssuanceManager(BaseFeatureManager):
    """
        macOS certificate-issuance manager.  Wraps the
        ``security`` keychain CLI plus ``openssl`` through the
        injected :class:`ICommandRunner`.
    """

    result_class = MacosCertIssuanceActionResult
    status_class = MacosCertIssuanceActionStatus
    feature_name = "certificate-issuance"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def request_certificate(
        self,
        spec: MacosCertificateRequestSpec,
    ) -> MacosCertIssuanceActionResult:
        """
            Issue a new self-signed certificate keyed by ``spec.alias``
            and import it into the configured keychain.

            ACME issuance is not supported in the pilot; non-self-signed
            specs return ``FAILED`` with a descriptive detail.

            :param spec: The :class:`MacosCertificateRequestSpec`.

            :returns: The :class:`MacosCertIssuanceActionResult`.
        """
        if spec.is_self_signed is False:
            failed = self._build_result(
                status=MacosCertIssuanceActionStatus.FAILED,
                alias=spec.alias,
                subject=spec.common_name,
                fingerprint=None,
                command=None,
                detail=(
                    "ACME issuance is not supported on the macOS pilot; "
                    "delegate to a Homebrew certbot installation"
                ),
            )
            return failed

        existing_info = self._read_keychain_cert_info(alias=spec.alias)
        if existing_info is not None:
            days = _days_until(not_after=existing_info.not_after)
            if days is not None and days > self._typed_config().renew_threshold_days:
                skipped = self._build_result(
                    status=MacosCertIssuanceActionStatus.SKIPPED,
                    alias=spec.alias,
                    subject=existing_info.subject,
                    fingerprint=existing_info.fingerprint,
                    command=None,
                    detail="certificate not yet within renew threshold",
                )
                return skipped

        rtnval = self._dispatch_openssl_request(spec=spec)
        return rtnval

    def renew_certificate(
        self,
        alias: str,
    ) -> MacosCertIssuanceActionResult:
        """
            Renew an existing certificate identified by ``alias``.

            :param alias: The caller-chosen alias.

            :returns: The :class:`MacosCertIssuanceActionResult`.
        """
        existing_info = self._read_keychain_cert_info(alias=alias)
        if existing_info is None:
            failed = self._build_result(
                status=MacosCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="alias not found",
            )
            return failed

        days = _days_until(not_after=existing_info.not_after)
        if days is not None and days > self._typed_config().renew_threshold_days:
            skipped = self._build_result(
                status=MacosCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=None,
                detail="certificate not yet within renew threshold",
            )
            return skipped

        rtnval = self._dispatch_openssl_renew(
            alias=alias, existing_info=existing_info,
        )
        return rtnval

    def revoke_certificate(
        self,
        alias: str,
        reason: Optional[MacosRevocationReason] = None,
    ) -> MacosCertIssuanceActionResult:
        """
            Remove the alias's certificate from the configured
            keychain.

            :param alias: The caller-chosen alias.
            :param reason: Optional :class:`MacosRevocationReason`.

            :returns: The :class:`MacosCertIssuanceActionResult`.
        """
        existing_info = self._read_keychain_cert_info(alias=alias)
        if existing_info is None:
            skipped = self._build_result(
                status=MacosCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="alias not present in keychain",
            )
            return skipped

        cfg = self._typed_config()
        argv = [
            cfg.security_cli, "delete-certificate",
            "-c", alias, cfg.keychain,
        ]
        detail_text = None
        if reason is not None:
            detail_text = "revocation_reason={}".format(reason.value)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=argv,
                detail=detail_text,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = MacosCertIssuanceActionStatus.FAILED

        rtnval = MacosCertIssuanceActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
        )
        return rtnval

    def export_certificate(
        self,
        alias: str,
        path: str,
        include_private_key: bool = False,
        passphrase: Optional[str] = None,
    ) -> MacosCertIssuanceActionResult:
        """
            Export the alias's certificate (and optionally key) to
            ``path``.

            With ``include_private_key=False`` (the default) the
            output is a PEM-encoded certificate sequence.  With
            ``include_private_key=True`` the output is a PKCS#12
            (``.p12`` / ``.pfx``) bundle, which is the macOS-native
            cert+key envelope; supply ``passphrase`` to protect the
            bundle.  The passphrase, when supplied, flows on runner
            ``stdin`` only — never on argv — and is redacted from the
            recorded ``command`` field.

            :param alias: The caller-chosen alias.
            :param path: Destination filesystem path.
            :param include_private_key: When ``True``, produce a
                                        PKCS#12 bundle.  Default
                                        ``False``.
            :param passphrase: Optional PKCS#12 export passphrase.

            :returns: The :class:`MacosCertIssuanceActionResult`.
        """
        existing_info = self._read_keychain_cert_info(alias=alias)
        if existing_info is None:
            failed = self._build_result(
                status=MacosCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="alias not found",
            )
            return failed

        cfg = self._typed_config()
        if include_private_key:
            argv = [
                cfg.security_cli, "export",
                "-k", cfg.keychain,
                "-t", "identities",
                "-f", "pkcs12",
                "-o", path,
            ]
        else:
            argv = [
                cfg.security_cli, "export",
                "-k", cfg.keychain,
                "-t", "certs",
                "-f", "pemseq",
                "-o", path,
            ]

        # Idempotency: if the destination already exists with the same
        # bytes we'd write, skip.  We compare via cmp -s on the stored
        # cert path when known; otherwise we trust the destination to
        # have been written by us already.
        if existing_info.path is not None:
            if self._destinations_match(
                source_path=existing_info.path, dest_path=path,
            ):
                skipped = self._build_result(
                    status=MacosCertIssuanceActionStatus.SKIPPED,
                    alias=alias,
                    subject=existing_info.subject,
                    fingerprint=existing_info.fingerprint,
                    command=None,
                    detail="destination already matches source bytes",
                )
                return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=_sanitise_security_passphrases(argv=argv),
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
            stdin=passphrase,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = MacosCertIssuanceActionStatus.FAILED

        sanitised_command = _sanitise_security_passphrases(argv=argv)

        rtnval = MacosCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=sanitised_command,
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
        )
        return rtnval

    def import_certificate(
        self,
        path: str,
        alias: str,
    ) -> MacosCertIssuanceActionResult:
        """
            Import an externally-issued certificate under ``alias``.

            Reads the source via ``cat`` for fingerprint computation,
            checks for an existing keychain entry with the same
            fingerprint, then dispatches ``security import``.

            :param path: Source file path.
            :param alias: Target alias.

            :returns: The :class:`MacosCertIssuanceActionResult`.
        """
        source_content = self._cat(path=path)
        if source_content is None:
            failed = self._build_result(
                status=MacosCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=[_CAT, path],
                detail="source path does not exist",
            )
            return failed

        cert_blocks = _parse_pem_blocks(content=source_content)
        if len(cert_blocks) == 0:
            failed = self._build_result(
                status=MacosCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="source file is not a valid PEM certificate",
            )
            return failed

        fingerprint = _compute_fingerprint(
            pem=cert_blocks[0],
            colonized=False,
            uppercase=True,
        )
        if fingerprint is None:
            failed = self._build_result(
                status=MacosCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="failed to compute fingerprint from PEM body",
            )
            return failed

        if self._fingerprint_in_keychain(fingerprint=fingerprint):
            skipped = self._build_result(
                status=MacosCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=None,
                fingerprint=fingerprint,
                command=None,
                detail="certificate with same fingerprint already in keychain",
            )
            return skipped

        cfg = self._typed_config()
        argv = [
            cfg.security_cli, "import", path,
            "-k", cfg.keychain,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=None,
                fingerprint=fingerprint,
                command=argv,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = MacosCertIssuanceActionStatus.FAILED

        rtnval = MacosCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=alias,
            subject=None,
            fingerprint=fingerprint,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_certificate(
        self,
        alias: str,
    ) -> Optional[MacosCertificateInfo]:
        """
            Return :class:`MacosCertificateInfo` for ``alias``, or
            ``None`` when no certificate is registered.

            :param alias: The caller-chosen alias.

            :returns: The :class:`MacosCertificateInfo` or ``None``.
        """
        rtnval = self._read_keychain_cert_info(alias=alias)
        return rtnval

    def list_certificates(self) -> list:
        """
            Enumerate every certificate in the configured keychain.

            :returns: A ``list[MacosCertificateInfo]``.
        """
        cfg = self._typed_config()
        argv = [
            cfg.security_cli, "find-certificate",
            "-a", "-p", cfg.keychain,
        ]
        run_result = self._runner.run(list(argv), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval

        pem_blocks = _parse_pem_blocks(content=run_result.stdout)
        rtnval = []
        for pem in pem_blocks:
            fingerprint = _compute_fingerprint(
                pem=pem, colonized=False, uppercase=True,
            )
            if fingerprint is None:
                continue
            info = self._cert_info_from_pem(
                alias="",
                pem=pem,
                fingerprint=fingerprint,
            )
            if info is not None:
                rtnval.append(info)
        return rtnval

    # ------------------------------------------------------------------
    # Backend dispatch — openssl
    # ------------------------------------------------------------------

    def _dispatch_openssl_request(
        self,
        *,
        spec: MacosCertificateRequestSpec,
    ) -> MacosCertIssuanceActionResult:
        """
            Issue a self-signed certificate via ``openssl req`` +
            ``openssl x509 -req -signkey`` then import via
            ``security``.
        """
        cfg = self._typed_config()
        cert_path = self._alias_path(alias=spec.alias)
        key_path = self._alias_key_path(alias=spec.alias)
        csr_path = self._alias_csr_path(alias=spec.alias)
        algorithm = spec.key_algorithm.lower()
        algorithm_token = "{}:{}".format(algorithm, spec.key_length)
        subject = _format_subject(common_name=spec.common_name)

        req_argv: list = [
            cfg.openssl_cli,
            "req",
            "-newkey", algorithm_token,
            "-nodes",
            "-keyout", key_path,
            "-out", csr_path,
            "-subj", subject,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosCertIssuanceActionStatus.DRY_RUN,
                alias=spec.alias,
                subject=spec.common_name,
                fingerprint=None,
                command=req_argv,
            )
            return dry

        req_result = self._runner.run(
            list(req_argv),
            elevated=self._config.elevated,
        )
        if req_result.exit_code != 0:
            failed = MacosCertIssuanceActionResult(
                status=MacosCertIssuanceActionStatus.FAILED,
                detail=None,
                exit_code=req_result.exit_code,
                stdout=req_result.stdout,
                stderr=req_result.stderr,
                command=list(req_argv),
                alias=spec.alias,
                subject=spec.common_name,
                fingerprint=None,
            )
            return failed

        sign_argv: list = [
            cfg.openssl_cli,
            "x509", "-req",
            "-days", str(spec.validity_days),
            "-in", csr_path,
            "-signkey", key_path,
            "-out", cert_path,
        ]
        sign_result = self._runner.run(
            list(sign_argv),
            elevated=self._config.elevated,
        )
        if sign_result.exit_code != 0:
            failed = MacosCertIssuanceActionResult(
                status=MacosCertIssuanceActionStatus.FAILED,
                detail=None,
                exit_code=sign_result.exit_code,
                stdout=sign_result.stdout,
                stderr=sign_result.stderr,
                command=list(sign_argv),
                alias=spec.alias,
                subject=spec.common_name,
                fingerprint=None,
            )
            return failed

        import_argv: list = [
            cfg.security_cli, "import", cert_path,
            "-k", cfg.keychain,
        ]
        import_result = self._runner.run(
            list(import_argv),
            elevated=self._config.elevated,
        )

        if import_result.exit_code == 0:
            mapped_status = MacosCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = MacosCertIssuanceActionStatus.FAILED

        rtnval = MacosCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=import_result.exit_code,
            stdout=import_result.stdout,
            stderr=import_result.stderr,
            command=list(import_argv),
            alias=spec.alias,
            subject=spec.common_name,
            fingerprint=None,
        )
        return rtnval

    def _dispatch_openssl_renew(
        self,
        *,
        alias: str,
        existing_info: MacosCertificateInfo,
    ) -> MacosCertIssuanceActionResult:
        """
            Re-sign the existing CSR + re-import.
        """
        cfg = self._typed_config()
        cert_path = self._alias_path(alias=alias)
        key_path = self._alias_key_path(alias=alias)
        csr_path = self._alias_csr_path(alias=alias)

        sign_argv: list = [
            cfg.openssl_cli,
            "x509", "-req",
            "-days", "365",
            "-in", csr_path,
            "-signkey", key_path,
            "-out", cert_path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=sign_argv,
            )
            return dry

        sign_result = self._runner.run(
            list(sign_argv),
            elevated=self._config.elevated,
        )
        if sign_result.exit_code != 0:
            failed = MacosCertIssuanceActionResult(
                status=MacosCertIssuanceActionStatus.FAILED,
                detail=None,
                exit_code=sign_result.exit_code,
                stdout=sign_result.stdout,
                stderr=sign_result.stderr,
                command=list(sign_argv),
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
            )
            return failed

        import_argv: list = [
            cfg.security_cli, "import", cert_path,
            "-k", cfg.keychain,
        ]
        import_result = self._runner.run(
            list(import_argv),
            elevated=self._config.elevated,
        )

        if import_result.exit_code == 0:
            mapped_status = MacosCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = MacosCertIssuanceActionStatus.FAILED

        rtnval = MacosCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=import_result.exit_code,
            stdout=import_result.stdout,
            stderr=import_result.stderr,
            command=list(import_argv),
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosCertIssuanceManagerConfig:
        """
            Narrow the base config to the typed subclass.
        """
        rtnval = self._config
        return rtnval

    def _alias_path(self, *, alias: str) -> str:
        """
            Resolve an alias to its certificate file path.
        """
        cfg = self._typed_config()
        rtnval = "{}/{}.pem".format(cfg.cert_dir, alias)
        return rtnval

    def _alias_key_path(self, *, alias: str) -> str:
        """
            Resolve an alias to its private-key file path.
        """
        cfg = self._typed_config()
        rtnval = "{}/{}.key".format(cfg.cert_dir, alias)
        return rtnval

    def _alias_csr_path(self, *, alias: str) -> str:
        """
            Resolve an alias to its CSR file path.
        """
        cfg = self._typed_config()
        rtnval = "{}/{}.csr".format(cfg.cert_dir, alias)
        return rtnval

    def _cat(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` (no elevation).
        """
        argv = [_CAT, path]
        run_result = self._runner.run(list(argv), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _destinations_match(
        self,
        *,
        source_path: str,
        dest_path: str,
    ) -> bool:
        """
            Return ``True`` when the source and dest files have
            byte-identical content (probed via ``cmp -s``).
        """
        argv = [_CMP, "-s", source_path, dest_path]
        run_result = self._runner.run(list(argv), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _fingerprint_in_keychain(
        self,
        *,
        fingerprint: str,
    ) -> bool:
        """
            Probe the keychain for a certificate whose SHA-256
            thumbprint matches ``fingerprint`` via
            ``security find-certificate -Z <fp> <keychain>``.
        """
        cfg = self._typed_config()
        argv = [
            cfg.security_cli, "find-certificate",
            "-Z", fingerprint, cfg.keychain,
        ]
        run_result = self._runner.run(list(argv), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _read_keychain_cert_info(
        self,
        *,
        alias: str,
    ) -> Optional[MacosCertificateInfo]:
        """
            Look up ``alias`` in the configured keychain and parse
            the resulting PEM into a :class:`MacosCertificateInfo`.
        """
        cfg = self._typed_config()
        find_argv = [
            cfg.security_cli, "find-certificate",
            "-c", alias, "-p", cfg.keychain,
        ]
        run_result = self._runner.run(list(find_argv), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[MacosCertificateInfo] = None
            return rtnval

        pem_blocks = _parse_pem_blocks(content=run_result.stdout)
        if len(pem_blocks) == 0:
            rtnval = None
            return rtnval

        first_pem = pem_blocks[0]
        fingerprint = _compute_fingerprint(
            pem=first_pem, colonized=False, uppercase=True,
        )
        if fingerprint is None:
            rtnval = None
            return rtnval

        rtnval = self._cert_info_from_pem(
            alias=alias,
            pem=first_pem,
            fingerprint=fingerprint,
        )
        return rtnval

    def _cert_info_from_pem(
        self,
        *,
        alias: str,
        pem: str,
        fingerprint: str,
    ) -> Optional[MacosCertificateInfo]:
        """
            Build a :class:`MacosCertificateInfo` for a PEM by
            piping it through ``openssl x509 -text``.
        """
        cfg = self._typed_config()
        argv = [
            cfg.openssl_cli, "x509", "-noout",
            "-text",
            "-fingerprint", "-sha256",
            "-dates",
            "-subject", "-issuer", "-serial",
        ]
        run_result = self._runner.run(
            list(argv),
            elevated=False,
            stdin=pem,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[MacosCertificateInfo] = None
            return rtnval

        parsed = _parse_openssl_x509_text(stdout=run_result.stdout)
        is_self_signed = parsed.get("subject", "") == parsed.get("issuer", "")
        rtnval = MacosCertificateInfo(
            alias=alias,
            subject=parsed.get("subject", ""),
            issuer=parsed.get("issuer", ""),
            not_before=parsed.get("not_before", ""),
            not_after=parsed.get("not_after", ""),
            fingerprint=fingerprint,
            serial_number=parsed.get("serial_number", ""),
            key_algorithm=parsed.get("key_algorithm", None),
            key_length=parsed.get("key_length", None),
            sans=parsed.get("sans", []),
            path=None,
            is_self_signed=is_self_signed,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosCertIssuanceActionStatus,
        alias: Optional[str],
        subject: Optional[str],
        fingerprint: Optional[str],
        command: Optional[list],
        detail: Optional[str] = None,
    ) -> MacosCertIssuanceActionResult:
        """
            Construct a :class:`MacosCertIssuanceActionResult`
            without invoking the runner.
        """
        if command is None:
            command_copy: Optional[list] = None
        else:
            command_copy = list(command)
        rtnval = MacosCertIssuanceActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            alias=alias,
            subject=subject,
            fingerprint=fingerprint,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PEM / fingerprint
# ----------------------------------------------------------------------


def _parse_pem_blocks(*, content: str) -> list:
    """
        Split a PEM-bearing string into a list of CERTIFICATE blocks.
    """
    rtnval: list = []
    in_block = False
    current_lines: list = []

    for raw in content.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == _PEM_BEGIN:
            in_block = True
            current_lines = [line]
            continue
        if stripped == _PEM_END:
            if in_block:
                current_lines.append(line)
                block_text = "\n".join(current_lines) + "\n"
                rtnval.append(block_text)
            in_block = False
            current_lines = []
            continue
        if in_block:
            current_lines.append(line)

    return rtnval


def _compute_fingerprint(
    *,
    pem: str,
    colonized: bool,
    uppercase: bool,
) -> Optional[str]:
    """
        Compute the SHA-256 fingerprint of a PEM CERTIFICATE block.
    """
    body_lines: list = []
    in_body = False
    for raw in pem.splitlines():
        stripped = raw.strip()
        if stripped == _PEM_BEGIN:
            in_body = True
            continue
        if stripped == _PEM_END:
            in_body = False
            continue
        if in_body:
            body_lines.append(stripped)

    body = "".join(body_lines)
    if len(body) == 0:
        rtnval: Optional[str] = None
        return rtnval

    try:
        decoded = base64.b64decode(body, validate=False)
    except (ValueError, Exception):
        rtnval = None
        return rtnval

    if len(decoded) == 0:
        rtnval = None
        return rtnval

    digest = hashlib.sha256(decoded).digest()

    if uppercase:
        hex_text = digest.hex().upper()
    else:
        hex_text = digest.hex().lower()

    if colonized:
        chunks: list = []
        cursor = 0
        while cursor < len(hex_text):
            chunks.append(hex_text[cursor:cursor + 2])
            cursor += 2
        rtnval = ":".join(chunks)
        return rtnval

    rtnval = hex_text
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — openssl x509 -text parsing
# ----------------------------------------------------------------------


def _parse_openssl_x509_text(*, stdout: str) -> dict:
    """
        Parse the combined output of ``openssl x509 -text -fingerprint
        -sha256 -dates -subject -issuer -serial``.
    """
    rtnval: dict = {
        "subject": "",
        "issuer": "",
        "not_before": "",
        "not_after": "",
        "serial_number": "",
        "sans": [],
        "key_algorithm": None,
        "key_length": None,
    }

    san_marker = "X509v3 Subject Alternative Name"
    in_san = False
    public_key_marker = "Public Key Algorithm:"
    rsa_size_marker = "Public-Key:"

    for raw in stdout.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("subject="):
            rtnval["subject"] = stripped[len("subject="):].strip()
            continue
        if stripped.startswith("issuer="):
            rtnval["issuer"] = stripped[len("issuer="):].strip()
            continue
        if stripped.startswith("notBefore="):
            rtnval["not_before"] = stripped[len("notBefore="):].strip()
            continue
        if stripped.startswith("notAfter="):
            rtnval["not_after"] = stripped[len("notAfter="):].strip()
            continue
        if stripped.startswith("serial="):
            rtnval["serial_number"] = stripped[len("serial="):].strip()
            continue

        if stripped.startswith(public_key_marker):
            algo_text = stripped[len(public_key_marker):].strip()
            if "rsaEncryption" in algo_text:
                rtnval["key_algorithm"] = "RSA"
            elif "id-ecPublicKey" in algo_text:
                rtnval["key_algorithm"] = "EC"
            else:
                rtnval["key_algorithm"] = algo_text
            continue

        if stripped.startswith(rsa_size_marker):
            size_text = stripped[len(rsa_size_marker):].strip()
            digits: list = []
            for ch in size_text:
                if ch.isdigit():
                    digits.append(ch)
                elif len(digits) > 0:
                    break
            if len(digits) > 0:
                try:
                    parsed_length = int("".join(digits))
                    rtnval["key_length"] = parsed_length
                except ValueError:
                    # A key size that cannot be parsed leaves 'key_length' unset; the remaining
                    # certificate fields are still reported.
                    pass
            continue

        if san_marker in stripped:
            in_san = True
            continue

        if in_san:
            if stripped.startswith("X509v3") or stripped.startswith("Signature"):
                in_san = False
                continue
            if len(stripped) == 0:
                in_san = False
                continue
            for chunk in stripped.split(","):
                chunk_clean = chunk.strip()
                if len(chunk_clean) == 0:
                    continue
                if chunk_clean.startswith("DNS:"):
                    rtnval["sans"].append(chunk_clean[len("DNS:"):].strip())
                elif chunk_clean.startswith("IP Address:"):
                    rtnval["sans"].append(chunk_clean[len("IP Address:"):].strip())
                elif chunk_clean.startswith("URI:"):
                    rtnval["sans"].append(chunk_clean[len("URI:"):].strip())
                elif chunk_clean.startswith("email:"):
                    rtnval["sans"].append(chunk_clean[len("email:"):].strip())
                else:
                    rtnval["sans"].append(chunk_clean)
            in_san = False

    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — date math + sanitisation
# ----------------------------------------------------------------------


def _days_until(*, not_after: str) -> Optional[int]:
    """
        Parse an openssl-format ``notAfter`` string and return integer
        days between now and that time.
    """
    if len(not_after) == 0:
        rtnval: Optional[int] = None
        return rtnval

    formats = [
        "%b %d %H:%M:%S %Y %Z",
        "%b  %d %H:%M:%S %Y %Z",
        "%b %d %H:%M:%S %Y",
    ]
    parsed: Optional[datetime] = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(not_after, fmt)
            break
        except (ValueError, Exception):
            continue

    if parsed is None:
        rtnval = None
        return rtnval

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    now = datetime.now(tz=timezone.utc)
    delta = parsed - now
    rtnval = delta.days
    return rtnval


def _format_subject(*, common_name: str) -> str:
    """
        Build an OpenSSL ``-subj`` value for a single CN.
    """
    rtnval = "/CN={}".format(common_name)
    return rtnval


def _sanitise_security_passphrases(*, argv: list) -> list:
    """
        Redact any passphrase tokens from a ``security`` argv before
        the result records the ``command`` field.

        The macOS ``security`` CLI accepts ``-P <passphrase>`` /
        ``--password <passphrase>`` as a passphrase carrier.  Because
        this manager always prefers stdin delivery, the recorded
        argv should never contain a passphrase — but if a future
        integration adds one, this helper redacts it to
        ``"<redacted>"`` so audit logs cannot leak the secret.

        :param argv: The original command argv.

        :returns: A new ``list[str]`` with passphrase tokens
                  replaced.
    """
    rtnval: list = []
    redact_next = False
    for token in argv:
        if redact_next:
            rtnval.append("<redacted>")
            redact_next = False
            continue
        if token in ("-P", "--password", "-passin"):
            rtnval.append(token)
            redact_next = True
            continue
        rtnval.append(token)
    return rtnval
