"""
    macOS ssh-server-configuration feature.

    Implements the cross-OS ssh-server-configuration contract for
    macOS.  macOS ships OpenSSH server (Remote Login) as part of the
    base operating system; the daemon is managed by ``launchd`` and
    exposed under the ``system`` domain as ``com.openssh.sshd``.  The
    primary configuration file lives at the same path as the Linux
    family — ``/etc/ssh/sshd_config`` — and the validation tool is the
    same ``sshd -t`` (located at ``/usr/sbin/sshd``).

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | macOS tool                        |
    +=============================+===================================+
    | ``get_sshd_setting``        | ``cat /etc/ssh/sshd_config`` then |
    |                             | parse                             |
    +-----------------------------+-----------------------------------+
    | ``set_sshd_setting``        | ``cat`` + parse + render +        |
    |                             | ``tee /etc/ssh/sshd_config``      |
    +-----------------------------+-----------------------------------+
    | ``remove_sshd_setting``     | ``cat`` + parse + render +        |
    |                             | ``tee /etc/ssh/sshd_config``      |
    +-----------------------------+-----------------------------------+
    | ``list_sshd_settings``      | ``cat`` + parse                   |
    +-----------------------------+-----------------------------------+
    | ``add_match_block`` /       | ``cat`` + parse + render +        |
    | ``remove_match_block``      | ``tee``                           |
    +-----------------------------+-----------------------------------+
    | ``list_match_blocks``       | ``cat`` + parse                   |
    +-----------------------------+-----------------------------------+
    | ``reload_sshd``             | ``launchctl kickstart -k          |
    |                             | system/com.openssh.sshd``         |
    +-----------------------------+-----------------------------------+
    | ``test_sshd_config``        | ``sshd -t``                       |
    +-----------------------------+-----------------------------------+
    | ``list_authorized_keys``    | ``cat``                           |
    +-----------------------------+-----------------------------------+
    | ``add_authorized_key`` /    | ``cat`` + render +                |
    | ``remove_authorized_key``   | ``tee /Users/<user>/.ssh/         |
    |                             | authorized_keys``                 |
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content on
        stdin.  This module does NOT consume an :class:`IFileOperator`.

    Reload mechanism:
        macOS has no `systemctl reload` equivalent.  ``launchctl
        kickstart -k system/com.openssh.sshd`` is the supported way to
        re-execute the daemon under launchd.  The ``-k`` flag stops a
        running instance before re-launching it; this is closer to a
        full restart than a true `reload` but is the canonical
        signal-handler hook for OpenSSH on macOS.

    Authorized-keys path:
        macOS user homes live under ``/Users/<user>``, so the default
        template is ``/Users/{user}/.ssh/authorized_keys``.

    Authorized-key fingerprinting:
        Fingerprint computation is performed in pure Python using
        SHA-256 of the base64-decoded key body, formatted as
        ``SHA256:<base64-no-padding>``.  This matches OpenSSH's
        ``ssh-keygen -lf`` output exactly.

    Elevation policy:
        Write verbs honour ``config.elevated``.  Reads
        (``get_sshd_setting``, ``list_sshd_settings``,
        ``list_match_blocks``, ``list_authorized_keys``) never
        elevate.
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

_CAT = "cat"
_TEE = "tee"
_SSHD = "sshd"
_LAUNCHCTL = "launchctl"

_DEFAULT_SSHD_CONFIG_PATH = "/etc/ssh/sshd_config"
_DEFAULT_AUTHORIZED_KEYS_TEMPLATE = "/Users/{user}/.ssh/authorized_keys"
_DEFAULT_SERVICE_NAME = "com.openssh.sshd"
_DEFAULT_SERVICE_DOMAIN = "system"

_MATCH_KEYWORD = "Match"


MacosSshActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class MacosSshServerConfigurationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosSshServerConfigurationManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param sshd_config_path: Absolute path to the primary
                                 ``sshd_config`` file.  Default is the
                                 macOS stock location
                                 ``/etc/ssh/sshd_config``.
        :param authorized_keys_path_template: Format string used to
                                              resolve a user's
                                              ``authorized_keys`` path.
                                              Must contain a single
                                              ``{user}`` placeholder.
                                              Default is
                                              ``/Users/{user}/.ssh/authorized_keys``.
        :param service_name: launchd service identifier used by
                             :meth:`MacosSshServerConfigurationManager.reload_sshd`.
                             Default is ``"com.openssh.sshd"``.
        :param service_domain: launchd domain qualifier (e.g.
                               ``"system"`` or ``"gui/501"``).  Default
                               is ``"system"`` since the daemon is
                               operated as a system-domain service.
    """
    sshd_config_path: str = _DEFAULT_SSHD_CONFIG_PATH
    authorized_keys_path_template: str = _DEFAULT_AUTHORIZED_KEYS_TEMPLATE
    service_name: str = _DEFAULT_SERVICE_NAME
    service_domain: str = _DEFAULT_SERVICE_DOMAIN


