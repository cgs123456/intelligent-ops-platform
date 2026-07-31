"""AIGCService 测试 — 建议生成、日报、Text2SQL、语义检索、反馈学习

覆盖：
- 补货建议生成 generate_suggestions（含 LLM 润色 + 规则兜底）
- 置信度计算 _calc_confidence
- 经营日报生成 generate_daily_report（规则模板 + 幂等）
- Text2SQL text2sql + _validate_sql（白名单 + 写操作拦截 + LIMIT 自动补全）
- 语义检索 semantic_search（关键词匹配 + 评分）
- 自然语言查询 natural_language_query（路由 semantic/text2sql）
- 审核反馈 record_feedback + get_feedback_stats
- 建议审核 review_suggestion（approve/reject + 数量修改）
- 待审核列表 get_pending_suggestions
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from extensions import db
from models.aigc import ChatHistory, DailyReport, Suggestion, SuggestionFeedback
from models.erp import Product
from models.warehouse import AdsDailyOpsReport, AdsReplenishmentSuggest
from services.aigc_service import AIGCService


@pytest.fixture(autouse=True)
def _cleanup_aigc_tables(app):
    """每个测试前后清理 AIGC + 依赖表"""
    models_to_clean = [
        ChatHistory,
        DailyReport,
        SuggestionFeedback,
        Suggestion,
        AdsReplenishmentSuggest,
        AdsDailyOpsReport,
        Product,
    ]
    with app.app_context():
        for m in models_to_clean:
            db.session.query(m).delete()
        db.session.commit()
    yield
    with app.app_context():
        for m in models_to_clean:
            db.session.query(m).delete()
        db.session.commit()


@pytest.fixture
def seed_ads_suggestions(app):
    """创建 ADS 补货建议数据（suggested_qty > 0）供 generate_suggestions 读取"""
    with app.app_context():
        # 创建产品（generate_suggestions 不直接依赖 Product，但 reason 引用 product_name）
        p = Product(
            sku="AIGC-001",
            name="AIGC测试商品",
            cost_price=10,
            sale_price=20,
            stock_qty=5,
            safety_stock=20,
            is_active=True,
        )
        db.session.add(p)
        db.session.flush()

        # ADS 补货建议（suggested_qty > 0 才会被 generate_suggestions 选中）
        ads = AdsReplenishmentSuggest(
            product_id=p.id,
            product_name="AIGC测试商品",
            recent_7d_sales=50,
            current_stock=5,
            in_transit=10,
            suggested_qty=30,
            suggested_supplier_id=None,
            suggested_supplier_name="测试供应商",
            reason="近7日销量50件，当前库存5件，建议补货30件。",
            dt=date.today(),
        )
        db.session.add(ads)
        db.session.commit()
        return {"product_id": p.id, "ads_id": ads.id}


class TestGenerateSuggestions:
    """补货建议生成测试"""

    def test_generate_suggestions_rule_engine(self, app, seed_ads_suggestions):
        """规则引擎模式（LLM 不可用）：直接用 ADS reason"""
        with app.app_context():
            svc = AIGCService()
            result = svc.generate_suggestions()
            assert len(result) == 1
            s = result[0]
            assert s.status == "pending"
            assert s.suggested_qty == 30
            assert s.original_qty == 30
            assert s.product_name == "AIGC测试商品"
            # 置信度基于库存缺口比例计算
            assert 0.5 <= float(s.confidence) <= 0.99

    def test_generate_suggestions_with_llm(self, app, seed_ads_suggestions, mock_llm):
        """LLM 模式：reason 被 LLM 响应覆盖"""
        with app.app_context():
            svc = AIGCService()
            result = svc.generate_suggestions()
            assert len(result) == 1
            s = result[0]
            # mock_llm 返回固定字符串
            assert s.reason == "这是 LLM 测试响应"

    def test_generate_suggestions_empty_ads(self, app):
        """ADS 无 suggested_qty>0 的数据时返回空列表"""
        with app.app_context():
            svc = AIGCService()
            result = svc.generate_suggestions()
            assert result == []

    def test_generate_suggestions_idempotent_multiple_calls(self, app, seed_ads_suggestions):
        """重复调用 generate_suggestions 每次都创建新 Suggestion（无去重）"""
        with app.app_context():
            svc = AIGCService()
            svc.generate_suggestions()
            svc.generate_suggestions()
            assert Suggestion.query.count() == 2


class TestCalcConfidence:
    """置信度计算测试"""

    def test_confidence_no_shortage(self, app):
        """库存充足时 confidence=0.5（最低）"""
        with app.app_context():
            svc = AIGCService()
            conf = svc._calc_confidence(recent_7d_sales=100, current_stock=200, in_transit=0)
            assert conf == 0.5

    def test_confidence_full_shortage(self, app):
        """完全缺货时 confidence 接近 0.99"""
        with app.app_context():
            svc = AIGCService()
            conf = svc._calc_confidence(recent_7d_sales=100, current_stock=0, in_transit=0)
            # ratio = 100/100 = 1.0, conf = 0.5 + 1.0*0.49 = 0.99
            assert conf == 0.99

    def test_confidence_zero_sales(self, app):
        """近 7 天无销量时 confidence=0.5"""
        with app.app_context():
            svc = AIGCService()
            conf = svc._calc_confidence(recent_7d_sales=0, current_stock=10, in_transit=0)
            assert conf == 0.5

    def test_confidence_clamped_to_range(self, app):
        """置信度始终在 [0.5, 0.99] 区间"""
        with app.app_context():
            svc = AIGCService()
            for sales in [0, 10, 50, 100, 1000]:
                for stock in [0, 10, 50, 100, 1000]:
                    for transit in [0, 10, 100]:
                        conf = svc._calc_confidence(sales, stock, transit)
                        assert 0.5 <= conf <= 0.99


class TestGenerateDailyReport:
    """经营日报生成测试"""

    @pytest.fixture
    def seed_ads_report(self, app):
        with app.app_context():
            # 写 7 天 ADS 日报数据
            today = date.today()
            for i in range(7):
                dt = today - timedelta(days=6 - i)
                db.session.add(
                    AdsDailyOpsReport(
                        dt=dt,
                        total_sales_amount=1000 + i * 100,
                        total_purchase_amount=800 + i * 50,
                        total_sale_qty=50 + i,
                        inventory_value=5000,
                        top_sku="畅销品A",
                        low_stock_count=2,
                    )
                )
            db.session.commit()
            return today

    def test_generate_daily_report_rule_template(self, app, seed_ads_report):
        """规则模板日报（LLM 不可用）：4 段式【昨日回顾】【趋势分析】【风险提示】【建议行动】"""
        with app.app_context():
            svc = AIGCService()
            report = svc.generate_daily_report(dt=seed_ads_report)
            assert report is not None
            text = report.report_text
            assert "【昨日回顾】" in text
            assert "【趋势分析】" in text
            assert "【风险提示】" in text
            assert "【建议行动】" in text

    def test_generate_daily_report_with_llm(self, app, seed_ads_report, mock_llm):
        """LLM 模式：使用 LLM 返回的文本"""
        with app.app_context():
            svc = AIGCService()
            report = svc.generate_daily_report(dt=seed_ads_report)
            assert report is not None
            assert report.report_text == "这是 LLM 测试响应"

    def test_generate_daily_report_idempotent(self, app, seed_ads_report):
        """同一天重复生成覆盖旧日报（幂等）"""
        with app.app_context():
            svc = AIGCService()
            svc.generate_daily_report(dt=seed_ads_report)
            svc.generate_daily_report(dt=seed_ads_report)
            count = DailyReport.query.filter_by(dt=seed_ads_report).count()
            assert count == 1

    def test_generate_daily_report_no_data(self, app):
        """ADS 无日报数据时返回 None"""
        with app.app_context():
            svc = AIGCService()
            result = svc.generate_daily_report(dt=date.today())
            assert result is None

    def test_generate_daily_report_accepts_string_date(self, app, seed_ads_report):
        """支持字符串日期格式"""
        with app.app_context():
            svc = AIGCService()
            report = svc.generate_daily_report(dt=seed_ads_report.strftime("%Y-%m-%d"))
            assert report is not None
            assert report.dt == seed_ads_report


class TestText2Sql:
    """Text2SQL 测试"""

    def test_text2sql_daily_report(self, app):
        """问日报 → 匹配 ads_daily_ops_report"""
        with app.app_context():
            svc = AIGCService()
            result = svc.text2sql("近7天销售总额日报")
            assert "ads_daily_ops_report" in result["sql"]
            assert isinstance(result["result"], list)

    def test_text2sql_replenishment(self, app):
        """问补货 → 匹配 ads_replenishment_suggest"""
        with app.app_context():
            svc = AIGCService()
            result = svc.text2sql("哪些产品需要补货")
            assert "ads_replenishment_suggest" in result["sql"]

    def test_text2sql_unknown_intent(self, app):
        """未识别意图 → 返回未匹配提示"""
        with app.app_context():
            svc = AIGCService()
            result = svc.text2sql("今天天气怎么样")
            assert "未匹配" in result["sql"] or "未识别" in result["explanation"]
            assert result["result"] == []

    def test_validate_sql_select_passes(self, app):
        """合法 SELECT 通过校验"""
        with app.app_context():
            svc = AIGCService()
            result = svc._validate_sql("SELECT * FROM ads_daily_ops_report LIMIT 10")
            assert result is not None

    def test_validate_sql_insert_blocked(self, app):
        """INSERT 被拒绝"""
        with app.app_context():
            svc = AIGCService()
            result = svc._validate_sql("INSERT INTO ads_daily_ops_report VALUES (1)")
            assert result is None

    def test_validate_sql_auto_adds_limit(self, app):
        """无 LIMIT 时自动追加 LIMIT 100"""
        with app.app_context():
            svc = AIGCService()
            result = svc._validate_sql("SELECT * FROM ads_daily_ops_report")
            assert result is not None
            assert "LIMIT 100" in result.upper()

    def test_validate_sql_word_boundary(self, app):
        """列名含 update 关键字不误判（word boundary）"""
        with app.app_context():
            svc = AIGCService()
            # 'updated_at' 不应被误判为 UPDATE 关键字
            result = svc._validate_sql("SELECT updated_at FROM ads_daily_ops_report LIMIT 10")
            # 注意：ads_daily_ops_report 表没有 updated_at 列，但校验只看 SQL 语法不看表结构
            # 由于 'updated_at' 包含 'update' 但 \bupdate\b 不匹配 'updated_at'
            assert result is not None


class TestSemanticSearch:
    """语义检索测试"""

    def test_semantic_search_returns_results(self, app):
        """关键词命中返回相关文档"""
        with app.app_context():
            svc = AIGCService()
            results = svc.semantic_search("ERP 产品表结构字段说明", top_k=3)
            assert len(results) > 0
            # ERP 产品主数据表结构 应排前列
            top = results[0]
            assert "产品" in top["title"] or "ERP" in top["title"]
            assert top["score"] > 0

    def test_semantic_search_empty_query(self, app):
        """空查询返回空列表"""
        with app.app_context():
            svc = AIGCService()
            results = svc.semantic_search("", top_k=3)
            assert results == []

    def test_semantic_search_top_k_limit(self, app):
        """top_k 限制返回数量"""
        with app.app_context():
            svc = AIGCService()
            results = svc.semantic_search("ERP 流程 SOP 说明 怎么用", top_k=2)
            assert len(results) <= 2

    def test_semantic_search_no_keyword_match(self, app):
        """无领域关键词命中时仅靠字符重叠产生极低分（< 0.1），无有意义语义匹配"""
        with app.app_context():
            svc = AIGCService()
            results = svc.semantic_search("xyzqwerty", top_k=3)
            # 实现含字符级 Jaccard 兜底，纯 ASCII 串可能与英文术语有少量字符重叠，
            # 但分数极低（< 0.1），表示无领域关键词命中
            for r in results:
                assert r["score"] < 0.1


class TestNaturalLanguageQuery:
    """自然语言查询测试"""

    def test_nlq_routes_to_semantic(self, app):
        """含'表结构/字段/怎么用'等关键词路由到 semantic"""
        with app.app_context():
            svc = AIGCService()
            result = svc.natural_language_query("ERP 产品表结构字段是什么", session_id="test-sem-1")
            assert result["route"] == "semantic"
            assert "session_id" in result
            assert "answer" in result

    def test_nlq_routes_to_text2sql(self, app):
        """含'销量/补货/日报'等关键词路由到 text2sql"""
        with app.app_context():
            svc = AIGCService()
            result = svc.natural_language_query("近7天销售总额", session_id="test-sql-1")
            assert result["route"] == "text2sql"

    def test_nlq_records_chat_history(self, app):
        """对话历史写入 ChatHistory"""
        with app.app_context():
            svc = AIGCService()
            sid = "test-history-1"
            svc.natural_language_query("近7天销量排行", session_id=sid)
            history = ChatHistory.query.filter_by(session_id=sid).all()
            # user 问 + assistant 答 = 2 条
            assert len(history) >= 2
            roles = {h.role for h in history}
            assert "user" in roles
            assert "assistant" in roles

    def test_nlq_multiturn_with_session(self, app):
        """多轮对话：同 session_id 第二次查询包含历史"""
        with app.app_context():
            svc = AIGCService()
            sid = "test-multi-1"
            svc.natural_language_query("近7天销量", session_id=sid)
            svc.natural_language_query("补货建议", session_id=sid)
            # 2 轮 × 2 条 = 4 条
            assert ChatHistory.query.filter_by(session_id=sid).count() >= 4


class TestFeedbackLearning:
    """审核反馈学习测试"""

    @pytest.fixture
    def seed_suggestion(self, app):
        with app.app_context():
            s = Suggestion(
                product_id=1,
                product_name="反馈测试商品",
                suggested_qty=30,
                original_qty=30,
                reason="测试建议",
                confidence=0.85,
                status="pending",
            )
            db.session.add(s)
            db.session.commit()
            return s.id

    def test_record_feedback_approved(self, app, seed_suggestion):
        """记录 approved 反馈：qty_delta = final - original"""
        with app.app_context():
            svc = AIGCService()
            fb = svc.record_feedback(seed_suggestion, "approved", original_qty=30, final_qty=25, note="减量")
            assert fb.action == "approved"
            assert fb.qty_delta == -5
            assert fb.note == "减量"

    def test_record_feedback_rejected(self, app, seed_suggestion):
        """记录 rejected 反馈：final_qty=0, qty_delta=-original"""
        with app.app_context():
            svc = AIGCService()
            fb = svc.record_feedback(seed_suggestion, "rejected", original_qty=30, final_qty=0, note="价格高")
            assert fb.action == "rejected"
            assert fb.qty_delta == -30

    def test_get_feedback_stats_empty(self, app):
        """无反馈数据时统计为 0"""
        with app.app_context():
            svc = AIGCService()
            stats = svc.get_feedback_stats()
            assert stats["total"] == 0
            assert stats["approval_rate"] == 0.0

    def test_get_feedback_stats_with_data(self, app, seed_suggestion):
        """有反馈数据时统计正确"""
        with app.app_context():
            svc = AIGCService()
            svc.record_feedback(seed_suggestion, "approved", 30, 30, note="")
            svc.record_feedback(seed_suggestion, "rejected", 30, 0, note="价格过高")
            # 再创建一条 suggestion + 反馈（approved 减量）
            s2 = Suggestion(product_id=2, product_name="商品2", suggested_qty=20, original_qty=20, status="pending")
            db.session.add(s2)
            db.session.commit()
            svc.record_feedback(s2.id, "approved", 20, 15, note="减量")

            stats = svc.get_feedback_stats()
            assert stats["total"] == 3
            assert stats["approved"] == 2
            assert stats["rejected"] == 1
            assert stats["approval_rate"] == round(2 / 3, 2)
            # qty_delta = final - original：approved#1=30-30=0，approved#2=15-20=-5
            # 平均修改量 = (0 + -5) / 2 = -2.5
            assert stats["avg_modification"] == -2.5


class TestReviewSuggestion:
    """建议审核测试"""

    @pytest.fixture
    def seed_pending_suggestion(self, app):
        with app.app_context():
            s = Suggestion(
                product_id=1,
                product_name="审核测试商品",
                suggested_qty=30,
                original_qty=30,
                reason="待审核",
                confidence=0.8,
                status="pending",
            )
            db.session.add(s)
            db.session.commit()
            return s.id

    def test_review_approve_default_qty(self, app, seed_pending_suggestion):
        """approve 不传 final_qty 时用 suggested_qty"""
        with app.app_context():
            svc = AIGCService()
            s = svc.review_suggestion(seed_pending_suggestion, action="approve", reviewer="admin")
            assert s.status == "approved"
            assert s.reviewed_by == "admin"

    def test_review_approve_with_modified_qty(self, app, seed_pending_suggestion):
        """approve 修改数量：review_note 记录调整"""
        with app.app_context():
            svc = AIGCService()
            s = svc.review_suggestion(
                seed_pending_suggestion, action="approve", final_qty=20, note="减量", reviewer="admin"
            )
            assert s.status == "approved"
            assert "数量调整 30→20" in (s.review_note or "")

    def test_review_reject(self, app, seed_pending_suggestion):
        """reject 后 status=rejected，final_qty=0"""
        with app.app_context():
            svc = AIGCService()
            s = svc.review_suggestion(seed_pending_suggestion, action="reject", note="价格高", reviewer="admin")
            assert s.status == "rejected"

    def test_review_already_processed_raises(self, app, seed_pending_suggestion):
        """重复审核已处理建议抛 ValueError"""
        with app.app_context():
            svc = AIGCService()
            svc.review_suggestion(seed_pending_suggestion, action="approve", reviewer="admin")
            with pytest.raises(ValueError, match="建议已处理"):
                svc.review_suggestion(seed_pending_suggestion, action="reject", reviewer="admin")

    def test_review_invalid_action_raises(self, app, seed_pending_suggestion):
        """非法 action 抛 ValueError"""
        with app.app_context():
            svc = AIGCService()
            with pytest.raises(ValueError, match="action 必须为"):
                svc.review_suggestion(seed_pending_suggestion, action="invalid", reviewer="admin")

    def test_review_not_found_raises(self, app):
        """建议不存在抛 ValueError"""
        with app.app_context():
            svc = AIGCService()
            with pytest.raises(ValueError, match="建议不存在"):
                svc.review_suggestion(99999, action="approve")


class TestGetPendingSuggestions:
    """待审核建议列表测试"""

    def test_get_pending_empty(self, app):
        """无待审核建议返回空列表"""
        with app.app_context():
            svc = AIGCService()
            result = svc.get_pending_suggestions()
            assert result == []

    def test_get_pending_returns_only_pending(self, app):
        """只返回 status=pending 的建议"""
        with app.app_context():
            svc = AIGCService()
            s1 = Suggestion(product_id=1, product_name="待审1", suggested_qty=10, original_qty=10, status="pending")
            s2 = Suggestion(product_id=2, product_name="已审", suggested_qty=20, original_qty=20, status="approved")
            db.session.add_all([s1, s2])
            db.session.commit()

            result = svc.get_pending_suggestions()
            assert len(result) == 1
            assert result[0]["product_name"] == "待审1"
            assert result[0]["status"] == "pending"

    def test_get_pending_fields_complete(self, app):
        """返回字段完整"""
        with app.app_context():
            svc = AIGCService()
            s = Suggestion(
                product_id=1,
                product_name="字段测试",
                suggested_qty=15,
                original_qty=15,
                reason="测试",
                confidence=0.9,
                status="pending",
            )
            db.session.add(s)
            db.session.commit()

            result = svc.get_pending_suggestions()
            item = result[0]
            for field in (
                "id",
                "product_id",
                "product_name",
                "suggested_qty",
                "original_qty",
                "reason",
                "status",
                "confidence",
                "created_at",
            ):
                assert field in item
