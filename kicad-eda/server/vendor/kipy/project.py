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

from typing import overload

from google.protobuf.empty_pb2 import Empty

from kipy.client import KiCadClient
from kipy.project_types import NetClass, TextVariables
from kipy.proto.common.commands import project_commands_pb2
from kipy.proto.common.types import (
    DocumentSpecifier,
    DocumentType,
    MapMergeMode,
    project_settings_pb2,
)


class Project:
    def __init__(self, kicad: KiCadClient, document: DocumentSpecifier):
        self._kicad = kicad
        self._doc = DocumentSpecifier()
        self._doc.CopyFrom(document)

        # TODO clean this up; no identifier for project right now
        if self._doc.type != DocumentType.DOCTYPE_PROJECT:
            self._doc.type = DocumentType.DOCTYPE_PROJECT

    def __repr__(self) -> str:
        return f"Project(name={self.name!r}, path={self.path!r})"

    @property
    def document(self) -> DocumentSpecifier:
        return self._doc

    @property
    def name(self) -> str:
        """Returns the name of the project"""
        return self._doc.project.name

    @property
    def path(self) -> str:
        return self._doc.project.path

    def get_net_classes(self) -> list[NetClass]:
        command = project_commands_pb2.GetNetClasses()
        response = self._kicad.send(command, project_commands_pb2.NetClassesResponse)
        return [NetClass(p) for p in response.net_classes]

    def set_net_classes(
        self,
        net_classes: list[NetClass],
        merge_mode: MapMergeMode.ValueType = MapMergeMode.MMM_MERGE,
    ):
        """Sets the project net classes, merging with existing classes by default

        :param net_classes: the net classes to set
        :param merge_mode: ``MMM_MERGE`` merges the given classes into the existing set,
                           ``MMM_REPLACE`` replaces the existing set of net classes

        .. versionadded:: 0.9.0"""
        command = project_commands_pb2.SetNetClasses()
        command.merge_mode = merge_mode
        command.net_classes.extend([nc.proto for nc in net_classes])
        self._kicad.send(command, Empty)

    @overload
    def expand_text_variables(self, text: str, *, expand_env_vars: bool = False) -> str: ...

    @overload
    def expand_text_variables(
        self, text: list[str], *, expand_env_vars: bool = False
    ) -> list[str]: ...

    def expand_text_variables(
        self, text: str | list[str], *, expand_env_vars: bool = False
    ) -> str | list[str]:
        """Expands text variables in the given text, optionally expanding environment variables

        .. param:: expand_env_vars will expand environment variables in addition to built-in
                                           KiCad text variables when True.
        """

        command = project_commands_pb2.ExpandTextVariables()
        command.document.CopyFrom(self._doc)
        command.expand_env_vars = expand_env_vars
        if isinstance(text, list):
            command.text.extend(text)
        else:
            command.text.append(text)
        response = self._kicad.send(command, project_commands_pb2.ExpandTextVariablesResponse)
        return (
            [text for text in response.text]
            if isinstance(text, list)
            else response.text[0]
            if len(response.text) > 0
            else ""
        )

    def get_text_variables(self) -> TextVariables:
        command = project_commands_pb2.GetTextVariables()
        command.document.CopyFrom(self._doc)
        response = self._kicad.send(command, project_settings_pb2.TextVariables)
        return TextVariables(response)

    def set_text_variables(
        self, variables: TextVariables, merge_mode: MapMergeMode.ValueType = MapMergeMode.MMM_MERGE
    ):
        command = project_commands_pb2.SetTextVariables()
        command.document.CopyFrom(self._doc)
        command.merge_mode = merge_mode
        command.variables.CopyFrom(variables.proto)
        self._kicad.send(command, Empty)
