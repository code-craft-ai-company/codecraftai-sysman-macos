"""
    macOS ACL-management feature.

    Implements the cross-OS ACL-management contract for macOS using
    the BSD ``chmod`` extended-ACL surface (``chmod +a`` / ``chmod
    -a`` / ``chmod -N``) for writes and ``ls -le`` for reads.  Every
    system command is dispatched through the injected
    :class:`ICommandRunner`; this module never imports
    ``subprocess`` and never touches the filesystem directly.

    Verb catalogue:
        * ``get_acl(path)`` -> ``ls -le <path>`` parsed into a
          :class:`MacosAclInfo`.
        * ``set_acl(path, acl_spec)`` -> ``chmod -N <path>`` followed
          by ``chmod +a "<rendered>" <path>`` per ACE.
        * ``add_ace(path, principal, rights)`` -> ``chmod +a
          "<rendered>" <path>``.
        * ``remove_ace(path, principal)`` -> ``chmod -a "<rendered>"
          <path>`` for each matching ACE in the parsed ACL.
        * ``clear_acl(path)`` -> ``chmod -N <path>``.
        * ``copy_acl(src, dst)`` -> ``ls -le <src>`` parsed; then
          ``chmod -N <dst>`` followed by ``chmod +a "<rendered>"
          <dst>`` per ACE.
        * ``list_aces(path)`` -> ``ls -le <path>`` parsed.

    macOS ACL model:
        macOS uses an NFSv4-style ACL model.  ACEs carry an explicit
        ``allow`` / ``deny`` permission, a richer rights vocabulary
        (``read``, ``write``, ``execute``, ``delete``, ``append``,
        ``readattr``, ``writeattr``, ``readextattr``,
        ``writeextattr``, ``readsecurity``, ``writesecurity``,
        ``chown``, ``list``, ``search``, ``add_file``,
        ``add_subdirectory``, ``delete_child``, ``file_inherit``,
        ``directory_inherit``, ``limit_inherit``, ``only_inherit``),
        and per-ACE inheritance flags rather than a single
        ``default:``-prefixed partition like POSIX ACLs.  HFS+ /
        APFS do not carry a "default ACL" partition; the
        ``UbuntuAclInfo``-style ``default_aces`` field exposed here
        is therefore always empty on macOS.

    Elevation policy:
        Every write verb (``set_acl``, ``add_ace``, ``remove_ace``,
        ``clear_acl``, ``copy_acl``) honours ``config.elevated``.
        Reads (``get_acl``, ``list_aces``, every idempotency probe)
        never elevate.

    Idempotency:
        * ``set_acl`` parses the current ACL and skips when the
          (allow/deny + principal + rights-set + inheritable)
          tuples match exactly.
        * ``add_ace`` skips when an ACE matching that tuple is
          already present.
        * ``remove_ace`` skips when no ACE matches the principal.
        * ``clear_acl`` skips when the parsed ACL has zero ACEs.
        * ``copy_acl`` skips when the parsed source and destination
          ACLs are tuple-equal.

    File-I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner`.  This module does NOT consume an
        :class:`IFileOperator`.  ACL state on macOS lives on the
        inode (HFS+) or the file record (APFS); ``chmod`` writes it
        directly.
"""

import os

from dataclasses import dataclass, field
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

_LS = "ls"
_CHMOD = "chmod"

# ``ls`` flags.  ``-l`` requests long-listing (a precondition for
# ``-e``); ``-e`` requests the per-line ACL listing; ``-d`` keeps the
# command pointed at the directory itself rather than its children.
_LS_FLAGS = "-lde"

# ``chmod`` ACL flags.  ``+a`` adds an ACE; ``-a`` removes an exact
# matching ACE; ``-N`` removes the entire ACL on the path.
_CHMOD_ADD_ACE = "+a"
_CHMOD_REMOVE_ACE = "-a"
_CHMOD_CLEAR_ACL = "-N"


MacosAclActionStatus = ActionStatus


class MacosAcePrincipalKind(Enum):
    """
        macOS ACL principal-kind tag.  Maps to the ``user:`` or
        ``group:`` prefix in the canonical ACE display form (e.g.
        ``user:alice allow read,write``).
    """
    USER = "user"
    GROUP = "group"


