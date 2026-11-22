"""订阅费用归属明细场景测试。

业务场景：同一团队两份有效期重叠的订阅（SUB-A / SUB-B）。
- 先用一笔已经明确归属的 8 元调用（10 月，仅 SUB-A 有效）出账；
- 再加入一笔 12 元的调用（11 月，两份订阅重叠，只有调用方信息）→ 待核；
- 分别在封账前和封账后补入订阅凭据，核对两份账单及仍待核的金额；
- 任何时点：归属金额 + 待核余额 = 原调用金额，两账单合计始终是 20 元；
- 并发补录凭据只能确认一次；进程重启后仍能沿调用编号查封账依据。

运行方式：
    python3 tests/test_usage_attribution.py        # 无 pytest 时直接运行
    pytest tests/test_usage_attribution.py         # 有 pytest 时
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

TEAM = "team-1"
SUB_A, SUB_B = "SUB-A", "SUB-B"
CALL_8, CALL_12 = "CALL-8", "CALL-12"


class TestUsageAttribution:
    """用量计量 × 订阅管理：订阅费用归属明细场景"""

    def setup_method(self):
        from app.seed import SEED_ROWS
        from app.store import store
        from app.services import data_manager

        store._tables = {name: [dict(row) for row in rows]
                         for name, rows in SEED_ROWS.items()}
        self._data_dir = tempfile.mkdtemp(prefix="attribution_test_")
        self._old_root = data_manager.DATA_ROOT
        data_manager.DATA_ROOT = Path(self._data_dir)

    def teardown_method(self):
        from app.services import data_manager
        data_manager.DATA_ROOT = self._old_root
        shutil.rmtree(self._data_dir, ignore_errors=True)

    # ---------- 场景准备 ----------

    def _services(self):
        from app.services import service_for
        return service_for("usage_billing"), service_for("subscription")

    def _setup_team(self):
        """两份有效期重叠的订阅 + 8 元明确归属先出账 + 12 元待核调用。"""
        usage_svc, sub_svc = self._services()
        sub_svc.register_effective_subscription(
            subscription_id=SUB_A, team_id=TEAM, caller="alice", api_name="gpt-x",
            valid_from="2026-10-01T00:00:00", valid_to="2026-11-30T23:59:59")
        sub_svc.register_effective_subscription(
            subscription_id=SUB_B, team_id=TEAM, caller="alice", api_name="gpt-x",
            valid_from="2026-11-01T00:00:00", valid_to="2026-12-31T23:59:59")

        # 8 元：10 月调用，仅 SUB-A 有效 → 即时核定
        rec8 = usage_svc.record_call_detail(
            call_id=CALL_8, team_id=TEAM, caller="alice", api_name="gpt-x",
            amount_cents=800, called_at="2026-10-15T10:00:00")
        assert rec8["status"] == "CONFIRMED" and rec8["subscription_id"] == SUB_A
        # 先出账：10 月账期封账
        sub_svc.close_billing_period(team_id=TEAM, month="2026-10",
                                     closed_at="2026-10-31T23:59:59")

        # 12 元：11 月调用，两份订阅重叠，只有调用方信息 → 待核
        rec12 = usage_svc.record_call_detail(
            call_id=CALL_12, team_id=TEAM, caller="alice", api_name="gpt-x",
            amount_cents=1200, called_at="2026-11-10T10:00:00")
        assert rec12["status"] == "PENDING" and rec12["pending_cents"] == 1200
        return usage_svc, sub_svc

    def _assert_statement(self, sub_svc, *, a_billed, b_billed, pending):
        """核对两份账单及待核金额，合计始终是 20 元（2000 分）。"""
        st = sub_svc.team_statement(team_id=TEAM)
        assert st["billed_by_subscription"].get(SUB_A, 0) == a_billed, st
        assert st["billed_by_subscription"].get(SUB_B, 0) == b_billed, st
        assert st["pending_cents"] == pending, st
        assert st["calls_cents"] == 2000, st
        # 已出账 + 已核定未出账 + 待核 ≡ 调用总额
        assert st["billed_cents"] + st["unbilled_cents"] + st["pending_cents"] == 2000, st
        assert st["balanced"] is True, st
        return st

    # ---------- 场景 0：8 元明确归属先出账 ----------

    def test_clear_call_billed_first(self):
        usage_svc, sub_svc = self._setup_team()

        # 8 元已出账到 SUB-A 的 10 月账期
        bill10 = sub_svc.period_bill(team_id=TEAM, month="2026-10")
        assert bill10["status"] == "CLOSED"
        assert bill10["by_subscription"] == {SUB_A: 800}

        # 12 元仍待核；任何时点 归属 + 待核 = 原调用金额
        assert usage_svc.verify_call_balance(CALL_8)
        assert usage_svc.verify_call_balance(CALL_12)
        self._assert_statement(sub_svc, a_billed=800, b_billed=0, pending=1200)

        # 旧的「按调用方、API、月份累计」仍然成立
        from app.store import store
        cycles = {(r.get("调用方"), r.get("API名称"), r.get("计费周期")): r.get("费用金额")
                  for r in store.rows("usage_billing") if r.get("调用方") == "alice"}
        assert cycles[("alice", "gpt-x", "2026-10")] == 8.0
        assert cycles[("alice", "gpt-x", "2026-11")] == 12.0

    # ---------- 场景 1：封账前补入凭据 ----------

    def test_evidence_before_closing(self):
        usage_svc, sub_svc = self._setup_team()

        # 月末封账前，凭据交来：12 元核定到 SUB-B
        result = usage_svc.submit_evidence(
            call_id=CALL_12, subscription_id=SUB_B,
            evidence_no="EV-001", submitted_at="2026-11-25T09:00:00")
        assert result["outcome"] == "CONFIRMED"
        assert result["billing_entry"] is None  # 账期未封，暂不补记
        assert usage_svc.verify_call_balance(CALL_12)
        self._assert_statement(sub_svc, a_billed=800, b_billed=0, pending=0)

        # 同一调用重复补录只确认一次
        dup = usage_svc.submit_evidence(
            call_id=CALL_12, subscription_id=SUB_A,
            evidence_no="EV-002", submitted_at="2026-11-26T09:00:00")
        assert dup["outcome"] == "DUPLICATE"
        self._assert_statement(sub_svc, a_billed=800, b_billed=0, pending=0)

        # 封账：12 元按已核定金额形成 SUB-B 的账期明细
        sub_svc.close_billing_period(team_id=TEAM, month="2026-11",
                                     closed_at="2026-11-30T23:59:59")
        bill11 = sub_svc.period_bill(team_id=TEAM, month="2026-11")
        assert bill11["by_subscription"] == {SUB_B: 1200}
        assert bill11["entries"][0]["entry_type"] == "CHARGE"
        self._assert_statement(sub_svc, a_billed=800, b_billed=1200, pending=0)

        # 重复封账被拒绝
        try:
            sub_svc.close_billing_period(team_id=TEAM, month="2026-11")
            raise AssertionError("重复封账应报错")
        except ValueError:
            pass

        # 重新出具归属明细：CALL-12 归属 SUB-B，在 11 月账期出账
        report = usage_svc.attribution_report(team_id=TEAM, month="2026-11")
        row = report["rows"][0]
        assert row["subscription_id"] == SUB_B and row["pending_cents"] == 0
        assert row["billing"][0]["period"] == "2026-11"

    # ---------- 场景 2：封账后补入凭据 ----------

    def test_evidence_after_closing(self):
        usage_svc, sub_svc = self._setup_team()

        # 12 元凭据未到，11 月先封账：待核不出账，留下封账快照
        sub_svc.close_billing_period(team_id=TEAM, month="2026-11",
                                     closed_at="2026-11-30T23:59:59")
        bill11 = sub_svc.period_bill(team_id=TEAM, month="2026-11")
        assert bill11["total_cents"] == 0
        self._assert_statement(sub_svc, a_billed=800, b_billed=0, pending=1200)

        # 封账后凭据交来：核定到 SUB-B，补记到下一个未封账账期
        result = usage_svc.submit_evidence(
            call_id=CALL_12, subscription_id=SUB_B,
            evidence_no="EV-101", submitted_at="2026-12-02T09:00:00")
        assert result["outcome"] == "CONFIRMED"
        entry = result["billing_entry"]
        assert entry["entry_type"] == "POST_CLOSE_CONFIRMATION"
        assert entry["original_period"] == "2026-11"
        self._assert_statement(sub_svc, a_billed=800, b_billed=1200, pending=0)

        # 补记落在 12 月账期，指向原账期 11 月
        bill12 = sub_svc.period_bill(team_id=TEAM, month="2026-12")
        assert bill12["by_subscription"] == {SUB_B: 1200}

        # 财务依原单复核：重新出具 11 月归属明细，两份账单各受多大影响
        report = usage_svc.attribution_report(team_id=TEAM, month="2026-11")
        row = report["rows"][0]
        assert row["call_id"] == CALL_12 and row["subscription_id"] == SUB_B
        assert row["billing"][0]["period"] == "2026-12"          # 影响 SUB-B 的 12 月账单
        assert row["billing"][0]["original_period"] == "2026-11"  # 原账期 11 月
        assert report["pending_cents"] == 0

        # 沿调用编号回溯：封账依据 + 待核余额变化 + 补记记录
        trail = usage_svc.call_trail(CALL_12)
        event_types = [e["event_type"] for e in trail["call"]["events"]]
        assert event_types == ["CALL_RECORDED", "CLOSE_SNAPSHOT",
                               "CONFIRMED", "BILLED_POST_CLOSE"]
        snapshot = trail["call"]["events"][1]
        assert snapshot["pending_cents"] == 1200  # 封账时仍待核（封账依据）
        confirmed = trail["call"]["events"][2]
        assert confirmed["pending_cents"] == 0    # 确认后待核余额清零
        assert trail["billing_entries"][0]["entry_type"] == "POST_CLOSE_CONFIRMATION"

    # ---------- 场景 3：进程重启后仍可回溯 ----------

    def test_restart_keeps_trail(self):
        usage_svc, sub_svc = self._setup_team()
        sub_svc.close_billing_period(team_id=TEAM, month="2026-11",
                                     closed_at="2026-11-30T23:59:59")
        usage_svc.submit_evidence(call_id=CALL_12, subscription_id=SUB_B,
                                  evidence_no="EV-101", submitted_at="2026-12-02T09:00:00")

        # 模拟进程重启：丢掉内存中的服务对象，重建实例
        from app.services.usage_billing import UsageBillingService
        from app.services.subscription import SubscriptionService
        usage_svc2, sub_svc2 = UsageBillingService(), SubscriptionService()
        trail = usage_svc2.call_trail(CALL_12)
        assert [e["event_type"] for e in trail["call"]["events"]] == [
            "CALL_RECORDED", "CLOSE_SNAPSHOT", "CONFIRMED", "BILLED_POST_CLOSE"]
        self._assert_statement(sub_svc2, a_billed=800, b_billed=1200, pending=0)

        # 真正的另起进程：只凭磁盘文件沿调用编号查
        backend_dir = os.path.join(os.path.dirname(__file__), '..')
        code = (
            "import json;"
            "from app.services import service_for;"
            "trail = service_for('usage_billing').call_trail('CALL-12');"
            "st = service_for('subscription').team_statement(team_id='team-1');"
            "print(json.dumps({'trail': trail, 'statement': st}, ensure_ascii=False))"
        )
        env = dict(os.environ, DATA_ROOT=self._data_dir,
                   PYTHONPATH=os.path.abspath(backend_dir))
        proc = subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert [e["event_type"] for e in payload["trail"]["call"]["events"]] == [
            "CALL_RECORDED", "CLOSE_SNAPSHOT", "CONFIRMED", "BILLED_POST_CLOSE"]
        assert payload["statement"]["balanced"] is True
        assert payload["statement"]["billed_cents"] == 2000
        assert payload["statement"]["pending_cents"] == 0

    # ---------- 场景 4：并发补录凭据只确认一次 ----------

    def test_concurrent_evidence_confirms_once(self):
        usage_svc, sub_svc = self._setup_team()

        def submit(i):
            return usage_svc.submit_evidence(
                call_id=CALL_12,
                subscription_id=SUB_A if i % 2 else SUB_B,
                evidence_no=f"EV-C{i:02d}",
                submitted_at="2026-11-25T09:00:00")

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(submit, range(8)))

        assert sum(1 for o in outcomes if o["outcome"] == "CONFIRMED") == 1
        assert sum(1 for o in outcomes if o["outcome"] == "DUPLICATE") == 7

        # 只确认一次：归属金额没有翻倍，待核清零，合计仍是 20 元
        rec = usage_svc.attribution_of(CALL_12)
        assert rec["confirmed_cents"] == 1200 and rec["pending_cents"] == 0
        events = [e["event_type"] for e in rec["events"]]
        assert events.count("CONFIRMED") == 1
        assert events.count("EVIDENCE_REJECTED") == 7
        winner = rec["subscription_id"]
        st = self._assert_statement(sub_svc, a_billed=800, b_billed=0, pending=0)
        assert st["unbilled_by_subscription"] == {winner: 1200}

        # 封账后也只出账一次
        sub_svc.close_billing_period(team_id=TEAM, month="2026-11")
        bill11 = sub_svc.period_bill(team_id=TEAM, month="2026-11")
        assert bill11["by_subscription"] == {winner: 1200}

    # ---------- 场景 5：无效凭据不能承接费用 ----------

    def test_invalid_evidence_rejected(self):
        usage_svc, sub_svc = self._setup_team()
        # 调用发生时尚未生效的订阅
        sub_svc.register_effective_subscription(
            subscription_id="SUB-C", team_id=TEAM, caller="alice", api_name="gpt-x",
            valid_from="2026-12-01T00:00:00", valid_to="2026-12-31T23:59:59")
        # 匹配调用方但 API 不同的订阅
        sub_svc.register_effective_subscription(
            subscription_id="SUB-D", team_id=TEAM, caller="alice", api_name="other-api",
            valid_from="2026-10-01T00:00:00", valid_to="2026-12-31T23:59:59")

        for sub_id in ("SUB-C", "SUB-D", "SUB-GHOST"):
            result = usage_svc.submit_evidence(
                call_id=CALL_12, subscription_id=sub_id,
                evidence_no=f"EV-{sub_id}", submitted_at="2026-11-25T09:00:00")
            assert result["outcome"] == "REJECTED", (sub_id, result)

        # 待核金额不受无效凭据影响
        rec = usage_svc.attribution_of(CALL_12)
        assert rec["status"] == "PENDING" and rec["pending_cents"] == 1200
        self._assert_statement(sub_svc, a_billed=800, b_billed=0, pending=1200)


if __name__ == "__main__":
    import traceback

    instance = TestUsageAttribution()
    failures = 0
    names = sorted(n for n in dir(instance) if n.startswith("test_"))
    for name in names:
        instance.setup_method()
        try:
            getattr(instance, name)()
            print(f"PASS {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        finally:
            instance.teardown_method()
    print(f"\n{len(names) - failures}/{len(names)} passed")
    sys.exit(1 if failures else 0)
