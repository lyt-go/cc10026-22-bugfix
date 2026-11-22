"""用量计量领域服务：管理API调用用量计量和计费周期结算。

跨模块数据流：
- 消费 api_registry 共享快照 → 核验计费API
- 消费 subscription 共享快照 → 审批通过后初始化用量
- 消费 rate_limiter 快照 → 超限写入用量异常
- 写入 data/usage/ 共享文件 → call_audit 消费审计
- 计费周期结束后触发结算 → 写入结算汇总
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
                     data_rows: int = 0, amount: float = 0.0) -> dict[str, Any]:
        """记录一次API调用用量。"""
        from app.services.data_manager import next_usage_id, read_usage, write_usage
        from app.store import store as s

        rows = s.rows(self.MODULE)
        # 查找已有记录
        billing_cycle = self._current_billing_cycle()
        for row in rows:
            if (row.get("API名称") == api_name and row.get("调用方") == caller
                    and row.get("计费周期") == billing_cycle):
                usage_id = int(row.get("id", 0))
                old_count = int(row.get("调用次数", "0") or 0)
                old_rows = int(row.get("数据行数", "0") or 0)
                old_amount = float(row.get("费用金额", 0) or 0)
                row["调用次数"] = str(old_count + call_count)
                row["数据行数"] = str(old_rows + data_rows)
                row["费用金额"] = old_amount + amount
                # 保留磁盘原单上已有关联（订阅、结算凭据等），只更新计量值
                persisted = read_usage(usage_id)
                persisted.update({
                    "usage_id": usage_id,
                    "record_no": row.get("记录编号"),
                    "caller": caller,
                    "api_name": api_name,
                    "call_count": old_count + call_count,
                    "data_rows": old_rows + data_rows,
                    "amount": old_amount + amount,
                    "billing_cycle": billing_cycle,
                    "settlement_status": persisted.get("settlement_status") or row.get("status"),
                })
                write_usage(usage_id, persisted)
                return row

        # 新记录：编号不与磁盘原单、内存记录冲突
        new_id = next_usage_id(int(r.get("id", 0)) for r in rows)
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

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        result = super().run_action(entry_id, action)

        if action == "完成结算" and entry:
            from app.services.data_manager import settle_billing
            billing_cycle = entry.get("计费周期", self._current_billing_cycle())
            settlement = settle_billing(billing_cycle, use_auto_settlement=True)

            # 联动增强：结算完成写入调用审计记录，
            # 金额与次数以实际确认的结算记录为准，并带上结算凭据编号
            from app.services.data_manager import write_audit, DATA_ROOT
            audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
            write_audit(audit_id, {
                "audit_id": audit_id,
                "api_name": entry.get("API名称", ""),
                "caller": entry.get("调用方", ""),
                "event": "billing_settled",
                "billing_cycle": billing_cycle,
                "settlement_ids": [v.get("settlement_id")
                                   for v in settlement.get("vouchers", [])],
                "total_amount": settlement.get("confirmed_amount", 0),
                "total_calls": settlement.get("confirmed_calls", 0),
                "confirmed_count": settlement.get("record_count", 0),
                "pending_count": settlement.get("pending_count", 0),
            })

        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def _current_billing_cycle(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")

    def get_billing_cycle_detail(self, billing_cycle: str | None = None) -> dict[str, Any]:
        """账期视图：返回持久化的原单和账期结算明细。

        工作台再次打开账期时调用。原单与结算凭据都从共享目录读取，
        并基于原单实况给出已确认/待完成拆分，便于对照明细与汇总。
        """
        from app.services.data_manager import list_usage, read_settlement

        cycle = billing_cycle or self._current_billing_cycle()
        usages = list_usage(cycle)
        settlement = read_settlement(cycle)
        confirmed = [u for u in usages
                     if u.get("settlement_status") == "已结算" and u.get("settlement_id")]
        pending = [u for u in usages if u not in confirmed]
        confirmed_amount = round(sum(float(u.get("amount", 0) or 0) for u in confirmed), 2)
        confirmed_calls = sum(int(u.get("call_count", 0) or 0) for u in confirmed)
        pending_amount = round(sum(float(u.get("amount", 0) or 0) for u in pending), 2)
        pending_calls = sum(int(u.get("call_count", 0) or 0) for u in pending)
        consistent = (
            not settlement
            or (float(settlement.get("confirmed_amount", 0) or 0) == confirmed_amount
                and int(settlement.get("confirmed_calls", 0) or 0) == confirmed_calls
                and float(settlement.get("pending_amount", 0) or 0) == pending_amount
                and int(settlement.get("pending_calls", 0) or 0) == pending_calls)
        )
        return {
            "billing_cycle": cycle,
            "usages": usages,
            "settlement": settlement,
            "vouchers": settlement.get("vouchers", []),
            "confirmed_amount": confirmed_amount,
            "confirmed_calls": confirmed_calls,
            "pending_amount": pending_amount,
            "pending_calls": pending_calls,
            "consistent": consistent,
        }

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
        }