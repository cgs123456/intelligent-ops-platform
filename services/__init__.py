"""services 包初始化"""
from .aigc_service import AIGCService
from .closed_loop import ClosedLoop
from .erp_service import ERPService
from .rpa_service import RPAService
from .warehouse_service import WarehouseService

__all__ = ['ERPService', 'RPAService', 'WarehouseService', 'AIGCService', 'ClosedLoop']
