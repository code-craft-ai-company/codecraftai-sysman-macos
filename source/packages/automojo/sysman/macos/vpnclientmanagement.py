"""
    macOS vpn-client-management feature manager.

    Implements the vpn-client-management contract for macOS using either
    the Homebrew formula ``tailscale`` or the signed ``.pkg`` from
    Tailscale for the Tailscale client, and the signed ``.pkg`` from
    Twingate for the Twingate Connector.  Service lifecycle is controlled
    with ``launchctl bootstrap`` / ``launchctl bootout`` against the
    appropriate ``/Library/LaunchDaemons/*.plist``.

    Every command flows through the injected
    :class:`automojo.interfaces.icommandrunner.ICommandRunner`.  No direct
    subprocess is spawned and no direct filesystem write is performed.

    See:
        - :doc:`/specs/contracts/vpn-client-management/contract.md`
        - :doc:`/specs/vpn-client-management-base/design.md`
"""

import json
import os

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from automojo.aspects.aspects import Aspects
from automojo.interfaces.icommandrunner import ICommandRunner, RunResult

from automojo.sysman.common.vpnactionresult import VPNActionResult
from automojo.sysman.common.vpnactionstatus import VPNActionStatus
from automojo.sysman.common.vpnclientmanagementbase import (
    VPNClientManagementBase,
    VERB_KIND_WRITE,
)
from automojo.sysman.common.vpnclientmanagerconfig import VPNClientManagerConfig
from automojo.sysman.common.vpnclientqueryerror import VPNClientQueryError
from automojo.sysman.common.vpnclientstatus import VPNClientStatus
from automojo.sysman.common.vpnprovider import VPNProvider


# ------------------------------------------------------------------
# Macos-prefixed aliases for the shared contract types.
# ------------------------------------------------------------------

MacosVPNActionStatus = VPNActionStatus
MacosVPNActionResult = VPNActionResult
MacosVPNClientStatus = VPNClientStatus
MacosVPNClientQueryError = VPNClientQueryError


# ------------------------------------------------------------------
# Constants.
# ------------------------------------------------------------------

_TAILSCALE_PKG_ID = "io.tailscale.tailscaled"
_TWINGATE_PKG_ID = "com.twingate.connector"

_DEFAULT_TAILSCALE_PLIST = "/Library/LaunchDaemons/com.tailscale.tailscaled.plist"
_DEFAULT_TWINGATE_PLIST = "/Library/LaunchDaemons/com.twingate.connector.plist"
_DEFAULT_TWINGATE_CONFIG = "/Library/Application Support/Twingate/connector.conf"
_DEFAULT_PKG_CACHE_DIR = "/var/tmp/automojo/vpn-pkgs"

_TAILSCALE_UNINSTALL_SCRIPT = "/Library/Application Support/Tailscale/uninstall.sh"
_TAILSCALE_STATE_DIR = "/Library/Tailscale"

_TAILSCALE_PKG_URL_TEMPLATE = (
    "https://pkgs.tailscale.com/stable/Tailscale-{version}.pkg"
)
_TAILSCALE_PKG_URL_LATEST = "https://pkgs.tailscale.com/stable/Tailscale.pkg"
_TWINGATE_PKG_URL_TEMPLATE = (
    "https://binaries.twingate.com/connector/macos/{version}/TwingateConnector.pkg"
)
_TWINGATE_PKG_URL_LATEST = (
    "https://binaries.twingate.com/connector/macos/TwingateConnector.pkg"
)


class InstallSource(Enum):
    """
        Tailscale install source selector.

        ``HOMEBREW`` installs via ``brew install tailscale``;
        ``PKG`` downloads the signed ``.pkg`` and runs ``installer``.
    """
    HOMEBREW = "homebrew"
    PKG = "pkg"


@dataclass
class MacosVPNClientManagerConfig(VPNClientManagerConfig):
    """
        Configuration for :class:`MacosVPNClientManager`.

        Extends :class:`VPNClientManagerConfig` with macOS-specific
        fields.

        :param install_source: Selects the Tailscale install source.
        :param tailscale_plist_path: Absolute path of the Tailscale
                                     launchd plist.
        :param twingate_plist_path: Absolute path of the Twingate
                                    Connector launchd plist.
        :param twingate_config_path: Absolute path of the Twingate
                                     connector configuration file.
        :param pkg_cache_dir: Directory where downloaded ``.pkg`` files
                              are staged before ``installer`` is run.
    """
    install_source: InstallSource = InstallSource.HOMEBREW
    tailscale_plist_path: str = _DEFAULT_TAILSCALE_PLIST
    twingate_plist_path: str = _DEFAULT_TWINGATE_PLIST
    twingate_config_path: str = _DEFAULT_TWINGATE_CONFIG
    pkg_cache_dir: str = _DEFAULT_PKG_CACHE_DIR


