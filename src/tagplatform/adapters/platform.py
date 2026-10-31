"""平台格式适配器（Task 8）。

平台格式即统一内容 JSON 标准格式，`adapt` 为 identity：raw dict 直接
经 `parse_content` 校验后返回。包导入时由 `tagplatform.adapters` 自动
注册（kind="platform"）。
"""

from tagplatform.schemas import ContentJSON, parse_content


class PlatformAdapter:
    """identity 适配器：源格式即平台标准格式（kind="platform"）。"""

    kind: str

    def __init__(self, kind: str = "platform") -> None:
        self.kind = kind

    def adapt(self, raw: dict) -> ContentJSON:
        """校验并解析平台格式 raw dict，失败抛 pydantic.ValidationError。"""
        return parse_content(raw)
