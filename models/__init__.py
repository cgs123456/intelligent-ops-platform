"""models 包初始化 · 按层组织数据模型"""

from .aigc import ChatHistory, DailyReport, Suggestion, SuggestionFeedback
from .erp import (
    AccountMove,
    Budget,
    CostCenter,
    Product,
    PurchaseOrder,
    ReorderRule,
    ReturnOrder,
    SaleOrder,
    StockLot,
    StockMove,
    Supplier,
    Warehouse,
)
from .external import ExtEcommerceOrder, ExtSupplierQuote
from .system import AuditLog, LoopState, Role, User, user_roles
from .warehouse import (
    AdsDailyOpsReport,
    AdsReplenishmentSuggest,
    DataLineage,
    DataQualityLog,
    DwdSalesFact,
    DwdStockFact,
    DwsInventoryDaily,
    DwsSalesSkuDaily,
    EtlMeta,
    OdsPurchaseOrder,
    OdsSaleOrder,
    OdsStockMove,
)

__all__ = [
    # ERP
    "Product",
    "Supplier",
    "Warehouse",
    "PurchaseOrder",
    "SaleOrder",
    "StockMove",
    "AccountMove",
    "ReturnOrder",
    # P1-9 ERP 业务模块扩展
    "StockLot",
    "ReorderRule",
    "CostCenter",
    "Budget",
    # 外部数据源
    "ExtSupplierQuote",
    "ExtEcommerceOrder",
    # FDE 数仓
    "OdsSaleOrder",
    "OdsPurchaseOrder",
    "OdsStockMove",
    "DwdSalesFact",
    "DwdStockFact",
    "DwsSalesSkuDaily",
    "DwsInventoryDaily",
    "AdsReplenishmentSuggest",
    "AdsDailyOpsReport",
    "EtlMeta",
    "DataQualityLog",
    "DataLineage",
    # AIGC
    "Suggestion",
    "DailyReport",
    "ChatHistory",
    "SuggestionFeedback",
    # 系统
    "LoopState",
    "AuditLog",
    "User",
    "Role",
    "user_roles",
]
