"""
    Tests for :class:`MacosStorageManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  The ``diskutil ... -plist`` -> ``plutil -convert json``
    pipeline is mocked at the runner layer; tests pre-seed the JSON
    response that the manager would otherwise have built by piping the
    plist through ``plutil``.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.storagemanagement import (
    MacosDiskInfo,
    MacosPoolSpec,
    MacosStorageActionStatus,
    MacosStorageManager,
    MacosStorageManagerConfig,
    MacosVolumeInfo,
    MacosVolumeSpec,
    _container_in_apfs_payload,
    _disk_in_apfs_container,
    _first_container_for_any_disk,
    _parse_diskutil_apfs_list,
    _parse_diskutil_list_output,
    _strip_partition_suffix,
)


_DISKUTIL_LIST_PHYS = ("diskutil", "list", "-plist", "physical")
_DISKUTIL_LIST = ("diskutil", "list", "-plist")
_DISKUTIL_APFS_LIST = ("diskutil", "apfs", "list", "-plist")
_PLUTIL_ARGV = ("plutil", "-convert", "json", "-o", "-", "-")


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values.

        The macOS manager pipes ``diskutil ... -plist`` through
        ``plutil -convert json`` via ``stdin``.  This mock matches both
        argv (when present in ``results``) and the plutil-with-stdin
        case (when ``stdin`` is non-empty and the argv is the plutil
        invocation): the returned response then carries the JSON the
        coordinator pre-seeded against ``plutil_responses[stdin]``.
    """

    results: dict = field(default_factory=dict)
    plutil_responses: dict = field(default_factory=dict)
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
        if key == _PLUTIL_ARGV and stdin is not None:
            if stdin in self.plutil_responses:
                rtnval = self.plutil_responses[stdin]
                return rtnval
            rtnval = RunResult(exit_code=0, stdout=stdin, stderr="")
            return rtnval

        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _seed_apfs_list(
    *,
    runner: _MockCommandRunner,
    payload: dict,
) -> None:
    """
        Wire the ``diskutil apfs list -plist`` -> plutil pipeline so
        that the manager parses ``payload``.
    """
    sentinel = "<plist>{}</plist>".format(json.dumps(payload))
    runner.results[_DISKUTIL_APFS_LIST] = RunResult(
        exit_code=0, stdout=sentinel, stderr="",
    )
    runner.plutil_responses[sentinel] = RunResult(
        exit_code=0, stdout=json.dumps(payload), stderr="",
    )
    return


def _seed_diskutil_list(
    *,
    runner: _MockCommandRunner,
    payload: dict,
    physical: bool = False,
) -> None:
    """
        Wire the ``diskutil list -plist [physical]`` -> plutil pipeline.
    """
    sentinel = "<list-{}>{}</list>".format(
        "phys" if physical else "all", json.dumps(payload),
    )
    if physical:
        runner.results[_DISKUTIL_LIST_PHYS] = RunResult(
            exit_code=0, stdout=sentinel, stderr="",
        )
    else:
        runner.results[_DISKUTIL_LIST] = RunResult(
            exit_code=0, stdout=sentinel, stderr="",
        )
    runner.plutil_responses[sentinel] = RunResult(
        exit_code=0, stdout=json.dumps(payload), stderr="",
    )
    return


# ----------------------------------------------------------------------
# list_disks
# ----------------------------------------------------------------------


