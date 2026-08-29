from abc import ABC, abstractmethod


class ServiceProviderInterface(ABC):
    """A request-scoped service constructed by FastAPI dependency injection."""

    @staticmethod
    @abstractmethod
    def get_with_deps(*args, **kwargs) -> ServiceProviderInterface:
        """All subclasses must implement this method"""
