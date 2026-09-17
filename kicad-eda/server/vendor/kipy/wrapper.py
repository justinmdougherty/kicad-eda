# Copyright The KiCad Developers
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the “Software”), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from collections.abc import Iterable, MutableSequence
from typing import TypeVar

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from google.protobuf.internal.containers import RepeatedCompositeFieldContainer
from google.protobuf.message import Message

from kipy.proto.common.types.base_types_pb2 import KIID

T = TypeVar("T", bound="Wrapper")


class MutableWrapperSequence(MutableSequence[T]):
    """A mutable view over a protobuf repeated field of messages, exposing
    wrapped protos.

    This is used to provide a list-like container that can be extended without
    having to make a copy first.

    Note: slice assignment and reordering in place are not supported.  Make a
    new copy of the sequence and assign via a property setter if you need to
    do one of those operations.
    """

    def __init__(self, container: RepeatedCompositeFieldContainer, wrapper_type: type[T]):
        self._container = container
        self._wrapper_type = wrapper_type

    def __len__(self) -> int:
        return len(self._container)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._wrapper_type(proto_ref=item) for item in self._container[index]]
        return self._wrapper_type(proto_ref=self._container[index])

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            raise TypeError(
                "Slice assignment is not supported; assign the sequence via the property setter"
            )
        self._container[index].CopyFrom(value.proto)

    def __delitem__(self, index):
        del self._container[index]

    def __eq__(self, other) -> bool:
        if isinstance(other, MutableWrapperSequence):
            return len(self) == len(other) and all(a.proto == b.proto for a, b in zip(self, other))
        if isinstance(other, (list, tuple)):
            return len(self) == len(other) and all(
                item.proto == getattr(other_item, "proto", other_item)
                for item, other_item in zip(self, other)
            )
        return NotImplemented

    def __repr__(self) -> str:
        return repr(list(self))

    def insert(self, index: int, value: T):
        self._container.insert(index, type(value.proto)())
        self._container[index].CopyFrom(value.proto)

    def append(self, value: T):
        self._container.add().CopyFrom(value.proto)

    def extend(self, values: Iterable[T]):
        for value in values:
            self.append(value)

    def clear(self):
        del self._container[:]

    def sort(self, *args, **kwargs):
        raise TypeError(
            "MutableWrapperSequence cannot be reordered; rebuild the sequence and assign it via the property setter"
        )

    def reverse(self):
        raise TypeError(
            "MutableWrapperSequence cannot be reordered; rebuild the sequence and assign it via the property setter"
        )


class Wrapper(ABC):
    def __init__(self, proto: Message | None = None, proto_ref: Message | None = None):
        pass

    @property
    def proto(self):
        self._pack()
        return self.__dict__["_proto"]

    def _pack(self):
        """Used in some cases to ensure the internal proto state matches the Python
        class instance, for subclasses where the properties are not directly acting on
        the proto object.
        """


class Item(Wrapper):
    @property
    @abstractmethod
    def id(self) -> KIID:
        return KIID()

    def clone(self) -> Self:
        """Creates a copy of this item with the ID field cleared, so that it can be passed to
        create_items where KiCad will generate a new unique ID for it

        .. versionadded:: 0.8.0"""
        new_proto = self.proto.__class__()
        new_proto.CopyFrom(self.proto)
        new_proto.ClearField("id")
        return self.__class__(proto=new_proto)
