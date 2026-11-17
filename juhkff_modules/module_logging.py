"""Gate plugin logs by their owning feature, including shared async helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from .common import BaseModule


module_log_scope: ContextVar[BaseModule | None] = ContextVar(
    "juhkff_module_log_scope", default=None
)


class ModuleLogger:
    """Forward logs only while the owning module permits them."""

    def __init__(self, owner: BaseModule | None = None) -> None:
        """Bind a module or resolve the current async scope for every call.

        Args:
            owner: Explicit module owner; None uses the current context.
        """
        self.owner = owner

    @property
    def enabled(self) -> bool:
        """Return the owner's current logging setting, or False without an owner."""
        owner = self.owner if self.owner is not None else module_log_scope.get()
        return owner is not None and bool(getattr(owner, "log_enabled", False))

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Attribute shared helper logs to this logger's owner until scope exit.

        Yields:
            Control to the caller with the owning module in the current context.
        """
        token = module_log_scope.set(self.owner)
        try:
            yield
        finally:
            module_log_scope.reset(token)

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        if self.enabled:
            logger.info(msg, *args, **kwargs)

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        if self.enabled:
            logger.debug(msg, *args, **kwargs)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        if self.enabled:
            logger.warning(msg, *args, **kwargs)

    warn = warning

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        if self.enabled:
            logger.error(msg, *args, **kwargs)

    def exception(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        if self.enabled:
            logger.exception(msg, *args, **kwargs)
