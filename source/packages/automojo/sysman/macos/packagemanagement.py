"""
    macOS package-management feature.

    Implements the cross-OS package-management contract for macOS using
    Homebrew (``brew``).  Every verb is dispatched through an
    :class:`ICommandRunner` so the manager works identically under local
    and SSH execution.

    Elevation policy:
        **Homebrew refuses to run as root.**  This manager therefore never
        passes ``elevated=True`` to the runner — the ``config.elevated``
        flag is honoured for contract conformance but intentionally
        overridden down to ``False`` for every ``brew`` invocation by
        :meth:`_dispatch_brew`.  When the calling process IS root, the
        caller must populate ``MacosPackageManagerConfig.brew_user`` so
        every argv gets wrapped with ``sudo -u <brew_user>`` — that
        wrapping is NOT elevation of the runner; it is re-targeting the
        ``brew`` child process at a non-root user while the runner call
        itself remains ``elevated=False``.  When ``brew_user`` is ``None``
        the manager calls ``brew`` directly and assumes the calling
        process is the brew user.

    Cask handling:
        ``brew`` treats casks as a separate channel.  The manager handles
        formulae by default and treats casks as an opt-in via the config's
        ``cask_default`` flag; cask-specific argv construction is
        concentrated in :meth:`_build_install_argv` and
        :meth:`_build_uninstall_argv`.
"""

import json
import os

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_BREW = "brew"
_SUDO = "sudo"


MacosPackageActionStatus = ActionStatus


class MacosPackageKind(Enum):
    """
        The two channels Homebrew exposes.  ``FORMULA`` is the default
        command-line-tool channel; ``CASK`` is the GUI-application
        channel that installs ``.app`` bundles into ``/Applications``.
    """
    FORMULA = "formula"
    CASK    = "cask"