class MacosAcePermission(Enum):
    """
        ACE polarity on macOS.  Unlike POSIX ACLs, macOS NFSv4-style
        ACEs can be allow OR deny.
    """
    ALLOW = "allow"
    DENY = "deny"


@dataclass
class MacosAclManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosAclManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param ls_path: Path / name of the ``ls`` binary.  Defaults
                        to ``"ls"`` (PATH-resolved).
        :param chmod_path: Path / name of the ``chmod`` binary.
                           Defaults to ``"chmod"``.
    """
    ls_path: str = _LS
    chmod_path: str = _CHMOD


@dataclass
class MacosAclActionResult(FeatureActionResult):
    """
        Result of an ACL-management write operation.

        :param path: The target path, when applicable.
        :param principal: The principal acted upon, when applicable.
                          ``None`` for verbs whose write target spans
                          many principals (``set_acl``, ``copy_acl``,
                          ``clear_acl``).
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    path: Optional[str] = None
    principal: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosAceInfo:
    """
        A single parsed macOS extended ACL entry.

        :param principal_kind: One of :class:`MacosAcePrincipalKind`.
        :param principal: The user or group name as resolved by
                          Directory Services (e.g. ``"alice"``,
                          ``"_developer"``).
        :param permission: ``ALLOW`` or ``DENY``.
        :param rights: macOS-native rights tokens (e.g. ``["read",
                       "write", "execute"]``).
        :param inheritable: ``True`` when any of ``file_inherit``,
                            ``directory_inherit``, ``limit_inherit``,
                            or ``only_inherit`` was present in the
                            rights tokens.  Always derived from
                            ``rights``; we never carry an
                            "inheritable" rights token *and* the
                            boolean — the boolean is the canonical
                            inheritance summary the manager exposes
                            to callers, while ``rights`` retains the
                            verbatim token for round-trip fidelity.
        :param index: Zero-based index of this ACE in the ``ls -le``
                      listing.  Useful for downstream tooling that
                      wants to address ACEs by position (``chmod
                      -a# <index>``).
    """
    principal_kind: MacosAcePrincipalKind
    principal: str
    permission: MacosAcePermission
    rights: list = field(default_factory=list)
    inheritable: bool = False
    index: int = 0


@dataclass
class MacosAceSpec:
    """
        Caller-supplied ACE shape used by :meth:`MacosAclManager.set_acl`
        and :meth:`MacosAclManager.add_ace`.

        :param principal: The user or group name.
        :param principal_kind: ``USER`` or ``GROUP``.
        :param permission: ``ALLOW`` or ``DENY``.
        :param rights: List of macOS-native rights tokens.
        :param inheritable: When ``True``, the rendered ACE carries
                            the ``file_inherit,directory_inherit``
                            inheritance flags.
    """
    principal: str
    principal_kind: MacosAcePrincipalKind
    permission: MacosAcePermission
    rights: list
    inheritable: bool = False


@dataclass
class MacosAclInfo:
    """
        Snapshot of a path's macOS ACL.

        :param path: The path the ACL was queried for.
        :param owner: Owner principal name, parsed from the ``ls
                      -l`` line (the third whitespace-delimited
                      field).
        :param group: Primary group principal name (the fourth
                      field).
        :param aces: ACEs in source order.
        :param default_aces: Empty on macOS; HFS+ / APFS do not
                             carry a default-ACL partition.
                             Maintained on the dataclass for
                             cross-OS-contract parity.
    """
    path: str
    owner: Optional[str] = None
    group: Optional[str] = None
    aces: list = field(default_factory=list)
    default_aces: list = field(default_factory=list)


