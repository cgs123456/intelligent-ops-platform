"""闭环编排器测试 — 状态机、步骤执行、回滚、路由、重置"""

import pytest


@pytest.mark.usefixtures("db_session")
class TestClosedLoop:
    """ClosedLoop 状态机测试：run_id 推进、步骤执行、回滚校验、路由逻辑、重置归档"""

    @pytest.fixture(autouse=True)
    def _cleanup_loop_state(self, app):
        """每个测试前清理 LoopState 表，避免跨测试数据污染。

        db_session fixture 的 transaction rollback 在生产代码调用 commit() 后失效，
        故需在每个测试前显式清理上一轮残留的 LoopState 记录。
        """
        from extensions import db
        from models.system import LoopState

        with app.app_context():
            LoopState.query.delete()
            db.session.commit()
        yield

    # ==================== 1. 初始 run_id ====================

    def test_get_current_run_id_initial(self, app):
        """无任何 LoopState 记录时，get_current_run_id 返回 1"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            assert ClosedLoop.get_current_run_id() == 1

    # ==================== 2. 空状态查询 ====================

    def test_get_status_all_pending(self, app):
        """无记录时 get_status 返回所有步骤 pending，current_step=1"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            status = ClosedLoop.get_status()
            assert status["run_id"] == 1
            assert status["current_step"] == 1
            assert len(status["steps"]) == 5
            for s in status["steps"]:
                assert s["status"] == "pending"

    # ==================== 3. 步骤2 人工审核 ====================

    def test_run_step2_returns_need_manual(self, app, disable_timeout):
        """执行步骤2（人工审核）返回 need_manual=True"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            result = ClosedLoop.run_step(2, actor="tester")
            assert result.get("step") == 2
            assert result.get("need_manual") is True
            assert "message" in result

    # ==================== 4. 已完成步骤不可重复执行 ====================

    def test_step_done_blocks_rerun(self, app, disable_timeout):
        """步骤状态为 done 时再次执行返回"已完成"错误"""
        from datetime import datetime

        from extensions import db
        from models.system import LoopState
        from services.closed_loop import ClosedLoop

        with app.app_context():
            # 预置一条 done 状态的步骤2记录
            run_id = ClosedLoop.get_current_run_id()
            rec = LoopState(run_id=run_id, step=2, step_name="人工审核", status="done")
            rec.started_at = datetime.now()
            rec.finished_at = datetime.now()
            db.session.add(rec)
            db.session.commit()

            result = ClosedLoop.run_step(2, actor="tester")
            assert "error" in result
            assert "已完成" in result["error"]
            assert result["step"] == 2

    # ==================== 5. 路由：无建议跳过审核 ====================

    def test_route_after_step1_skip(self, app):
        """_route_after_step1：suggestions 为空时返回 'skip'"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            state = {"suggestions": [], "detail": ""}
            assert ClosedLoop._route_after_step1(state) == "skip"

            # detail 含"无需补货"也应跳过
            state_with_detail = {"suggestions": [{"id": 1}], "detail": "当前无需补货（所有SKU库存充足）"}
            assert ClosedLoop._route_after_step1(state_with_detail) == "skip"

    # ==================== 6. 路由：有建议进入审核 ====================

    def test_route_after_step1_review(self, app):
        """_route_after_step1：suggestions 非空且 detail 无"无需补货"时返回 'review'"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            state = {"suggestions": [{"id": 1, "product_name": "测试商品"}], "detail": "生成 1 条补货建议"}
            assert ClosedLoop._route_after_step1(state) == "review"

    # ==================== 7. 回滚无记录步骤 ====================

    def test_rollback_no_record_returns_error(self, app):
        """回滚不存在 LoopState 记录的步骤，返回"步骤无记录"错误"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            result = ClosedLoop.rollback_step(3, actor="tester")
            assert "error" in result
            assert "无记录" in result["error"]

    # ==================== 8. reset 归档当前轮次 ====================

    def test_reset_archives_current_run(self, app):
        """reset 将当前 run 的所有 LoopState 记录标记为 archived"""
        from datetime import datetime

        from extensions import db
        from models.system import LoopState
        from services.closed_loop import ClosedLoop

        with app.app_context():
            # 预置当前 run 的 2 条记录
            run_id = ClosedLoop.get_current_run_id()
            for step_no in (1, 2):
                rec = LoopState(run_id=run_id, step=step_no, step_name=f"步骤{step_no}", status="done")
                rec.started_at = datetime.now()
                rec.finished_at = datetime.now()
                db.session.add(rec)
            db.session.commit()

            msg = ClosedLoop.reset(actor="tester")
            assert "重置" in msg

            # 验证所有记录已归档
            records = LoopState.query.filter_by(run_id=run_id).all()
            assert len(records) == 2
            for r in records:
                assert r.status == "archived"
                assert "归档" in (r.detail or "")

            # 下次 run_id 应递增
            assert ClosedLoop.get_current_run_id() == run_id + 1


