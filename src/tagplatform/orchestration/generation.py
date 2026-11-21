"""资产正式挂载视图编排（GET /assets/{asset_id}/tags）——orchestration/generation 域。

用例清单（原 api/routers/generation.py 的资产标签端点内嵌逻辑，Task 11
原样下沉；SQL 与装配零语义变化，Task 12 消 _dir_info N+1）：

- `list_asset_tags`  资产正式挂载标签（读 asset_tag 权威口径，origin/候选名
  由同键 pre 行回填，目录列按 tag_define → tag_catalog 解析；未知资产 → 404）

行输出模型与装配三件套在 orchestration/_shared（与生成任务详情、审核列表
共用同一套行装配）。
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from tagplatform.capabilities.storage.models import AssetTag, MediaAsset, PreAssetTag
from tagplatform.orchestration._shared import AssetTagOut, _dir_info, _tag_info


def list_asset_tags(session: Session, asset_id: str) -> list[AssetTagOut]:
    """资产正式挂载标签：读 `asset_tag`（审核通过的复制行才是权威口径，
    doc §3.6）；origin/候选名由同键 pre 行回填，目录列按 `tag_define →
    tag_catalog` 解析；未知资产 → 404。

    历史说明：早期本端点直接读 `pre_asset_tag` 的 approved 行（那时还没有
    "审核生效 → 复制进 asset_tag"这一步）。两表口径一旦分叉（历史 approved
    行没被复制、或 modify 改了 pre 侧名字），以 asset_tag 为准。
    """
    if session.get(MediaAsset, asset_id) is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    mounts = (
        session.query(AssetTag)
        .filter(AssetTag.asset_id == asset_id)
        .order_by(AssetTag.id)
        .all()
    )
    if not mounts:
        return []
    # 同键 pre 行：origin / candidate_name 只存在于审核侧（正式表不存）。
    # 新词入库后 pre 行的 tag_id 可能仍为空（ingest 只改 define/挂载），所以
    # 再按"候选名 = 挂载名"建一份索引兜底。
    pre_rows = session.query(PreAssetTag).filter(
        PreAssetTag.asset_id == asset_id,
    ).all()
    pres = {
        (p.target_type, p.target_id, p.tag_id): p
        for p in pre_rows if p.tag_id
    }
    pres_by_name = {
        (p.target_type, p.target_id, p.candidate_name): p
        for p in pre_rows if p.candidate_name
    }
    info = _tag_info(pre_rows)
    out: list[AssetTagOut] = []
    for mount in mounts:
        pre = pres.get((mount.target_type, mount.target_id, mount.tag_id))
        if pre is None:
            pre = pres_by_name.get(
                (mount.target_type, mount.target_id, mount.tag_name))
        # 目录：新词候选挂在 pre 的 candidate_dir_id；库内标签走 tag_define.dir_id
        dir_id = (pre.candidate_dir_id if pre is not None else None) or \
            info.get(mount.tag_id, {}).get("dir_id")
        dir_name, dir_path, _ = _dir_info(dir_id)
        out.append(AssetTagOut(
            id=mount.id,
            tag_id=mount.tag_id,
            tag_name=mount.tag_name,
            candidate_name=(pre.candidate_name if pre is not None else None),
            origin=(pre.origin if pre is not None else None),
            target_type=mount.target_type,
            dir_id=dir_id,
            dir_name=dir_name,
            dir_path=dir_path,
            created_at=mount.created_at,
        ))
    return out
