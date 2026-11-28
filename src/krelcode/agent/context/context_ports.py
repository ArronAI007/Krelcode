"""Low-level context store ports shared by models and checkpoint adapters.

These protocols intentionally live below both checkpoint DTOs and checkpoint
restore code.  Keeping them in a leaf module prevents a dataclass model and a
host port from importing each other only to describe a working-context value.
"""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence


class WorkingEntryPort(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def metadata(self) -> Mapping[str, object]: ...

    @property
    def source(self) -> str: ...

    @property
    def status(self) -> str: ...


class WorkingContextPort(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def count(self) -> int: ...

    @property
    def entries(self) -> Sequence[WorkingEntryPort]: ...

    @property
    def truncated(self) -> bool: ...

    @property
    def dropped_entries(self) -> int: ...

    @property
    def counts_by_kind(self) -> Mapping[str, int]: ...

    @property
    def sequence(self) -> int: ...

    @property
    def token_count(self) -> int: ...

    def as_checkpoint_record(self) -> Mapping[str, object]: ...


__all__ = ["WorkingContextPort", "WorkingEntryPort"]
