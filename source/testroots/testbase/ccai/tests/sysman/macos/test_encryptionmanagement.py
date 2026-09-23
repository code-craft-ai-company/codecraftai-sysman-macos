"""
    Tests for :class:`MacosEncryptionManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call (argv + elevation flag + stdin payload) and returns pre-seeded
    :class:`RunResult` values keyed by argv tuple.  No live ``fdesetup``
    or ``diskutil`` binary is invoked.
"""

import plistlib

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.encryptionmanagement import (
    MacosEncryptionActionStatus,
    MacosEncryptionManager,
    MacosEncryptionManagerConfig,
    MacosEncryptionSpec,
    _DESTRUCTIVE_GUARD_DETAIL,
    _parse_apfs_list_plist,
    _parse_fdesetup_status,
    _parse_plist_dict,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
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
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


# ----------------------------------------------------------------------
# Seed helpers
# ----------------------------------------------------------------------


_BOOT_VOLUME = "/"
_DATA_VOLUME = "/Volumes/Data"


_FDESETUP_STATUS_ON = "FileVault is On.\n"
_FDESETUP_STATUS_OFF = "FileVault is Off.\n"


def _seed_fdesetup_status(
    *,
    runner: _MockCommandRunner,
    output: str,
) -> None:
    """
        Seed ``fdesetup status`` with the given stdout payload.
    """
    key = ("fdesetup", "status")
    runner.results[key] = RunResult(exit_code=0, stdout=output, stderr="")
    return


def _seed_diskutil_info(
    *,
    runner: _MockCommandRunner,
    volume: str,
    info_dict: dict,
) -> None:
    """
        Seed ``diskutil info -plist <volume>`` with a plist payload
        built from ``info_dict``.
    """
    key = ("diskutil", "info", "-plist", volume)
    payload = plistlib.dumps(info_dict).decode("utf-8")
    runner.results[key] = RunResult(exit_code=0, stdout=payload, stderr="")
    return


def _seed_apfs_list(
    *,
    runner: _MockCommandRunner,
    plist_dict: dict,
) -> None:
    """
        Seed ``diskutil apfs list -plist`` with the given plist
        payload.
    """
    key = ("diskutil", "apfs", "list", "-plist")
    payload = plistlib.dumps(plist_dict).decode("utf-8")
    runner.results[key] = RunResult(exit_code=0, stdout=payload, stderr="")
    return


# ----------------------------------------------------------------------
# enable_encryption
# ----------------------------------------------------------------------


def test_enable_encryption_invokes_fdesetup_for_boot_volume():
    """
        enable_encryption on the boot volume dispatches
        ``fdesetup enable -user <user> -inputplist`` with the
        username + passphrase plist on stdin.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="ABCD-EFGH-IJKL", stderr=""),
    )
    _seed_fdesetup_status(runner=runner, output=_FDESETUP_STATUS_OFF)

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    spec = MacosEncryptionSpec(passphrase="hunter2", user="alice")
    result = manager.enable_encryption(volume=_BOOT_VOLUME, spec=spec)

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.SUCCESS)
    enable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["fdesetup", "enable"]
    ]
    testbase.assert_equal(len(enable_calls), 1)
    testbase.assert_equal(
        enable_calls[0]["args"],
        ["fdesetup", "enable", "-user", "alice", "-inputplist"],
    )
    stdin_payload = enable_calls[0]["stdin"]
    testbase.assert_true("hunter2" in stdin_payload)
    testbase.assert_true("alice" in stdin_payload)
    testbase.assert_equal(result.requires_reboot, True)
    return


def test_enable_encryption_invokes_diskutil_for_data_volume():
    """
        enable_encryption on a data volume dispatches
        ``diskutil apfs encryptVolume <volume> -user <user>`` with
        the passphrase delivered via stdin.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_diskutil_info(
        runner=runner,
        volume=_DATA_VOLUME,
        info_dict={"Encryption": False, "FileVault": False, "Locked": False},
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    spec = MacosEncryptionSpec(passphrase="hunter2", user="alice")
    result = manager.enable_encryption(volume=_DATA_VOLUME, spec=spec)

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.SUCCESS)
    encrypt_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "encryptVolume"]
    ]
    testbase.assert_equal(len(encrypt_calls), 1)
    testbase.assert_equal(encrypt_calls[0]["stdin"], "hunter2")
    testbase.assert_equal(result.requires_reboot, False)
    return


