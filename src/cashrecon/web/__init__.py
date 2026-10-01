"""Local web console (Flask). Listens on 127.0.0.1 by default.

Pages: 总览 / 报表 / 待处理 / 当日流水 / 余额录入 / 分类规则 / 运行记录.
Every change (manual decision, balance entry, rule) triggers a recomputation of the
affected days so reports and dashboards stay consistent.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from datetime import date, datetime, timedelta
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from cashrecon import dates
from cashrecon.config import Settings, load_settings
from cashrecon.db import Store, now_text
from cashrecon.engine import categories
from cashrecon.engine.accounts import sync_accounts
from cashrecon.engine.rules import RuleError, add_user_rule, ensure_default_rules
from cashrecon.logging_setup import get_logger
from cashrecon.money import MoneyError, fmt_wan, fmt_yuan, to_cents
from cashrecon.paths import Paths

log = get_logger("web")
DECISIONS = {"ignore": "忽略（不计入）", "normal": "确认为正常收支", "duplicate": "与另一笔重复",
             "transfer": "与另一笔为内部划转", "category": "只修改科目"}
_job_lock = threading.Lock()
_job_state: dict[str, Any] = {"running": False, "last": None}


def recompute(store: Store, settings: Settings, days: list[date], render: bool = True) -> None:
    from cashrecon.engine import reconcile
    from cashrecon.reports import render_report
    days = sorted(set(days))
    if not days:
        return
    reconcile(store, settings, days)
    if render:
        for day in days:
            try:
                render_report(store, settings, "daily", day)
            except Exception:  # report rendering must not break the console action
                log.warning("re-render %s failed", day, exc_info=True)


def _secret_key(paths: Paths) -> bytes:
    path = paths.data_dir / ".web-secret"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        if hasattr(path, "chmod"):
            try:
                path.chmod(0o600)
            except OSError:
                pass
    return path.read_bytes()


def create_app(paths: Paths | None = None) -> Flask:
    paths = (paths or Paths.resolve()).ensure()
    app = Flask(__name__)
    app.secret_key = _secret_key(paths)
    app.config["PATHS"] = paths
    app.jinja_env.filters.update(
        yuan=lambda c, sign=False: "—" if c is None else fmt_yuan(int(c), sign=sign),
        wan=lambda c: "—" if c is None else fmt_wan(int(c)),
        catname=categories.name)

    def settings() -> Settings:
        if "settings" not in g:
            g.settings = load_settings(paths)
        return g.settings

    def store() -> Store:
        if "store" not in g:
            g.store = Store(paths.database)
            ensure_default_rules(g.store)
            sync_accounts(g.store, settings())
        return g.store

    @app.teardown_appcontext
    def _close(_: object) -> None:
        db = g.pop("store", None)
        if db is not None:
            db.close()

    @app.before_request
    def _guard() -> Any:
        cfg = settings().web
        password = settings().secret("CASHRECON_WEB_PASSWORD")
        if cfg.get("host", "127.0.0.1") not in ("127.0.0.1", "localhost") or password:
            auth = request.authorization
            if not password or not auth or not hmac.compare_digest(auth.password or "", password):
                return ("需要登录", 401, {"WWW-Authenticate": 'Basic realm="cashrecon"'})
        if request.method == "POST":
            submitted = request.form.get("csrf", "")
            if not submitted or not hmac.compare_digest(submitted, session.get("csrf", "")):
                abort(400, "表单已过期，请刷新页面后重试")
        return None

    @app.context_processor
    def _inject() -> dict[str, Any]:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return {"csrf": session["csrf"], "site": settings().site_name, "job": _job_state,
                "categories": categories.CATEGORIES, "DECISIONS": DECISIONS}

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def dashboard() -> str:
        from cashrecon.analysis import action_items, headline, load_history
        from cashrecon.reports import charts
        db = store()
        latest = db.one("SELECT biz_date FROM daily_results ORDER BY biz_date DESC LIMIT 1")
        if latest is None:
            return render_template("empty.html")
        day = date.fromisoformat(latest["biz_date"])
        history = load_history(db, day, days=30)
        payload = json.loads(db.scalar("SELECT payload FROM daily_results WHERE biz_date = ?", (day.isoformat(),)))
        actions = action_items(payload, settings(), history)
        items = [i.to_dict() for i in actions]
        series = history + [payload]
        labels = [p["day"][5:] for p in series]
        return render_template("dashboard.html", p=payload, items=items, headline=headline(payload, actions),
            chart_profit=charts.bar_chart(labels, [p["profit"]["profit"] for p in series]),
            chart_position=charts.line_chart(labels, [p["position"]["total"] for p in series]),
            month_profit=sum(p["profit"]["profit"] for p in series if p["day"][:7] == payload["day"][:7]))

    @app.get("/reports")
    def reports() -> str:
        root = paths.reports
        listing: dict[str, list[dict[str, str]]] = {}
        for cadence in ("daily", "weekly", "monthly"):
            folder = root / cadence
            entries = []
            for sub in sorted(folder.iterdir(), reverse=True) if folder.is_dir() else []:
                html = next((p for p in sub.glob("*.html") if p.name != "mail-preview.html"), None)
                if html:
                    entries.append({"key": sub.name, "file": html.name})
            listing[cadence] = entries[:120]
        return render_template("reports.html", listing=listing)

    @app.get("/reports/<cadence>/<key>/<path:name>")
    def report_file(cadence: str, key: str, name: str) -> Any:
        base = paths.reports.resolve()
        target = (base / cadence / key / name).resolve()
        if base not in target.parents or not target.is_file():
            abort(404)
        return send_file(target)

    @app.get("/review")
    def review() -> str:
        db = store()
        days = int(request.args.get("days", 31))
        kind = request.args.get("kind", "")
        start = (dates.today() - timedelta(days=days)).isoformat()
        sql = ("SELECT f.*, s.state, s.category, s.reason, s.kind, a.name AS account_name, md.decision, md.note AS md_note "
               "FROM flows f JOIN flow_states s USING (flow_id) LEFT JOIN accounts a USING (account_code) "
               "LEFT JOIN manual_decisions md USING (flow_id) WHERE s.state IN ('REVIEW','PENDING') AND f.biz_date >= ?")
        params: list[Any] = [start]
        if kind:
            sql += " AND s.kind = ?"
            params.append(kind)
        rows = [dict(r) for r in db.query(sql + " ORDER BY s.kind, f.biz_time DESC", tuple(params))]
        kinds = db.query("SELECT s.kind, COUNT(*) n, SUM(f.amount_cents) a FROM flows f JOIN flow_states s USING (flow_id) "
                         "WHERE s.state IN ('REVIEW','PENDING') AND f.biz_date >= ? GROUP BY s.kind", (start,))
        decided = db.query("SELECT md.*, f.biz_date, f.amount_cents, f.direction, a.name AS account_name, "
                           "f.src_category FROM manual_decisions md JOIN flows f USING (flow_id) "
                           "LEFT JOIN accounts a USING (account_code) ORDER BY md.created_at DESC LIMIT 50")
        return render_template("review.html", rows=rows, kinds=kinds, kind=kind, days=days, decided=decided)

    @app.post("/review/decide")
    def decide() -> Any:
        db = store()
        ids = request.form.getlist("flow_id")
        decision = request.form.get("decision", "")
        category = request.form.get("category") or None
        target = (request.form.get("target_flow_id") or "").strip() or None
        note = (request.form.get("note") or "").strip()[:200]
        if not ids or decision not in DECISIONS:
            flash("请选择记录和处理方式")
            return redirect(request.referrer or url_for("review"))
        if decision in ("duplicate", "transfer") and (not target or len(ids) != 1):
            flash("“重复/划转”需要且只能选择一笔，并填写对应流水编号")
            return redirect(request.referrer or url_for("review"))
        if decision == "category" and not category:
            flash("请选择科目")
            return redirect(request.referrer or url_for("review"))
        if target and db.one("SELECT 1 FROM flows WHERE flow_id = ?", (target,)) is None:
            flash(f"找不到流水 {target}")
            return redirect(request.referrer or url_for("review"))
        days = set()
        with db.tx():
            for fid in ids:
                row = db.one("SELECT biz_date FROM flows WHERE flow_id = ?", (fid,))
                if row is None:
                    continue
                days.add(date.fromisoformat(row["biz_date"]))
                db.execute("INSERT INTO manual_decisions (flow_id, decision, target_flow_id, category, note, actor, "
                           "created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(flow_id) DO UPDATE SET "
                           "decision=excluded.decision, target_flow_id=excluded.target_flow_id, "
                           "category=excluded.category, note=excluded.note, actor=excluded.actor, "
                           "created_at=excluded.created_at",
                           (fid, decision, target, category, note, "console", now_text()))
        recompute(db, settings(), _with_neighbours(days))
        flash(f"已处理 {len(ids)} 笔并重新计算")
        return redirect(request.referrer or url_for("review"))

    @app.post("/review/undo")
    def undo() -> Any:
        db = store()
        fid = request.form.get("flow_id", "")
        row = db.one("SELECT biz_date FROM flows WHERE flow_id = ?", (fid,))
        db.execute("DELETE FROM manual_decisions WHERE flow_id = ?", (fid,))
        if row:
            recompute(db, settings(), _with_neighbours({date.fromisoformat(row["biz_date"])}))
        flash("已撤销人工结论")
        return redirect(request.referrer or url_for("review"))

    @app.post("/rules/from-flow")
    def rule_from_flow() -> Any:
        db = store()
        field, _, fid = (request.form.get("rule") or "").partition("|")
        category = request.form.get("category", "")
        if not category:
            flash("请先在上方选择科目，再点“建规则”")
            return redirect(request.referrer or url_for("review"))
        flow = db.one("SELECT * FROM flows WHERE flow_id = ?", (fid,))
        if flow is None or field not in ("src_category", "counterparty"):
            abort(400)
        try:
            add_user_rule(db, {"source": "*", "field": field, "op": "equals", "pattern": flow[field],
                               "direction": flow["direction"], "category": category, "priority": 5,
                               "note": f"由控制台根据 {fid} 创建"})
        except RuleError as exc:
            flash(f"规则无效：{exc}")
            return redirect(request.referrer or url_for("review"))
        recompute(db, settings(), _recent_days(db, 62))
        flash(f"已新增规则：{field} = “{flow[field]}” → {categories.name(category)}，并重算近两个月")
        return redirect(request.referrer or url_for("review"))

    @app.get("/flows")
    def flows() -> str:
        db = store()
        day = request.args.get("date") or db.scalar("SELECT MAX(biz_date) FROM daily_results") or dates.today().isoformat()
        account = request.args.get("account", "")
        sql = ("SELECT f.*, s.state, s.category, s.reason, a.name AS account_name FROM flows f "
               "LEFT JOIN flow_states s USING (flow_id) LEFT JOIN accounts a USING (account_code) "
               "WHERE f.biz_date = ? AND f.removed = 0")
        params: list[Any] = [day]
        if account:
            sql += " AND f.account_code = ?"
            params.append(account)
        rows = db.query(sql + " ORDER BY f.account_code, f.biz_time", tuple(params))
        accounts = db.query("SELECT account_code, name FROM accounts WHERE active = 1")
        return render_template("flows.html", rows=rows, day=day, account=account, accounts=accounts)

    @app.route("/balances", methods=["GET", "POST"])
    def balances() -> Any:
        db = store()
        if request.method == "POST":
            code = request.form.get("account", "")
            as_of = (request.form.get("as_of") or "").replace("T", " ")
            try:
                when = datetime.strptime(as_of[:16], "%Y-%m-%d %H:%M")
                cents = to_cents(request.form.get("amount"))
            except (ValueError, MoneyError):
                flash("请填写正确的时间和金额")
                return redirect(url_for("balances"))
            if not settings().has_account(code):
                abort(400)
            db.execute("INSERT INTO manual_balances (account_code, as_of, balance_cents, note, actor, created_at) "
                       "VALUES (?,?,?,?,?,?) ON CONFLICT(account_code, as_of) DO UPDATE SET "
                       "balance_cents=excluded.balance_cents, note=excluded.note, created_at=excluded.created_at",
                       (code, when.strftime("%Y-%m-%d %H:%M"), cents, (request.form.get("note") or "")[:200],
                        "console", now_text()))
            if db.one("SELECT 1 FROM daily_results WHERE biz_date = ?", (when.date().isoformat(),)):
                recompute(db, settings(), [when.date()])
            flash("已保存实际余额并完成比对")
            return redirect(url_for("balances"))
        manual_first = sorted(settings().accounts, key=lambda a: (a.is_auto, a.code))
        entries = []
        for row in db.query("SELECT m.*, a.name FROM manual_balances m LEFT JOIN accounts a USING (account_code) "
                            "ORDER BY as_of DESC LIMIT 60"):
            book = db.one("SELECT payload FROM daily_results WHERE biz_date = ?", (row["as_of"][:10],))
            closing = None
            if book:
                acc = next((a for a in json.loads(book["payload"])["accounts"] if a["code"] == row["account_code"]), None)
                closing = acc["closing"] if acc else None
            entries.append({**dict(row), "book": closing,
                            "diff": None if closing is None else row["balance_cents"] - closing})
        return render_template("balances.html", accounts=manual_first, entries=entries,
                               now=dates.now().strftime("%Y-%m-%dT%H:%M"))

    @app.route("/rules", methods=["GET", "POST"])
    def rules() -> Any:
        db = store()
        if request.method == "POST":
            try:
                add_user_rule(db, {k: request.form.get(k, "") for k in
                                   ("source", "field", "op", "pattern", "direction", "category", "note")} |
                              {"priority": int(request.form.get("priority") or 5)})
            except (RuleError, ValueError) as exc:
                flash(f"规则无效：{exc}")
                return redirect(url_for("rules"))
            recompute(db, settings(), _recent_days(db, 62))
            flash("已新增规则并重算近两个月")
            return redirect(url_for("rules"))
        rows = db.query("SELECT * FROM category_rules ORDER BY origin DESC, priority, id")
        return render_template("rules.html", rows=rows)

    @app.post("/rules/<int:rule_id>/toggle")
    def toggle_rule(rule_id: int) -> Any:
        db = store()
        db.execute("UPDATE category_rules SET enabled = 1 - enabled, updated_at = ? WHERE id = ?", (now_text(), rule_id))
        recompute(db, settings(), _recent_days(db, 62))
        flash("已切换规则状态并重算")
        return redirect(url_for("rules"))

    @app.get("/runs")
    def runs() -> str:
        db = store()
        return render_template(
            "runs.html",
            runs=db.query("SELECT * FROM runs ORDER BY started_at DESC LIMIT 40"),
            deliveries=db.query("SELECT * FROM deliveries ORDER BY updated_at DESC LIMIT 40"),
            fetches=db.query("SELECT * FROM fetches WHERE id IN (SELECT MAX(id) FROM fetches GROUP BY source, biz_date) "
                             "ORDER BY biz_date DESC, source LIMIT 60"))

    @app.post("/actions/run")
    def run_action() -> Any:
        action = request.form.get("action", "")
        day_text = request.form.get("date", "")
        if _job_state["running"]:
            flash("已有任务在运行，请稍后")
            return redirect(url_for("runs"))
        try:
            day = dates.parse_day(day_text) if day_text else dates.yesterday()
        except ValueError:
            flash("日期格式不正确")
            return redirect(url_for("runs"))
        threading.Thread(target=_background, args=(paths, action, day), daemon=True).start()
        flash("任务已在后台开始，完成后刷新本页查看结果")
        return redirect(url_for("runs"))

    @app.get("/api/status")
    def api_status() -> Any:
        db = store()
        last = db.one("SELECT run_id, job, status, finished_at FROM runs ORDER BY started_at DESC LIMIT 1")
        latest = db.one("SELECT biz_date, data_status FROM daily_results ORDER BY biz_date DESC LIMIT 1")
        return jsonify({"last_run": dict(last) if last else None, "latest_day": dict(latest) if latest else None,
                        "job": _job_state})

    return app


def _with_neighbours(days: set[date]) -> list[date]:
    result = set()
    for day in days:
        for offset in range(-3, 4):
            candidate = day + timedelta(days=offset)
            if candidate < dates.today():
                result.add(candidate)
    return sorted(result)


def _recent_days(store: Store, n: int) -> list[date]:
    rows = store.query("SELECT biz_date FROM daily_results ORDER BY biz_date DESC LIMIT ?", (n,))
    return [date.fromisoformat(r["biz_date"]) for r in rows]


def _background(paths: Paths, action: str, day: date) -> None:
    with _job_lock:
        _job_state.update(running=True, last={"action": action, "date": day.isoformat(), "started": now_text()})
        try:
            settings = load_settings(paths)
            with Store(paths.database) as db:
                if action == "refetch":
                    from cashrecon.ingest import fetch_days
                    fetch_days(settings, db, [day])
                    recompute(db, settings, [day])
                elif action == "recompute":
                    recompute(db, settings, [day])
                elif action in ("daily", "retry"):
                    from cashrecon.pipeline import Runner, job_lock
                    with job_lock(settings):
                        Runner(settings, db).run(action, day + timedelta(days=1))
                else:
                    raise ValueError(action)
            _job_state["last"]["result"] = "完成"
        except Exception as exc:
            log.exception("background action failed")
            _job_state["last"]["result"] = f"失败：{exc.__class__.__name__}"
        finally:
            _job_state["running"] = False
            _job_state["last"]["finished"] = now_text()


def serve(paths: Paths | None = None, host: str | None = None, port: int | None = None, open_browser: bool = False) -> None:
    from waitress import serve as waitress_serve
    paths = paths or Paths.resolve()
    cfg = load_settings(paths).web
    host = host or cfg.get("host", "127.0.0.1")
    port = int(port or cfg.get("port", 8765))
    app = create_app(paths)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"控制台：{url}（Ctrl+C 退出）")
    if open_browser:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    waitress_serve(app, host=host, port=port, threads=4)