class MacosAclQueryError(Exception):
    """
        Raised when an ACL read fails for a reason other than the
        target simply being absent.
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

        err_msg_lines = ["ACL query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosAclManager(BaseFeatureManager):
    """
        macOS ACL manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner`; the manager never imports
        ``subprocess`` and never touches the filesystem directly.
    """

    result_class = MacosAclActionResult
    status_class = MacosAclActionStatus
    feature_name = "acl-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_acl(self, path: str) -> MacosAclInfo:
        """
            Return a :class:`MacosAclInfo` snapshot of ``path``'s
            current ACL.

            Runs ``ls -lde <path>``.  The first line carries the
            standard ``ls -l`` long-form listing (mode, link count,
            owner, group, size, mtime, name); the subsequent lines
            (when extended ACEs are present) carry one ACE per line
            in the canonical ``<n>: <principal> <allow|deny>
            <rights>`` form.

            :param path: The absolute path to query.

            :returns: The parsed :class:`MacosAclInfo`.

            :raises: :class:`MacosAclQueryError`
        """
        command = [self._ls_path(), _LS_FLAGS, path]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise MacosAclQueryError(
                command=list(command),
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_ls_le_output(
            output=run_result.stdout,
            path=path,
        )
        return rtnval

    def list_aces(self, path: str) -> list:
        """
            Return every ACE attached to ``path`` as a flat list in
            the order ``ls -le`` emitted them.  macOS has no default
            ACL partition; the returned list is exactly
            :class:`MacosAclInfo.aces`.

            :param path: The absolute path to query.

            :returns: A ``list[MacosAceInfo]``.

            :raises: :class:`MacosAclQueryError`
        """
        info = self.get_acl(path=path)

        rtnval: list = []
        for ace in info.aces:
            rtnval.append(ace)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_acl(
        self,
        path: str,
        acl_spec: list,
    ) -> MacosAclActionResult:
        """
            Replace the full ACL on ``path`` with ``acl_spec``.

            Implemented as ``chmod -N <path>`` followed by ``chmod +a
            "<rendered>" <path>`` per ACE.  Skipped when the parsed
            current ACL is already tuple-equal to ``acl_spec``.

            :param path: The target path.
            :param acl_spec: A list of :class:`MacosAceSpec`.

            :returns: The :class:`MacosAclActionResult`.
        """
        clear_command = [self._chmod_path(), _CHMOD_CLEAR_ACL, path]
        # Record the full intended sequence as ``command`` for the
        # caller; the per-ACE write argv list is rendered below.
        rendered_aces: list = []
        for spec in acl_spec:
            rendered_aces.append(_render_chmod_ace_token(spec=spec))

        if self._config.dry_run:
            preview_command = list(clear_command)
            for token in rendered_aces:
                preview_command.append("&&")
                preview_command.append(self._chmod_path())
                preview_command.append(_CHMOD_ADD_ACE)
                preview_command.append(token)
                preview_command.append(path)
            dry = self._build_result(
                status=MacosAclActionStatus.DRY_RUN,
                command=preview_command,
                path=path,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except MacosAclQueryError:
            current = None

        if current is not None and _acl_matches_spec(
            current=current,
            desired=acl_spec,
        ):
            skipped = self._build_result(
                status=MacosAclActionStatus.SKIPPED,
                command=clear_command,
                path=path,
                message="path ACL already matches the requested spec",
            )
            return skipped

        clear_result = self._runner.run(
            list(clear_command),
            elevated=self._config.elevated,
        )
        if clear_result.exit_code != 0:
            failed = MacosAclActionResult(
                status=MacosAclActionStatus.FAILED,
                detail=path,
                exit_code=clear_result.exit_code,
                stdout=clear_result.stdout,
                stderr=clear_result.stderr,
                command=list(clear_command),
                path=path,
                message="chmod -N failed before set",
            )
            return failed

        # Dispatch one ``chmod +a "<rendered>" <path>`` per ACE.  The
        # last result wins; a per-ACE failure short-circuits and
        # returns FAILED.
        last_result: Optional[MacosAclActionResult] = None
        for token in rendered_aces:
            command = [self._chmod_path(), _CHMOD_ADD_ACE, token, path]
            step = self._dispatch_acl_write(
                command=command,
                path=path,
            )
            last_result = step
            if step.status == MacosAclActionStatus.FAILED:
                return step

        if last_result is None:
            # Empty acl_spec: a successful clear with nothing to add.
            rtnval = MacosAclActionResult(
                status=MacosAclActionStatus.SUCCESS,
                detail=path,
                exit_code=clear_result.exit_code,
                stdout=clear_result.stdout,
                stderr=clear_result.stderr,
                command=list(clear_command),
                path=path,
            )
            return rtnval

        rtnval = last_result
        return rtnval

    def add_ace(
        self,
        path: str,
        principal: MacosAceSpec,
        rights: Optional[list] = None,
    ) -> MacosAclActionResult:
        """
            Add a single ACE to ``path``.

            Two calling shapes are accepted:

            * Pass a fully-populated :class:`MacosAceSpec` as
              ``principal``.  ``rights`` MUST be ``None`` in that
              case.
            * Pass a :class:`MacosAceSpec` whose ``rights`` is the
              empty list and a ``rights`` list argument.  The
              argument overrides the spec's ``rights`` field.

            Skipped when an ACE with the same principal /
            permission / rights / inheritable tuple is already
            present.

            :param path: The target path.
            :param principal: The :class:`MacosAceSpec` to add.
            :param rights: Optional override for ``principal.rights``.

            :returns: The :class:`MacosAclActionResult`.
        """
        effective_spec = _materialise_ace_spec(
            principal=principal,
            rights_override=rights,
        )
        rendered = _render_chmod_ace_token(spec=effective_spec)

        command = [
            self._chmod_path(),
            _CHMOD_ADD_ACE,
            rendered,
            path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAclActionStatus.DRY_RUN,
                command=command,
                path=path,
                principal=effective_spec.principal,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except MacosAclQueryError:
            current = None

        if current is not None and _ace_already_present(
            current=current,
            spec=effective_spec,
        ):
            skipped = self._build_result(
                status=MacosAclActionStatus.SKIPPED,
                command=command,
                path=path,
                principal=effective_spec.principal,
                message="ACE is already present on the target path",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=command,
            path=path,
            principal=effective_spec.principal,
        )
        return rtnval

    def remove_ace(
        self,
        path: str,
        principal: MacosAceSpec,
    ) -> MacosAclActionResult:
        """
            Remove every ACE matching the ``principal`` (by name and
            kind) from ``path``.

            ``chmod -a`` requires an exact ACE token match (the
            man-page warning).  This manager therefore parses the
            current ACL, identifies every matching ACE, and dispatches
            one ``chmod -a "<rendered>" <path>`` per match.  Skipped
            when no ACE matches the principal.

            :param path: The target path.
            :param principal: The :class:`MacosAceSpec`; only
                              ``principal_kind`` and ``principal``
                              are consulted for matching, but
                              ``permission`` / ``rights`` /
                              ``inheritable`` are still rendered onto
                              each removal token.

            :returns: The :class:`MacosAclActionResult`.
        """
        # We dispatch ONE summary command into ``command`` for the
        # caller; the actual per-match dispatch happens below.
        summary_token = "{}:{}".format(
            principal.principal_kind.value,
            principal.principal,
        )
        summary_command = [
            self._chmod_path(),
            _CHMOD_REMOVE_ACE,
            summary_token,
            path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAclActionStatus.DRY_RUN,
                command=summary_command,
                path=path,
                principal=principal.principal,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except MacosAclQueryError:
            current = None

        if current is None:
            # No probe -- fall through to dispatch the summary form;
            # ``chmod -a`` will surface its own error if no match.
            rtnval = self._dispatch_acl_write(
                command=summary_command,
                path=path,
                principal=principal.principal,
            )
            return rtnval

        matching_aces = _find_matching_aces(
            current=current,
            principal_kind=principal.principal_kind,
            principal=principal.principal,
        )
        if len(matching_aces) == 0:
            skipped = self._build_result(
                status=MacosAclActionStatus.SKIPPED,
                command=summary_command,
                path=path,
                principal=principal.principal,
                message="no matching ACE to remove",
            )
            return skipped

        last_result: Optional[MacosAclActionResult] = None
        for ace in matching_aces:
            equivalent_spec = MacosAceSpec(
                principal=ace.principal,
                principal_kind=ace.principal_kind,
                permission=ace.permission,
                rights=list(ace.rights),
                inheritable=ace.inheritable,
            )
            rendered = _render_chmod_ace_token(spec=equivalent_spec)
            command = [
                self._chmod_path(),
                _CHMOD_REMOVE_ACE,
                rendered,
                path,
            ]
            step = self._dispatch_acl_write(
                command=command,
                path=path,
                principal=principal.principal,
            )
            last_result = step
            if step.status == MacosAclActionStatus.FAILED:
                return step

        rtnval = last_result
        return rtnval

    def clear_acl(self, path: str) -> MacosAclActionResult:
        """
            Remove every ACE from ``path``.

            Skipped when the parsed ACL contains zero ACEs.

            :param path: The target path.

            :returns: The :class:`MacosAclActionResult`.
        """
        command = [self._chmod_path(), _CHMOD_CLEAR_ACL, path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAclActionStatus.DRY_RUN,
                command=command,
                path=path,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except MacosAclQueryError:
            current = None

        if current is not None and len(current.aces) == 0:
            skipped = self._build_result(
                status=MacosAclActionStatus.SKIPPED,
                command=command,
                path=path,
                message="path has no ACL entries to clear",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=command,
            path=path,
        )
        return rtnval

    def copy_acl(
        self,
        src: str,
        dst: str,
    ) -> MacosAclActionResult:
        """
            Copy the ACL from ``src`` to ``dst``.

            Implemented as a parsed read of ``src`` followed by a
            ``chmod -N <dst>`` clear and a sequence of ``chmod +a
            "<rendered>" <dst>`` writes — one per ACE on the source.
            macOS has no native ``getfacl``-style stream
            representation, so a stdin pipe is not available; we
            therefore re-render each parsed ACE.

            Skipped when the parsed source and destination ACLs are
            tuple-equal.

            :param src: Source path to copy from.
            :param dst: Destination path to copy onto.

            :returns: The :class:`MacosAclActionResult`.
        """
        clear_command = [self._chmod_path(), _CHMOD_CLEAR_ACL, dst]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAclActionStatus.DRY_RUN,
                command=clear_command,
                path=dst,
            )
            return dry

        try:
            src_info = self.get_acl(path=src)
        except MacosAclQueryError as exc:
            failed = MacosAclActionResult(
                status=MacosAclActionStatus.FAILED,
                detail=src,
                exit_code=exc.exit_code,
                stdout=None,
                stderr=exc.stderr,
                command=list(exc.command),
                path=src,
                message="ls -le on source path failed",
            )
            return failed

        try:
            dst_info = self.get_acl(path=dst)
        except MacosAclQueryError:
            dst_info = None

        if dst_info is not None and _acl_infos_equal(
            left=src_info,
            right=dst_info,
        ):
            skipped = self._build_result(
                status=MacosAclActionStatus.SKIPPED,
                command=clear_command,
                path=dst,
                message="destination ACL already matches source ACL",
            )
            return skipped

        clear_result = self._runner.run(
            list(clear_command),
            elevated=self._config.elevated,
        )
        if clear_result.exit_code != 0:
            failed = MacosAclActionResult(
                status=MacosAclActionStatus.FAILED,
                detail=dst,
                exit_code=clear_result.exit_code,
                stdout=clear_result.stdout,
                stderr=clear_result.stderr,
                command=list(clear_command),
                path=dst,
                message="chmod -N failed before copy",
            )
            return failed

        last_result: Optional[MacosAclActionResult] = None
        for ace in src_info.aces:
            equivalent_spec = MacosAceSpec(
                principal=ace.principal,
                principal_kind=ace.principal_kind,
                permission=ace.permission,
                rights=list(ace.rights),
                inheritable=ace.inheritable,
            )
            rendered = _render_chmod_ace_token(spec=equivalent_spec)
            command = [
                self._chmod_path(),
                _CHMOD_ADD_ACE,
                rendered,
                dst,
            ]
            step = self._dispatch_acl_write(
                command=command,
                path=dst,
            )
            last_result = step
            if step.status == MacosAclActionStatus.FAILED:
                return step

        if last_result is None:
            rtnval = MacosAclActionResult(
                status=MacosAclActionStatus.SUCCESS,
                detail=dst,
                exit_code=clear_result.exit_code,
                stdout=clear_result.stdout,
                stderr=clear_result.stderr,
                command=list(clear_command),
                path=dst,
            )
            return rtnval

        rtnval = last_result
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ls_path(self) -> str:
        """
            Return the configured ``ls`` path.
        """
        config = self._config
        if isinstance(config, MacosAclManagerConfig):
            rtnval = config.ls_path
        else:
            rtnval = _LS
        return rtnval

    def _chmod_path(self) -> str:
        """
            Return the configured ``chmod`` path.
        """
        config = self._config
        if isinstance(config, MacosAclManagerConfig):
            rtnval = config.chmod_path
        else:
            rtnval = _CHMOD
        return rtnval

    def _dispatch_acl_write(
        self,
        *,
        command: list,
        path: Optional[str] = None,
        principal: Optional[str] = None,
    ) -> MacosAclActionResult:
        """
            Dispatch a write command and wrap the result with the
            ACL-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=path,
            operation="acl_write",
            path=path,
            principal=principal,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosAclActionStatus,
        command: list,
        path: Optional[str] = None,
        principal: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosAclActionResult:
        """
            Construct a :class:`MacosAclActionResult` without
            invoking the runner.
        """
        rtnval = MacosAclActionResult(
            status=status,
            detail=path if path is not None else principal,
            command=list(command),
            path=path,
            principal=principal,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


# Inheritance tokens -- the four macOS-native rights tokens that
# express directory inheritance behaviour.  Used to compute the
# ``inheritable`` boolean on parsed ACEs.
_INHERITANCE_TOKENS = frozenset({
    "file_inherit",
    "directory_inherit",
    "limit_inherit",
    "only_inherit",
})


def _materialise_ace_spec(
    *,
    principal: MacosAceSpec,
    rights_override: Optional[list],
) -> MacosAceSpec:
    """
        Return a normalised :class:`MacosAceSpec`, applying any
        ``rights_override`` from the caller.  We never mutate the
        caller's spec.

        :param principal: The caller-supplied spec.
        :param rights_override: Optional overriding rights list.
    """
    if rights_override is not None:
        effective_rights = list(rights_override)
    else:
        effective_rights = list(principal.rights)

    rtnval = MacosAceSpec(
        principal=principal.principal,
        principal_kind=principal.principal_kind,
        permission=principal.permission,
        rights=effective_rights,
        inheritable=principal.inheritable,
    )
    return rtnval


def _render_chmod_ace_token(*, spec: MacosAceSpec) -> str:
    """
        Render a :class:`MacosAceSpec` into a ``chmod +a`` / ``chmod
        -a`` argument token.

        Examples::

            MacosAceSpec("alice", USER, ALLOW, ["read", "write"])
                -> "user:alice allow read,write"

            MacosAceSpec("admin", GROUP, DENY, ["delete"])
                -> "group:admin deny delete"

            MacosAceSpec("alice", USER, ALLOW, ["read"], inheritable=True)
                -> "user:alice allow read,file_inherit,directory_inherit"

        The ``inheritable`` flag adds the canonical inheritance pair
        (``file_inherit,directory_inherit``).  Callers that need
        finer control over individual inheritance tokens can include
        them directly in ``rights`` and leave ``inheritable=False``.

        :param spec: The ACE.
    """
    rights_tokens: list = list(spec.rights)
    if spec.inheritable:
        if "file_inherit" not in rights_tokens:
            rights_tokens.append("file_inherit")
        if "directory_inherit" not in rights_tokens:
            rights_tokens.append("directory_inherit")

    rights_text = ",".join(rights_tokens)
    rtnval = "{}:{} {} {}".format(
        spec.principal_kind.value,
        spec.principal,
        spec.permission.value,
        rights_text,
    )
    return rtnval


def _parse_ls_le_output(
    *,
    output: str,
    path: str,
) -> MacosAclInfo:
    """
        Parse the output of ``ls -lde <path>`` into a
        :class:`MacosAclInfo`.

        ``ls -lde`` output for a file with extended ACEs looks like::

            drwxr-xr-x+ 1 alice staff 64 Jan 01 12:00 /srv/data
             0: user:alice allow read,write,execute
             1: group:wheel allow read,execute
             2: user:bob deny delete

        The first line is the standard ``ls -l`` long-form line.
        Subsequent lines (zero or more) carry one ACE per line.  We
        anchor on the leading ``<n>: `` index marker.

        :param output: The captured stdout.
        :param path: The path the read was for (used to populate
                     :class:`MacosAclInfo.path`).

        :returns: The parsed :class:`MacosAclInfo`.
    """
    info = MacosAclInfo(path=path)

    lines = output.splitlines()
    if len(lines) == 0:
        rtnval = info
        return rtnval

    # First line: ``ls -l``.
    header_owner, header_group = _parse_ls_long_line_owner_group(
        line=lines[0],
    )
    info.owner = header_owner
    info.group = header_group

    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue

        ace = _parse_ls_le_ace_line(line=stripped)
        if ace is None:
            continue

        info.aces.append(ace)

    rtnval = info
    return rtnval


def _parse_ls_long_line_owner_group(
    *,
    line: str,
) -> tuple:
    """
        Parse the third and fourth whitespace-delimited fields from
        an ``ls -l`` long-form line.  Returns ``(owner, group)``;
        either may be ``None`` when the line carries fewer fields
        than expected.

        :param line: The first line of ``ls -lde`` output.
    """
    fields = line.split()
    if len(fields) < 4:
        rtnval = (None, None)
        return rtnval

    owner = fields[2]
    group = fields[3]
    rtnval = (owner, group)
    return rtnval


def _parse_ls_le_ace_line(*, line: str) -> Optional[MacosAceInfo]:
    """
        Parse a single ``ls -le`` ACE line.

        Expected shape (with the leading whitespace already stripped)::

            "0: user:alice allow read,write,execute"
            "1: group:wheel allow read,execute"
            "2: user:bob deny delete,file_inherit,directory_inherit"

        :param line: The stripped non-header line.
    """
    # Index prefix.
    colon_index = line.find(":")
    if colon_index <= 0:
        rtnval: Optional[MacosAceInfo] = None
        return rtnval

    index_token = line[:colon_index].strip()
    try:
        index_value = int(index_token)
    except ValueError:
        rtnval = None
        return rtnval

    body = line[colon_index + 1:].strip()
    if len(body) == 0:
        rtnval = None
        return rtnval

    body_parts = body.split(None, 2)
    if len(body_parts) < 3:
        rtnval = None
        return rtnval

    principal_token = body_parts[0]
    permission_token = body_parts[1]
    rights_token = body_parts[2]

    if ":" not in principal_token:
        rtnval = None
        return rtnval

    kind_word, _, principal_name = principal_token.partition(":")
    if kind_word == "user":
        principal_kind_value: Optional[MacosAcePrincipalKind] = (
            MacosAcePrincipalKind.USER
        )
    elif kind_word == "group":
        principal_kind_value = MacosAcePrincipalKind.GROUP
    else:
        principal_kind_value = None

    if principal_kind_value is None or len(principal_name) == 0:
        rtnval = None
        return rtnval

    if permission_token == "allow":
        permission_value: Optional[MacosAcePermission] = (
            MacosAcePermission.ALLOW
        )
    elif permission_token == "deny":
        permission_value = MacosAcePermission.DENY
    else:
        permission_value = None

    if permission_value is None:
        rtnval = None
        return rtnval

    rights_list: list = []
    for token in rights_token.split(","):
        cleaned = token.strip()
        if len(cleaned) == 0:
            continue
        rights_list.append(cleaned)

    inheritable_flag = False
    for token in rights_list:
        if token in _INHERITANCE_TOKENS:
            inheritable_flag = True
            break

    rtnval = MacosAceInfo(
        principal_kind=principal_kind_value,
        principal=principal_name,
        permission=permission_value,
        rights=rights_list,
        inheritable=inheritable_flag,
        index=index_value,
    )
    return rtnval


def _ace_already_present(
    *,
    current: MacosAclInfo,
    spec: MacosAceSpec,
) -> bool:
    """
        Return ``True`` when ``current`` contains an ACE matching
        ``spec`` on principal-kind, principal, permission, rights-set
        (order-insensitive), and inheritable flag.

        :param current: The current :class:`MacosAclInfo`.
        :param spec: The desired ACE.
    """
    spec_rights_tokens = list(spec.rights)
    if spec.inheritable:
        if "file_inherit" not in spec_rights_tokens:
            spec_rights_tokens.append("file_inherit")
        if "directory_inherit" not in spec_rights_tokens:
            spec_rights_tokens.append("directory_inherit")

    spec_rights_set = frozenset(spec_rights_tokens)

    rtnval = False
    for ace in current.aces:
        if ace.principal_kind != spec.principal_kind:
            continue
        if ace.principal != spec.principal:
            continue
        if ace.permission != spec.permission:
            continue
        ace_rights_set = frozenset(ace.rights)
        if ace_rights_set != spec_rights_set:
            continue
        rtnval = True
        break
    return rtnval


def _find_matching_aces(
    *,
    current: MacosAclInfo,
    principal_kind: MacosAcePrincipalKind,
    principal: str,
) -> list:
    """
        Return every ACE in ``current.aces`` matching the given
        principal-kind / principal pair.

        :param current: The current :class:`MacosAclInfo`.
        :param principal_kind: The principal kind to match.
        :param principal: The principal name to match.
    """
    rtnval: list = []
    for ace in current.aces:
        if ace.principal_kind != principal_kind:
            continue
        if ace.principal != principal:
            continue
        rtnval.append(ace)
    return rtnval


def _acl_matches_spec(
    *,
    current: MacosAclInfo,
    desired: list,
) -> bool:
    """
        Return ``True`` when the parsed ACL is tuple-equal to the
        requested ``desired`` list.

        :param current: The current :class:`MacosAclInfo`.
        :param desired: A list of :class:`MacosAceSpec`.
    """
    desired_tuples: list = []
    for spec in desired:
        spec_rights_tokens = list(spec.rights)
        if spec.inheritable:
            if "file_inherit" not in spec_rights_tokens:
                spec_rights_tokens.append("file_inherit")
            if "directory_inherit" not in spec_rights_tokens:
                spec_rights_tokens.append("directory_inherit")
        normalised = (
            spec.principal_kind,
            spec.principal,
            spec.permission,
            frozenset(spec_rights_tokens),
        )
        desired_tuples.append(normalised)

    current_tuples: list = []
    for ace in current.aces:
        normalised = (
            ace.principal_kind,
            ace.principal,
            ace.permission,
            frozenset(ace.rights),
        )
        current_tuples.append(normalised)

    desired_sorted = sorted(desired_tuples, key=_ace_sort_key)
    current_sorted = sorted(current_tuples, key=_ace_sort_key)

    rtnval = desired_sorted == current_sorted
    return rtnval


def _ace_sort_key(item) -> tuple:
    """
        Stable sort key for an ACE comparison tuple
        ``(principal_kind, principal, permission, rights_set)``.

        :param item: The 4-tuple.
    """
    kind_value, principal_value, permission_value, rights_set = item
    rights_text = ",".join(sorted(rights_set))
    rtnval = (
        kind_value.value,
        principal_value,
        permission_value.value,
        rights_text,
    )
    return rtnval


def _acl_infos_equal(
    *,
    left: MacosAclInfo,
    right: MacosAclInfo,
) -> bool:
    """
        Return ``True`` when two :class:`MacosAclInfo` snapshots
        carry identical ACE lists.  Owner / group / path are
        intentionally NOT compared -- :meth:`copy_acl` is about ACE
        state, not file ownership.

        :param left: One snapshot.
        :param right: The other snapshot.
    """
    left_tuples: list = []
    for ace in left.aces:
        normalised = (
            ace.principal_kind,
            ace.principal,
            ace.permission,
            frozenset(ace.rights),
        )
        left_tuples.append(normalised)

    right_tuples: list = []
    for ace in right.aces:
        normalised = (
            ace.principal_kind,
            ace.principal,
            ace.permission,
            frozenset(ace.rights),
        )
        right_tuples.append(normalised)

    left_sorted = sorted(left_tuples, key=_ace_sort_key)
    right_sorted = sorted(right_tuples, key=_ace_sort_key)

    rtnval = left_sorted == right_sorted
    return rtnval
