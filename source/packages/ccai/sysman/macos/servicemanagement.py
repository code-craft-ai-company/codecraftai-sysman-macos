"""
    macOS service-management feature.

    Implements the cross-OS service-management contract for macOS using
    ``launchctl``.  Every verb is dispatched through an
    :class:`ICommandRunner` so the manager works identically under local and
    SSH execution.

    Scope mapping:
        ``MacosServiceScope.SYSTEM`` -> domain ``"system"``
        ``MacosServiceScope.USER``   -> domain ``f"gui/{config.target_uid}"``

    The ``user/<uid>`` background-user domain is also a valid macOS launchd
    domain target.  This pilot uses ``gui/<uid>`` as the primary user-scope
    domain because GUI-session agents are by far the most common
    user-scoped target on macOS workstations; callers needing the
    background-user domain can subclass and override
    :func:`_resolve_user_domain`.

    Elevation policy:
        SYSTEM-scope writes elevate when ``config.elevated`` is True.
        USER-scope writes never elevate (per-user launchd domains accept
        no ``sudo``).  Reads and idempotency-probe commands never elevate.

    Notes on command surface:
        - ``start_service`` and ``restart_service`` both use
          ``launchctl kickstart -k`` because that is the canonical way to
          deliver "start" semantics to an already-bootstrapped job in
          modern macOS (10.10+).  The ``-k`` flag forces a kill+restart,
          which is also the correct primitive for ``restart_service``.
          Bootstrapping a not-yet-loaded plist is out of scope for this
          pilot (see Follow-ups in ``specs/3-service-management/tasks.md``).
        - ``mask_service`` and ``unmask_service`` are emulated via
          ``disable`` / ``enable`` because launchd has no first-class
          ``mask`` primitive.  See the docstrings on those methods for the
          implications and the spec follow-up for the override-plist
          fallback.
        - ``reload_daemon`` is a SUCCESS no-op: launchd re-reads plists at
          ``bootstrap`` time, so there is no equivalent to systemd's
          ``daemon-reload``.
"""

import os

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_LAUNCHCTL = "launchctl"

# Active-state strings parsed out of ``launchctl print`` output and surfaced
# on :class:`MacosServiceStatus`.
_ACTIVE_STATE_RUNNING = "running"
_ACTIVE_STATE_NOT_RUNNING = "not-running"
_ACTIVE_STATE_NOT_FOUND = "not-found"

# Enabled-state strings surfaced on :class:`MacosServiceStatus`.
_ENABLED_STATE_ENABLED = "enabled"
_ENABLED_STATE_DISABLED = "disabled"
_ENABLED_STATE_NOT_FOUND = "not-found"

# Skip-state sets used by the idempotency guards.
_DISABLE_SKIP_STATES = frozenset({_ENABLED_STATE_DISABLED})


MacosServiceActionStatus = ActionStatus


class MacosServiceScope(Enum):
    """
        Scope selector for the macOS service manager.  Maps to a launchd
        domain string at write/read time:

        - ``SYSTEM`` -> ``"system"``
        - ``USER``   -> ``f"gui/{config.target_uid}"``
    """
    SYSTEM = "system"
    USER   = "user"


