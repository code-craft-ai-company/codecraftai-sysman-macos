"""
    Execution aspects that compose cross-cutting behaviour onto the
    macOS command runners.
"""

from ccai.sysman.macos.runners.aspects.base import IMacosExecutionAspect
from ccai.sysman.macos.runners.aspects.persistence import (
    MacosPersistenceAspect,
    MacosPersistenceAspectConfig,
)
from ccai.sysman.macos.runners.aspects.retry import (
    MacosRetryAspect,
    MacosRetryAspectConfig,
    MacosRetryDecision,
)
from ccai.sysman.macos.runners.aspects.timeout import (
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
