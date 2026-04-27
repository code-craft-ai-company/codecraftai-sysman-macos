"""
    Execution aspects that compose cross-cutting behaviour onto the
    macOS command runners.
"""

from automojo.sysman.macos.runners.aspects.base import IMacosExecutionAspect
from automojo.sysman.macos.runners.aspects.persistence import (
    MacosPersistenceAspect,
    MacosPersistenceAspectConfig,
)
from automojo.sysman.macos.runners.aspects.retry import (
    MacosRetryAspect,
    MacosRetryAspectConfig,
    MacosRetryDecision,
)
from automojo.sysman.macos.runners.aspects.timeout import (
    MacosTimeoutAspect,
    MacosTimeoutAspectConfig,
)


__all__ = [
    "IMacosExecutionAspect",
    "MacosPersistenceAspect",
    "MacosPersistenceAspectConfig",
    "MacosRetryAspect",
    "MacosRetryAspectConfig",
    "MacosRetryDecision",
    "MacosTimeoutAspect",
    "MacosTimeoutAspectConfig",
]
