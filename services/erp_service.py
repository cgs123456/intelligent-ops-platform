"""
ERP 服务层（模拟 Odoo 进销存 + 财务核心链路）

职责：
    - 采购管理：创建采购单、确认采购单（生成应付凭证）、到货入库（更新库存 + 移动加权平均成本 + 入库流水）、取消采购单
    - 销售管理：创建销售单（扣减库存 + 出库流水 + 应收凭证 + 按 avg_cost 结转成本）
    - 退货管理：创建退货单（库存回增 + 退货入库流水 + 红字应收凭证 refund）
    - 库存调拨：warehouse 间转移库存 + 调拨流水
    - 成本核算：采购入库时按移动加权平均法更新 Product.avg_cost
    - 查询接口：库存汇总 / 近期订单（修复 N+1）/ 会计汇总 / 仓库汇总
    - 审计日志：所有写操作调用 AuditLog 留痕（actor 默认 'system'）

对应 Odoo 模块：purchase / sale / stock / account / inventory。
"""

import logging
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from extensions import db
from models.erp import (
    AccountingPeriod,
    AccountMove,
    Budget,
    ExchangeRate,
    Product,
    PurchaseOrder,
    ReorderRule,
    ReturnOrder,
    SaleOrder,
    StockLocation,
    StockLot,
    StockMove,
    StockTake,
    Supplier,
    Warehouse,
)
from models.external import ExtSupplierQuote
from models.system import AuditLog
from models.warehouse import DwsSalesSkuDaily
from services.forecast import safety_stock_recommendation
from services.notifier import notifier

logger = logging.getLogger(__name__)


