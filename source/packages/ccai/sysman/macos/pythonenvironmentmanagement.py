"""
    macOS python-environment-management feature.

    Implements the cross-OS python-environment-management contract for
    macOS.  All venv / pip behaviour is inherited from
    :class:`PythonEnvironmentManagementBase`; this module only selects
    the POSIX layout (``<venv>/bin/python``, ``python3`` launcher,
    ``test -d`` / ``rm -rf`` probes) and adds the macOS prerequisite step.

    Interpreter caveat:
        A stock macOS install ships a ``/usr/bin/python3`` *stub* that
        belongs to the Xcode Command Line Tools.  Invoking it on a host
        without the tools installed triggers an interactive install
        prompt (GUI) or a non-zero exit (SSH).  Fleet automation should
        either install the tools out-of-band or point
        ``base_interpreter`` at a Homebrew interpreter
        (``/opt/homebrew/bin/python3`` on Apple silicon,
        ``/usr/local/bin/python3`` on Intel).

    Prerequisites:
        :meth:`MacosPythonEnvironmentManager.ensure_prerequisites`
        installs the ``python`` Homebrew formula (a versioned formula
        such as ``python@3.12`` can be configured) through the existing
        :class:`MacosPackageManager`.  That manager enforces Homebrew's
        refuse-root rule — it never elevates and wraps argv with
        ``sudo -u <brew_user>`` when configured — so the elevation
        inversion documented for package-management applies to the
        prerequisite step only; venv and pip calls follow the standard
        ``config.elevated`` rule.

    See:
        * :doc:`/specs/contracts/python-environment-management/contract.md`
        * :doc:`/specs/python-environment-management-base/design.md`
        * :doc:`/specs/3-python-environment-management/design.md`
"""

from dataclasses import dataclass
from typing import List, Optional

from ccai.sysman.common.pythonenvironmentlayout import (
    PosixPythonEnvironmentLayout,
    PythonEnvironmentLayout,
)
from ccai.sysman.common.pythonenvironmentmanagementbase import (
    PythonEnvActionResult,
    PythonEnvActionStatus,
    PythonEnvDeployResult,
    PythonEnvInfo,
    PythonEnvManagerConfig,
    PythonEnvQueryError,
    PythonEnvSpec,
    PythonEnvironmentManagementBase,
    PythonPackageInfo,
)

from ccai.sysman.macos.packagemanagement import (
    MacosPackageActionResult,
    MacosPackageActionStatus,
    MacosPackageManager,
    MacosPackageManagerConfig,
)


MacosPythonEnvActionStatus = PythonEnvActionStatus
MacosPythonEnvActionResult = PythonEnvActionResult
MacosPythonEnvDeployResult = PythonEnvDeployResult
MacosPythonEnvInfo = PythonEnvInfo
MacosPythonEnvQueryError = PythonEnvQueryError
MacosPythonEnvSpec = PythonEnvSpec
MacosPythonPackageInfo = PythonPackageInfo


_DEFAULT_PREREQUISITE_PACKAGES = ["python"]


@dataclass
class MacosPythonEnvManagerConfig(PythonEnvManagerConfig):
    """
        Configuration for :class:`MacosPythonEnvironmentManager`.
        Inherits every contract field from :class:`PythonEnvManagerConfig`.

        :param prerequisite_packages: Homebrew formulae installed by
                                      :meth:`ensure_prerequisites`.
                                      ``None`` (the default) means the
                                      ``python`` formula.
        :param brew_user: Forwarded to
                          :class:`MacosPackageManagerConfig`; when the
                          calling process is root, ``brew`` is run as
                          this user.
    """
    prerequisite_packages: Optional[List[str]] = None
    brew_user: Optional[str] = None


class MacosPythonEnvironmentManager(PythonEnvironmentManagementBase):
    """
        macOS concrete implementation of the python-environment-management
        contract.
    """

    def _build_layout(self) -> PythonEnvironmentLayout:
        """
            :returns: The POSIX layout.
        """
        rtnval = PosixPythonEnvironmentLayout()
        return rtnval

    def ensure_prerequisites(
        self,
        packages: Optional[List[str]] = None,
    ) -> List[MacosPackageActionResult]:
        """
            Install the Homebrew formulae providing the base interpreter,
            delegating to :class:`MacosPackageManager`.

            Each formula is installed in order; the sequence stops at the
            first ``FAILED`` result.  Dry-run follows this manager's
            config.  Elevation is handled by the package manager's
            Homebrew rule (never elevate; ``sudo -u brew_user`` wrap).

            :param packages: Override of the formula list; ``None`` uses
                             the config, then the module default.

            :returns: One :class:`MacosPackageActionResult` per formula
                      attempted, in order.
        """
        names = self._resolve_prerequisite_packages(packages=packages)
        package_manager = self._build_package_manager()

        rtnval: List[MacosPackageActionResult] = []
        for name in names:
            result = package_manager.install_package(name=name)
            rtnval.append(result)
            if result.status == MacosPackageActionStatus.FAILED:
                break
        return rtnval

    def _resolve_prerequisite_packages(
        self,
        *,
        packages: Optional[List[str]],
    ) -> List[str]:
        """
            Pick the prerequisite list: explicit argument, then config,
            then the module default.

            :param packages: Caller-supplied override or ``None``.

            :returns: A copy of the chosen list.
        """
        if packages is not None and len(packages) > 0:
            rtnval = list(packages)
            return rtnval

        config = self._typed_config()
        if config.prerequisite_packages is not None and len(config.prerequisite_packages) > 0:
            rtnval = list(config.prerequisite_packages)
            return rtnval

        rtnval = list(_DEFAULT_PREREQUISITE_PACKAGES)
        return rtnval

    def _build_package_manager(self) -> MacosPackageManager:
        """
            Construct the :class:`MacosPackageManager` used for
            prerequisite installs, sharing this manager's runner, its
            ``dry_run`` / ``elevated`` flags, and the ``brew_user``.

            :returns: The package manager.
        """
        config = self._typed_config()
        package_config = MacosPackageManagerConfig(
            dry_run=config.dry_run,
            elevated=config.elevated,
            brew_user=config.brew_user,
        )
        rtnval = MacosPackageManager(runner=self._runner, config=package_config)
        return rtnval

    def _typed_config(self) -> MacosPythonEnvManagerConfig:
        """
            Return the config as :class:`MacosPythonEnvManagerConfig`,
            upgrading a plain :class:`PythonEnvManagerConfig` so the
            macOS-only fields read as their defaults.

            :returns: The typed config.
        """
        if isinstance(self._config, MacosPythonEnvManagerConfig):
            rtnval = self._config
            return rtnval

        rtnval = MacosPythonEnvManagerConfig(
            dry_run=self._config.dry_run,
            elevated=self._config.elevated,
            base_interpreter=self._config.base_interpreter,
            index_url=self._config.index_url,
            extra_index_urls=self._config.extra_index_urls,
            trusted_hosts=self._config.trusted_hosts,
            pip_options=self._config.pip_options,
            upgrade_pip=self._config.upgrade_pip,
            system_site_packages=self._config.system_site_packages,
        )
        return rtnval


__all__ = [
    "MacosPythonEnvActionResult",
    "MacosPythonEnvActionStatus",
    "MacosPythonEnvDeployResult",
    "MacosPythonEnvInfo",
    "MacosPythonEnvManagerConfig",
    "MacosPythonEnvQueryError",
    "MacosPythonEnvSpec",
    "MacosPythonEnvironmentManager",
    "MacosPythonPackageInfo",
]
