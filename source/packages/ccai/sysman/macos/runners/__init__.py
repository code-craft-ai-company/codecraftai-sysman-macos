"""
    Command runner implementations for the macOS sysman package.

    Public surface:

    - :class:`MacosLocalCommandRunner` -- subprocess-backed local
      runner that satisfies
      :class:`ccai.interfaces.icommandrunner.ICommandRunner`.
    - :class:`MacosSshCommandRunner` -- skeleton SSH runner that
      preserves the public surface of the local runner so that a
      future patch can plug in the transport without affecting
      callers.
    - :class:`MacosRunResult` -- the runner result dataclass.
    - The aspect classes for retry, timeout, and persistence.
"""

from ccai.sysman.macos.runners.aspects import (
    IMacosExecutionAspect,
    MacosPersistenceAspect,
    MacosPersistenceAspectConfig,
    MacosRetryAspect,
    MacosRetryAspectConfig,
    MacosRetryDecision,
    MacosTimeoutAspect,
    MacosTimeoutAspectConfig,
)
from ccai.sysman.macos.runners.local import (
    MacosLocalCommandRunner,
    MacosLocalRunnerConfig,
)
from ccai.sysman.macos.runners.models import (
    RunResult,
    MacosExecutionContext,
    MacosRunRecord,
    MacosRunResult,
    MacosSshErrorType,
    MacosSshReliabilityMetrics,
)
from ccai.sysman.macos.runners.ssh import (
    MacosSshCommandRunner,
    MacosSshRunnerConfig,
)


__all__ = [
    "IMacosExecutionAspect",
    "RunResult",
    "MacosExecutionContext",
    "MacosLocalCommandRunner",
    "MacosLocalRunnerConfig",
    "MacosPersistenceAspect",
    "MacosPersistenceAspectConfig",
    "MacosRetryAspect",
    "MacosRetryAspectConfig",
    "MacosRetryDecision",
    "MacosRunRecord",
    "MacosRunResult",
    "MacosSshCommandRunner",
    "MacosSshErrorType",
    "MacosSshReliabilityMetrics",
    "MacosSshRunnerConfig",
    "MacosTimeoutAspect",
    "MacosTimeoutAspectConfig",
]