class ERPService:
    """ERP 进销存 + 财务服务（实例方法风格，支持外部传入 db_session）"""

    def __init__(self, db_session=None):
        # 允许外部注入会话（便于测试/分库），默认用全局 db.session
        self.db = db_session or db.session

    # ==================== 内部工具 ====================

    def _assert_period_open(self, move_date=None):
        """会计期间锁定校验：关账期间禁止过账（P3-F4）。

        无期间记录视为开放（未建期间不禁账）；命中 is_closed 抛 ValueError，
        由调用方在事务内回滚。四个过账入口调用：confirm_purchase_order /
        create_sale_order / create_return_order / create_purchase_return。
        """
        d = move_date or datetime.now()
        name = f"{d.year:d}-{d.month:02d}"
        period = self.db.query(AccountingPeriod).filter_by(name=name).first()
        if period is not None and period.is_closed:
            raise ValueError(f"会计期间 {name} 已关账（{period.closed_by or '未知'} 关闭），禁止过账")

    def recalc_safety_stock(self, product_id, service_level=0.95, window_days=90, apply=False, actor="system"):
        """基于需求波动与交期的安全库存建议（P3-F1）。

        SS = z × σ_日需求 × √提前期。σ 取 DWS 日销序列（含零日）标准差；
        提前期取最优活跃报价供应商的 lead_days，无报价回退 5 天（与 ADS 补货同口径）。
        apply=True 时更新 Product.safety_stock 并写审计。
        """
        product = self.db.query(Product).filter_by(id=product_id).first()
        if product is None:
            raise ValueError(f"产品不存在: {product_id}")

        series_start = date.today() - timedelta(days=window_days - 1)
        rows = (
            self.db.query(DwsSalesSkuDaily.dt, DwsSalesSkuDaily.sale_qty)
            .filter(
                DwsSalesSkuDaily.product_id == product_id,
                DwsSalesSkuDaily.dt >= series_start,
            )
            .all()
        )
        series = [(dt, int(q or 0)) for dt, q in rows]

        best = (
            self.db.query(Supplier)
            .join(ExtSupplierQuote, ExtSupplierQuote.supplier_id == Supplier.id)
            .filter(ExtSupplierQuote.product_id == product_id, Supplier.is_active.is_(True))
            .order_by(ExtSupplierQuote.quote_price.asc(), ExtSupplierQuote.quote_date.desc())
            .first()
        )
        lead_days = best.lead_days if best else 5

        rec = safety_stock_recommendation(series, lead_days, service_level=service_level, window_days=window_days)
        rec["lead_days"] = lead_days
        rec["current_safety_stock"] = product.safety_stock

        if apply:
            product.safety_stock = rec["safety_stock"]
            self._audit(
                actor,
                "update",
                "product",
                product.id,
                detail=f"安全库存重算: {rec['current_safety_stock']} -> {rec['safety_stock']}"
                f"（服务水位{service_level}，σ={rec['sigma_daily']}，交期{lead_days}天）",
            )
            self.db.commit()
        return rec

    # ---- P3-F4: 会计期间管理 ----

    def list_accounting_periods(self):
        """列出全部会计期间（按名称倒序，最近在前）。"""
        return self.db.query(AccountingPeriod).order_by(AccountingPeriod.name.desc()).all()

    def create_accounting_period(self, name, actor="system"):
        """创建会计期间。name 格式 YYYY-MM（正则校验），唯一约束防重。"""
        if not isinstance(name, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", (name or "").strip()):
            raise ValueError("期间格式必须为 YYYY-MM（如 2026-10）")
        name = name.strip()
        if self.db.query(AccountingPeriod).filter_by(name=name).first():
            raise ValueError(f"会计期间 {name} 已存在")
        period = AccountingPeriod(name=name)
        self.db.add(period)
        self._audit(actor, "create", "accounting_period", None, detail=f"创建会计期间 {name}")
        self.db.commit()
        return period

    def close_accounting_period(self, name, actor="system"):
        """关闭会计期间：此后该月四个过账入口全部拒绝（_assert_period_open）。

        幂等：重复关闭同一期间不报错（返回已关账对象）。
        """
        period = self.db.query(AccountingPeriod).filter_by(name=name).first()
        if period is None:
            raise ValueError(f"会计期间 {name} 不存在，请先创建")
        if not period.is_closed:
            period.is_closed = True
            period.closed_at = datetime.now()
            period.closed_by = actor
            self._audit(actor, "close", "accounting_period", period.id, detail=f"关闭会计期间 {name}")
            self.db.commit()
        return period

    def _audit(self, actor, action, target_type, target_id, detail="", ip=None):
        """写审计日志（对应 Odoo auditlog 模块，所有关键操作留痕）"""
        log = AuditLog(
            actor=actor or "system",
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=detail,
            ip=ip,
        )
        self.db.add(log)
        return log

    @staticmethod
    def _gen_order_no(prefix):
        """生成带随机后缀的单号，避免高并发冲突

        P2-26: 改用 uuid.uuid4().hex[:8] 替代 random.randint(1000, 9999)，
        避免随机数碰撞及 random 模块在多进程下的可预测性问题。
        """
        return f'{prefix}-{datetime.now().strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:8]}'

    @staticmethod
    def _to_decimal(value) -> Decimal:
        """P1-1: 安全转换为 Decimal，避免 Decimal(float) 精度污染。

        None/空值返回 0；Decimal 原样返回；其他类型先 str() 再 Decimal()，
        规避 Decimal(0.1) == 0.1000000000000000055511151231257827021181583404541015625 的问题。
        """
        if value is None:
            return Decimal(0)
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    # ==================== 采购管理 ====================

    @staticmethod
    def _validate_qty_price(qty, unit_price):
        """P0-4: 数量/单价合法性校验（采购单/销售单共用）。

        负数 qty 会反向"卖入库存"（stock_qty -= qty 库存凭空增加）并生成负应收/应付凭证；
        非正数单价会污染移动加权平均成本核算。qty 列为 Integer，这里拒绝小数与字符串数字
        以外的输入；unit_price 允许 int/float/str/Decimal，但必须 > 0。
        """
        if isinstance(qty, bool):  # bool 是 int 子类，int(True)==1 会绕过数值校验
            raise ValueError(f"数量必须为正整数: {qty!r}")
        try:
            qty_val = int(qty)
        except (TypeError, ValueError):
            raise ValueError(f"数量必须为整数: {qty!r}")
        if qty_val != qty or qty_val <= 0:
            raise ValueError(f"数量必须为正整数: {qty!r}")
        try:
            price_val = ERPService._to_decimal(unit_price)
        except Exception:
            raise ValueError(f"单价必须为数字: {unit_price!r}")
        if price_val <= 0:
            raise ValueError(f"单价必须大于 0: {unit_price!r}")

    @staticmethod
    def _validate_positive_qty(qty):
        """P0-1/P0-2: 退货/调拨数量校验 —— 必须为正整数。

        负数退货会反向搬运库存（stock_qty += 负数）并生成负红字凭证；
        负数调拨负负得正凭空入库；0 只产生无效流水。bool 视为非法输入。
        """
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"数量必须为正整数: {qty!r}")

    def create_purchase_order(
        self,
        supplier_id,
        product_id,
        qty,
        unit_price,
        warehouse_id=None,
        ext_order_no=None,
        suggestion_id=None,
        currency_code=None,
        tax_rate=None,
        actor="system",
        ip=None,
    ):
        """创建采购单（对应 Odoo purchase.order 创建，状态 draft）

        P1-9: 支持 currency_code/tax_rate 参数，未传时回退到 Product 默认值。
        """
        try:
            # P0-4: 数量/单价合法性校验（负数会凭空增库存/污染成本核算）
            self._validate_qty_price(qty, unit_price)
            # P1-9: 未显式传入币种/税率时，从 Product 取默认值（保证向后兼容）
            if currency_code is None or tax_rate is None:
                product = self.db.get(Product, product_id)
                if product is None:
                    raise ValueError(f"产品不存在: {product_id}")
                if currency_code is None:
                    currency_code = product.currency_code or "CNY"
                if tax_rate is None:
                    tax_rate = product.tax_rate if product.tax_rate is not None else Decimal("0.13")

            order = PurchaseOrder(
                order_no=self._gen_order_no("PO"),
                supplier_id=supplier_id,
                product_id=product_id,
                qty=qty,
                unit_price=unit_price,
                status="draft",
                ext_order_no=ext_order_no,
                suggestion_id=suggestion_id,
                warehouse_id=warehouse_id,
                currency_code=currency_code,
                tax_rate=self._to_decimal(tax_rate),
            )
            self.db.add(order)
            self.db.flush()  # 取 order.id
            self._audit(
                actor,
                "create",
                "purchase_order",
                order.id,
                detail=f"创建采购单 {order.order_no} supplier={supplier_id} "
                f"product={product_id} qty={qty} price={unit_price} "
                f"currency={currency_code} tax_rate={tax_rate}",
                ip=ip,
            )
            self.db.commit()
            logger.info("采购单已创建: %s", order.order_no)
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception("创建采购单失败: %s", e)
            raise

    def confirm_purchase_order(self, order_id, ext_order_no=None, actor="system", ip=None):
        """确认采购单（对应 Odoo purchase.button_confirm）：draft -> confirmed，生成应付凭证
        - ext_order_no: 可选，RPA 场景下回填外部平台订单号（如 1688/京东 采购单号）

        P1-9: 凭证写入 currency_code 与 tax_amount（税额 = 不含税金额 * tax_rate）。
              约定：amount 字段保持不含税金额，tax_amount 单独存储税额。
              若订单无税率字段（旧数据），按 0 处理，向后兼容。
        """
        # P3-F4: 会计期间锁定校验（关账期间禁止过账）
        self._assert_period_open()
        try:
            order = self.db.get(PurchaseOrder, order_id)
            if not order:
                raise ValueError(f"采购单不存在: {order_id}")
            if order.status != "draft":
                raise ValueError(f"采购单状态非 draft，无法确认: 当前={order.status}")

            order.status = "confirmed"
            order.confirmed_at = datetime.now()
            if ext_order_no:
                order.ext_order_no = ext_order_no

            # 生成应付凭证（对应 Odoo account.move payable）
            # P1-9: 计算税额 tax_amount = 不含税金额 * tax_rate
            amount = self._to_decimal(order.qty) * self._to_decimal(order.unit_price)
            tax_rate = self._to_decimal(order.tax_rate) if order.tax_rate is not None else Decimal(0)
            tax_amount = (amount * tax_rate).quantize(Decimal("0.01"))
            currency_code = order.currency_code or "CNY"
            # P1-9: 从 Product 继承 cost_center_id 写入凭证（支持预算实际金额回填）
            product = self.db.get(Product, order.product_id)
            cost_center_id = product.cost_center_id if product else None
            acc = AccountMove(
                ref_type="payable",
                ref_order=order.order_no,
                amount=amount,
                account="应付账款",
                tax_amount=tax_amount,
                currency_code=currency_code,
                cost_center_id=cost_center_id,
            )
            self.db.add(acc)

            self._audit(
                actor,
                "confirm",
                "purchase_order",
                order.id,
                detail=f"确认采购单 {order.order_no} 生成应付凭证 {amount} 税额 {tax_amount} "
                f"currency={currency_code}"
                f'{f" ext_order_no={ext_order_no}" if ext_order_no else ""}',
                ip=ip,
            )
            self.db.commit()
            logger.info("采购单已确认: %s", order.order_no)
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception("确认采购单失败: %s", e)
            raise

    def receive_purchase_order(
        self,
        order_id,
        qty=None,
        lot_no=None,
        manufacture_date=None,
        expiry_date=None,
        actor="system",
        ip=None,
    ):
        """到货入库（对应 Odoo purchase.button_done + stock.picking 入库）
        - 更新 Product.stock_qty
        - 按移动加权平均法更新 Product.avg_cost
        - 生成入库流水 StockMove(in)
        - P1-9: 支持可选 lot_no 参数，传入时创建 StockLot 批次记录并关联 StockMove
        - P1-12: 支持部分到货 —— 传入 qty 参数为本次到货数量，累计 received_qty，
          当 received_qty >= order.qty 时状态置 received。

        :param qty: 本次到货数量（可选）。None 表示全量到货（向后兼容）
        :param lot_no: 批次号（可选）。传入时创建 StockLot，并关联到入库流水
        :param manufacture_date: 生产日期（可选，配合 lot_no）
        :param expiry_date: 过期日期（可选，配合 lot_no）
        """
        try:
            order = self.db.get(PurchaseOrder, order_id)
            if not order:
                raise ValueError(f"采购单不存在: {order_id}")
            if order.status != "confirmed":
                raise ValueError(f"采购单状态非 confirmed，无法入库: 当前={order.status}")

            # P1-12: 部分到货支持 —— qty 为 None 时全量入库（向后兼容）
            this_qty = qty if qty is not None else (order.qty - (order.received_qty or 0))
            if this_qty <= 0:
                raise ValueError(f"本次到货数量必须为正数: qty={this_qty}")
            remaining_to_receive = order.qty - (order.received_qty or 0)
            if this_qty > remaining_to_receive:
                raise ValueError(f"本次到货数量超过未交数量: 本次={this_qty} 未交={remaining_to_receive}")

            # P0-2: 加行锁 SELECT FOR UPDATE，防止高并发入库库存/成本错乱
            # with_for_update() 在 SQLite 下被忽略，PostgreSQL 下生效
            product = self.db.query(Product).filter_by(id=order.product_id).with_for_update().first()
            if not product:
                raise ValueError(f"产品不存在: {order.product_id}")

            # 移动加权平均成本：avg_cost = (原库存*原avg_cost + 入库数量*采购单价) / (原库存+入库数量)
            old_qty = self._to_decimal(product.stock_qty or 0)
            old_avg = self._to_decimal(product.avg_cost or 0)
            in_qty = self._to_decimal(this_qty)
            in_price = self._to_decimal(order.unit_price)
            new_qty = old_qty + in_qty
            if new_qty > 0:
                new_avg = (old_qty * old_avg + in_qty * in_price) / new_qty
            else:
                new_avg = old_avg

            product.stock_qty = int(new_qty)
            product.avg_cost = new_avg

            # P1-9: 批次管理 —— 传入 lot_no 时创建 StockLot 记录
            lot_id = None
            if lot_no:
                lot = StockLot(
                    lot_no=lot_no,
                    product_id=product.id,
                    supplier_id=order.supplier_id,
                    manufacture_date=manufacture_date,
                    expiry_date=expiry_date,
                    quantity=int(in_qty),
                    remaining_qty=int(in_qty),
                    status="active",
                )
                self.db.add(lot)
                self.db.flush()  # 取 lot.id
                lot_id = lot.id

            # 入库流水（对应 Odoo stock.move in）
            move = StockMove(
                product_id=product.id,
                qty=int(in_qty),  # 正数=入库
                move_type="in",
                ref_order=order.order_no,
                warehouse_id=order.warehouse_id,
                lot_id=lot_id,
            )
            self.db.add(move)

            # P1-12: 累计 received_qty，全部到货时状态置 received
            order.received_qty = (order.received_qty or 0) + int(in_qty)
            if order.received_qty >= order.qty:
                order.status = "received"
                order.received_at = datetime.now()
            else:
                order.status = "confirmed"

            self._audit(
                actor,
                "receive",
                "purchase_order",
                order.id,
                detail=f"采购入库 {order.order_no} this_qty={int(in_qty)} "
                f"received_qty={order.received_qty}/{order.qty} "
                f"avg_cost {old_avg}->{new_avg}"
                f'{f" lot_no={lot_no}" if lot_no else ""}',
                ip=ip,
            )
            self.db.commit()
            logger.info(
                "采购入库完成: %s this_qty=%s received=%s/%s avg_cost=%s lot=%s",
                order.order_no,
                int(in_qty),
                order.received_qty,
                order.qty,
                new_avg,
                lot_no or "-",
            )
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception("采购入库失败: %s", e)
            raise

    def cancel_purchase_order(self, order_id, actor="system", ip=None):
        """取消采购单（对应 Odoo purchase.button_cancel）：置 cancelled 状态

        P1-11: 取消 confirmed 状态的采购单时，生成红字（负数金额）应付凭证冲销原凭证。
        draft 状态的取消无需冲销（尚未生成凭证）。

        P1-2: 部分到货（received_qty > 0 且 < qty）的 confirmed 单取消时，
        按整单全额生成红字凭证会与实际入库数量不符，故禁止直接取消；
        received / returned 状态也不允许取消。
        """
        try:
            order = self.db.get(PurchaseOrder, order_id)
            if not order:
                raise ValueError(f"采购单不存在: {order_id}")
            # P1-2: received / returned 状态拦截（已入库或已退货不可取消）
            if order.status in ("received", "returned"):
                raise ValueError(f"采购单状态 {order.status} 无法取消")
            # P1-2: 部分到货拦截 —— confirmed 但 received_qty > 0 时禁止整单取消
            if order.received_qty and order.received_qty > 0:
                raise ValueError("已部分到货，不可直接取消")

            # P1-11: confirmed 状态取消时，生成红字应付凭证冲销原凭证
            if order.status == "confirmed":
                amount = self._to_decimal(order.qty) * self._to_decimal(order.unit_price)
                tax_rate = self._to_decimal(order.tax_rate) if order.tax_rate is not None else Decimal(0)
                tax_amount = (amount * tax_rate).quantize(Decimal("0.01"))
                currency_code = order.currency_code or "CNY"
                reversal = AccountMove(
                    ref_type="payable",
                    ref_order=order.order_no,
                    amount=-amount,
                    account="应付账款",
                    tax_amount=-tax_amount,
                    currency_code=currency_code,
                )
                self.db.add(reversal)
                cancel_detail = (
                    f"取消采购单 {order.order_no} 生成红字应付凭证 -{amount} 税额 -{tax_amount} "
                    f"currency={currency_code}"
                )
            else:
                cancel_detail = f"取消采购单 {order.order_no}"

            order.status = "cancelled"
            self._audit(actor, "cancel", "purchase_order", order.id, detail=cancel_detail, ip=ip)
            self.db.commit()
            logger.info("采购单已取消: %s", order.order_no)
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception("取消采购单失败: %s", e)
            raise

    # ==================== 销售管理 ====================

    def create_sale_order(
        self,
        product_id,
        qty,
        unit_price,
        customer,
        warehouse_id=None,
        platform="线下",
        currency_code=None,
        tax_rate=None,
        actor="system",
        ip=None,
    ):
        """创建销售单（对应 Odoo sale.order 创建 + 确认 + 出库）
        - 扣减库存
        - 出库流水 StockMove(out)
        - 应收凭证 AccountMove(receivable)
        - 按 avg_cost 结转成本（COGS 记入审计）

        P1-9: 支持 currency_code/tax_rate 参数，未传时回退到 Product 默认值；
              应收凭证写入 currency_code 与 tax_amount。
        """
        # P3-F4: 会计期间锁定校验（关账期间禁止过账）
        self._assert_period_open()
        try:
            # P0-4: 数量/单价合法性校验（负数 qty 会反向"卖入库存"，生成负应收凭证）
            self._validate_qty_price(qty, unit_price)
            # P0-1: 加行锁 SELECT FOR UPDATE，防止高并发超卖
            # with_for_update() 在 SQLite 下被忽略，PostgreSQL 下生效
            product = self.db.query(Product).filter_by(id=product_id).with_for_update().first()
            if not product:
                raise ValueError(f"产品不存在: {product_id}")
            if product.stock_qty < qty:
                raise ValueError(f"库存不足: 当前={product.stock_qty} 需要={qty}")

            # P1-9: 未显式传入币种/税率时，从 Product 取默认值（保证向后兼容）
            if currency_code is None:
                currency_code = product.currency_code or "CNY"
            if tax_rate is None:
                tax_rate = product.tax_rate if product.tax_rate is not None else Decimal("0.13")

            # 扣减库存
            product.stock_qty -= qty

            order = SaleOrder(
                order_no=self._gen_order_no("SO"),
                product_id=product_id,
                qty=qty,
                unit_price=unit_price,
                customer=customer,
                platform=platform,
                status="confirmed",
                warehouse_id=warehouse_id,
                currency_code=currency_code,
                tax_rate=self._to_decimal(tax_rate),
            )
            self.db.add(order)
            self.db.flush()

            # P1-9: 出库流水（对应 Odoo stock.move out，负数=出库）
            # 按 FEFO（先到期先出）选择批次，扣减 StockLot.remaining_qty 并关联 StockMove.lot_id
            # 如果无批次（无 StockLot 记录）则跳过批次分配，兼容无批次产品
            lots = (
                self.db.query(StockLot)
                .filter(
                    StockLot.product_id == product_id,
                    StockLot.remaining_qty > 0,
                    StockLot.status == "active",
                )
                .order_by(StockLot.expiry_date.asc().nullslast(), StockLot.id.asc())
                .with_for_update()
                .all()
            )
            if lots:
                # 有批次：按 FEFO 分配，每个批次生成一条出库流水
                remaining_to_pick = qty
                for lot in lots:
                    if remaining_to_pick <= 0:
                        break
                    pick_qty = min(lot.remaining_qty, remaining_to_pick)
                    lot.remaining_qty -= pick_qty
                    if lot.remaining_qty == 0:
                        lot.status = "exhausted"
                    out_move = StockMove(
                        product_id=product_id,
                        qty=-pick_qty,
                        move_type="out",
                        ref_order=order.order_no,
                        warehouse_id=warehouse_id,
                        lot_id=lot.id,
                    )
                    self.db.add(out_move)
                    remaining_to_pick -= pick_qty
                # 批次库存不足时回退到无批次出库（兼容部分批次商品）
                if remaining_to_pick > 0:
                    fallback_move = StockMove(
                        product_id=product_id,
                        qty=-remaining_to_pick,
                        move_type="out",
                        ref_order=order.order_no,
                        warehouse_id=warehouse_id,
                    )
                    self.db.add(fallback_move)
            else:
                # 无批次：单条出库流水（向后兼容）
                move = StockMove(
                    product_id=product_id,
                    qty=-qty,
                    move_type="out",
                    ref_order=order.order_no,
                    warehouse_id=warehouse_id,
                )
                self.db.add(move)

            # 应收凭证（对应 Odoo account.move receivable）
            # P1-9: 计算税额 tax_amount = 不含税金额 * tax_rate
            receivable = self._to_decimal(qty) * self._to_decimal(unit_price)
            tax_rate_dec = self._to_decimal(tax_rate)
            tax_amount = (receivable * tax_rate_dec).quantize(Decimal("0.01"))
            # P1-9: 从 product 继承 cost_center_id 写入凭证（支持预算实际金额回填）
            acc = AccountMove(
                ref_type="receivable",
                ref_order=order.order_no,
                amount=receivable,
                account="应收账款",
                tax_amount=tax_amount,
                currency_code=currency_code,
                cost_center_id=product.cost_center_id,
            )
            self.db.add(acc)

            # 按 avg_cost 结转成本（COGS）
            cogs = self._to_decimal(qty) * self._to_decimal(product.avg_cost or 0)

            # P1-17: COGS 入凭证 —— 生成两条 AccountMove 结转成本
            # 借：主营业务成本（amount=cogs）
            # 贷：库存商品（amount=-cogs，负数表示减少）
            if cogs > 0:
                cogs_debit = AccountMove(
                    ref_type="cogs",
                    ref_order=order.order_no,
                    amount=cogs,
                    account="主营业务成本",
                    currency_code=currency_code,
                    cost_center_id=product.cost_center_id,
                )
                cogs_credit = AccountMove(
                    ref_type="cogs",
                    ref_order=order.order_no,
                    amount=-cogs,
                    account="库存商品",
                    currency_code=currency_code,
                    cost_center_id=product.cost_center_id,
                )
                self.db.add_all([cogs_debit, cogs_credit])

            self._audit(
                actor,
                "order",
                "sale_order",
                order.id,
                detail=f"创建销售单 {order.order_no} customer={customer} "
                f"qty={qty} receivable={receivable} tax={tax_amount} "
                f"currency={currency_code} cogs={cogs}",
                ip=ip,
            )
            self.db.commit()
            logger.info(
                "销售单已创建: %s receivable=%s tax=%s cogs=%s",
                order.order_no,
                receivable,
                tax_amount,
                cogs,
            )
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception("创建销售单失败: %s", e)
            raise

    # ==================== 退货管理 ====================

    def create_return_order(
        self, original_order_no, product_id, qty, refund_amount, reason=None, actor="system", ip=None
    ):
        """创建退货单（对应 Odoo sale 退货 + 红字凭证）
        - 库存回增
        - 退货入库流水 StockMove(return_in)
        - 红字应收凭证 AccountMove(refund)
        - ReturnOrder 记录
        """
        # P3-F4: 会计期间锁定校验（关账期间禁止过账）
        self._assert_period_open()
        try:
            # P0-1: 拒绝非正数数量/金额 —— 负数退货会反向搬运库存并生成负红字凭证
            self._validate_positive_qty(qty)
            try:
                refund_val = self._to_decimal(refund_amount)
            except Exception:
                raise ValueError(f"退款金额必须为数字: {refund_amount!r}")
            if refund_val <= 0:
                raise ValueError(f"退款金额必须大于 0: {refund_amount!r}")
            # P1-7: 校验原销售单存在 + product_id 一致 + 退货数量不超过原销量（扣减已退量）
            sale_order = self.db.query(SaleOrder).filter_by(order_no=original_order_no).first()
            if not sale_order:
                raise ValueError(f"原销售单不存在: {original_order_no}")
            if sale_order.product_id != product_id:
                raise ValueError(
                    f"退货产品与原单不一致: 原单 product_id={sale_order.product_id} " f"退货 product_id={product_id}"
                )
            # 累计已退货数量
            already_returned = (
                self.db.query(db.func.sum(ReturnOrder.qty))
                .filter(
                    ReturnOrder.original_order_no == original_order_no,
                    ReturnOrder.status == "confirmed",
                )
                .scalar()
            ) or 0
            max_returnable = sale_order.qty - int(already_returned)
            if qty > max_returnable:
                raise ValueError(
                    f"退货数量超过可退数量: 本次={qty} 已退={int(already_returned)} "
                    f"原单={sale_order.qty} 可退={max_returnable}"
                )

            # P0-ERP2: 加行锁 SELECT FOR UPDATE，防止高并发库存回增错乱
            # with_for_update() 在 SQLite 下被忽略，PostgreSQL 下生效
            product = self.db.query(Product).filter_by(id=product_id).with_for_update().first()
            if not product:
                raise ValueError(f"产品不存在: {product_id}")

            # 库存回增
            product.stock_qty += qty

            # P1-4: 回写 StockLot.remaining_qty —— 查找原销售单的出库 StockMove（按
            # original_order_no 关联 ref_order），获取 lot_id 后回增批次剩余库存。
            # 找不到原出库记录或无批次时跳过（兼容无批次销售场景）。
            original_out_moves = (
                self.db.query(StockMove)
                .filter_by(ref_order=original_order_no, move_type="out")
                .order_by(StockMove.id.asc())
                .all()
            )
            remaining_to_return = qty
            for mv in original_out_moves:
                if remaining_to_return <= 0:
                    break
                if mv.lot_id is None:
                    continue
                lot = self.db.get(StockLot, mv.lot_id)
                if lot is None:
                    continue
                # 回增上限：该出库流水的数量（取绝对值，避免超回填）
                returnable_to_lot = min(abs(mv.qty), remaining_to_return)
                lot.remaining_qty += returnable_to_lot
                # 批次状态从 exhausted 恢复为 active
                if lot.status == "exhausted" and lot.remaining_qty > 0:
                    lot.status = "active"
                remaining_to_return -= returnable_to_lot

            return_no = self._gen_order_no("RO")
            ret = ReturnOrder(
                return_no=return_no,
                original_order_no=original_order_no,
                product_id=product_id,
                qty=qty,
                refund_amount=refund_amount,
                reason=reason,
                status="confirmed",
            )
            self.db.add(ret)
            self.db.flush()

            # 退货入库流水（对应 Odoo stock.move return_in，正数=入库）
            move = StockMove(
                product_id=product_id,
                qty=qty,
                move_type="return_in",
                ref_order=return_no,
            )
            self.db.add(move)

            # P1-8: 红字应收凭证（对应 Odoo account.move refund）
            # 从原销售单获取 currency_code 和 tax_rate，计算 tax_amount = refund_amount * tax_rate
            currency_code = sale_order.currency_code or "CNY"
            tax_rate = self._to_decimal(sale_order.tax_rate) if sale_order.tax_rate is not None else Decimal(0)
            tax_amount = (self._to_decimal(refund_amount) * tax_rate).quantize(Decimal("0.01"))
            # P1-9: 从 product 继承 cost_center_id 写入凭证（支持预算实际金额回填）
            acc = AccountMove(
                ref_type="refund",
                ref_order=return_no,
                amount=self._to_decimal(refund_amount),
                account="应收账款",
                tax_amount=tax_amount,
                currency_code=currency_code,
                cost_center_id=product.cost_center_id,
            )
            self.db.add(acc)

            # P1-3: 反向 COGS 凭证 —— 原销售单结转了 COGS，退货时按 qty * avg_cost
            # 生成反向结转凭证（冲减原 COGS，库存回增）：
            #   借：库存商品（amount=cogs）
            #   贷：主营业务成本（amount=-cogs）
            cogs = self._to_decimal(qty) * self._to_decimal(product.avg_cost or 0)
            if cogs > 0:
                cogs_debit = AccountMove(
                    ref_type="cogs",
                    ref_order=return_no,
                    amount=cogs,
                    account="库存商品",
                    currency_code=currency_code,
                    cost_center_id=product.cost_center_id,
                )
                cogs_credit = AccountMove(
                    ref_type="cogs",
                    ref_order=return_no,
                    amount=-cogs,
                    account="主营业务成本",
                    currency_code=currency_code,
                    cost_center_id=product.cost_center_id,
                )
                self.db.add_all([cogs_debit, cogs_credit])

            self._audit(
                actor,
                "return",
                "return_order",
                ret.id,
                detail=f"创建退货单 {return_no} 原单={original_order_no} "
                f"qty={qty} refund={refund_amount} tax={tax_amount} "
                f"currency={currency_code} cogs={cogs} reason={reason}",
                ip=ip,
            )
            self.db.commit()
            logger.info("退货单已创建: %s cogs=%s", return_no, cogs)
            return ret
        except Exception as e:
            self.db.rollback()
            logger.exception("创建退货单失败: %s", e)
            raise

    # ==================== 库存调拨 ====================

    def transfer_stock(
        self,
        product_id,
        qty,
        from_warehouse_id,
        to_warehouse_id,
        actor="system",
        ip=None,
        from_location_id=None,
        to_location_id=None,
    ):
        """库存调拨（对应 Odoo stock.move internal transfer）：warehouse 间转移库存
        Product.stock_qty 为总库存（不随调拨变化），仅记录调拨流水影响仓库级余额。
        生成两条 transfer 流水：源仓库出库(负)、目标仓库入库(正)。
        P3-G3: 可选记录库位——出库流水挂 from_location_id、入库流水挂 to_location_id。
        库位归属仓库校验在此强制；库位级余额始终以流水聚合为准（事件溯源口径）。
        """
        try:
            if from_warehouse_id == to_warehouse_id:
                raise ValueError("源仓库与目标仓库相同")

            # P0-2: 拒绝非正数数量 —— 负数调拨负负得正凭空入库，0 产生两条无效流水
            self._validate_positive_qty(qty)

            # P3-G3: 库位归属校验（库位必须属于对应一侧仓库且启用）
            if from_location_id is not None:
                loc = self.db.get(StockLocation, from_location_id)
                if loc is None or loc.warehouse_id != from_warehouse_id:
                    raise ValueError(f"源库位不属于源仓库: location_id={from_location_id}")
                if not loc.is_active:
                    raise ValueError(f"源库位已停用: {loc.code}")
            if to_location_id is not None:
                loc = self.db.get(StockLocation, to_location_id)
                if loc is None or loc.warehouse_id != to_warehouse_id:
                    raise ValueError(f"目标库位不属于目标仓库: location_id={to_location_id}")
                if not loc.is_active:
                    raise ValueError(f"目标库位已停用: {loc.code}")

            # P1-5: 加行锁 SELECT FOR UPDATE，防止并发调拨导致超卖
            # with_for_update() 在 SQLite 下被忽略，PostgreSQL 下生效
            if not self.db.query(Product).filter_by(id=product_id).with_for_update().first():
                raise ValueError(f"产品不存在: {product_id}")

            # P1-10: 校验源仓库库存 —— 聚合源仓库 StockMove.qty 求和，校验 >= qty
            source_stock = (
                self.db.query(db.func.sum(StockMove.qty))
                .filter(
                    StockMove.product_id == product_id,
                    StockMove.warehouse_id == from_warehouse_id,
                )
                .scalar()
            ) or 0
            if source_stock < qty:
                raise ValueError(
                    f"源仓库库存不足: 当前={int(source_stock)} 需要={qty} " f"warehouse_id={from_warehouse_id}"
                )

            # 源仓库出库流水
            out_move = StockMove(
                product_id=product_id,
                qty=-qty,
                move_type="transfer",
                ref_order=f'TR-{datetime.now().strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:8]}',
                warehouse_id=from_warehouse_id,
                from_location_id=from_location_id,
            )
            # 目标仓库入库流水
            in_move = StockMove(
                product_id=product_id,
                qty=qty,
                move_type="transfer",
                ref_order=out_move.ref_order,
                warehouse_id=to_warehouse_id,
                to_location_id=to_location_id,
            )
            self.db.add_all([out_move, in_move])
            self.db.flush()

            self._audit(
                actor,
                "transfer",
                "stock_move",
                out_move.id,
                detail=f"调拨 product={product_id} qty={qty} "
                f"from_wh={from_warehouse_id} to_wh={to_warehouse_id} "
                f"from_loc={from_location_id} to_loc={to_location_id} "
                f"ref={out_move.ref_order}",
                ip=ip,
            )
            self.db.commit()
            logger.info("库存调拨完成: %s -> %s qty=%s", from_warehouse_id, to_warehouse_id, qty)
            return out_move.ref_order
        except Exception as e:
            self.db.rollback()
            logger.exception("库存调拨失败: %s", e)
            raise

    # ==================== P3-G3: 库位管理 ====================

    def create_location(self, warehouse_id, code, name=None, is_active=True, actor="system", ip=None):
        """创建库位（仓库内唯一 code）。

        库位是流水级追溯维度，不改变仓库级余额语义；重复 code 由
        ux_stock_location_wh_code 唯一索引兜底，这里前置校验给出可读错误。
        """
        try:
            code = (code or "").strip()
            if not code or len(code) > 32:
                raise ValueError("库位编码必填且不超过 32 字符")
            wh = self.db.get(Warehouse, warehouse_id)
            if wh is None:
                raise ValueError(f"仓库不存在: {warehouse_id}")
            dup = self.db.query(StockLocation).filter_by(warehouse_id=warehouse_id, code=code).first()
            if dup is not None:
                raise ValueError(f"库位编码已存在: {code}")
            loc = StockLocation(warehouse_id=warehouse_id, code=code, name=name, is_active=is_active)
            self.db.add(loc)
            self.db.flush()
            self._audit(
                actor, "create", "stock_location", loc.id, detail=f"创建库位 {code} warehouse={warehouse_id}", ip=ip
            )
            self.db.commit()
            return loc
        except Exception:
            self.db.rollback()
            raise

    def list_locations(self, warehouse_id=None, include_inactive=False):
        """库位清单（可按仓库过滤）。"""
        q = self.db.query(StockLocation).order_by(StockLocation.warehouse_id, StockLocation.code)
        if warehouse_id is not None:
            q = q.filter_by(warehouse_id=warehouse_id)
        if not include_inactive:
            q = q.filter_by(is_active=True)
        return q.all()

    def location_stock(self, location_id):
        """库位现存量（事件溯源口径）：按 StockMove.qty 有符号聚合。

        约定：入库流水(to_location_id=本库位, qty>0)、出库流水(from_location_id=本库位,
        qty<0)、调拨两条流水各挂一侧库位 —— 因此 sum(qty) over 关联流水的带符号和即
        库位现存量。仓库级余额校验仍是权威约束；库位余额是追溯视图（未启用库位前
        的历史流水不计入任何库位）。
        """
        loc = self.db.get(StockLocation, location_id)
        if loc is None:
            raise ValueError(f"库位不存在: {location_id}")
        rows = (
            self.db.query(
                StockMove.product_id,
                db.func.sum(StockMove.qty),
                db.func.max(StockMove.created_at),
            )
            .filter(
                db.or_(
                    StockMove.from_location_id == location_id,
                    StockMove.to_location_id == location_id,
                )
            )
            .group_by(StockMove.product_id)
            .all()
        )
        product_ids = [r[0] for r in rows]
        products = {p.id: p for p in self.db.query(Product).filter(Product.id.in_(product_ids))} if product_ids else {}
        return {
            "location": {"id": loc.id, "code": loc.code, "name": loc.name, "warehouse_id": loc.warehouse_id},
            "items": [
                {
                    "product_id": pid,
                    "sku": products[pid].sku if pid in products else None,
                    "qty": int(qty or 0),
                    "last_move_at": last_at.isoformat() if last_at else None,
                }
                for pid, qty, last_at in rows
            ],
        }

    # ==================== P3-G1: 试算平衡（科目余额表） ====================

    def trial_balance(self):
        """科目余额表（单行凭证模型下的试算平衡视图）。

        AccountMove 是单行凭证（无借贷列）：payable/receivable 正记、refund 红字
        正记靠 ref_type 区分、cogs 借贷成对（净额≈0）。本报表按 (account, ref_type)
        聚合净额与税额并给出全局总计；同时校验 ref_order 断链（指向不存在的
        采购单/销售单/退货单）。真正的复式双分录改造是架构级变更，见
        docs/PHASE3_BUSINESS.md 的取舍记录。
        """
        rows = (
            self.db.query(
                AccountMove.account,
                AccountMove.ref_type,
                db.func.count(AccountMove.id),
                db.func.sum(AccountMove.amount),
                db.func.sum(AccountMove.tax_amount),
            )
            .group_by(AccountMove.account, AccountMove.ref_type)
            .order_by(AccountMove.account, AccountMove.ref_type)
            .all()
        )
        accounts = [
            {
                "account": account or "(未设置)",
                "ref_type": ref_type,
                "entry_count": int(cnt or 0),
                "net_amount": float(net or 0),
                "tax_amount": float(tax or 0),
            }
            for account, ref_type, cnt, net, tax in rows
        ]
        totals = {
            "entry_count": sum(a["entry_count"] for a in accounts),
            "net_amount": round(sum(a["net_amount"] for a in accounts), 2),
            "tax_amount": round(sum(a["tax_amount"] for a in accounts), 2),
        }

        # 断链检查：ref_order 必须指向真实单据（采购/销售/退货）
        po = {r[0] for r in self.db.query(PurchaseOrder.order_no).all()}
        so = {r[0] for r in self.db.query(SaleOrder.order_no).all()}
        ro = {r[0] for r in self.db.query(ReturnOrder.return_no).all()}
        valid = po | so | ro
        dangling = (
            self.db.query(AccountMove.ref_order, AccountMove.ref_type, db.func.count(AccountMove.id))
            .group_by(AccountMove.ref_order, AccountMove.ref_type)
            .all()
        )
        dangling_refs = [
            {"ref_order": ref, "ref_type": rt, "entry_count": int(cnt)} for ref, rt, cnt in dangling if ref not in valid
        ]
        return {"accounts": accounts, "totals": totals, "dangling_refs": dangling_refs}

    # ==================== 查询接口 ====================

    def get_inventory_summary(self):
        """库存汇总（对应 Odoo product.product 库存视图）：SKU 级库存 + 价值 + 安全库存预警"""
        products = self.db.query(Product).filter(Product.is_active.is_(True)).all()
        result = []
        for p in products:
            stock_value = self._to_decimal(p.stock_qty or 0) * self._to_decimal(p.avg_cost or 0)
            result.append(
                {
                    "id": p.id,
                    "sku": p.sku,
                    "name": p.name,
                    "category": p.category,
                    "stock_qty": p.stock_qty,
                    "avg_cost": float(p.avg_cost or 0),
                    "stock_value": float(stock_value),
                    "safety_stock": p.safety_stock,
                    "is_below_safety": p.stock_qty < p.safety_stock,
                }
            )
        return result

    def get_recent_orders(self, limit=20):
        """近期采购单（对应 Odoo purchase.order 列表）
        修复 N+1：先取所有 supplier_id，再用 Supplier.id.in_(ids) 批量预取建 map，避免逐单查供应商。
        """
        orders = self.db.query(PurchaseOrder).order_by(PurchaseOrder.created_at.desc()).limit(limit).all()
        if not orders:
            return []

        # 批量预取供应商，建 id -> supplier map（修复 N+1 查询）
        supplier_ids = {o.supplier_id for o in orders}
        suppliers = self.db.query(Supplier).filter(Supplier.id.in_(supplier_ids)).all()
        supplier_map = {s.id: s for s in suppliers}

        result = []
        for o in orders:
            sup = supplier_map.get(o.supplier_id)
            result.append(
                {
                    "id": o.id,
                    "order_no": o.order_no,
                    "supplier_id": o.supplier_id,
                    "supplier_name": sup.name if sup else None,
                    "supplier_contact": sup.contact if sup else None,
                    "supplier_phone": sup.phone if sup else None,
                    "product_id": o.product_id,
                    "qty": o.qty,
                    "unit_price": float(o.unit_price),
                    "amount": float(o.qty * o.unit_price),
                    "status": o.status,
                    "warehouse_id": o.warehouse_id,
                    "ext_order_no": o.ext_order_no,
                    "created_at": o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else None,
                }
            )
        return result

    def get_account_summary(self):
        """会计汇总（对应 Odoo account.move 聚合）：应付 / 应收 / 红字退款 余额

        P2-18: 分开返回 subtotal（amount 之和）、tax_total（tax_amount 之和）、
              total = subtotal + tax_total，避免税额被遗漏。
        P2-21: 多币种场景下，非 CNY 凭证按最新汇率折算为 CNY 后再汇总；
              无汇率数据时保持原值（仅记日志注释），不阻断汇总。
        """
        rows = (
            self.db.query(
                AccountMove.ref_type,
                AccountMove.currency_code,
                db.func.sum(AccountMove.amount),
                db.func.sum(AccountMove.tax_amount),
            )
            .group_by(AccountMove.ref_type, AccountMove.currency_code)
            .all()
        )
        summary = {"payable": 0.0, "receivable": 0.0, "refund": 0.0}
        subtotal = Decimal(0)  # 不含税金额合计（折算后）
        tax_total = Decimal(0)  # 税额合计（折算后）
        for ref_type, currency_code, amount_sum, tax_sum in rows:
            if ref_type not in summary:
                continue
            amount_dec = self._to_decimal(amount_sum or 0)
            tax_dec = self._to_decimal(tax_sum or 0)
            # P2-21: 非 CNY 凭证按最新汇率折算为 CNY
            currency_code = currency_code or "CNY"
            if currency_code != "CNY":
                rate = self._get_latest_rate(currency_code, "CNY")
                if rate is not None:
                    amount_dec = (amount_dec * Decimal(str(rate))).quantize(Decimal("0.01"))
                    tax_dec = (tax_dec * Decimal(str(rate))).quantize(Decimal("0.01"))
                else:
                    # 无汇率数据：保持原值（不阻断汇总，仅记录便于运维补充汇率）
                    logger.warning("未找到 %s->CNY 汇率，凭证按原值汇总", currency_code)
            summary[ref_type] += float(amount_dec)
            subtotal += amount_dec
            tax_total += tax_dec
        # 净应收 = 应收 - 退款
        summary["net_receivable"] = summary["receivable"] - summary["refund"]
        # P2-18: 不含税合计 / 税额合计 / 含税合计
        summary["subtotal"] = float(subtotal)
        summary["tax_total"] = float(tax_total)
        summary["total"] = float(subtotal + tax_total)
        return summary

    def _get_latest_rate(self, from_currency, to_currency):
        """P2-21: 取 from_currency -> to_currency 的最新汇率（按 date 降序取首条）。

        :return: 汇率（float）或 None（无数据时）
        """
        rate = (
            self.db.query(ExchangeRate.rate)
            .filter(
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency,
            )
            .order_by(ExchangeRate.date.desc())
            .first()
        )
        return rate[0] if rate else None

    def get_warehouse_summary(self):
        """仓库汇总（对应 Odoo stock 仓库视图）：按仓库聚合 StockMove 得仓库级库存余额"""
        rows = (
            self.db.query(
                StockMove.warehouse_id,
                db.func.sum(StockMove.qty),
            )
            .group_by(StockMove.warehouse_id)
            .all()
        )
        wh_ids = [r[0] for r in rows if r[0] is not None]
        wh_map = {}
        if wh_ids:
            whs = self.db.query(Warehouse).filter(Warehouse.id.in_(wh_ids)).all()
            wh_map = {w.id: w for w in whs}

        summary = []
        for wh_id, qty_sum in rows:
            wh = wh_map.get(wh_id) if wh_id else None
            summary.append(
                {
                    "warehouse_id": wh_id,
                    "warehouse_code": wh.code if wh else None,
                    "warehouse_name": wh.name if wh else "未指定",
                    "net_stock": int(qty_sum or 0),
                }
            )
        # 按库存降序
        summary.sort(key=lambda x: x["net_stock"], reverse=True)
        return summary

    # ==================== P1-9: 库存预警 ====================

    def check_reorder_rules(self):
        """库存预警检查（对应 Odoo stock.warehouse.orderpoint cron）

        触发条件：
            - Product.stock_qty <= safety_stock（兜底，所有产品均检查）
            - 命中 ReorderRule 时按 rule.min_qty 阈值判定
        命中后通过 Notifier 发送告警，并返回告警列表供调用方记录/落库。

        :return: list[dict]，每条 dict 包含 product_id/sku/current_qty/min_qty/suggest_qty
        """
        try:
            # 预取所有启用的补货规则，建 product_id -> rule map
            rules = self.db.query(ReorderRule).filter(ReorderRule.is_active.is_(True)).all()
            rule_map: dict = {}
            for r in rules:
                # 同一 product 取 min_qty 最大的规则（更严格的阈值优先）
                if r.product_id not in rule_map or r.min_qty > rule_map[r.product_id].min_qty:
                    rule_map[r.product_id] = r

            # 取所有在售产品
            products = self.db.query(Product).filter(Product.is_active.is_(True)).all()
            alerts = []
            for p in products:
                rule = rule_map.get(p.id)
                # 阈值：命中 ReorderRule 用 rule.min_qty，否则兜底用 product.safety_stock
                threshold = rule.min_qty if rule else (p.safety_stock or 0)
                if p.stock_qty <= threshold:
                    # 建议补货量：达到 max_qty（rule 或 safety_stock * 5 兜底）
                    target_qty = rule.max_qty if rule else max(threshold * 2, (p.safety_stock or 10) * 5)
                    suggest_qty = max(0, target_qty - p.stock_qty)
                    alert = {
                        "product_id": p.id,
                        "sku": p.sku,
                        "name": p.name,
                        "current_qty": p.stock_qty,
                        "min_qty": threshold,
                        "suggest_qty": suggest_qty,
                        "supplier_id": rule.supplier_id if rule else None,
                    }
                    alerts.append(alert)

            # 通过 Notifier 异步发送告警（无 app context 时降级为仅记日志）
            if alerts:
                try:
                    lines = [
                        f"- SKU={a['sku']} 当前={a['current_qty']} 阈值={a['min_qty']} 建议补货={a['suggest_qty']}"
                        for a in alerts
                    ]
                    message = "\n".join(lines)
                    notifier.send_alert(
                        title=f"库存预警：{len(alerts)} 个 SKU 低于安全库存",
                        message=message,
                        level="warning",
                        context={"alert_count": len(alerts)},
                    )
                except Exception as ne:
                    # 告警发送失败不影响主流程
                    logger.warning("库存预警通知发送失败: %s", ne)

            logger.info("库存预警检查完成: %d 条告警", len(alerts))
            return alerts
        except Exception as e:
            logger.exception("库存预警检查失败: %s", e)
            raise

    # ==================== P2-20: 预算实际金额回填 ====================

    def refresh_budget_actual(self, budget_id, actor="system", ip=None):
        """刷新预算实际发生额（对应 Odoo account.budget cron 汇总）

        按 cost_center_id + period 聚合 AccountMove.amount（含 tax_amount），
        写入 Budget.actual_amount，实现预算执行情况的自动回填。

        聚合口径：
            - period 为 YYYY-MM，按 AccountMove.created_at 月份过滤
            - cost_center_id 匹配预算的成本中心
            - 金额 = SUM(amount) + SUM(tax_amount)（含税口径）
            - 仅统计非 COGS 结转凭证（cogs 借贷成对自抵，不重复计入预算执行）

        :param budget_id: 预算 ID
        :return: 更新后的 actual_amount
        :raises ValueError: 预算不存在时抛出
        """
        try:
            budget = self.db.get(Budget, budget_id)
            if not budget:
                raise ValueError(f"预算不存在: {budget_id}")

            period = budget.period or ""  # YYYY-MM
            try:
                year, month = period.split("-")
                period_start = datetime(int(year), int(month), 1)
                if int(month) == 12:
                    period_end = datetime(int(year) + 1, 1, 1)
                else:
                    period_end = datetime(int(year), int(month) + 1, 1)
            except (ValueError, AttributeError) as e:
                raise ValueError(f"预算 period 格式非法：{period}") from e

            # 聚合该成本中心 + 期间内的凭证金额（不含 cogs 自抵分录）
            rows = (
                self.db.query(
                    db.func.sum(AccountMove.amount),
                    db.func.sum(AccountMove.tax_amount),
                )
                .filter(AccountMove.cost_center_id == budget.cost_center_id)
                .filter(AccountMove.created_at >= period_start)
                .filter(AccountMove.created_at < period_end)
                .filter(AccountMove.ref_type != "cogs")
                .first()
            )
            amount_sum = self._to_decimal(rows[0] or 0)
            tax_sum = self._to_decimal(rows[1] or 0)
            actual = (amount_sum + tax_sum).quantize(Decimal("0.01"))

            old_actual = self._to_decimal(budget.actual_amount or 0)
            budget.actual_amount = actual

            self._audit(
                actor,
                "refresh",
                "budget",
                budget.id,
                detail=f"刷新预算实际金额 budget={budget_id} cost_center={budget.cost_center_id} "
                f"period={period} {old_actual}->{actual}",
                ip=ip,
            )
            self.db.commit()
            logger.info("预算实际金额已刷新: budget=%s actual=%s", budget_id, actual)
            return actual
        except Exception as e:
            self.db.rollback()
            logger.exception("刷新预算实际金额失败: %s", e)
            raise

    # ==================== P2-27: 采购退货 ====================

    def create_purchase_return(self, order_no, qty, reason, actor="system", ip=None):
        """创建采购退货（对应 Odoo purchase 退货 + 红字应付凭证）

        流程：
            - 校验采购单存在且状态为 received
            - 校验退货数量不超过已收货数量
            - 减少产品库存（Product.stock_qty）
            - 生成出库流水 StockMove(return_out，负数)
            - 生成红字应付凭证 AccountMove(payable，负数金额)
            - 采购单状态置 returned

        :param order_no: 采购单号
        :param qty: 退货数量（正数）
        :param reason: 退货原因
        :param actor: 操作人
        :param ip: 操作 IP
        :return: PurchaseOrder 对象
        :raises ValueError: 采购单不存在 / 状态非 received / 退货数量超限时抛出
        """
        # P3-F4: 会计期间锁定校验（关账期间禁止过账）
        self._assert_period_open()
        try:
            order = self.db.query(PurchaseOrder).filter_by(order_no=order_no).first()
            if not order:
                raise ValueError(f"采购单不存在: {order_no}")
            if order.status != "received":
                raise ValueError(f"采购单状态非 received，无法退货: 当前={order.status}")
            if qty <= 0:
                raise ValueError(f"退货数量必须为正数: qty={qty}")
            received = order.received_qty or order.qty
            if qty > received:
                raise ValueError(f"退货数量超过已收货数量: 本次={qty} 已收={received}")

            # P1-5: 加行锁 SELECT FOR UPDATE，防止并发退货库存错乱
            # with_for_update() 在 SQLite 下被忽略，PostgreSQL 下生效
            product = self.db.query(Product).filter_by(id=order.product_id).with_for_update().first()
            if not product:
                raise ValueError(f"产品不存在: {order.product_id}")
            if product.stock_qty < qty:
                raise ValueError(f"库存不足无法退货: 当前={product.stock_qty} 退货={qty}")

            # 减少库存
            product.stock_qty -= qty

            # 出库流水（return_out，负数表示出库）
            move = StockMove(
                product_id=product.id,
                qty=-qty,
                move_type="return_out",
                ref_order=order.order_no,
                warehouse_id=order.warehouse_id,
            )
            self.db.add(move)

            # 红字应付凭证（负数金额冲销原应付）
            amount = self._to_decimal(qty) * self._to_decimal(order.unit_price)
            tax_rate = self._to_decimal(order.tax_rate) if order.tax_rate is not None else Decimal(0)
            tax_amount = (amount * tax_rate).quantize(Decimal("0.01"))
            currency_code = order.currency_code or "CNY"
            # P1-9: 从 product 继承 cost_center_id 写入凭证（支持预算实际金额回填）
            acc = AccountMove(
                ref_type="payable",
                ref_order=order.order_no,
                amount=-amount,
                account="应付账款",
                tax_amount=-tax_amount,
                currency_code=currency_code,
                cost_center_id=product.cost_center_id,
            )
            self.db.add(acc)

            # 状态置 returned
            order.status = "returned"

            self._audit(
                actor,
                "return",
                "purchase_order",
                order.id,
                detail=f"采购退货 {order.order_no} qty={qty} reason={reason} "
                f"红字应付凭证 -{amount} 税额 -{tax_amount} currency={currency_code}",
                ip=ip,
            )
            self.db.commit()
            logger.info("采购退货完成: %s qty=%s", order.order_no, qty)
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception("采购退货失败: %s", e)
            raise

    # ==================== 库存盘点（P1: cycle count）====================

    def create_stock_take(self, product_id, warehouse_id=None, actor="system", ip=None):
        """创建盘点单（对应 Odoo stock.inventory）

        记录当前系统库存 system_qty，等待人工录入 actual_qty 后确认。
        状态：draft → confirmed。

        :param product_id: 盘点产品 ID
        :param warehouse_id: 盘点仓库 ID（可选，None 表示总库存盘点）
        :param actor: 操作人
        :param ip: 操作 IP
        :return: StockTake 对象
        :raises ValueError: 产品不存在时抛出
        """
        try:
            product = self.db.get(Product, product_id)
            if not product:
                raise ValueError(f"产品不存在: {product_id}")

            # 记录系统库存：若指定仓库则聚合 StockMove，否则用 Product.stock_qty
            if warehouse_id:
                system_qty = (
                    self.db.query(db.func.sum(StockMove.qty))
                    .filter(
                        StockMove.product_id == product_id,
                        StockMove.warehouse_id == warehouse_id,
                    )
                    .scalar()
                ) or 0
            else:
                system_qty = product.stock_qty or 0

            take = StockTake(
                product_id=product_id,
                warehouse_id=warehouse_id,
                system_qty=int(system_qty),
                actual_qty=int(system_qty),  # 默认实际=系统，待人工修改
                diff=0,
                status="draft",
            )
            self.db.add(take)
            self._audit(
                actor,
                "create",
                "stock_take",
                None,
                detail=f"创建盘点单 product={product_id} warehouse={warehouse_id} system_qty={system_qty}",
                ip=ip,
            )
            self.db.commit()
            logger.info("盘点单创建: product=%s system_qty=%s", product_id, system_qty)
            return take
        except Exception as e:
            self.db.rollback()
            logger.exception("创建盘点单失败: %s", e)
            raise

    def confirm_stock_take(self, take_id, actual_qty, actor="system", ip=None):
        """确认盘点单：计算差异并调整库存（对应 Odoo stock.inventory.action_done）

        流程：
            - 校验盘点单存在且状态为 draft
            - 计算 diff = actual_qty - system_qty
            - 加行锁更新 Product.stock_qty
            - 生成 StockMove(adjust，正/负) 记录差异调整
            - 状态置 confirmed

        :param take_id: 盘点单 ID
        :param actual_qty: 实际盘点数量
        :param actor: 操作人
        :param ip: 操作 IP
        :return: StockTake 对象
        :raises ValueError: 盘点单不存在 / 状态非 draft / 实际数量非整数时抛出
        """
        try:
            if not isinstance(actual_qty, int) or actual_qty < 0:
                raise ValueError(f"实际盘点数量必须为非负整数: actual_qty={actual_qty}")

            take = self.db.get(StockTake, take_id)
            if not take:
                raise ValueError(f"盘点单不存在: {take_id}")
            if take.status != "draft":
                raise ValueError(f"盘点单状态非 draft，不可确认: 当前={take.status}")

            # P1-5: 加行锁防止并发调整库存
            product = self.db.query(Product).filter_by(id=take.product_id).with_for_update().first()
            if not product:
                raise ValueError(f"产品不存在: {take.product_id}")

            diff = actual_qty - take.system_qty
            take.actual_qty = actual_qty
            take.diff = diff

            # 仅当差异不为 0 时调整库存 + 生成流水
            if diff != 0:
                product.stock_qty = (product.stock_qty or 0) + diff
                move = StockMove(
                    product_id=take.product_id,
                    qty=diff,
                    move_type="adjust",
                    ref_order=f"STK-{take.id}",
                    warehouse_id=take.warehouse_id,
                )
                self.db.add(move)

            take.status = "confirmed"
            self._audit(
                actor,
                "confirm",
                "stock_take",
                take.id,
                detail=f"确认盘点单 product={take.product_id} system={take.system_qty} "
                f"actual={actual_qty} diff={diff}",
                ip=ip,
            )
            self.db.commit()
            logger.info(
                "盘点单确认: id=%s product=%s system=%s actual=%s diff=%s",
                take_id,
                take.product_id,
                take.system_qty,
                actual_qty,
                diff,
            )
            return take
        except Exception as e:
            self.db.rollback()
            logger.exception("确认盘点单失败: %s", e)
            raise

    def list_stock_takes(self, product_id=None, status=None, limit=50):
        """查询盘点单列表

        :param product_id: 按产品过滤（可选）
        :param status: 按状态过滤（draft/confirmed，可选）
        :param limit: 返回条数上限（默认 50，最大 200）
        :return: list[dict]
        """
        limit = max(1, min(limit, 200))
        query = self.db.query(StockTake)
        if product_id:
            query = query.filter_by(product_id=product_id)
        if status:
            query = query.filter_by(status=status)
        rows = query.order_by(StockTake.id.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "product_id": r.product_id,
                "warehouse_id": r.warehouse_id,
                "system_qty": r.system_qty,
                "actual_qty": r.actual_qty,
                "diff": r.diff,
                "status": r.status,
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else None,
                "updated_at": r.updated_at.strftime("%Y-%m-%d %H:%M:%S") if r.updated_at else None,
            }
            for r in rows
        ]
