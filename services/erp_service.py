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
import random
import logging
from datetime import datetime
from decimal import Decimal

from extensions import db
from models.erp import (
    Product, Supplier, Warehouse,
    PurchaseOrder, SaleOrder, StockMove, AccountMove, ReturnOrder,
)
from models.system import AuditLog

logger = logging.getLogger(__name__)


class ERPService:
    """ERP 进销存 + 财务服务（实例方法风格，支持外部传入 db_session）"""

    def __init__(self, db_session=None):
        # 允许外部注入会话（便于测试/分库），默认用全局 db.session
        self.db = db_session or db.session

    # ==================== 内部工具 ====================

    def _audit(self, actor, action, target_type, target_id, detail='', ip=None):
        """写审计日志（对应 Odoo auditlog 模块，所有关键操作留痕）"""
        log = AuditLog(
            actor=actor or 'system',
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
        """生成带随机后缀的单号，避免高并发冲突"""
        return f'{prefix}-{datetime.now().strftime("%Y%m%d%H%M%S")}-{random.randint(1000, 9999)}'

    # ==================== 采购管理 ====================

    def create_purchase_order(self, supplier_id, product_id, qty, unit_price,
                              warehouse_id=None, ext_order_no=None,
                              suggestion_id=None, actor='system', ip=None):
        """创建采购单（对应 Odoo purchase.order 创建，状态 draft）"""
        try:
            order = PurchaseOrder(
                order_no=self._gen_order_no('PO'),
                supplier_id=supplier_id,
                product_id=product_id,
                qty=qty,
                unit_price=unit_price,
                status='draft',
                ext_order_no=ext_order_no,
                suggestion_id=suggestion_id,
                warehouse_id=warehouse_id,
            )
            self.db.add(order)
            self.db.flush()  # 取 order.id
            self._audit(actor, 'create', 'purchase_order', order.id,
                        detail=f'创建采购单 {order.order_no} supplier={supplier_id} '
                               f'product={product_id} qty={qty} price={unit_price}', ip=ip)
            self.db.commit()
            logger.info('采购单已创建: %s', order.order_no)
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception('创建采购单失败: %s', e)
            raise

    def confirm_purchase_order(self, order_id, ext_order_no=None,
                              actor='system', ip=None):
        """确认采购单（对应 Odoo purchase.button_confirm）：draft -> confirmed，生成应付凭证
        - ext_order_no: 可选，RPA 场景下回填外部平台订单号（如 1688/京东 采购单号）
        """
        try:
            order = self.db.get(PurchaseOrder, order_id)
            if not order:
                raise ValueError(f'采购单不存在: {order_id}')
            if order.status != 'draft':
                raise ValueError(f'采购单状态非 draft，无法确认: 当前={order.status}')

            order.status = 'confirmed'
            order.confirmed_at = datetime.now()
            if ext_order_no:
                order.ext_order_no = ext_order_no

            # 生成应付凭证（对应 Odoo account.move payable）
            amount = Decimal(order.qty) * Decimal(order.unit_price)
            acc = AccountMove(
                ref_type='payable',
                ref_order=order.order_no,
                amount=amount,
                account='应付账款',
            )
            self.db.add(acc)

            self._audit(actor, 'confirm', 'purchase_order', order.id,
                        detail=f'确认采购单 {order.order_no} 生成应付凭证 {amount}'
                               f'{f" ext_order_no={ext_order_no}" if ext_order_no else ""}',
                        ip=ip)
            self.db.commit()
            logger.info('采购单已确认: %s', order.order_no)
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception('确认采购单失败: %s', e)
            raise

    def receive_purchase_order(self, order_id, actor='system', ip=None):
        """到货入库（对应 Odoo purchase.button_done + stock.picking 入库）
        - 更新 Product.stock_qty
        - 按移动加权平均法更新 Product.avg_cost
        - 生成入库流水 StockMove(in)
        """
        try:
            order = self.db.get(PurchaseOrder, order_id)
            if not order:
                raise ValueError(f'采购单不存在: {order_id}')
            if order.status != 'confirmed':
                raise ValueError(f'采购单状态非 confirmed，无法入库: 当前={order.status}')

            product = self.db.get(Product, order.product_id)
            if not product:
                raise ValueError(f'产品不存在: {order.product_id}')

            # 移动加权平均成本：avg_cost = (原库存*原avg_cost + 入库数量*采购单价) / (原库存+入库数量)
            old_qty = Decimal(product.stock_qty or 0)
            old_avg = Decimal(product.avg_cost or 0)
            in_qty = Decimal(order.qty)
            in_price = Decimal(order.unit_price)
            new_qty = old_qty + in_qty
            if new_qty > 0:
                new_avg = (old_qty * old_avg + in_qty * in_price) / new_qty
            else:
                new_avg = old_avg

            product.stock_qty = int(new_qty)
            product.avg_cost = new_avg

            # 入库流水（对应 Odoo stock.move in）
            move = StockMove(
                product_id=product.id,
                qty=int(in_qty),  # 正数=入库
                move_type='in',
                ref_order=order.order_no,
                warehouse_id=order.warehouse_id,
            )
            self.db.add(move)

            order.status = 'received'
            order.received_at = datetime.now()

            self._audit(actor, 'receive', 'purchase_order', order.id,
                        detail=f'采购入库 {order.order_no} qty={order.qty} '
                               f'avg_cost {old_avg}->{new_avg}', ip=ip)
            self.db.commit()
            logger.info('采购入库完成: %s avg_cost=%s', order.order_no, new_avg)
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception('采购入库失败: %s', e)
            raise

    def cancel_purchase_order(self, order_id, actor='system', ip=None):
        """取消采购单（对应 Odoo purchase.button_cancel）：置 cancelled 状态"""
        try:
            order = self.db.get(PurchaseOrder, order_id)
            if not order:
                raise ValueError(f'采购单不存在: {order_id}')
            if order.status == 'received':
                raise ValueError('采购单已入库，无法取消')
            order.status = 'cancelled'
            self._audit(actor, 'cancel', 'purchase_order', order.id,
                        detail=f'取消采购单 {order.order_no}', ip=ip)
            self.db.commit()
            logger.info('采购单已取消: %s', order.order_no)
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception('取消采购单失败: %s', e)
            raise

    # ==================== 销售管理 ====================

    def create_sale_order(self, product_id, qty, unit_price, customer,
                          warehouse_id=None, platform='线下',
                          actor='system', ip=None):
        """创建销售单（对应 Odoo sale.order 创建 + 确认 + 出库）
        - 扣减库存
        - 出库流水 StockMove(out)
        - 应收凭证 AccountMove(receivable)
        - 按 avg_cost 结转成本（COGS 记入审计）
        """
        try:
            product = self.db.get(Product, product_id)
            if not product:
                raise ValueError(f'产品不存在: {product_id}')
            if product.stock_qty < qty:
                raise ValueError(f'库存不足: 当前={product.stock_qty} 需要={qty}')

            # 扣减库存
            product.stock_qty -= qty

            order = SaleOrder(
                order_no=self._gen_order_no('SO'),
                product_id=product_id,
                qty=qty,
                unit_price=unit_price,
                customer=customer,
                platform=platform,
                status='confirmed',
                warehouse_id=warehouse_id,
            )
            self.db.add(order)
            self.db.flush()

            # 出库流水（对应 Odoo stock.move out，负数=出库）
            move = StockMove(
                product_id=product_id,
                qty=-qty,
                move_type='out',
                ref_order=order.order_no,
                warehouse_id=warehouse_id,
            )
            self.db.add(move)

            # 应收凭证（对应 Odoo account.move receivable）
            receivable = Decimal(qty) * Decimal(unit_price)
            acc = AccountMove(
                ref_type='receivable',
                ref_order=order.order_no,
                amount=receivable,
                account='应收账款',
            )
            self.db.add(acc)

            # 按 avg_cost 结转成本（COGS），记入审计
            cogs = Decimal(qty) * Decimal(product.avg_cost or 0)

            self._audit(actor, 'order', 'sale_order', order.id,
                        detail=f'创建销售单 {order.order_no} customer={customer} '
                               f'qty={qty} receivable={receivable} cogs={cogs}', ip=ip)
            self.db.commit()
            logger.info('销售单已创建: %s receivable=%s cogs=%s',
                        order.order_no, receivable, cogs)
            return order
        except Exception as e:
            self.db.rollback()
            logger.exception('创建销售单失败: %s', e)
            raise

    # ==================== 退货管理 ====================

    def create_return_order(self, original_order_no, product_id, qty, refund_amount,
                            reason=None, actor='system', ip=None):
        """创建退货单（对应 Odoo sale 退货 + 红字凭证）
        - 库存回增
        - 退货入库流水 StockMove(return_in)
        - 红字应收凭证 AccountMove(refund)
        - ReturnOrder 记录
        """
        try:
            product = self.db.get(Product, product_id)
            if not product:
                raise ValueError(f'产品不存在: {product_id}')

            # 库存回增
            product.stock_qty += qty

            return_no = self._gen_order_no('RO')
            ret = ReturnOrder(
                return_no=return_no,
                original_order_no=original_order_no,
                product_id=product_id,
                qty=qty,
                refund_amount=refund_amount,
                reason=reason,
                status='confirmed',
            )
            self.db.add(ret)
            self.db.flush()

            # 退货入库流水（对应 Odoo stock.move return_in，正数=入库）
            move = StockMove(
                product_id=product_id,
                qty=qty,
                move_type='return_in',
                ref_order=return_no,
            )
            self.db.add(move)

            # 红字应收凭证（对应 Odoo account.move refund）
            acc = AccountMove(
                ref_type='refund',
                ref_order=return_no,
                amount=Decimal(refund_amount),
                account='应收账款',
            )
            self.db.add(acc)

            self._audit(actor, 'return', 'return_order', ret.id,
                        detail=f'创建退货单 {return_no} 原单={original_order_no} '
                               f'qty={qty} refund={refund_amount} reason={reason}', ip=ip)
            self.db.commit()
            logger.info('退货单已创建: %s', return_no)
            return ret
        except Exception as e:
            self.db.rollback()
            logger.exception('创建退货单失败: %s', e)
            raise

    # ==================== 库存调拨 ====================

    def transfer_stock(self, product_id, qty, from_warehouse_id, to_warehouse_id,
                       actor='system', ip=None):
        """库存调拨（对应 Odoo stock.move internal transfer）：warehouse 间转移库存
        Product.stock_qty 为总库存（不随调拨变化），仅记录调拨流水影响仓库级余额。
        生成两条 transfer 流水：源仓库出库(负)、目标仓库入库(正)。
        """
        try:
            if from_warehouse_id == to_warehouse_id:
                raise ValueError('源仓库与目标仓库相同')

            # 源仓库出库流水
            out_move = StockMove(
                product_id=product_id,
                qty=-qty,
                move_type='transfer',
                ref_order=f'TR-{datetime.now().strftime("%Y%m%d%H%M%S")}-{random.randint(1000, 9999)}',
                warehouse_id=from_warehouse_id,
            )
            # 目标仓库入库流水
            in_move = StockMove(
                product_id=product_id,
                qty=qty,
                move_type='transfer',
                ref_order=out_move.ref_order,
                warehouse_id=to_warehouse_id,
            )
            self.db.add_all([out_move, in_move])
            self.db.flush()

            self._audit(actor, 'transfer', 'stock_move', out_move.id,
                        detail=f'调拨 product={product_id} qty={qty} '
                               f'from_wh={from_warehouse_id} to_wh={to_warehouse_id} '
                               f'ref={out_move.ref_order}', ip=ip)
            self.db.commit()
            logger.info('库存调拨完成: %s -> %s qty=%s',
                        from_warehouse_id, to_warehouse_id, qty)
            return out_move.ref_order
        except Exception as e:
            self.db.rollback()
            logger.exception('库存调拨失败: %s', e)
            raise

    # ==================== 查询接口 ====================

    def get_inventory_summary(self):
        """库存汇总（对应 Odoo product.product 库存视图）：SKU 级库存 + 价值 + 安全库存预警"""
        products = self.db.query(Product).filter(Product.is_active.is_(True)).all()
        result = []
        for p in products:
            stock_value = Decimal(p.stock_qty or 0) * Decimal(p.avg_cost or 0)
            result.append({
                'id': p.id,
                'sku': p.sku,
                'name': p.name,
                'category': p.category,
                'stock_qty': p.stock_qty,
                'avg_cost': float(p.avg_cost or 0),
                'stock_value': float(stock_value),
                'safety_stock': p.safety_stock,
                'is_below_safety': p.stock_qty < p.safety_stock,
            })
        return result

    def get_recent_orders(self, limit=20):
        """近期采购单（对应 Odoo purchase.order 列表）
        修复 N+1：先取所有 supplier_id，再用 Supplier.id.in_(ids) 批量预取建 map，避免逐单查供应商。
        """
        orders = (
            self.db.query(PurchaseOrder)
            .order_by(PurchaseOrder.created_at.desc())
            .limit(limit)
            .all()
        )
        if not orders:
            return []

        # 批量预取供应商，建 id -> supplier map（修复 N+1 查询）
        supplier_ids = {o.supplier_id for o in orders}
        suppliers = (
            self.db.query(Supplier)
            .filter(Supplier.id.in_(supplier_ids))
            .all()
        )
        supplier_map = {s.id: s for s in suppliers}

        result = []
        for o in orders:
            sup = supplier_map.get(o.supplier_id)
            result.append({
                'id': o.id,
                'order_no': o.order_no,
                'supplier_id': o.supplier_id,
                'supplier_name': sup.name if sup else None,
                'supplier_contact': sup.contact if sup else None,
                'supplier_phone': sup.phone if sup else None,
                'product_id': o.product_id,
                'qty': o.qty,
                'unit_price': float(o.unit_price),
                'amount': float(o.qty * o.unit_price),
                'status': o.status,
                'warehouse_id': o.warehouse_id,
                'ext_order_no': o.ext_order_no,
                'created_at': o.created_at.strftime('%Y-%m-%d %H:%M:%S') if o.created_at else None,
            })
        return result

    def get_account_summary(self):
        """会计汇总（对应 Odoo account.move 聚合）：应付 / 应收 / 红字退款 余额"""
        rows = (
            self.db.query(
                AccountMove.ref_type,
                db.func.sum(AccountMove.amount),
            )
            .group_by(AccountMove.ref_type)
            .all()
        )
        summary = {'payable': 0.0, 'receivable': 0.0, 'refund': 0.0}
        for ref_type, total in rows:
            if ref_type in summary:
                summary[ref_type] = float(total or 0)
        # 净应收 = 应收 - 退款
        summary['net_receivable'] = summary['receivable'] - summary['refund']
        return summary

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
            summary.append({
                'warehouse_id': wh_id,
                'warehouse_code': wh.code if wh else None,
                'warehouse_name': wh.name if wh else '未指定',
                'net_stock': int(qty_sum or 0),
            })
        # 按库存降序
        summary.sort(key=lambda x: x['net_stock'], reverse=True)
        return summary