def test_enable_encryption_skipped_when_already_encrypted():
    """
        enable_encryption returns SKIPPED when the boot volume
        already reports ``FileVault is On``.
    """
    runner = _MockCommandRunner()
    _seed_fdesetup_status(runner=runner, output=_FDESETUP_STATUS_ON)

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    spec = MacosEncryptionSpec(passphrase="hunter2", user="alice")
    result = manager.enable_encryption(volume=_BOOT_VOLUME, spec=spec)

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.SKIPPED)
    enable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["fdesetup", "enable"]
    ]
    testbase.assert_equal(enable_calls, [])
    return


def test_enable_encryption_dry_run_short_circuits():
    """
        Dry-run records the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(dry_run=True),
    )
    spec = MacosEncryptionSpec(passphrase="hunter2", user="alice")
    result = manager.enable_encryption(volume=_BOOT_VOLUME, spec=spec)

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(result.command[0], "fdesetup")
    return


# ----------------------------------------------------------------------
# disable_encryption
# ----------------------------------------------------------------------


def test_disable_encryption_requires_allow_destructive():
    """
        disable_encryption returns FAILED with the destructive-guard
        detail when ``allow_destructive`` is False.
    """
    runner = _MockCommandRunner()
    _seed_fdesetup_status(runner=runner, output=_FDESETUP_STATUS_ON)

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(allow_destructive=False),
    )
    result = manager.disable_encryption(volume=_BOOT_VOLUME)

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.FAILED)
    testbase.assert_equal(result.detail, _DESTRUCTIVE_GUARD_DETAIL)
    disable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["fdesetup", "disable"]
    ]
    testbase.assert_equal(disable_calls, [])
    return


def test_disable_encryption_invokes_fdesetup_when_allowed():
    """
        disable_encryption on the boot volume dispatches
        ``fdesetup disable`` when ``allow_destructive=True``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_fdesetup_status(runner=runner, output=_FDESETUP_STATUS_ON)

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(allow_destructive=True),
    )
    result = manager.disable_encryption(volume=_BOOT_VOLUME)

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.SUCCESS)
    disable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["fdesetup", "disable"]
    ]
    testbase.assert_equal(len(disable_calls), 1)
    return


# ----------------------------------------------------------------------
# get_encryption_status
# ----------------------------------------------------------------------


def test_get_encryption_status_parses_fdesetup_for_boot():
    """
        get_encryption_status on the boot volume dispatches
        ``fdesetup status`` and parses the FileVault on/off flag.
    """
    runner = _MockCommandRunner()
    _seed_fdesetup_status(runner=runner, output=_FDESETUP_STATUS_ON)

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    status = manager.get_encryption_status(volume=_BOOT_VOLUME)

    testbase.assert_equal(status.is_encrypted, True)
    testbase.assert_equal(status.is_unlocked, True)
    testbase.assert_equal(status.cipher, "aes-xts")
    testbase.assert_equal(len(status.protector_types), 2)
    return


