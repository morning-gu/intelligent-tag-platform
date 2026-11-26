"""ContentAdapter 注册表测试：重复注册冲突与未知 kind 错误提示。"""

import pytest

from tagplatform.capabilities.adapters import (
    PlatformAdapter,
    get_adapter,
    register_adapter,
)


def test_registry_duplicate_and_unknown():
    with pytest.raises(ValueError):
        register_adapter(PlatformAdapter())  # kind="platform" 已自动注册 → ValueError
    with pytest.raises(KeyError) as exc_info:
        get_adapter("nope")
    # 契约：KeyError 信息列出可用 kind（API 422 信息依赖此约定）
    assert "platform" in str(exc_info.value)
