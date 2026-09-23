"""
    Base protocol shared by every macOS execution aspect.

    Aspects compose cross-cutting behaviour (persistence, retry,
    timeout) onto a :class:`MacosLocalCommandRunner` /
    :class:`MacosSshCommandRunner` without modifying the runner.  The
    runner invokes ``before_execute`` in registration order before
    dispatching the command and ``after_execute`` in reverse order
    afterwards, threading the :class:`MacosRunResult` through each
    aspect.
"""

from typing import Protocol, runtime_checkable

from ccai.sysman.macos.runners.models import (
    MacosExecutionContext,
    MacosRunResult,
)


__all__ = [
    "IMacosExecutionAspect",
]


@runtime_checkable
class IMacosExecutionAspect(Protocol):
    """
        Protocol that every macOS execution aspect satisfies.
    """

    def before_execute(self, context: MacosExecutionContext) -> None:
        """
            Invoked before the command is dispatched.  Aspects may
            inspect or mutate ``context``; raising aborts execution.

            :param context: The execution context for the pending
                            command.
        """

    def after_execute(
        self,
        context: MacosExecutionContext,
        result: MacosRunResult,
    ) -> MacosRunResult:
        """
            Invoked after the command has completed.  Aspects may
            inspect or transform ``result`` before returning it.

            :param context: The execution context for the completed
                            command.
            :param result: The result returned by the runner.

            :returns: The (possibly modified) :class:`MacosRunResult`.
        """

    @property
    def enabled(self) -> bool:
        """
            Whether the aspect is currently enabled.  Disabled aspects
            are skipped by the runner.
        """
