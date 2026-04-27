"""
    macOS certificate-trust-management feature.

    Implements the cross-OS certificate-trust-management contract for
    macOS by wrapping the ``security`` keychain CLI.  Trust stores
    map to keychain files: ``"system-ca"`` -> System.keychain,
    ``"user-ca"`` -> the calling user's login keychain.  Every verb
    is dispatched through the injected :class:`ICommandRunner` so the
    manager works identically under local and SSH execution.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | macOS tool                        |
    +=============================+===================================+
    | ``add_trusted_cert``        | ``security add-trusted-cert -d -r |
    |                             | trustRoot -k <keychain> <path>``  |
    +-----------------------------+-----------------------------------+
    | ``remove_trusted_cert``     | ``security delete-certificate -Z  |
    |                             | <fingerprint> <keychain>``        |
    +-----------------------------+-----------------------------------+
    | ``list_trusted_certs``      | ``security find-certificate -a    |
    |                             | -p <keychain>`` then split the    |
    |                             | multi-PEM stream                  |
    +-----------------------------+-----------------------------------+
    | ``get_cert_details``        | ``security find-certificate -Z    |
    |                             | <fingerprint> -p <keychain>`` +   |
    |                             | ``openssl x509 -text``            |
    +-----------------------------+-----------------------------------+
    | ``update_trust_store``      | no-op SUCCESS (the macOS keychain |
    |                             | reflects changes immediately)     |
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Apart from the source-PEM read in :meth:`add_trusted_cert`
        (handled by ``security`` itself, given the path), the manager
        does not perform direct file I/O.  All keychain mutations and
        reads route through ``security``.

    Reload mechanism:
        macOS has no equivalent of
        ``update-ca-certificates``.  Trust changes made via
        ``security add-trusted-cert`` / ``security
        delete-certificate`` are picked up immediately by Trust
        Services on the next certificate-evaluation call (no
        background bundle is rebuilt).  Therefore
        :meth:`update_trust_store` always returns ``SUCCESS`` without
        invoking the runner.

    Authentication caveat:
        ``security add-trusted-cert -d -k /Library/Keychains/System.keychain``
        prompts for an admin password unless the runner is already
        running as root (or the System.keychain is unlocked
        non-interactively, e.g. via ``security unlock-keychain``).
        This module relies on the runner / aspect chain to provide
        elevation; it does NOT prompt or stash credentials.

    Fingerprint format:
        Apple's ``security find-certificate -Z`` emits SHA-1 by
        default; we explicitly request SHA-256 via PEM extraction +
        pure-Python hashing for cross-OS consistency.  The native
        macOS convention is uppercase hex with no colons (e.g.
        ``"AB12CD..."``); fingerprints are normalised to that form
        before being passed to ``security delete-certificate -Z``.

    Elevation policy:
        Writes against ``"system-ca"`` honour ``config.elevated``.
        Writes against ``"user-ca"`` do not elevate (current-user
        keychain).  Reads do not elevate.
"""

import base64
import hashlib
import os

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_SECURITY = "security"
_OPENSSL = "openssl"

_SYSTEM_CA_STORE = "system-ca"
_USER_CA_STORE = "user-ca"

_SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"
_USER_KEYCHAIN = "~/Library/Keychains/login.keychain-db"

_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


MacosCertTrustActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class MacosCertTrustManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosCertificateTrustManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param store_path_map: Optional override mapping of abstract
                               store name to keychain path.  When
                               ``None`` the bundled defaults are used
                               (``"system-ca"`` ->
                               ``/Library/Keychains/System.keychain``,
                               ``"user-ca"`` ->
                               ``~/Library/Keychains/login.keychain-db``).
        :param default_store: The abstract store name used by
                              ``add_trusted_cert`` /
                              ``remove_trusted_cert`` /
                              ``list_trusted_certs`` when callers do not
                              supply an explicit store argument.
                              Defaults to ``"system-ca"``.
        :param security_executable: Path to the ``security``
                                    executable.  Defaults to
                                    ``"security"`` (PATH-resolved).
        :param openssl_executable: Path to the ``openssl`` executable
                                   used for text-mode metadata
                                   parsing.  Defaults to ``"openssl"``.
    """
    store_path_map: Optional[dict] = None
    default_store: str = _SYSTEM_CA_STORE
    security_executable: str = _SECURITY
    openssl_executable: str = _OPENSSL


@dataclass
class MacosCertTrustActionResult(FeatureActionResult):
    """
        Result of a certificate-trust-management write operation.

        :param store: The abstract store name targeted.
        :param fingerprint: The SHA-256 fingerprint of the affected
                            certificate (macOS native form: uppercase
                            hex, no colons).
        :param path: The on-disk source path the action targeted, or
                     ``None`` for delete / refresh.
    """
    store: Optional[str] = None
    fingerprint: Optional[str] = None
    path: Optional[str] = None


@dataclass
class MacosCertTrustInfo:
    """
        Structured metadata for a single trusted certificate.

        :param fingerprint: SHA-256 fingerprint in the macOS native
                            form (uppercase hex, no colons).
        :param subject: Subject DN.
        :param issuer: Issuer DN.
        :param not_before: Validity start (openssl format).
        :param not_after: Validity end (openssl format).
        :param serial_number: Hex serial number.
        :param store: The abstract store the certificate was found in.
        :param path: Always ``None`` on macOS — keychain entries are
                     not file-backed at the cert level.
        :param purposes: Trust purposes extracted from the
                         certificate text, when known.
    """
    fingerprint: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    serial_number: str
    store: str
    path: Optional[str] = None
    purposes: list = field(default_factory=list)


class MacosCertTrustQueryError(Exception):
    """
        Raised when a certificate-trust-management read fails for a
        reason other than the target source simply being absent.
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
        err_msg_lines = ["Certificate trust query command failed."]
        err_msg_lines.append("    OPERATION: {}".format(operation))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosCertificateTrustManager(BaseFeatureManager):
    """
        macOS certificate-trust-management manager.  Wraps the
        ``security`` keychain CLI and pure-Python PEM
        parsing through the injected :class:`ICommandRunner`.
    """

    result_class = MacosCertTrustActionResult
    status_class = MacosCertTrustActionStatus
    feature_name = "certificate-trust-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_trusted_cert(
        self,
        store: str,
        path: str,
        purpose: Optional[str] = None,
    ) -> MacosCertTrustActionResult:
        """
            Install a PEM certificate into a keychain trust store via
            ``security add-trusted-cert``.

            :param store: The abstract store name (e.g.
                          ``"system-ca"``).
            :param path: Source PEM file path.
            :param purpose: Optional purpose hint recorded on the
                            result.

            :returns: The :class:`MacosCertTrustActionResult`.
        """
        keychain = self._resolve_store_path(store=store)
        if keychain is None:
            failed = self._build_result(
                status=MacosCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=None,
                detail="store '{}' is not supported on macOS".format(store),
            )
            return failed

        # Read the PEM via cat through the runner so the bytes are
        # available for in-process fingerprinting / validation.
        source_content = self._cat(path=path)
        if source_content is None:
            failed = self._build_result(
                status=MacosCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=["cat", path],
                detail="source path does not exist",
            )
            return failed

        cert_blocks = _parse_pem_blocks(content=source_content)
        if len(cert_blocks) == 0:
            failed = self._build_result(
                status=MacosCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=None,
                detail="source file is not a valid PEM certificate",
            )
            return failed

        first_pem = cert_blocks[0]
        fingerprint = _compute_fingerprint(
            pem=first_pem,
            colonized=False,
            uppercase=True,
        )
        if fingerprint is None:
            failed = self._build_result(
                status=MacosCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=None,
                detail="failed to compute fingerprint from PEM body",
            )
            return failed

        # Idempotency: if the keychain already trusts a cert with the
        # same fingerprint, skip.
        existing = self._find_keychain_fingerprint(
            keychain=keychain, fingerprint=fingerprint,
        )
        if existing is True:
            skipped = self._build_result(
                status=MacosCertTrustActionStatus.SKIPPED,
                store=store,
                fingerprint=fingerprint,
                path=path,
                command=None,
                detail="certificate already present in keychain",
            )
            return skipped

        executable = self._typed_config().security_executable
        write_command = [
            executable, "add-trusted-cert", "-d", "-r", "trustRoot",
            "-k", keychain, path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosCertTrustActionStatus.DRY_RUN,
                store=store,
                fingerprint=fingerprint,
                path=path,
                command=write_command,
            )
            return dry

        elevated = self._elevation_for_store(store=store)
        run_result = self._runner.run(
            list(write_command),
            elevated=elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosCertTrustActionStatus.SUCCESS
        else:
            mapped_status = MacosCertTrustActionStatus.FAILED

        rtnval = MacosCertTrustActionResult(
            status=mapped_status,
            detail=purpose,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            store=store,
            fingerprint=fingerprint,
            path=path,
        )
        return rtnval

    def remove_trusted_cert(
        self,
        store: str,
        fingerprint: str,
    ) -> MacosCertTrustActionResult:
        """
            Remove the trusted certificate whose fingerprint matches
            via ``security delete-certificate -Z``.

            :param store: The abstract store name.
            :param fingerprint: The SHA-256 fingerprint to remove.
                                Accepted in colonised or non-colonised
                                form; normalised to uppercase no-colon.

            :returns: The :class:`MacosCertTrustActionResult`.
        """
        keychain = self._resolve_store_path(store=store)
        if keychain is None:
            failed = self._build_result(
                status=MacosCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=fingerprint,
                path=None,
                command=None,
                detail="store '{}' is not supported on macOS".format(store),
            )
            return failed

        normalized = _normalize_fingerprint(value=fingerprint)
        existing = self._find_keychain_fingerprint(
            keychain=keychain, fingerprint=normalized,
        )
        if existing is False:
            skipped = self._build_result(
                status=MacosCertTrustActionStatus.SKIPPED,
                store=store,
                fingerprint=normalized,
                path=None,
                command=None,
                detail="no certificate with that fingerprint in keychain",
            )
            return skipped

        executable = self._typed_config().security_executable
        delete_command = [
            executable, "delete-certificate", "-Z", normalized, keychain,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosCertTrustActionStatus.DRY_RUN,
                store=store,
                fingerprint=normalized,
                path=None,
                command=delete_command,
            )
            return dry

        elevated = self._elevation_for_store(store=store)
        run_result = self._runner.run(
            list(delete_command),
            elevated=elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosCertTrustActionStatus.SUCCESS
        else:
            mapped_status = MacosCertTrustActionStatus.FAILED

        rtnval = MacosCertTrustActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(delete_command),
            store=store,
            fingerprint=normalized,
            path=None,
        )
        return rtnval

    def update_trust_store(self) -> MacosCertTrustActionResult:
        """
            No-op on macOS — Trust Services pick up keychain
            mutations immediately.  Always returns ``SUCCESS`` with
            ``command=None``; the runner is NOT invoked.

            Honours dry-run for symmetry with other OSes.

            :returns: The :class:`MacosCertTrustActionResult`.
        """
        if self._config.dry_run:
            dry = self._build_result(
                status=MacosCertTrustActionStatus.DRY_RUN,
                store=None,
                fingerprint=None,
                path=None,
                command=None,
                detail="macOS keychain reflects changes immediately",
            )
            return dry

        rtnval = self._build_result(
            status=MacosCertTrustActionStatus.SUCCESS,
            store=None,
            fingerprint=None,
            path=None,
            command=None,
            detail="macOS keychain reflects changes immediately",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_trusted_certs(
        self,
        store: str,
    ) -> list:
        """
            Enumerate every trusted certificate in the named keychain.

            Dispatches ``security find-certificate -a -p <keychain>``
            and splits the resulting multi-PEM stream into individual
            envelopes.  Each envelope is fingerprinted in pure Python
            and parsed for metadata via ``openssl x509 -text``.

            :param store: The abstract store name.

            :returns: A ``list[MacosCertTrustInfo]``.

            :raises MacosCertTrustQueryError: When the store name is
                                              unsupported.
        """
        keychain = self._resolve_store_path(store=store)
        if keychain is None:
            raise MacosCertTrustQueryError(
                operation="list_trusted_certs",
                message="store '{}' is not supported on macOS".format(store),
            )

        executable = self._typed_config().security_executable
        list_command = [
            executable, "find-certificate", "-a", "-p", keychain,
        ]
        run_result = self._runner.run(
            list(list_command), elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval

        pem_blocks = _parse_pem_blocks(content=run_result.stdout)
        rtnval = []
        for pem in pem_blocks:
            fingerprint = _compute_fingerprint(
                pem=pem,
                colonized=False,
                uppercase=True,
            )
            if fingerprint is None:
                continue
            info = self._cert_info_from_pem(
                pem=pem,
                fingerprint=fingerprint,
                store=store,
            )
            if info is not None:
                rtnval.append(info)
        return rtnval

    def get_cert_details(
        self,
        fingerprint: str,
    ) -> Optional[MacosCertTrustInfo]:
        """
            Look up a certificate across every supported keychain by
            fingerprint and return its structured metadata.

            :param fingerprint: SHA-256 fingerprint, colonised or
                                non-colonised.

            :returns: The :class:`MacosCertTrustInfo`, or ``None``
                      when no certificate with that fingerprint is
                      found.
        """
        normalized = _normalize_fingerprint(value=fingerprint)
        store_map = self._effective_store_map()
        executable = self._typed_config().security_executable
        for store_name, keychain in store_map.items():
            if keychain is None:
                continue
            command = [
                executable, "find-certificate",
                "-Z", normalized,
                "-p", keychain,
            ]
            run_result = self._runner.run(
                list(command), elevated=False,
            )
            if run_result.exit_code != 0:
                continue
            pem_blocks = _parse_pem_blocks(content=run_result.stdout)
            if len(pem_blocks) == 0:
                continue
            first_pem = pem_blocks[0]
            info = self._cert_info_from_pem(
                pem=first_pem,
                fingerprint=normalized,
                store=store_name,
            )
            if info is not None:
                rtnval: Optional[MacosCertTrustInfo] = info
                return rtnval
        rtnval = None
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosCertTrustManagerConfig:
        """
            Narrow the base config to the typed subclass.
        """
        rtnval = self._config
        return rtnval

    def _effective_store_map(self) -> dict:
        """
            Merge the bundled defaults with any caller-supplied
            override, preserving the override's precedence.
        """
        defaults: dict = {
            _SYSTEM_CA_STORE: _SYSTEM_KEYCHAIN,
            _USER_CA_STORE: _USER_KEYCHAIN,
        }
        override = self._typed_config().store_path_map
        if override is None:
            rtnval = defaults
            return rtnval
        merged = dict(defaults)
        for key, value in override.items():
            merged[key] = value
        rtnval = merged
        return rtnval

    def _resolve_store_path(self, *, store: str) -> Optional[str]:
        """
            Resolve a store name to its keychain path.

            :param store: The abstract store name.

            :returns: The keychain path or ``None`` for unsupported.
        """
        store_map = self._effective_store_map()
        if store not in store_map:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = store_map[store]
        return rtnval

    def _elevation_for_store(self, *, store: str) -> bool:
        """
            ``"system-ca"`` honours ``config.elevated``;
            ``"user-ca"`` never elevates (current-user keychain).

            :param store: The abstract store name.

            :returns: The effective elevation flag.
        """
        if store == _USER_CA_STORE:
            rtnval = False
            return rtnval
        rtnval = self._config.elevated
        return rtnval

    def _cat(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` through the runner.  Reads do not
            elevate.

            :param path: The absolute path.

            :returns: The contents on success, ``None`` on missing.
        """
        run_result = self._runner.run(
            ["cat", path], elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _find_keychain_fingerprint(
        self,
        *,
        keychain: str,
        fingerprint: str,
    ) -> bool:
        """
            Probe a keychain for the presence of a certificate with the
            supplied fingerprint.  Uses ``security find-certificate
            -Z <fp> <keychain>``.

            :param keychain: The keychain path.
            :param fingerprint: The fingerprint (no-colon uppercase).

            :returns: ``True`` when present, ``False`` when absent.
        """
        executable = self._typed_config().security_executable
        command = [
            executable, "find-certificate", "-Z", fingerprint, keychain,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _cert_info_from_pem(
        self,
        *,
        pem: str,
        fingerprint: str,
        store: str,
    ) -> Optional[MacosCertTrustInfo]:
        """
            Build a :class:`MacosCertTrustInfo` for a single PEM
            block by piping it through ``openssl x509 -text``.

            :param pem: The PEM envelope.
            :param fingerprint: The pre-computed fingerprint.
            :param store: The store name to record on the result.

            :returns: The :class:`MacosCertTrustInfo` or ``None``
                      when the openssl invocation fails.
        """
        executable = self._typed_config().openssl_executable
        command = [
            executable, "x509", "-noout", "-subject", "-issuer",
            "-startdate", "-enddate", "-serial", "-text",
        ]
        run_result = self._runner.run(
            list(command),
            elevated=False,
            stdin=pem,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[MacosCertTrustInfo] = None
            return rtnval

        parsed = _parse_openssl_cert_text(stdout=run_result.stdout)
        rtnval = MacosCertTrustInfo(
            fingerprint=fingerprint,
            subject=parsed.get("subject", ""),
            issuer=parsed.get("issuer", ""),
            not_before=parsed.get("not_before", ""),
            not_after=parsed.get("not_after", ""),
            serial_number=parsed.get("serial_number", ""),
            store=store,
            path=None,
            purposes=parsed.get("purposes", []),
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosCertTrustActionStatus,
        store: Optional[str],
        fingerprint: Optional[str],
        path: Optional[str],
        command: Optional[list],
        detail: Optional[str] = None,
    ) -> MacosCertTrustActionResult:
        """
            Build a :class:`MacosCertTrustActionResult` without
            invoking the runner.
        """
        if command is None:
            command_copy: Optional[list] = None
        else:
            command_copy = list(command)
        rtnval = MacosCertTrustActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            store=store,
            fingerprint=fingerprint,
            path=path,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PEM / fingerprint / openssl text parsing
# ----------------------------------------------------------------------


def _parse_pem_blocks(*, content: str) -> list:
    """
        Split a PEM-bearing string into a list of CERTIFICATE blocks.

        :param content: Raw content (multi-PEM allowed; common output
                        format for ``security find-certificate -a
                        -p``).

        :returns: A ``list[str]``, one full PEM envelope per element.
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

        :param pem: The full PEM envelope.
        :param colonized: When True, format with colons.
        :param uppercase: When True, uppercase hex digits.

        :returns: The fingerprint string, or ``None`` on decode
                  failure.
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


def _normalize_fingerprint(*, value: str) -> str:
    """
        Normalise a fingerprint string to the macOS canonical form
        (uppercase hex, no colons).

        :param value: The raw input.

        :returns: The normalised string.
    """
    text = value.strip()
    lower_prefix = text.lower()
    if lower_prefix.startswith("sha256 fingerprint="):
        text = text[len("SHA256 Fingerprint="):]
    elif lower_prefix.startswith("sha256:"):
        text = text[len("SHA256:"):]
    text = text.replace(":", "").replace(" ", "").upper()
    rtnval = text
    return rtnval


def _parse_openssl_cert_text(*, stdout: str) -> dict:
    """
        Parse the combined output of ``openssl x509 -subject -issuer
        -startdate -enddate -serial -text``.

        :param stdout: Raw openssl stdout.

        :returns: Dict with ``subject``, ``issuer``, ``not_before``,
                  ``not_after``, ``serial_number``, ``purposes``.
    """
    rtnval: dict = {
        "subject": "",
        "issuer": "",
        "not_before": "",
        "not_after": "",
        "serial_number": "",
        "purposes": [],
    }

    eku_marker = "X509v3 Extended Key Usage"
    in_eku = False

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

        if eku_marker in stripped:
            in_eku = True
            continue

        if in_eku:
            if stripped.startswith("X509v3") or stripped.startswith("Signature"):
                in_eku = False
                continue
            if len(stripped) == 0:
                in_eku = False
                continue
            purposes_text = stripped.split(":", 1)[-1].strip()
            for chunk in purposes_text.split(","):
                chunk_clean = chunk.strip()
                if len(chunk_clean) == 0:
                    continue
                if chunk_clean not in rtnval["purposes"]:
                    rtnval["purposes"].append(chunk_clean)
            in_eku = False

    return rtnval