@dataclass
class MacosSshActionResult(FeatureActionResult):
    """
        Result of an ssh-server-configuration write operation.

        :param keyword: The ``sshd_config`` keyword targeted, or
                        ``None``.
        :param path: The file path the action targeted, or ``None``.
        :param user: The user whose authorized-keys file was targeted,
                     or ``None``.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    keyword: Optional[str] = None
    path: Optional[str] = None
    user: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosSshSettingValue:
    """
        A single ``sshd_config`` keyword / value pair.
    """
    keyword: str
    value: str
    source_file: str


@dataclass
class MacosSshMatchBlock:
    """
        A single ``Match <criteria>`` block.
    """
    criteria: str
    settings: dict = field(default_factory=dict)


@dataclass
class MacosSshAuthorizedKey:
    """
        A single ``authorized_keys`` entry.
    """
    key_type: str
    key_data: str
    comment: Optional[str] = None
    fingerprint: Optional[str] = None
    options: Optional[str] = None


class MacosSshQueryError(Exception):
    """
        Raised when an ssh-server-configuration read fails for a reason
        other than the target source simply being absent.
    """

    def __init__(
        self,
        *,
        path: str,
        message: str,
    ) -> None:
        """
            :param path: The file path whose read failed.
            :param message: Human-readable detail.
        """
        self.path = path
        self.message = message
        err_msg_lines = ["SSH query command failed."]
        err_msg_lines.append("    PATH: {}".format(path))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosSshServerConfigurationManager(BaseFeatureManager):
    """
        macOS ssh-server-configuration manager.  Reads, writes, and
        validates ``/etc/ssh/sshd_config`` and per-user
        ``authorized_keys`` through the injected
        :class:`ICommandRunner`.
    """

    result_class = MacosSshActionResult
    status_class = MacosSshActionStatus
    feature_name = "ssh-server-configuration"

    # ------------------------------------------------------------------
    # Read verbs — sshd_config
    # ------------------------------------------------------------------

    def get_sshd_setting(
        self,
        keyword: str,
    ) -> Optional[MacosSshSettingValue]:
        """
            Read a single top-level ``sshd_config`` keyword.

            :param keyword: The ``sshd_config`` keyword.

            :returns: The :class:`MacosSshSettingValue` or ``None``.
        """
        path = self._typed_config().sshd_config_path
        content = self._read_file(path=path)
        if content is None:
            rtnval: Optional[MacosSshSettingValue] = None
            return rtnval

        top_settings, _ = _parse_sshd_config(content=content)
        keyword_lower = keyword.lower()
        for parsed_keyword, parsed_value in top_settings.items():
            if parsed_keyword.lower() == keyword_lower:
                rtnval = MacosSshSettingValue(
                    keyword=parsed_keyword,
                    value=parsed_value,
                    source_file=path,
                )
                return rtnval
        rtnval = None
        return rtnval

    def list_sshd_settings(self) -> dict:
        """
            Enumerate every top-level ``sshd_config`` keyword.
        """
        path = self._typed_config().sshd_config_path
        content = self._read_file(path=path)
        if content is None:
            rtnval: dict = {}
            return rtnval

        top_settings, _ = _parse_sshd_config(content=content)
        rtnval = top_settings
        return rtnval

    def list_match_blocks(self) -> list:
        """
            Enumerate every ``Match`` block.
        """
        path = self._typed_config().sshd_config_path
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        _, match_blocks = _parse_sshd_config(content=content)
        rtnval = match_blocks
        return rtnval

    def list_authorized_keys(self, user: str) -> list:
        """
            Read and parse a user's ``authorized_keys`` file.

            :param user: The username whose key file is queried.

            :returns: A ``list[MacosSshAuthorizedKey]``.
        """
        path = self._authorized_keys_path(user=user)
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        rtnval = _parse_authorized_keys(content=content)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — sshd_config
    # ------------------------------------------------------------------

    def set_sshd_setting(
        self,
        keyword: str,
        value: str,
    ) -> MacosSshActionResult:
        """
            Set a top-level ``sshd_config`` keyword.

            :param keyword: The keyword.
            :param value: The desired value text.

            :returns: The :class:`MacosSshActionResult`.
        """
        path = self._typed_config().sshd_config_path
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        top_settings, match_blocks = _parse_sshd_config(
            content=existing_content,
        )
        new_top_settings = _set_top_setting(
            top_settings=top_settings,
            keyword=keyword,
            value=value,
        )
        new_content = _render_sshd_config(
            top_settings=new_top_settings,
            match_blocks=match_blocks,
        )

        if existing is not None and existing == new_content:
            skipped = self._build_result(
                status=MacosSshActionStatus.SKIPPED,
                keyword=keyword,
                path=path,
                command=write_command,
                message="setting already at desired value",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSshActionStatus.DRY_RUN,
                keyword=keyword,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=keyword,
            path=path,
            user=None,
        )
        return rtnval

    def remove_sshd_setting(
        self,
        keyword: str,
    ) -> MacosSshActionResult:
        """
            Remove every top-level occurrence of ``keyword``.
        """
        path = self._typed_config().sshd_config_path
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=MacosSshActionStatus.SKIPPED,
                keyword=keyword,
                path=path,
                command=write_command,
                message="config file does not exist",
            )
            return skipped

        top_settings, match_blocks = _parse_sshd_config(content=existing)
        new_top_settings = _remove_top_setting(
            top_settings=top_settings,
            keyword=keyword,
        )
        new_content = _render_sshd_config(
            top_settings=new_top_settings,
            match_blocks=match_blocks,
        )

        if new_content == existing:
            skipped = self._build_result(
                status=MacosSshActionStatus.SKIPPED,
                keyword=keyword,
                path=path,
                command=write_command,
                message="keyword not present at top level",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSshActionStatus.DRY_RUN,
                keyword=keyword,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=keyword,
            path=path,
            user=None,
        )
        return rtnval

    def add_match_block(
        self,
        criteria: str,
        settings: dict,
    ) -> MacosSshActionResult:
        """
            Add or update a ``Match <criteria>`` block.
        """
        path = self._typed_config().sshd_config_path
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        top_settings, match_blocks = _parse_sshd_config(
            content=existing_content,
        )

        normalized_settings = dict(settings)
        new_blocks = _upsert_match_block(
            match_blocks=match_blocks,
            criteria=criteria,
            settings=normalized_settings,
        )
        new_content = _render_sshd_config(
            top_settings=top_settings,
            match_blocks=new_blocks,
        )

        if existing is not None and existing == new_content:
            skipped = self._build_result(
                status=MacosSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="match block already present",
            )
            skipped.detail = "match:{}".format(criteria)
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.detail = "match:{}".format(criteria)
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=None,
        )
        rtnval.detail = "match:{}".format(criteria)
        return rtnval

    def remove_match_block(
        self,
        criteria: str,
    ) -> MacosSshActionResult:
        """
            Remove a ``Match <criteria>`` block.
        """
        path = self._typed_config().sshd_config_path
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=MacosSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="config file does not exist",
            )
            skipped.detail = "match:{}".format(criteria)
            return skipped

        top_settings, match_blocks = _parse_sshd_config(content=existing)
        new_blocks = _remove_match_block(
            match_blocks=match_blocks,
            criteria=criteria,
        )

        if len(new_blocks) == len(match_blocks):
            skipped = self._build_result(
                status=MacosSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="match block not present",
            )
            skipped.detail = "match:{}".format(criteria)
            return skipped

        new_content = _render_sshd_config(
            top_settings=top_settings,
            match_blocks=new_blocks,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.detail = "match:{}".format(criteria)
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=None,
        )
        rtnval.detail = "match:{}".format(criteria)
        return rtnval

    # ------------------------------------------------------------------
    # Operational verbs
    # ------------------------------------------------------------------

    def reload_sshd(self) -> MacosSshActionResult:
        """
            Re-execute the sshd daemon under launchd.

            macOS has no signal-driven `reload`; the canonical
            equivalent is ``launchctl kickstart -k <domain>/<name>``,
            which stops a running instance and restarts it.  This is
            slightly heavier than a Linux SIGHUP-style reload — long
            running connections survive (sshd's existing established
            connections are owned by per-connection child processes
            and are not torn down by stopping the listener) but new
            connections will see a brief listener gap.

            :returns: The :class:`MacosSshActionResult`.
        """
        domain = self._typed_config().service_domain
        name = self._typed_config().service_name
        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "kickstart", "-k", target]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSshActionStatus.DRY_RUN,
                keyword=None,
                path=None,
                command=command,
                message=target,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            keyword=None,
            path=None,
            user=None,
        )
        return rtnval

    def test_sshd_config(self) -> MacosSshActionResult:
        """
            Validate the current ``sshd_config`` via ``sshd -t``.
        """
        command = [_SSHD, "-t"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSshActionStatus.DRY_RUN,
                keyword=None,
                path=self._typed_config().sshd_config_path,
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
            keyword=None,
            path=self._typed_config().sshd_config_path,
            user=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Authorized-key write verbs
    # ------------------------------------------------------------------

    def add_authorized_key(
        self,
        user: str,
        key: str,
    ) -> MacosSshActionResult:
        """
            Append a key line to a user's ``authorized_keys``.
        """
        path = self._authorized_keys_path(user=user)
        write_command = [_TEE, path]

        parsed_new = _parse_authorized_key_line(line=key)
        if parsed_new is None:
            failed = self._build_result(
                status=MacosSshActionStatus.FAILED,
                keyword=None,
                path=path,
                command=write_command,
                message="key line failed to parse",
            )
            failed.user = user
            return failed

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        existing_keys = _parse_authorized_keys(content=existing_content)
        for existing_entry in existing_keys:
            if existing_entry.fingerprint == parsed_new.fingerprint:
                skipped = self._build_result(
                    status=MacosSshActionStatus.SKIPPED,
                    keyword=None,
                    path=path,
                    command=write_command,
                    message="key already present",
                )
                skipped.user = user
                return skipped

        new_content = _append_authorized_key(
            content=existing_content, key_line=key,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.user = user
            return dry

        # TODO: per-user authorized_keys writes for the calling user
        # do not strictly require elevation.  Honour the config for
        # now (matches prior wave-1 batches).
        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=user,
        )
        return rtnval

    def remove_authorized_key(
        self,
        user: str,
        fingerprint: str,
    ) -> MacosSshActionResult:
        """
            Remove the entry whose fingerprint matches.
        """
        path = self._authorized_keys_path(user=user)
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=MacosSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="authorized_keys file does not exist",
            )
            skipped.user = user
            return skipped

        new_content = _remove_authorized_key(
            content=existing, fingerprint=fingerprint,
        )

        if new_content == existing:
            skipped = self._build_result(
                status=MacosSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="key with given fingerprint not present",
            )
            skipped.user = user
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.user = user
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=user,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosSshServerConfigurationManagerConfig:
        """
            Narrow the base config to the typed subclass.
        """
        rtnval = self._config
        return rtnval

    def _authorized_keys_path(self, *, user: str) -> str:
        """
            Render the ``authorized_keys`` path for a user.
        """
        template = self._typed_config().authorized_keys_path_template
        rtnval = template.format(user=user)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosSshActionStatus,
        keyword: Optional[str],
        path: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> MacosSshActionResult:
        """
            Build a result without invoking the runner.
        """
        if keyword is not None:
            detail_text = keyword
        elif path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = MacosSshActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            keyword=keyword,
            path=path,
            user=None,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        keyword: Optional[str],
        path: Optional[str],
        user: Optional[str],
    ) -> MacosSshActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`MacosSshActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = MacosSshActionStatus.SUCCESS
        else:
            mapped_status = MacosSshActionStatus.FAILED

        if keyword is not None:
            detail_text = keyword
        elif path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = MacosSshActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            keyword=keyword,
            path=path,
            user=user,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — sshd_config parser / renderer
# ----------------------------------------------------------------------


def _parse_sshd_config(*, content: str) -> tuple:
    """
        Parse ``sshd_config`` content into a top-level settings map and
        a list of :class:`MacosSshMatchBlock`.

        :param content: The raw file content.

        :returns: ``(top_settings, match_blocks)``.
    """
    top_settings: dict = {}
    match_blocks: list = []
    current_block: Optional[MacosSshMatchBlock] = None

    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        tokens = stripped.split(None, 1)
        if len(tokens) < 2:
            keyword = tokens[0]
            value = ""
        else:
            keyword = tokens[0]
            value = tokens[1].strip()

        if keyword.lower() == _MATCH_KEYWORD.lower():
            current_block = MacosSshMatchBlock(
                criteria=value,
                settings={},
            )
            match_blocks.append(current_block)
            continue

        if current_block is None:
            top_settings[keyword] = value
        else:
            current_block.settings[keyword] = value

    rtnval = (top_settings, match_blocks)
    return rtnval


def _render_sshd_config(
    *,
    top_settings: dict,
    match_blocks: list,
) -> str:
    """
        Render a deterministic ``sshd_config`` from structured data.
    """
    lines: list = []
    for keyword, value in top_settings.items():
        if len(value) == 0:
            lines.append(keyword)
        else:
            lines.append("{} {}".format(keyword, value))

    for block in match_blocks:
        lines.append("Match {}".format(block.criteria))
        for keyword, value in block.settings.items():
            if len(value) == 0:
                lines.append("    {}".format(keyword))
            else:
                lines.append("    {} {}".format(keyword, value))

    body = "\n".join(lines)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _set_top_setting(
    *,
    top_settings: dict,
    keyword: str,
    value: str,
) -> dict:
    """
        Return a new top-settings dict with ``keyword`` set to
        ``value``.
    """
    keyword_lower = keyword.lower()
    rtnval: dict = {}
    replaced = False
    for existing_keyword, existing_value in top_settings.items():
        if existing_keyword.lower() == keyword_lower:
            rtnval[existing_keyword] = value
            replaced = True
            continue
        rtnval[existing_keyword] = existing_value
    if not replaced:
        rtnval[keyword] = value
    return rtnval


def _remove_top_setting(
    *,
    top_settings: dict,
    keyword: str,
) -> dict:
    """
        Return a new top-settings dict with ``keyword`` removed
        (case-insensitive).
    """
    keyword_lower = keyword.lower()
    rtnval: dict = {}
    for existing_keyword, existing_value in top_settings.items():
        if existing_keyword.lower() == keyword_lower:
            continue
        rtnval[existing_keyword] = existing_value
    return rtnval


def _upsert_match_block(
    *,
    match_blocks: list,
    criteria: str,
    settings: dict,
) -> list:
    """
        Return a new list of match blocks with the given criteria's
        settings replaced (or the block inserted at the end).
    """
    target_norm = _collapse_whitespace(text=criteria)
    rtnval: list = []
    found = False
    for block in match_blocks:
        block_norm = _collapse_whitespace(text=block.criteria)
        if block_norm == target_norm:
            replacement = MacosSshMatchBlock(
                criteria=block.criteria,
                settings=dict(settings),
            )
            rtnval.append(replacement)
            found = True
            continue
        rtnval.append(block)
    if not found:
        new_block = MacosSshMatchBlock(
            criteria=criteria,
            settings=dict(settings),
        )
        rtnval.append(new_block)
    return rtnval


def _remove_match_block(
    *,
    match_blocks: list,
    criteria: str,
) -> list:
    """
        Return a new list with every block whose criteria matches
        ``criteria`` removed (whitespace-collapsed comparison).
    """
    target_norm = _collapse_whitespace(text=criteria)
    rtnval: list = []
    for block in match_blocks:
        block_norm = _collapse_whitespace(text=block.criteria)
        if block_norm == target_norm:
            continue
        rtnval.append(block)
    return rtnval


def _collapse_whitespace(*, text: str) -> str:
    """
        Collapse runs of whitespace into a single space and strip ends.
    """
    parts = text.split()
    rtnval = " ".join(parts)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — authorized_keys parser / renderer
# ----------------------------------------------------------------------


_KNOWN_KEY_TYPES = (
    "ssh-rsa",
    "ssh-dss",
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "sk-ssh-ed25519@openssh.com",
)


def _parse_authorized_keys(*, content: str) -> list:
    """
        Parse a full ``authorized_keys`` file into entries.
    """
    rtnval: list = []
    for raw in content.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        parsed = _parse_authorized_key_line(line=line)
        if parsed is None:
            continue
        rtnval.append(parsed)
    return rtnval


def _parse_authorized_key_line(
    *,
    line: str,
) -> Optional[MacosSshAuthorizedKey]:
    """
        Parse a single ``authorized_keys`` line.
    """
    parts = line.split()
    if len(parts) < 2:
        rtnval: Optional[MacosSshAuthorizedKey] = None
        return rtnval

    type_index: Optional[int] = None
    for i, token in enumerate(parts):
        if token in _KNOWN_KEY_TYPES:
            type_index = i
            break

    if type_index is None:
        rtnval = None
        return rtnval

    if type_index + 1 >= len(parts):
        rtnval = None
        return rtnval

    if type_index == 0:
        options: Optional[str] = None
    else:
        options = " ".join(parts[:type_index])

    key_type = parts[type_index]
    key_data = parts[type_index + 1]

    comment_parts = parts[type_index + 2:]
    if len(comment_parts) > 0:
        comment: Optional[str] = " ".join(comment_parts)
    else:
        comment = None

    fingerprint = _compute_fingerprint(key_data=key_data)

    rtnval = MacosSshAuthorizedKey(
        key_type=key_type,
        key_data=key_data,
        comment=comment,
        fingerprint=fingerprint,
        options=options,
    )
    return rtnval


def _compute_fingerprint(*, key_data: str) -> Optional[str]:
    """
        Compute an OpenSSH-format SHA-256 fingerprint for a key body.
    """
    try:
        decoded = base64.b64decode(key_data, validate=True)
    except (ValueError, Exception):
        rtnval: Optional[str] = None
        return rtnval

    digest = hashlib.sha256(decoded).digest()
    encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
    rtnval = "SHA256:{}".format(encoded)
    return rtnval


def _append_authorized_key(*, content: str, key_line: str) -> str:
    """
        Append a key line to the file content.
    """
    trimmed = key_line.strip()
    if len(content) == 0:
        rtnval = trimmed + "\n"
        return rtnval
    if not content.endswith("\n"):
        body = content + "\n"
    else:
        body = content
    rtnval = body + trimmed + "\n"
    return rtnval


def _remove_authorized_key(
    *,
    content: str,
    fingerprint: str,
) -> str:
    """
        Return content with every line whose computed fingerprint
        matches ``fingerprint`` removed.
    """
    if len(content) == 0:
        return content

    kept: list = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            kept.append(raw)
            continue
        parsed = _parse_authorized_key_line(line=stripped)
        if parsed is None:
            kept.append(raw)
            continue
        if parsed.fingerprint == fingerprint:
            continue
        kept.append(raw)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body