def test_get_encryption_status_parses_diskutil_for_data():
    """
        get_encryption_status on a data volume dispatches
        ``diskutil info -plist <volume>`` and parses the FileVault /
        Locked keys.
    """
    runner = _MockCommandRunner()
    _seed_diskutil_info(
        runner=runner,
        volume=_DATA_VOLUME,
        info_dict={"FileVault": True, "Encryption": True, "Locked": False},
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    status = manager.get_encryption_status(volume=_DATA_VOLUME)

    testbase.assert_equal(status.is_encrypted, True)
    testbase.assert_equal(status.is_unlocked, True)
    testbase.assert_equal(status.cipher, "aes-xts")
    return


# ----------------------------------------------------------------------
# list_encrypted_volumes
# ----------------------------------------------------------------------


def test_list_encrypted_volumes_filters_correctly():
    """
        list_encrypted_volumes returns one row per volume whose
        ``Encryption`` (or ``FileVault``) is True.  Plain volumes are
        filtered out.
    """
    runner = _MockCommandRunner()
    _seed_apfs_list(
        runner=runner,
        plist_dict={
            "Containers": [
                {
                    "Volumes": [
                        {
                            "DeviceIdentifier": "disk2s1",
                            "Name": "BootSystem",
                            "MountPoint": "/",
                            "Encryption": True,
                            "FileVault": True,
                            "Locked": False,
                        },
                        {
                            "DeviceIdentifier": "disk2s5",
                            "Name": "Data",
                            "MountPoint": "/Volumes/Data",
                            "Encryption": False,
                            "FileVault": False,
                            "Locked": False,
                        },
                        {
                            "DeviceIdentifier": "disk2s6",
                            "Name": "Vault",
                            "MountPoint": "/Volumes/Vault",
                            "Encryption": True,
                            "FileVault": True,
                            "Locked": True,
                        },
                    ],
                },
            ],
        },
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    volumes = manager.list_encrypted_volumes()

    testbase.assert_equal(len(volumes), 2)
    boot_entry = volumes[0]
    testbase.assert_equal(boot_entry.device, "/dev/disk2s1")
    testbase.assert_equal(boot_entry.is_unlocked, True)
    vault_entry = volumes[1]
    testbase.assert_equal(vault_entry.volume, "Vault")
    testbase.assert_equal(vault_entry.is_unlocked, False)
    return


# ----------------------------------------------------------------------
# rotate_key
# ----------------------------------------------------------------------


def test_rotate_key_invokes_fdesetup_changerecovery_for_boot():
    """
        rotate_key on the boot volume dispatches
        ``fdesetup changerecovery -personal``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="ABCD-EFGH", stderr=""),
    )
    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    result = manager.rotate_key(volume=_BOOT_VOLUME)

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.SUCCESS)
    rotate_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["fdesetup", "changerecovery"]
    ]
    testbase.assert_equal(len(rotate_calls), 1)
    testbase.assert_equal(
        rotate_calls[0]["args"],
        ["fdesetup", "changerecovery", "-personal"],
    )
    return


# ----------------------------------------------------------------------
# backup_recovery_key
# ----------------------------------------------------------------------


def test_backup_recovery_key_writes_to_path():
    """
        backup_recovery_key dispatches ``fdesetup showrecoverykey``,
        captures the recovery key from stdout, then dispatches
        ``tee <path>`` with the key on stdin.
    """
    runner = _MockCommandRunner()
    runner.results[("fdesetup", "showrecoverykey")] = RunResult(
        exit_code=0, stdout="ABCD-EFGH-IJKL\n", stderr="",
    )
    runner.results[("tee", "/var/backups/fv.key")] = RunResult(
        exit_code=0, stdout="ABCD-EFGH-IJKL", stderr="",
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    result = manager.backup_recovery_key(
        volume=_BOOT_VOLUME, path="/var/backups/fv.key",
    )

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.SUCCESS)
    show_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["fdesetup", "showrecoverykey"]
    ]
    testbase.assert_equal(len(show_calls), 1)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["stdin"], "ABCD-EFGH-IJKL")
    return


# ----------------------------------------------------------------------
# unlock_volume
# ----------------------------------------------------------------------


def test_unlock_volume_passes_passphrase_via_stdin():
    """
        unlock_volume dispatches ``diskutil apfs unlockVolume <vol>``
        with the passphrase delivered via stdin.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_diskutil_info(
        runner=runner,
        volume=_DATA_VOLUME,
        info_dict={"Encryption": True, "FileVault": True, "Locked": True},
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    result = manager.unlock_volume(volume=_DATA_VOLUME, passphrase="hunter2")

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.SUCCESS)
    unlock_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "unlockVolume"]
    ]
    testbase.assert_equal(len(unlock_calls), 1)
    testbase.assert_equal(unlock_calls[0]["stdin"], "hunter2")
    return


def test_unlock_volume_skipped_when_already_unlocked():
    """
        unlock_volume returns SKIPPED when ``diskutil info`` reports
        ``Locked: No``.
    """
    runner = _MockCommandRunner()
    _seed_diskutil_info(
        runner=runner,
        volume=_DATA_VOLUME,
        info_dict={"Encryption": True, "FileVault": True, "Locked": False},
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    result = manager.unlock_volume(volume=_DATA_VOLUME, passphrase="hunter2")

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.SKIPPED)
    unlock_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "unlockVolume"]
    ]
    testbase.assert_equal(unlock_calls, [])
    return


# ----------------------------------------------------------------------
# Passphrase / argv hygiene
# ----------------------------------------------------------------------


