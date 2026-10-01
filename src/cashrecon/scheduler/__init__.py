"""OS scheduler integration (launchd on macOS, Task Scheduler on Windows)."""

from __future__ import annotations

from typing import Any


def status(settings: Any) -> list[str]:
    return ["调度：尚未安装（运行 `cashrecon schedule install`）"]
