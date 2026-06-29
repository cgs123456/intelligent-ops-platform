"""种子数据初始化 · 含多仓库、供应商联系人、电商订单（带重试字段）"""
from datetime import datetime, timedelta, date
import random
import logging
from extensions import db
from models.erp import (Product, Supplier, Warehouse, SaleOrder, StockMove)
from models.external import ExtSupplierQuote, ExtEcommerceOrder

logger = logging.getLogger(__name__)


def seed_all(app):
    with app.app_context():
        if Product.query.first():
            return False

        # 仓库
        warehouses = [
            Warehouse(code='WH-01', name='主仓库', location='深圳'),
            Warehouse(code='WH-02', name='华东仓', location='上海'),
        ]
        db.session.add_all(warehouses)
        db.session.flush()

        # 产品（含 avg_cost / category）
        products = [
            Product(sku='SKU-001', name='无线蓝牙耳机', cost_price=80, sale_price=199,
                    stock_qty=45, safety_stock=30, avg_cost=80, category='数码'),
            Product(sku='SKU-002', name='便携充电宝', cost_price=45, sale_price=129,
                    stock_qty=18, safety_stock=25, avg_cost=45, category='数码'),
            Product(sku='SKU-003', name='手机保护壳', cost_price=8, sale_price=39,
                    stock_qty=120, safety_stock=50, avg_cost=8, category='配件'),
            Product(sku='SKU-004', name='USB-C数据线', cost_price=5, sale_price=25,
                    stock_qty=60, safety_stock=40, avg_cost=5, category='配件'),
            Product(sku='SKU-005', name='智能手环', cost_price=120, sale_price=299,
                    stock_qty=12, safety_stock=20, avg_cost=120, category='数码'),
            Product(sku='SKU-006', name='蓝牙音箱', cost_price=95, sale_price=249,
                    stock_qty=28, safety_stock=25, avg_cost=95, category='数码'),
        ]
        db.session.add_all(products)
        db.session.flush()

        # 供应商
        suppliers = [
            Supplier(name='华南电子供应商', lead_days=3, rating='A', contact='陈经理', phone='138xxxx0001'),
            Supplier(name='华东科技贸易', lead_days=5, rating='B', contact='李经理', phone='139xxxx0002'),
            Supplier(name='深圳智造工厂', lead_days=2, rating='A', contact='王经理', phone='137xxxx0003'),
        ]
        db.session.add_all(suppliers)
        db.session.flush()

        # 外部供应商报价
        quote_data = [
            (1, 1, 78), (1, 2, 43), (1, 4, 4.5), (1, 6, 92),
            (2, 1, 82), (2, 3, 7.5), (2, 5, 118), (2, 6, 96),
            (3, 2, 44), (3, 3, 7), (3, 4, 4.2), (3, 5, 115),
        ]
        for sid, pid, price in quote_data:
            db.session.add(ExtSupplierQuote(
                supplier_id=sid, product_id=pid,
                quote_price=price, quote_date=date.today(),
                source=f'供应商{sid}官网'
            ))

        # 外部电商订单（过去7天）
        platforms = ['淘宝', '京东', '抖店']
        customers = ['张三', '李四', '王五', '赵六', '钱七', '孙八', '周九', '吴十']
        now = datetime.now()
        for days_ago in range(7, 0, -1):
            order_date = now - timedelta(days=days_ago)
            for _ in range(random.randint(5, 10)):
                p = random.choice(products)
                db.session.add(ExtEcommerceOrder(
                    platform=random.choice(platforms),
                    product_sku=p.sku,
                    qty=random.randint(1, 5),
                    unit_price=p.sale_price * random.uniform(0.9, 1.0),
                    customer=random.choice(customers),
                    order_time=order_date,
                    synced=False, retry_count=0
                ))

        # ERP 历史销售单
        for days_ago in range(3, 0, -1):
            order_date = now - timedelta(days=days_ago)
            for _ in range(random.randint(3, 6)):
                p = random.choice(products)
                db.session.add(SaleOrder(
                    order_no=f'SO-{order_date.strftime("%Y%m%d")}-{random.randint(1000,9999)}',
                    product_id=p.id, qty=random.randint(1, 4),
                    unit_price=p.sale_price, customer=random.choice(customers),
                    platform='线下', status='confirmed', warehouse_id=1,
                    created_at=order_date
                ))

        db.session.commit()
        logger.info('种子数据已加载：6 SKU + 3 供应商 + 2 仓库 + 外部报价 + 7天电商订单')
        return True
