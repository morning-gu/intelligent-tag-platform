"""ContentAdapter 协议、注册表与平台格式适配器测试（Task 8）。

覆盖契约：
- platform 适配器 roundtrip：fixture 经 get_adapter("platform").adapt
  解析后关键字段落位；
- 注册表：重复注册同 kind 抛 ValueError，未知 kind 抛 KeyError 且信息
  列出可用 kind；包导入时自动注册使 list_adapters 含 "platform"；
- adapt 校验失败抛 pydantic.ValidationError。
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tagplatform.adapters import (
    PlatformAdapter,
    get_adapter,
    list_adapters,
    register_adapter,
)

FIXTURE = Path("tests/fixtures/content_movie.json")


def test_platform_adapter_roundtrip():
    a = get_adapter("platform")
    c = a.adapt(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert c.asset.title == "示例电影"


def test_registry_duplicate_and_unknown():
    with pytest.raises(ValueError):
        register_adapter(PlatformAdapter())  # kind="platform" 已自动注册 → ValueError
    with pytest.raises(KeyError):
        get_adapter("nope")


def test_unknown_kind_message_lists_available():
    # 契约：KeyError 信息列出可用 kind（Task 11 的 422 信息依赖此约定）
    with pytest.raises(KeyError) as exc_info:
        get_adapter("nope")
    assert "platform" in str(exc_info.value)


def test_list_adapters_contains_platform():
    # 包导入时自动注册：list_adapters 必含 "platform"
    assert "platform" in list_adapters()


def test_platform_adapter_adapt_invalid_raises():
    # 契约：adapt 校验失败抛 pydantic.ValidationError
    with pytest.raises(ValidationError):
        get_adapter("platform").adapt({"schema_version": "0.9"})
