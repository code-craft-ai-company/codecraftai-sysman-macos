"""
    macOS nfs-server-management feature.

    Implements the cross-OS nfs-server-management contract for
    macOS.  macOS ships its own ``nfsd`` daemon as part of the base
    operating system, exposed under launchd as ``com.apple.nfsd``
    (LaunchDaemon plist at
    ``/System/Library/LaunchDaemons/com.apple.nfsd.plist``).  This
    module wraps the ``nfsd`` command, the ``showmount`` query tool,
    and the on-disk content of ``/etc/exports`` (in Darwin syntax)
    through the injected :class:`ICommandRunner` so the manager
    works identically under local and SSH execution.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | macOS tool                        |
    +=============================+===================================+
    | ``add_export``              | ``cat /etc/exports`` + parse +    |
    |                             | render + ``tee /etc/exports`` +   |
    |                             | ``nfsd update``                   |
    +-----------------------------+-----------------------------------+
    | ``remove_export``           | ``cat /etc/exports`` + parse +    |
    |                             | render + ``tee /etc/exports`` +   |
    |                             | ``nfsd update``                   |
    +-----------------------------+-----------------------------------+
    | ``list_exports``            | ``cat /etc/exports`` + parse +    |
    |                             | ``showmount -e localhost`` for    |
    |                             | active state                      |
    +-----------------------------+-----------------------------------+
    | ``reload_exports``          | ``nfsd update``                   |
    +-----------------------------+-----------------------------------+
    | ``get_export_status``       | ``showmount -e localhost`` parse  |
    |                             | for the path                      |
    +-----------------------------+-----------------------------------+
    | ``set_export_options``      | ``cat`` + parse + render-with-new |
    |                             | -options + ``tee`` + ``nfsd``     |
    +-----------------------------+-----------------------------------+

    Cross-OS exports-syntax divergence (load-bearing!):
        macOS uses BSD's exports grammar inherited from FreeBSD,
        **NOT** Linux's parenthesised form.  A Linux-canonical
        line such as ``/srv/share *(rw,sync,no_subtree_check)`` is
        rejected by macOS ``nfsd`` with a syntax error.  The
        canonical Darwin shape is::

            <path> -<opt1> -<opt2>… <client1> [<client2>…]

        Examples::

            /Users/Shared/share -ro -alldirs -mapall=nobody \\
                                    -network 0.0.0.0 -mask 0.0.0.0
            /srv/restricted 10.0.0.0 -mask 255.255.255.0

        Options are dash-prefixed and shared across the entire line
        (Darwin does not natively scope options per client).
        Wildcard hosts must be expressed as
        ``-network 0.0.0.0 -mask 0.0.0.0`` rather than the literal
        ``"*"``.

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content
        on stdin.  This module does NOT consume an
        :class:`IFileOperator`.

    Drop-in caveat:
        Darwin's ``nfsd`` does NOT consult drop-in directories such
        as ``/etc/exports.d/``; the primary ``/etc/exports`` file is
        the only configuration surface.

    Idempotency model:
        Every write verb reads the current exports file, parses it,
        mutates the parsed structure in memory, re-renders the file,
        and compares to the original content.  Idempotency is exact-
        match on the rendered string.

    Reload-after-change:
        Every successful mutation that touches ``/etc/exports`` is
        followed by ``nfsd update``.  When the chained reload fails
        the verb maps to ``FAILED`` and the reload's argv / exit /
        stderr are surfaced on the returned result.

    Per-client option-list collapse:
        For cross-OS API parity each :class:`MacosNfsClientSpec`
        carries its own ``options`` list.  Darwin's grammar shares
        options across the whole line, so the renderer uses **only
        the first client's option list** when emitting a line; the
        option lists on subsequent clients are ignored.  Callers who
        need per-client option scoping must emit one export per
        client.

    Wildcard normalisation:
        ``MacosNfsClientSpec(host="*")`` is rendered as
        ``-network 0.0.0.0 -mask 0.0.0.0`` because Darwin does not
        accept the literal ``*`` as a client matcher.  The parser
        folds the ``-network 0.0.0.0 -mask 0.0.0.0`` pair back to
        the literal ``"*"`` on round-trip.

    Elevation policy:
        Write verbs (``add_export``, ``remove_export``,
        ``reload_exports``, ``set_export_options``) honour
        ``config.elevated``.  Reads (``list_exports``,
        ``get_export_status``) never elevate.  The ``cat`` /
        ``showmount`` probes used inside write paths to drive
        idempotency decisions also do not elevate.
"""

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

