"""
    macOS repository-management feature.

    Implements the cross-OS repository-management contract for macOS by
    managing Homebrew taps through ``brew tap`` / ``brew untap``.  Every
    verb is dispatched through an :class:`ICommandRunner` so the manager
    works identically under local and SSH execution.

    Elevation policy:
        **Homebrew refuses to run as root.**  This manager therefore
        never passes ``elevated=True`` to the runner — the
        ``config.elevated`` flag is honoured for contract conformance
        but intentionally overridden down to ``False`` for every
        ``brew`` invocation by :meth:`_dispatch_brew`.  When the
        calling process IS root, the caller must populate
        :attr:`MacosRepositoryManagerConfig.brew_user` so every argv
        gets wrapped with ``sudo -u <brew_user>``.  That wrapping is
        NOT elevation of the runner; it is re-targeting the ``brew``
        child process at a non-root user.

    Enable / disable semantics:
        Homebrew has no "enabled but not tapped" intermediate state —
        a tap is either present (enabled) or absent (untapped).
        :meth:`enable_repository` therefore returns ``SKIPPED`` with
        an explanatory message; :meth:`disable_repository` returns
        ``FAILED`` pointing callers at :meth:`remove_repository`.
"""

import os

from dataclasses import dataclass
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_BREW = "brew"
_SUDO = "sudo"


MacosRepositoryActionStatus = ActionStatus


