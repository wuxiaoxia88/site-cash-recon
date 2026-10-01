"""Classification and cross-source matching.

Re-derives ``flow_states`` and automatic ``links`` for all active flows from
facts + rules + manual decisions (a pure recomputation; safe to run any time).

States
  NORMAL      counted in totals under its category
  DUPLICATE   journal entry that duplicates an auto-collected record (excluded)
  TRANSFER    one side of a matched internal transfer / ZT top-up / withdrawal
  PENDING     transfer/withdrawal still inside its matching window (not yet overdue)
  REVIEW      needs a human decision (reason given); excluded from P&L when doubtful
  IGNORED     excluded by a manual decision or a non-effective portal status
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from cashrecon.config import Settings
from cashrecon.db import Store
from cashrecon.engine import categories
from cashrecon.engine.rules import Classifier
from cashrecon.money import to_cents

XFER = {"XFER_INTERNAL", "XFER_ZT_TOPUP", "XFER_ZT_WITHDRAW"}
INEFFECTIVE_STATUS = ("草稿", "审核驳回", "已作废", "作废")
AUTO_SOURCES = {"BANK_SMS", "ICBC", "ALIPAY", "ZT_FLOW"}


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
    category: str = "UNCLASSIFIED"
    state: str = "NORMAL"
    reason: str = ""
    kind: str = ""  # review/pending kind, used to group action items
    link_id: str | None = None   # transfer / top-up / withdrawal pairing
    dup_link: str | None = None  # this auto record is the primary of a journal duplicate
    locked: bool = False  # decided manually


@dataclass
class MatchResult:
    flows: dict[str, EFlow]
    links: list[dict] = field(default_factory=list)


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


def load_flows(store: Store) -> dict[str, EFlow]:
    flows = {}
    for r in store.query("SELECT flow_id, source, account_code, biz_date, biz_time, direction, amount_cents, "
                         "src_category, counterparty, summary, initiator, status_text FROM flows WHERE removed = 0"):
        flows[r["flow_id"]] = EFlow(
            r["flow_id"], r["source"], r["account_code"], date.fromisoformat(r["biz_date"]),
            _parse_time(r["biz_time"]), r["direction"], r["amount_cents"], r["src_category"] or "",
            r["counterparty"] or "", r["summary"] or "", r["initiator"] or "", r["status_text"] or "")
    return flows


class Matcher:
    def __init__(self, store: Store, settings: Settings, today: date | None = None) -> None:
        self.store, self.settings = store, settings
        self.classifier = Classifier(store)
        self.rules = settings.rules
        self.today = today or date.today()
        self.auto_accounts = {a.code for a in settings.accounts if a.is_auto}
        self.zt_account = settings.zt_account.code if settings.zt_account else None

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
        self._internal_transfers(result)
        self._review_leftovers(flows)
        return result

    # ------------------------------------------------------------------ steps
    def _classify(self, flows: dict[str, EFlow]) -> None:
        for f in flows.values():
            f.category, _ = self.classifier.classify(f.source, f.direction, {
                "src_category": f.src_category, "counterparty": f.counterparty, "summary": f.summary})
            if f.account.startswith("UNMAPPED:"):
                f.state, f.reason, f.kind = "REVIEW", "日记账账户未在配置中登记", "unmapped_account"
            mapping = self.settings.withdraw_initiators.get(f.initiator) if f.initiator else None
            if mapping and mapping.get("category") and f.category == "XFER_ZT_WITHDRAW":
                f.category = mapping["category"]
                f.reason = f"提现发起人 {f.initiator} 已配置为“{categories.name(f.category)}”"

    def _apply_manual(self, result: MatchResult) -> None:
        flows = result.flows
        for d in self.store.query("SELECT * FROM manual_decisions"):
            f = flows.get(d["flow_id"])
            if f is None:
                continue
            f.locked = True
            if d["category"]:
                f.category = d["category"]
            decision = d["decision"]
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
                if best.category == "UNCLASSIFIED" or (j.category != "UNCLASSIFIED" and best.category in
                                                         ("INC_OTHER", "COST_OTHER")):
                    best.category = j.category
                    best.reason = "科目取自日记账"
                result.links.append({"link_id": link, "kind": "DUPLICATE", "flow_a": j.flow_id,
                                     "flow_b": best.flow_id, "biz_date": j.day.isoformat(), "amount_cents": j.amount,
                                     "fee_cents": 0, "rule": "same_account_direction_amount", "actor": "auto"})

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
            o.reason = f"转入 {self.settings.account(best.account).name if self.settings.has_account(best.account) else best.account}"
            best.reason = f"来自 {self.settings.account(o.account).name if self.settings.has_account(o.account) else o.account}"
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
        if not self.zt_account:
            return
        days = int(self.rules["withdraw_days"])
        outs = [f for f in result.flows.values() if self._free(f) and f.account == self.zt_account
                and f.direction == "OUT" and f.category == "XFER_ZT_WITHDRAW"]
        candidates = [f for f in result.flows.values() if self._free(f) and f.direction == "IN"
                      and f.account != self.zt_account and f.category in XFER | {"UNCLASSIFIED"}]
        by_target: dict[str | None, list[EFlow]] = defaultdict(list)
        for o in outs:
            mapping = self.settings.withdraw_initiators.get(o.initiator, {}) if o.initiator else {}
            by_target[mapping.get("account")].append(o)
        for target, group in by_target.items():
            ins = [i for i in candidates if target is None or i.account == target]
            self._pair(result, group, ins, kind="ZT_WITHDRAW", category="XFER_ZT_WITHDRAW", days=days,
                       tolerance=False, rule="withdraw_same_amount")

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
                age = (self.today - f.day).days
                who = f"（发起人 {f.initiator}）" if f.initiator else ""
                f.kind = "withdraw_unknown"
                if age <= overdue:
                    f.state, f.reason = "PENDING", f"中天提现{who}，等待到账"
                else:
                    f.state, f.reason = "REVIEW", f"中天提现{who}超过 {overdue} 天未在本网点账户找到入账，去向待确认"
            elif f.category == "XFER_ZT_TOPUP":
                f.kind = "topup_unmatched"
                if (self.today - f.day).days <= 1:
                    f.state, f.reason = "PENDING", "中天充值，等待另一端记录"
                elif f.account != self.zt_account:
                    f.state = "REVIEW"
                    f.reason = ("付款给中通总部，但中天账户当日及次日没有对应充值，"
                                "请确认用途（面单/物料/保证金/其他）")
            elif f.category == "XFER_INTERNAL":
                f.state, f.kind = "REVIEW", "transfer_out_unknown" if f.direction == "OUT" else "transfer_in_unknown"
                f.reason = ("转出到本网点其他账户，但未找到对应转入（对方账户可能未纳入系统）" if f.direction == "OUT"
                            else "转入但未找到来源账户的转出记录")


def persist(store: Store, result: MatchResult) -> None:
    with store.tx():
        store.execute("DELETE FROM flow_states")
        store.execute("DELETE FROM links")
        for link in result.links:
            store.execute("INSERT OR REPLACE INTO links (link_id, kind, flow_a, flow_b, biz_date, amount_cents, "
                          "fee_cents, rule, actor) VALUES (:link_id,:kind,:flow_a,:flow_b,:biz_date,:amount_cents,"
                          ":fee_cents,:rule,:actor)", link)
        store.conn.executemany(
            "INSERT INTO flow_states (flow_id, biz_date, state, category, reason, link_id, kind) VALUES (?,?,?,?,?,?,?)",
            [(f.flow_id, f.day.isoformat(), f.state, f.category, f.reason, f.link_id or f.dup_link, f.kind)
             for f in result.flows.values()])
