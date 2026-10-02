"""OS scheduler integration: launchd (macOS) and Task Scheduler (Windows).

Jobs run ``python -m cashrecon --home <HOME> run <job>``. Both schedulers use the
machine's local time, so the machine should be set to China Standard Time (UTC+8);
``doctor`` warns otherwise. Missed runs (machine asleep/off) are started when the
machine wakes: launchd does this for calendar jobs, and the Windows tasks enable
"StartWhenAvailable". The pipeline's self-healing covers anything still missed.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from cashrecon.paths import IS_MAC, IS_WINDOWS

LABEL_PREFIX = "com.sitecashrecon"
TASK_FOLDER = "SiteCashRecon"
JOBS = ("daily", "retry", "weekly", "monthly", "monthly_final")
WEEKDAYS = {"MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6, "SUN": 0}
WIN_DAYS = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday"}


class ScheduleError(ValueError):
    pass


@dataclass
class Slot:
    hour: int
    minute: int
    weekday: int | None = None  # 0=Sunday (launchd convention)
    day: int | None = None


def parse_slot(job: str, text: str) -> Slot:
    parts = str(text).split()
    try:
        hour, minute = (int(x) for x in parts[-1].split(":"))
    except ValueError:
        raise ScheduleError(f"{job}: 时间格式应为 HH:MM") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ScheduleError(f"{job}: 时间超出范围")
    if job == "weekly":
        if len(parts) != 2 or parts[0].upper() not in WEEKDAYS:
            raise ScheduleError("weekly 格式应为 'MON 12:20'")
        return Slot(hour, minute, weekday=WEEKDAYS[parts[0].upper()])
    if job in ("monthly", "monthly_final"):
        if len(parts) != 2 or not parts[0].isdigit() or not 1 <= int(parts[0]) <= 28:
            raise ScheduleError("monthly 格式应为 '3 12:30'（日期 1–28）")
        return Slot(hour, minute, day=int(parts[0]))
    return Slot(hour, minute)


def command(home: Path, job: str, python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "cashrecon", "--home", str(home), "run", job]


def console_command(home: Path, python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "cashrecon", "--home", str(home), "web"]


def _path_env() -> str:
    """PATH for scheduled jobs: system defaults plus the directories of tools we call."""
    dirs = []
    for tool in ("agently-cli", "node"):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    base = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return os.pathsep.join(dict.fromkeys(dirs + base))


# ---------------------------------------------------------------- macOS
def launchd_plist(home: Path, job: str, slot: Slot | None, python: str | None = None) -> bytes:
    label = f"{LABEL_PREFIX}.{job}"
    logs = home / "logs"
    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": console_command(home, python) if job == "console" else command(home, job, python),
        "EnvironmentVariables": {"TZ": "Asia/Shanghai", "PATH": _path_env(), "PYTHONIOENCODING": "utf-8"},
        "StandardOutPath": str(logs / f"launchd-{job}.out.log"),
        "StandardErrorPath": str(logs / f"launchd-{job}.err.log"),
        "WorkingDirectory": str(home),
        "ProcessType": "Background",
    }
    if job == "console":
        plist.update(RunAtLoad=True, KeepAlive=True)
    else:
        assert slot is not None
        interval: dict[str, int] = {"Hour": slot.hour, "Minute": slot.minute}
        if slot.weekday is not None:
            interval["Weekday"] = slot.weekday
        if slot.day is not None:
            interval["Day"] = slot.day
        plist["StartCalendarInterval"] = interval
    return plistlib.dumps(plist)


def _launch_agents() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


# ---------------------------------------------------------------- Windows
def windows_task_xml(home: Path, job: str, slot: Slot | None, python: str | None = None) -> str:
    argv = console_command(home, python) if job == "console" else command(home, job, python)
    exe, args = argv[0], " ".join(f'"{a}"' if " " in a else a for a in argv[1:])
    start = datetime.now().replace(hour=slot.hour if slot else 0, minute=slot.minute if slot else 0,
                                   second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
    if job == "console":
        trigger = "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
    elif slot and slot.weekday is not None:
        trigger = (f"<CalendarTrigger><StartBoundary>{start}</StartBoundary><ScheduleByWeek><DaysOfWeek>"
                   f"<{WIN_DAYS[slot.weekday]} /></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
                   "</CalendarTrigger>")
    elif slot and slot.day is not None:
        months = "".join(f"<{m} />" for m in ("January", "February", "March", "April", "May", "June", "July",
                                               "August", "September", "October", "November", "December"))
        trigger = (f"<CalendarTrigger><StartBoundary>{start}</StartBoundary><ScheduleByMonth><DaysOfMonth>"
                   f"<Day>{slot.day}</Day></DaysOfMonth><Months>{months}</Months></ScheduleByMonth></CalendarTrigger>")
    else:
        trigger = (f"<CalendarTrigger><StartBoundary>{start}</StartBoundary><ScheduleByDay><DaysInterval>1"
                   "</DaysInterval></ScheduleByDay></CalendarTrigger>")
    limit = "PT0S" if job == "console" else "PT2H"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>site-cash-recon {escape(job)}</Description></RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(args)}</Arguments>
    <WorkingDirectory>{escape(str(home))}</WorkingDirectory></Exec></Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, check=False)


# ---------------------------------------------------------------- public API
def plan(settings: Any, with_console: bool = False) -> list[tuple[str, Slot | None]]:
    jobs: list[tuple[str, Slot | None]] = [(job, parse_slot(job, settings.schedule[job])) for job in JOBS]
    if with_console:
        jobs.append(("console", None))
    return jobs


def install(settings: Any, *, with_console: bool = False, python: str | None = None) -> list[str]:
    home = settings.paths.home
    (home / "logs").mkdir(parents=True, exist_ok=True)
    done = []
    for job, slot in plan(settings, with_console):
        if IS_MAC:
            target = _launch_agents() / f"{LABEL_PREFIX}.{job}.plist"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(launchd_plist(home, job, slot, python))
            domain = f"gui/{os.getuid()}"
            _launchctl("bootout", domain, str(target))
            result = _launchctl("bootstrap", domain, str(target))
            if result.returncode != 0:
                raise ScheduleError(f"launchctl bootstrap {job} 失败：{result.stderr.strip()}")
            done.append(f"{job}: {target}")
        elif IS_WINDOWS:
            xml_path = home / "logs" / f"task-{job}.xml"
            xml_path.write_text(windows_task_xml(home, job, slot, python), encoding="utf-16")
            result = _schtasks("/Create", "/TN", f"\\{TASK_FOLDER}\\{job}", "/XML", str(xml_path), "/F")
            if result.returncode != 0:
                raise ScheduleError(f"schtasks {job} 失败：{result.stderr.strip() or result.stdout.strip()}")
            done.append(f"{job}: \\{TASK_FOLDER}\\{job}")
        else:
            raise ScheduleError("当前系统不支持自动注册，请用 cron 调用：" + " ".join(command(home, job, python)))
    return done


def uninstall(settings: Any) -> list[str]:
    removed = []
    for job in (*JOBS, "console"):
        if IS_MAC:
            target = _launch_agents() / f"{LABEL_PREFIX}.{job}.plist"
            if target.exists():
                _launchctl("bootout", f"gui/{os.getuid()}", str(target))
                target.unlink()
                removed.append(job)
        elif IS_WINDOWS:
            if _schtasks("/Delete", "/TN", f"\\{TASK_FOLDER}\\{job}", "/F").returncode == 0:
                removed.append(job)
    return removed


def status(settings: Any) -> list[str]:
    lines = []
    for job in (*JOBS, "console"):
        if IS_MAC:
            target = _launch_agents() / f"{LABEL_PREFIX}.{job}.plist"
            if not target.exists():
                if job != "console":
                    lines.append(f"{job}：未安装")
                continue
            loaded = _launchctl("print", f"gui/{os.getuid()}/{LABEL_PREFIX}.{job}").returncode == 0
            lines.append(f"{job}：已安装{'并已加载' if loaded else '，但未加载'}（{settings.schedule.get(job, '常驻')}）")
        elif IS_WINDOWS:
            ok = _schtasks("/Query", "/TN", f"\\{TASK_FOLDER}\\{job}").returncode == 0
            if ok or job != "console":
                lines.append(f"{job}：{'已安装' if ok else '未安装'}")
        else:
            lines.append("当前系统不支持自动检测调度")
            break
    offset = datetime.now().astimezone().utcoffset()
    if offset is not None and offset.total_seconds() != 8 * 3600:
        lines.append("注意：本机时区不是 UTC+8，调度时刻将按本机时间执行")
    return lines