@pytest.mark.usefixtures("db_session")
class TestStep3RpaOrder:
    """_step3_rpa_order 下单 + 重试逻辑测试"""

    @pytest.fixture(autouse=True)
    def _cleanup_loop_state(self, app):
        """每个测试前清理 LoopState 和 Suggestion 表"""
        from extensions import db
        from models.aigc import Suggestion
        from models.system import LoopState

        with app.app_context():
            LoopState.query.delete()
            Suggestion.query.delete()
            db.session.commit()
        yield

    def test_step3_no_approved_suggestions(self, app):
        """无已审核建议时返回跳过信息"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            result = ClosedLoop._step3_rpa_order(actor="tester")
            assert "无已审核建议" in result
            assert "跳过下单" in result

    def test_step3_order_success(self, app, monkeypatch):
        """已审核建议下单成功：suggestion 状态升级为 ordered"""
        from extensions import db
        from models.aigc import Suggestion
        from services.closed_loop import ClosedLoop

        # 避免 time.sleep 阻塞测试
        monkeypatch.setattr("time.sleep", lambda *a, **kw: None)

        with app.app_context():
            # 创建已审核建议
            sug = Suggestion(
                product_id=1,
                product_name="测试商品",
                suggested_qty=10,
                status="approved",
            )
            db.session.add(sug)
            db.session.commit()
            sug_id = sug.id

            # mock RPAService
            class FakeRPA:
                def place_supplier_order(self, suggestion):
                    return {
                        "status": "ok",
                        "supplier": "测试供应商",
                        "ext_order_no": "EXT-001",
                    }

            # mock _services 返回 FakeRPA
            # ClosedLoop._services will be patched below

            @staticmethod
            def fake_services():
                return (None, FakeRPA, None, None)

            monkeypatch.setattr(ClosedLoop, "_services", fake_services)

            result = ClosedLoop._step3_rpa_order(actor="tester")
            assert "下单" in result
            assert "测试商品" in result
            assert "测试供应商" in result
            # suggestion 状态升级为 ordered
            sug_db = db.session.get(Suggestion, sug_id)
            assert sug_db.status == "ordered"

    def test_step3_order_retry_on_fail(self, app, monkeypatch):
        """下单失败时重试 3 次：前 2 次失败，第 3 次成功"""
        from extensions import db
        from models.aigc import Suggestion
        from services.closed_loop import ClosedLoop

        monkeypatch.setattr("time.sleep", lambda *a, **kw: None)

        with app.app_context():
            sug = Suggestion(
                product_id=1,
                product_name="重试商品",
                suggested_qty=5,
                status="approved",
            )
            db.session.add(sug)
            db.session.commit()

            call_count = [0]

            class FakeRPA:
                def place_supplier_order(self, suggestion):
                    call_count[0] += 1
                    if call_count[0] < 3:
                        return {"status": "fail", "reason": "供应商系统繁忙"}
                    return {"status": "ok", "supplier": "备选供应商", "ext_order_no": "EXT-002"}

            @staticmethod
            def fake_services():
                return (None, FakeRPA, None, None)

            monkeypatch.setattr(ClosedLoop, "_services", fake_services)

            result = ClosedLoop._step3_rpa_order(actor="tester")
            assert "备选供应商" in result
            assert call_count[0] == 3  # 重试 3 次后成功

    def test_step3_order_all_retries_failed(self, app, monkeypatch):
        """下单 3 次均失败：返回失败信息"""
        from extensions import db
        from models.aigc import Suggestion
        from services.closed_loop import ClosedLoop

        monkeypatch.setattr("time.sleep", lambda *a, **kw: None)

        with app.app_context():
            sug = Suggestion(
                product_id=1,
                product_name="失败商品",
                suggested_qty=5,
                status="approved",
            )
            db.session.add(sug)
            db.session.commit()

            call_count = [0]

            class FakeRPA:
                def place_supplier_order(self, suggestion):
                    call_count[0] += 1
                    return {"status": "fail", "reason": "供应商拒绝"}

            @staticmethod
            def fake_services():
                return (None, FakeRPA, None, None)

            monkeypatch.setattr(ClosedLoop, "_services", fake_services)

            result = ClosedLoop._step3_rpa_order(actor="tester")
            assert "失败" in result
            assert call_count[0] == 3  # 共尝试 3 次

    def test_step3_order_exception_retry(self, app, monkeypatch):
        """下单抛异常时重试 3 次"""
        from extensions import db
        from models.aigc import Suggestion
        from services.closed_loop import ClosedLoop

        monkeypatch.setattr("time.sleep", lambda *a, **kw: None)

        with app.app_context():
            sug = Suggestion(
                product_id=1,
                product_name="异常商品",
                suggested_qty=5,
                status="approved",
            )
            db.session.add(sug)
            db.session.commit()

            call_count = [0]

            class FakeRPA:
                def place_supplier_order(self, suggestion):
                    call_count[0] += 1
                    raise RuntimeError("网络异常")

            @staticmethod
            def fake_services():
                return (None, FakeRPA, None, None)

            monkeypatch.setattr(ClosedLoop, "_services", fake_services)

            result = ClosedLoop._step3_rpa_order(actor="tester")
            assert "异常" in result
            assert call_count[0] == 3

    def test_step3_order_exception_then_success(self, app, monkeypatch):
        """下单前 2 次抛异常，第 3 次成功"""
        from extensions import db
        from models.aigc import Suggestion
        from services.closed_loop import ClosedLoop

        monkeypatch.setattr("time.sleep", lambda *a, **kw: None)

        with app.app_context():
            sug = Suggestion(
                product_id=1,
                product_name="恢复商品",
                suggested_qty=8,
                status="approved",
            )
            db.session.add(sug)
            db.session.commit()

            call_count = [0]

            class FakeRPA:
                def place_supplier_order(self, suggestion):
                    call_count[0] += 1
                    if call_count[0] < 3:
                        raise RuntimeError("暂时故障")
                    return {"status": "ok", "supplier": "恢复供应商", "ext_order_no": "EXT-003"}

            @staticmethod
            def fake_services():
                return (None, FakeRPA, None, None)

            monkeypatch.setattr(ClosedLoop, "_services", fake_services)

            result = ClosedLoop._step3_rpa_order(actor="tester")
            assert "恢复供应商" in result
            assert call_count[0] == 3


@pytest.mark.usefixtures("db_session")
class TestCheckAutoTrigger:
    """check_auto_trigger 自动触发逻辑测试"""

    @pytest.fixture(autouse=True)
    def _cleanup_loop_state(self, app):
        from extensions import db
        from models.system import LoopState

        with app.app_context():
            LoopState.query.delete()
            db.session.commit()
        yield

    def test_check_auto_trigger_disabled(self, app):
        """LOOP_AUTO_TRIGGER 未启用时返回 triggered=False"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            app.config["LOOP_AUTO_TRIGGER"] = False
            result = ClosedLoop.check_auto_trigger()
            assert result["triggered"] is False
            assert "未启用" in result["reason"]

    def test_check_auto_trigger_loop_not_finished(self, app):
        """LOOP_AUTO_TRIGGER 启用但当前轮次未完成时返回 triggered=False"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            app.config["LOOP_AUTO_TRIGGER"] = True
            # 默认状态：所有步骤 pending，current_step=1，未完成
            result = ClosedLoop.check_auto_trigger()
            assert result["triggered"] is False
            assert "未完成" in result["reason"]

    def test_check_auto_trigger_no_low_stock(self, app, monkeypatch):
        """当前轮次已完成但无低库存时返回 triggered=False

        注：get_current_run_id 在所有步骤 done 时会自动递增到新轮次，
        所以需要 mock get_status 返回"所有步骤 done"的状态来测试低库存检查分支。
        临时调整所有低库存产品的 stock_qty 以确保无低库存。
        """
        from services.closed_loop import ClosedLoop

        with app.app_context():
            app.config["LOOP_AUTO_TRIGGER"] = True

            # mock get_status 返回所有步骤 done 的状态
            done_steps = [
                {"step": i, "name": f"步骤{i}", "owner": "X", "status": "done", "detail": "", "time": "-"}
                for i in range(1, 6)
            ]
            monkeypatch.setattr(
                ClosedLoop,
                "get_status",
                staticmethod(lambda: {"run_id": 1, "current_step": 5, "steps": done_steps}),
            )

            # 临时调整所有低库存产品的 stock_qty，确保无低库存
            from extensions import db
            from models.erp import Product

            low_products = Product.query.filter(Product.stock_qty < Product.safety_stock).all()
            original_qtys = [(p, p.stock_qty) for p in low_products]
            for p in low_products:
                p.stock_qty = p.safety_stock  # 临时提升到安全库存
            db.session.commit()

            try:
                result = ClosedLoop.check_auto_trigger()
                assert result["triggered"] is False
                assert "无低库存" in result["reason"]
            finally:
                # 恢复原始 stock_qty
                for p, original_qty in original_qtys:
                    p.stock_qty = original_qty
                db.session.commit()

    def test_check_auto_trigger_with_low_stock(self, app, monkeypatch):
        """当前轮次已完成且存在低库存时返回 triggered=True

        注：同上，需要 mock get_status 返回"所有步骤 done"的状态。
        """
        from extensions import db
        from models.erp import Product
        from services.closed_loop import ClosedLoop

        with app.app_context():
            app.config["LOOP_AUTO_TRIGGER"] = True

            # mock get_status 返回所有步骤 done 的状态
            done_steps = [
                {"step": i, "name": f"步骤{i}", "owner": "X", "status": "done", "detail": "", "time": "-"}
                for i in range(1, 6)
            ]
            monkeypatch.setattr(
                ClosedLoop,
                "get_status",
                staticmethod(lambda: {"run_id": 1, "current_step": 5, "steps": done_steps}),
            )

            # 创建低库存产品（stock_qty < safety_stock）
            low_product = Product(
                sku="TEST-LOWSTOCK-001",
                name="低库存品",
                cost_price=10,
                sale_price=20,
                stock_qty=5,
                safety_stock=50,
                avg_cost=10,
            )
            db.session.add(low_product)
            db.session.commit()

            try:
                result = ClosedLoop.check_auto_trigger()
                assert result["triggered"] is True
                assert "低库存" in result["reason"]
                assert result["low_count"] >= 1
            finally:
                # 清理测试数据
                db.session.delete(low_product)
                db.session.commit()


@pytest.mark.usefixtures("db_session")
class TestRouteAfterStep3:
    """_route_after_step3 路由逻辑测试"""

    def test_route_after_step3_retry(self, app):
        """有错误且重试次数 < 2 时返回 retry"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            state = {"errors": ["下单失败"], "retry_count": 0}
            assert ClosedLoop._route_after_step3(state) == "retry"

            state = {"errors": ["下单失败"], "retry_count": 1}
            assert ClosedLoop._route_after_step3(state) == "retry"

    def test_route_after_step3_continue_no_errors(self, app):
        """无错误时返回 continue"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            state = {"errors": [], "retry_count": 0}
            assert ClosedLoop._route_after_step3(state) == "continue"

    def test_route_after_step3_continue_max_retry(self, app):
        """重试次数达上限时返回 continue"""
        from services.closed_loop import ClosedLoop

        with app.app_context():
            state = {"errors": ["仍失败"], "retry_count": 2}
            assert ClosedLoop._route_after_step3(state) == "continue"

