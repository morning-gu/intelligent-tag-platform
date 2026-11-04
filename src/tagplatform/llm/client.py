"""OpenAI 兼容 LLM 客户端：transport 可注入便于测试，openai SDK 懒导入。

不做输出解析——解析在 llm/parsing.py（任务 5），重试解析在任务执行器
（任务 8）。本层只负责：传输、重试、response_format 降级、限速间隔、
usage 记录。不做响应磁盘缓存（YAGNI，接口允许后加）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable

from tagplatform.settings import Settings

Transport = Callable[[str, list[dict], float, dict | None, dict | None, int | None], dict]


@dataclass
class LLMResponse:
    content: str
    usage: dict
    raw: dict
    ok: bool


class OpenAIClient:
    def __init__(self, model: str, base_url_env: str = "OPENAI_BASE_URL",
                 api_key_env: str = "OPENAI_API_KEY", temperature: float = 0.7,
                 request_interval: float = 0.5, transport: Transport | None = None,
                 max_retries: int = 3, extra_body: dict | None = None,
                 max_tokens: int | None = None):
        self.model = model
        self.base_url_env = base_url_env
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.request_interval = request_interval
        self.max_retries = max_retries
        self.extra_body = dict(extra_body) if extra_body else {}
        # 输出上限：推理模型偶尔会把思考写爆（实测一次复核烧了 27k tokens），
        # 只在需要卡住延迟的调用点设置，生成主链不设
        self.max_tokens = max_tokens
        self._transport = transport

    def _build_transport(self) -> Transport:
        """按环境变量构建真实 SDK transport（仅首次 complete 且未注入时调用）。"""
        base_url = os.environ.get(self.base_url_env)
        api_key = os.environ.get(self.api_key_env)
        if not base_url or not api_key:
            raise RuntimeError(
                f"环境变量 {self.base_url_env} / {self.api_key_env} 未设置"
                "（复制 .env.example 为 .env 并填入）")
        from openai import OpenAI  # 懒导入：mock/测试路径不需要安装后首次 import 的开销

        sdk = OpenAI(base_url=base_url, api_key=api_key)

        def transport(model: str, messages: list[dict], temperature: float,
                      response_format: dict | None, extra_body: dict | None,
                      max_tokens: int | None = None) -> dict:
            kwargs: dict = {"model": model, "messages": messages,
                            "temperature": temperature}
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            if response_format is not None:
                kwargs["response_format"] = response_format
            if extra_body:
                kwargs["extra_body"] = extra_body
            return sdk.chat.completions.create(**kwargs).model_dump()

        return transport

    def complete(self, messages: list[dict], run_id: str) -> LLMResponse:
        """单次补全：run_id 仅用于日志语境（缓存键在 pipeline 层构造）。

        重试语义：json_object 不被端点支持 → 降级为无 response_format 重试
        （不耗退避）；其他传输异常 → 指数退避重试至多 max_retries 次，仍失败
        返回 ok=False（不抛异常，由上层决定任务失败语义）。
        """
        if self._transport is None:
            self._transport = self._build_transport()
        time.sleep(self.request_interval)

        last_err: Exception | None = None
        use_json = True
        for attempt in range(self.max_retries):
            try:
                return self._call(self._transport, messages, allow_json=use_json)
            except Exception as e:  # noqa: BLE001 —— 传输层任何异常统一处理
                if use_json and _detect_json_format_error(e):
                    use_json = False   # 端点不支持 json_object → 降级后重试
                    continue
                last_err = e
                time.sleep(0.5 * (2 ** attempt))
        return LLMResponse(content="", usage={}, raw={"error": str(last_err)}, ok=False)

    def _call(self, transport: Transport, messages: list[dict],
              allow_json: bool) -> LLMResponse:
        rf = {"type": "json_object"} if allow_json else None
        raw = transport(self.model, messages, self.temperature, rf,
                        self.extra_body or None, self.max_tokens)
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        return LLMResponse(content=content or "", usage=raw.get("usage", {}),
                           raw=raw, ok=True)


def _detect_json_format_error(e: Exception) -> bool:
    """端点不支持 response_format=json_object 的错误文本识别。"""
    text = str(e).lower()
    return "response_format" in text or "json_object" in text or "json mode" in text


def make_llm_client(settings: Settings):
    """工厂：use_mock_llm → MockLLMClient（不联网）；否则 OpenAIClient。

    load_dotenv 放工厂内（而非模块顶层）保持 import 无副作用——README 的
    文档化步骤是 cp .env.example .env，此处负责把 .env 装入进程环境，
    否则该步骤是空操作。
    """
    from dotenv import load_dotenv
    load_dotenv()
    if settings.use_mock_llm:
        from tagplatform.llm.mock import MockLLMClient  # 延迟导入避免 client↔mock 循环
        return MockLLMClient()
    return OpenAIClient(
        model=settings.llm_model,
        base_url_env=settings.llm_base_url_env,
        api_key_env=settings.llm_api_key_env,
        temperature=settings.llm_temperature,
        request_interval=settings.llm_request_interval)
