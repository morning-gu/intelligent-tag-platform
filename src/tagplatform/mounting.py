"""挂载点解析：目录的 applicable_scope → `pre_asset_tag` 的 target_type/target_id。

一个标签"挂在哪"由它所属目录的适用范围决定（设计文档：子目录覆盖父目录的
适用范围，不继承）：

- `content`   → 整部影片，target_id = asset_id；
- `scene`     → 场景，target_id = 证据里第一个真实存在的 scene_id；
- `character` → 角色，target_id = 证据场景里唯一的 character_id（多个角色
  时无法确定归属，返回 None 由调用方丢弃并记原因）。

解析不出挂载点时返回 None——宁可丢一条候选，也不要把标签挂错实体。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from dataclasses import dataclass

from tagplatform.generation.base import Evidence
from tagplatform.library import SCOPE_CHARACTER, SCOPE_SCENE
from tagplatform.schemas import ContentJSON


@dataclass(frozen=True)
class Mount:
    """一条候选的落库挂载点。"""

    target_type: str
    target_id: str


def _scene_ids(content: ContentJSON) -> dict[str, list[str]]:
    """scene_id → 该场景的角色 id 列表（无则空列表）。"""
    mapping: dict[str, list[str]] = {}
    for scene in content.content.scenes:
        mapping[scene.scene_id] = list(getattr(scene, "character_ids", []) or [])
    return mapping


def resolve_mount(scope: str, asset_id: str, content: ContentJSON,
                  evidence: list[Evidence]) -> Mount | None:
    """按目录适用范围解析挂载点；解析不出返回 None。"""
    if scope == SCOPE_SCENE:
        scenes = _scene_ids(content)
        for item in evidence:
            if item.scene_id in scenes:
                return Mount(target_type="scene", target_id=item.scene_id)
        return None

    if scope == SCOPE_CHARACTER:
        scenes = _scene_ids(content)
        candidates: list[str] = []
        for item in evidence:
            for character_id in scenes.get(item.scene_id, []):
                if character_id not in candidates:
                    candidates.append(character_id)
        if len(candidates) == 1:
            return Mount(target_type="character", target_id=candidates[0])
        return None

    return Mount(target_type="content", target_id=asset_id)
