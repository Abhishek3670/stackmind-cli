"""Contained process execution rooted in a ScratchWorkspace."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import PurePath
from typing import Sequence

from .workspace import ScratchWorkspace, WorkspaceEscapeError


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessSandbox:
    def __init__(self, workspace: ScratchWorkspace) -> None:
        self.workspace = workspace

    @staticmethod
    def _validate(command: Sequence[str]) -> None:
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("Commands must be a non-empty string sequence")
        for argument in command[1:]:
            path = PurePath(argument.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise WorkspaceEscapeError("Command contains an out-of-workspace path")

    def run(self, command: Sequence[str], timeout: float = 30) -> CommandResult:
        self._validate(command)
        completed = subprocess.run(list(command), cwd=self.workspace.root, shell=False, text=True,
                                   capture_output=True, timeout=timeout, check=False)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)
