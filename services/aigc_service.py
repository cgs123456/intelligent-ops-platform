"""
AIGC 服务层
===========
封装 LLM 抲象、补货建议生成、经营日报、Text2SQL、向量检索模拟、
自然语言查询、审核反馈自学习等能力，为智能运营平台提供 AI 助手内核。

设计要点：
- LLM 可切换：rule(规则引擎兜底) / glm / qwen，配了 LLM_API_KEY 自动启用真实 LLM，
  任何调用失败均 fallback 到规则引擎并记日志，保证 Demo 永远可用。
- Text2SQL 走白名单 + SQL 校验，Demo 不真正执行 SQL，按规则匹配直接查对应 ORM。
- 向量检索用关键词重叠度模拟语义匹配，避免引入向量库依赖。
- 所有写操作有显式事务边界（commit / rollback）。
"""
import re
import uuid
import logging
import requests
from datetime import datetime, date
from collections import Counter

from flask import current_app
from extensions import db
from models.aigc import Suggestion, DailyReport, ChatHistory, SuggestionFeedback
from models.warehouse import AdsReplenishmentSuggest, AdsDailyOpsReport

logger = logging.getLogger(__name__)


class AIGCService:
    """AIGC 服务：LLM 抽象 + 补货建议 + 日报 + Text2SQL + 语义检索 + 对话 + 反馈学习"""

    # Text2SQL 白名单表（含中文注释，作为 LLM schema context）
    WHITELIST_TABLES = {
        'ads_daily_ops_report': (
            'ads_daily_ops_report 经营日报表 | 字段：'
            'dt(日期), total_sales_amount(销售总额), total_purchase_amount(采购总额), '
            'total_sale_qty(销售件数), inventory_value(库存货值), top_sku(畅销SKU), '
            'low_stock_count(低库存SKU数)'
        ),
        'ads_replenishment_suggest': (
            'ads_replenishment_suggest 补货建议表 | 字段：'
            'product_id(产品ID), product_name(产品名), recent_7d_sales(近7天销量), '
            'current_stock(当前库存), in_transit(在途量), suggested_qty(建议补货量), '
            'suggested_supplier_name(建议供应商), dt(日期)'
        ),
        'dws_sales_sku_daily': (
            'dws_sales_sku_daily SKU日销售汇总 | 字段：'
            'product_id(产品ID), product_name(产品名), dt(日期), '
            'sale_qty(销量), sale_amount(销售额)'
        ),
    }

    # 模拟向量库的文档片段（ERP字段说明 / SOP / 财务科目）
    DOC_LIBRARY = [
        {
            'id': 'erp_product',
            'title': 'ERP 产品主数据表结构',
            'content': (
                'erp_product 表存储 SKU 主数据。字段：sku(编码,唯一), name(名称), '
                'cost_price(成本价), sale_price(售价), stock_qty(当前库存), '
                'safety_stock(安全库存), avg_cost(移动加权平均成本), category(品类), '
                'is_active(是否启用)。补货建议基于 stock_qty 与 safety_stock 比对生成。'
            ),
            'tags': ['表结构', '字段', 'ERP', '产品', '库存', '说明'],
        },
        {
            'id': 'erp_supplier',
            'title': 'ERP 供应商主数据表结构',
            'content': (
                'erp_supplier 表存储供应商主数据。字段：name(名称), lead_days(交货周期,天), '
                'rating(评级 A/B/C), contact(联系人), phone(电话), is_active(是否启用)。'
                'RPA 采集报价时按 rating 优先级排序，A 级供应商优先询价。'
            ),
            'tags': ['表结构', '字段', 'ERP', '供应商', '说明', '流程'],
        },
        {
            'id': 'erp_purchase_order',
            'title': 'ERP 采购单与入库流程 SOP',
            'content': (
                '采购流程 SOP：AIGC 生成补货建议 → 人工审核 → 生成 erp_purchase_order '
                '(status: draft/confirmed/received/cancelled) → 入库 erp_stock_move(move_type=in) '
                '→ 生成应付凭证 erp_account_move(ref_type=payable)。'
            ),
            'tags': ['SOP', '流程', '采购', '入库', '凭证', '怎么用'],
        },
        {
            'id': 'erp_sale_order',
            'title': 'ERP 销售单与出库流程 SOP',
            'content': (
                '销售流程 SOP：创建 erp_sale_order → 出库 erp_stock_move(move_type=out) '
                '→ 生成应收凭证 erp_account_move(ref_type=receivable)。退货走 erp_return_order，'
                '生成红字销售单 + 入库 + 红字应收凭证。'
            ),
            'tags': ['SOP', '流程', '销售', '出库', '退货', '凭证', '怎么用'],
        },
        {
            'id': 'finance_account',
            'title': '财务会计科目说明',
            'content': (
                '财务科目：应付账款(payable, 2202)、应收账款(receivable, 1122)、'
                '库存商品(1405)、主营业务收入(6001)、主营业务成本(6401)、销售费用(6601)。'
                '采购入库借记库存商品贷记应付；销售出库借记主营业务成本贷记库存商品。'
            ),
            'tags': ['财务', '科目', '凭证', '说明', '怎么用'],
        },
        {
            'id': 'warehouse_layer',
            'title': '数仓分层与 ADS 应用层说明',
            'content': (
                '数仓分 ODS(贴源)/DWD(明细)/DWS(汇总)/ADS(应用) 四层。'
                'ads_daily_ops_report 为经营日报，ads_replenishment_suggest 为补货建议，'
                'dws_sales_sku_daily 为 SKU 日销售汇总。Text2SQL 仅允许查询这三张 ADS/DWS 表。'
            ),
            'tags': ['表结构', '字段', '流程', '说明', '数仓', '怎么用'],
        },
        {
            'id': 'aigc_feedback',
            'title': 'AIGC 补货建议审核与反馈自学习',
            'content': (
                'AIGC 补货建议审核流程：生成 Suggestion(status=pending) → 人工 review '
                '(approved/rejected) → 自动记录 SuggestionFeedback(qty_delta=final-original)。'
                '反馈统计(批准率/平均修改量/拒绝原因)作为后续 LLM prompt 的 few-shot 示例，'
                '持续优化建议质量。'
            ),
            'tags': ['SOP', '流程', 'AIGC', '补货', '审核', '反馈', '怎么用'],
        },
    ]

    def __init__(self, db_session=None):
        """可选注入 db_session，默认用 extensions.db.session"""
        self._db_session = db_session
        self._llm_backend = None  # P1-1: 延迟初始化

    @property
    def session(self):
        """当前使用的事务会话"""
        return self._db_session or db.session

    # ==================== LLM 抽象层 ====================

    def _get_llm_backend(self):
        """P1-1: 延迟获取 LLM 后端实例（通过 adapters 抽象）"""
        if self._llm_backend is None:
            try:
                from adapters import get_llm_backend
                self._llm_backend = get_llm_backend()
            except Exception as e:
                logger.warning('LLM backend 初始化失败，回退 rule：%s', e)
                from adapters.llm_backend import RuleLLMBackend
                self._llm_backend = RuleLLMBackend()
        return self._llm_backend

    def _call_llm(self, messages, temperature=0.3):
        """LLM 统一调用入口（委托给 backend）。
        - 任何异常：记日志并返回 None，保证规则引擎兜底。
        """
        try:
            return self._get_llm_backend().call(messages, temperature)
        except Exception as e:
            logger.warning('LLM 调用异常：%s，回退规则引擎', e)
            return None

    def _llm_available(self):
        """是否启用了真实 LLM（供调用方决定是否拼 prompt）"""
        return self._get_llm_backend().available()

    # ==================== 补货建议生成 ====================

    def generate_suggestions(self):
        """
        读 ADS 层 ads_replenishment_suggest，对 suggested_qty>0 的产品生成 Suggestion。
        - 置信度 confidence 基于库存缺口比例计算。
        - reason 可选用 LLM 润色，并把历史反馈统计作为 few-shot。
        """
        rows = (
            self.session.query(AdsReplenishmentSuggest)
            .filter(AdsReplenishmentSuggest.suggested_qty > 0)
            .all()
        )
        if not rows:
            logger.info('无待生成的补货建议（ADS 层 suggested_qty>0 为空）')
            return []

        use_llm = self._llm_available()
        few_shot = ''
        if use_llm:
            try:
                stats = self.get_feedback_stats()
                few_shot = (
                    f"历史反馈参考：总建议{stats['total']}条，批准率{stats['approval_rate']}，"
                    f"平均修改量{stats['avg_modification']}，"
                    f"常见拒绝原因{stats['common_rejection_reasons']}。"
                )
            except Exception as e:
                logger.debug('获取反馈统计失败，few-shot 省略: %s', e)

        created = []
        try:
            for r in rows:
                confidence = self._calc_confidence(
                    r.recent_7d_sales, r.current_stock, r.in_transit
                )
                reason = r.reason or ''

                if use_llm:
                    polished = self._call_llm(
                        [
                            {
                                'role': 'system',
                                'content': (
                                    '你是供应链补货助手，把补货原因改写为简洁、专业的中文说明，'
                                    '突出库存缺口与时效风险，不超过80字。'
                                ),
                            },
                            {
                                'role': 'user',
                                'content': (
                                    f"{few_shot}产品：{r.product_name}，近7天销量："
                                    f"{r.recent_7d_sales}，当前库存：{r.current_stock}，"
                                    f"在途：{r.in_transit}，建议补货：{r.suggested_qty}。"
                                    f"原始原因：{reason}"
                                ),
                            },
                        ],
                        temperature=0.3,
                    )
                    if polished:
                        reason = polished.strip()

                s = Suggestion(
                    product_id=r.product_id,
                    product_name=r.product_name,
                    suggested_supplier_id=r.suggested_supplier_id,
                    suggested_supplier_name=r.suggested_supplier_name,
                    suggested_qty=r.suggested_qty,
                    original_qty=r.suggested_qty,
                    reason=reason,
                    confidence=confidence,
                    status='pending',
                )
                self.session.add(s)
                created.append(s)
            self.session.commit()
            logger.info('生成补货建议 %d 条', len(created))
            return created
        except Exception as e:
            self.session.rollback()
            logger.error('生成补货建议失败: %s', e)
            raise

    def _calc_confidence(self, recent_7d_sales, current_stock, in_transit):
        """
        基于库存缺口比例计算置信度（0.50~0.99）。
        shortage = max(0, 近7天销量 - (当前库存 + 在途))
        ratio = shortage / max(近7天销量, 1)
        confidence = clamp(0.5 + ratio*0.49, 0.5, 0.99)
        """
        demand = max(recent_7d_sales or 0, 0)
        available = (current_stock or 0) + (in_transit or 0)
        shortage = max(0, demand - available)
        ratio = shortage / demand if demand > 0 else 0
        conf = 0.5 + ratio * 0.49
        conf = max(0.5, min(0.99, conf))
        return round(conf, 2)

    # ==================== 经营日报生成 ====================

    def generate_daily_report(self, dt=None):
        """
        读 ADS 日报数据生成自然语言日报。
        - 优先 LLM 生成，不可用走模板。
        - 幂等：同天覆盖。
        - SQLite Date 字段只接受 date 对象，字符串日期需转换。
        """
        if dt is None:
            dt = date.today()
        elif isinstance(dt, str):
            dt = datetime.strptime(dt, '%Y-%m-%d').date()
        elif isinstance(dt, datetime):
            dt = dt.date()

        report = (
            self.session.query(AdsDailyOpsReport)
            .filter(AdsDailyOpsReport.dt == dt)
            .first()
        )
        if not report:
            logger.warning('ADS 日报数据不存在 dt=%s', dt)
            return None

        context = self._build_report_context(report, dt)
        text = None

        if self._llm_available():
            text = self._call_llm(
                [
                    {
                        'role': 'system',
                        'content': (
                            '你是经营分析师，根据结构化数据生成简洁的中文经营日报，'
                            '包含整体表现、亮点、风险提示三段，不超过200字。'
                        ),
                    },
                    {'role': 'user', 'content': context},
                ],
                temperature=0.4,
            )

        if not text:
            text = self._template_daily_report(report, dt)

        try:
            existing = (
                self.session.query(DailyReport).filter(DailyReport.dt == dt).first()
            )
            if existing:
                existing.report_text = text
                existing.created_at = datetime.now()
            else:
                existing = DailyReport(dt=dt, report_text=text)
                self.session.add(existing)
            self.session.commit()
            return existing
        except Exception as e:
            self.session.rollback()
            logger.error('保存日报失败 dt=%s: %s', dt, e)
            raise

    def _build_report_context(self, report, dt):
        return (
            f"日期：{dt}\n"
            f"销售总额：{report.total_sales_amount}\n"
            f"采购总额：{report.total_purchase_amount}\n"
            f"销售总件数：{report.total_sale_qty}\n"
            f"库存货值：{report.inventory_value}\n"
            f"畅销SKU：{report.top_sku}\n"
            f"低库存SKU数：{report.low_stock_count}"
        )

    def _template_daily_report(self, report, dt):
        """规则引擎模板日报"""
        sales = float(report.total_sales_amount or 0)
        purchase = float(report.total_purchase_amount or 0)
        profit = sales - purchase
        low = report.low_stock_count or 0
        top = report.top_sku or '无'
        trend = '盈利' if profit >= 0 else '亏损'
        risk = f'存在 {low} 个低库存SKU，建议及时补货' if low > 0 else '库存健康'
        return (
            f"【经营日报 {dt}】\n"
            f"1. 整体表现：销售总额 ¥{sales:,.2f}，采购总额 ¥{purchase:,.2f}，"
            f"毛差 ¥{profit:,.2f}（{trend}）；销售件数 {report.total_sale_qty or 0}。"
            f"库存货值 ¥{float(report.inventory_value or 0):,.2f}。\n"
            f"2. 亮点：畅销SKU为 {top}。\n"
            f"3. 风险提示：{risk}。"
        )

    # ==================== Text2SQL 引擎 ====================

    def text2sql(self, question):
        """
        自然语言 → SQL → 结果。
        - 白名单表：仅 ads_daily_ops_report / ads_replenishment_suggest / dws_sales_sku_daily 可查。
        - LLM 生成 SQL 后校验：只允许 SELECT、必须有 LIMIT、禁止写操作与子查询写操作。
        - Demo 用规则匹配模拟，不真执行 SQL，直接查对应 ORM。
        - 返回 {sql, result, explanation}
        """
        schema = '\n'.join(self.WHITELIST_TABLES.values())
        sql = None
        explanation = ''

        if self._llm_available():
            llm_sql = self._call_llm(
                [
                    {
                        'role': 'system',
                        'content': (
                            '你是 Text2SQL 引擎。仅可查询以下白名单表：\n'
                            f'{schema}\n'
                            '规则：只生成 SELECT 语句；必须包含 LIMIT；'
                            '禁止 INSERT/UPDATE/DELETE/DROP/ALTER/CREATE 及任何写操作子查询；'
                            '只返回一条 SQL，不要解释。'
                        ),
                    },
                    {'role': 'user', 'content': question},
                ],
                temperature=0.1,
            )
            if llm_sql:
                validated = self._validate_sql(llm_sql)
                if validated:
                    sql = validated
                    explanation = 'LLM 生成 SQL（已校验，Demo 不执行，改走规则查 ADS）'

        # Demo 规则匹配查 ORM（无论 LLM 是否产出 SQL，结果都来自规则查表）
        rule = self._rule_query_ads(question)
        if not sql:
            sql = rule['sql']
            explanation = rule['explanation']

        return {'sql': sql, 'result': rule['result'], 'explanation': explanation}

    def _validate_sql(self, sql):
        """
        SQL 安全校验：
        - 必须以 SELECT/WITH 开头
        - 禁止写操作关键字
        - 必须有 LIMIT，没有则补 LIMIT 100
        """
        if not sql:
            return None
        s = sql.strip().strip('`').rstrip(';').strip()
        low = s.lower()
        if not (low.startswith('select') or low.startswith('with')):
            logger.warning('SQL 校验失败：非 SELECT/WITH 开头')
            return None
        forbid = ('insert', 'update', 'delete', 'drop', 'alter', 'create',
                  'truncate', 'replace', 'grant', 'revoke', 'merge')
        for kw in forbid:
            if kw in low:
                logger.warning('SQL 校验失败：包含禁止关键字 %s', kw)
                return None
        # 简单子查询写操作检测
        if re.search(r'\(\s*(insert|update|delete|drop|alter|create)\s', low):
            return None
        if 'limit' not in low:
            s = s + ' LIMIT 100'
        return s

    def _rule_query_ads(self, question):
        """规则匹配模拟 SQL 执行，直接查对应 ORM。"""
        from models.warehouse import DwsSalesSkuDaily

        q = question or ''

        # 经营日报类
        if any(k in q for k in ('日报', '经营', '销售总额', '销售额', '低库存', '采购总额')):
            rows = (
                self.session.query(AdsDailyOpsReport)
                .order_by(AdsDailyOpsReport.dt.desc())
                .limit(7)
                .all()
            )
            result = [
                {
                    'dt': str(r.dt),
                    'total_sales_amount': float(r.total_sales_amount or 0),
                    'total_purchase_amount': float(r.total_purchase_amount or 0),
                    'total_sale_qty': r.total_sale_qty or 0,
                    'low_stock_count': r.low_stock_count or 0,
                    'top_sku': r.top_sku,
                }
                for r in rows
            ]
            return {
                'sql': 'SELECT dt, total_sales_amount, total_purchase_amount, '
                       'total_sale_qty, low_stock_count, top_sku '
                       'FROM ads_daily_ops_report ORDER BY dt DESC LIMIT 7',
                'result': result,
                'explanation': '查询近7天经营日报（规则匹配 ads_daily_ops_report）',
            }

        # 补货建议类
        if any(k in q for k in ('补货', '建议补货', '缺货', '断货')):
            rows = (
                self.session.query(AdsReplenishmentSuggest)
                .filter(AdsReplenishmentSuggest.suggested_qty > 0)
                .order_by(AdsReplenishmentSuggest.suggested_qty.desc())
                .limit(20)
                .all()
            )
            result = [
                {
                    'product_name': r.product_name,
                    'suggested_qty': r.suggested_qty,
                    'current_stock': r.current_stock,
                    'suggested_supplier_name': r.suggested_supplier_name,
                }
                for r in rows
            ]
            return {
                'sql': 'SELECT product_name, suggested_qty, current_stock, '
                       'suggested_supplier_name FROM ads_replenishment_suggest '
                       'WHERE suggested_qty > 0 ORDER BY suggested_qty DESC LIMIT 20',
                'result': result,
                'explanation': '查询补货建议（规则匹配 ads_replenishment_suggest）',
            }

        # SKU 销量排行类
        if any(k in q for k in ('销量', '排行', '排名', 'top', '畅销', '销售额排行')):
            rows = (
                self.session.query(DwsSalesSkuDaily)
                .order_by(DwsSalesSkuDaily.sale_qty.desc())
                .limit(10)
                .all()
            )
            result = [
                {
                    'product_name': r.product_name,
                    'sale_qty': r.sale_qty,
                    'sale_amount': float(r.sale_amount or 0),
                    'dt': str(r.dt),
                }
                for r in rows
            ]
            return {
                'sql': 'SELECT product_name, sale_qty, sale_amount, dt '
                       'FROM dws_sales_sku_daily ORDER BY sale_qty DESC LIMIT 10',
                'result': result,
                'explanation': '查询SKU销量排行（规则匹配 dws_sales_sku_daily）',
            }

        return {
            'sql': '-- 未匹配到可查询意图',
            'result': [],
            'explanation': '未识别查询意图，请尝试询问日报/补货/销量排行等',
        }

    # ==================== 向量检索模拟 ====================

    def semantic_search(self, query, top_k=3):
        """
        模拟向量检索：用关键词重叠度做语义匹配排序，返回 top_k 文档片段。
        """
        if not query:
            return []
        scored = []
        for doc in self.DOC_LIBRARY:
            score = self._score_doc(query, doc)
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, doc in scored[:top_k]:
            results.append({
                'id': doc['id'],
                'title': doc['title'],
                'content': doc['content'],
                'score': round(score, 3),
            })
        return results

    def _score_doc(self, query, doc):
        """TF-IDF 风格评分：tag 命中高权重 + 词频/逆文档频率 + 字符重叠补充。
        相比纯关键词重叠，TF-IDF 对常见词降权，对区分性强的词升权。
        """
        doc_text = (doc['title'] + ' ' + doc['content'] + ' ' + ' '.join(doc['tags']))
        doc_lower = doc_text.lower()
        score = 0.0
        # tag 关键词短语命中（最高权重）
        for tag in doc['tags']:
            if tag and tag in query:
                score += 2.0
        # 领域关键词命中 + TF-IDF 权重（低频词权重更高）
        domain_kws = {
            '表结构': 1.5, '字段': 1.2, '流程': 1.0, '怎么用': 1.3, '说明': 0.8,
            '是什么': 0.8, 'SOP': 1.5, 'ERP': 1.0, '供应商': 1.0, '产品': 0.6,
            '补货': 1.2, '日报': 1.2, '销售': 0.8, '库存': 0.8, '财务': 1.0,
            '科目': 1.2, '凭证': 1.2, '退货': 1.3, '采购': 0.8, '入库': 0.8,
            '出库': 0.8, '数仓': 1.3, '审核': 1.0, '反馈': 1.0, 'AIGC': 1.3,
        }
        for kw, idf in domain_kws.items():
            if kw in query and kw.lower() in doc_lower:
                # TF: query 中出现次数 / IDF: 领域区分度
                tf = query.count(kw)
                score += tf * idf
        # 字符级 Jaccard 相似度（补充中文无分词不足）
        q_chars = set(query)
        d_chars = set(doc_lower)
        intersection = len(q_chars & d_chars)
        union = len(q_chars | d_chars)
        if union > 0:
            score += (intersection / union) * 0.5
        return score

    # ==================== 自然语言查询（双路检索） ====================

    def natural_language_query(self, question, session_id=None):
        """
        自然语言查询：路由器判断走语义检索还是 Text2SQL。
        - 含"表结构/字段/怎么用/流程/是什么/SOP"等 → semantic_search
        - 含数字/多少/统计/排行/销量/日报/补货/库存 → text2sql + 规则查 ADS
        - 记录到 ChatHistory（user 问 + assistant 答）
        - 传入 session_id 时取最近5条历史拼入 context，支持多轮
        """
        sid = session_id or str(uuid.uuid4())
        history_ctx = ''
        if session_id:
            recent = (
                self.session.query(ChatHistory)
                .filter(ChatHistory.session_id == session_id)
                .order_by(ChatHistory.created_at.desc())
                .limit(5)
                .all()
            )
            recent.reverse()
            if recent:
                # P1-3: 对话历史 token 截断 — 控制单条消息长度 + 总长度
                # 避免 LLM 上下文窗口溢出（glm-4-flash 上下文 128K，但应保守截断）
                MAX_MSG_CHARS = 500  # 单条消息最多保留 500 字
                MAX_TOTAL_CHARS = 2000  # 历史总长度上限 2000 字
                truncated = []
                total = 0
                for m in reversed(recent):  # 从最近的开始
                    content = (m.content or '')[:MAX_MSG_CHARS]
                    if total + len(content) > MAX_TOTAL_CHARS:
                        # 已超限，截断当前消息
                        remain = MAX_TOTAL_CHARS - total
                        if remain > 50:  # 至少留 50 字才有意义
                            truncated.insert(0, f'{m.role}: {content[:remain]}...(截断)')
                        break
                    truncated.insert(0, f'{m.role}: {content}')
                    total += len(content)
                history_ctx = '\n'.join(truncated) + '\n'

        semantic_kws = ('表结构', '字段', '怎么用', '流程', '是什么', '说明', '文档', 'SOP', '含义')
        sql_kws = ('多少', '统计', '排行', '排名', '列表', '汇总', '销量', '销售',
                   '补货', '日报', '库存', '低库存', '总额', '采购', '畅销')

        is_semantic = any(k in question for k in semantic_kws)
        has_number = bool(re.search(r'\d', question or ''))
        is_sql = any(k in question for k in sql_kws) or has_number

        answer = ''
        route = ''
        if is_semantic and not is_sql:
            route = 'semantic'
            docs = self.semantic_search(question, top_k=3)
            answer = self._format_search_result(question, docs)
        else:
            route = 'text2sql'
            res = self.text2sql(question)
            answer = self._format_sql_result(question, res)

        # 多轮：若启用 LLM，把历史 + 检索结果喂 LLM 做最终润色回答
        if self._llm_available() and history_ctx:
            refined = self._call_llm(
                [
                    {
                        'role': 'system',
                        'content': (
                            '你是智能运营平台助手，结合对话历史与检索结果回答用户问题，'
                            '中文，简洁专业。'
                        ),
                    },
                    {
                        'role': 'user',
                        'content': f'对话历史：\n{history_ctx}\n检索结果：\n{answer}\n问题：{question}',
                    },
                ],
                temperature=0.3,
            )
            if refined:
                answer = refined.strip()

        try:
            self.session.add(ChatHistory(session_id=sid, role='user', content=question))
            self.session.add(ChatHistory(session_id=sid, role='assistant', content=answer))
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            logger.error('记录对话历史失败: %s', e)
            raise

        return {'session_id': sid, 'route': route, 'answer': answer}

    def _format_search_result(self, question, docs):
        if not docs:
            return f'未找到与「{question}」相关的文档片段。'
        lines = [f'关于「{question}」检索到 {len(docs)} 条相关文档：']
        for i, d in enumerate(docs, 1):
            lines.append(f"{i}. [{d['title']}]（相关度 {d['score']}）\n   {d['content']}")
        return '\n'.join(lines)

    def _format_sql_result(self, question, res):
        sql = res.get('sql', '')
        result = res.get('result', [])
        explanation = res.get('explanation', '')
        if not result:
            return f'{explanation}\nSQL: {sql}\n查询结果为空。'
        lines = [f'{explanation}', f'SQL: {sql}', f'共 {len(result)} 条结果：']
        for i, row in enumerate(result, 1):
            lines.append(f'{i}. {row}')
        return '\n'.join(lines)

    # ==================== 审核反馈学习 ====================

    def record_feedback(self, suggestion_id, action, original_qty, final_qty, note=''):
        """记录审核反馈 SuggestionFeedback，自动计算 qty_delta"""
        delta = (final_qty or 0) - (original_qty or 0)
        fb = SuggestionFeedback(
            suggestion_id=suggestion_id,
            original_qty=original_qty,
            final_qty=final_qty,
            action=action,
            qty_delta=delta,
            note=note,
        )
        try:
            self.session.add(fb)
            self.session.commit()
            return fb
        except Exception as e:
            self.session.rollback()
            logger.error('记录反馈失败 suggestion_id=%s: %s', suggestion_id, e)
            raise

    def get_feedback_stats(self):
        """
        反馈统计：总建议数、批准率、平均修改量、常见拒绝原因。
        作为后续 LLM prompt 的 few-shot 示例。
        """
        total = self.session.query(SuggestionFeedback).count()
        approved = (
            self.session.query(SuggestionFeedback)
            .filter(SuggestionFeedback.action == 'approved')
            .count()
        )
        rejected = total - approved
        approval_rate = round(approved / total, 2) if total else 0.0

        mod_rows = (
            self.session.query(SuggestionFeedback.qty_delta)
            .filter(SuggestionFeedback.action == 'approved')
            .all()
        )
        mods = [r[0] for r in mod_rows if r[0] is not None]
        avg_mod = round(sum(mods) / len(mods), 2) if mods else 0.0

        reject_notes = (
            self.session.query(SuggestionFeedback.note)
            .filter(
                SuggestionFeedback.action == 'rejected',
                SuggestionFeedback.note.isnot(None),
                SuggestionFeedback.note != '',
            )
            .all()
        )
        reason_kws = ('价格', '库存', '质量', '周期', '时效', '冗余', '其他')
        reasons = Counter()
        for (n,) in reject_notes:
            matched = False
            for kw in reason_kws:
                if n and kw in n:
                    reasons[kw] += 1
                    matched = True
            if not matched:
                reasons['其他'] += 1

        return {
            'total': total,
            'approved': approved,
            'rejected': rejected,
            'approval_rate': approval_rate,
            'avg_modification': avg_mod,
            'common_rejection_reasons': reasons.most_common(3),
        }

    # ==================== 查询接口 ====================

    def get_pending_suggestions(self):
        """获取待审核的补货建议（返回 dict 列表，可 JSON 序列化）"""
        rows = (
            self.session.query(Suggestion)
            .filter(Suggestion.status == 'pending')
            .order_by(Suggestion.created_at.desc())
            .all()
        )
        return [{
            'id': s.id, 'product_id': s.product_id,
            'product_name': s.product_name,
            'suggested_supplier_id': s.suggested_supplier_id,
            'suggested_supplier_name': s.suggested_supplier_name,
            'suggested_qty': s.suggested_qty,
            'original_qty': s.original_qty,
            'unit_price': float(s.unit_price) if s.unit_price else 0,
            'amount': float(s.unit_price * s.suggested_qty) if s.unit_price and s.suggested_qty else 0,
            'reason': s.reason, 'status': s.status,
            'confidence': float(s.confidence) if s.confidence else 0.8,
            'created_at': s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else ''
        } for s in rows]

    def review_suggestion(self, id, action, note='', reviewer='system', final_qty=None):
        """审核补货建议：action 为 approve/reject 或 approved/rejected。
        review 时自动调 record_feedback 记录反馈。

        :param final_qty: 可选，审核时修改最终采购数量（P1-3 前端数量修改）
                          approve 时若提供则覆盖 suggested_qty
                          reject 时强制为 0
        """
        s = self.session.query(Suggestion).filter(Suggestion.id == id).first()
        if not s:
            raise ValueError('建议不存在')
        action = action.lower()
        # 兼容前端 approve/reject 与 approved/rejected
        if action in ('approve', 'approved'):
            action = 'approved'
        elif action in ('reject', 'rejected'):
            action = 'rejected'
        else:
            raise ValueError('action 必须为 approve 或 reject')

        if s.status != 'pending':
            raise ValueError(f'建议已处理（当前状态：{s.status})')

        # P1-3: 数量修改逻辑
        if action == 'approved':
            if final_qty is not None:
                if not isinstance(final_qty, int) or final_qty < 0:
                    raise ValueError('final_qty 必须为非负整数')
                final_qty = final_qty
            else:
                final_qty = s.suggested_qty
        else:
            final_qty = 0
        try:
            s.status = action
            s.reviewed_at = datetime.now()
            s.review_note = note
            s.reviewed_by = reviewer
            # P1-3: 若 final_qty 与 suggested_qty 不同，记到 review_note
            if action == 'approved' and final_qty != s.suggested_qty:
                extra = f'[数量调整 {s.suggested_qty}→{final_qty}] '
                s.review_note = extra + (note or '')
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            logger.error('审核建议失败 id=%s: %s', id, e)
            raise

        # 自动记录反馈（独立事务，审核结果已落库）
        self.record_feedback(
            suggestion_id=id,
            action=action,
            original_qty=s.original_qty,
            final_qty=final_qty,
            note=note,
        )
        return s

    def get_chat_history(self, session_id, limit=20):
        """获取指定会话的对话历史（按时间倒序，最多 limit 条）"""
        return (
            self.session.query(ChatHistory)
            .filter(ChatHistory.session_id == session_id)
            .order_by(ChatHistory.created_at.desc())
            .limit(limit)
            .all()
        )