class MacosVPNClientManager(VPNClientManagementBase):
    """
        macOS implementation of the vpn-client-management contract.

        Every command runs through the injected
        :class:`ICommandRunner`.  Idempotency probes use
        ``brew list --versions`` / ``pkgutil --pkg-info`` /
        ``tailscale status --json`` / ``twingate status``.  Dry-run
        short-circuits via :meth:`_short_circuit_dry_run` inherited
        from the base.
    """

    feature_name: Optional[str] = "vpn-client-management"

    def __init__(
        self,
        *,
        runner: ICommandRunner,
        config: Optional[MacosVPNClientManagerConfig] = None,
    ) -> None:
        """
            :param runner: Command runner injected by the maintenance
                           coordinator.
            :param config: Manager configuration.  Defaults to a fresh
                           :class:`MacosVPNClientManagerConfig` when
                           omitted.
        """
        if config is None:
            cfg = MacosVPNClientManagerConfig()
        else:
            cfg = config

        super().__init__(runner=runner, config=cfg)
        self._mac_config: MacosVPNClientManagerConfig = cfg
        return

    # ------------------------------------------------------------------
    # install_tailscale
    # ------------------------------------------------------------------

    def install_tailscale(
        self,
        *,
        version: Optional[str],
        channel: str,
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Install the Tailscale client via Homebrew or signed PKG.

            :param version: Specific version to install, or ``None`` to
                            take the channel default.
            :param channel: Release channel identifier (``"stable"``,
                            ``"unstable"``).  Reserved for parity with
                            the other OSes; macOS uses a single channel.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult` describing the
                      outcome.
        """
        install_source = self._mac_config.install_source

        if install_source == InstallSource.HOMEBREW:
            rtnval = self._install_tailscale_homebrew(
                version=version, aspects=aspects,
            )
            return rtnval

        if install_source == InstallSource.PKG:
            rtnval = self._install_tailscale_pkg(
                version=version, aspects=aspects,
            )
            return rtnval

        err_msg_lines = ["Unknown install_source for Tailscale."]
        err_msg_lines.append("    PROVIDED: {!r}".format(install_source))
        err_msg_lines.append(
            "    ACCEPTED: [{!r}, {!r}]".format(
                InstallSource.HOMEBREW, InstallSource.PKG,
            ),
        )
        err_msg = os.linesep.join(err_msg_lines)
        raise ValueError(err_msg)

    def _install_tailscale_homebrew(
        self,
        *,
        version: Optional[str],
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Homebrew install path for Tailscale.

            :param version: Specific version to pin, or ``None``.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        if version is not None and len(version) > 0:
            formula = "tailscale@{}".format(version)
        else:
            formula = "tailscale"

        command = ["brew", "install", formula]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TAILSCALE,
            command=command,
            detail="install_tailscale (homebrew)",
        )
        if dry is not None:
            return dry

        installed_version = self._brew_installed_version(
            formula="tailscale", aspects=aspects,
        )
        target_match = self._brew_versions_match(
            installed=installed_version, requested=version,
        )
        if target_match is True:
            skipped = VPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale already installed via homebrew",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=command,
            )
            return skipped

        # Homebrew refuses to run as root; force unelevated dispatch
        # regardless of the config's elevated flag.
        run_result: RunResult = self._runner.run(
            list(command),
            elevated=False,
        )
        rtnval = self._wrap_run_result(
            provider=VPNProvider.TAILSCALE,
            command=command,
            detail="install_tailscale (homebrew)",
            run_result=run_result,
        )
        return rtnval

    def _install_tailscale_pkg(
        self,
        *,
        version: Optional[str],
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Signed-PKG install path for Tailscale.

            :param version: Specific version to substitute into the
                            download URL, or ``None`` for latest.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        pkg_path = self._tailscale_pkg_path(version=version)
        pkg_url = self._tailscale_pkg_url(version=version)
        install_command = ["installer", "-pkg", pkg_path, "-target", "/"]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TAILSCALE,
            command=install_command,
            detail="install_tailscale (pkg)",
        )
        if dry is not None:
            return dry

        installed_version = self._pkgutil_installed_version(
            pkg_id=_TAILSCALE_PKG_ID, aspects=aspects,
        )
        target_match = self._pkgutil_versions_match(
            installed=installed_version, requested=version,
        )
        if target_match is True:
            skipped = VPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale already installed (pkg)",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=install_command,
            )
            return skipped

        prep_failure = self._stage_pkg_download(
            pkg_path=pkg_path,
            pkg_url=pkg_url,
            provider=VPNProvider.TAILSCALE,
        )
        if prep_failure is not None:
            return prep_failure

        rtnval = self._dispatch_write(
            provider=VPNProvider.TAILSCALE,
            command=install_command,
            detail="install_tailscale (pkg)",
            aspects=aspects,
        )
        return rtnval

    # ------------------------------------------------------------------
    # install_twingate_connector
    # ------------------------------------------------------------------

    def install_twingate_connector(
        self,
        *,
        version: Optional[str],
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Install the Twingate Connector via signed PKG.

            :param version: Specific version to substitute into the
                            download URL, or ``None`` for latest.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        pkg_path = self._twingate_pkg_path(version=version)
        pkg_url = self._twingate_pkg_url(version=version)
        install_command = ["installer", "-pkg", pkg_path, "-target", "/"]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=install_command,
            detail="install_twingate_connector",
        )
        if dry is not None:
            return dry

        installed_version = self._pkgutil_installed_version(
            pkg_id=_TWINGATE_PKG_ID, aspects=aspects,
        )
        target_match = self._pkgutil_versions_match(
            installed=installed_version, requested=version,
        )
        if target_match is True:
            skipped = VPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TWINGATE,
                detail="twingate connector already installed",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=install_command,
            )
            return skipped

        prep_failure = self._stage_pkg_download(
            pkg_path=pkg_path,
            pkg_url=pkg_url,
            provider=VPNProvider.TWINGATE,
        )
        if prep_failure is not None:
            return prep_failure

        rtnval = self._dispatch_write(
            provider=VPNProvider.TWINGATE,
            command=install_command,
            detail="install_twingate_connector",
            aspects=aspects,
        )
        return rtnval

    # ------------------------------------------------------------------
    # configure
    # ------------------------------------------------------------------

    def configure(
        self,
        *,
        provider: VPNProvider,
        settings: Dict[str, str],
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Write provider-specific configuration.

            Tailscale has no on-disk configuration step on macOS and
            therefore returns ``SKIPPED``.  Twingate renders the
            connector config file via ``tee``.

            :param provider: Target provider.
            :param settings: Provider-specific key/value settings.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        validation = self._validate_provider(
            provider=provider,
            supported=[VPNProvider.TAILSCALE, VPNProvider.TWINGATE],
        )
        if validation is not None:
            return validation

        if provider == VPNProvider.TAILSCALE:
            rtnval = VPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale has no on-disk configure step on macos",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=None,
            )
            return rtnval

        rtnval = self._configure_twingate(
            settings=settings, aspects=aspects,
        )
        return rtnval

    def _configure_twingate(
        self,
        *,
        settings: Dict[str, str],
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Write the Twingate connector configuration via ``tee``.

            :param settings: Twingate-specific key/value settings.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        config_path = self._mac_config.twingate_config_path
        desired_body = _render_twingate_conf(settings=settings)
        write_command = ["tee", config_path]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=write_command,
            detail="configure twingate",
        )
        if dry is not None:
            return dry

        current_body = self._read_file(path=config_path)
        if current_body is not None and current_body == desired_body:
            skipped = VPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TWINGATE,
                detail="twingate connector.conf already up to date",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=write_command,
            )
            return skipped

        run_elevated = self._should_elevate(kind=VERB_KIND_WRITE)
        run_result: RunResult = self._runner.run(
            list(write_command),
            elevated=run_elevated,
            stdin=desired_body,
        )
        rtnval = self._wrap_run_result(
            provider=VPNProvider.TWINGATE,
            command=write_command,
            detail="configure twingate",
            run_result=run_result,
        )
        return rtnval

    # ------------------------------------------------------------------
    # bring_up
    # ------------------------------------------------------------------

    def bring_up(
        self,
        *,
        provider: VPNProvider,
        authkey: str,
        hostname: Optional[str],
        advertise_tags: Optional[List[str]],
        advertise_routes: Optional[List[str]],
        exit_node: bool,
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Bring the mesh-VPN tunnel up.

            :param provider: Target provider.
            :param authkey: Vendor-issued authentication key.
            :param hostname: Hostname to register, or ``None``.
            :param advertise_tags: Tags to advertise, or ``None``.
            :param advertise_routes: Subnet routes to advertise, or
                                     ``None``.
            :param exit_node: Whether to advertise this host as an
                              exit node.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        validation = self._validate_provider(
            provider=provider,
            supported=[VPNProvider.TAILSCALE, VPNProvider.TWINGATE],
        )
        if validation is not None:
            return validation

        if provider == VPNProvider.TAILSCALE:
            rtnval = self._bring_up_tailscale(
                authkey=authkey,
                hostname=hostname,
                advertise_tags=advertise_tags,
                advertise_routes=advertise_routes,
                exit_node=exit_node,
                aspects=aspects,
            )
            return rtnval

        rtnval = self._bring_up_twingate(aspects=aspects)
        return rtnval

    def _bring_up_tailscale(
        self,
        *,
        authkey: str,
        hostname: Optional[str],
        advertise_tags: Optional[List[str]],
        advertise_routes: Optional[List[str]],
        exit_node: bool,
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Tailscale bring-up: ensure the launchd job is loaded and
            then run ``tailscale up`` with the supplied parameters.

            :param authkey: Vendor-issued auth key.
            :param hostname: Hostname to register, or ``None``.
            :param advertise_tags: Tags to advertise, or ``None``.
            :param advertise_routes: Subnet routes, or ``None``.
            :param exit_node: Advertise as exit node when True.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        up_command = _build_tailscale_up_command(
            authkey=authkey,
            hostname=hostname,
            advertise_tags=advertise_tags,
            advertise_routes=advertise_routes,
            exit_node=exit_node,
        )

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TAILSCALE,
            command=up_command,
            detail="bring_up tailscale",
        )
        if dry is not None:
            return dry

        current_status = self._safe_query_tailscale_status(aspects=aspects)
        already_running = self._tailscale_state_matches(
            current=current_status, hostname=hostname,
        )
        if already_running is True:
            skipped = VPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale already up with matching state",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=up_command,
            )
            return skipped

        bootstrap_command = [
            "launchctl", "bootstrap", "system",
            self._mac_config.tailscale_plist_path,
        ]
        elevation_failure = self._require_elevation(
            provider=VPNProvider.TAILSCALE,
            command=bootstrap_command,
            operation="bring_up tailscale (launchctl bootstrap)",
        )
        if elevation_failure is not None:
            return elevation_failure

        bootstrap_result = self._dispatch_write(
            provider=VPNProvider.TAILSCALE,
            command=bootstrap_command,
            detail="bring_up tailscale (launchctl bootstrap)",
            aspects=aspects,
        )
        if bootstrap_result.status == VPNActionStatus.FAILED:
            # ``launchctl bootstrap`` on an already-loaded plist exits
            # non-zero; we tolerate that by continuing to ``tailscale up``.
            stderr_text = bootstrap_result.stderr
            if stderr_text is None:
                stderr_text = ""
            if "already" not in stderr_text.lower():
                return bootstrap_result

        rtnval = self._dispatch_write(
            provider=VPNProvider.TAILSCALE,
            command=up_command,
            detail="bring_up tailscale (tailscale up)",
            aspects=aspects,
        )
        return rtnval

    def _bring_up_twingate(
        self,
        *,
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Twingate bring-up: bootstrap the launchd job.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        bootstrap_command = [
            "launchctl", "bootstrap", "system",
            self._mac_config.twingate_plist_path,
        ]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=bootstrap_command,
            detail="bring_up twingate",
        )
        if dry is not None:
            return dry

        current_status = self._safe_query_twingate_status(aspects=aspects)
        if current_status is not None and current_status.running is True:
            skipped = VPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TWINGATE,
                detail="twingate connector already up",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=bootstrap_command,
            )
            return skipped

        elevation_failure = self._require_elevation(
            provider=VPNProvider.TWINGATE,
            command=bootstrap_command,
            operation="bring_up twingate (launchctl bootstrap)",
        )
        if elevation_failure is not None:
            return elevation_failure

        rtnval = self._dispatch_write(
            provider=VPNProvider.TWINGATE,
            command=bootstrap_command,
            detail="bring_up twingate",
            aspects=aspects,
        )
        return rtnval

    # ------------------------------------------------------------------
    # bring_down
    # ------------------------------------------------------------------

    def bring_down(
        self,
        *,
        provider: VPNProvider,
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Bring the mesh-VPN tunnel down without uninstalling.

            :param provider: Target provider.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        validation = self._validate_provider(
            provider=provider,
            supported=[VPNProvider.TAILSCALE, VPNProvider.TWINGATE],
        )
        if validation is not None:
            return validation

        if provider == VPNProvider.TAILSCALE:
            command = ["tailscale", "down"]
        else:
            command = [
                "launchctl", "bootout", "system",
                self._mac_config.twingate_plist_path,
            ]

        dry = self._short_circuit_dry_run(
            provider=provider,
            command=command,
            detail="bring_down",
        )
        if dry is not None:
            return dry

        if provider == VPNProvider.TAILSCALE:
            current_status = self._safe_query_tailscale_status(aspects=aspects)
        else:
            current_status = self._safe_query_twingate_status(aspects=aspects)

        if current_status is not None and current_status.running is False:
            skipped = VPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=provider,
                detail="tunnel already down",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=command,
            )
            return skipped

        if provider == VPNProvider.TWINGATE:
            elevation_failure = self._require_elevation(
                provider=provider,
                command=command,
                operation="bring_down twingate (launchctl bootout)",
            )
            if elevation_failure is not None:
                return elevation_failure

        rtnval = self._dispatch_write(
            provider=provider,
            command=command,
            detail="bring_down",
            aspects=aspects,
        )
        return rtnval

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(
        self,
        *,
        provider: VPNProvider,
        aspects: Aspects,
    ) -> VPNClientStatus:
        """
            Report the local daemon state.

            :param provider: Target provider.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNClientStatus` snapshot.
            :raises MacosVPNClientQueryError: When the underlying query
                command fails for reasons other than the daemon being
                absent.
        """
        validation = self._validate_provider(
            provider=provider,
            supported=[VPNProvider.TAILSCALE, VPNProvider.TWINGATE],
        )
        if validation is not None:
            err_msg_lines = ["Unsupported VPN provider for status()."]
            err_msg_lines.append("    PROVIDED: {!r}".format(provider.value))
            err_msg = os.linesep.join(err_msg_lines)
            raise MacosVPNClientQueryError(
                message=err_msg,
                exit_code=None,
                stderr=None,
            )

        if provider == VPNProvider.TAILSCALE:
            rtnval = self._status_tailscale()
            return rtnval

        rtnval = self._status_twingate()
        return rtnval

    def _status_tailscale(self) -> VPNClientStatus:
        """
            Query and parse Tailscale status via
            ``tailscale status --json``.

            :returns: A :class:`MacosVPNClientStatus` snapshot.
            :raises MacosVPNClientQueryError: For non-zero exits that
                are not the daemon-missing case.
        """
        command = ["tailscale", "status", "--json"]
        run_result: RunResult = self._runner.run(
            list(command),
            elevated=False,
        )

        if run_result.exit_code != 0:
            stderr_text = run_result.stderr
            if stderr_text is None:
                stderr_text = ""
            lowered = stderr_text.lower()
            if "command not found" in lowered or "no such file" in lowered:
                rtnval = VPNClientStatus(
                    provider=VPNProvider.TAILSCALE,
                    installed=False,
                    running=False,
                )
                return rtnval
            raise MacosVPNClientQueryError(
                message="tailscale status query failed",
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        rtnval = _parse_tailscale_status_json(payload=run_result.stdout)
        return rtnval

    def _status_twingate(self) -> VPNClientStatus:
        """
            Query and parse Twingate status via ``twingate status``.

            :returns: A :class:`MacosVPNClientStatus` snapshot.
            :raises MacosVPNClientQueryError: For non-zero exits that
                are not the daemon-missing case.
        """
        command = ["twingate", "status"]
        run_result: RunResult = self._runner.run(
            list(command),
            elevated=False,
        )

        if run_result.exit_code != 0:
            stderr_text = run_result.stderr
            if stderr_text is None:
                stderr_text = ""
            lowered = stderr_text.lower()
            if "command not found" in lowered or "no such file" in lowered:
                rtnval = VPNClientStatus(
                    provider=VPNProvider.TWINGATE,
                    installed=False,
                    running=False,
                )
                return rtnval
            raise MacosVPNClientQueryError(
                message="twingate status query failed",
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        rtnval = _parse_twingate_status_text(payload=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # uninstall
    # ------------------------------------------------------------------

    def uninstall(
        self,
        *,
        provider: VPNProvider,
        purge: bool,
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Uninstall the provider daemon and optionally purge state.

            :param provider: Target provider.
            :param purge: When True, remove residual state directories
                          in addition to the package.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        validation = self._validate_provider(
            provider=provider,
            supported=[VPNProvider.TAILSCALE, VPNProvider.TWINGATE],
        )
        if validation is not None:
            return validation

        if provider == VPNProvider.TAILSCALE:
            rtnval = self._uninstall_tailscale(
                purge=purge, aspects=aspects,
            )
            return rtnval

        rtnval = self._uninstall_twingate(
            purge=purge, aspects=aspects,
        )
        return rtnval

    def _uninstall_tailscale(
        self,
        *,
        purge: bool,
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Tailscale uninstall — branches on the configured install
            source.

            :param purge: When True, remove residual state.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        install_source = self._mac_config.install_source

        if install_source == InstallSource.HOMEBREW:
            command = ["brew", "uninstall", "tailscale"]
        else:
            command = [_TAILSCALE_UNINSTALL_SCRIPT]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TAILSCALE,
            command=command,
            detail="uninstall tailscale",
        )
        if dry is not None:
            return dry

        if install_source == InstallSource.HOMEBREW:
            installed_version = self._brew_installed_version(
                formula="tailscale", aspects=aspects,
            )
            if installed_version is None:
                skipped = VPNActionResult(
                    status=VPNActionStatus.SKIPPED,
                    provider=VPNProvider.TAILSCALE,
                    detail="tailscale already absent",
                    exit_code=None,
                    stdout=None,
                    stderr=None,
                    command=command,
                )
                return skipped

            run_result: RunResult = self._runner.run(
                list(command),
                elevated=False,
            )
            uninstall_result = self._wrap_run_result(
                provider=VPNProvider.TAILSCALE,
                command=command,
                detail="uninstall tailscale (brew)",
                run_result=run_result,
            )
        else:
            installed_version = self._pkgutil_installed_version(
                pkg_id=_TAILSCALE_PKG_ID, aspects=aspects,
            )
            if installed_version is None:
                skipped = VPNActionResult(
                    status=VPNActionStatus.SKIPPED,
                    provider=VPNProvider.TAILSCALE,
                    detail="tailscale already absent",
                    exit_code=None,
                    stdout=None,
                    stderr=None,
                    command=command,
                )
                return skipped

            uninstall_result = self._dispatch_write(
                provider=VPNProvider.TAILSCALE,
                command=command,
                detail="uninstall tailscale (pkg)",
                aspects=aspects,
            )

        if purge is True and uninstall_result.status == VPNActionStatus.SUCCESS:
            purge_command = ["rm", "-rf", _TAILSCALE_STATE_DIR]
            purge_result = self._dispatch_write(
                provider=VPNProvider.TAILSCALE,
                command=purge_command,
                detail="uninstall tailscale (purge)",
                aspects=aspects,
            )
            if purge_result.status == VPNActionStatus.FAILED:
                return purge_result

        return uninstall_result

    def _uninstall_twingate(
        self,
        *,
        purge: bool,
        aspects: Aspects,
    ) -> VPNActionResult:
        """
            Twingate uninstall — always the PKG path.

            :param purge: When True, remove residual state.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: A :class:`MacosVPNActionResult`.
        """
        command = [
            "launchctl", "bootout", "system",
            self._mac_config.twingate_plist_path,
        ]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=command,
            detail="uninstall twingate",
        )
        if dry is not None:
            return dry

        installed_version = self._pkgutil_installed_version(
            pkg_id=_TWINGATE_PKG_ID, aspects=aspects,
        )
        if installed_version is None:
            skipped = VPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TWINGATE,
                detail="twingate connector already absent",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=command,
            )
            return skipped

        uninstall_result = self._dispatch_write(
            provider=VPNProvider.TWINGATE,
            command=command,
            detail="uninstall twingate",
            aspects=aspects,
        )

        if purge is True and uninstall_result.status == VPNActionStatus.SUCCESS:
            purge_command = [
                "rm", "-rf", self._mac_config.twingate_config_path,
            ]
            purge_result = self._dispatch_write(
                provider=VPNProvider.TWINGATE,
                command=purge_command,
                detail="uninstall twingate (purge)",
                aspects=aspects,
            )
            if purge_result.status == VPNActionStatus.FAILED:
                return purge_result

        return uninstall_result

    # ------------------------------------------------------------------
    # Probe helpers
    # ------------------------------------------------------------------

    def _brew_installed_version(
        self,
        *,
        formula: str,
        aspects: Aspects,
    ) -> Optional[str]:
        """
            Return the installed Homebrew version of ``formula``, or
            ``None`` when the formula is absent.

            :param formula: Homebrew formula name.
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: Version string, or ``None``.
        """
        command = ["brew", "list", "--versions", formula]
        run_result: RunResult = self._runner.run(
            list(command),
            elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        stdout_text = run_result.stdout
        if stdout_text is None:
            rtnval = None
            return rtnval

        stripped = stdout_text.strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        tokens = stripped.split()
        if len(tokens) < 2:
            rtnval = None
            return rtnval

        rtnval = tokens[1]
        return rtnval

    def _pkgutil_installed_version(
        self,
        *,
        pkg_id: str,
        aspects: Aspects,
    ) -> Optional[str]:
        """
            Return the installed package version reported by
            ``pkgutil --pkg-info <id>``, or ``None`` when absent.

            :param pkg_id: macOS package identifier (CFBundleIdentifier).
            :param aspects: Behaviour controls forwarded to the runner.

            :returns: Version string, or ``None``.
        """
        command = ["pkgutil", "--pkg-info", pkg_id]
        run_result: RunResult = self._runner.run(
            list(command),
            elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        stdout_text = run_result.stdout
        if stdout_text is None:
            rtnval = None
            return rtnval

        for raw_line in stdout_text.splitlines():
            stripped = raw_line.strip()
            if not stripped.lower().startswith("version:"):
                continue
            _, _, value = stripped.partition(":")
            rtnval = value.strip()
            return rtnval

        rtnval = None
        return rtnval

    def _brew_versions_match(
        self,
        *,
        installed: Optional[str],
        requested: Optional[str],
    ) -> bool:
        """
            Decide whether the installed Homebrew version satisfies the
            requested pin.

            :param installed: Currently installed version, or ``None``.
            :param requested: Caller-requested version, or ``None``.

            :returns: True when no further install action is needed.
        """
        if installed is None:
            rtnval = False
            return rtnval
        if requested is None:
            rtnval = True
            return rtnval
        rtnval = installed == requested
        return rtnval

    def _pkgutil_versions_match(
        self,
        *,
        installed: Optional[str],
        requested: Optional[str],
    ) -> bool:
        """
            Decide whether the installed PKG version satisfies the
            requested pin.

            :param installed: Currently installed version, or ``None``.
            :param requested: Caller-requested version, or ``None``.

            :returns: True when no further install action is needed.
        """
        if installed is None:
            rtnval = False
            return rtnval
        if requested is None:
            rtnval = True
            return rtnval
        rtnval = installed == requested
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: Absolute path to read.

            :returns: File contents on success; ``None`` on any
                      non-zero exit.
        """
        command = ["cat", path]
        run_result: RunResult = self._runner.run(
            list(command),
            elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _safe_query_tailscale_status(
        self,
        *,
        aspects: Aspects,
    ) -> Optional[VPNClientStatus]:
        """
            Best-effort tailscale status query for idempotency probes.
            Returns ``None`` rather than raising on failure so the
            caller can fall through to the write path.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: Parsed status, or ``None`` on any failure.
        """
        try:
            rtnval: Optional[VPNClientStatus] = self._status_tailscale()
        except MacosVPNClientQueryError:
            rtnval = None
        return rtnval

    def _safe_query_twingate_status(
        self,
        *,
        aspects: Aspects,
    ) -> Optional[VPNClientStatus]:
        """
            Best-effort twingate status query for idempotency probes.

            :param aspects: Behaviour controls forwarded to the runner.

            :returns: Parsed status, or ``None`` on any failure.
        """
        try:
            rtnval: Optional[VPNClientStatus] = self._status_twingate()
        except MacosVPNClientQueryError:
            rtnval = None
        return rtnval

    def _tailscale_state_matches(
        self,
        *,
        current: Optional[VPNClientStatus],
        hostname: Optional[str],
    ) -> bool:
        """
            Decide whether the current Tailscale state matches the
            requested bring-up parameters strongly enough to SKIP.

            :param current: Snapshot from a recent status query, or
                            ``None``.
            :param hostname: Requested hostname, or ``None``.

            :returns: True when the current state is "running" and the
                      hostname matches (or no hostname was requested).
        """
        if current is None:
            rtnval = False
            return rtnval
        if current.running is False:
            rtnval = False
            return rtnval
        if hostname is None:
            rtnval = True
            return rtnval
        if current.hostname is None:
            rtnval = False
            return rtnval
        rtnval = current.hostname == hostname
        return rtnval

    # ------------------------------------------------------------------
    # Path / URL helpers
    # ------------------------------------------------------------------

    def _tailscale_pkg_path(self, *, version: Optional[str]) -> str:
        """
            Build the staging path of the Tailscale ``.pkg``.

            :param version: Version pin, or ``None``.

            :returns: Absolute file path under the configured cache
                      directory.
        """
        if version is not None and len(version) > 0:
            file_name = "Tailscale-{}.pkg".format(version)
        else:
            file_name = "Tailscale.pkg"
        rtnval = "{}/{}".format(self._mac_config.pkg_cache_dir, file_name)
        return rtnval

    def _twingate_pkg_path(self, *, version: Optional[str]) -> str:
        """
            Build the staging path of the Twingate ``.pkg``.

            :param version: Version pin, or ``None``.

            :returns: Absolute file path under the configured cache
                      directory.
        """
        if version is not None and len(version) > 0:
            file_name = "TwingateConnector-{}.pkg".format(version)
        else:
            file_name = "TwingateConnector.pkg"
        rtnval = "{}/{}".format(self._mac_config.pkg_cache_dir, file_name)
        return rtnval

    def _tailscale_pkg_url(self, *, version: Optional[str]) -> str:
        """
            Build the download URL for the Tailscale ``.pkg``.

            :param version: Version pin, or ``None``.

            :returns: Download URL string.
        """
        if version is not None and len(version) > 0:
            rtnval = _TAILSCALE_PKG_URL_TEMPLATE.format(version=version)
            return rtnval
        rtnval = _TAILSCALE_PKG_URL_LATEST
        return rtnval

    def _twingate_pkg_url(self, *, version: Optional[str]) -> str:
        """
            Build the download URL for the Twingate connector ``.pkg``.

            :param version: Version pin, or ``None``.

            :returns: Download URL string.
        """
        if version is not None and len(version) > 0:
            rtnval = _TWINGATE_PKG_URL_TEMPLATE.format(version=version)
            return rtnval
        rtnval = _TWINGATE_PKG_URL_LATEST
        return rtnval

    def _stage_pkg_download(
        self,
        *,
        pkg_path: str,
        pkg_url: str,
        provider: VPNProvider,
    ) -> Optional[VPNActionResult]:
        """
            Ensure the staging directory exists and download the
            ``.pkg`` to ``pkg_path``.

            :param pkg_path: Local staging path for the ``.pkg``.
            :param pkg_url: Download URL.
            :param provider: Provider tag for the result, when an
                             error result is returned.

            :returns: ``None`` on success; a :class:`MacosVPNActionResult`
                      with ``status=FAILED`` when a preparatory step
                      fails.
        """
        mkdir_command = [
            "mkdir", "-p", self._mac_config.pkg_cache_dir,
        ]
        mkdir_result = self._runner.run(
            list(mkdir_command),
            elevated=self._should_elevate(kind=VERB_KIND_WRITE),
        )
        if mkdir_result.exit_code != 0:
            failed = VPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=provider,
                detail="failed to create pkg cache dir",
                exit_code=mkdir_result.exit_code,
                stdout=mkdir_result.stdout,
                stderr=mkdir_result.stderr,
                command=mkdir_command,
            )
            return failed

        curl_command = ["curl", "-fsSL", "-o", pkg_path, pkg_url]
        curl_result = self._runner.run(
            list(curl_command),
            elevated=False,
        )
        if curl_result.exit_code != 0:
            failed = VPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=provider,
                detail="failed to download pkg",
                exit_code=curl_result.exit_code,
                stdout=curl_result.stdout,
                stderr=curl_result.stderr,
                command=curl_command,
            )
            return failed

        rtnval: Optional[VPNActionResult] = None
        return rtnval

    def _require_elevation(
        self,
        *,
        provider: VPNProvider,
        command: List[str],
        operation: str,
    ) -> Optional[VPNActionResult]:
        """
            Guard a system-domain ``launchctl`` operation that cannot
            succeed without root.

            ``launchctl bootstrap/bootout system ...`` always requires
            elevation.  When the manager config has ``elevated=False``
            this returns a clear ``FAILED`` result stating elevation is
            required, rather than letting the operation fail later with
            an opaque permission error.

            :param provider: Provider tag for the result.
            :param command: The argv that would have been executed.
            :param operation: Human-readable operation name for the
                              detail text.

            :returns: A ``FAILED`` :class:`VPNActionResult` when
                      elevation is missing; otherwise ``None``.
        """
        if bool(self._config.elevated) is True:
            rtnval: Optional[VPNActionResult] = None
            return rtnval

        detail_lines = ["Elevation required for system-domain launchctl."]
        detail_lines.append("    OPERATION: {}".format(operation))
        detail_lines.append(
            "    REMEDY: set MacosVPNClientManagerConfig.elevated=True"
        )
        detail = os.linesep.join(detail_lines)

        rtnval = VPNActionResult(
            status=VPNActionStatus.FAILED,
            provider=provider,
            detail=detail,
            exit_code=None,
            stdout=None,
            stderr=None,
            command=list(command),
        )
        return rtnval

    def _wrap_run_result(
        self,
        *,
        provider: VPNProvider,
        command: List[str],
        detail: Optional[str],
        run_result: RunResult,
    ) -> VPNActionResult:
        """
            Map a :class:`RunResult` to a :class:`VPNActionResult`
            without going through ``_dispatch_write`` — used by the
            two write paths that intentionally override the elevated
            flag (Homebrew unelevated; Twingate ``tee`` carrying a
            stdin payload).

            :param provider: Provider tag for the result.
            :param command: argv recorded onto the result.
            :param detail: Optional human-readable description.
            :param run_result: The runner's output.

            :returns: A :class:`MacosVPNActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = VPNActionStatus.SUCCESS
        else:
            mapped_status = VPNActionStatus.FAILED

        rtnval = VPNActionResult(
            status=mapped_status,
            provider=provider,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _build_tailscale_up_command(
    *,
    authkey: str,
    hostname: Optional[str],
    advertise_tags: Optional[List[str]],
    advertise_routes: Optional[List[str]],
    exit_node: bool,
) -> List[str]:
    """
        Build the ``tailscale up`` argv from the supplied parameters.

        :param authkey: Vendor-issued auth key.
        :param hostname: Hostname to register, or ``None``.
        :param advertise_tags: Tags to advertise, or ``None``.
        :param advertise_routes: Subnet routes, or ``None``.
        :param exit_node: When True, advertise this host as an exit
                          node.

        :returns: The fully constructed argv list.
    """
    rtnval: List[str] = ["tailscale", "up", "--authkey={}".format(authkey)]
    if hostname is not None and len(hostname) > 0:
        rtnval.append("--hostname={}".format(hostname))
    if advertise_tags is not None and len(advertise_tags) > 0:
        tag_value = ",".join(advertise_tags)
        rtnval.append("--advertise-tags={}".format(tag_value))
    if advertise_routes is not None and len(advertise_routes) > 0:
        route_value = ",".join(advertise_routes)
        rtnval.append("--advertise-routes={}".format(route_value))
    if exit_node is True:
        rtnval.append("--advertise-exit-node")
    return rtnval


def _render_twingate_conf(*, settings: Dict[str, str]) -> str:
    """
        Render the Twingate connector configuration body from a
        settings dict.

        Each entry is emitted as ``KEY=VALUE\\n`` in sorted key order
        so byte-equality compares with on-disk content are stable.

        :param settings: Twingate-specific key/value settings.

        :returns: The rendered body, terminated with ``\\n``.
    """
    keys = sorted(settings.keys())
    body_lines: List[str] = []
    for key in keys:
        value = settings[key]
        body_lines.append("{}={}".format(key, value))
    if len(body_lines) == 0:
        rtnval = ""
        return rtnval
    rtnval = "\n".join(body_lines) + "\n"
    return rtnval


def _parse_tailscale_status_json(*, payload: str) -> VPNClientStatus:
    """
        Parse ``tailscale status --json`` stdout into a
        :class:`MacosVPNClientStatus`.

        Tailscale prints a JSON document whose top-level fields
        include ``BackendState`` (``"Running"`` when the tunnel is
        up), ``Self`` (an object with ``HostName``, ``TailscaleIPs``,
        ``DNSName``), ``Version``, ``MagicDNSSuffix``, and ``Peer``
        (a mapping of peer node id to a peer object).

        :param payload: Raw stdout text from ``tailscale status --json``.

        :returns: The parsed :class:`MacosVPNClientStatus`.
    """
    rtnval = VPNClientStatus(
        provider=VPNProvider.TAILSCALE,
        installed=True,
        running=False,
    )

    if payload is None:
        return rtnval

    stripped = payload.strip()
    if len(stripped) == 0:
        return rtnval

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return rtnval

    if not isinstance(data, dict):
        return rtnval

    backend_state = data.get("BackendState")
    if isinstance(backend_state, str) and backend_state == "Running":
        rtnval.running = True

    version_value = data.get("Version")
    if isinstance(version_value, str) and len(version_value) > 0:
        rtnval.version = version_value

    magic_dns = data.get("MagicDNSSuffix")
    if isinstance(magic_dns, str) and len(magic_dns) > 0:
        rtnval.tailnet = magic_dns

    self_entry = data.get("Self")
    if isinstance(self_entry, dict):
        host_name = self_entry.get("HostName")
        if isinstance(host_name, str) and len(host_name) > 0:
            rtnval.hostname = host_name
        ts_ips = self_entry.get("TailscaleIPs")
        if isinstance(ts_ips, list) and len(ts_ips) > 0:
            first_ip = ts_ips[0]
            if isinstance(first_ip, str) and len(first_ip) > 0:
                rtnval.self_address = first_ip

    peer_entry = data.get("Peer")
    if isinstance(peer_entry, dict):
        for _node_key, peer_record in peer_entry.items():
            if not isinstance(peer_record, dict):
                continue
            peer_host = peer_record.get("HostName")
            peer_ips = peer_record.get("TailscaleIPs")
            if not isinstance(peer_host, str) or len(peer_host) == 0:
                continue
            if not isinstance(peer_ips, list) or len(peer_ips) == 0:
                continue
            first_peer_ip = peer_ips[0]
            if not isinstance(first_peer_ip, str) or len(first_peer_ip) == 0:
                continue
            rtnval.peer_addresses[peer_host] = first_peer_ip

    return rtnval


def _parse_twingate_status_text(*, payload: str) -> VPNClientStatus:
    """
        Parse ``twingate status`` stdout into a
        :class:`MacosVPNClientStatus`.

        The ``twingate`` CLI prints a small set of free-form lines.
        We detect the running state by scanning for the literal
        substring ``"online"`` (case-insensitive) in the payload —
        ``"connector is online"`` is the canonical "up" message;
        anything else maps to ``running=False``.

        :param payload: Raw stdout text from ``twingate status``.

        :returns: The parsed :class:`MacosVPNClientStatus`.  The
                  ``self_address`` is always ``None`` and
                  ``peer_addresses`` is always empty — Twingate's hub-
                  and-spoke topology has no per-client overlay address.
    """
    rtnval = VPNClientStatus(
        provider=VPNProvider.TWINGATE,
        installed=True,
        running=False,
        self_address=None,
        peer_addresses={},
    )

    if payload is None:
        return rtnval

    lowered = payload.lower()
    if "online" in lowered:
        rtnval.running = True

    return rtnval


__all__ = [
    "InstallSource",
    "MacosVPNClientManager",
    "MacosVPNClientManagerConfig",
    "MacosVPNActionStatus",
    "MacosVPNActionResult",
    "MacosVPNClientStatus",
    "MacosVPNClientQueryError",
]