def test_passphrase_redacted_in_recorded_command():
    """
        Plaintext passphrase bytes never appear in any ``args`` field
        recorded by the runner; the bytes flow on stdin only.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_diskutil_info(
        runner=runner,
        volume=_DATA_VOLUME,
        info_dict={"Encryption": False, "FileVault": False, "Locked": False},
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    spec = MacosEncryptionSpec(passphrase="super-secret-string", user="alice")
    result = manager.enable_encryption(volume=_DATA_VOLUME, spec=spec)
    manager.unlock_volume(volume=_DATA_VOLUME, passphrase="super-secret-string")

    for call in runner.calls:
        for token in call["args"]:
            testbase.assert_true(token != "super-secret-string")
    for token in result.command:
        testbase.assert_true(token != "super-secret-string")
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes ``elevated=True`` to the runner when
        the config requests elevation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_diskutil_info(
        runner=runner,
        volume=_DATA_VOLUME,
        info_dict={"Encryption": False, "FileVault": False, "Locked": False},
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(elevated=True),
    )
    spec = MacosEncryptionSpec(passphrase="hunter2", user="alice")
    manager.enable_encryption(volume=_DATA_VOLUME, spec=spec)

    encrypt_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "encryptVolume"]
    ]
    testbase.assert_equal(len(encrypt_calls), 1)
    testbase.assert_equal(encrypt_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs (and idempotency probes) never pass
        ``elevated=True`` regardless of config.
    """
    runner = _MockCommandRunner()
    _seed_fdesetup_status(runner=runner, output=_FDESETUP_STATUS_ON)
    _seed_apfs_list(
        runner=runner,
        plist_dict={"Containers": []},
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(elevated=True),
    )
    manager.get_encryption_status(volume=_BOOT_VOLUME)
    manager.list_encrypted_volumes()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        FAILED results carry the runner's exit_code and stderr.
    """
    runner = _MockCommandRunner()
    _seed_diskutil_info(
        runner=runner,
        volume=_DATA_VOLUME,
        info_dict={"Encryption": False, "FileVault": False, "Locked": False},
    )
    runner.default_result = RunResult(
        exit_code=2, stdout="", stderr="Cannot acquire volume lock",
    )

    manager = MacosEncryptionManager(
        runner=runner,
        config=MacosEncryptionManagerConfig(),
    )
    spec = MacosEncryptionSpec(passphrase="hunter2", user="alice")
    result = manager.enable_encryption(volume=_DATA_VOLUME, spec=spec)

    testbase.assert_equal(result.status, MacosEncryptionActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "Cannot acquire volume lock")
    return


# ----------------------------------------------------------------------
# Module-level parser helpers
# ----------------------------------------------------------------------


def test_parse_fdesetup_status_detects_on_and_progress():
    """
        _parse_fdesetup_status returns ``is_encrypted=True`` and a
        progress percent when the output reports an in-flight
        encryption pass.
    """
    output = (
        "FileVault is On.\n"
        "Encryption in progress: Percent completed = 47.\n"
    )
    is_encrypted, progress = _parse_fdesetup_status(output=output)
    testbase.assert_equal(is_encrypted, True)
    testbase.assert_equal(progress, 47.0)
    return


def test_parse_plist_dict_round_trips_xml():
    """
        _parse_plist_dict loads an XML plist payload back into a dict.
    """
    payload = plistlib.dumps({"FileVault": True, "Locked": False}).decode(
        "utf-8",
    )
    parsed = _parse_plist_dict(output=payload)
    testbase.assert_equal(parsed["FileVault"], True)
    testbase.assert_equal(parsed["Locked"], False)
    return


def test_parse_apfs_list_emits_only_encrypted_volumes():
    """
        _parse_apfs_list_plist filters out volumes whose
        ``Encryption`` and ``FileVault`` flags are both False.
    """
    payload = plistlib.dumps({
        "Containers": [
            {
                "Volumes": [
                    {
                        "DeviceIdentifier": "disk2s1",
                        "Name": "Boot",
                        "MountPoint": "/",
                        "Encryption": True,
                        "FileVault": True,
                        "Locked": False,
                    },
                    {
                        "DeviceIdentifier": "disk2s5",
                        "Name": "Plain",
                        "MountPoint": "/Volumes/Plain",
                        "Encryption": False,
                        "FileVault": False,
                        "Locked": False,
                    },
                ],
            },
        ],
    }).decode("utf-8")
    rows = _parse_apfs_list_plist(output=payload)
    testbase.assert_equal(len(rows), 1)
    testbase.assert_equal(rows[0].device, "/dev/disk2s1")
    return
