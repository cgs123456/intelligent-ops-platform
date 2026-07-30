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
