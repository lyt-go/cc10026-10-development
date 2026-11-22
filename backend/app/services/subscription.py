"""订阅管理领域服务：管理API订阅申请、审批流转和续期。

跨模块数据流：
- 消费 api_registry 共享快照 → 校验订阅API是否存在
- 审批通过后 → usage_billing 消费用量计量
- 审批通过后 → call_audit 消费调用审计
- 订阅变更 → 写 subscriptions/ 共享记录
- 审批流转完整记录到共享目录

订阅费用归属（新增）：
- register_effective_subscription 登记有效订阅，供用量计量做归属计算；
- close_billing_period 封账，按已核定金额形成各订阅的账期明细；
- 封账后凭据确认 → apply_post_close_confirmation 在后续账期补记。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SubscriptionService(BaseModuleService):
    MODULE = "subscription"
    ENTITY = "订阅申请"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["申请编号", "订阅API", "申请方", "调用频率", "数据范围", "有效期", "审批状态", "驳回原因"]
    REQUIRED_FIELDS = ["申请编号", "订阅API", "申请方"]
    STATUS_ORDER = ["草稿", "待审批", "已通过", "已驳回"]
    ACTION_RULES = {"提交审批": "待审批", "审批通过": "已通过", "驳回申请": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回申请"]

    def _validate_api_exists(self, sub_data: dict[str, Any]) -> bool:
        """校验被订阅的API是否已注册。"""
        from app.services.data_manager import read_api_snapshot
        api_name = sub_data.get("订阅API", "")
        registry = read_api_snapshot()
        for api_data in (registry.values() if isinstance(registry, dict) else []):
            if isinstance(api_data, dict) and api_data.get("api_name") == api_name:
                return True
        return False

    def _write_subscription_record(self, sub_id: int, sub_data: dict[str, Any]) -> None:
        """将订阅记录写入共享目录。"""
        from app.services.data_manager import write_subscription
        record = {
            "application_no": sub_data.get("申请编号"),
            "api_name": sub_data.get("订阅API"),
            "applicant": sub_data.get("申请方"),
            "call_frequency": sub_data.get("调用频率"),
            "data_scope": sub_data.get("数据范围"),
            "valid_until": sub_data.get("有效期"),
            "approval_status": sub_data.get("status"),
            "reject_reason": sub_data.get("驳回原因", ""),
        }
        write_subscription(sub_id, record)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "提交审批":
            if not self._validate_api_exists(entry):
                return None, "订阅的目标API尚未注册，无法提交审批"
            self._write_subscription_record(entry_id, entry)

        if action == "审批通过":
            # 联动增强：创建用量初始化记录
            from app.services.data_manager import write_usage
            from app.store import store as s

            billing_data = {
                "subscription_id": entry_id,
                "applicant": entry.get("申请方"),
                "api_name": entry.get("订阅API"),
                "call_count": 0,
                "data_rows": 0,
                "amount": 0,
                "billing_cycle": self._current_billing_cycle(),
                "settlement_status": "计量中",
            }
            usage_rows = s.rows("usage_billing")
            billing_id = max((int(r.get("id", 0)) for r in usage_rows), default=0) + 1
            write_usage(billing_id, billing_data)

            # 联动增强：审批通过后自动创建限流策略
            rate_rows = s.rows("rate_limiter")
            rate_id = max((int(r.get("id", 0)) for r in rate_rows), default=0) + 1
            rate_rows.append({
                "id": rate_id,
                "策略编号": f"RATE-{rate_id:04d}",
                "目标API": entry.get("订阅API"),
                "调用方": entry.get("申请方"),
                "每秒上限": "10",
                "每分钟上限": "600",
                "每日上限": entry.get("调用频率", "10000"),
                "超限动作": "延迟重试",
                "策略版本": "v1",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
                "_auto_created": True,
            })

            # 联动增强：自动记录审计——审批通过事件
            from app.services.data_manager import write_audit, DATA_ROOT
            audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
            write_audit(audit_id, {
                "audit_id": audit_id,
                "api_name": entry.get("订阅API"),
                "caller": entry.get("申请方"),
                "event": "subscription_approved",
                "subscription_id": entry_id,
                "approval_status": "已通过",
            })

        result = super().run_action(entry_id, action)

        # 更新共享订阅记录
        if entry:
            self._write_subscription_record(entry_id, entry)

        # 写模块快照
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())

        return result

    def _current_billing_cycle(self) -> str:
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone(timedelta(hours=8)))
        return now.strftime("%Y-%m")

    # ========== 有效订阅与账期明细 ==========

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime
        from app.services.data_manager import TZ
        return datetime.now(TZ).isoformat()

    def register_effective_subscription(self, *, subscription_id: str, team_id: str,
                                        caller: str, api_name: str,
                                        valid_from: str, valid_to: str,
                                        registered_at: str | None = None) -> dict[str, Any]:
        """登记一份有效订阅，供用量计量做「调用明细 × 有效订阅」归属计算。"""
        from app.services import attribution_store as astore
        sub = {
            "subscription_id": subscription_id, "team_id": team_id,
            "caller": caller, "api_name": api_name,
            "valid_from": valid_from, "valid_to": valid_to,
            "registered_at": registered_at or self._now_iso(),
        }
        astore.save_subscription(sub)
        return sub

    def close_billing_period(self, *, team_id: str, month: str,
                             closed_at: str | None = None) -> dict[str, Any]:
        """封账：按已核定金额形成各订阅的账期明细。

        - 已核定且未出账的调用 → CHARGE 账期明细；
        - 待核调用不出账，待核余额保留，并留下封账快照作为封账依据；
        - 封账只能进行一次，重复封账直接报错。
        """
        from app.services import attribution_store as astore
        now = closed_at or self._now_iso()
        with astore.team_periods_lock(team_id):
            period = astore.load_period(team_id, month) or {
                "team_id": team_id, "month": month, "status": "OPEN", "entries": [],
            }
            if period.get("status") == "CLOSED":
                raise ValueError(f"账期 {team_id}/{month} 已封账，不能重复封账")
            for call_ref in astore.list_calls(team_id, month):
                with astore.call_lock(call_ref["call_id"]):
                    call = astore.load_call(call_ref["call_id"])
                    astore.append_event(call, astore.CLOSE_SNAPSHOT, now,
                                        subscription_id=call.get("subscription_id"),
                                        period=month,
                                        detail="封账时归属快照（封账依据）")
                    if call["status"] == "CONFIRMED" and call.get("billed_period") is None:
                        period["entries"].append({
                            "entry_id": f"{month}-{len(period['entries']) + 1:04d}",
                            "entry_type": astore.CHARGE,
                            "subscription_id": call["subscription_id"],
                            "call_id": call["call_id"],
                            "amount_cents": call["confirmed_cents"],
                            "original_period": None,
                            "evidence_no": call.get("evidence_no"),
                            "created_at": now,
                        })
                        call["billed_period"] = month
                    call["updated_at"] = now
                    astore.save_call(call)
            period["status"] = "CLOSED"
            period["closed_at"] = now
            astore.save_period(period)
        return period

    def apply_post_close_confirmation(self, *, call_id: str,
                                      at: str | None = None) -> dict[str, Any] | None:
        """封账后凭据确认：在下一个未封账账期补记账期明细，并指向原账期。

        若封账流程已把该调用出账（并发交错），则不再补记，保证只出账一次。
        """
        from app.services import attribution_store as astore
        now = at or self._now_iso()
        call_ref = astore.load_call(call_id)
        if call_ref is None:
            return None
        team_id, original_month = call_ref["team_id"], call_ref["called_at"][:7]
        with astore.team_periods_lock(team_id):
            with astore.call_lock(call_id):
                call = astore.load_call(call_id)
                if call.get("billed_period") is not None:
                    return None  # 封账流程已出账，不重复补记
                target = original_month
                period = None
                while True:
                    period = astore.load_period(team_id, target)
                    if period is None or period.get("status") == "OPEN":
                        break
                    target = astore.next_month(target)
                if period is None:
                    period = {"team_id": team_id, "month": target,
                              "status": "OPEN", "entries": []}
                entry = {
                    "entry_id": f"{target}-{len(period['entries']) + 1:04d}",
                    "entry_type": astore.POST_CLOSE_CONFIRMATION,
                    "subscription_id": call["subscription_id"],
                    "call_id": call_id,
                    "amount_cents": call["confirmed_cents"],
                    "original_period": original_month,
                    "evidence_no": call.get("evidence_no"),
                    "created_at": now,
                }
                period["entries"].append(entry)
                astore.save_period(period)
                call["billed_period"] = target
                call["updated_at"] = now
                astore.append_event(call, astore.BILLED_POST_CLOSE, now,
                                    subscription_id=call["subscription_id"],
                                    period=target,
                                    detail=f"原账期 {original_month} 已封账，补记至账期 {target}")
                astore.save_call(call)
                return entry

    def period_bill(self, *, team_id: str, month: str) -> dict[str, Any]:
        """某团队某账期的账期明细：按订阅汇总。"""
        from app.services import attribution_store as astore
        period = astore.load_period(team_id, month)
        entries = list(period.get("entries", [])) if period else []
        by_subscription: dict[str, int] = {}
        for entry in entries:
            sub_id = entry["subscription_id"]
            by_subscription[sub_id] = by_subscription.get(sub_id, 0) + entry["amount_cents"]
        return {
            "team_id": team_id, "month": month,
            "status": period.get("status") if period else None,
            "entries": entries,
            "by_subscription": by_subscription,
            "total_cents": sum(by_subscription.values()),
        }

    def team_statement(self, *, team_id: str) -> dict[str, Any]:
        """团队对账单：各订阅账单合计 + 已核定未出账 + 待核余额。

        校验不变量：已出账 + 已核定未出账 + 待核余额 ≡ 调用费用总额
        （即按调用编号 归属金额 + 待核余额 ≡ 原调用金额 的汇总）。
        """
        from app.services import attribution_store as astore
        calls = astore.list_calls(team_id)
        calls_cents = sum(int(c.get("amount_cents", 0)) for c in calls)
        pending_cents = sum(int(c.get("pending_cents", 0)) for c in calls)
        billed_by_subscription: dict[str, int] = {}
        for period in astore.list_periods(team_id):
            for entry in period.get("entries", []):
                sub_id = entry["subscription_id"]
                billed_by_subscription[sub_id] = \
                    billed_by_subscription.get(sub_id, 0) + entry["amount_cents"]
        billed_cents = sum(billed_by_subscription.values())
        # 已核定但尚未出账（凭据先到、账期未封）
        unbilled_by_subscription: dict[str, int] = {}
        for call in calls:
            if call.get("status") == "CONFIRMED" and call.get("billed_period") is None:
                sub_id = call["subscription_id"]
                unbilled_by_subscription[sub_id] = \
                    unbilled_by_subscription.get(sub_id, 0) + int(call["confirmed_cents"])
        unbilled_cents = sum(unbilled_by_subscription.values())
        return {
            "team_id": team_id,
            "calls_cents": calls_cents,
            "billed_cents": billed_cents,
            "unbilled_cents": unbilled_cents,
            "pending_cents": pending_cents,
            "billed_by_subscription": billed_by_subscription,
            "unbilled_by_subscription": unbilled_by_subscription,
            "balanced": billed_cents + unbilled_cents + pending_cents == calls_cents,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        sub_list = []
        for row in rows:
            sub_list.append({
                "id": row.get("id"),
                "app_no": row.get("申请编号"),
                "api_name": row.get("订阅API"),
                "applicant": row.get("申请方"),
                "frequency": row.get("调用频率"),
                "scope": row.get("数据范围"),
                "valid_until": row.get("有效期"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(sub_list),
            "subscriptions": sub_list,
            "approved_count": sum(1 for r in sub_list if r.get("status") == "已通过"),
            "pending_count": sum(1 for r in sub_list if r.get("status") == "待审批"),
        }