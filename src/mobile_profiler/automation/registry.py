"""Thread-safe component registry used by future adapter composition roots."""

from __future__ import annotations

import threading
from typing import Dict, Generic, Iterable, TypeVar

from .contracts import ComponentDescriptor


T = TypeVar("T")


class ComponentRegistry(Generic[T]):
    """Registers named components without importing their concrete implementations."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._components: Dict[str, T] = {}
        self._descriptors: Dict[str, ComponentDescriptor] = {}

    def register(
        self,
        descriptor: ComponentDescriptor,
        component: T,
        *,
        replace: bool = False,
    ) -> None:
        with self._lock:
            if descriptor.name in self._components and not replace:
                raise ValueError(f"component already registered: {descriptor.name}")
            self._components[descriptor.name] = component
            self._descriptors[descriptor.name] = descriptor

    def unregister(self, name: str) -> T:
        with self._lock:
            component = self.resolve(name)
            del self._components[name]
            del self._descriptors[name]
            return component

    def resolve(self, name: str) -> T:
        with self._lock:
            try:
                return self._components[name]
            except KeyError as exc:
                raise KeyError(f"component is not registered: {name}") from exc

    def descriptor(self, name: str) -> ComponentDescriptor:
        with self._lock:
            try:
                return self._descriptors[name]
            except KeyError as exc:
                raise KeyError(f"component is not registered: {name}") from exc

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._components))

    def descriptors(self) -> tuple[ComponentDescriptor, ...]:
        with self._lock:
            return tuple(self._descriptors[name] for name in sorted(self._descriptors))

    def update(
        self,
        entries: Iterable[tuple[ComponentDescriptor, T]],
        *,
        replace: bool = False,
    ) -> None:
        for descriptor, component in entries:
            self.register(descriptor, component, replace=replace)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._components

    def __len__(self) -> int:
        with self._lock:
            return len(self._components)
