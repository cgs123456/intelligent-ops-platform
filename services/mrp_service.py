"""MRP（物料需求计划）服务

实现 BOM 管理 + MRP 计算的核心业务逻辑：
    1. 创建 BOM（头 + 行）
    2. MRP 计算：根据计划产量展开 BOM 子件需求，扣减现有库存得净需求
    3. 查询 BOM 树结构

计算说明：
    - 本实现支持 BOM 一层展开（即只展开直接子件）
    - 多层 BOM 递归展开可扩展：在 _expand_requirements 中递归调用自身，
      传入子件作为新的 product_id，直到所有子件无下层 BOM
    - 损耗率 scrap_rate 仅影响毛需求量（required_qty * (1 + scrap_rate)）
    - 净需求 = max(0, 毛需求 - 现有库存)
    - 建议下单量（简化）= 净需求（实际可考虑最小批量、批量倍数、提前期）
"""

import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal

from extensions import db
from models.bom import BOM, BOMItem, MRPPlan, MRPRequirement
from models.erp import Product

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
        return f'BOM-{datetime.now().strftime("%Y%m%d%H%M%S")}-{random.randint(1000, 9999)}'

    @staticmethod
    def _gen_plan_no() -> str:
        """生成 MRP 计划单号：MRP-YYYYMMDDHHMMSS-NNNN"""
        return f'MRP-{datetime.now().strftime("%Y%m%d%H%M%S")}-{random.randint(1000, 9999)}'

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

        注：本实现支持 BOM 一层展开；多层 BOM 递归可扩展
            （在 _expand_requirements 中递归调用，传入子件作为新 product_id）
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

            items = (
                self.db.query(BOMItem)
                .filter_by(bom_id=bom.id)
                .order_by(BOMItem.sequence, BOMItem.id)
                .all()
            )

            requirements = []
            for item in items:
                scrap_rate = self._to_decimal(item.scrap_rate)
                # 毛需求 = 单位用量 * (1 + 损耗率) * 缩放系数
                required_qty = self._to_decimal(item.quantity) * (Decimal(1) + scrap_rate) * scale
                required_qty = required_qty.quantize(Decimal("0.0001"))

                # 现有库存
                comp = self.db.get(Product, item.component_product_id)
                on_hand_qty = self._to_decimal(comp.stock_qty if comp else 0)

                # 净需求 = max(0, 毛需求 - 现有库存)
                net_requirement = max(Decimal(0), required_qty - on_hand_qty)
                # 建议下单量 = 净需求（简化：不考虑最小批量 / 批量倍数）
                suggested_order_qty = net_requirement

                req = MRPRequirement(
                    plan_id=plan_id,
                    component_product_id=item.component_product_id,
                    required_qty=required_qty,
                    on_hand_qty=on_hand_qty,
                    net_requirement=net_requirement,
                    suggested_order_qty=suggested_order_qty,
                    suggested_order_date=plan.planned_date - timedelta(days=comp.safety_stock) if comp and comp.safety_stock else plan.planned_date,
                )
                self.db.add(req)
                requirements.append(
                    {
                        "component_product_id": item.component_product_id,
                        "component_sku": comp.sku if comp else None,
                        "component_name": comp.name if comp else None,
                        "required_qty": float(required_qty),
                        "on_hand_qty": float(on_hand_qty),
                        "net_requirement": float(net_requirement),
                        "suggested_order_qty": float(suggested_order_qty),
                        "suggested_order_date": req.suggested_order_date.strftime("%Y-%m-%d") if req.suggested_order_date else None,
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

        items = (
            self.db.query(BOMItem)
            .filter_by(bom_id=bom_id)
            .order_by(BOMItem.sequence, BOMItem.id)
            .all()
        )

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
                    "component_sku": comp_map[it.component_product_id].sku if it.component_product_id in comp_map else None,
                    "component_name": comp_map[it.component_product_id].name if it.component_product_id in comp_map else None,
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