def test_list_disks_invokes_diskutil_and_parses():
    """
        list_disks invokes ``diskutil list -plist physical`` and
        returns one MacosDiskInfo per disk.
    """
    runner = _MockCommandRunner()
    _seed_diskutil_list(
        runner=runner,
        physical=True,
        payload={
            "AllDisks": ["disk0", "disk1"],
            "AllDisksAndPartitions": [
                {
                    "DeviceIdentifier": "disk0",
                    "Size": 500107862016,
                    "Content": "GUID_partition_scheme",
                    "Partitions": [
                        {
                            "DeviceIdentifier": "disk0s1",
                            "Content": "EFI",
                            "Size": 209715200,
                            "MountPoint": "/Volumes/EFI",
                        },
                        {
                            "DeviceIdentifier": "disk0s2",
                            "Content": "Apple_APFS",
                            "Size": 499898146816,
                        },
                    ],
                },
                {
                    "DeviceIdentifier": "disk1",
                    "Size": 1000204886016,
                    "Content": "GUID_partition_scheme",
                    "Partitions": [],
                },
            ],
        },
    )

    manager = MacosStorageManager(
        runner=runner, config=MacosStorageManagerConfig(),
    )
    disks = manager.list_disks()

    by_id = {d.id: d for d in disks}
    testbase.assert_true("disk0" in by_id)
    testbase.assert_true("disk1" in by_id)
    testbase.assert_equal(by_id["disk0"].size_bytes, 500107862016)
    testbase.assert_equal(
        by_id["disk0"].partition_style, "GUID_partition_scheme",
    )
    testbase.assert_equal(by_id["disk0"].is_pool_member, True)
    testbase.assert_equal(by_id["disk1"].is_pool_member, False)
    testbase.assert_true("/Volumes/EFI" in by_id["disk0"].mount_points)
    return


# ----------------------------------------------------------------------
# list_volumes
# ----------------------------------------------------------------------


def test_list_volumes_parses_output():
    """
        list_volumes merges APFS volumes with non-APFS partitions.
    """
    runner = _MockCommandRunner()
    _seed_apfs_list(
        runner=runner,
        payload={
            "Containers": [
                {
                    "ContainerReference": "disk1",
                    "Volumes": [
                        {
                            "DeviceIdentifier": "disk1s1",
                            "Name": "Macintosh HD",
                            "CapacityCeiling": 499898146816,
                            "CapacityInUse": 100000000000,
                            "MountPoint": "/",
                            "FilesystemType": "apfs",
                        },
                        {
                            "DeviceIdentifier": "disk1s2",
                            "Name": "Data",
                            "CapacityCeiling": 50000000000,
                            "CapacityInUse": 0,
                            "FilesystemType": "apfs",
                        },
                    ],
                    "PhysicalStores": [
                        {"DeviceIdentifier": "disk0s2"},
                    ],
                },
            ],
        },
    )
    _seed_diskutil_list(
        runner=runner,
        payload={
            "AllDisksAndPartitions": [
                {
                    "DeviceIdentifier": "disk0",
                    "Size": 500107862016,
                    "Content": "GUID_partition_scheme",
                    "Partitions": [
                        {
                            "DeviceIdentifier": "disk0s1",
                            "Content": "EFI",
                            "Size": 209715200,
                            "MountPoint": "/Volumes/EFI",
                        },
                        {
                            "DeviceIdentifier": "disk0s2",
                            "Content": "Apple_APFS",
                            "Size": 499898146816,
                        },
                    ],
                },
            ],
        },
    )

    manager = MacosStorageManager(
        runner=runner, config=MacosStorageManagerConfig(),
    )
    volumes = manager.list_volumes()

    by_id = {v.id: v for v in volumes}
    testbase.assert_true("disk1s1" in by_id)
    testbase.assert_true("disk1s2" in by_id)
    testbase.assert_true("disk0s1" in by_id)  # EFI partition surfaced
    testbase.assert_true("disk0s2" not in by_id)  # APFS container skipped
    testbase.assert_equal(by_id["disk1s1"].pool, "disk1")
    testbase.assert_equal(by_id["disk1s1"].mount_point, "/")
    testbase.assert_equal(by_id["disk1s1"].used_bytes, 100000000000)
    testbase.assert_equal(by_id["disk0s1"].pool, None)
    return


# ----------------------------------------------------------------------
# create_volume
# ----------------------------------------------------------------------


