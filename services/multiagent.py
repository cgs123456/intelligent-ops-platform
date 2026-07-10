"""
多 Agent 采购博弈（改进7）
==========================
Agent A（我方采购）：基于销售趋势 + 库存 + 成本上限生成采购需求
Agent B（供应商1）：基于报价历史 + 利润率模拟报价策略
Agent C（供应商2）：模拟竞争报价

三方用 LLM 扮演不同角色，输出各自的报价 / 交期 / 采购数量。
LLM 不可用时回退到规则模拟（确保 Demo 永远可用）。

最终 score_quotes() 综合评分（价格 + 交期 + 供应商评级）选最优供应商。

模块独立，但通过注入 AIGCService 复用其 LLM 抽象层。
"""
import logging
import random

logger = logging.getLogger(__name__)


class MultiAgentNegotiator:
    """多 Agent 采购博弈：买方 Agent + 2 个供应商 Agent 竞价。"""

    # 综合评分权重（价格 50% + 交期 30% + 供应商评级 20%，归一化）
    PRICE_WEIGHT = 0.5
    LEAD_TIME_WEIGHT = 0.3
    RATING_WEIGHT = 0.2

    # 供应商评级映射：A=5 / B=4 / C=3
    RATING_SCORE = {'A': 5, 'B': 4, 'C': 3}

    def __init__(self, aigc_service=None, db_session=None):
        """注入 AIGCService 以复用 LLM 调用与 db_session。"""
        # 延迟导入避免循环依赖
        from services.aigc_service import AIGCService
        self._aigc = aigc_service or AIGCService(db_session=db_session)
        self.session = self._aigc.session

    # ---------------- 主入口 ----------------

    def negotiate(self, product, suppliers, suggested_qty=None):
        """对单个产品发起三方博弈。

        :param product: Product 对象或 dict {id, name, cost_price, sale_price, stock_qty, safety_stock}
        :param suppliers: list[Supplier] 候选供应商（取前 2 家，不足自动补 mock）
        :param suggested_qty: 建议采购量（可选，默认按库存缺口计算）
        :return: {product_id, product_name, buyer_demand, quotes, best, negotiation_log}
        """
        sup_list = list(suppliers or [])[:2]
        if len(sup_list) < 2:
            sup_list.append(self._mock_competitor(product, sup_list))

        # 1. Agent A：我方采购需求
        buyer_demand = self._agent_a_buyer(product, suggested_qty)

        # 2. Agent B/C：供应商报价
        quotes = []
        for sup in sup_list:
            try:
                q = self._agent_supplier(product, sup, buyer_demand)
                quotes.append(q)
            except Exception as e:
                logger.warning('[MultiAgent-Supplier] 报价生成失败 supplier=%s: %s',
                               self._get(sup, 'name', '?'), e)

        # 3. 综合评分选最优
        scored = self.score_quotes(quotes)
        best = scored[0] if scored else None

        pid = self._get(product, 'id')
        pname = self._get(product, 'name')
        result = {
            'product_id': pid,
            'product_name': pname,
            'buyer_demand': buyer_demand,
            'quotes': scored,
            'best': best,
            'negotiation_log': self._build_log(pname, buyer_demand, quotes, best),
        }
        logger.info('[MultiAgent] 博弈完成 product=%s quotes=%d best=%s',
                    pname, len(scored),
                    best.get('supplier_name') if best else None)
        return result

    # ---------------- Agent A：我方采购 ----------------

    def _agent_a_buyer(self, product, suggested_qty=None):
        """基于销售趋势 + 库存 + 成本上限生成采购需求。"""
        pid = self._get(product, 'id')
        name = self._get(product, 'name')
        cost = float(self._get(product, 'cost_price', 0) or 0)
        sale = float(self._get(product, 'sale_price', 0) or 0)
        stock = int(self._get(product, 'stock_qty', 0) or 0)
        safety = int(self._get(product, 'safety_stock', 0) or 0)

        # 库存缺口 = 安全库存*2 - 当前库存（与 _calc_confidence 思路一致）
        shortage = max(0, safety * 2 - stock)
        qty = int(suggested_qty) if suggested_qty is not None else max(shortage, 1)

        # 成本上限 = 当前成本价 * 1.1（允许 10% 上浮），无成本价时不限制
        max_unit_price = round(cost * 1.1, 2) if cost > 0 else None

        # LLM 扮演采购经理
        if self._aigc._llm_available():
            prompt = [
                {
                    'role': 'system',
                    'content': (
                        '你是中型电商企业的资深采购经理。基于销售趋势、当前库存、'
                        '安全库存和成本上限生成采购需求。'
                        '严格输出 JSON：{"qty": int, "max_unit_price": float, "reason": str}，'
                        'reason 不超过 50 字。'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        f"产品：{name}(ID={pid})，当前库存 {stock}，安全库存 {safety}，"
                        f"成本价 ¥{cost}，售价 ¥{sale}。"
                        f"初步计算缺口 {shortage} 件，建议采购 {qty} 件，"
                        f"成本上限 ¥{max_unit_price}。请确认或调整。"
                    ),
                },
            ]
            resp = self._aigc._call_llm(prompt, temperature=0.3)
            if resp:
                parsed = self._parse_json(resp)
                if parsed:
                    try:
                        qty = int(parsed.get('qty', qty))
                        if max_unit_price is not None and parsed.get('max_unit_price'):
                            max_unit_price = float(parsed['max_unit_price'])
                        reason = parsed.get('reason', '')
                        logger.info('[MultiAgent-Buyer] LLM 决策 qty=%d max_price=%s', qty, max_unit_price)
                        return {
                            'qty': qty, 'max_unit_price': max_unit_price,
                            'shortage': shortage, 'reason': reason, 'agent': 'buyer',
                        }
                    except (TypeError, ValueError) as e:
                        logger.warning('[MultiAgent-Buyer] LLM 字段解析失败：%s，回退规则', e)

        # 规则兜底
        return {
            'qty': qty, 'max_unit_price': max_unit_price, 'shortage': shortage,
            'reason': f'库存缺口 {shortage} 件，建议采购 {qty} 件，成本上限 ¥{max_unit_price}',
            'agent': 'buyer',
        }

    # ---------------- Agent B/C：供应商 ----------------

    def _agent_supplier(self, product, supplier, buyer_demand):
        """基于报价历史 + 利润率模拟报价策略。"""
        sid = self._get(supplier, 'id') or 0
        sname = self._get(supplier, 'name') or f'供应商{sid}'
        lead_days = int(self._get(supplier, 'lead_days', 7) or 7)
        rating = (self._get(supplier, 'rating', 'B') or 'B').upper()

        cost = float(self._get(product, 'cost_price', 0) or 0)
        if cost <= 0:
            cost = 100.0  # 兜底

        # 查报价历史（最近 5 条均值），无历史时按成本 +5% 利润
        try:
            from models.external import ExtSupplierQuote
            hist = (
                self.session.query(ExtSupplierQuote)
                .filter_by(supplier_id=sid)
                .order_by(ExtSupplierQuote.collected_at.desc())
                .limit(5)
                .all()
            )
            avg_hist = (sum(float(q.quote_price) for q in hist) / len(hist)) if hist else (cost * 1.05)
        except Exception as e:
            logger.debug('[MultiAgent-Supplier] 历史报价查询失败 sid=%s: %s', sid, e)
            avg_hist = cost * 1.05

        # 评级影响：A 级价格略高但交期短，C 级价格低但交期长
        rating_factor = {'A': 1.05, 'B': 1.0, 'C': 0.95}.get(rating, 1.0)
        base_price = round(max(avg_hist, cost) * rating_factor, 2)

        buyer_qty = buyer_demand.get('qty', 1)
        max_price = buyer_demand.get('max_unit_price')
        pname = self._get(product, 'name')

        # LLM 扮演供应商销售经理
        if self._aigc._llm_available():
            prompt = [
                {
                    'role': 'system',
                    'content': (
                        f'你是供应商「{sname}」的销售经理。评级 {rating} 级，'
                        f'标准交期 {lead_days} 天。基于成本价、利润率与竞争对手，'
                        f'给出有竞争力的报价与交期。'
                        f'严格输出 JSON：{{"price": float, "lead_days": int, "reason": str}}，'
                        f'reason 不超过 40 字。'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        f"采购方询价：产品 {pname}，数量 {buyer_qty} 件，"
                        f"最高可接受 ¥{max_price}/件。市场成本价 ¥{cost}，"
                        f"你的历史均价 ¥{avg_hist:.2f}。请给出报价。"
                    ),
                },
            ]
            resp = self._aigc._call_llm(prompt, temperature=0.5)
            if resp:
                parsed = self._parse_json(resp)
                if parsed:
                    try:
                        price = float(parsed.get('price', base_price))
                        lead = int(parsed.get('lead_days', lead_days))
                        reason = parsed.get('reason', '')
                        logger.info('[MultiAgent-Supplier] LLM 报价 %s price=%.2f lead=%d', sname, price, lead)
                        return {
                            'supplier_id': sid, 'supplier_name': sname, 'rating': rating,
                            'price': price, 'lead_days': lead, 'reason': reason,
                            'agent': 'supplier',
                        }
                    except (TypeError, ValueError) as e:
                        logger.warning('[MultiAgent-Supplier] LLM 字段解析失败：%s，回退规则', e)

        # 规则兜底：历史均价 ±3% 波动 + 评级交期调整
        fluct = random.uniform(-0.03, 0.03)
        price = round(base_price * (1 + fluct), 2)
        lead_adjust = {'A': -2, 'B': 0, 'C': 3}.get(rating, 0)
        lead = max(1, lead_days + lead_adjust)
        return {
            'supplier_id': sid, 'supplier_name': sname, 'rating': rating,
            'price': price, 'lead_days': lead,
            'reason': f'基于历史均价 ¥{avg_hist:.2f} 与 {rating}级评级，报价 ¥{price}，交期 {lead} 天',
            'agent': 'supplier',
        }

    # ---------------- 综合评分 ----------------

    def score_quotes(self, quotes):
        """综合评分（价格 50% + 交期 30% + 供应商评级 20%）。

        每个维度 min-max 归一化到 [0,1] 后乘以权重求和，分数越低越优。
        :return: 按 score 升序排列的列表（best 在 index 0）
        """
        if not quotes:
            return []

        prices = [float(q['price']) for q in quotes]
        leads = [int(q['lead_days']) for q in quotes]
        ratings = [self.RATING_SCORE.get((q.get('rating') or 'B').upper(), 4) for q in quotes]

        price_norm = self._min_max(prices, lower_better=True)
        lead_norm = self._min_max(leads, lower_better=True)
        rating_norm = self._min_max(ratings, lower_better=False)

        scored = []
        for i, q in enumerate(quotes):
            score = (
                price_norm[i] * self.PRICE_WEIGHT
                + lead_norm[i] * self.LEAD_TIME_WEIGHT
                + rating_norm[i] * self.RATING_WEIGHT
            )
            scored.append({**q, 'score': round(score, 4)})

        scored.sort(key=lambda x: x['score'])
        return scored

    # ---------------- 工具方法 ----------------

    def _mock_competitor(self, product, existing):
        """候选供应商不足 2 家时构造一个 mock 竞争对手（评级 B、交期 7 天）。"""
        existing_ids = {self._get(s, 'id') for s in existing}
        mock_id = max([i for i in existing_ids if isinstance(i, int)] + [0]) + 9001
        return {
            'id': mock_id,
            'name': f'竞争对手-{mock_id}',
            'lead_days': 7,
            'rating': 'B',
        }

    def _build_log(self, pname, buyer_demand, quotes, best):
        """生成博弈过程日志（供前端展示与审计）。"""
        lines = [
            f"【采购方】需采购 {pname} {buyer_demand['qty']} 件，"
            f"成本上限 ¥{buyer_demand.get('max_unit_price')}/件。{buyer_demand.get('reason', '')}",
        ]
        for q in quotes:
            lines.append(
                f"【供应商 {q['supplier_name']}】{q.get('rating', 'B')}级 "
                f"报价 ¥{q['price']}/件，交期 {q['lead_days']} 天。{q.get('reason', '')}"
            )
        if best:
            lines.append(
                f"【决策】综合评分（价格50%+交期30%+评级20%）"
                f"选择 {best['supplier_name']}，得分 {best['score']}。"
            )
        else:
            lines.append('【决策】无可用报价。')
        return lines

    @staticmethod
    def _get(obj, key, default=None):
        """兼容 ORM 对象与 dict 取字段。"""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _parse_json(text):
        """从 LLM 输出中提取首个 JSON 对象，失败返回 None。"""
        if not text:
            return None
        import json as _json
        import re as _re
        m = _re.search(r'\{.*\}', text, _re.DOTALL)
        if not m:
            return None
        try:
            return _json.loads(m.group(0))
        except _json.JSONDecodeError:
            return None

    @staticmethod
    def _min_max(values, lower_better=True):
        """min-max 归一化到 [0,1]，lower_better=True 时值越小分数越低（越优）。"""
        lo, hi = min(values), max(values)
        if hi == lo:
            return [0.5] * len(values)
        norm = [(v - lo) / (hi - lo) for v in values]
        return norm if lower_better else [1 - n for n in norm]
