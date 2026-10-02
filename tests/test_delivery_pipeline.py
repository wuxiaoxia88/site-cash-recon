from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
from datetime import date

import pytest

from cashrecon.config import settings_from_dict
from cashrecon.delivery import deliver
from cashrecon.delivery.kb import KbPublisher
from cashrecon.delivery.mail import MailError, MailSender
from cashrecon.engine.accounts import sync_accounts
from cashrecon.engine.rules import ensure_default_rules
from cashrecon.pipeline import JobLocked, Runner, job_lock
from cashrecon.reports import render_report
from cashrecon.sources.base import BalanceRecord, SourceBatch
from tests.conftest import BASE_CONFIG


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def kb_settings(paths, **mail):
    data = copy.deepcopy(BASE_CONFIG)
    data["delivery"] = {"kb": {"enabled": True, "base_urls": ["http://kb.test"], "public_base_url": "https://kb.example.com"},
                        "mail": {"enabled": True, "method": "agently", "to": ["boss@example.com"], **mail}}
    return settings_from_dict(data, paths, {"ZTO_KB_AGENT_TOKEN": "t" * 20})


class FakeKb:
    def __init__(self, tamper=False):
        self.published = {}
        self.tamper = tamper
        self.calls = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.calls.append((request.get_method(), url))
        if url.endswith("/api/agent/publish-package"):
            body = json.loads(request.data)
            self.published[body["destination"] + "/index.html"] = body["files"][0]["content"]
            return Resp(json.dumps({"ok": True, "data": {"job_id": "7", "stage": "queued"}}).encode())
        if "/api/agent/jobs/" in url:
            return Resp(json.dumps({"ok": True, "data": {"stage": "published"}}).encode())
        if "/api/agent/content" in url:
            path = url.split("path=")[1].replace("%2F", "/")
            content = self.published.get(path, "") + ("x" if self.tamper else "")
            return Resp(json.dumps({"ok": True, "data": {"path": path, "content": content}}).encode())
        raise AssertionError(url)


def test_kb_publish_verifies_readback(paths):
    fake = FakeKb()
    kb = KbPublisher(kb_settings(paths), opener=fake, sleep=lambda s: None)
    receipt = kb.publish(cadence="daily", period_key="2026-09-30", title="t", summary="s", html="<p>报表</p>")
    assert receipt["status"] == "verified"
    assert receipt["destination"] == "internal/reports/cash-recon/test-site/daily/2026-09-30"
    tampered = KbPublisher(kb_settings(paths), opener=FakeKb(tamper=True), sleep=lambda s: None)
    assert tampered.publish(cadence="daily", period_key="x", title="t", summary="s", html="a")["status"] == "published"


class SlowKb(FakeKb):
    """Accepts the publish but times out before answering, like a slow commit."""

    def __call__(self, request, timeout):
        response = super().__call__(request, timeout)
        if request.full_url.endswith("/api/agent/publish-package"):
            raise TimeoutError()
        return response


def test_kb_timeout_is_resolved_by_readback_without_resending(paths):
    settings = kb_settings(paths)
    settings.delivery["kb"]["base_urls"] = ["http://kb.test", "http://kb-public.test"]
    fake = SlowKb()
    receipt = KbPublisher(settings, opener=fake, sleep=lambda s: None).publish(
        cadence="daily", period_key="d", title="t", summary="s", html="<p>x</p>")
    assert receipt["status"] == "verified" and receipt["stage"] == "timeout"
    posts = [u for m, u in fake.calls if m == "POST"]
    assert posts == ["http://kb.test/api/agent/publish-package"]  # not re-sent to the second URL
    again = KbPublisher(settings, opener=fake, sleep=lambda s: None).publish(
        cadence="daily", period_key="d", title="t", summary="s", html="<p>x</p>")
    assert again["stage"] == "already_published"
    assert len([u for m, u in fake.calls if m == "POST"]) == 1


def test_mail_agently_and_errors(paths, tmp_path):
    seen = {}

    def runner(argv, cwd, **kw):
        seen["argv"] = argv
        seen["files"] = sorted(p.name for p in cwd.iterdir())
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "data": {"queued": True}}))

    attachment = tmp_path / "日报.html"
    attachment.write_text("<p>x</p>", encoding="utf-8")
    sender = MailSender(kb_settings(paths), runner=runner)
    import shutil as _sh
    if _sh.which("agently-cli") is None:
        pytest.skip("agently-cli not installed")
    assert sender.send(subject="s", html_body="<p>b</p>", attachments=[attachment])["status"] == "sent"
    assert "--confirmed" in seen["argv"] and "./日报.html" in seen["argv"] and "body.html" in seen["files"]