def test_create_volume_invokes_diskutil():
    """
        create_volume dispatches ``diskutil apfs addVolume <container>
        <fs> <name> -size <bytes>B`` with elevation when configured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_apfs_list(runner=runner, payload={"Containers": []})
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    spec = MacosVolumeSpec(
        name="Data",
        pool="disk1",
        size_bytes=50_000_000_000,
        filesystem="apfs",
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, MacosStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "addVolume"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        [
            "diskutil", "apfs", "addVolume",
            "disk1", "apfs", "Data",
            "-size", "50000000000B",
        ],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_create_volume_skipped_when_already_present():
    """
        create_volume returns SKIPPED when an APFS volume with the same
        name already exists in the same container.
    """
    runner = _MockCommandRunner()
    _seed_apfs_list(
        runner=runner,
        payload={
            "Containers": [
                {
                    "ContainerReference": "disk1",
                    "Volumes": [
                        {
                            "DeviceIdentifier": "disk1s2",
                            "Name": "Data",
                            "CapacityCeiling": 50_000_000_000,
                            "CapacityInUse": 0,
                            "FilesystemType": "apfs",
                        },
                    ],
                    "PhysicalStores": [],
                },
            ],
        },
    )
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    spec = MacosVolumeSpec(
        name="Data", pool="disk1", size_bytes=50_000_000_000,
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, MacosStorageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "addVolume"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_create_volume_dry_run_short_circuits():
    """
        Dry-run records the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(dry_run=True),
    )
    spec = MacosVolumeSpec(
        name="Data", pool="disk1", size_bytes=50_000_000_000,
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, MacosStorageActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# delete_volume
# ----------------------------------------------------------------------


def test_delete_volume_invokes_diskutil():
    """
        delete_volume dispatches ``diskutil apfs deleteVolume
        <volume_id>`` with a data-loss advisory on success.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_apfs_list(
        runner=runner,
        payload={
            "Containers": [
                {
                    "ContainerReference": "disk1",
                    "Volumes": [
                        {
                            "DeviceIdentifier": "disk1s2",
                            "Name": "Data",
                            "CapacityCeiling": 50_000_000_000,
                            "FilesystemType": "apfs",
                        },
                    ],
                    "PhysicalStores": [],
                },
            ],
        },
    )
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    result = manager.delete_volume(volume_id="disk1s2")

    testbase.assert_equal(result.status, MacosStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "deleteVolume"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["diskutil", "apfs", "deleteVolume", "disk1s2"],
    )
    testbase.assert_true(result.message is not None)
    testbase.assert_true("destroyed" in result.message)
    return


def test_delete_volume_skipped_when_absent():
    """
        delete_volume returns SKIPPED when the volume is unknown.
    """
    runner = _MockCommandRunner()
    _seed_apfs_list(runner=runner, payload={"Containers": []})
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    result = manager.delete_volume(volume_id="disk1s2")

    testbase.assert_equal(result.status, MacosStorageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "deleteVolume"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# resize_volume
# ----------------------------------------------------------------------


def test_resize_volume_invokes_resizecontainer():
    """
        resize_volume on an APFS volume dispatches ``diskutil apfs
        resizeContainer <container> <bytes>B``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_apfs_list(
        runner=runner,
        payload={
            "Containers": [
                {
                    "ContainerReference": "disk1",
                    "Volumes": [
                        {
                            "DeviceIdentifier": "disk1s2",
                            "Name": "Data",
                            "CapacityCeiling": 50_000_000_000,
                            "FilesystemType": "apfs",
                        },
                    ],
                    "PhysicalStores": [],
                },
            ],
        },
    )
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    result = manager.resize_volume(
        volume_id="disk1s2", size_bytes=100_000_000_000,
    )

    testbase.assert_equal(result.status, MacosStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "resizeContainer"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        [
            "diskutil", "apfs", "resizeContainer",
            "disk1", "100000000000B",
        ],
    )
    return


def test_resize_volume_skipped_when_size_matches():
    """
        resize_volume returns SKIPPED when the current size matches.
    """
    runner = _MockCommandRunner()
    _seed_apfs_list(
        runner=runner,
        payload={
            "Containers": [
                {
                    "ContainerReference": "disk1",
                    "Volumes": [
                        {
                            "DeviceIdentifier": "disk1s2",
                            "Name": "Data",
                            "CapacityCeiling": 50_000_000_000,
                            "FilesystemType": "apfs",
                        },
                    ],
                    "PhysicalStores": [],
                },
            ],
        },
    )
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    result = manager.resize_volume(
        volume_id="disk1s2", size_bytes=50_000_000_000,
    )

    testbase.assert_equal(result.status, MacosStorageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "resizeContainer"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# create_pool / delete_pool / add_to_pool / remove_from_pool
# ----------------------------------------------------------------------


def test_create_pool_invokes_diskutil_createcontainer():
    """
        create_pool dispatches ``diskutil apfs createContainer
        <disks...>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_apfs_list(runner=runner, payload={"Containers": []})
    spec = MacosPoolSpec(name="primary-data", disks=["disk2", "disk3"])
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    result = manager.create_pool(spec=spec)

    testbase.assert_equal(result.status, MacosStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "createContainer"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["diskutil", "apfs", "createContainer", "disk2", "disk3"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_delete_pool_invokes_diskutil_deletecontainer():
    """
        delete_pool dispatches ``diskutil apfs deleteContainer
        <pool>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_apfs_list(
        runner=runner,
        payload={
            "Containers": [{"ContainerReference": "disk1", "Volumes": []}],
        },
    )
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    result = manager.delete_pool(pool_name="disk1")

    testbase.assert_equal(result.status, MacosStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "deleteContainer"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["diskutil", "apfs", "deleteContainer", "disk1"],
    )
    testbase.assert_true(result.message is not None)
    testbase.assert_true("destroyed" in result.message)
    return


def test_add_to_pool_invokes_resizecontainer_with_zero():
    """
        add_to_pool dispatches ``diskutil apfs resizeContainer <pool>
        0 <new-disk>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_apfs_list(
        runner=runner,
        payload={
            "Containers": [
                {
                    "ContainerReference": "disk1",
                    "Volumes": [],
                    "PhysicalStores": [{"DeviceIdentifier": "disk2s2"}],
                },
            ],
        },
    )
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    result = manager.add_to_pool(pool_name="disk1", disk_id="disk3")

    testbase.assert_equal(result.status, MacosStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if (
            c["args"][:3] == ["diskutil", "apfs", "resizeContainer"]
            and "-none" not in c["args"]
        )
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["diskutil", "apfs", "resizeContainer", "disk1", "0", "disk3"],
    )
    return


def test_remove_from_pool_invokes_resizecontainer_with_none():
    """
        remove_from_pool dispatches ``diskutil apfs resizeContainer
        <pool> 0 -none <disk>`` and surfaces a data-loss advisory on
        success.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_apfs_list(
        runner=runner,
        payload={
            "Containers": [
                {
                    "ContainerReference": "disk1",
                    "Volumes": [],
                    "PhysicalStores": [
                        {"DeviceIdentifier": "disk2s2"},
                    ],
                },
            ],
        },
    )
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    result = manager.remove_from_pool(pool_name="disk1", disk_id="disk2")

    testbase.assert_equal(result.status, MacosStorageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if (
            c["args"][:3] == ["diskutil", "apfs", "resizeContainer"]
            and "-none" in c["args"]
        )
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        [
            "diskutil", "apfs", "resizeContainer",
            "disk1", "0", "-none", "disk2",
        ],
    )
    testbase.assert_true(result.message is not None)
    testbase.assert_true("wiped" in result.message)
    return


# ----------------------------------------------------------------------
# get_volume_info / get_disk_info
# ----------------------------------------------------------------------


def test_get_volume_info_parses_output():
    """
        get_volume_info returns the volume matching the id.
    """
    runner = _MockCommandRunner()
    _seed_apfs_list(
        runner=runner,
        payload={
            "Containers": [
                {
                    "ContainerReference": "disk1",
                    "Volumes": [
                        {
                            "DeviceIdentifier": "disk1s2",
                            "Name": "Data",
                            "CapacityCeiling": 50_000_000_000,
                            "CapacityInUse": 12_500_000_000,
                            "FilesystemType": "apfs",
                        },
                    ],
                    "PhysicalStores": [],
                },
            ],
        },
    )
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    manager = MacosStorageManager(
        runner=runner, config=MacosStorageManagerConfig(),
    )
    info = manager.get_volume_info(volume_id="disk1s2")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.size_bytes, 50_000_000_000)
    testbase.assert_equal(info.used_bytes, 12_500_000_000)
    testbase.assert_equal(info.pool, "disk1")
    return


def test_get_disk_info_parses_output():
    """
        get_disk_info returns the disk matching the id.
    """
    runner = _MockCommandRunner()
    _seed_diskutil_list(
        runner=runner,
        physical=True,
        payload={
            "AllDisksAndPartitions": [
                {
                    "DeviceIdentifier": "disk0",
                    "Size": 500107862016,
                    "Content": "GUID_partition_scheme",
                    "Partitions": [],
                },
            ],
        },
    )
    manager = MacosStorageManager(
        runner=runner, config=MacosStorageManagerConfig(),
    )
    info = manager.get_disk_info(disk_id="disk0")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.size_bytes, 500107862016)
    testbase.assert_equal(info.partition_style, "GUID_partition_scheme")
    return