@dataclass
class MacosServiceManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosServiceManager`.  Inherits ``dry_run``
        and ``elevated`` from :class:`FeatureConfig`.

        :param target_uid: Numeric UID used to construct the
                           ``gui/<uid>`` domain when the caller targets
                           USER scope.  Required for any USER-scope call;
                           the manager raises
                           :class:`MacosServiceQueryError` otherwise.
        :param default_plist_dir: Directory where system-scope launchd
                                  plists conventionally live.  Reserved
                                  for the future ``bootstrap`` fallback
                                  in ``start_service``; not consumed by
                                  the current verbs.
        :param user_plist_dir: Directory where user-scope launchd plists
                               conventionally live.  Reserved for the
                               same future ``bootstrap`` fallback.
    """
    target_uid: Optional[int] = None
    default_plist_dir: str = "/Library/LaunchDaemons"
    user_plist_dir: str = "/Library/LaunchAgents"


@dataclass
class MacosServiceActionResult(FeatureActionResult):
    """
        Result of a service-management write operation.

        :param service_name: The service targeted.  Empty string for
                             :meth:`MacosServiceManager.reload_daemon`.
        :param scope: The scope targeted.
        :param message: Optional human-readable detail for ``SKIPPED`` /
                        ``FAILED`` outcomes (e.g. "service is already
                        running", "service is not active; cannot
                        reload").
    """
    service_name: str = ""
    scope: Optional[MacosServiceScope] = None
    message: Optional[str] = None


@dataclass
class MacosServiceStatus:
    """
        Current state of a single launchd job.

        :param service_name: The service queried.
        :param scope: The scope queried.
        :param domain: The fully resolved launchd domain string
                       (``"system"`` or ``"gui/<uid>"``).
        :param active_state: One of ``"running"``, ``"not-running"``,
                             ``"not-found"``.
        :param enabled_state: One of ``"enabled"``, ``"disabled"``,
                              ``"not-found"``.
        :param main_pid: Main process PID when running; otherwise None.
        :param last_exit_code: ``last exit code`` field from
                               ``launchctl print``, when present.
        :param path: ``path`` (or ``program``) field from
                     ``launchctl print``, when present.
    """
    service_name: str
    scope: MacosServiceScope
    domain: str
    active_state: str
    enabled_state: str
    main_pid: Optional[int] = None
    last_exit_code: Optional[int] = None
    path: Optional[str] = None


@dataclass
class MacosServiceInfo:
    """
        One row from ``launchctl list`` (PID / Status / Label, tab
        separated).

        :param service_name: The Label column value.
        :param scope: The scope the listing was taken from.
        :param domain: The launchd domain the listing was taken from.
        :param pid: Numeric PID when the row reports one; otherwise None
                    (``-`` in the ``launchctl list`` PID column).
        :param status: Numeric Status column value when present;
                       otherwise None (``-`` in the Status column).
    """
    service_name: str
    scope: MacosServiceScope
    domain: str
    pid: Optional[int]
    status: Optional[int]


class MacosServiceQueryError(Exception):
    """
        Raised when a service read fails for an unrecoverable reason
        (non-zero exit + unrecognised stderr from ``launchctl``) or when
        the caller requests a USER-scope operation without configuring
        ``target_uid``.
    """

    def __init__(
        self,
        *,
        command: Optional[list[str]] = None,
        exit_code: Optional[int] = None,
        stderr: str = "",
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed (when relevant).
            :param exit_code: The runner's exit code (when relevant).
            :param stderr: The runner's captured stderr (when relevant).
            :param message: Optional pre-built message used for the
                            ``target_uid is None`` case where there is
                            no failed argv to report.
        """
        if command is None:
            self.command: list[str] = []
        else:
            self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Service query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(
                " ".join(self.command),
            ))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosServiceManager(BaseFeatureManager):
    """
        macOS launchd service manager.  Every verb is scoped by a
        :class:`MacosServiceScope` and dispatched through the injected
        :class:`ICommandRunner`.
    """

    result_class = MacosServiceActionResult
    status_class = MacosServiceActionStatus
    feature_name = "service-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def start_service(
        self,
        name: str,
        scope: MacosServiceScope,
    ) -> MacosServiceActionResult:
        """
            Start a service.  Skipped when the service is already running.

            Implementation note: macOS uses
            ``launchctl kickstart -k <domain>/<name>`` to deliver
            "start" semantics for an already-bootstrapped launchd job.
            For services that are not yet loaded into the domain,
            ``launchctl bootstrap <domain> <plist_path>`` is the correct
            primitive — but that path is out of scope for this pilot
            (see ``specs/3-service-management/tasks.md`` follow-ups).

            :param name: The launchd job label.
            :param scope: The scope selector.

            :returns: The :class:`MacosServiceActionResult`.
        """
        domain = self._resolve_domain(scope=scope)
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "kickstart", "-k", target]

        skip_result = self._maybe_skip_on_active(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_ACTIVE_STATE_RUNNING},
            skip_message="service is already running",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="start_service",
        )
        return rtnval

    def stop_service(
        self,
        name: str,
        scope: MacosServiceScope,
    ) -> MacosServiceActionResult:
        """
            Stop a service.  Skipped when the service is not running.

            ``launchctl bootout <domain>/<name>`` removes the job from
            the domain, which is the launchd primitive that maps to
            "stop".

            :param name: The launchd job label.
            :param scope: The scope selector.

            :returns: The :class:`MacosServiceActionResult`.
        """
        domain = self._resolve_domain(scope=scope)
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "bootout", target]

        skip_result = self._maybe_skip_on_active(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={
                _ACTIVE_STATE_NOT_RUNNING,
                _ACTIVE_STATE_NOT_FOUND,
            },
            skip_message="service is already stopped",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="stop_service",
        )
        return rtnval

    def restart_service(
        self,
        name: str,
        scope: MacosServiceScope,
    ) -> MacosServiceActionResult:
        """
            Restart a service.  Never skips.

            ``launchctl kickstart -k`` forces a kill+start on an
            already-bootstrapped job, which is the canonical "restart"
            primitive on macOS.

            :param name: The launchd job label.
            :param scope: The scope selector.

            :returns: The :class:`MacosServiceActionResult`.
        """
        domain = self._resolve_domain(scope=scope)
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "kickstart", "-k", target]

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="restart_service",
        )
        return rtnval

    def reload_service(
        self,
        name: str,
        scope: MacosServiceScope,
    ) -> MacosServiceActionResult:
        """
            Send a ``SIGHUP`` to a running service.  Returns ``FAILED``
            without invoking the runner when the service is not currently
            running (parallel to systemd's "reload requires a running
            service" rule).  Dry-run short-circuits before the
            precondition check per the cross-OS contract.

            :param name: The launchd job label.
            :param scope: The scope selector.

            :returns: The :class:`MacosServiceActionResult`.
        """
        domain = self._resolve_domain(scope=scope)
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "kill", "SIGHUP", target]

        if self._config.dry_run:
            rtnval = self._build_result(
                status=MacosServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return rtnval

        status_info = self.get_service_status(name=name, scope=scope)
        if status_info.active_state != _ACTIVE_STATE_RUNNING:
            message = "service is not active; cannot reload"
            stderr_text = "service is not active: {}".format(
                status_info.active_state,
            )
            rtnval = self._build_result(
                status=MacosServiceActionStatus.FAILED,
                name=name,
                scope=scope,
                command=command,
                message=message,
                stderr=stderr_text,
            )
            return rtnval

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="reload_service",
        )
        return rtnval

    def enable_service(
        self,
        name: str,
        scope: MacosServiceScope,
    ) -> MacosServiceActionResult:
        """
            Enable a service so launchd will load it at the next
            ``bootstrap``.  Skipped when the service is already enabled
            (i.e., not present in the domain's disabled set).

            :param name: The launchd job label.
            :param scope: The scope selector.

            :returns: The :class:`MacosServiceActionResult`.
        """
        domain = self._resolve_domain(scope=scope)
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "enable", target]

        skip_result = self._maybe_skip_on_enabled(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_ENABLED_STATE_ENABLED},
            skip_message="service is already enabled",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="enable_service",
        )
        return rtnval

    def disable_service(
        self,
        name: str,
        scope: MacosServiceScope,
    ) -> MacosServiceActionResult:
        """
            Disable a service so launchd will refuse to load it.
            Skipped when the service is already disabled.

            :param name: The launchd job label.
            :param scope: The scope selector.

            :returns: The :class:`MacosServiceActionResult`.
        """
        domain = self._resolve_domain(scope=scope)
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "disable", target]

        skip_result = self._maybe_skip_on_enabled(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in=_DISABLE_SKIP_STATES,
            skip_message="service is already disabled",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="disable_service",
        )
        return rtnval

    def mask_service(
        self,
        name: str,
        scope: MacosServiceScope,
    ) -> MacosServiceActionResult:
        """
            Mask a service.

            macOS / launchd has no first-class ``mask`` primitive: the
            closest analogue is ``launchctl disable``, which prevents
            the job from loading.  This implementation aliases
            ``mask_service`` to ``disable`` for cross-OS consistency
            with the contract; mask state is therefore equivalent to
            disable state on macOS.  Skipped when the service is
            already disabled (== masked in this fallback).

            See ``specs/3-service-management/tasks.md`` follow-ups for
            the override-plist (``Disabled=true``) fallback that the
            spec documents but this pilot does not implement.

            :param name: The launchd job label.
            :param scope: The scope selector.

            :returns: The :class:`MacosServiceActionResult`.
        """
        domain = self._resolve_domain(scope=scope)
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "disable", target]

        skip_result = self._maybe_skip_on_enabled(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_ENABLED_STATE_DISABLED},
            skip_message="service is already masked",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="mask_service",
        )
        return rtnval

    def unmask_service(
        self,
        name: str,
        scope: MacosServiceScope,
    ) -> MacosServiceActionResult:
        """
            Unmask a service.

            Inverse of :meth:`mask_service`.  Aliases to
            ``launchctl enable`` because mask is implemented as disable
            on macOS.  Skipped when the service is already enabled
            (i.e., not currently masked).

            See :meth:`mask_service` for the cross-OS rationale.

            :param name: The launchd job label.
            :param scope: The scope selector.

            :returns: The :class:`MacosServiceActionResult`.
        """
        domain = self._resolve_domain(scope=scope)
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "enable", target]

        skip_result = self._maybe_skip_on_enabled(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={_ENABLED_STATE_ENABLED},
            skip_message="service is not masked",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="unmask_service",
        )
        return rtnval

    def reload_daemon(
        self,
        scope: MacosServiceScope,
    ) -> MacosServiceActionResult:
        """
            Reload the launchd manager configuration.

            macOS launchd does NOT have an equivalent of systemd's
            ``daemon-reload``: plist files are re-read on
            ``launchctl bootstrap``.  This method returns SUCCESS with a
            descriptive message and does NOT invoke the runner, so that
            cross-OS callers can call ``reload_daemon`` blindly without
            paying for a launchd no-op or branching on OS in the
            coordinator.  Honours dry-run.

            :param scope: The scope selector.

            :returns: The :class:`MacosServiceActionResult`.
        """
        # Resolve the domain primarily for validation -- it raises on a
        # USER-scope call without target_uid before we report SUCCESS, so
        # callers cannot use this verb as a backdoor scope test.
        domain = self._resolve_domain(scope=scope)

        command = [_LAUNCHCTL, "print", domain]

        if self._config.dry_run:
            rtnval = self._build_result(
                status=MacosServiceActionStatus.DRY_RUN,
                name="",
                scope=scope,
                command=command,
            )
            return rtnval

        message = (
            "macOS launchd reloads plists at bootstrap; reload_daemon is a no-op."
        )
        rtnval = self._build_result(
            status=MacosServiceActionStatus.SUCCESS,
            name="",
            scope=scope,
            command=command,
            message=message,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_service_status(
        self,
        name: str,
        scope: MacosServiceScope,
    ) -> MacosServiceStatus:
        """
            Query a service's current state.

            Runs ``launchctl print <domain>/<name>`` (no elevation) and
            parses the multi-line output for ``state``, ``pid``,
            ``last exit code``, ``program``/``path``, and ``disabled``.
            When ``print`` reports the service missing, returns a
            synthetic ``not-found`` :class:`MacosServiceStatus` rather
            than raising.

            :param name: The launchd job label.
            :param scope: The scope selector.

            :returns: The :class:`MacosServiceStatus`.
            :raises MacosServiceQueryError: When ``print`` exits non-zero
                                            with an unrecognised stderr.
        """
        domain = self._resolve_domain(scope=scope)
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "print", target]

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            stderr_lower = run_result.stderr.lower()
            looks_not_found = (
                "could not find service" in stderr_lower
                or "service not found" in stderr_lower
                or "no such service" in stderr_lower
            )
            if looks_not_found:
                rtnval = MacosServiceStatus(
                    service_name=name,
                    scope=scope,
                    domain=domain,
                    active_state=_ACTIVE_STATE_NOT_FOUND,
                    enabled_state=_ENABLED_STATE_NOT_FOUND,
                    main_pid=None,
                    last_exit_code=None,
                    path=None,
                )
                return rtnval

            raise MacosServiceQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        properties = self._parse_print_output(output=run_result.stdout)

        active_state = self._derive_active_state(properties=properties)
        enabled_state = self._derive_enabled_state(properties=properties)
        main_pid = self._derive_pid(properties=properties)
        last_exit_code = self._derive_last_exit_code(properties=properties)
        path = self._derive_path(properties=properties)

        rtnval = MacosServiceStatus(
            service_name=name,
            scope=scope,
            domain=domain,
            active_state=active_state,
            enabled_state=enabled_state,
            main_pid=main_pid,
            last_exit_code=last_exit_code,
            path=path,
        )
        return rtnval

    def list_services(
        self,
        scope: MacosServiceScope,
    ) -> list[MacosServiceInfo]:
        """
            List all services for the given scope.

            Runs ``launchctl list``.  Per macOS, ``launchctl list``
            does not take a domain argument from the command line but
            implicitly targets the calling user's domain — for SSH /
            cross-user invocation the runner can wrap the command via
            ``launchctl asuser <uid>``; that is out of scope for the
            pilot.  The output is tab-separated with a single header
            row: ``PID\tStatus\tLabel``.  Never elevates.

            :param scope: The scope selector.

            :returns: One :class:`MacosServiceInfo` per row.
            :raises MacosServiceQueryError: When the command fails.
        """
        # Resolve the domain for validation + to stamp on each row, even
        # though ``launchctl list`` does not take it as an argument.
        domain = self._resolve_domain(scope=scope)
        command = [_LAUNCHCTL, "list"]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosServiceQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_list_output(
            output=run_result.stdout,
            scope=scope,
            domain=domain,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — domain / dispatch / result construction
    # ------------------------------------------------------------------

    def _resolve_domain(self, *, scope: MacosServiceScope) -> str:
        """
            Resolve the launchd domain string for the given scope.

            :param scope: The scope selector.

            :returns: ``"system"`` for SYSTEM, ``"gui/<uid>"`` for USER.
            :raises ValueError: When ``scope`` is not a recognised
                                :class:`MacosServiceScope` member.
            :raises MacosServiceQueryError: When ``scope`` is USER and
                                            ``config.target_uid`` is None.
        """
        if not isinstance(scope, MacosServiceScope):
            err_msg = "Unsupported service scope: {!r}".format(scope)
            raise ValueError(err_msg)

        if scope == MacosServiceScope.SYSTEM:
            rtnval = "system"
        elif scope == MacosServiceScope.USER:
            target_uid = self._typed_config().target_uid
            if target_uid is None:
                err_msg_lines = [
                    "USER-scope service operations require "
                    "MacosServiceManagerConfig.target_uid to be set.",
                ]
                err_msg_lines.append(
                    "    target_uid: None",
                )
                err_msg = os.linesep.join(err_msg_lines)
                raise MacosServiceQueryError(message=err_msg)
            rtnval = "gui/{}".format(target_uid)
        else:
            # Defensive: enum extension without matching branch above.
            err_msg = "Unhandled service scope: {!r}".format(scope)
            raise ValueError(err_msg)

        return rtnval

    def _typed_config(self) -> MacosServiceManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosServiceManagerConfig`.

            The base class stores it as :class:`FeatureConfig` for
            type-checker friendliness.  This helper centralises the
            ``isinstance`` cast so callers can read ``target_uid``
            without losing static-type clarity.

            :returns: The active config typed as
                      :class:`MacosServiceManagerConfig`.
        """
        if isinstance(self._config, MacosServiceManagerConfig):
            rtnval = self._config
        else:
            # Fallback when the caller passed a plain FeatureConfig: read
            # ``target_uid`` from a synthesised default-bearing config so
            # the rest of the manager code does not need to special-case.
            rtnval = MacosServiceManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        name: str,
        scope: MacosServiceScope,
        operation: str,
    ) -> MacosServiceActionResult:
        """
            Dispatch a write command, applying the scope-based elevation
            policy and short-circuiting dry-run via the base
            :meth:`_dispatch`.

            :param command: The full argv.
            :param name: The service name to echo onto the result.
            :param scope: The scope targeted.
            :param operation: Reserved for future auto-reporting.

            :returns: The :class:`MacosServiceActionResult`.
        """
        if scope == MacosServiceScope.USER:
            elevated_override = False
        else:
            elevated_override = None

        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation=operation,
            elevated_override=elevated_override,
            service_name=name,
            scope=scope,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosServiceActionStatus,
        name: str,
        scope: MacosServiceScope,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosServiceActionResult:
        """
            Construct a :class:`MacosServiceActionResult` without
            dispatching.

            :param status: The outcome status.
            :param name: The service name.
            :param scope: The scope targeted.
            :param command: The would-have-been argv.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`MacosServiceActionResult`.
        """
        rtnval = MacosServiceActionResult(
            status=status,
            detail=name,
            command=list(command),
            service_name=name,
            scope=scope,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _maybe_skip_on_active(
        self,
        *,
        name: str,
        scope: MacosServiceScope,
        command: list[str],
        skip_when_state_in,
        skip_message: str,
    ) -> Optional[MacosServiceActionResult]:
        """
            Evaluate the dry-run short-circuit and the ``active_state``
            idempotency guard.

            :param name: The service name.
            :param scope: The scope targeted.
            :param command: The would-have-been argv, for dry-run capture.
            :param skip_when_state_in: States that trigger ``SKIPPED``.
            :param skip_message: Human-readable reason for the skip.

            :returns: A result when the dispatch should short-circuit;
                      None when the caller should proceed with the write.
        """
        if self._config.dry_run:
            dry_result = self._build_result(
                status=MacosServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return dry_result

        status_info = self.get_service_status(name=name, scope=scope)
        if status_info.active_state in skip_when_state_in:
            skip_result = self._build_result(
                status=MacosServiceActionStatus.SKIPPED,
                name=name,
                scope=scope,
                command=command,
                message=skip_message,
            )
            return skip_result

        return None

    def _maybe_skip_on_enabled(
        self,
        *,
        name: str,
        scope: MacosServiceScope,
        command: list[str],
        skip_when_state_in,
        skip_message: str,
    ) -> Optional[MacosServiceActionResult]:
        """
            Evaluate the dry-run short-circuit and the ``enabled_state``
            idempotency guard.

            :param name: The service name.
            :param scope: The scope targeted.
            :param command: The would-have-been argv, for dry-run capture.
            :param skip_when_state_in: States that trigger ``SKIPPED``.
            :param skip_message: Human-readable reason for the skip.

            :returns: A result when the dispatch should short-circuit;
                      None when the caller should proceed with the write.
        """
        if self._config.dry_run:
            dry_result = self._build_result(
                status=MacosServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return dry_result

        status_info = self.get_service_status(name=name, scope=scope)
        if status_info.enabled_state in skip_when_state_in:
            skip_result = self._build_result(
                status=MacosServiceActionStatus.SKIPPED,
                name=name,
                scope=scope,
                command=command,
                message=skip_message,
            )
            return skip_result

        return None

    # ------------------------------------------------------------------
    # Internal helpers — output parsing
    # ------------------------------------------------------------------

    def _parse_print_output(self, *, output: str) -> dict:
        """
            Parse ``launchctl print <domain>/<name>`` output into a
            ``key -> value`` dict.

            ``launchctl print`` emits a multi-line text block of the form

                ::

                    com.apple.something = {
                            state = running
                            pid = 12345
                            program = /usr/bin/...
                            last exit code = 0
                            ...
                    }

            This parser splits each line on the FIRST ``=``, strips
            whitespace, and discards lines that do not contain ``=``.
            Brace-only lines and blank lines are skipped.  Tolerant by
            design: missing or extra fields are simply absent /
            irrelevant in the returned dict.

            :param output: Raw stdout text.

            :returns: A flat ``dict[str, str]`` of stripped key-value
                      pairs.
        """
        rtnval: dict = {}
        for raw_line in output.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            if stripped == "{" or stripped == "}":
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            cleaned_key = key.strip()
            cleaned_value = value.strip().rstrip(";")
            if len(cleaned_key) == 0:
                continue
            # Strip trailing brace fragments that show up on
            # one-line "key = { ..." headers.
            cleaned_value = cleaned_value.rstrip("{").strip()
            rtnval[cleaned_key] = cleaned_value
        return rtnval

    def _derive_active_state(self, *, properties: dict) -> str:
        """
            Derive the ``active_state`` field from parsed ``print``
            properties.

            ``launchctl print`` reports ``state = running`` for an
            actively running job and ``state = not running`` (or omits
            the field) otherwise.  We normalise the latter into
            ``"not-running"`` to match the documented model.

            :param properties: Parsed key-value map from
                               :meth:`_parse_print_output`.

            :returns: One of ``"running"``, ``"not-running"``.
        """
        state_raw = properties.get("state", "").strip()
        if state_raw == "running":
            rtnval = _ACTIVE_STATE_RUNNING
        else:
            rtnval = _ACTIVE_STATE_NOT_RUNNING
        return rtnval

    def _derive_enabled_state(self, *, properties: dict) -> str:
        """
            Derive the ``enabled_state`` field from parsed ``print``
            properties.

            ``disabled = false`` -> ``enabled``;
            ``disabled = true``  -> ``disabled``;
            field absent         -> ``enabled`` (launchd's default).

            :param properties: Parsed key-value map from
                               :meth:`_parse_print_output`.

            :returns: ``"enabled"`` or ``"disabled"``.
        """
        disabled_raw = properties.get("disabled", "").strip().lower()
        if disabled_raw == "true":
            rtnval = _ENABLED_STATE_DISABLED
        else:
            rtnval = _ENABLED_STATE_ENABLED
        return rtnval

    def _derive_pid(self, *, properties: dict) -> Optional[int]:
        """
            Derive the ``main_pid`` field from parsed ``print``
            properties.  Tolerates missing or non-numeric values.

            :param properties: Parsed key-value map.

            :returns: The PID when parseable and non-zero; otherwise
                      None.
        """
        pid_raw = properties.get("pid", "").strip()
        if len(pid_raw) == 0:
            rtnval: Optional[int] = None
        else:
            try:
                pid_value = int(pid_raw)
            except ValueError:
                pid_value = 0
            if pid_value == 0:
                rtnval = None
            else:
                rtnval = pid_value
        return rtnval

    def _derive_last_exit_code(
        self,
        *,
        properties: dict,
    ) -> Optional[int]:
        """
            Derive the ``last_exit_code`` field from parsed ``print``
            properties.  Tolerates missing or non-numeric values.

            :param properties: Parsed key-value map.

            :returns: The last exit code when parseable; otherwise None.
        """
        last_raw = properties.get("last exit code", "").strip()
        if len(last_raw) == 0:
            rtnval: Optional[int] = None
        else:
            try:
                last_value = int(last_raw)
            except ValueError:
                last_value = 0
                rtnval = None
                return rtnval
            rtnval = last_value
        return rtnval

    def _derive_path(self, *, properties: dict) -> Optional[str]:
        """
            Derive the ``path`` field from parsed ``print`` properties.
            Prefers the explicit ``path`` key (the plist path) and falls
            back to ``program`` (the executable path) when ``path`` is
            absent.

            :param properties: Parsed key-value map.

            :returns: The discovered path string, or None when neither
                      field is present.
        """
        path_raw = properties.get("path", "").strip()
        if len(path_raw) > 0:
            rtnval: Optional[str] = path_raw
        else:
            program_raw = properties.get("program", "").strip()
            if len(program_raw) > 0:
                rtnval = program_raw
            else:
                rtnval = None
        return rtnval

    def _parse_list_output(
        self,
        *,
        output: str,
        scope: MacosServiceScope,
        domain: str,
    ) -> list[MacosServiceInfo]:
        """
            Parse ``launchctl list`` output into a list of
            :class:`MacosServiceInfo`.

            Output is tab-separated, with a single header row::

                PID\tStatus\tLabel
                12345\t0\tcom.apple.something
                -\t0\tcom.example.disabled

            ``-`` in the PID column means the job is not running;
            ``-`` in the Status column means no exit status is reported.

            :param output: Raw stdout text.
            :param scope: The scope the listing came from.
            :param domain: The domain the listing was taken from.

            :returns: One :class:`MacosServiceInfo` per parsed row.
        """
        rtnval: list[MacosServiceInfo] = []
        for raw_index, raw_line in enumerate(output.splitlines()):
            line = raw_line.rstrip()
            if len(line.strip()) == 0:
                continue

            # ``launchctl list`` always emits a literal header row first.
            # ``startswith("PID")`` is a safe guard because no real Label
            # value starts with that token at column 0.
            if raw_index == 0 and line.lstrip().startswith("PID"):
                continue

            # Tab-separated columns; tolerate runs of whitespace by
            # falling back to whitespace splitting when the tab split
            # comes back short.
            columns = line.split("\t")
            if len(columns) < 3:
                columns = line.split()
                if len(columns) < 3:
                    continue

            pid_raw = columns[0].strip()
            status_raw = columns[1].strip()
            label = columns[2].strip()
            if len(label) == 0:
                continue

            if pid_raw == "-" or len(pid_raw) == 0:
                pid_value: Optional[int] = None
            else:
                try:
                    pid_value = int(pid_raw)
                except ValueError:
                    pid_value = None

            if status_raw == "-" or len(status_raw) == 0:
                status_value: Optional[int] = None
            else:
                try:
                    status_value = int(status_raw)
                except ValueError:
                    status_value = None

            info = MacosServiceInfo(
                service_name=label,
                scope=scope,
                domain=domain,
                pid=pid_value,
                status=status_value,
            )
            rtnval.append(info)

        return rtnval
