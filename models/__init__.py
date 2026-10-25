"""models 包初始化 · 按层组织数据模型"""

# 多租户基础设施必须最先导入（其余模型 import TenantScoped）
from .aigc import ChatHistory, DailyReport, Suggestion, SuggestionFeedback
from .bom import BOM, BOMItem, MRPPlan, MRPRequirement
from .erp import (
    AccountingPeriod,
    AccountMove,
    Budget,
    CostCenter,
    Customer,
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
from .external import ExtEcommerceOrder, ExtSupplierQuote
from .finance import ReconciliationBatch, ReconciliationEntry
from .system import AuditLog, LoopState, Role, TokenBlacklist, User, user_roles
from .tenant import DEFAULT_TENANT_ID, Tenant, TenantScoped
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
    "AccountingPeriod",
    "ReturnOrder",
    # P1-9 ERP 业务模块扩展
    "StockLot",
    "ReorderRule",
    "CostCenter",
    "Budget",
    # P2-21 / P2-27 ERP 业务模块扩展
    "ExchangeRate",
    "StockTake",
    "Customer",
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
    "TokenBlacklist",
    "user_roles",
    # 财务对账（P1-11）
    "ReconciliationBatch",
    "ReconciliationEntry",
    # BOM / MRP（P1-9）
    "BOM",
    "BOMItem",
    "MRPPlan",
    "MRPRequirement",
    # 会计期间 / 库位（P3）
    "AccountingPeriod",
    "StockLocation",
    # 多租户基础设施
    "Tenant",
    "TenantScoped",
    "DEFAULT_TENANT_ID",
]