# ----------------------------------------------------------------------
# Elevation contract
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs pass ``elevated=True`` when configured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_apfs_list(runner=runner, payload={"Containers": []})
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    spec = MacosVolumeSpec(
        name="Data", pool="disk1", size_bytes=10_000_000_000,
    )
    manager.create_volume(spec=spec)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["diskutil", "apfs", "addVolume"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs never pass ``elevated=True`` even when the config
        sets elevated for writes.
    """
    runner = _MockCommandRunner()
    _seed_diskutil_list(
        runner=runner,
        physical=True,
        payload={"AllDisksAndPartitions": []},
    )
    _seed_apfs_list(runner=runner, payload={"Containers": []})
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    manager.list_disks()
    manager.list_volumes()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        FAILED writes carry the runner's exit code and stderr.
    """
    runner = _MockCommandRunner()
    _seed_apfs_list(runner=runner, payload={"Containers": []})
    _seed_diskutil_list(runner=runner, payload={"AllDisksAndPartitions": []})
    runner.default_result = RunResult(
        exit_code=2, stdout="", stderr="addVolume failed",
    )
    manager = MacosStorageManager(
        runner=runner,
        config=MacosStorageManagerConfig(elevated=True),
    )
    spec = MacosVolumeSpec(
        name="Data", pool="disk1", size_bytes=1_000_000,
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, MacosStorageActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "addVolume failed")
    return


# ----------------------------------------------------------------------
# Module-level parser tests
# ----------------------------------------------------------------------


def test_strip_partition_suffix_removes_trailing_slot():
    """
        ``_strip_partition_suffix`` removes ``sN`` suffixes.
    """
    testbase.assert_equal(
        _strip_partition_suffix(text="disk2s2"), "disk2",
    )
    testbase.assert_equal(_strip_partition_suffix(text="disk0"), "disk0")
    testbase.assert_equal(_strip_partition_suffix(text=""), "")
    return


def test_container_in_apfs_payload_detects_match():
    """
        ``_container_in_apfs_payload`` finds the container by reference.
    """
    payload = {
        "Containers": [
            {"ContainerReference": "disk1"},
            {"ContainerReference": "disk2"},
        ],
    }
    testbase.assert_equal(
        _container_in_apfs_payload(payload=payload, container_id="disk2"),
        True,
    )
    testbase.assert_equal(
        _container_in_apfs_payload(payload=payload, container_id="disk5"),
        False,
    )
    return


def test_disk_in_apfs_container_matches_by_partition_root():
    """
        ``_disk_in_apfs_container`` matches both bare and suffixed
        physical-store ids.
    """
    payload = {
        "Containers": [
            {
                "ContainerReference": "disk1",
                "PhysicalStores": [{"DeviceIdentifier": "disk2s2"}],
            },
        ],
    }
    testbase.assert_equal(
        _disk_in_apfs_container(
            payload=payload, container_id="disk1", disk_id="disk2",
        ),
        True,
    )
    testbase.assert_equal(
        _disk_in_apfs_container(
            payload=payload, container_id="disk1", disk_id="disk3",
        ),
        False,
    )
    return
