from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from cashrecon.scheduler import ScheduleError, launchd_plist, parse_slot, plan, windows_task_xml


def test_parse_slots():
    assert parse_slot("daily", "12:10").__dict__ == {"hour": 12, "minute": 10, "weekday": None, "day": None}
    assert parse_slot("weekly", "MON 12:20").weekday == 1
    assert parse_slot("monthly", "3 12:30").day == 3
    for job, text in (("daily", "25:00"), ("weekly", "XYZ 1:00"), ("monthly", "31 1:00"), ("daily", "noon")):
        with pytest.raises(ScheduleError):
            parse_slot(job, text)


def test_plan_uses_settings(settings):
    jobs = dict(plan(settings, with_console=True))
    assert set(jobs) == {"daily", "retry", "weekly", "monthly", "console"}
    assert (jobs["retry"].hour, jobs["retry"].minute) == (18, 0)


def test_launchd_plist_logs_and_calendar():
    home = Path("/tmp/home")
    data = plistlib.loads(launchd_plist(home, "weekly", parse_slot("weekly", "MON 12:20"), "/py"))
    assert data["ProgramArguments"] == ["/py", "-m", "cashrecon", "--home", "/tmp/home", "run", "weekly"]
    assert data["StartCalendarInterval"] == {"Hour": 12, "Minute": 20, "Weekday": 1}
    assert data["StandardErrorPath"].endswith("launchd-weekly.err.log")
    assert data["EnvironmentVariables"]["TZ"] == "Asia/Shanghai"
    console = plistlib.loads(launchd_plist(home, "console", None, "/py"))
    assert console["KeepAlive"] is True and console["ProgramArguments"][-1] == "web"


def test_windows_xml():
    home = Path("C:/Users/x/.site-cash-recon")
    xml = windows_task_xml(home, "monthly", parse_slot("monthly", "3 12:30"), "C:/py/python.exe")
    assert "<Day>3</Day>" in xml and "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "run monthly" in xml and "T12:30:00" in xml
    weekly = windows_task_xml(home, "weekly", parse_slot("weekly", "MON 12:20"), "C:/py/python.exe")
    assert "<Monday />" in weekly
    assert "<LogonTrigger>" in windows_task_xml(home, "console", None, "C:/py/python.exe")
