"""models 包初始化 · 按层组织数据模型"""
from .erp import (Product, Supplier, Warehouse, PurchaseOrder, SaleOrder,
                  StockMove, AccountMove, ReturnOrder)
from .external import ExtSupplierQuote, ExtEcommerceOrder
from .warehouse import (OdsSaleOrder, OdsPurchaseOrder, OdsStockMove,
                        DwdSalesFact, DwdStockFact,
                        DwsSalesSkuDaily, DwsInventoryDaily,
                        AdsReplenishmentSuggest, AdsDailyOpsReport,
                        EtlMeta, DataQualityLog, DataLineage)
from .aigc import Suggestion, DailyReport, ChatHistory, SuggestionFeedback
from .system import LoopState, AuditLog, User, Role, user_roles

__all__ = [
    # ERP
    'Product', 'Supplier', 'Warehouse', 'PurchaseOrder', 'SaleOrder',
    'StockMove', 'AccountMove', 'ReturnOrder',
    # 外部数据源
    'ExtSupplierQuote', 'ExtEcommerceOrder',
    # FDE 数仓
    'OdsSaleOrder', 'OdsPurchaseOrder', 'OdsStockMove',
    'DwdSalesFact', 'DwdStockFact', 'DwsSalesSkuDaily', 'DwsInventoryDaily',
    'AdsReplenishmentSuggest', 'AdsDailyOpsReport',
    'EtlMeta', 'DataQualityLog', 'DataLineage',
    # AIGC
    'Suggestion', 'DailyReport', 'ChatHistory', 'SuggestionFeedback',
    # 系统
    'LoopState', 'AuditLog', 'User', 'Role', 'user_roles',
]
