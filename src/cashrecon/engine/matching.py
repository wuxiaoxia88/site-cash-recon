"""Classification and cross-source matching.

Re-derives ``flow_states``, automatic ``links`` and derived (virtual-account) flows
for all active flows from facts + rules + manual decisions. It is a pure
recomputation and safe to run any time.

States
  NORMAL      counted in totals under its category
  DUPLICATE   journal entry that duplicates an auto-collected record (excluded)
  TRANSFER    one side of a matched internal transfer / ZT top-up / withdrawal / fund sweep
  PENDING     transfer still inside its matching window (not yet overdue)
  REVIEW      needs a human decision (reason given); excluded from P&L when doubtful
  IGNORED     excluded by a manual decision or a non-effective portal status

Business period (业务期间) per flow, in priority order:
  manual decision > journal 业务发生时间 (when it differs from the entry date) >
  the duplicate journal entry's explicit period > payroll rule (previous month) > cash date.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from cashrecon.config import Settings
from cashrecon.db import Store, dumps, now_text
from cashrecon.engine import categories
from cashrecon.engine.rules import Classifier
from cashrecon.money import to_cents

XFER = {"XFER_INTERNAL", "XFER_ZT_TOPUP", "XFER_ZT_WITHDRAW"}
INEFFECTIVE_STATUS = ("草稿", "审核驳回", "已作废", "作废")
AUTO_SOURCES = {"BANK_SMS", "ICBC", "ALIPAY", "ZT_FLOW"}
WITHDRAW_TYPES = ("线下提现", "中天余额提现")
# Auto-collected categories that a more specific journal category may replace.
INHERITABLE = {"UNCLASSIFIED", "INC_OTHER", "COST_OTHER", "XFER_ZT_TOPUP"}
PERIOD_BASIS_CN = {"manual": "人工指定", "journal": "日记账业务发生时间", "journal_dup": "对应日记账业务发生时间",
                   "payroll": "月度工资/派费按上月计", "cash": "按收付日"}


@dataclass
class EFlow:
    flow_id: str
    source: str
    account: str
    day: date
    time: datetime
    direction: str
    amount: int
    src_category: str
    counterparty: str
    summary: str
    initiator: str
    status_text: str
    period_start: date | None = None
    period_end: date | None = None
    category: str = "UNCLASSIFIED"
    state: str = "NORMAL"
    reason: str = ""
    kind: str = ""  # review/pending kind, used to group action items
    link_id: str | None = None   # transfer / top-up / withdrawal pairing
    dup_link: str | None = None  # this auto record is the primary of a journal duplicate
    locked: bool = False  # decided manually
    p_start: date | None = None
    p_end: date | None = None
    period_basis: str = "cash"
    manual_period: tuple[date, date] | None = None
    dup_period: tuple[date, date] | None = None

    @property
    def signed(self) -> int:
        return self.amount if self.direction == "IN" else -self.amount


@dataclass
class MatchResult:
    flows: dict[str, EFlow]
    links: list[dict] = field(default_factory=list)
    derived: list[EFlow] = field(default_factory=list)


def _parse_time(text: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return datetime.strptime(text[:10], "%Y-%m-%d")


def _leaf(text: str) -> str:
    return text.rsplit("/", 1)[-1]


def _link_id(kind: str, a: str, b: str | None) -> str:
    return kind + ":" + hashlib.sha1(f"{a}|{b}".encode()).hexdigest()[:16]


def _day(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text[:10]) if text else None
    except ValueError:
        return None


def previous_month(day: date) -> tuple[date, date]:
    end = day.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def load_flows(store: Store) -> dict[str, EFlow]:
    flows = {}
    for r in store.query("SELECT flow_id, source, account_code, biz_date, biz_time, direction, amount_cents, "
                         "src_category, counterparty, summary, initiator, status_text, period_start, period_end "
                         "FROM flows WHERE removed = 0 AND source <> 'DERIVED'"):
        flows[r["flow_id"]] = EFlow(
            r["flow_id"], r["source"], r["account_code"], date.fromisoformat(r["biz_date"]),
            _parse_time(r["biz_time"]), r["direction"], r["amount_cents"], r["src_category"] or "",
            r["counterparty"] or "", r["summary"] or "", r["initiator"] or "", r["status_text"] or "",
            _day(r["period_start"]), _day(r["period_end"]))
    return flows


class Matcher:
    def __init__(self, store: Store, settings: Settings, today: date | None = None) -> None:
        self.store, self.settings = store, settings
        self.classifier = Classifier(store)
        self.rules = settings.rules
        self.today = today or date.today()
        self.auto_accounts = {a.code for a in settings.accounts if a.is_auto}
        self.zt_account = settings.zt_account.code if settings.zt_account else None

    def _is_owner(self, f: EFlow) -> bool:
        names = set(self.settings.owner_names)
        for sweep in self.settings.sweeps:
            names.update(sweep.get("owner_hints") or [])
        return any(n and n in f"{f.counterparty} {f.summary}" for n in names)

    def _name(self, code: str) -> str:
        return self.settings.account(code).name if self.settings.has_account(code) else code

    # ------------------------------------------------------------------ entry
    def run(self) -> MatchResult:
        flows = load_flows(self.store)
        result = MatchResult(flows)
        self._classify(flows)
        self._apply_manual(result)
        self._ineffective(flows)
        self._dedup(result)
        self._zt_topups(result)
        self._withdrawals(result)
        self._sweeps(result)
        self._internal_transfers(result)
        self._sweep_hints(result)
        self._review_leftovers(flows)
        self._periods(result)
        return result

    # ------------------------------------------------------------------ steps
    def _classify(self, flows: dict[str, EFlow]) -> None:
        for f in flows.values():
            f.category, _ = self.classifier.classify(f.source, f.direction, {
                "src_category": f.src_category, "counterparty": f.counterparty, "summary": f.summary})
            if f.account.startswith("UNMAPPED:"):
                f.state, f.reason, f.kind = "REVIEW", "日记账账户未在配置中登记", "unmapped_account"
            mapping = self.settings.withdraw_initiators.get(f.initiator) if f.initiator else None
            if mapping and f.src_category in WITHDRAW_TYPES:
                if mapping.get("account"):
                    f.category = "XFER_ZT_WITHDRAW"  # this initiator withdraws into one of our accounts
                elif mapping.get("category"):
                    f.category = mapping["category"]

    def _apply_manual(self, result: MatchResult) -> None:
        flows = result.flows
        for d in self.store.query("SELECT * FROM manual_decisions"):
            f = flows.get(d["flow_id"])
            if f is None:
                continue
            start, end = _day(d["period_start"]), _day(d["period_end"])
            if start and end:
                f.manual_period = (min(start, end), max(start, end))
            decision = d["decision"]
            if decision == "period":
                continue  # only the business period is fixed
            f.locked = True
            if d["category"]:
                f.category = d["category"]
            note = d["note"] or ""
            if decision == "ignore":
                f.state, f.reason = "IGNORED", note or "人工忽略"
            elif decision == "normal":
                f.state, f.reason = "NORMAL", note or "人工确认"
            elif decision == "category":
                f.locked = False  # category fixed, matching still allowed
            elif decision in ("duplicate", "transfer"):
                target = flows.get(d["target_flow_id"] or "")
                if target is None:
                    f.state, f.reason, f.kind = "REVIEW", "人工结论引用的流水不存在", "manual_broken"
                    continue
                kind = "DUPLICATE" if decision == "duplicate" else "TRANSFER"
                link = _link_id(kind, f.flow_id, target.flow_id)
                if decision == "duplicate":
                    f.state, f.reason, f.link_id = "DUPLICATE", note or "人工判定重复", link
                    target.dup_link = link
                else:
                    category = f.category if f.category in XFER else "XFER_INTERNAL"
                    for side in (f, target):
                        side.state, side.category, side.link_id, side.locked = "TRANSFER", category, link, True
                        side.reason = note or "人工确认划转"
                result.links.append({"link_id": link, "kind": kind, "flow_a": f.flow_id,
                                     "flow_b": target.flow_id, "biz_date": f.day.isoformat(),
                                     "amount_cents": f.amount, "fee_cents": 0, "rule": "manual", "actor": "manual"})

    def _ineffective(self, flows: dict[str, EFlow]) -> None:
        for f in flows.values():
            if f.locked or f.source != "JOURNAL":
                continue
            if any(word in f.status_text for word in INEFFECTIVE_STATUS):
                f.state, f.reason = "IGNORED", "日记账记录未生效（草稿/驳回/作废）"

    def _free(self, f: EFlow) -> bool:
        return not f.locked and f.state == "NORMAL" and f.link_id is None

    def _dedup(self, result: MatchResult) -> None:
        window_days = int(self.rules["dedup_days"])
        journal, auto = defaultdict(list), defaultdict(list)
        for f in result.flows.values():
            if f.account not in self.auto_accounts:
                continue
            key = (f.account, f.direction, f.amount)
            if f.source == "JOURNAL" and self._free(f):
                journal[key].append(f)
            elif f.source in AUTO_SOURCES and f.dup_link is None and f.state == "NORMAL":
                auto[key].append(f)
        for key, entries in journal.items():
            pool = sorted(auto.get(key, []), key=lambda x: x.time)
            for j in sorted(entries, key=lambda x: x.time):
                candidates = [a for a in pool if a.dup_link is None and abs((a.day - j.day).days) <= window_days]
                if not candidates:
                    continue
                best = min(candidates, key=lambda a: (abs((a.day - j.day).days), abs(a.time - j.time)))
                link = _link_id("DUPLICATE", j.flow_id, best.flow_id)
                j.state, j.link_id = "DUPLICATE", link
                j.reason = f"与{best.source}记录重复（{best.day.isoformat()}）"
                best.dup_link = link
                if j.category != "UNCLASSIFIED" and best.category in INHERITABLE and best.category != j.category:
                    best.category = j.category
                    best.reason = "科目取自日记账"
                explicit = self._explicit_journal_period(j)
                if explicit:
                    best.dup_period = explicit
                result.links.append({"link_id": link, "kind": "DUPLICATE", "flow_a": j.flow_id,
                                     "flow_b": best.flow_id, "biz_date": j.day.isoformat(), "amount_cents": j.amount,
                                     "fee_cents": 0, "rule": "same_account_direction_amount", "actor": "auto"})

    @staticmethod
    def _explicit_journal_period(f: EFlow) -> tuple[date, date] | None:
        if f.source != "JOURNAL" or not f.period_start or not f.period_end:
            return None
        if f.period_start == f.day and f.period_end == f.day:
            return None  # same as the entry date: the field was not really filled in
        return f.period_start, f.period_end

    def _pair(self, result: MatchResult, outs: list[EFlow], ins: list[EFlow], *, kind: str, category: str,
              days: int, tolerance: bool, rule: str) -> None:
        floor = to_cents(self.rules["transfer_fee_floor_yuan"])
        ratio = float(self.rules["transfer_fee_ratio"])
        for o in sorted(outs, key=lambda x: x.time):
            best, best_key = None, None
            for i in ins:
                if i.link_id is not None or i.account == o.account:
                    continue
                gap = (i.day - o.day).days
                if gap < 0 or gap > days:
                    continue
                diff = o.amount - i.amount
                allowed = max(floor, int(o.amount * ratio)) if tolerance else 0
                if diff < 0 or diff > allowed:
                    continue
                key = (diff, gap, abs(i.time - o.time))
                if best_key is None or key < best_key:
                    best, best_key = i, key
            if best is None:
                continue
            link = _link_id(kind, o.flow_id, best.flow_id)
            for side in (o, best):
                side.state, side.category, side.link_id = "TRANSFER", category, link
            fee = o.amount - best.amount
            o.reason = f"转入 {self._name(best.account)}"
            best.reason = f"来自 {self._name(o.account)}"
            if fee:
                o.reason += f"（手续费 {fee / 100:.2f}）"
            result.links.append({"link_id": link, "kind": kind, "flow_a": o.flow_id, "flow_b": best.flow_id,
                                 "biz_date": o.day.isoformat(), "amount_cents": best.amount, "fee_cents": fee,
                                 "rule": rule, "actor": "auto"})

    def _zt_topups(self, result: MatchResult) -> None:
        if not self.zt_account:
            return
        outs = [f for f in result.flows.values() if self._free(f) and f.direction == "OUT"
                and f.category == "XFER_ZT_TOPUP" and f.account != self.zt_account]
        ins = [f for f in result.flows.values() if self._free(f) and f.direction == "IN"
               and f.account == self.zt_account and f.category == "XFER_ZT_TOPUP"]
        self._pair(result, outs, ins, kind="ZT_TOPUP", category="XFER_ZT_TOPUP", days=1, tolerance=False,
                   rule="topup_same_amount")

    def _withdrawals(self, result: MatchResult) -> None:
        """Only initiators configured with a destination account are treated as internal transfers."""
        if not self.zt_account:
            return
        days = int(self.rules["withdraw_days"])
        outs = [f for f in result.flows.values() if self._free(f) and f.account == self.zt_account
                and f.direction == "OUT" and f.category == "XFER_ZT_WITHDRAW"]
        by_target: dict[str, list[EFlow]] = defaultdict(list)
        for o in outs:
            target = (self.settings.withdraw_initiators.get(o.initiator) or {}).get("account")
            if target:
                by_target[target].append(o)
        for target, group in by_target.items():
            ins = [i for i in result.flows.values() if self._free(i) and i.direction == "IN"
                   and i.account == target]
            self._pair(result, group, ins, kind="ZT_WITHDRAW", category="XFER_ZT_WITHDRAW", days=days,
                       tolerance=False, rule="withdraw_same_amount")

    # ---------------------------------------------------------- fund sweeps (余额宝)
    def _derive(self, result: MatchResult, origin: EFlow, fund: str, reason_origin: str, reason_fund: str) -> None:
        direction = "IN" if origin.direction == "OUT" else "OUT"
        derived = EFlow(f"DERIVED:{fund}:{origin.flow_id}", "DERIVED", fund, origin.day, origin.time, direction,
                        origin.amount, "内部划转", self._name(origin.account), origin.summary, "", "")
        link = _link_id("SWEEP", origin.flow_id, derived.flow_id)
        for side in (origin, derived):
            side.state, side.category, side.link_id = "TRANSFER", "XFER_INTERNAL", link
        origin.reason, derived.reason = reason_origin, reason_fund
        result.derived.append(derived)
        result.links.append({"link_id": link, "kind": "SWEEP", "flow_a": origin.flow_id, "flow_b": derived.flow_id,
                             "biz_date": origin.day.isoformat(), "amount_cents": origin.amount, "fee_cents": 0,
                             "rule": "fund_sweep", "actor": "auto"})

    def _fund_move(self, result: MatchResult, f: EFlow, fund: str | None, reason: str, reason_fund: str) -> None:
        """Movement between an account and its fund (e.g. 余额宝). With a fund account configured a mirror
        flow is derived on it; without one the fund is not tracked and the move is simply non-operating."""
        if fund:
            self._derive(result, f, fund, reason, reason_fund)
            return
        f.category, f.reason = "XFER_FUND", reason
        f.link_id = _link_id("FUND", f.flow_id, None)

    def _sweeps(self, result: MatchResult) -> None:
        for sweep in self.settings.sweeps:
            account, fund = sweep["account"], sweep.get("fund") or None
            to_fund = sweep.get("to_fund_summary", "余额自动转入")
            from_fund = sweep.get("from_fund_summary", "转出到余额")
            # Transfers to the owner's own fund identity (e.g. masked "****y") count as fund moves even
            # without the automatic-sweep note, e.g. a customer payment that is swept right away.
            fund_parties = set(sweep.get("fund_counterparties") or [])
            fund_categories = set(sweep.get("to_fund_categories") or ["账户间互转"])
            fund_name = self._name(fund) if fund else sweep.get("fund_name", "余额宝")
            for f in list(result.flows.values()):
                if not self._free(f) or f.account != account:
                    continue
                to_party = f.counterparty in fund_parties and f.src_category in fund_categories
                if f.direction == "OUT" and (f.summary == to_fund or to_party):
                    self._fund_move(result, f, fund, f"转入{fund_name}", f"来自{self._name(account)}")
                elif f.direction == "IN" and f.summary == from_fund:
                    self._fund_move(result, f, fund, f"来自{fund_name}", f"转回{self._name(account)}")

    def _sweep_hints(self, result: MatchResult) -> None:
        """Owner transfers registered on other accounts (e.g. the monthly hand-over of last month's
        Alipay income to the cashier) are internal money, not revenue."""
        for sweep in self.settings.sweeps:
            hints = [h for h in (sweep.get("owner_hints") or self.settings.owner_names) if h]
            accounts = set(sweep.get("hint_accounts") or [])
            minimum = to_cents(sweep.get("hint_min_yuan", 1000))
            fund = sweep.get("fund") or None
            fund_name = self._name(fund) if fund else sweep.get("fund_name", "余额宝")
            if not hints:
                continue
            for f in list(result.flows.values()):
                if not self._free(f) or f.direction != "IN" or f.amount < minimum:
                    continue
                if accounts and f.account not in accounts:
                    continue
                if f.account in (sweep["account"], fund):
                    continue
                if any(h in f"{f.summary} {f.counterparty}" for h in hints):
                    self._fund_move(result, f, fund, f"来自店主{fund_name}（上月收入汇总转入，非经营收入）",
                                    f"转给{self._name(f.account)}")

    def _internal_transfers(self, result: MatchResult) -> None:
        days = int(self.rules["transfer_days"])
        outs = [f for f in result.flows.values() if self._free(f) and f.direction == "OUT"
                and f.category == "XFER_INTERNAL"]
        ins = [f for f in result.flows.values() if self._free(f) and f.direction == "IN"
               and f.category in ("XFER_INTERNAL", "UNCLASSIFIED")]
        self._pair(result, outs, ins, kind="TRANSFER", category="XFER_INTERNAL", days=days, tolerance=True,
                   rule="internal_transfer")

    def _review_leftovers(self, flows: dict[str, EFlow]) -> None:
        overdue = int(self.settings.alerts["withdraw_overdue_days"])
        for f in flows.values():
            if f.locked or f.state != "NORMAL" or f.link_id is not None:
                continue
            if f.source == "JOURNAL" and f.account in self.auto_accounts:
                f.state = "REVIEW"
                f.kind = "adjust" if f.category == "ADJUST" else "journal_unmatched"
                f.reason = ("日记账余额修改，需确认原因" if f.category == "ADJUST" else
                            "自动采集账户上的日记账登记，没有找到对应的银行/支付宝流水（可能登记错误或采集遗漏）")
            elif f.category == "ADJUST":
                f.state, f.reason, f.kind = "REVIEW", "余额修改/测试记录，需确认", "adjust"
            elif f.category == "UNCLASSIFIED" and f.source == "JOURNAL" and (
                    (f.direction == "OUT" and _leaf(f.src_category).startswith("收")) or
                    (f.direction == "IN" and _leaf(f.src_category).startswith("付"))):
                f.state, f.kind = "REVIEW", "contradiction"
                f.reason = f"登记科目“{_leaf(f.src_category)}”与收付方向（{'付款' if f.direction == 'OUT' else '收款'}）矛盾，请核实"
            elif f.category == "XFER_ZT_WITHDRAW" and f.account == self.zt_account:
                target = (self.settings.withdraw_initiators.get(f.initiator) or {}).get("account", "")
                f.kind = "withdraw_unknown"
                if (self.today - f.day).days <= overdue:
                    f.state, f.reason = "PENDING", f"提现到{self._name(target)}，等待到账"
                else:
                    f.state, f.reason = "REVIEW", f"提现超过 {overdue} 天未在{self._name(target)}找到入账"
            elif f.category == "XFER_ZT_TOPUP":
                f.kind = "topup_unmatched"
                if (self.today - f.day).days <= 1:
                    f.state, f.reason = "PENDING", "中天充值，等待另一端记录"
                elif f.account != self.zt_account:
                    # Paying ZTO head office without a matching ZT top-up = buying waybill numbers / materials.
                    f.category, f.kind = "COST_WAYBILL", ""
                    f.reason = "付中通总部且中天账户无对应充值，按购买单号/物料计"
            elif f.category == "XFER_INTERNAL" and self._is_owner(f):
                f.category = "OWNER_DRAW"
                f.reason = "店主个人收支，与网点经营无关"
            elif f.category == "XFER_INTERNAL":
                f.state, f.kind = "REVIEW", "transfer_out_unknown" if f.direction == "OUT" else "transfer_in_unknown"
                f.reason = ("转出到本网点其他账户，但未找到对应转入（对方账户可能未纳入系统）" if f.direction == "OUT"
                            else "转入但未找到来源账户的转出记录")

    # ---------------------------------------------------------- business periods
    def _periods(self, result: MatchResult) -> None:
        acc = self.settings.accrual
        prev_cats = set(acc.get("prev_month_categories") or [])
        prev_min = to_cents(acc.get("prev_month_min_yuan", 5000))
        lo, hi = (acc.get("prev_month_days") or [15, 28])[:2]
        for f in list(result.flows.values()) + result.derived:
            explicit = self._explicit_journal_period(f)
            if f.manual_period:
                (f.p_start, f.p_end), f.period_basis = f.manual_period, "manual"
            elif explicit:
                (f.p_start, f.p_end), f.period_basis = explicit, "journal"
            elif f.dup_period:
                (f.p_start, f.p_end), f.period_basis = f.dup_period, "journal_dup"
            elif (f.category in prev_cats and f.source not in ("ZT_FLOW", "ZT_SUMMARY", "DERIVED")
                  and f.direction == "OUT" and f.amount >= prev_min and lo <= f.day.day <= hi):
                (f.p_start, f.p_end), f.period_basis = previous_month(f.day), "payroll"
            else:
                f.p_start, f.p_end, f.period_basis = f.day, f.day, "cash"


def persist(store: Store, result: MatchResult) -> None:
    now = now_text()
    with store.tx():
        store.execute("DELETE FROM flow_states")
        store.execute("DELETE FROM links")
        store.execute("DELETE FROM flows WHERE source = 'DERIVED'")
        for d in result.derived:
            store.execute(
                "INSERT INTO flows (flow_id, source, source_ref, account_code, biz_date, biz_time, direction, "
                "amount_cents, counterparty, src_category, summary, raw_json, raw_hash, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.flow_id, "DERIVED", d.flow_id.split(":", 2)[2], d.account, d.day.isoformat(),
                 d.time.strftime("%Y-%m-%d %H:%M:%S"), d.direction, d.amount, d.counterparty, d.src_category,
                 d.summary, dumps({}), "derived", now, now))
        for link in result.links:
            store.execute("INSERT OR REPLACE INTO links (link_id, kind, flow_a, flow_b, biz_date, amount_cents, "
                          "fee_cents, rule, actor) VALUES (:link_id,:kind,:flow_a,:flow_b,:biz_date,:amount_cents,"
                          ":fee_cents,:rule,:actor)", link)
        store.conn.executemany(
            "INSERT INTO flow_states (flow_id, biz_date, state, category, reason, link_id, kind, p_start, p_end, "
            "period_basis) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(f.flow_id, f.day.isoformat(), f.state, f.category, f.reason, f.link_id or f.dup_link, f.kind,
              f.p_start.isoformat() if f.p_start else None, f.p_end.isoformat() if f.p_end else None, f.period_basis)
             for f in list(result.flows.values()) + result.derived])


__all__ = ["EFlow", "Matcher", "MatchResult", "PERIOD_BASIS_CN", "categories", "persist", "previous_month"]