def test_mail_smtp(paths, tmp_path):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout, **kw):
            sent["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            sent["tls"] = True

        def login(self, user, password):
            sent["user"] = user

        def send_message(self, msg):
            sent["subject"] = msg["Subject"]

    settings = kb_settings(paths, method="smtp")
    settings.secrets.update({"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "u@example.com",
                             "SMTP_PASSWORD": "p"})
    MailSender(settings, smtp_factory=FakeSMTP).send(subject="主题", html_body="<p>x</p>", attachments=[])
    assert sent["subject"] == "主题" and sent["user"] == "u@example.com"
    settings.secrets.pop("SMTP_HOST")
    with pytest.raises(MailError):
        MailSender(settings, smtp_factory=FakeSMTP).send(subject="s", html_body="b", attachments=[])


# ------------------------------------------------------------------ pipeline
class FakeSource:
    def __init__(self, code, fail_days=()):
        self.code, self.fail_days, self.calls = code, set(fail_days), []

    def fetch(self, day):
        self.calls.append(day)
        if day in self.fail_days:
            raise RuntimeError("upstream down")
        batch = SourceBatch(self.code, day)
        if self.code == "ZT_SUMMARY":
            batch.zt_categories = [(("主营业务收入", "进港派件收入", "x", "收派件派费"), 100000)]
            batch.balances = [BalanceRecord("ZT_MAIN", day.isoformat(), "ZT_SUMMARY", 0, 100000, 100000, 0)]
        return batch


@pytest.fixture
def pipeline_env(paths, store, monkeypatch):
    data = copy.deepcopy(BASE_CONFIG)
    settings = settings_from_dict(data, paths)
    ensure_default_rules(store)
    sync_accounts(store, settings)
    sources = {"ZT_SUMMARY": FakeSource("ZT_SUMMARY"), "JOURNAL": FakeSource("JOURNAL")}
    monkeypatch.setattr("cashrecon.ingest.build_sources", lambda settings, only=None: sources)
    return settings, store, sources


def test_daily_run_self_heals_and_reports(pipeline_env):
    settings, store, sources = pipeline_env
    sources["JOURNAL"].fail_days = {date(2026, 9, 27)}
    summary = Runner(settings, store, deliver=False).run("daily", date(2026, 9, 28))
    assert summary["status"] == "partial"  # required source failed on 9-27
    assert any("2026-09-27 JOURNAL" in f for f in summary["fetch_failed"])
    # next day: 9-27 is incomplete and gets re-fetched (self-healing); 9-28 is new
    sources["JOURNAL"].fail_days = set()
    sources["JOURNAL"].calls.clear()
    summary = Runner(settings, store, deliver=False).run("daily", date(2026, 9, 29))
    assert summary["status"] == "success"
    assert date(2026, 9, 27) in sources["JOURNAL"].calls and date(2026, 9, 28) in sources["JOURNAL"].calls
    assert (settings.paths.reports / "daily" / "2026-09-27").exists()
    runs = store.query("SELECT status FROM runs ORDER BY started_at")
    assert [r["status"] for r in runs] == ["partial", "success"]
    assert list(settings.paths.backups.glob("cashrecon-*.db"))
    assert json.loads((settings.paths.logs / "last-run.json").read_text(encoding="utf-8"))["status"] == "success"


def test_weekly_run_fetches_missing_days(pipeline_env):
    settings, store, sources = pipeline_env
    summary = Runner(settings, store, deliver=False).run("weekly", date(2026, 10, 1))
    assert summary["period"] == ["2026-09-21", "2026-09-27"]
    assert len(sources["ZT_SUMMARY"].calls) == 7
    assert (settings.paths.reports / "weekly" / "2026-09-21_2026-09-27").exists()


def test_job_lock(settings):
    with job_lock(settings):
        with pytest.raises(JobLocked):
            with job_lock(settings):
                pass
    with job_lock(settings):  # released
        pass


def test_delivery_ledger_once_only(pipeline_env, monkeypatch):
    settings, store, _ = pipeline_env
    settings.delivery = kb_settings(settings.paths).delivery
    settings.secrets["ZTO_KB_AGENT_TOKEN"] = "t" * 20
    Runner(settings, store, deliver=False).run("daily", date(2026, 9, 29))
    artifact = render_report(store, settings, "daily", date(2026, 9, 28))
    fake = FakeKb()
    monkeypatch.setattr("cashrecon.delivery.kb.urllib.request.urlopen", fake)
    monkeypatch.setattr("cashrecon.delivery.kb.time.sleep", lambda s: None)
    mails = []

    class FakeSender:
        def __init__(self, settings):
            pass

        def send(self, subject, html_body, attachments):
            mails.append(subject)
            return {"status": "sent", "detail": "queued"}

    monkeypatch.setattr("cashrecon.delivery.mail.MailSender", FakeSender)
    first = {r.channel: r.status for r in deliver(store, settings, artifact)}
    assert first == {"kb": "verified", "mail": "sent"}
    assert "本月至今" in mails[0]
    second = {r.channel: r.status for r in deliver(store, settings, artifact)}
    assert second == {"kb": "skipped", "mail": "skipped"} and len(mails) == 1
    expected = hashlib.sha256(artifact.html_path.read_bytes()).hexdigest()
    assert store.scalar("SELECT content_hash FROM deliveries WHERE channel='kb'") == expected
