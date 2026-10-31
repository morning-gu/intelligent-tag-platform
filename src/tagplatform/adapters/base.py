"""ContentAdapter 协议与适配器注册表（Task 8）。

适配器把源格式 raw dict 转为统一 `ContentJSON`；注册表为模块级 dict
（阶段 1 单进程足够，不做插件发现）。内容导入与 API 层统一从
`tagplatform.adapters` 引用本模块的协议与注册表函数。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from typing import Protocol

from tagplatform.schemas import ContentJSON


class ContentAdapter(Protocol):
    """内容格式适配器协议。

    约定：
    - `kind` 唯一标识源格式（如 "platform"），同一 kind 不可重复注册；
    - `adapt` 校验失败抛 pydantic.ValidationError。
    """

    kind: str

    def adapt(self, raw: dict) -> ContentJSON:
        """把源格式 raw dict 校验并转换为统一 ContentJSON。"""
        ...


# 模块级注册表：kind -> 适配器实例（阶段 1 单进程足够，不做插件发现）
_REGISTRY: dict[str, ContentAdapter] = {}


def register_adapter(adapter: ContentAdapter) -> None:
    """注册适配器；kind 重复抛 ValueError。"""
    if adapter.kind in _REGISTRY:
        raise ValueError(
            f"适配器 kind 已注册: {adapter.kind}（可用: {list_adapters()}）"
        )
    _REGISTRY[adapter.kind] = adapter


def get_adapter(kind: str) -> ContentAdapter:
    """按 kind 取适配器；未注册抛 KeyError，信息列出可用 kind。"""
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise KeyError(f"未注册的适配器 kind: {kind!r}（可用: {list_adapters()}）") from None


def list_adapters() -> list[str]:
    """列出全部已注册 kind（排序返回，保证确定性）。"""
    return sorted(_REGISTRY)
