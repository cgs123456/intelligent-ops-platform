"""MRP（物料需求计划）服务

实现 BOM 管理 + MRP 计算的核心业务逻辑：
    1. 创建 BOM（头 + 行）
    2. MRP 计算：根据计划产量展开 BOM 子件需求，扣减现有库存得净需求
    3. 查询 BOM 树结构

计算说明：
    - 支持 BOM 多层递归展开（_expand_requirements 递归调用自身，直到叶子节点）
    - 环路检测（visited set 防止 BOM 自引用死循环）+ 深度限制 10 层
    - 损耗率 scrap_rate 仅影响毛需求量（required_qty * (1 + scrap_rate)）
    - 净需求 = max(0, 毛需求 - 现有库存)
    - 建议下单量（简化）= 净需求（实际可考虑最小批量、批量倍数、提前期）
"""

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from extensions import db
from models.bom import BOM, BOMItem, MRPPlan, MRPRequirement
from models.erp import Product, PurchaseOrder, Supplier

logger = logging.getLogger(__name__)


class MRPService:
    """MRP + BOM 服务（实例方法风格，支持外部注入 db_session）"""

    def __init__(self, db_session=None):
        # 允许外部注入会话（便于测试/分库），默认用全局 db.session
        self.db = db_session or db.session

    # ==================== 内部工具 ====================

    @staticmethod
    def _gen_bom_code() -> str:
        """生成 BOM 编码：BOM-YYYYMMDDHHMMSS-NNNN"""
        return f'BOM-{datetime.now().strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:8]}'

    @staticmethod
    def _gen_plan_no() -> str:
        """生成 MRP 计划单号：MRP-YYYYMMDDHHMMSS-NNNN"""
        return f'MRP-{datetime.now().strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:8]}'

    @staticmethod
    def _to_decimal(value) -> Decimal:
        """安全转换为 Decimal，None/空值返回 0"""
        if value is None:
            return Decimal(0)
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    # ==================== 业务方法 ====================

    def create_bom(
        self,
        product_id: int,
        items: list,
        code: str = None,
        version: str = "1.0",
        quantity=1,
        unit: str = "个",
        description: str = None,
    ) -> BOM:
        """创建 BOM（头 + 行）

        :param product_id: 成品/半成品 Product ID
        :param items: 行项目列表，每项为 dict：
            - component_product_id: 子件 Product ID（必填）
            - quantity: 单位用量（必填）
            - unit: 单位（可选，默认 '个'）
            - sequence: 行序号（可选，默认 10 * index + 10）
            - scrap_rate: 损耗率（可选，默认 0）
            - notes: 备注（可选）
        :param code: BOM 编码（可选，自动生成）
        :param version: 版本号，默认 '1.0'
        :param quantity: 基础产量，默认 1
        :param unit: 计量单位，默认 '个'
        :param description: 描述（可选）
        :return: BOM 对象
        :raises ValueError: 产品不存在 / items 为空 / 子件不存在时抛出
        """
        if not items or not isinstance(items, list):
            raise ValueError("BOM 行项目不能为空")

        product = self.db.get(Product, product_id)
        if not product:
            raise ValueError(f"产品不存在: {product_id}")

        try:
            bom = BOM(
                code=code or self._gen_bom_code(),
                product_id=product_id,
                version=version or "1.0",
                status="active",
                quantity=self._to_decimal(quantity),
                unit=unit or "个",
                description=description,
            )
            self.db.add(bom)
            self.db.flush()  # 取 bom.id

            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"第 {idx + 1} 行项目非 dict")
                comp_id = item.get("component_product_id")
                if not comp_id:
                    raise ValueError(f"第 {idx + 1} 行缺少 component_product_id")
                comp = self.db.get(Product, comp_id)
                if not comp:
                    raise ValueError(f"子件不存在: {comp_id}")
                qty = self._to_decimal(item.get("quantity"))
                if qty <= 0:
                    raise ValueError(f"第 {idx + 1} 行 quantity 必须为正数")

                line = BOMItem(
                    bom_id=bom.id,
                    component_product_id=comp_id,
                    quantity=qty,
                    unit=item.get("unit", "个"),
                    sequence=item.get("sequence", (idx + 1) * 10),
                    scrap_rate=self._to_decimal(item.get("scrap_rate", 0)),
                    notes=item.get("notes"),
                )
                self.db.add(line)

            self.db.commit()
            logger.info("BOM 已创建: %s product=%s items=%d", bom.code, product_id, len(items))
            return bom
        except Exception as e:
            self.db.rollback()
            logger.exception("创建 BOM 失败: %s", e)
            raise

    def _expand_requirements(self, component_id, qty, visited=None):
        """递归展开 BOM 子件需求（多层）

        将 component_id 的毛需求 qty 沿其下层 BOM 递归展开到叶子节点（无 active BOM 的子件），
        返回 {component_id: gross_qty} 聚合字典（同一叶子在多分支出现时毛需求累加）。

        - 环路检测：visited 记录当前递归路径上的祖先 component_id，遇到已存在者抛 ValueError
        - 深度限制：visited 长度达到 10 时停止递归，按叶子处理
        - 叶子节点（无 active BOM）直接返回自身毛需求

        :param component_id: 待展开的子件 Product ID
        :param qty: 该子件的毛需求量（Decimal）
        :param visited: 当前递归路径已访问的 component_id 集合（环路检测用）
        :return: dict {component_id: gross_qty}
        :raises ValueError: 检测到 BOM 环路时抛出
        """
        if visited is None:
            visited = set()

        # 环路检测：当前组件已在祖先路径上 → BOM 自引用死循环
        if component_id in visited:
            raise ValueError(f"BOM 环路检测：component_id={component_id} 已在展开路径中")

        # 深度限制 10 层：超出后按叶子处理，停止递归
        if len(visited) >= 10:
            return {component_id: self._to_decimal(qty)}

        # 查找该组件的 active BOM（取第一条）
        sub_bom = self.db.query(BOM).filter_by(product_id=component_id, status="active").order_by(BOM.id).first()
        if not sub_bom:
            # 叶子节点：无下层 BOM，直接记录毛需求
            return {component_id: self._to_decimal(qty)}

        # 有下层 BOM：按 子件单位用量 * (1 + 损耗率) * (qty / bom.quantity) 递归展开
        path = visited | {component_id}
        bom_quantity = self._to_decimal(sub_bom.quantity) or Decimal(1)
        sub_items = self.db.query(BOMItem).filter_by(bom_id=sub_bom.id).order_by(BOMItem.sequence, BOMItem.id).all()

        result = {}
        for sub_item in sub_items:
            scrap_rate = self._to_decimal(sub_item.scrap_rate)
            child_qty = (
                self._to_decimal(sub_item.quantity) * (Decimal(1) + scrap_rate) * (self._to_decimal(qty) / bom_quantity)
            )
            child_qty = child_qty.quantize(Decimal("0.0001"))
            child_result = self._expand_requirements(sub_item.component_product_id, child_qty, path)
            for cid, q in child_result.items():
                result[cid] = result.get(cid, Decimal(0)) + self._to_decimal(q)
        return result

    def calculate_mrp(self, plan_id: int) -> dict:
        """MRP 计算：根据计划产量展开 BOM 子件需求

        计算公式：
            毛需求 required_qty = BOM 行 quantity * (1 + scrap_rate) * (plan_qty / bom.quantity)
            净需求 net_requirement = max(0, required_qty - on_hand_qty)
            建议下单量 suggested_order_qty = net_requirement（简化：不考虑最小批量）
            建议下单日期 suggested_order_date = planned_date - 提前期（简化：直接用 planned_date）

        :param plan_id: MRP 计划单 ID
        :return: {"plan_id": int, "requirements": list[dict]}
        :raises ValueError: 计划不存在 / 未关联 BOM 时抛出

        注：支持 BOM 多层递归展开（_expand_requirements 递归到叶子节点，
            含环路检测与 10 层深度限制）
        """
        plan = self.db.get(MRPPlan, plan_id)
        if not plan:
            raise ValueError(f"MRP 计划不存在: {plan_id}")
        if not plan.bom_id:
            raise ValueError(f"MRP 计划 {plan_id} 未关联 BOM")

        bom = self.db.get(BOM, plan.bom_id)
        if not bom:
            raise ValueError(f"BOM 不存在: {plan.bom_id}")

        try:
            # 清空旧需求（重复计算场景）
            self.db.query(MRPRequirement).filter_by(plan_id=plan_id).delete()

            plan_qty = self._to_decimal(plan.planned_qty)
            bom_quantity = self._to_decimal(bom.quantity) or Decimal(1)  # 防止 0 除
            # 缩放系数：plan_qty / bom.quantity（按比例放大子件需求）
            scale = plan_qty / bom_quantity

            items = self.db.query(BOMItem).filter_by(bom_id=bom.id).order_by(BOMItem.sequence, BOMItem.id).all()

            # P1-13: 多层 BOM 递归展开——将每行子件毛需求沿下层 BOM 展开到叶子节点，
            # 同一叶子在多分支出现时累加毛需求，再统一扣减库存计算净需求
            gross_map = {}  # {component_id: 毛需求量}
            for item in items:
                scrap_rate = self._to_decimal(item.scrap_rate)
                # 顶层毛需求 = 单位用量 * (1 + 损耗率) * 缩放系数
                required_qty = self._to_decimal(item.quantity) * (Decimal(1) + scrap_rate) * scale
                required_qty = required_qty.quantize(Decimal("0.0001"))
                sub_gross = self._expand_requirements(item.component_product_id, required_qty)
                for cid, q in sub_gross.items():
                    gross_map[cid] = gross_map.get(cid, Decimal(0)) + self._to_decimal(q)

            requirements = []
            for cid in sorted(gross_map.keys()):
                required_qty = self._to_decimal(gross_map[cid]).quantize(Decimal("0.0001"))

                # 现有库存
                comp = self.db.get(Product, cid)
                on_hand_qty = self._to_decimal(comp.stock_qty if comp else 0)

                # 净需求 = max(0, 毛需求 - 现有库存)
                net_requirement = max(Decimal(0), required_qty - on_hand_qty)
                # 建议下单量 = 净需求（简化：不考虑最小批量 / 批量倍数）
                suggested_order_qty = net_requirement

                # P0-ERP4: 建议下单日期 = 计划完成日 - 供应商提前期
                # 查询该产品默认供应商的 lead_days，找不到则默认 3 天
                if comp:
                    supplier = (
                        self.db.query(Supplier)
                        .join(PurchaseOrder, PurchaseOrder.supplier_id == Supplier.id)
                        .filter(PurchaseOrder.product_id == comp.id)
                        .first()
                    )
                    lead_days = supplier.lead_days if supplier else 3
                    suggested_order_date = plan.planned_date - timedelta(days=lead_days)
                else:
                    suggested_order_date = plan.planned_date

                req = MRPRequirement(
                    plan_id=plan_id,
                    component_product_id=cid,
                    required_qty=required_qty,
                    on_hand_qty=on_hand_qty,
                    net_requirement=net_requirement,
                    suggested_order_qty=suggested_order_qty,
                    suggested_order_date=suggested_order_date,
                )
                self.db.add(req)
                requirements.append(
                    {
                        "component_product_id": cid,
                        "component_sku": comp.sku if comp else None,
                        "component_name": comp.name if comp else None,
                        "required_qty": float(required_qty),
                        "on_hand_qty": float(on_hand_qty),
                        "net_requirement": float(net_requirement),
                        "suggested_order_qty": float(suggested_order_qty),
                        "suggested_order_date": (
                            req.suggested_order_date.strftime("%Y-%m-%d") if req.suggested_order_date else None
                        ),
                    }
                )

            plan.status = "calculated"
            self.db.commit()
            logger.info("MRP 计算完成: plan=%s requirements=%d", plan.plan_no, len(requirements))
            return {"plan_id": plan_id, "plan_no": plan.plan_no, "requirements": requirements}
        except Exception as e:
            self.db.rollback()
            logger.exception("MRP 计算失败: %s", e)
            raise

    def release_plan(self, plan_id: int) -> dict:
        """下达 MRP 计划：将建议下单量 > 0 的需求转为草稿采购单

        对每条 suggested_order_qty > 0 的 MRPRequirement，调用 ERPService.create_purchase_order
        生成状态为 draft 的采购单。计划状态升级为 released。

        :param plan_id: MRP 计划单 ID
        :return: {"plan_id": int, "released_count": int, "orders": list}
        :raises ValueError: 计划不存在 / 计划未计算（状态非 calculated）时抛出
        """
        plan = self.db.get(MRPPlan, plan_id)
        if not plan:
            raise ValueError(f"MRP 计划不存在: {plan_id}")
        if plan.status != "calculated":
            raise ValueError(f"计划状态 {plan.status} 不可下达，需先 calculate_mrp")

        try:
            requirements = (
                self.db.query(MRPRequirement)
                .filter_by(plan_id=plan_id)
                .filter(MRPRequirement.suggested_order_qty > 0)
                .order_by(MRPRequirement.id)
                .all()
            )

            orders = []
            for req in requirements:
                comp = self.db.get(Product, req.component_product_id)
                if not comp:
                    logger.warning("release_plan 跳过：子件不存在 product_id=%s", req.component_product_id)
                    continue

                # 默认供应商：取该产品历史采购单的最近供应商，找不到则取任意 active 供应商
                supplier = (
                    self.db.query(Supplier)
                    .join(PurchaseOrder, PurchaseOrder.supplier_id == Supplier.id)
                    .filter(PurchaseOrder.product_id == comp.id)
                    .order_by(PurchaseOrder.id.desc())
                    .first()
                )
                if supplier is None:
                    supplier = self.db.query(Supplier).filter_by(is_active=True).order_by(Supplier.id).first()
                if supplier is None:
                    logger.warning("release_plan 跳过：无可用供应商 product_id=%s", comp.id)
                    continue

                # P1-6: 直接创建 PurchaseOrder 对象，不调用 erp_service.create_purchase_order
                # （后者会内部 commit，中途失败导致部分释放）。所有 PO 在循环结束后统一 commit。
                unit_price = self._to_decimal(comp.cost_price)
                currency_code = comp.currency_code or "CNY"
                tax_rate = self._to_decimal(comp.tax_rate) if comp.tax_rate is not None else Decimal("0.13")
                qty = int(req.suggested_order_qty)
                order = PurchaseOrder(
                    order_no=f'PO-{datetime.now().strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:8]}',
                    supplier_id=supplier.id,
                    product_id=comp.id,
                    qty=qty,
                    unit_price=unit_price,
                    status="draft",
                    suggestion_id=req.id,
                    currency_code=currency_code,
                    tax_rate=tax_rate,
                )
                self.db.add(order)
                self.db.flush()  # 取 order.id 用于返回
                orders.append(
                    {
                        "order_id": order.id,
                        "order_no": order.order_no,
                        "product_id": comp.id,
                        "qty": float(req.suggested_order_qty),
                    }
                )

            plan.status = "released"
            self.db.commit()
            logger.info("MRP 计划 %s 已下达：生成 %d 张采购单", plan.plan_no, len(orders))
            return {
                "plan_id": plan_id,
                "plan_no": plan.plan_no,
                "released_count": len(orders),
                "orders": orders,
            }
        except Exception as e:
            self.db.rollback()
            logger.exception("下达 MRP 计划失败: %s", e)
            raise

    def get_bom_tree(self, bom_id: int) -> dict:
        """返回 BOM 树结构

        本实现返回一层子件列表；多层 BOM 递归可扩展（在每个 node 下递归挂 children）。

        :param bom_id: BOM ID
        :return: {"bom": {...}, "items": [{...}]}
        :raises ValueError: BOM 不存在时抛出
        """
        bom = self.db.get(BOM, bom_id)
        if not bom:
            raise ValueError(f"BOM 不存在: {bom_id}")

        items = self.db.query(BOMItem).filter_by(bom_id=bom_id).order_by(BOMItem.sequence, BOMItem.id).all()

        # 批量预取子件 Product，避免 N+1
        comp_ids = [it.component_product_id for it in items]
        comp_map: dict = {}
        if comp_ids:
            comps = self.db.query(Product).filter(Product.id.in_(comp_ids)).all()
            comp_map = {c.id: c for c in comps}

        product = self.db.get(Product, bom.product_id)

        result = {
            "bom": {
                "id": bom.id,
                "code": bom.code,
                "product_id": bom.product_id,
                "product_sku": product.sku if product else None,
                "product_name": product.name if product else None,
                "version": bom.version,
                "status": bom.status,
                "quantity": float(bom.quantity or 1),
                "unit": bom.unit,
                "description": bom.description,
                "created_at": bom.created_at.strftime("%Y-%m-%d %H:%M:%S") if bom.created_at else None,
            },
            "items": [
                {
                    "id": it.id,
                    "sequence": it.sequence,
                    "component_product_id": it.component_product_id,
                    "component_sku": (
                        comp_map[it.component_product_id].sku if it.component_product_id in comp_map else None
                    ),
                    "component_name": (
                        comp_map[it.component_product_id].name if it.component_product_id in comp_map else None
                    ),
                    "quantity": float(it.quantity),
                    "unit": it.unit,
                    "scrap_rate": float(it.scrap_rate or 0),
                    "notes": it.notes,
                }
                for it in items
            ],
        }
        return result

    def create_plan(
        self,
        product_id: int,
        bom_id: int,
        planned_qty,
        planned_date,
    ) -> MRPPlan:
        """创建 MRP 计划单（辅助方法，便于测试与端到端流程）

        :param product_id: 计划生产的产品 ID
        :param bom_id: 使用的 BOM ID
        :param planned_qty: 计划生产数量
        :param planned_date: 计划完成日期（datetime 或 YYYY-MM-DD 字符串）
        :return: MRPPlan 对象
        :raises ValueError: 产品/BOM 不存在时抛出
        """
        product = self.db.get(Product, product_id)
        if not product:
            raise ValueError(f"产品不存在: {product_id}")
        bom = self.db.get(BOM, bom_id)
        if not bom:
            raise ValueError(f"BOM 不存在: {bom_id}")

        if isinstance(planned_date, str):
            try:
                planned_date = datetime.strptime(planned_date, "%Y-%m-%d")
            except ValueError as e:
                raise ValueError(f"planned_date 格式非法：{e}") from e

        try:
            plan = MRPPlan(
                plan_no=self._gen_plan_no(),
                product_id=product_id,
                bom_id=bom_id,
                planned_qty=self._to_decimal(planned_qty),
                planned_date=planned_date,
                status="draft",
            )
            self.db.add(plan)
            self.db.commit()
            logger.info("MRP 计划已创建: %s product=%s qty=%s", plan.plan_no, product_id, planned_qty)
            return plan
        except Exception as e:
            self.db.rollback()
            logger.exception("创建 MRP 计划失败: %s", e)
            raise
