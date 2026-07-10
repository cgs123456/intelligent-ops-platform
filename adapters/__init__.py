"""适配器抽象层

设计目标：把"模拟实现"与"业务接口"解耦，便于从原型演进到生产。

当前提供：
- RPABackend：抽象 RPA 采集后端，MockBackend（当前模拟）+ SeleniumBackend（占位）
- LLMBackend：抽象 LLM 调用后端，MockBackend（rule）+ OpenAICompatibleBackend（GLM/Qwen）

切换方式：
    from adapters import get_rpa_backend, get_llm_backend
    rpa = get_rpa_backend()  # 根据 RPA_BACKEND 环境变量自动选择
    rpa.login('supplier_site')

新增真实实现：在 adapters/rpa_backends/selenium_backend.py 实现.SeleniumBackend 类，
然后在 RPA_BACKEND 环境变量配置 selenium 即可。
"""
from .llm_backend import LLMBackend, get_llm_backend
from .rpa_backend import RPABackend, get_rpa_backend

__all__ = [
    'RPABackend', 'get_rpa_backend',
    'LLMBackend', 'get_llm_backend',
]