_CAT = "cat"
_TEE = "tee"
_NFSD = "nfsd"
_SHOWMOUNT = "showmount"

_DEFAULT_EXPORTS_FILE = "/etc/exports"
_DEFAULT_SERVICE_LABEL = "com.apple.nfsd"

_DEFAULT_CLIENT_OPTIONS = (
    "ro",
    "alldirs",
    "mapall=nobody",
)

_WILDCARD_HOST = "*"
_WILDCARD_NETWORK = "0.0.0.0"
_WILDCARD_MASK = "0.0.0.0"


MacosNfsActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


def _default_options_list() -> list:
    """
        Build the default per-client options list for macOS.

        :returns: A fresh list of the safe-default options.
    """
    rtnval = list(_DEFAULT_CLIENT_OPTIONS)
    return rtnval


@dataclass
class MacosNfsManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosNfsServerManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param exports_file: Absolute path to the primary exports
                             file.  Default ``/etc/exports``.
        :param service_label: launchd label for the nfsd daemon.
                              The manager does NOT call launchctl
                              directly; this label is recorded for
                              downstream callers' convenience.
                              Default ``"com.apple.nfsd"``.
    """
    exports_file: str = _DEFAULT_EXPORTS_FILE
    service_label: str = _DEFAULT_SERVICE_LABEL


@dataclass
class MacosNfsActionResult(FeatureActionResult):
    """
        Result of an nfs-server write operation.

        :param path: The export path the action targeted, or ``None``
                     for verbs that do not target a single export.
        :param share_name: Reserved for cross-OS parity with the
                           Windows manager.  Always ``None`` on
                           macOS.
        :param message: Optional human-readable detail for SKIPPED /
                        FAILED outcomes.
    """
    path: Optional[str] = None
    share_name: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosNfsClientSpec:
    """
        A single client matcher for a macOS export.

        :param host: Client address matcher (hostname, IP literal,
                     or ``"*"`` for the wildcard).  Default ``"*"``.
        :param options: Per-client export options.  Default is the
                        safe-default list ``["ro", "alldirs",
                        "mapall=nobody"]``.

        Note: Darwin's exports grammar shares options across the
        entire line.  When a spec carries multiple clients the
        renderer uses only the first client's option list — the
        ``options`` field on subsequent clients is ignored.
    """
    host: str = "*"
    options: list = field(default_factory=_default_options_list)


@dataclass
class MacosNfsExportSpec:
    """
        Declarative spec for a single macOS export.

        :param path: The filesystem path to export.
        :param clients: One or more client matchers.  Must be
                        non-empty.
        :param share_name: Reserved for cross-OS parity.  Ignored
                           on macOS.
    """
    path: str
    clients: list
    share_name: Optional[str] = None


@dataclass
class MacosNfsExportInfo:
    """
        One parsed export entry.

        :param path: The exported filesystem path.
        :param share_name: Reserved for cross-OS parity.  Always
                           ``None`` on macOS.
        :param clients: The client matchers parsed from the line.
        :param is_active: True when ``showmount -e localhost``
                          advertises the path.
    """
    path: str
    share_name: Optional[str]
    clients: list
    is_active: bool


@dataclass
class MacosNfsExportStatus:
    """
        Result of :meth:`MacosNfsServerManager.get_export_status`.

        :param path: The export path the status describes.
        :param is_active: True when the path appears in
                          ``showmount -e localhost`` output.
        :param client_count: The number of distinct clients seen
                             for the path.
        :param flags: Best-effort options list parsed from the
                      ``/etc/exports`` line for the same path.
                      Empty when the path is not configured.
    """
    path: str
    is_active: bool
    client_count: int
    flags: list


class MacosNfsQueryError(Exception):
    """
        Raised when a macOS NFS read fails for a reason other than
        the target file simply being absent.
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
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["macOS NFS query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosNfsServerManager(BaseFeatureManager):
    """
        macOS nfs-server-management manager.  Reads, writes, and
        reloads ``/etc/exports`` (Darwin syntax) through the injected
        :class:`ICommandRunner`.
    """

    result_class = MacosNfsActionResult
    status_class = MacosNfsActionStatus
    feature_name = "nfs-server-management"

    def __init__(
        self,
        runner,
        config: Optional[MacosNfsManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh :class:`MacosNfsManagerConfig`.
        """
        if config is None:
            config = MacosNfsManagerConfig()
        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_exports(self) -> list:
        """
            Enumerate every export configured in ``/etc/exports`` and
            mark each one active or inactive based on the daemon's
            current ``showmount -e`` output.

            :returns: A ``list[MacosNfsExportInfo]``.  Empty when the
                      configuration file is missing.

            :raises MacosNfsQueryError: When ``showmount -e
                                        localhost`` exits non-zero
                                        with non-empty stderr.
        """
        path = self._typed_config().exports_file
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        configured_exports = _parse_exports_file(content=content)
        active_paths = self._read_active_export_paths()

        rtnval = []
        for entry in configured_exports:
            if entry.path in active_paths:
                entry.is_active = True
            else:
                entry.is_active = False
            rtnval.append(entry)
        return rtnval

    def get_export_status(self, path: str) -> MacosNfsExportStatus:
        """
            Query the daemon for whether ``path`` is currently
            advertised.

            :param path: The export path to query.

            :returns: The :class:`MacosNfsExportStatus`.

            :raises MacosNfsQueryError: When ``showmount -e
                                        localhost`` exits non-zero
                                        with non-empty stderr.
        """
        active = self._invoke_showmount_e()
        if path not in active:
            rtnval = MacosNfsExportStatus(
                path=path,
                is_active=False,
                client_count=0,
                flags=[],
            )
            return rtnval

        client_count = len(active[path])
        flag_list = self._lookup_configured_options(path=path)
        rtnval = MacosNfsExportStatus(
            path=path,
            is_active=True,
            client_count=client_count,
            flags=flag_list,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_export(
        self,
        spec: MacosNfsExportSpec,
    ) -> MacosNfsActionResult:
        """
            Add or update an export at ``spec.path``.

            Idempotent: returns ``SKIPPED`` when the file already
            contains an export at ``spec.path`` whose rendered line
            matches.  Honours dry-run.  After a successful ``tee``
            the manager dispatches ``nfsd update``.

            :param spec: The :class:`MacosNfsExportSpec`.

            :returns: The :class:`MacosNfsActionResult`.
        """
        target_path = self._typed_config().exports_file
        write_command = [_TEE, target_path]

        existing = self._read_file(path=target_path)
        existing_content = "" if existing is None else existing

        new_content = _upsert_export(
            content=existing_content, spec=spec,
        )
        if existing is not None and new_content == existing:
            skipped = self._build_result(
                status=MacosNfsActionStatus.SKIPPED,
                path=spec.path,
                command=write_command,
                message="export already at desired state",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNfsActionStatus.DRY_RUN,
                path=spec.path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = MacosNfsActionResult(
                status=MacosNfsActionStatus.FAILED,
                detail="add_export:{}".format(spec.path),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                path=spec.path,
                share_name=None,
            )
            return failed

        rtnval = self._invoke_nfsd_update_reload(
            base_action="add_export:{}".format(spec.path),
            path=spec.path,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def remove_export(self, path: str) -> MacosNfsActionResult:
        """
            Remove every export line whose first token equals
            ``path``.

            Idempotent: returns ``SKIPPED`` when the file is missing
            or no line matches.  Honours dry-run.  After a successful
            ``tee`` the manager dispatches ``nfsd update``.

            :param path: The export path to remove.

            :returns: The :class:`MacosNfsActionResult`.
        """
        target_path = self._typed_config().exports_file
        write_command = [_TEE, target_path]

        existing = self._read_file(path=target_path)
        if existing is None:
            skipped = self._build_result(
                status=MacosNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="exports file does not exist",
            )
            return skipped

        new_content = _remove_export_lines(content=existing, path=path)
        if new_content == existing:
            skipped = self._build_result(
                status=MacosNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="export not present",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNfsActionStatus.DRY_RUN,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = MacosNfsActionResult(
                status=MacosNfsActionStatus.FAILED,
                detail="remove_export:{}".format(path),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                path=path,
                share_name=None,
            )
            return failed

        rtnval = self._invoke_nfsd_update_reload(
            base_action="remove_export:{}".format(path),
            path=path,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def reload_exports(self) -> MacosNfsActionResult:
        """
            Reload the active export table via ``nfsd update``.

            Operational verb — never skips.  Honours dry-run.

            :returns: The :class:`MacosNfsActionResult`.
        """
        command = [_NFSD, "update"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNfsActionStatus.DRY_RUN,
                path=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            path=None,
        )
        return rtnval

    def set_export_options(
        self,
        path: str,
        options: list,
    ) -> MacosNfsActionResult:
        """
            Replace the shared options list on the export at ``path``.

            Idempotent: returns ``SKIPPED`` when the file is missing,
            no line matches, or the line already carries exactly
            ``options``.  Honours dry-run.

            :param path: The export path whose options should be
                         replaced.
            :param options: The new options list.

            :returns: The :class:`MacosNfsActionResult`.
        """
        target_path = self._typed_config().exports_file
        write_command = [_TEE, target_path]

        existing = self._read_file(path=target_path)
        if existing is None:
            skipped = self._build_result(
                status=MacosNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="exports file does not exist",
            )
            return skipped

        new_content = _set_options_for_path(
            content=existing, path=path, options=list(options),
        )
        if new_content is None:
            skipped = self._build_result(
                status=MacosNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="export not present",
            )
            return skipped
        if new_content == existing:
            skipped = self._build_result(
                status=MacosNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="options already at desired value",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosNfsActionStatus.DRY_RUN,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = MacosNfsActionResult(
                status=MacosNfsActionStatus.FAILED,
                detail="set_export_options:{}".format(path),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                path=path,
                share_name=None,
            )
            return failed

        rtnval = self._invoke_nfsd_update_reload(
            base_action="set_export_options:{}".format(path),
            path=path,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosNfsManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed :class:`MacosNfsManagerConfig`.
        """
        rtnval = self._config
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _invoke_showmount_e(self) -> dict:
        """
            Read the active export table via
            ``showmount -e localhost``.

            :returns: A dict mapping path to client list.

            :raises MacosNfsQueryError: When ``showmount`` exits
                                        non-zero with non-empty
                                        stderr.
        """
        command = [_SHOWMOUNT, "-e", "localhost"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise MacosNfsQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_showmount_e_output(text=run_result.stdout)
        return rtnval

    def _read_active_export_paths(self) -> set:
        """
            Build the set of paths the daemon currently advertises.
        """
        active = self._invoke_showmount_e()
        rtnval = set(active.keys())
        return rtnval

    def _lookup_configured_options(self, *, path: str) -> list:
        """
            Read the configured options for ``path`` from
            ``/etc/exports`` to populate
            :attr:`MacosNfsExportStatus.flags`.

            :param path: The export path to look up.

            :returns: The first-client options list when found;
                      empty list otherwise.
        """
        target_path = self._typed_config().exports_file
        content = self._read_file(path=target_path)
        if content is None:
            rtnval: list = []
            return rtnval

        for entry in _parse_exports_file(content=content):
            if entry.path == path and len(entry.clients) > 0:
                rtnval = list(entry.clients[0].options)
                return rtnval
        rtnval = []
        return rtnval

    def _invoke_nfsd_update_reload(
        self,
        *,
        base_action: str,
        path: Optional[str],
        primary_run,
        primary_command: list,
    ) -> MacosNfsActionResult:
        """
            Dispatch ``nfsd update`` after a successful primary
            mutation.  When the reload fails the result maps to
            ``FAILED`` even though the primary mutation succeeded.

            :param base_action: The detail string identifying the
                                primary verb.
            :param path: The export path the primary verb targeted.
            :param primary_run: The :class:`RunResult` from the
                                preceding ``tee`` call.
            :param primary_command: The argv that ran first.

            :returns: A :class:`MacosNfsActionResult`.
        """
        reload_command = [_NFSD, "update"]
        reload_run = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_run.exit_code != 0:
            rtnval = MacosNfsActionResult(
                status=MacosNfsActionStatus.FAILED,
                detail=base_action,
                exit_code=reload_run.exit_code,
                stdout=reload_run.stdout,
                stderr=reload_run.stderr,
                command=list(reload_command),
                path=path,
                share_name=None,
            )
            return rtnval

        rtnval = MacosNfsActionResult(
            status=MacosNfsActionStatus.SUCCESS,
            detail=base_action,
            exit_code=primary_run.exit_code,
            stdout=primary_run.stdout,
            stderr=primary_run.stderr,
            command=list(primary_command),
            path=path,
            share_name=None,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosNfsActionStatus,
        path: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> MacosNfsActionResult:
        """
            Build a result without invoking the runner.
        """
        if path is not None:
            detail_text = path
        else:
            detail_text = None
        rtnval = MacosNfsActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            path=path,
            share_name=None,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        path: Optional[str],
    ) -> MacosNfsActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`MacosNfsActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = MacosNfsActionStatus.SUCCESS
        else:
            mapped_status = MacosNfsActionStatus.FAILED
        if path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = MacosNfsActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            path=path,
            share_name=None,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — Darwin exports parser / renderer
# ----------------------------------------------------------------------


def _parse_exports_file(*, content: str) -> list:
    """
        Parse a Darwin-format ``/etc/exports`` file body into a list
        of :class:`MacosNfsExportInfo`.

        Comment and blank lines are skipped.  Each remaining line is
        tokenised on whitespace; the first token is the path,
        subsequent tokens are classified as options (``-flag`` /
        ``-flag=value``), network qualifiers
        (``-network <addr>`` / ``-mask <mask>``), or client hosts.

        Lines whose only token is the path are skipped.  The
        ``-network 0.0.0.0 -mask 0.0.0.0`` pair folds back to the
        wildcard literal ``"*"`` on the resulting client list.

        :param content: The raw file content.

        :returns: A ``list[MacosNfsExportInfo]`` in declaration order
                  with ``is_active=False`` on every entry.
    """
    rtnval: list = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if len(tokens) < 2:
            continue

        path = tokens[0]
        opts, clients = _classify_darwin_tokens(tokens=tokens[1:])
        if len(clients) == 0:
            continue

        client_specs: list = []
        for host in clients:
            client_specs.append(
                MacosNfsClientSpec(host=host, options=list(opts)),
            )

        entry = MacosNfsExportInfo(
            path=path,
            share_name=None,
            clients=client_specs,
            is_active=False,
        )
        rtnval.append(entry)
    return rtnval


def _classify_darwin_tokens(*, tokens: list) -> tuple:
    """
        Walk the post-path tokens of a Darwin export line and split
        them into a shared-option list and a client list.

        :param tokens: The tokens after the path (already split on
                       whitespace).

        :returns: ``(options, clients)`` where ``options`` is a
                  ``list[str]`` of dash-stripped option strings and
                  ``clients`` is a ``list[str]`` of host literals.
                  The ``-network 0.0.0.0 -mask 0.0.0.0`` pair folds
                  to a single ``"*"`` host.
    """
    options: list = []
    clients: list = []

    network_value: Optional[str] = None
    mask_value: Optional[str] = None

    index = 0
    total = len(tokens)
    while index < total:
        current = tokens[index]
        if current == "-network" and index + 1 < total:
            network_value = tokens[index + 1]
            index = index + 2
            continue
        if current == "-mask" and index + 1 < total:
            next_mask = tokens[index + 1]
            # When ``-mask`` follows a bare host literal (rather
            # than a preceding ``-network``), fold the most recent
            # client into a ``host/mask`` form.  This preserves
            # Darwin's ``<host> -mask <mask>`` idiom seen alongside
            # ``-network <net> -mask <mask>``.
            if network_value is None and len(clients) > 0:
                last_host = clients.pop()
                clients.append(
                    "{}/{}".format(last_host, next_mask),
                )
            else:
                mask_value = next_mask
            index = index + 2
            continue
        if current.startswith("-"):
            options.append(current[1:])
            index = index + 1
            continue
        clients.append(current)
        index = index + 1

    if network_value is not None and mask_value is not None:
        if (
            network_value == _WILDCARD_NETWORK
            and mask_value == _WILDCARD_MASK
        ):
            clients.append(_WILDCARD_HOST)
        else:
            clients.append(
                "{}/{}".format(network_value, mask_value),
            )
    elif network_value is not None:
        clients.append(network_value)

    rtnval = (options, clients)
    return rtnval


def _render_export_line(*, spec: MacosNfsExportSpec) -> str:
    """
        Render a :class:`MacosNfsExportSpec` as a single canonical
        Darwin ``/etc/exports`` line.

        Darwin grammar::

            <path> -<opt1> -<opt2>… <client1> [<client2>…]

        The wildcard host ``"*"`` is expanded to
        ``-network 0.0.0.0 -mask 0.0.0.0`` because Darwin does not
        accept the literal ``*``.

        Only the **first** client's options are emitted on the line —
        Darwin scopes options across the whole line, not per client.

        :param spec: The export spec.  Must carry at least one
                     client.

        :returns: The rendered line (no trailing newline).

        :raises ValueError: When ``spec.clients`` is empty.
    """
    if spec.clients is None or len(spec.clients) == 0:
        err_msg_lines = ["Cannot render export line with no clients."]
        err_msg_lines.append("    PATH: {}".format(spec.path))
        err_msg = os.linesep.join(err_msg_lines)
        raise ValueError(err_msg)

    first_client = spec.clients[0]
    opt_tokens: list = []
    if first_client.options is not None:
        for opt in first_client.options:
            opt_tokens.append("-{}".format(opt))

    client_tokens: list = []
    for client in spec.clients:
        if client.host == _WILDCARD_HOST:
            client_tokens.append(
                "-network {} -mask {}".format(
                    _WILDCARD_NETWORK, _WILDCARD_MASK,
                ),
            )
            continue
        client_tokens.append(client.host)

    parts: list = [spec.path]
    for opt in opt_tokens:
        parts.append(opt)
    for client in client_tokens:
        parts.append(client)
    rtnval = " ".join(parts)
    return rtnval


def _render_export_line_for_info(*, info: MacosNfsExportInfo) -> str:
    """
        Render a parsed :class:`MacosNfsExportInfo` back to its
        canonical Darwin line.
    """
    spec = MacosNfsExportSpec(
        path=info.path,
        clients=list(info.clients),
        share_name=info.share_name,
    )
    rtnval = _render_export_line(spec=spec)
    return rtnval


def _upsert_export(
    *,
    content: str,
    spec: MacosNfsExportSpec,
) -> str:
    """
        Return content with the spec's line upserted.

        Existing lines whose first token matches ``spec.path`` are
        replaced in place; otherwise a new line is appended.
        Comments and blank lines are preserved.

        :param content: Existing file content.
        :param spec: The :class:`MacosNfsExportSpec`.

        :returns: The updated content (with a trailing newline).
    """
    rendered_line = _render_export_line(spec=spec)
    new_lines: list = []
    replaced = False
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            new_lines.append(raw)
            continue
        if stripped.startswith("#"):
            new_lines.append(raw)
            continue
        tokens = stripped.split()
        if len(tokens) >= 1 and tokens[0] == spec.path and not replaced:
            new_lines.append(rendered_line)
            replaced = True
            continue
        if len(tokens) >= 1 and tokens[0] == spec.path and replaced:
            continue
        new_lines.append(raw)

    if not replaced:
        new_lines.append(rendered_line)

    body = "\n".join(new_lines)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_export_lines(*, content: str, path: str) -> str:
    """
        Return content with every line whose first token equals
        ``path`` stripped.
    """
    new_lines: list = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            new_lines.append(raw)
            continue
        if stripped.startswith("#"):
            new_lines.append(raw)
            continue
        tokens = stripped.split()
        if len(tokens) >= 1 and tokens[0] == path:
            continue
        new_lines.append(raw)

    body = "\n".join(new_lines)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


def _set_options_for_path(
    *,
    content: str,
    path: str,
    options: list,
) -> Optional[str]:
    """
        Return content with the option list of the line whose first
        token equals ``path`` rewritten to ``options``.

        Returns ``None`` when no matching line is found.
    """
    new_lines: list = []
    matched = False
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            new_lines.append(raw)
            continue
        if stripped.startswith("#"):
            new_lines.append(raw)
            continue
        tokens = stripped.split()
        if len(tokens) < 2:
            new_lines.append(raw)
            continue
        if tokens[0] != path:
            new_lines.append(raw)
            continue
        matched = True
        existing_opts, clients = _classify_darwin_tokens(tokens=tokens[1:])
        _ = existing_opts  # discarded — replaced wholesale.
        client_specs: list = []
        for host in clients:
            client_specs.append(
                MacosNfsClientSpec(host=host, options=list(options)),
            )
        if len(client_specs) == 0:
            # Drop a path-only line during option rewrite — it was
            # malformed to begin with.
            continue
        new_info = MacosNfsExportInfo(
            path=path,
            share_name=None,
            clients=client_specs,
            is_active=False,
        )
        new_lines.append(_render_export_line_for_info(info=new_info))

    if not matched:
        rtnval: Optional[str] = None
        return rtnval

    body = "\n".join(new_lines)
    if not body.endswith("\n"):
        body = body + "\n"
    rtnval = body
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — showmount parser
# ----------------------------------------------------------------------


def _parse_showmount_e_output(*, text: str) -> dict:
    """
        Parse ``showmount -e localhost`` output into a path → client-
        list dict.

        ``showmount -e localhost`` emits a header line followed by
        one entry per export.  Each entry is::

            <path>      <client>[ <client>…]

        ``Everyone`` is the canonical text form of the wildcard.

        :param text: The captured stdout.

        :returns: A ``dict[str, list[str]]`` mapping path to client
                  list.
    """
    rtnval: dict = {}
    for raw in text.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped.lower().startswith("export"):
            # Header line "Exports list on localhost:".
            continue
        tokens = stripped.split()
        if len(tokens) < 2:
            continue
        path = tokens[0]
        client_tokens = tokens[1:]
        rtnval[path] = list(client_tokens)
    return rtnval
