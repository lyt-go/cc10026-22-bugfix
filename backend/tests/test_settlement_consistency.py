"""账期结算一致性测试：明细与汇总落库一致、部分写入可恢复、确认金额只计一次。

场景还原：
- 用量 7 号、8 号（挂在同一订阅下）分别累计 4 次、11 次调用，再加一笔无订阅用量；
- 第一次出账在写盘尚未完成时中断：第一笔原单已确认、汇总已可读、其余明细未落盘；
- 另一位财务人员重新打开账期再次提交：沿用未完成的结算凭据补齐，不冲突、不重复计数；
- 最终对照明细、汇总与调用审计：三笔原单各一份，最终次数与对应金额一致。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestSettlementConsistency:
    """账期结算明细与汇总落库一致性"""

    def setup_method(self):
        """每个测试前重置内存仓库，并把数据目录切到独立临时目录。"""
        from app.seed import SEED_ROWS
        from app.store import store as s
        from app.services import data_manager as dm

        s._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        self._tmp = tempfile.mkdtemp(prefix='settlement_test_')
        self._orig_root = dm.DATA_ROOT
        dm.DATA_ROOT = Path(self._tmp)

    def teardown_method(self):
        from app.services import data_manager as dm
        dm.DATA_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---------- 准备 ----------

    def _cycle(self) -> str:
        from app.services.usage_billing import UsageBillingService
        return UsageBillingService()._current_billing_cycle()

    def _seed_subscribed_usage(self) -> str:
        """用量7号(4次/40元)、8号(11次/110元)，两笔挂在同一订阅(subscription_id=3)下。"""
        from app.services import data_manager as dm

        cycle = self._cycle()
        dm.write_usage(7, {
            "record_no": "USAG-0007", "subscription_id": 3,
            "caller": "订阅调用方", "api_name": "订单查询API",
            "call_count": 4, "data_rows": 40, "amount": 40.0,
            "billing_cycle": cycle, "settlement_status": "计量中",
        })
        dm.write_usage(8, {
            "record_no": "USAG-0008", "subscription_id": 3,
            "caller": "订阅调用方", "api_name": "订单查询API",
            "call_count": 11, "data_rows": 110, "amount": 110.0,
            "billing_cycle": cycle, "settlement_status": "计量中",
        })
        return cycle

    def _add_unsubscribed_usage(self) -> dict:
        """再加入一笔无订阅用量(3次/30元)。"""
        from app.services.usage_billing import UsageBillingService
        svc = UsageBillingService()
        return svc.record_usage("开放数据API", "临时调用方",
                                call_count=3, data_rows=30, amount=30.0)

    # ---------- 主场景 ----------

    def test_partial_write_then_resettle_keeps_details_summary_audit_consistent(self):
        from app.services import data_manager as dm
        from app.services.usage_billing import UsageBillingService

        cycle = self._seed_subscribed_usage()
        unsub = self._add_unsubscribed_usage()
        unsub_id = unsub["id"]
        assert unsub_id == 9, "无订阅用量应占用自己的记录编号，不与7号/8号冲突"

        # ---- 第一次出账：第一笔写盘后中断，后续明细未落盘 ----
        original_write = dm.write_usage
        written = []

        def flaky_write(usage_id, data):
            if len(written) >= 1:
                raise IOError("模拟写盘中断")
            written.append(usage_id)
            return original_write(usage_id, data)

        dm.write_usage = flaky_write
        try:
            dm.settle_billing(cycle)
            raise AssertionError("写盘中断应当抛出异常")
        except IOError:
            pass
        finally:
            dm.write_usage = original_write

        assert written == [7], "第一笔(7号)已写盘，第二笔起中断"

        # 部分写入期间：汇总已可读，且能区分已确认与待完成金额
        summary = dm.read_settlement(cycle)
        assert summary["status"] == "结算中"
        assert summary["confirmed_calls"] == 4 and summary["confirmed_amount"] == 40.0
        assert summary["pending_calls"] == 14 and summary["pending_amount"] == 140.0
        assert summary["record_count"] == 1 and summary["pending_count"] == 2
        voucher = summary["vouchers"][0]
        assert voucher["status"] == "结算中"
        assert voucher["usage_ids"] == [7, 8, unsub_id]
        voucher_id = voucher["settlement_id"]

        # 明细侧：只有7号关联了结算凭据，8号/9号仍待完成
        assert dm.read_usage(7)["settlement_id"] == voucher_id
        assert dm.read_usage(7)["settlement_status"] == "已结算"
        assert dm.read_usage(8).get("settlement_id") is None
        assert dm.read_usage(unsub_id).get("settlement_id") is None

        # 工作台此时打开账期：返回持久化的原单和结算明细，汇总与原单一致
        svc = UsageBillingService()
        mid_view = svc.get_billing_cycle_detail(cycle)
        assert len(mid_view["usages"]) == 3
        assert mid_view["consistent"] is True
        assert mid_view["confirmed_calls"] == 4 and mid_view["pending_calls"] == 14

        # ---- 另一位财务人员重新打开账期再次提交：沿用未完成的凭据补齐 ----
        summary = dm.settle_billing(cycle)
        assert summary["status"] == "已确认"
        assert len(summary["vouchers"]) == 1, "应沿用原结算凭据，而不是冲突地重复出账"
        voucher = summary["vouchers"][0]
        assert voucher["settlement_id"] == voucher_id
        assert voucher["status"] == "已确认"
        assert voucher["total_calls"] == 18 and voucher["total_amount"] == 180.0

        # 汇总：三笔原单全部确认，金额只计一次
        assert summary["record_count"] == 3 and summary["pending_count"] == 0
        assert summary["total_calls"] == 18 and summary["total_amount"] == 180.0
        assert summary["confirmed_calls"] == 18 and summary["confirmed_amount"] == 180.0

        # 明细：三笔原单仍各有一份，按自己的记录编号落盘并关联实际凭据
        usage_files = sorted(f.name for f in dm.DATA_ROOT.glob("usage/[0-9]*.json"))
        assert usage_files == ["000007.json", "000008.json", "000009.json"]
        assert not (dm.DATA_ROOT / "usage" / "000000.json").exists(), "无订阅用量不得挤到0号"
        assert not (dm.DATA_ROOT / "usage" / "000003.json").exists(), "同订阅多笔不得落到订阅编号"
        for usage_id, calls, amount in ((7, 4, 40.0), (8, 11, 110.0), (unsub_id, 3, 30.0)):
            record = dm.read_usage(usage_id)
            assert record["usage_id"] == usage_id
            assert record["record_no"] == f"USAG-{usage_id:04d}"
            assert int(record["call_count"]) == calls
            assert float(record["amount"]) == amount
            assert record["settlement_status"] == "已结算"
            assert record["settlement_id"] == voucher_id

        # 按凭据可查清哪些用量已记入出账
        billed = dm.list_usage_by_settlement(voucher_id, cycle)
        assert sorted(r["usage_id"] for r in billed) == [7, 8, unsub_id]

        # 工作台再次打开账期：持久化的原单与结算明细一致返回
        view = svc.get_billing_cycle_detail(cycle)
        assert view["consistent"] is True
        assert len(view["usages"]) == 3
        assert view["confirmed_calls"] == 18 and view["confirmed_amount"] == 180.0
        assert view["pending_calls"] == 0 and view["pending_amount"] == 0.0
        assert view["settlement"]["status"] == "已确认"

        # ---- 调用审计：金额与次数依据实际确认的结算记录生成 ----
        entry, msg = svc.run_action(unsub_id, "完成结算")
        assert entry is not None, msg
        audits = [a for a in dm.list_audit() if a.get("event") == "billing_settled"]
        assert len(audits) == 1
        audit = audits[0]
        assert audit["billing_cycle"] == cycle
        assert audit["settlement_ids"] == [voucher_id]
        assert audit["total_calls"] == 18 and audit["total_amount"] == 180.0
        assert audit["confirmed_count"] == 3 and audit["pending_count"] == 0

        # ---- 确认后的金额只能计一次：再次出账幂等 ----
        again = dm.settle_billing(cycle)
        assert len(again["vouchers"]) == 1
        assert again["total_calls"] == 18 and again["total_amount"] == 180.0
        assert again["record_count"] == 3
        usage_files = sorted(f.name for f in dm.DATA_ROOT.glob("usage/[0-9]*.json"))
        assert usage_files == ["000007.json", "000008.json", "000009.json"]

    def test_repeat_settle_counts_confirmed_amount_once(self):
        """无中断重复出账：确认后的金额只计一次，汇总文件不会被当成原单。"""
        from app.services import data_manager as dm

        cycle = self._seed_subscribed_usage()
        self._add_unsubscribed_usage()

        first = dm.settle_billing(cycle)
        second = dm.settle_billing(cycle)

        assert first["status"] == "已确认" and second["status"] == "已确认"
        assert first["total_calls"] == 18 and second["total_calls"] == 18
        assert first["total_amount"] == 180.0 and second["total_amount"] == 180.0
        assert len(second["vouchers"]) == 1
        # 结算汇总等附属文件不算原单
        assert len(dm.list_usage(cycle)) == 3
        assert sorted(r["usage_id"] for r in dm.list_usage(cycle)) == [7, 8, 9]

    def test_new_usage_after_settle_gets_new_voucher_without_recount(self):
        """账期已确认后新增用量：新开凭据只结算待完成部分，已确认金额不重复计。"""
        from app.services import data_manager as dm

        cycle = self._seed_subscribed_usage()
        first = dm.settle_billing(cycle)
        assert first["total_calls"] == 15 and first["total_amount"] == 150.0

        self._add_unsubscribed_usage()
        second = dm.settle_billing(cycle)

        assert len(second["vouchers"]) == 2
        assert second["vouchers"][0]["settlement_id"] == first["vouchers"][0]["settlement_id"]
        assert second["vouchers"][0]["total_calls"] == 15
        assert second["vouchers"][1]["total_calls"] == 3
        assert second["vouchers"][1]["total_amount"] == 30.0
        assert second["total_calls"] == 18 and second["total_amount"] == 180.0
        # 两张凭据各自可查
        first_billed = dm.list_usage_by_settlement(second["vouchers"][0]["settlement_id"], cycle)
        second_billed = dm.list_usage_by_settlement(second["vouchers"][1]["settlement_id"], cycle)
        assert sorted(r["usage_id"] for r in first_billed) == [7, 8]
        assert [r["usage_id"] for r in second_billed] == [9]

    def test_cycle_endpoint_returns_persisted_originals_and_settlement(self):
        """账期视图接口：再次打开账期返回持久化的原单和账期结算明细。"""
        from app.routers.resource import get_billing_cycle
        from app.services import data_manager as dm

        cycle = self._seed_subscribed_usage()
        self._add_unsubscribed_usage()
        dm.settle_billing(cycle)

        detail = get_billing_cycle("usage_billing", cycle)
        assert detail["billing_cycle"] == cycle
        assert sorted(u["usage_id"] for u in detail["usages"]) == [7, 8, 9]
        assert detail["settlement"]["status"] == "已确认"
        assert detail["confirmed_calls"] == 18 and detail["confirmed_amount"] == 180.0
        assert detail["consistent"] is True
