from abc import ABC, abstractmethod
from typing import Any


class ServiceProviderInterface(ABC):
    """A request-scoped service constructed by FastAPI dependency injection."""

    @staticmethod
    @abstractmethod
    def get_with_deps(*args: Any, **kwargs: Any) -> ServiceProviderInterface:
        """All subclasses must implement this method"""
