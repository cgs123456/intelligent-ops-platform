"""BOM（物料清单）+ MRP（物料需求计划）模型

支持按产品维护多版本 BOM，并基于 BOM 展开 MRP 计算物料需求。

核心概念：
    - BOM：物料清单头，关联成品/半成品 Product，带版本号与状态
    - BOMItem：物料清单行，记录每个子件的需求量、损耗率、顺序
    - MRPPlan：MRP 计划单，按 product + bom + planned_qty 制定生产/采购计划
    - MRPRequirement：MRP 计算结果，记录每个子件的净需求与建议下单量

物料数量精度较高，统一使用 Numeric(18,4)。
"""

from datetime import datetime

from sqlalchemy import Index

from extensions import db
from models.tenant import TenantScoped


class BOM(TenantScoped, db.Model):
    """物料清单头（对应 Odoo mrp.bom）

    一个 Product 可有多个版本 BOM，但同一时间只有一条 active。
    """

    __tablename__ = "bom"
    # P1-5: (product_id, version) 唯一 —— 防止同产品同版本重复建 BOM（与迁移 h9i0j1k2l3m4 对齐）
    __table_args__ = (
        db.Index("ux_bom_tenant_code", "tenant_id", "code", unique=True),
        Index("ux_bom_product_version", "tenant_id", "product_id", "version", unique=True),
    )
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)  # 成品/半成品
    code = db.Column(db.String(32), nullable=False, index=True)  # BOM 编码
    version = db.Column(db.String(16), nullable=False, default="1.0")  # 版本号
    status = db.Column(db.String(16), nullable=False, default="active", index=True)  # active/frozen
    quantity = db.Column(db.Numeric(18, 4), nullable=False, default=1)  # 基础产量（生产 quantity 单位成品所需子件量）
    unit = db.Column(db.String(8), nullable=False, default="个")  # 计量单位
    description = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class BOMItem(TenantScoped, db.Model):
    """物料清单行（对应 Odoo mrp.bom.line）

    每行记录一个子件及其用量。scrap_rate 为损耗率（如 0.05 表示 5% 损耗）。
    """

    __tablename__ = "bom_item"
    # P1-5: (bom_id, component_product_id) 唯一 —— 同一子件不可重复行
    __table_args__ = (Index("ux_bom_item_bom_component", "tenant_id", "bom_id", "component_product_id", unique=True),)
    id = db.Column(db.Integer, primary_key=True)
    bom_id = db.Column(db.Integer, db.ForeignKey("bom.id"), nullable=False, index=True)
    component_product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)
    quantity = db.Column(db.Numeric(18, 4), nullable=False)  # 单位用量
    unit = db.Column(db.String(8), nullable=False, default="个")
    sequence = db.Column(db.Integer, nullable=False, default=10)  # 行序号
    scrap_rate = db.Column(db.Numeric(5, 4), nullable=False, default=0)  # 损耗率，0~1
    notes = db.Column(db.String(256))


class MRPPlan(TenantScoped, db.Model):
    """MRP 计划单（对应 Odoo mrp.production）

    status 流转：draft -> calculated -> released -> closed
        - draft：新建，未计算
        - calculated：已计算需求
        - released：已下达生产/采购
        - closed：已关闭
    """

    __tablename__ = "mrp_plan"
    __table_args__ = (db.Index("ux_mrp_tenant_plan_no", "tenant_id", "plan_no", unique=True),)
    id = db.Column(db.Integer, primary_key=True)
    plan_no = db.Column(db.String(40), nullable=False, index=True)  # 计划单号
    product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)  # 计划生产的产品
    bom_id = db.Column(db.Integer, db.ForeignKey("bom.id"), nullable=True, index=True)  # 使用的 BOM
    planned_qty = db.Column(db.Numeric(18, 4), nullable=False)  # 计划生产数量
    planned_date = db.Column(db.DateTime, nullable=False)  # 计划完成日期
    status = db.Column(db.String(16), nullable=False, default="draft", index=True)  # draft/calculated/released/closed
    created_at = db.Column(db.DateTime, default=datetime.now)


class MRPRequirement(TenantScoped, db.Model):
    """MRP 需求明细（对应 Odoo mrp.production 的 component 需求展开）

    每行记录一个子件的毛需求 / 现有库存 / 净需求 / 建议下单量 / 建议下单日期。
    """

    __tablename__ = "mrp_requirement"
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("mrp_plan.id"), nullable=False, index=True)
    component_product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)
    required_qty = db.Column(db.Numeric(18, 4), nullable=False)  # 毛需求量
    on_hand_qty = db.Column(db.Numeric(18, 4), nullable=False, default=0)  # 现有库存
    net_requirement = db.Column(db.Numeric(18, 4), nullable=False, default=0)  # 净需求 = 毛需求 - 现有库存（最小 0）
    suggested_order_qty = db.Column(db.Numeric(18, 4), nullable=False, default=0)  # 建议下单量
    suggested_order_date = db.Column(db.DateTime)  # 建议下单日期
