"""用量计量领域服务：管理API调用用量计量和计费周期结算。

跨模块数据流：
- 消费 api_registry 共享快照 → 核验计费API
- 消费 subscription 共享快照 → 审批通过后初始化用量
- 消费 rate_limiter 快照 → 超限写入用量异常
- 写入 data/usage/ 共享文件 → call_audit 消费审计
- 计费周期结束后触发结算 → 写入结算汇总

订阅费用归属（新增）：
- record_call_detail 记录调用明细，并与有效订阅关联计算，生成可用于出账的
  订阅费用归属明细（归属金额 + 待核余额 ≡ 原调用金额）；
- submit_evidence 补录订阅凭据并核定归属，并发补录只允许确认一次；
- 归属状态持久化在 data/attribution/，进程重启后仍可沿调用编号回溯。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class UsageBillingService(BaseModuleService):
    MODULE = "usage_billing"
    ENTITY = "用量记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "调用方", "API名称", "调用次数", "数据行数", "计费周期", "费用金额", "结算状态"]
    REQUIRED_FIELDS = ["记录编号", "调用方", "API名称"]
    STATUS_ORDER = ["计量中", "已出账", "已确认", "已结算"]
    ACTION_RULES = {"确认出账": "已出账", "标记确认": "已确认", "完成结算": "已结算"}
    NEGATIVE_ACTIONS = []

    def record_usage(self, api_name: str, caller: str, call_count: int = 1,
                     data_rows: int = 0, amount: float = 0.0,
                     billing_cycle: str | None = None) -> dict[str, Any]:
        """记录一次API调用用量（按调用方、API、月份累计）。"""
        from app.services.data_manager import write_usage
        from app.store import store as s

        rows = s.rows(self.MODULE)
        # 查找已有记录
        billing_cycle = billing_cycle or self._current_billing_cycle()
        for row in rows:
            if (row.get("API名称") == api_name and row.get("调用方") == caller
                    and row.get("计费周期") == billing_cycle):
                old_count = int(row.get("调用次数", "0") or 0)
                old_rows = int(row.get("数据行数", "0") or 0)
                old_amount = float(row.get("费用金额", 0) or 0)
                row["调用次数"] = str(old_count + call_count)
                row["数据行数"] = str(old_rows + data_rows)
                row["费用金额"] = old_amount + amount
                write_usage(row.get("id", 0), {
                    "usage_id": row.get("id"),
                    "record_no": row.get("记录编号"),
                    "caller": caller,
                    "api_name": api_name,
                    "call_count": old_count + call_count,
                    "data_rows": old_rows + data_rows,
                    "amount": old_amount + amount,
                    "billing_cycle": billing_cycle,
                    "settlement_status": row.get("status"),
                })
                return row

        # 新记录
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        new_record = {
            "id": new_id,
            "记录编号": f"USAG-{new_id:04d}",
            "调用方": caller,
            "API名称": api_name,
            "调用次数": str(call_count),
            "数据行数": str(data_rows),
            "计费周期": billing_cycle,
            "费用金额": amount,
            "结算状态": self.STATUS_ORDER[0],
            "status": self.STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
        }
        rows.append(new_record)
        write_usage(new_id, {
            "usage_id": new_id,
            "record_no": f"USAG-{new_id:04d}",
            "caller": caller,
            "api_name": api_name,
            "call_count": call_count,
            "data_rows": data_rows,
            "amount": amount,
            "billing_cycle": billing_cycle,
            "settlement_status": self.STATUS_ORDER[0],
        })
        return new_record

    # ========== 订阅费用归属：调用明细 × 有效订阅 ==========

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime
        from app.services.data_manager import TZ
        return datetime.now(TZ).isoformat()

    def record_call_detail(self, *, call_id: str, team_id: str, caller: str,
                           api_name: str, amount_cents: int, called_at: str,
                           recorded_at: str | None = None) -> dict[str, Any]:
        """记录一笔调用明细，并与有效订阅关联计算，生成订阅费用归属明细。

        归属规则：只有调用发生时有效且匹配调用方和 API 的订阅可以承接费用。
        恰好一个候选订阅 → 立即核定；否则金额挂待核，等待订阅凭据。
        任何时点都满足：归属金额 + 待核余额 = 原调用金额。
        """
        from app.services import attribution_store as astore

        now = recorded_at or self._now_iso()
        candidates = astore.find_candidates(team_id, caller, api_name, called_at)
        if len(candidates) == 1:
            status, subscription_id = "CONFIRMED", candidates[0]["subscription_id"]
            confirmed_cents, pending_cents = amount_cents, 0
            detail = "唯一候选订阅，即时核定"
        else:
            status, subscription_id = "PENDING", None
            confirmed_cents, pending_cents = 0, amount_cents
            detail = f"候选订阅 {len(candidates)} 个，费用挂待核，等待订阅凭据"
        record = {
            "call_id": call_id, "team_id": team_id, "caller": caller,
            "api_name": api_name, "amount_cents": amount_cents, "called_at": called_at,
            "status": status, "subscription_id": subscription_id,
            "confirmed_cents": confirmed_cents, "pending_cents": pending_cents,
            "evidence_no": None, "billed_period": None,
            "events": [], "created_at": now, "updated_at": now,
        }
        astore.append_event(record, astore.CALL_RECORDED, now,
                            subscription_id=subscription_id, detail=detail)
        astore.create_call(record)
        # 旧的「按调用方、API、月份累计」保持不变，明细与累计同源
        self.record_usage(api_name, caller, call_count=1,
                          amount=amount_cents / 100, billing_cycle=called_at[:7])
        return record

    def submit_evidence(self, *, call_id: str, subscription_id: str,
                        evidence_no: str, submitted_at: str | None = None) -> dict[str, Any]:
        """补录订阅凭据并核定归属。

        - 只有调用发生时有效且匹配调用方和 API 的订阅可以承接费用；
        - 并发/重复补录只允许确认一次（按调用编号加互斥锁）；
        - 若调用所在账期已封账，核定金额由订阅管理在后续账期补记。
        """
        from app.services import attribution_store as astore

        now = submitted_at or self._now_iso()
        with astore.call_lock(call_id):
            record = astore.load_call(call_id)
            if record is None:
                return {"outcome": "REJECTED", "call_id": call_id,
                        "reason": f"调用 {call_id} 不存在"}
            if record["status"] == "CONFIRMED":
                astore.append_event(record, astore.EVIDENCE_REJECTED, now,
                                    subscription_id=subscription_id,
                                    detail=f"凭据 {evidence_no} 被拒绝：该调用已核定，补录只确认一次")
                astore.save_call(record)
                return {"outcome": "DUPLICATE", "call_id": call_id,
                        "confirmed_subscription": record["subscription_id"],
                        "reason": "该调用已核定，凭据补录只确认一次"}
            valid = {s["subscription_id"] for s in astore.find_candidates(
                record["team_id"], record["caller"], record["api_name"], record["called_at"])}
            if subscription_id not in valid:
                astore.append_event(record, astore.EVIDENCE_REJECTED, now,
                                    subscription_id=subscription_id,
                                    detail=f"凭据 {evidence_no} 被拒绝：订阅在调用发生时无效或不匹配调用方/API")
                astore.save_call(record)
                return {"outcome": "REJECTED", "call_id": call_id,
                        "reason": "订阅在调用发生时无效或不匹配调用方/API"}
            # 核定：待核余额 → 归属金额
            record["status"] = "CONFIRMED"
            record["subscription_id"] = subscription_id
            record["confirmed_cents"] = record["amount_cents"]
            record["pending_cents"] = 0
            record["evidence_no"] = evidence_no
            record["updated_at"] = now
            astore.append_event(record, astore.CONFIRMED, now,
                                subscription_id=subscription_id,
                                detail=f"凭据 {evidence_no} 核定，待核余额转入归属金额")
            astore.save_call(record)

        # 账期已封账 → 订阅管理补记账期明细（锁外联动，避免锁嵌套）
        billing_entry = None
        period = astore.load_period(record["team_id"], record["called_at"][:7])
        if period is not None and period.get("status") == "CLOSED":
            from app.services import service_for
            billing_entry = service_for("subscription").apply_post_close_confirmation(
                call_id=call_id, at=now)
        return {
            "outcome": "CONFIRMED", "call_id": call_id,
            "subscription_id": subscription_id,
            "confirmed_cents": record["confirmed_cents"],
            "pending_cents": record["pending_cents"],
            "billing_entry": billing_entry,
        }

    def attribution_of(self, call_id: str) -> dict[str, Any] | None:
        """按调用编号查当前归属明细。"""
        from app.services import attribution_store as astore
        return astore.load_call(call_id)

    def verify_call_balance(self, call_id: str) -> bool:
        """不变量：归属金额 + 待核余额 ≡ 原调用金额。"""
        record = self.attribution_of(call_id)
        if record is None:
            return False
        return record["confirmed_cents"] + record["pending_cents"] == record["amount_cents"]

    def call_trail(self, call_id: str) -> dict[str, Any] | None:
        """沿调用编号回溯：归属状态、事件流、命中的账期明细。

        事件流里有封账时的归属快照（封账依据）与后续确认形成的待核余额
        变化；账期明细里有出账、封账后补记/冲正记录。全部来自磁盘文件，
        进程重启后仍可追查。
        """
        from app.services import attribution_store as astore
        record = astore.load_call(call_id)
        if record is None:
            return None
        entries = [dict(entry, period=period["month"])
                   for period in astore.list_periods(record["team_id"])
                   for entry in period.get("entries", [])
                   if entry.get("call_id") == call_id]
        return {"call": record, "billing_entries": entries}

    def attribution_report(self, *, team_id: str, month: str) -> dict[str, Any]:
        """重新出具某团队某月的订阅费用归属明细。

        封账后补入凭据的调用，会带上补记到的账期与原账期，两份订阅账单
        各受多大影响一目了然。
        """
        from app.services import attribution_store as astore
        entries_by_call: dict[str, list[dict[str, Any]]] = {}
        for period in astore.list_periods(team_id):
            for entry in period.get("entries", []):
                entries_by_call.setdefault(entry.get("call_id"), []).append(
                    dict(entry, period=period["month"]))
        rows = []
        for call in astore.list_calls(team_id, month):
            rows.append({
                "call_id": call["call_id"], "caller": call["caller"],
                "api_name": call["api_name"], "amount_cents": call["amount_cents"],
                "status": call["status"], "subscription_id": call["subscription_id"],
                "confirmed_cents": call["confirmed_cents"],
                "pending_cents": call["pending_cents"],
                "billing": entries_by_call.get(call["call_id"], []),
            })
        return {
            "team_id": team_id, "month": month, "rows": rows,
            "amount_cents": sum(r["amount_cents"] for r in rows),
            "confirmed_cents": sum(r["confirmed_cents"] for r in rows),
            "pending_cents": sum(r["pending_cents"] for r in rows),
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        result = super().run_action(entry_id, action)

        if action == "完成结算" and entry:
            from app.services.data_manager import settle_billing
            billing_cycle = entry.get("计费周期", self._current_billing_cycle())
            settlement = settle_billing(billing_cycle, use_auto_settlement=True)

            # 联动增强：结算完成写入调用审计记录
            from app.services.data_manager import write_audit, DATA_ROOT
            audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
            write_audit(audit_id, {
                "audit_id": audit_id,
                "api_name": entry.get("API名称", ""),
                "caller": entry.get("调用方", ""),
                "event": "billing_settled",
                "billing_cycle": billing_cycle,
                "total_amount": settlement.get("total_amount", 0),
                "total_calls": settlement.get("total_calls", 0),
            })

        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def _current_billing_cycle(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        usage_list = []
        for row in rows:
            usage_list.append({
                "id": row.get("id"),
                "record_no": row.get("记录编号"),
                "caller": row.get("调用方"),
                "api_name": row.get("API名称"),
                "call_count": row.get("调用次数"),
                "data_rows": row.get("数据行数"),
                "billing_cycle": row.get("计费周期"),
                "amount": row.get("费用金额"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(usage_list),
            "usages": usage_list,
            "total_amount": sum(float(r.get("amount", 0) or 0) for r in usage_list),
            "settled_count": sum(1 for r in usage_list if r.get("status") == "已结算"),
            "attribution": self._attribution_summary(),
        }

    @staticmethod
    def _attribution_summary() -> dict[str, Any]:
        """订阅费用归属汇总：归属金额 + 待核余额 ≡ 调用总额。"""
        from app.services import attribution_store as astore
        calls = astore.list_calls()
        return {
            "call_count": len(calls),
            "amount_cents": sum(int(c.get("amount_cents", 0)) for c in calls),
            "confirmed_cents": sum(int(c.get("confirmed_cents", 0)) for c in calls),
            "pending_cents": sum(int(c.get("pending_cents", 0)) for c in calls),
        }