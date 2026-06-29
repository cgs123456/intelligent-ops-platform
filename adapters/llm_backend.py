"""LLM 后端抽象 + 多实现

设计目的：让 AIGCService 不直接耦合具体 LLM 厂商 API，
便于在 rule（无 LLM）/ glm / qwen / openai 兼容协议间切换。

接口契约：
- call(messages, temperature=0.3) -> Optional[str]
- available() -> bool

切换实现：
    LLM_PROVIDER=rule    默认，无 LLM 调用，由 AIGCService 走规则引擎
    LLM_PROVIDER=glm     智谱 GLM
    LLM_PROVIDER=qwen    通义千问
    LLM_PROVIDER=openai  OpenAI 兼容协议（vLLM/Ollama/LM Studio 等）
"""
import os
import logging
from abc import ABC, abstractmethod
from flask import current_app

logger = logging.getLogger(__name__)


class LLMBackend(ABC):
    """LLM 后端抽象基类"""

    @abstractmethod
    def call(self, messages: list, temperature: float = 0.3):
        """调用 LLM，返回文本或 None"""

    @abstractmethod
    def available(self) -> bool:
        """是否可用"""


class RuleLLMBackend(LLMBackend):
    """规则引擎后端：无 LLM 调用，永远返回 None（由 AIGCService 走规则兜底）"""

    def call(self, messages, temperature=0.3):
        return None

    def available(self) -> bool:
        return False


class OpenAICompatibleBackend(LLMBackend):
    """OpenAI 兼容协议后端（GLM/Qwen/OpenAI/vLLM 等）

    适用于所有遵循 OpenAI Chat Completions API 协议的服务：
        POST {LLM_API_URL}
        Headers: Authorization: Bearer {LLM_API_KEY}
        Body: {"model": "...", "messages": [...], "temperature": ...}
        Response: {"choices": [{"message": {"content": "..."}}]}
    """
    provider_name = 'openai-compatible'

    def __init__(self):
        self.api_key = current_app.config.get('LLM_API_KEY', '')
        self.api_url = current_app.config.get('LLM_API_URL', '')
        self.model = current_app.config.get('LLM_MODEL', 'glm-4-flash')

    def available(self) -> bool:
        return bool(self.api_key) and bool(self.api_url)

    def call(self, messages, temperature=0.3):
        if not self.available():
            logger.warning('[LLM-%s] api_key 或 api_url 未配置', self.provider_name)
            return None
        try:
            import requests
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
            }
            payload = {
                'model': self.model,
                'messages': messages,
                'temperature': temperature,
            }
            resp = requests.post(self.api_url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data['choices'][0]['message']['content']
        except Exception as e:
            logger.warning('[LLM-%s] 调用失败 err=%s，回退规则引擎',
                           self.provider_name, e)
            return None


class GLMBackend(OpenAICompatibleBackend):
    provider_name = 'glm'


class QwenBackend(OpenAICompatibleBackend):
    provider_name = 'qwen'


def get_llm_backend() -> LLMBackend:
    """根据 LLM_PROVIDER 环境变量返回对应后端实例"""
    provider = current_app.config.get('LLM_PROVIDER', 'rule').lower()
    if provider == 'glm':
        return GLMBackend()
    if provider == 'qwen':
        return QwenBackend()
    if provider == 'openai':
        return OpenAICompatibleBackend()
    if provider == 'rule':
        return RuleLLMBackend()
    logger.warning('未知 LLM_PROVIDER=%s，回退 rule', provider)
    return RuleLLMBackend()