@dataclass
class MacosPackageManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosPackageManager`.

        :param brew_user: Optional non-root username.  When set, every
                          ``brew`` invocation is wrapped with
                          ``sudo -u <brew_user> -- <argv>`` so a caller
                          running as root can still execute ``brew``
                          under a non-root identity.  When ``None``,
                          the manager assumes the calling process IS
                          the brew user and invokes ``brew`` directly.
        :param cask_default: When True, install / remove verbs default
                             to the cask channel (``brew install
                             --cask``).  When False (the default), they
                             default to the formula channel.  Callers
                             can still override per-call via the
                             ``kind`` argument on the public verbs.
    """
    brew_user: Optional[str] = None
    cask_default: bool = False


@dataclass
class MacosPackageActionResult(FeatureActionResult):
    """
        Result of a package-management write operation.

        :param name: The package name targeted, or ``None`` for
                     ``upgrade_all`` and ``refresh_metadata``.
        :param version: The version targeted (or resolved), when known.
        :param kind: The channel the action targeted, when known.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    version: Optional[str] = None
    kind: Optional[MacosPackageKind] = None
    message: Optional[str] = None


@dataclass
class MacosPackageInfo:
    """
        Information about a package as reported by ``brew``.

        :param name: Package name.
        :param version: Installed version string (or the latest version
                        reported by ``brew info`` when not installed).
        :param kind: The channel the package belongs to.
        :param installed_on_request: True when the package was
                                     explicitly installed by the user
                                     (as opposed to being pulled in as
                                     a dependency).
        :param homepage: Upstream homepage URL when known.
        :param description: Short description when known.
    """
    name: str
    version: str
    kind: MacosPackageKind
    installed_on_request: bool = False
    homepage: Optional[str] = None
    description: Optional[str] = None


class MacosPackageQueryError(Exception):
    """
        Raised when a package read fails for a reason other than the
        package being absent from Homebrew's known set.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command: list = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Package query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosPackageManager(BaseFeatureManager):
    """
        macOS Homebrew package manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`.

        **Homebrew never runs as root.**  Every argv assembled by this
        manager is dispatched with ``elevated=False`` via
        :meth:`_dispatch_brew`, regardless of ``config.elevated``.
        When the calling process is root the caller must populate
        :attr:`MacosPackageManagerConfig.brew_user` so the manager can
        wrap every argv with ``sudo -u <brew_user>`` and execute
        ``brew`` under a non-root identity.
    """

    result_class = MacosPackageActionResult
    status_class = MacosPackageActionStatus
    feature_name = "package-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def install_package(
        self,
        name: str,
        version: Optional[str] = None,
        kind: Optional[MacosPackageKind] = None,
    ) -> MacosPackageActionResult:
        """
            Install a Homebrew package.

            Skipped when the package is already installed.  When
            ``version`` is provided, ``brew install <name>@<version>``
            is dispatched and the idempotency guard only skips when the
            installed version equals the requested version.

            :param name: The package name.
            :param version: Optional version suffix for versioned
                            formulae (``name@version``).
            :param kind: Channel override; defaults to
                        :attr:`MacosPackageManagerConfig.cask_default`.

            :returns: The :class:`MacosPackageActionResult`.
        """
        resolved_kind = self._resolve_kind(kind=kind)
        install_target = self._format_install_target(
            name=name,
            version=version,
        )
        command = self._build_install_argv(
            target=install_target,
            kind=resolved_kind,
        )

        if self._config.dry_run:
            dry_result = self._build_result(
                status=MacosPackageActionStatus.DRY_RUN,
                name=name,
                version=version,
                kind=resolved_kind,
                command=command,
            )
            return dry_result

        installed_version = self._query_installed_version(
            name=name,
            kind=resolved_kind,
        )
        if installed_version is not None:
            if version is None or installed_version == version:
                skipped = self._build_result(
                    status=MacosPackageActionStatus.SKIPPED,
                    name=name,
                    version=installed_version,
                    kind=resolved_kind,
                    command=command,
                    message="package is already installed",
                )
                return skipped

        rtnval = self._dispatch_brew(
            command=command,
            name=name,
            version=version,
            kind=resolved_kind,
            operation="install_package",
        )
        return rtnval

    def remove_package(
        self,
        name: str,
        kind: Optional[MacosPackageKind] = None,
    ) -> MacosPackageActionResult:
        """
            Remove an installed Homebrew package.  Skipped when the
            package is not installed.

            :param name: The package name.
            :param kind: Channel override; defaults to
                        :attr:`MacosPackageManagerConfig.cask_default`.

            :returns: The :class:`MacosPackageActionResult`.
        """
        resolved_kind = self._resolve_kind(kind=kind)
        command = self._build_uninstall_argv(
            name=name,
            kind=resolved_kind,
        )

        if self._config.dry_run:
            dry_result = self._build_result(
                status=MacosPackageActionStatus.DRY_RUN,
                name=name,
                kind=resolved_kind,
                command=command,
            )
            return dry_result

        if not self.is_installed(name=name, kind=resolved_kind):
            skipped = self._build_result(
                status=MacosPackageActionStatus.SKIPPED,
                name=name,
                kind=resolved_kind,
                command=command,
                message="package is not installed",
            )
            return skipped

        rtnval = self._dispatch_brew(
            command=command,
            name=name,
            kind=resolved_kind,
            operation="remove_package",
        )
        return rtnval

    def upgrade_package(
        self,
        name: str,
        kind: Optional[MacosPackageKind] = None,
    ) -> MacosPackageActionResult:
        """
            Upgrade a single package to its latest available version.

            Skipped when the package is not installed (with an explicit
            "nothing to upgrade" message) and when the package is not
            present in ``brew outdated``'s output.

            :param name: The package name.
            :param kind: Channel override; defaults to
                        :attr:`MacosPackageManagerConfig.cask_default`.

            :returns: The :class:`MacosPackageActionResult`.
        """
        resolved_kind = self._resolve_kind(kind=kind)
        command = self._build_upgrade_argv(
            name=name,
            kind=resolved_kind,
        )

        if self._config.dry_run:
            dry_result = self._build_result(
                status=MacosPackageActionStatus.DRY_RUN,
                name=name,
                kind=resolved_kind,
                command=command,
            )
            return dry_result

        installed_version = self._query_installed_version(
            name=name,
            kind=resolved_kind,
        )
        if installed_version is None:
            skipped = self._build_result(
                status=MacosPackageActionStatus.SKIPPED,
                name=name,
                kind=resolved_kind,
                command=command,
                message="package is not installed; nothing to upgrade",
            )
            return skipped

        if not self._is_outdated(name=name, kind=resolved_kind):
            skipped = self._build_result(
                status=MacosPackageActionStatus.SKIPPED,
                name=name,
                version=installed_version,
                kind=resolved_kind,
                command=command,
                message="package is already at the latest available version",
            )
            return skipped

        rtnval = self._dispatch_brew(
            command=command,
            name=name,
            kind=resolved_kind,
            operation="upgrade_package",
        )
        return rtnval

    def upgrade_all(self) -> MacosPackageActionResult:
        """
            Upgrade every installed Homebrew package to its latest
            available version.  Never skips.

            :returns: The :class:`MacosPackageActionResult`.
        """
        command = self._wrap_with_brew_user(argv=[_BREW, "upgrade"])
        rtnval = self._dispatch_brew(
            command=command,
            name=None,
            operation="upgrade_all",
        )
        return rtnval

    def refresh_metadata(self) -> MacosPackageActionResult:
        """
            Refresh Homebrew's tap index (``brew update``).  Never skips.

            :returns: The :class:`MacosPackageActionResult`.
        """
        command = self._wrap_with_brew_user(argv=[_BREW, "update"])
        rtnval = self._dispatch_brew(
            command=command,
            name=None,
            operation="refresh_metadata",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def is_installed(
        self,
        name: str,
        kind: Optional[MacosPackageKind] = None,
    ) -> bool:
        """
            Return whether a Homebrew package is currently installed.

            Dispatches ``brew list --versions [--cask] <name>`` without
            elevation and treats a zero exit with non-empty stdout as
            "installed".

            :param name: The package name.
            :param kind: Channel override; defaults to
                        :attr:`MacosPackageManagerConfig.cask_default`.

            :returns: True when ``brew list --versions`` returns non-empty
                      stdout at exit code zero; False otherwise.
        """
        resolved_kind = self._resolve_kind(kind=kind)
        command = self._build_list_versions_argv(
            name=name,
            kind=resolved_kind,
        )

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        trimmed = run_result.stdout.strip()
        if len(trimmed) == 0:
            rtnval = False
            return rtnval

        rtnval = True
        return rtnval

    def get_package_info(
        self,
        name: str,
        kind: Optional[MacosPackageKind] = None,
    ) -> Optional[MacosPackageInfo]:
        """
            Query Homebrew for package metadata via
            ``brew info --json=v2 <name>``.

            :param name: The package name.
            :param kind: Channel override; defaults to
                        :attr:`MacosPackageManagerConfig.cask_default`.

            :returns: A :class:`MacosPackageInfo` when ``brew`` recognises
                      the name; ``None`` when ``brew`` reports "No
                      available formula".
            :raises MacosPackageQueryError: When ``brew info`` fails for
                                            any other reason.
        """
        resolved_kind = self._resolve_kind(kind=kind)
        command = self._wrap_with_brew_user(
            argv=[_BREW, "info", "--json=v2", name],
        )

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            stderr_lower = run_result.stderr.lower()
            looks_missing = (
                "no available formula" in stderr_lower
                or "no available cask" in stderr_lower
                or "no formula or cask" in stderr_lower
            )
            if looks_missing:
                rtnval = None
                return rtnval
            raise MacosPackageQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        parsed = self._parse_brew_info_json(
            stdout=run_result.stdout,
            default_kind=resolved_kind,
        )
        if parsed is None:
            rtnval = None
            return rtnval

        rtnval = parsed
        return rtnval

    def list_installed(self) -> list:
        """
            List every installed formula (and every installed cask) as
            reported by ``brew list --versions`` and ``brew list
            --cask --versions``.

            :returns: One :class:`MacosPackageInfo` per installed
                      package.  Formulae come first, then casks.
            :raises MacosPackageQueryError: When either ``brew list``
                                            invocation fails.
        """
        rtnval: list = []

        formula_command = self._wrap_with_brew_user(
            argv=[_BREW, "list", "--versions"],
        )
        formula_result = self._runner.run(
            list(formula_command),
            elevated=False,
        )
        if formula_result.exit_code != 0:
            raise MacosPackageQueryError(
                command=formula_command,
                exit_code=formula_result.exit_code,
                stderr=formula_result.stderr,
            )
        formula_rows = self._parse_brew_list_versions(
            stdout=formula_result.stdout,
            kind=MacosPackageKind.FORMULA,
        )
        rtnval.extend(formula_rows)

        cask_command = self._wrap_with_brew_user(
            argv=[_BREW, "list", "--cask", "--versions"],
        )
        cask_result = self._runner.run(
            list(cask_command),
            elevated=False,
        )
        if cask_result.exit_code != 0:
            raise MacosPackageQueryError(
                command=cask_command,
                exit_code=cask_result.exit_code,
                stderr=cask_result.stderr,
            )
        cask_rows = self._parse_brew_list_versions(
            stdout=cask_result.stdout,
            kind=MacosPackageKind.CASK,
        )
        rtnval.extend(cask_rows)

        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv / dispatch / result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosPackageManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosPackageManagerConfig`.

            The base class stores the config as
            :class:`FeatureConfig`; this helper centralises the
            ``isinstance`` cast so callers can read ``brew_user`` and
            ``cask_default`` without losing static-type clarity.

            :returns: The active config as
                      :class:`MacosPackageManagerConfig`.
        """
        if isinstance(self._config, MacosPackageManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosPackageManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _resolve_kind(
        self,
        *,
        kind: Optional[MacosPackageKind],
    ) -> MacosPackageKind:
        """
            Pick the effective :class:`MacosPackageKind` for a verb
            call, falling back to the config default when the caller
            did not specify.

            :param kind: The per-call override; may be ``None``.

            :returns: The resolved kind.
        """
        if kind is not None:
            rtnval = kind
            return rtnval

        if self._typed_config().cask_default:
            rtnval = MacosPackageKind.CASK
        else:
            rtnval = MacosPackageKind.FORMULA
        return rtnval

    def _format_install_target(
        self,
        *,
        name: str,
        version: Optional[str],
    ) -> str:
        """
            Build the ``brew install`` target token.

            Homebrew's versioned-formula syntax is ``<name>@<version>``
            (e.g. ``node@18``).  For unversioned installs the plain
            name is used.

            :param name: The package name.
            :param version: Optional version suffix.

            :returns: The install target token.
        """
        if version is None:
            rtnval = name
            return rtnval
        rtnval = "{}@{}".format(name, version)
        return rtnval

    def _build_install_argv(
        self,
        *,
        target: str,
        kind: MacosPackageKind,
    ) -> list:
        """
            Assemble the full ``brew install`` argv, wrapped in the
            ``sudo -u <brew_user>`` prefix when configured.

            :param target: The install target token.
            :param kind: The channel to install from.

            :returns: The full argv.
        """
        inner_argv: list = [_BREW, "install"]
        if kind == MacosPackageKind.CASK:
            inner_argv.append("--cask")
        inner_argv.append(target)

        rtnval = self._wrap_with_brew_user(argv=inner_argv)
        return rtnval

    def _build_uninstall_argv(
        self,
        *,
        name: str,
        kind: MacosPackageKind,
    ) -> list:
        """
            Assemble the full ``brew uninstall`` argv, wrapped in the
            ``sudo -u <brew_user>`` prefix when configured.

            :param name: The package name.
            :param kind: The channel to uninstall from.

            :returns: The full argv.
        """
        inner_argv: list = [_BREW, "uninstall"]
        if kind == MacosPackageKind.CASK:
            inner_argv.append("--cask")
        inner_argv.append(name)

        rtnval = self._wrap_with_brew_user(argv=inner_argv)
        return rtnval

    def _build_upgrade_argv(
        self,
        *,
        name: str,
        kind: MacosPackageKind,
    ) -> list:
        """
            Assemble the full ``brew upgrade <name>`` argv.

            :param name: The package name.
            :param kind: The channel to upgrade on.

            :returns: The full argv.
        """
        inner_argv: list = [_BREW, "upgrade"]
        if kind == MacosPackageKind.CASK:
            inner_argv.append("--cask")
        inner_argv.append(name)

        rtnval = self._wrap_with_brew_user(argv=inner_argv)
        return rtnval

    def _build_list_versions_argv(
        self,
        *,
        name: str,
        kind: MacosPackageKind,
    ) -> list:
        """
            Assemble ``brew list --versions [--cask] <name>`` used as
            the is-installed probe.

            :param name: The package name.
            :param kind: The channel to probe.

            :returns: The full argv.
        """
        inner_argv: list = [_BREW, "list", "--versions"]
        if kind == MacosPackageKind.CASK:
            inner_argv.append("--cask")
        inner_argv.append(name)

        rtnval = self._wrap_with_brew_user(argv=inner_argv)
        return rtnval

    def _wrap_with_brew_user(self, *, argv: list) -> list:
        """
            When :attr:`MacosPackageManagerConfig.brew_user` is set,
            wrap ``argv`` with ``sudo -u <brew_user> -- <argv>``.

            The wrapping is purely about re-targeting the ``brew``
            child process at a non-root user — the runner call itself
            is still made with ``elevated=False`` (see
            :meth:`_dispatch_brew`).

            :param argv: The unwrapped argv.

            :returns: The wrapped argv (or ``argv`` unchanged when
                      ``brew_user`` is None).
        """
        typed_config = self._typed_config()
        brew_user = typed_config.brew_user
        if brew_user is None:
            rtnval = list(argv)
            return rtnval

        rtnval = [_SUDO, "-u", brew_user, "--"]
        rtnval.extend(argv)
        return rtnval

    def _dispatch_brew(
        self,
        *,
        command: list,
        name: Optional[str],
        version: Optional[str] = None,
        kind: Optional[MacosPackageKind] = None,
        operation: str,
    ) -> MacosPackageActionResult:
        """
            Dispatch a ``brew`` command through the runner.

            **Always passes ``elevated=False`` regardless of
            ``config.elevated``** because Homebrew refuses to run as
            root.  When the process is root, the argv must already
            have been wrapped by :meth:`_wrap_with_brew_user` so the
            ``brew`` child process runs under a non-root identity.

            :param command: The full argv.
            :param name: The package name to echo onto the result.
            :param version: The version to echo onto the result.
            :param kind: The channel to echo onto the result.
            :param operation: Reserved for future auto-reporting.

            :returns: The :class:`MacosPackageActionResult`.
        """
        command_copy = list(command)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosPackageActionStatus.DRY_RUN,
                name=name,
                version=version,
                kind=kind,
                command=command_copy,
            )
            return dry

        run_result = self._runner.run(
            command_copy,
            elevated=False,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosPackageActionStatus.SUCCESS
        else:
            mapped_status = MacosPackageActionStatus.FAILED

        rtnval = MacosPackageActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=command_copy,
            name=name,
            version=version,
            kind=kind,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosPackageActionStatus,
        name: Optional[str],
        version: Optional[str] = None,
        kind: Optional[MacosPackageKind] = None,
        command: list,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosPackageActionResult:
        """
            Construct a :class:`MacosPackageActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param name: The package name to echo onto the result.
            :param version: The version to echo onto the result.
            :param kind: The channel to echo onto the result.
            :param command: The would-have-been argv, copied for
                            capture.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`MacosPackageActionResult`.
        """
        rtnval = MacosPackageActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            version=version,
            kind=kind,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — idempotency probes
    # ------------------------------------------------------------------

    def _query_installed_version(
        self,
        *,
        name: str,
        kind: MacosPackageKind,
    ) -> Optional[str]:
        """
            Run ``brew list --versions <name>`` and parse the version
            token from the single expected line.

            :param name: The package name.
            :param kind: The channel to probe.

            :returns: The last whitespace-separated token on the first
                      matching line (``brew`` emits the active version
                      last); ``None`` when the package is not
                      installed.
        """
        command = self._build_list_versions_argv(
            name=name,
            kind=kind,
        )
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        rtnval = self._parse_brew_list_versions_single(
            stdout=run_result.stdout,
        )
        return rtnval

    def _is_outdated(
        self,
        *,
        name: str,
        kind: MacosPackageKind,
    ) -> bool:
        """
            Return True when ``brew outdated --json=v2 <name>`` reports
            the named package as outdated.

            :param name: The package name.
            :param kind: The channel to probe.

            :returns: True when the returned JSON lists ``name`` under
                      ``formulae`` (or ``casks``); False otherwise
                      (including on command failure, which is treated
                      conservatively as "no upgrade visible").
        """
        inner_argv: list = [_BREW, "outdated", "--json=v2"]
        if kind == MacosPackageKind.CASK:
            inner_argv.append("--cask")
        inner_argv.append(name)

        command = self._wrap_with_brew_user(argv=inner_argv)
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = False
            return rtnval

        try:
            parsed = json.loads(stripped)
        except ValueError:
            rtnval = False
            return rtnval

        if not isinstance(parsed, dict):
            rtnval = False
            return rtnval

        if kind == MacosPackageKind.CASK:
            bucket = parsed.get("casks", [])
        else:
            bucket = parsed.get("formulae", [])

        if not isinstance(bucket, list):
            rtnval = False
            return rtnval

        for entry in bucket:
            if isinstance(entry, dict):
                entry_name = entry.get("name", "")
                if entry_name == name:
                    rtnval = True
                    return rtnval

        rtnval = False
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — output parsing
    # ------------------------------------------------------------------

    def _parse_brew_list_versions_single(self, *, stdout: str) -> Optional[str]:
        """
            Parse a single ``brew list --versions <name>`` line.

            ``brew list --versions`` emits one row per package of the
            form ``<name> <version1> [<version2> ...]``.  When multiple
            versions are kegged, the active version is the last token
            on the line.

            :param stdout: Raw stdout text.

            :returns: The active version string, or ``None`` when the
                      output is empty.
        """
        for raw_line in stdout.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            tokens = stripped.split()
            if len(tokens) < 2:
                continue
            rtnval: Optional[str] = tokens[-1]
            return rtnval

        rtnval = None
        return rtnval

    def _parse_brew_list_versions(
        self,
        *,
        stdout: str,
        kind: MacosPackageKind,
    ) -> list:
        """
            Parse multi-line ``brew list --versions`` output into a
            list of :class:`MacosPackageInfo`.

            :param stdout: Raw stdout text.
            :param kind: The channel the listing came from.

            :returns: One :class:`MacosPackageInfo` per parsed row.
        """
        rtnval: list = []
        for raw_line in stdout.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            tokens = stripped.split()
            if len(tokens) < 2:
                continue
            package_name = tokens[0]
            active_version = tokens[-1]
            info = MacosPackageInfo(
                name=package_name,
                version=active_version,
                kind=kind,
                installed_on_request=False,
                homepage=None,
                description=None,
            )
            rtnval.append(info)

        return rtnval

    def _parse_brew_info_json(
        self,
        *,
        stdout: str,
        default_kind: MacosPackageKind,
    ) -> Optional[MacosPackageInfo]:
        """
            Parse ``brew info --json=v2`` output into a
            :class:`MacosPackageInfo`.

            The ``--json=v2`` payload is a dict with ``formulae`` and
            ``casks`` lists.  We return the first entry found,
            preferring the channel indicated by ``default_kind``.

            :param stdout: Raw stdout text.
            :param default_kind: The channel to check first; the other
                                 channel is also searched in case the
                                 caller didn't specify.

            :returns: The :class:`MacosPackageInfo`, or ``None`` when
                      the payload contains neither a formula nor a
                      cask entry.
        """
        try:
            parsed = json.loads(stdout)
        except ValueError:
            rtnval: Optional[MacosPackageInfo] = None
            return rtnval

        if not isinstance(parsed, dict):
            rtnval = None
            return rtnval

        formulae = parsed.get("formulae", [])
        casks = parsed.get("casks", [])

        if default_kind == MacosPackageKind.CASK:
            first_bucket = casks
            first_kind = MacosPackageKind.CASK
            second_bucket = formulae
            second_kind = MacosPackageKind.FORMULA
        else:
            first_bucket = formulae
            first_kind = MacosPackageKind.FORMULA
            second_bucket = casks
            second_kind = MacosPackageKind.CASK

        candidate = self._pick_first_info_entry(
            bucket=first_bucket,
            kind=first_kind,
        )
        if candidate is not None:
            rtnval = candidate
            return rtnval

        candidate = self._pick_first_info_entry(
            bucket=second_bucket,
            kind=second_kind,
        )
        if candidate is not None:
            rtnval = candidate
            return rtnval

        rtnval = None
        return rtnval

    def _pick_first_info_entry(
        self,
        *,
        bucket,
        kind: MacosPackageKind,
    ) -> Optional[MacosPackageInfo]:
        """
            Return the first valid :class:`MacosPackageInfo` parsed
            from a ``brew info --json=v2`` bucket (``formulae[]`` or
            ``casks[]``).

            :param bucket: The list-of-dicts extracted from the JSON
                           payload.
            :param kind: The channel each row belongs to.

            :returns: The first parsed entry, or ``None`` when the
                      bucket is empty / malformed.
        """
        if not isinstance(bucket, list):
            rtnval: Optional[MacosPackageInfo] = None
            return rtnval

        for raw_entry in bucket:
            if not isinstance(raw_entry, dict):
                continue

            info = self._build_info_from_entry(
                entry=raw_entry,
                kind=kind,
            )
            if info is not None:
                rtnval = info
                return rtnval

        rtnval = None
        return rtnval

    def _build_info_from_entry(
        self,
        *,
        entry: dict,
        kind: MacosPackageKind,
    ) -> Optional[MacosPackageInfo]:
        """
            Build a :class:`MacosPackageInfo` from one entry in a
            ``brew info --json=v2`` bucket.

            Formulae put the version under ``versions.stable``;
            casks put the version directly under ``version``.

            :param entry: The parsed dict.
            :param kind: The channel the entry came from.

            :returns: The :class:`MacosPackageInfo`, or ``None`` when
                      ``entry`` does not carry the required ``name``
                      field.
        """
        package_name = entry.get("name", "")
        if len(package_name) == 0:
            # Casks report under ``token`` rather than ``name``.
            package_name = entry.get("token", "")

        if len(package_name) == 0:
            rtnval: Optional[MacosPackageInfo] = None
            return rtnval

        version_value = ""
        if kind == MacosPackageKind.FORMULA:
            versions_block = entry.get("versions", {})
            if isinstance(versions_block, dict):
                stable_value = versions_block.get("stable", "")
                if isinstance(stable_value, str):
                    version_value = stable_value
        else:
            cask_version = entry.get("version", "")
            if isinstance(cask_version, str):
                version_value = cask_version

        installed_entries = entry.get("installed", [])
        installed_on_request = False
        if isinstance(installed_entries, list) and len(installed_entries) > 0:
            first_installed = installed_entries[0]
            if isinstance(first_installed, dict):
                flag = first_installed.get("installed_on_request", False)
                if isinstance(flag, bool):
                    installed_on_request = flag
                version_field = first_installed.get("version", "")
                if isinstance(version_field, str) and len(version_field) > 0:
                    version_value = version_field

        homepage_value = entry.get("homepage", "")
        if isinstance(homepage_value, str) and len(homepage_value) > 0:
            homepage_final: Optional[str] = homepage_value
        else:
            homepage_final = None

        description_value = entry.get("desc", "")
        if isinstance(description_value, str) and len(description_value) > 0:
            description_final: Optional[str] = description_value
        else:
            description_final = None

        rtnval = MacosPackageInfo(
            name=package_name,
            version=version_value,
            kind=kind,
            installed_on_request=installed_on_request,
            homepage=homepage_final,
            description=description_final,
        )
        return rtnval
