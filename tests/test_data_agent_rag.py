"""P3-RAG 测试：问答对沉淀 + few-shot 检索。"""
import uuid

import pytest

from extensions import db as _db
from models.aigc import ChatHistory
from services.data_agent import DataAgent


def _uid8():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def app_context(app):
    with app.app_context():
        yield


@pytest.fixture
def cleanup(app_context):
    yield
    _db.session.query(ChatHistory).delete()
    _db.session.commit()


def _seed_qa(pairs):
    """pairs: [(question, sql, days_ago)]"""
    from datetime import datetime, timedelta

    for q, sql, days_ago in pairs:
        _db.session.add(
            ChatHistory(
                session_id=f"s-{_uid8()}",
                role="assistant",
                content="回答",
                question=q,
                sql=sql,
                created_at=datetime(2026, 10, 25) - timedelta(days=days_ago),
            )
        )
    _db.session.commit()


class TestRetrieveSimilarQa:
    def test_cold_start_returns_empty(self, app_context, cleanup):
        agent = DataAgent()
        assert agent._retrieve_similar_qa("今天销量多少") == []

    def test_ranks_by_2gram_jaccard(self, app_context, cleanup):
        _seed_qa(
            [
                ("昨天库存周转率是多少", "SELECT ... LIMIT 10", 5),
                ("昨天各仓库库存是多少", "SELECT ... LIMIT 20", 3),
                ("本月预算使用情况", "SELECT ... LIMIT 30", 1),
            ]
        )
        agent = DataAgent()
        hits = agent._retrieve_similar_qa("昨天库存是多少")
        assert len(hits) >= 2
        # 与"昨天各仓库库存是多少"最相似（共享 昨天/库存/是多少）
        assert hits[0]["question"] == "昨天各仓库库存是多少"

    def test_no_overlap_excluded(self, app_context, cleanup):
        _seed_qa([("供应商报价情况", "SELECT ... LIMIT 10", 2)])
        agent = DataAgent()
        assert agent._retrieve_similar_qa("昨天销量趋势") == []


class TestRagPersistOnSuccess:
    def test_query_persists_sql_only_on_llm_success(self, app_context, cleanup, monkeypatch):
        """规则回退路径不沉淀；LLM 成功路径沉淀 question+sql。"""
        agent = DataAgent()

        # 场景 1：LLM 不可用 → 规则回退 → 不沉淀 sql
        monkeypatch.setattr(agent._aigc, "_llm_available", lambda: False)
        res = agent.query("昨天库存是多少")
        assert res["route"] == "text2sql"
        rows = ChatHistory.query.filter_by(role="assistant").all()
        assert rows and all(r.sql is None for r in rows)

        _db.session.query(ChatHistory).delete()
        _db.session.commit()

        # 场景 2：LLM 可用且生成合法 SQL → 真执行成功 → 沉淀
        monkeypatch.setattr(agent._aigc, "_llm_available", lambda: True)
        monkeypatch.setattr(
            agent._aigc,
            "_call_llm",
            lambda prompt, temperature=0.1: "SELECT product_name, current_stock FROM ads_replenishment_suggest ORDER BY current_stock LIMIT 5",
            raising=True,
        )
        # _result_to_nl 也会调 _call_llm —— 同一 stub 返回 SQL 文本作为 NL 无妨
        res2 = agent.query("补货建议前5")
        assert res2["rows_count"] >= 0
        persisted = ChatHistory.query.filter(ChatHistory.sql.isnot(None)).all()
        assert len(persisted) == 1
        assert persisted[0].question == "补货建议前5"
        assert persisted[0].sql.startswith("SELECT")

        # 场景 3：沉淀后可被检索（闭环验证）
        hits = agent._retrieve_similar_qa("补货建议")
        assert hits and hits[0]["sql"].startswith("SELECT")
