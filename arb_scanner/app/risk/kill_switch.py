"""Kill switch: engages via code, or by the presence of a file on disk so an
operator can stop alerts/execution without touching the process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class KillSwitch:
    flag_file: Path | None = None
    _engaged: bool = False

    @property
    def engaged(self) -> bool:
        if self._engaged:
            return True
        return self.flag_file is not None and self.flag_file.exists()

    def engage(self) -> None:
        self._engaged = True

    def reset(self) -> None:
        self._engaged = False
