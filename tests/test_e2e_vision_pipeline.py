"""端到端：上游资产 JSON → 导入 → 开放生成 → 待审核行。

这条链路是「视频理解部门给资产 → 平台出标签 → 人工审核」的最小闭环，
用 mock LLM/Embedder 固定行为（零网络），断言四件事：
1. 上游格式经 vision 适配器可直接导入：影片/场景资产行落库，识别内容
   content_json 直写影片行（v2 合并语义：media_context 退役）；
2. 生成任务（mode=open，任务行 tag_generation_task 直连 asset_id）在导入
   的资产上跑通；
3. 产出全部是 pre_asset_tag 中 `review_status="pending"` 的待审核行
   （可审核状态），origin=open 自由候选（tag_id 空、候选名原文——审核
   merge 归置时才赋 tag_id）；
4. 证据锚定到上游场景（scene_id + 毫秒区间）——不是空证据。

v1→v2 映射：GenerationTask→TagGenerationTask（context_id→asset_id）、
AssetTag 待审行→PreAssetTag、origin "generated"→"open"（词表退役）。
"""

import json
from datetime import datetime, timezone

from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.adapters import get_adapter
from tagplatform.content_import import import_content
from tagplatform.db.models import MediaAsset, PreAssetTag, TagGenerationTask
from tagplatform.tasks import run_generation_task

UPSTREAM = {
    "schema_version": "1.0",
    "asset": {
        "asset_id": "movie_e2e_001",
        "asset_type": "movie",
        "title": "端到端样例",
        "duration_ms": 6_000_000,
        "language": "zh-CN",
    },
    "content": {
        "description": "一名警探在雨夜追查连环失踪案，逐步发现真相与自己有关。",
        "characters": [
            {"character_id": "c1", "name": "陈默", "description": "刑警队长"},
            {"character_id": "c2", "name": "林晚", "description": "失踪者家属"},
        ],
        "scenes": [
            {
                "scene_id": "scene_001",
                "start_ms": 120_000,
                "end_ms": 240_000,
                "description": "仓库对峙，陈默质问林晚为何隐瞒关键线索。",
                "location": "废弃仓库",
                "character_ids": ["c1", "c2"],
                "emotion": [{"name": "紧张"}, {"name": "压抑"}],
                "dialogues": [{
                    "speaker_id": "c1", "speaker_name": "陈默",
                    "text": "你早就知道他在哪，对吗？",
                    "start_ms": 130_000, "end_ms": 133_000,
                    "emotion": {"text_emotion": "愤怒", "voice_emotion": "低沉"},
                }],
                "audio_events": [{
                    "event_type": "background_music", "description": "低频弦乐持续压迫",
                    "start_ms": 120_000, "end_ms": 240_000,
                }],
            },
            {
                "scene_id": "scene_002",
                "start_ms": 240_000,
                "end_ms": 360_000,
                "description": "雨夜追车，霓虹在积水里拉出长条光斑。",
                "emotion": [{"name": "急促"}],
            },
        ],
    },
}


def test_upstream_to_pending_tags(session):
    # 1. 适配 + 导入（服务层内部提交）
    content = get_adapter("vision").adapt(UPSTREAM)
    result = import_content(content, session, None)
    assert result.asset_id == "movie_e2e_001"
    assert len(result.scene_asset_ids) == 2
    asset = session.get(MediaAsset, "movie_e2e_001")
    assert asset is not None
    # v2 合并语义：识别内容直落影片行 content_json（media_context 退役）
    assert asset.content_json is not None

    # 2. 建任务并执行（mock 模式 B：固定三候选；min_confidence 降到 0.5
    #    使三条全部过线——mock 的 confidence 落在 [0.5,1)，默认 0.7 会随机丢）
    task = TagGenerationTask(
        task_id="task_e2e_001", asset_id=result.asset_id, mode="open",
        policy_json='{"min_confidence": 0.5}',
        status="queued", created_at=datetime.now(timezone.utc))
    session.add(task)
    session.commit()

    run_generation_task(task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    session.refresh(task)
    assert task.status == "succeeded", task.error
    assert task.prompt_version == "mtg-v1"

    # 3. 产出是待审核行（人工审核入口可见），不是已定终态
    rows = (session.query(PreAssetTag)
            .filter(PreAssetTag.asset_id == "movie_e2e_001").all())
    assert len(rows) == 3
    assert {r.review_status for r in rows} == {"pending"}
    assert {r.origin for r in rows} == {"open"}  # v2 词表（generated 退役）
    # 开放生成落自由候选：tag_id 空、候选名原文（审核 merge 归置时才赋 tag_id）
    assert all(r.tag_id is None and r.candidate_name for r in rows)

    # 4. 证据锚定在上游场景的时间区间内
    scene_ranges = {(s.time_range.start_ms, s.time_range.end_ms) for s in content.content.scenes}
    for row in rows:
        evidence = json.loads(row.evidence_json)
        assert evidence, "证据不得为空（上游给了场景时间戳）"
        for item in evidence:
            assert (item["start_ms"], item["end_ms"]) in scene_ranges