@dataclass
class MacosRepositoryManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosRepositoryManager`.

        :param brew_user: Optional non-root username.  When set, every
                          ``brew`` invocation is wrapped with
                          ``sudo -u <brew_user> -- <argv>`` so a caller
                          running as root can still execute ``brew``
                          under a non-root identity.  When ``None``,
                          the manager assumes the calling process IS
                          the brew user and invokes ``brew`` directly.
    """
    brew_user: Optional[str] = None


@dataclass
class MacosRepositoryActionResult(FeatureActionResult):
    """
        Result of a repository-management write operation.

        :param name: The tap name targeted (e.g. ``"homebrew/cask"``),
                     or ``None`` for :meth:`refresh_repositories`.
        :param url: The remote URL associated with the tap, when
                    applicable.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    url: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosRepositorySpec:
    """
        Specification for a Homebrew tap.

        :param name: Tap identifier in ``owner/repo`` form (e.g.
                     ``"homebrew/cask"`` or ``"hashicorp/tap"``).
        :param url: Optional Git URL when the tap is not hosted under
                    the default GitHub ``Homebrew/homebrew-<repo>`` or
                    ``<owner>/homebrew-<repo>`` path.
        :param is_cask_only: Metadata marker indicating the tap
                             publishes casks only.  Not passed to
                             ``brew tap`` (which infers kind from the
                             tap contents) but surfaced for callers
                             that want to distinguish tap kinds.
    """
    name: str
    url: Optional[str] = None
    is_cask_only: bool = False


@dataclass
class MacosRepositoryInfo:
    """
        Summary of one Homebrew tap as enumerated by
        :meth:`MacosRepositoryManager.list_repositories`.

        :param name: Tap identifier (``owner/repo``).
        :param url: Remote URL when available.  The current
                    implementation does not call ``brew tap-info`` so
                    this field is ``None`` for rows parsed from
                    ``brew tap`` alone.
        :param is_cask_only: True when the tap is known to publish
                             casks only.  The current implementation
                             leaves this field ``False`` since
                             ``brew tap`` does not expose per-tap
                             kind metadata.
    """
    name: str
    url: Optional[str] = None
    is_cask_only: bool = False


class MacosRepositoryQueryError(Exception):
    """
        Raised when a repository read fails for a reason other than
        the target simply being absent.
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

        err_msg_lines = ["Repository query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosRepositoryManager(BaseFeatureManager):
    """
        macOS Homebrew tap manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`.

        **Homebrew never runs as root.**  Every argv assembled by this
        manager is dispatched with ``elevated=False`` via
        :meth:`_dispatch_brew`, regardless of ``config.elevated``.
        When the calling process is root the caller must populate
        :attr:`MacosRepositoryManagerConfig.brew_user` so the manager
        can wrap every argv with ``sudo -u <brew_user>`` and execute
        ``brew`` under a non-root identity.
    """

    result_class = MacosRepositoryActionResult
    status_class = MacosRepositoryActionStatus
    feature_name = "repository-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_repository(
        self,
        spec: MacosRepositorySpec,
    ) -> MacosRepositoryActionResult:
        """
            Add a Homebrew tap.

            Skipped when the tap is already present in ``brew tap``
            output.  When ``spec.url`` is set, the URL is passed as the
            second positional argument to ``brew tap``.

            :param spec: The :class:`MacosRepositorySpec` to tap.

            :returns: The :class:`MacosRepositoryActionResult`.
        """
        command = self._build_tap_argv(spec=spec)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosRepositoryActionStatus.DRY_RUN,
                name=spec.name,
                url=spec.url,
                command=command,
            )
            return dry

        existing_taps = self._query_tap_names()
        if spec.name in existing_taps:
            skipped = self._build_result(
                status=MacosRepositoryActionStatus.SKIPPED,
                name=spec.name,
                url=spec.url,
                command=command,
                message="tap is already present",
            )
            return skipped

        rtnval = self._dispatch_brew(
            command=command,
            name=spec.name,
            url=spec.url,
            operation="add_repository",
        )
        return rtnval

    def remove_repository(
        self,
        name: str,
    ) -> MacosRepositoryActionResult:
        """
            Remove a Homebrew tap via ``brew untap``.  Skipped when the
            tap is not currently present.

            :param name: The tap identifier (``owner/repo``).

            :returns: The :class:`MacosRepositoryActionResult`.
        """
        command = self._wrap_with_brew_user(argv=[_BREW, "untap", name])

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosRepositoryActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        existing_taps = self._query_tap_names()
        if name not in existing_taps:
            skipped = self._build_result(
                status=MacosRepositoryActionStatus.SKIPPED,
                name=name,
                command=command,
                message="tap is not present",
            )
            return skipped

        rtnval = self._dispatch_brew(
            command=command,
            name=name,
            operation="remove_repository",
        )
        return rtnval

    def enable_repository(
        self,
        name: str,
    ) -> MacosRepositoryActionResult:
        """
            Enable a tap.

            Homebrew has no "enabled-but-not-tapped" intermediate
            state: a tap is either present or absent.  This verb is
            therefore a documented no-op that returns ``SKIPPED``
            with an explanatory message so cross-OS callers do not
            need to branch on OS.

            :param name: The tap identifier.

            :returns: The :class:`MacosRepositoryActionResult`.
        """
        # Reflect the no-op status via a would-have-been argv so
        # dry-run / audit tooling still captures something meaningful.
        command = [_BREW, "tap", name]

        message = (
            "homebrew taps are always enabled when present; "
            "enable_repository is a no-op"
        )
        rtnval = self._build_result(
            status=MacosRepositoryActionStatus.SKIPPED,
            name=name,
            command=command,
            message=message,
        )
        return rtnval

    def disable_repository(
        self,
        name: str,
    ) -> MacosRepositoryActionResult:
        """
            Disable a tap.

            Homebrew has no concept of disabling a tap without
            untapping it: the only way to stop consulting a tap is
            :meth:`remove_repository`.  This verb therefore returns
            ``FAILED`` with an explanatory message rather than silently
            succeeding, so callers are alerted that the requested
            state change is not expressible.

            :param name: The tap identifier.

            :returns: The :class:`MacosRepositoryActionResult`.
        """
        command = [_BREW, "untap", name]

        message = (
            "homebrew_no_disable: untap_required "
            "(use remove_repository to stop consulting the tap)"
        )
        rtnval = self._build_result(
            status=MacosRepositoryActionStatus.FAILED,
            name=name,
            command=command,
            message=message,
            stderr=message,
        )
        return rtnval

    def refresh_repositories(self) -> MacosRepositoryActionResult:
        """
            Refresh Homebrew's tap index via ``brew update``.  Never
            skips.

            :returns: The :class:`MacosRepositoryActionResult`.
        """
        command = self._wrap_with_brew_user(argv=[_BREW, "update"])
        rtnval = self._dispatch_brew(
            command=command,
            name=None,
            operation="refresh_repositories",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_repositories(self) -> list:
        """
            Enumerate every Homebrew tap.

            Runs ``brew tap`` (no elevation) and parses the one-name
            per-line output into :class:`MacosRepositoryInfo` rows.
            The URL and cask-only fields are left at their defaults
            because ``brew tap`` does not emit that metadata; callers
            that need URL information can invoke ``brew tap-info`` via
            a future extension.

            :returns: One :class:`MacosRepositoryInfo` per tap.
            :raises MacosRepositoryQueryError: When ``brew tap`` exits
                                               non-zero.
        """
        command = self._wrap_with_brew_user(argv=[_BREW, "tap"])
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise MacosRepositoryQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_brew_tap_output(stdout=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv / dispatch / result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosRepositoryManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosRepositoryManagerConfig`.

            :returns: The active config as
                      :class:`MacosRepositoryManagerConfig`.
        """
        if isinstance(self._config, MacosRepositoryManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosRepositoryManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_tap_argv(
        self,
        *,
        spec: MacosRepositorySpec,
    ) -> list:
        """
            Assemble the ``brew tap <name> [<url>]`` argv.

            :param spec: The :class:`MacosRepositorySpec`.

            :returns: The full argv, wrapped with ``sudo -u
                      <brew_user>`` when configured.
        """
        inner_argv: list = [_BREW, "tap", spec.name]
        if spec.url is not None and len(spec.url) > 0:
            inner_argv.append(spec.url)

        rtnval = self._wrap_with_brew_user(argv=inner_argv)
        return rtnval

    def _wrap_with_brew_user(self, *, argv: list) -> list:
        """
            When :attr:`MacosRepositoryManagerConfig.brew_user` is set,
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
        url: Optional[str] = None,
        operation: str,
    ) -> MacosRepositoryActionResult:
        """
            Dispatch a ``brew`` command through the runner.

            **Always passes ``elevated=False`` regardless of
            ``config.elevated``** because Homebrew refuses to run as
            root.  When the process is root, the argv must already
            have been wrapped by :meth:`_wrap_with_brew_user` so the
            ``brew`` child process runs under a non-root identity.

            :param command: The full argv.
            :param name: The tap name to echo onto the result.
            :param url: The tap URL to echo onto the result.
            :param operation: Reserved for future auto-reporting.

            :returns: The :class:`MacosRepositoryActionResult`.
        """
        command_copy = list(command)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosRepositoryActionStatus.DRY_RUN,
                name=name,
                url=url,
                command=command_copy,
            )
            return dry

        run_result = self._runner.run(
            command_copy,
            elevated=False,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosRepositoryActionStatus.SUCCESS
        else:
            mapped_status = MacosRepositoryActionStatus.FAILED

        rtnval = MacosRepositoryActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=command_copy,
            name=name,
            url=url,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosRepositoryActionStatus,
        name: Optional[str],
        command: list,
        url: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosRepositoryActionResult:
        """
            Construct a :class:`MacosRepositoryActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param name: The tap name to echo onto the result.
            :param command: The would-have-been argv, copied for
                            capture.
            :param url: The tap URL to echo onto the result.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`MacosRepositoryActionResult`.
        """
        rtnval = MacosRepositoryActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            url=url,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — idempotency probes
    # ------------------------------------------------------------------

    def _query_tap_names(self) -> list:
        """
            Run ``brew tap`` and return the list of tap names.

            ``brew tap`` returns the already-tapped repositories, one
            per line.  On non-zero exit we return an empty list rather
            than raising, because the idempotency guard should let the
            subsequent dispatch fail (and surface the failure) instead
            of short-circuiting.

            :returns: The list of tap names, possibly empty.
        """
        command = self._wrap_with_brew_user(argv=[_BREW, "tap"])
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval

        rtnval = self._parse_brew_tap_names(stdout=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — output parsing
    # ------------------------------------------------------------------

    def _parse_brew_tap_names(self, *, stdout: str) -> list:
        """
            Extract the list of tap-name strings from ``brew tap``
            output.

            :param stdout: Raw stdout text.

            :returns: One name per non-empty line, in listing order.
        """
        rtnval: list = []
        for raw_line in stdout.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            rtnval.append(stripped)
        return rtnval

    def _parse_brew_tap_output(self, *, stdout: str) -> list:
        """
            Parse ``brew tap`` output into a list of
            :class:`MacosRepositoryInfo`.

            :param stdout: Raw stdout text.

            :returns: One :class:`MacosRepositoryInfo` per tap.
        """
        rtnval: list = []
        for raw_line in stdout.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            info = MacosRepositoryInfo(
                name=stripped,
                url=None,
                is_cask_only=False,
            )
            rtnval.append(info)
        return rtnval
