"""开放式生成流水线（MovieTagGen 移植版）：多轮生成 → 语义合并 → judge → 落库前产物。

与平台原 open 实现的差别（2026-11 替换）：
- 输入不是一份文本 prompt，而是"资产 JSON 的场景文本 + 按场景对齐的关键帧"；
- 逐场单独生成，再用全片材料和逐场候选综合生成影片级标签，两级候选一起去重聚类；
- 产出先过 judge（deepseek-v4.1-flash）逐条做三维结果导向评分
  （relevance/semantics/value，0-4），只有三维分数过线且置信度过线的
  候选才落 `asset_tag`；
- 证据锚点必须是资产里真实存在的场景（模型编的 id 直接丢），
  与 MovieTagGen 的"证据可追溯"要求对齐。

保留的平台语义：
- 空目录不报错（与 closed 相反）；
- 标签库装满后按"库内对齐"分三档标记（`catalog_align`）：同名 → origin=catalog
  并挂 tag_id；疑似经 LLM 复核（`verify`）确认同一概念 → 替换成库内标准名并
  挂 tag_id（原词记进审计，供将来写别名）；其余走 generated，分档只进审计与
  排序，不删任何行；
- `persist_tags` 的 upsert 与审核已定集跳过规则不变（runner 负责落库）。

每任务一份审计 JSON（`mtg_report_dir/<task_id>.json`）：两轮原始候选、合并簇、
judge 判决、淘汰原因、tokens 与耗时——`generation_task` 表没有 usage 列，
不为此加迁移。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from sqlalchemy.orm import Session

from tagplatform.capabilities.llm.usage import accumulate_usage
from tagplatform.services.generation.base import Evidence, GenerationSpec, ProducedTag
from tagplatform.services.generation.open.mtg import keyframes as keyframes_mod
from tagplatform.services.generation.open.mtg.catalog_align import (
    BAND_DUPLICATE,
    BAND_NEW,
    BAND_RANK,
    BAND_SUSPECT,
    Alignment,
    align_candidates,
    recall_library_hints,
    summarize_bands,
)
from tagplatform.services.generation.open.mtg.descriptions import (
    build_description_template,
    content_names,
    fill_descriptions,
    is_general,
)
from tagplatform.services.generation.open.mtg.judge import build_judge_template, judge_candidates
from tagplatform.services.generation.open.mtg.merge import (
    MergedCandidate,
    merge_candidates,
)
from tagplatform.services.generation.open.mtg.normalization import normalize_key
from tagplatform.services.generation.open.mtg.parse import parse_generation_items
from tagplatform.services.generation.open.mtg.prompt_samples import SampledPromptClient
from tagplatform.services.generation.open.mtg.prompts import (
    build_generation_messages,
    build_generation_template,
    build_scene_generation_messages,
    render_content,
)
from tagplatform.services.generation.open.mtg.verify import build_verify_template, verify_pairs
from tagplatform.services.tags.library import (
    DirAssignment,
    library_index,
    rank_dir_assignments,
)
from tagplatform.services.tags.mounting import resolve_mount
from tagplatform.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# 口径版本号：写入 generation_task.prompt_version，重跑/换方案时可区分。
# v2 = 引入标签库（提示注入 + 候选分档 + 分档参与排序），与 v1（纯生成）分开记。
# v3 = 生成阶段就注入"可扩展目录"清单，由模型给出 dir（校验不过才回退向量归类）。
# v4 = 逐场提炼、全片综合，给场景级候选单独的产出配额。
# v6 = judge 改三维结果导向评分（relevance/semantics/value 0-4/null），
#      按分数阈值筛选标签，value 分取代 importance 参与排序。
# v8 = judge 评分协议收紧：value 必须给 0-4 整数（不再接受 null），漏字段与
#      非法值按协议错误重试；judge 原始响应进审计（judge_trace）。
# v9 = 目录契合硬闸：judge 逐条判 in_dir（词的核心含义属不属于目标目录），
#      不属于的候选直接淘汰；生成侧目标文案改为服从目录定义。
# v10 = 按候选对齐重试漏判，并统一三维定义和重试格式。
MTG_VERSION = "mtg-v10-judge-coverage"

# 单条证据保留的场景数（审核卡展示用，多了没意义）
_MAX_EVIDENCE = 3

# judge 置信度下限：只挡"模型自己都不确定"的判定，不拿它跟
# policy.min_confidence 比——两者语义不同（一个是模型对标签的自评，一个是
# 审核判定把握），硬套 0.7 会误杀事实正确的标签（实测把 Memento 的
# "倒叙结构" 0.65 判死）。过线的判定本来就该交给人工复核。
_JUDGE_CONFIDENCE_FLOOR = 0.5

# judge 三维分数的保留阈值：relevance/semantics ≥3（内容支持可推导、名称
# 清晰无歧义），value ≥2（至少能筛到一类内容——0/1 档的泛泛评价与情节
# 依赖词在这里淘汰）。null 分按未过线处理，绝不猜分放行。
_JUDGE_SCORE_FLOORS = {"relevance": 3, "semantics": 3, "value": 2}

# 小节 4 过滤段（judge 判决 + 证据把关）的淘汰原因常量：审计读侧
# （open_report 的数量漏斗）据 is_filter_stage_drop 把"过滤段淘汰"与
# "目录归属段淘汰"分开——改文案必须同步分类函数，否则漏斗推导失真。
_DROP_NO_VERDICT = "judge 未返回判决"
_DROP_DIR_MISMATCH = "标签含义不属于目标目录"
_DROP_SCORE_BELOW = "judge 评分未过线"
_DROP_CONFIDENCE_BELOW = "judge 判定把握"
_DROP_NO_EVIDENCE = "无有效场景证据"
_DROP_SINGLE_SCENE = "全片级但只有单场支撑"


def is_filter_stage_drop(why: str) -> bool:
    """审计 dropped.why 是否属于小节 4 过滤段（judge + 证据把关）。

    评分/把握度两类原因带动态后缀（具体分值），按前缀匹配；其余四类
    （未判 / 目录不符 / 无证据 / 单场支撑）是定值文案，精确匹配。目录归属段的
    淘汰原因（无可归属目录等）不在此列——它们发生在排序配额之后，不影响
    judge 通过数。
    """
    return (why in (_DROP_NO_VERDICT, _DROP_DIR_MISMATCH, _DROP_NO_EVIDENCE,
                    _DROP_SINGLE_SCENE)
            or why.startswith((_DROP_SCORE_BELOW, _DROP_CONFIDENCE_BELOW)))


# judge 必须引用原文：判决里给出 quote，与内容逐字对上（容忍标点/省略号差异）
# 才采信。这条把"judge 复读生成侧理由"变成可检测的失败：没有原文锚点的
# 高 relevance 分一律降档淘汰，而不是凭一句"内容有依据"放行。
_QUOTE_MIN_CHARS = 8
_QUOTE_STRIP_RE = re.compile(
    r"[\s「」『』“”‘’\"'…—·,，。、；;：:！!？?（）()\[\]【】]+")


def _normalize_quote(text: str) -> str:
    return _QUOTE_STRIP_RE.sub("", text or "")


def _quote_ok(quote: str | None, haystack: str) -> bool:
    """quote 与内容有 ≥8 字连续重合即视为有效引用。"""
    normalized = _normalize_quote(str(quote or ""))
    if len(normalized) < _QUOTE_MIN_CHARS:
        return False
    for start in range(len(normalized) - _QUOTE_MIN_CHARS + 1):
        if normalized[start:start + _QUOTE_MIN_CHARS] in haystack:
            return True
    return False


def _require_quotes(verdicts: dict[str, dict], haystack: str) -> None:
    """引用不到原文的高 relevance 评分压到 1（只有话题、无可核验依据）。

    并入评分本身（不单开一道闸、不加淘汰原因）：judge 给了 ≥3 的内容支持
    分但给不出可核对的原文，等同于没依据——降档后自然过不了保留阈值。
    """
    for verdict in verdicts.values():
        relevance = verdict.get("relevance")
        if not isinstance(relevance, int) or relevance < _JUDGE_SCORE_FLOORS["relevance"]:
            continue
        if _quote_ok(verdict.get("quote"), haystack):
            continue
        verdict["relevance"] = 1
        verdict["reason"] = (verdict.get("reason") or "未引用可核对原文")[:40]


def _make_stage_client(settings: Settings, llm, *, temperature: float,
                       max_tokens: int | None = None):
    """judge / verify 阶段客户端工厂（原两函数 85% 相同，参数化合并）。

    可复用判定按**能力探测**而非客户端类型：配置开关 settings.use_mock_llm，
    或客户端声明 `zero_network` 能力位（零网络替身，如 MockLLMClient）——
    管线因此不 import 测试替身的类型（测试关注点不泄漏进生产代码），任何
    声明该能力位的客户端都适配。可复用 = 直接把传入客户端当阶段客户端；
    否则按 mtg_judge_model + 阶段参数另建真实客户端：
    - judge：温度取 settings.mtg_judge_temperature，不限输出（一次 20 条判决，
      输出量本来就大）；
    - verify：温度 0 且卡 max_tokens（推理模型在模棱两可的对子上会把思考
      写爆，实测一次 27k completion tokens 把 4 分钟的任务拖到 7 分钟；
      超限解析失败 → 不替换，代价可控）。
    """
    if settings.use_mock_llm or getattr(llm, "zero_network", False):
        return llm
    from tagplatform.capabilities.llm import OpenAIClient
    return OpenAIClient(
        model=settings.mtg_judge_model,
        base_url_env=settings.llm_base_url_env,
        api_key_env=settings.llm_api_key_env,
        temperature=temperature,
        max_tokens=max_tokens,
        request_interval=settings.llm_request_interval,
    )


def _make_judge_client(settings: Settings, llm):
    """judge 阶段客户端（_make_stage_client 的 judge 特化）。"""
    return _make_stage_client(settings, llm,
                              temperature=settings.mtg_judge_temperature)


def _make_verify_client(settings: Settings, llm):
    """相似度复核阶段客户端（_make_stage_client 的 verify 特化）。"""
    return _make_stage_client(settings, llm, temperature=0.0,
                              max_tokens=settings.open_library_verify_max_tokens)


def _dir_hint_entries(tag_library, spec: GenerationSpec,
                      limit: int) -> list[dict]:
    """可新增标签的目录 → 提示词条目（id + 全路径 + 粒度）。

    只收可扩充目录（dir_dynamic/dir_asset，允许扩充）且落在本次生成
    范围内的目录——静态目录不该出现在提示词里，否则模型会往不允许新增的
    目录里塞词，然后在校验环节被打回，白烧一轮 token。
    """
    if tag_library is None or limit <= 0:
        return []
    entries: list[dict] = []
    for directory in tag_library.new_tag_dirs(spec.policy.dir_ids):
        path = "/".join(tag_library.path_of(directory.dir_id)) or directory.dir_name
        entries.append({"dir_id": directory.dir_id, "path": path,
                        "scope": tag_library.scope_of(directory.dir_id),
                        "description": directory.description})
    entries.sort(key=lambda item: (item["path"], item["dir_id"]))
    return entries[:limit]


def _resolve_dir_value(tag_library, raw: str | None) -> str | None:
    """模型给的目录串 → dir_id（容忍 id / 全路径 / 唯一目录名）。"""
    if not raw or tag_library is None:
        return None
    text = raw.strip()
    if tag_library.directory(text) is not None:
        return text
    by_path = {"/".join(tag_library.path_of(d.dir_id)): d.dir_id
               for d in tag_library.dirs()}
    if text in by_path:
        return by_path[text]
    by_name = [d.dir_id for d in tag_library.dirs() if d.dir_name == text]
    return by_name[0] if len(by_name) == 1 else None


def _validate_model_dir(tag_library, dir_id: str | None, spec: GenerationSpec,
                        contents, evidence: list[Evidence],
                        per_dir_count: dict[str, int]
                        ) -> tuple[DirAssignment | None, object | None, str]:
    """校验模型指定的目录：范围 → 存在 → 可扩展 → 配额 → 挂载点。

    任何一步不过都返回 (None, None, 原因)，由调用方回退到向量归类；不静默
    改写模型的目录归属（审核页要能看到"模型选错了"这件事）。
    """
    if not dir_id:
        return None, None, "模型未给目录"
    allowed = tag_library.descendants(spec.policy.dir_ids)
    if dir_id not in allowed:
        return None, None, f"模型给的目录 {dir_id} 不在本次生成范围"
    directory = tag_library.directory(dir_id)
    if directory is None:
        return None, None, f"模型给的目录 {dir_id} 不存在"
    if not directory.allows_new_tags:
        return None, None, f"目录「{directory.dir_name}」不允许新增标签"
    if (spec.policy.max_tags_per_dir
            and per_dir_count.get(dir_id, 0) >= spec.policy.max_tags_per_dir):
        return None, None, f"目录「{directory.dir_name}」已达产出上限"
    scope = tag_library.scope_of(dir_id)
    mount = resolve_mount(scope, spec.asset_id, contents, evidence)
    if mount is None:
        return None, None, (f"目录「{directory.dir_name}」适用范围 {scope} "
                            "解析不出唯一挂载点")
    return (DirAssignment(dir_id=dir_id, dir_name=directory.dir_name,
                          scope=scope, score=0.0, reason="模型指定目录"),
            mount, "")


def _evidence_for(candidate: MergedCandidate, verdict: dict | None,
                  scene_index: dict) -> list[Evidence]:
    """候选 → 证据列表：judge 引用优先，回退生成侧场景，只保留真实场景。

    judge 给了场景就只用它（那是"它凭什么判支持"的依据）；judge 没给场景
    时才回退生成侧的场景并集——不把两边混在一起，避免证据卡里出现
    与判定无关的场景。
    """
    verdict_scenes = [s for s in (verdict or {}).get("scenes") or []
                      if s in scene_index
                      and (candidate.scope != "scene" or s in candidate.scenes)]
    source = verdict_scenes or [s for s in candidate.scenes if s in scene_index]
    evidence: list[Evidence] = []
    seen: set[str] = set()
    for scene_id in source:
        if scene_id in seen:
            continue
        seen.add(scene_id)
        scene = scene_index[scene_id]
        evidence.append(Evidence(
            scene_id=scene_id,
            start_ms=scene.time_range.start_ms,
            end_ms=scene.time_range.end_ms,
            description=(scene.description or "")[:120],
        ))
        if len(evidence) >= _MAX_EVIDENCE:
            break
    return evidence


def _sample_client(spec: GenerationSpec, client, stage, template):
    """按 stage 包装采样客户端（prompt_samples 开启时记录提示样本）。"""
    if spec.prompt_samples is None:
        return client
    return SampledPromptClient(client, spec.prompt_samples, stage, template)


def _load_library_context(settings: Settings, spec: GenerationSpec, embedder,
                          contents) -> tuple[object | None, list, list[str],
                                             list[dict]]:
    """小节 0：加载只读标签库、向量索引、库内提示与可扩展目录清单（绝不写库）。"""
    # 库来自 JSON 模拟（spec.library），按任务的目录范围过滤；向量按
    # 库/模型/范围缓存，同一进程内不重复嵌入。
    tag_library = spec.library if settings.open_library_enabled else None
    library = (library_index(tag_library, embedder, spec.policy.dir_ids)
               if tag_library is not None else [])
    hints: list[str] = []
    if library and settings.open_library_hint_enabled:
        hints = recall_library_hints(library, embedder, contents,
                                     settings.open_library_hint_count)
    # 可扩展目录清单：生成阶段就交给模型决定归属（"在某目录下新增某标签"），
    # 而不是等生成完再按向量猜。只列可扩充目录（dir_dynamic/dir_asset）
    # 且落在范围内的目录。
    dir_hint = (_dir_hint_entries(tag_library, spec, settings.open_dir_hint_max)
                if settings.open_dir_hint_max > 0 else [])
    return tag_library, library, hints, dir_hint


@dataclass
class _GenerationLimits:
    """小节 1 的产出配额：队列截断上限与单轮请求条数（小节 1/5/6 共用）。"""
    limit: int
    total_limit: int
    scene_limit: int
    scene_request_limit: int
    film_request_limit: int


def _plan_generation_limits(settings: Settings, spec: GenerationSpec,
                            contents) -> _GenerationLimits | None:
    """小节 1 开头：按策略上限计算逐场/全片两级配额；无可产出时返回 None。"""
    scenes = contents.content.scenes
    limit = max(0, min(settings.mtg_max_final_tags, spec.policy.max_tags))
    total_limit = min(spec.policy.max_tags, 2 * limit)
    if total_limit <= 0 or not scenes:
        return None
    scene_limit = (total_limit if len(scenes) == 1 else
                   min(len(scenes), 5, max(1, total_limit // 3)))
    scene_request_limit = min(3, scene_limit) if len(scenes) == 1 else 1
    film_request_limit = (max(1, min(total_limit, settings.mtg_max_candidates_per_run)
                              - scene_limit) if len(scenes) >= 2 else 0)
    return _GenerationLimits(limit=limit, total_limit=total_limit,
                             scene_limit=scene_limit,
                             scene_request_limit=scene_request_limit,
                             film_request_limit=film_request_limit)


def _generate_items(client, messages, suffix, max_items, scope, scene_id=None,
                    *, spec, usage, scene_ids, parsed_calls
                    ) -> tuple[list[dict], int]:
    """单轮生成调用 → 过滤后的候选条目；顺带累计 usage 与成功解析轮数。"""
    response = client.complete(messages, f"{spec.run_id}:{suffix}")
    accumulate_usage(usage, response.usage)
    if not response.ok:
        raise RuntimeError(f"LLM 调用失败（{suffix}）: {response.raw.get('error')}")
    try:
        items = parse_generation_items(response.content, max_items=None)
        parsed_calls += 1
    except ValueError as exc:
        logger.warning("生成输出解析失败（%s）：%s", suffix, exc)
        items = []

    valid = []
    for item in items:
        if item["scope"] not in (None, scope):
            continue
        ids = list(dict.fromkeys(s for s in item["scenes"] if s in scene_ids))
        if scope == "scene":
            if scene_id not in ids:
                continue
            ids = [scene_id]
        elif len(ids) < 2:
            continue
        valid.append({**item, "scope": scope, "scenes": ids})
        if len(valid) == max_items:
            break
    return valid, parsed_calls


def _run_generation_rounds(settings: Settings, spec: GenerationSpec, llm,
                           contents, frames, modalities, usage: dict,
                           hints: list[str], dir_hint: list[dict],
                           limits: _GenerationLimits,
                           *, catalog_only: bool = False,
                           ) -> tuple[list[dict], list[dict], list[dict]]:
    """小节 1：逐场生成 → 全片综合（两轮 LLM 调用，产出 runs 审计与两级候选）。"""
    scenes = contents.content.scenes
    scene_request_limit = limits.scene_request_limit
    film_request_limit = limits.film_request_limit
    scene_ids = {scene.scene_id for scene in scenes}
    runs: list[dict] = []
    scene_client = _sample_client(spec, llm, "generation_scene", partial(build_generation_template, "scene"))
    film_client = _sample_client(spec, llm, "generation_film", partial(build_generation_template, "film"))
    parsed_calls = 0

    scene_usage = usage.copy()
    per_scene = {}
    for index, scene in enumerate(scenes):
        messages = build_scene_generation_messages(
            scene, frames.get(scene.scene_id, ()), content=contents, max_tags=scene_request_limit,
            dir_hint=dir_hint, modalities=modalities,
            catalog_choices=hints if catalog_only else None)
        per_scene[scene.scene_id], parsed_calls = _generate_items(
            scene_client, messages, f"scene-{index + 1:03d}", scene_request_limit,
            "scene", scene_id=scene.scene_id, spec=spec, usage=usage,
            scene_ids=scene_ids, parsed_calls=parsed_calls)
        logger.info("mtg 场景 %d/%d %s：%d 个候选", index + 1, len(scenes),
                    scene.scene_id, len(per_scene[scene.scene_id]))

    scene_entries = [item for items in per_scene.values() for item in items]
    runs.append({
        "run": 0, "scope": "scene", "items": len(scene_entries),
        "tags": [item["tag"] for item in scene_entries],
        "scenes": {scene_id: [item["tag"] for item in items]
                   for scene_id, items in per_scene.items()},
        **{key: value - scene_usage[key] for key, value in usage.items()},
    })
    film_entries = []
    if film_request_limit:
        messages = build_generation_messages(
            contents, frames, max_tags=film_request_limit, scene_tags=per_scene,
            library_hint=hints, dir_hint=dir_hint, modalities=modalities,
            catalog_only=catalog_only)
        film_usage = usage.copy()
        film_entries, parsed_calls = _generate_items(
            film_client, messages, "film", film_request_limit, "film",
            spec=spec, usage=usage, scene_ids=scene_ids, parsed_calls=parsed_calls)
        runs.append({
            "run": 1, "scope": "film", "items": len(film_entries),
            "tags": [item["tag"] for item in film_entries],
            **{key: value - film_usage[key] for key, value in usage.items()},
        })
        logger.info("mtg 全片综合：%d 个候选", len(film_entries))

    if not parsed_calls:
        raise RuntimeError("各轮生成均无法解析为 JSON")
    return runs, scene_entries, film_entries


def _merge_two_level_candidates(settings: Settings, embedder,
                                film_entries: list[dict],
                                scene_entries: list[dict]) -> list[MergedCandidate]:
    """小节 2：两级候选合并（同名候选优先保留影片级生成的理由和释义）。"""
    entries = ([{**item, "run": 1} for item in film_entries]
               + [{**item, "run": 0} for item in scene_entries])
    return merge_candidates(entries, embedder, settings.mtg_merge_threshold)


def _judge_merged_candidates(settings: Settings, spec: GenerationSpec, llm,
                             judge_client, contents, merged, modalities,
                             usage: dict, frame_count: int, started: float,
                             *, trace: dict | None = None,
                             tag_library=None, dir_hint: list[dict] | None = None,
                             ) -> tuple[dict[str, dict], dict]:
    """小节 3：judge 逐条判内容支持度；失败也留审计现场后原样抛出。

    trace 收 judge 逐次原始响应与解析错误，成功/失败两条路径都落进审计。
    """
    verdicts: dict[str, dict] = {}
    judge_usage: dict = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    if settings.mtg_judge_enabled and merged:
        client = _sample_client(spec, judge_client or _make_judge_client(settings, llm),
                                "judge", build_judge_template)
        # 目录契合只对"模型认领了本次可扩展目录"的候选核；没认领的由
        # 小节 5 的目录归属校验处理，不在这里替它猜一个目标目录。
        entries = {entry["dir_id"]: entry for entry in (dir_hint or [])}
        payload = []
        for m in merged:
            dir_id = _resolve_dir_value(tag_library, m.dir_raw)
            payload.append({"tag": m.tag, "dimension": m.dimension, "scope": m.scope,
                            "reason": m.reason, "scenes": list(m.scenes),
                            "dir": dir_id if dir_id in entries else None})
        used_dirs = [entries[dir_id] for dir_id in
                     dict.fromkeys(item["dir"] for item in payload if item["dir"])]
        try:
            verdicts = judge_candidates(client, contents, payload,
                                        run_id=spec.run_id, usage=judge_usage,
                                        include_dialogue="dialogue" in modalities and "script" in modalities,
                                        trace=trace, dirs=used_dirs)
        except Exception as exc:  # noqa: BLE001 —— 失败也要留审计现场
            _write_report(settings, spec.task_id, {
                "version": MTG_VERSION,
                "task_id": spec.task_id,
                "asset_id": spec.asset_id,
                "error": f"{type(exc).__name__}: {exc}",
                "scenes": len(contents.content.scenes),
                "frames": frame_count,
                "usage": usage,
                "judge_usage": judge_usage,
                "judge_trace": trace or {},
                "candidates": [m.tag for m in merged],
                "elapsed_s": round(time.monotonic() - started, 1),
            })
            raise
    return verdicts, judge_usage


def _filter_candidates(settings: Settings, contents, verdicts: dict[str, dict],
                       modalities, merged) -> tuple[list, list[dict]]:
    """小节 4：过滤 + 证据 + 目录匹配（judge 三维分/把握度/场景证据逐条把关）。"""
    scene_index = {scene.scene_id: scene for scene in contents.content.scenes}
    if settings.mtg_judge_enabled:
        # 引用核对的底本必须与生成侧同口径：消融关掉台词后再拿全文去核，
        # 会让“引用了模型没看过的台词”误判为有效。
        _require_quotes(verdicts,
                        _normalize_quote(render_content(contents, modalities)))
    accepted: list[tuple[MergedCandidate, dict | None, list[Evidence]]] = []
    dropped: list[dict] = []
    for candidate in merged:
        verdict = verdicts.get(normalize_key(candidate.tag))
        if settings.mtg_judge_enabled:
            if verdict is None:
                dropped.append({"tag": candidate.tag, "why": _DROP_NO_VERDICT})
                continue
            # 目录契合先于评分：词本身不属于目标目录时，内容再支持也不该
            # 落进这个目录（情感氛围目录里的"屋顶枪战追逐"就是这么来的）。
            if verdict.get("in_dir") is False:
                dropped.append({"tag": candidate.tag, "why": _DROP_DIR_MISMATCH})
                continue
            below = [f"{axis}={'null' if verdict.get(axis) is None else verdict[axis]}"
                     f" < {floor}"
                     for axis, floor in _JUDGE_SCORE_FLOORS.items()
                     if not isinstance(verdict.get(axis), int)
                     or verdict[axis] < floor]
            if below:
                dropped.append({"tag": candidate.tag,
                                "why": _DROP_SCORE_BELOW + "（" + "；".join(below) + "）"})
                continue
            if verdict["confidence"] < _JUDGE_CONFIDENCE_FLOOR:
                dropped.append({"tag": candidate.tag,
                                "why": f"{_DROP_CONFIDENCE_BELOW} {verdict['confidence']:.2f} "
                                       f"< {_JUDGE_CONFIDENCE_FLOOR}"})
                continue
        evidence = _evidence_for(candidate, verdict, scene_index)
        if not evidence:
            dropped.append({"tag": candidate.tag, "why": _DROP_NO_EVIDENCE})
            continue
        # 层级约束（只用于把关，不落库）：全片级必须有两个以上场景支撑，
        # 单场支撑的"全片级"标签一律不进队列——它本质是场景级措辞被写大了
        if candidate.scope == "film" and len(evidence) < 2:
            dropped.append({"tag": candidate.tag,
                            "why": _DROP_SINGLE_SCENE})
            continue
        accepted.append((candidate, verdict, evidence))
    return accepted, dropped


def _align_and_verify_candidates(settings: Settings, spec: GenerationSpec, llm,
                                 embedder, library: list, accepted: list
                                 ) -> tuple[dict[str, Alignment], dict, dict]:
    """小节 5 前段：库内对齐分档 + 疑似对子 LLM 复核（结论只进排序与审计）。"""
    alignment: dict[str, Alignment] = {}
    verify_usage: dict = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    verify_trace: dict = {}
    if library and accepted:
        alignment = align_candidates(
            library, embedder, [candidate.tag for candidate, _, _ in accepted],
            suspect_cosine=settings.open_library_suspect_cosine,
            name_overlap=settings.open_library_name_overlap,
        )
        # 疑似对子交给模型复核：分档只能看"像不像"，替换要看"是不是"。
        # 复核失败/开关关掉都按"未复核"处理（不替换），并把结论写进审计。
        if settings.open_library_verify_enabled:
            pairs = [(name, item.matched_name, item.cosine or 0.0)
                     for name, item in alignment.items()
                     if item.band == BAND_SUSPECT and item.matched_name]
            if pairs:
                client = _sample_client(spec, _make_verify_client(settings, llm),
                                        "verify", build_verify_template)
                verdict_map = verify_pairs(client, pairs, run_id=spec.run_id,
                                           usage=verify_usage, trace=verify_trace)
                for name, same in verdict_map.items():
                    if name not in alignment:
                        continue
                    item = alignment[name]
                    if same is False and item.band == BAND_SUSPECT:
                        # 复核否定的就是"新标签"：分档要跟着改，否则它会被
                        # 一直当疑似压在排序后面，把名额让给别的疑似。
                        alignment[name] = replace(
                            item, same_concept=False, band=BAND_NEW,
                            reason=f"{item.reason}；复核判非同一概念")
                    else:
                        alignment[name] = replace(item, same_concept=same)
    return alignment, verify_usage, verify_trace


def _band_rank(alignment: dict[str, Alignment], tag: str) -> int:
    """候选标签的库内分档序号（未参与对齐的按新词计）。"""
    item = alignment.get(tag)
    return BAND_RANK[item.band] if item is not None else BAND_RANK["new"]


def _rank(alignment: dict[str, Alignment], item
          ) -> tuple[int, int, float, int]:
    """队列排序：value 分 → 库内分档（新词优先）→ judge 把握 → 出现轮数。

    先看标签价值而不是轮数：重复出现只说明"模型爱说"，不代表这条标签
    对用户有用；实测按轮数排会把 0.5 分的"自然光效运用"排进队列，
    而高价值的"视角反转"被挤掉。分档插在价值之后、把握之前：
    同样有用的两条，优先留库外新词。
    """
    candidate, verdict, _ = item
    value = (verdict or {}).get("value")
    value_rank = value if isinstance(value, int) else 0
    confidence = verdict["confidence"] if verdict else 0.8
    return (-value_rank, -_band_rank(alignment, candidate.tag), -confidence,
            -candidate.runs)


def _plan(alignment: dict[str, Alignment], settings: Settings,
          candidate) -> tuple[Alignment | None, bool]:
    """(对齐结论, 是否替换成库内标签)。

    同名 → 一律算库内标签（替换开关不影响这条：名字本来一样）；
    疑似 → 复核通过 + 替换开关打开才换成库内标准名。
    """
    item = alignment.get(candidate.tag)
    same_name = bool(item is not None and item.band == BAND_DUPLICATE
                     and item.matched_tag_id)
    replace_suspect = bool(item is not None
                           and settings.open_library_replace_enabled
                           and item.band == BAND_SUSPECT
                           and item.same_concept is True
                           and item.matched_tag_id)
    return item, same_name or replace_suspect


def _rank_and_truncate(settings: Settings, alignment: dict[str, Alignment],
                       accepted: list, limits: _GenerationLimits
                       ) -> tuple[list, list[dict], dict]:
    """小节 5 中段：按 value 分/分档排序，再按总量、来源与场景级配额截断。"""
    total_limit = limits.total_limit
    limit = limits.limit
    scene_limit = limits.scene_limit
    accepted.sort(key=partial(_rank, alignment))

    plans = {candidate.tag: _plan(alignment, settings, candidate)
             for candidate, _, _ in accepted}
    # 按原排序分配名额，同时执行总量、来源和场景级配额。
    by_origin: dict[bool, list] = {False: [], True: []}
    truncated = []
    scene_count = 0
    kept_count = 0
    for item in accepted:
        candidate = item[0]
        queue = by_origin[plans[candidate.tag][1]]
        if (kept_count >= total_limit or len(queue) >= limit
                or (candidate.scope == "scene" and scene_count >= scene_limit)):
            truncated.append(item)
            continue
        queue.append(item)
        kept_count += 1
        scene_count += candidate.scope == "scene"
    accepted = by_origin[False] + by_origin[True]
    return accepted, truncated, plans


def _assign_tag_dirs(spec: GenerationSpec, contents, embedder, library: list,
                     tag_library, accepted: list, plans: dict, dropped: list[dict],
                     *, require_model_dir: bool
                     ) -> tuple[list[ProducedTag], list[dict], list[dict]]:
    """小节 5 尾段：落库归属——库内替换生成 ProducedTag，新词按模型指定目录/向量回退归类。"""
    tags: list[ProducedTag] = []
    tag_rows: list[dict] = []
    # 新词要落到"可扩展目录"里：先批量嵌入候选名，再逐个按目录描述相似度归属
    new_names = [candidate.tag for candidate, _, _ in accepted
                 if not require_model_dir and not plans.get(candidate.tag, (None, False))[1]]
    new_vectors = (embedder.embed(new_names, role="query")
                   if new_names else [])
    vector_by_name = {name: vec for name, vec in zip(new_names, new_vectors)}
    per_dir_count: dict[str, int] = {}
    replacements: list[dict] = []
    for candidate, verdict, evidence in accepted:
        confidence = verdict["confidence"] if verdict else 0.8
        # 层级随合并结果落库：film 需≥2 场景支撑（上游已校验），scene 为
        # 个别场景特征；scope 缺失（旧数据/解析失败）按 film 兜底
        level = candidate.scope if candidate.scope in ("film", "scene") else "film"
        item, is_replace = plans.get(candidate.tag, (None, False))
        if is_replace and item is not None:
            matched = next((tag for tag in library
                            if tag.tag_id == item.matched_tag_id), None)
            mount = resolve_mount(matched.scope if matched else "content",
                                  spec.asset_id, contents, evidence)
            if mount is None:
                dropped.append({"tag": candidate.tag,
                                "why": f"目录适用范围 {matched.scope if matched else '?'} "
                                       "解析不出唯一挂载点"})
                continue
            if item.band == BAND_SUSPECT:
                replacements.append({
                    "generated": candidate.tag,
                    "library": item.matched_name,
                    "tag_id": item.matched_tag_id,
                    "cosine": (None if item.cosine is None
                               else round(item.cosine, 3)),
                    "reason": item.reason,
                })
            tags.append(ProducedTag(
                origin="catalog", tag_id=item.matched_tag_id,
                tag_name=item.matched_name or candidate.tag,
                candidate_type=None, candidate_description=None,
                relevance_score=item.cosine, confidence=confidence,
                evidence=evidence, level=level, generation_reason=candidate.reason,
                dir_id=matched.dir_id if matched else None,
                target_type=mount.target_type, target_id=mount.target_id,
                # 相似命中：模型原词将来写进 tag_define.aliases
                alias_name=(candidate.tag
                            if item.band == BAND_SUSPECT
                            and candidate.tag != (item.matched_name or "") else None),
            ))
            tag_rows.append({"tag": candidate.tag, "name": tags[-1].tag_name,
                             "origin": "catalog", "band": item.band,
                             "dimension": None,
                             "dir_id": tags[-1].dir_id,
                             "dir_source": "library",
                             "target_type": mount.target_type,
                             "confidence": confidence,
                             "evidence": [e.scene_id for e in evidence]})
            continue

        # 新词：先看模型给的目录能不能用，再回退到向量归类
        if not spec.policy.allow_new_tags:
            dropped.append({"tag": candidate.tag, "why": "策略禁止产出新词"})
            continue
        if tag_library is None:
            dropped.append({"tag": candidate.tag, "why": "标签库不可用，新词无处归属"})
            continue
        # ① 模型在生成时就选好的目录：只做校验，合法就直接用（这是"生成时
        #    考虑目录"的主路径）；不合法记下原因，回退②，不静默改写归属。
        assignment = None
        mount = None
        dir_source = None
        dir_reason = ""
        reasons: list[str] = []
        model_dir = _resolve_dir_value(tag_library, candidate.dir_raw)
        if candidate.dir_raw and model_dir is None:
            reasons.append(f"模型给的目录「{candidate.dir_raw}」无法解析")
        if model_dir:
            assignment, mount, why = _validate_model_dir(
                tag_library, model_dir, spec, contents, evidence, per_dir_count)
            if assignment is not None:
                dir_source = "model"
            else:
                reasons.append(why)
                assignment = mount = None
        # ② 回退：按与目录描述的向量相似度逐个试（模型没给/给错时的兜底）
        if assignment is None and not require_model_dir:
            for option in rank_dir_assignments(tag_library, embedder,
                                               vector_by_name.get(candidate.tag),
                                               spec.policy.dir_ids):
                if option.dir_id is None:
                    reasons.append(f"无可归属目录（{option.reason}）")
                    continue
                if (spec.policy.max_tags_per_dir
                        and per_dir_count.get(option.dir_id, 0)
                        >= spec.policy.max_tags_per_dir):
                    reasons.append(f"目录「{option.dir_name}」已达产出上限")
                    continue
                candidate_mount = resolve_mount(option.scope, spec.asset_id,
                                                contents, evidence)
                if candidate_mount is None:
                    reasons.append(f"目录「{option.dir_name}」适用范围 "
                                   f"{option.scope} 解析不出唯一挂载点")
                    continue
                assignment, mount = option, candidate_mount
                dir_source = "fallback"
                dir_reason = option.reason
                break
        if assignment is None or mount is None:
            dropped.append({"tag": candidate.tag,
                            "why": "；".join(reasons) or "无可归属目录"})
            continue
        per_dir_count[assignment.dir_id] = per_dir_count.get(assignment.dir_id, 0) + 1
        tags.append(ProducedTag(
            origin="generated", tag_id=None, tag_name=candidate.tag,
            candidate_type=candidate.dimension,
            # 描述是"这个词本身"的通用释义；模型给的 desc 先落在这里，
            # 不合规的在本段之后统一补写/清空（见描述通用化那一步）。
            candidate_description=(candidate.desc or None),
            # 相关度 = 与库内最相近标签的余弦（对齐阶段的 top-1，判成新词也有
            # 值——"有多不像库里的东西"本身就是审核时要看的信号）。库里没标签
            # 或本条没参与对齐时才是 None。
            relevance_score=(item.cosine if item is not None else None),
            confidence=confidence,
            evidence=evidence, level=level, generation_reason=candidate.reason,
            dir_id=assignment.dir_id,
            target_type=mount.target_type, target_id=mount.target_id,
        ))
        tag_rows.append({"tag": candidate.tag, "name": tags[-1].tag_name,
                         "origin": "generated",
                         "band": (item.band if item is not None else None),
                         "dimension": candidate.dimension,
                         "dir_id": assignment.dir_id,
                         "dir_name": assignment.dir_name,
                         "dir_source": dir_source,
                         "dir_model_raw": candidate.dir_raw,
                         "dir_reason": (assignment.reason
                                        if dir_source == "model"
                                        else (dir_reason or assignment.reason)),
                         "target_type": mount.target_type,
                         "confidence": confidence,
                         "evidence": [e.scene_id for e in evidence]})
    return tags, tag_rows, replacements


def _generalize_descriptions(spec: GenerationSpec, llm, contents, tags,
                             tag_library) -> dict:
    """小节 5.5：描述通用化——剧情味描述批量补写成通用释义，补不出就留空。"""
    names = content_names(contents)
    needs_desc = [tag for tag in tags
                  if tag.origin == "generated"
                  and not is_general(tag.candidate_description, names)]
    desc_usage: dict = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    desc_filled: dict[str, str] = {}
    if needs_desc:
        desc_filled = fill_descriptions(
            _sample_client(spec, llm, "description", build_description_template),
            [{"tag": tag.tag_name,
              "dir": (" / ".join(tag_library.path_of(tag.dir_id))
                      if tag_library is not None and tag.dir_id else None)}
             for tag in needs_desc],
            names=names, run_id=spec.run_id, usage=desc_usage)
        for tag in needs_desc:
            tag.candidate_description = desc_filled.get(normalize_key(tag.tag_name))
    descriptions_summary = {
        "needs": len(needs_desc),
        "filled": len(desc_filled),
        "empty": len(needs_desc) - len(desc_filled),
        "usage": desc_usage,
    }
    return descriptions_summary


def _build_report(settings: Settings, spec: GenerationSpec, llm, contents,
                  elapsed: float, modalities, frame_count: int,
                  runs: list[dict], limits: _GenerationLimits, library: list,
                  hints: list[str], dir_hint: list[dict], usage: dict,
                  judge_usage: dict, merged, verdicts: dict[str, dict],
                  tag_rows: list[dict], alignment: dict[str, Alignment],
                  replacements: list[dict], verify_trace: dict,
                  verify_usage: dict, truncated: list,
                  descriptions_summary: dict, dropped: list[dict],
                  judge_trace: dict | None = None) -> dict:
    """小节 6：组装审计报告（落盘由 _write_report 负责）。"""
    limit = limits.limit
    total_limit = limits.total_limit
    scene_limit = limits.scene_limit
    scene_request_limit = limits.scene_request_limit
    film_request_limit = limits.film_request_limit
    return {
        "version": MTG_VERSION,
        "task_id": spec.task_id,
        "asset_id": spec.asset_id,
        "model": getattr(llm, "model", None),
        "judge_model": (settings.mtg_judge_model
                        if settings.mtg_judge_enabled and not settings.use_mock_llm
                        else "mock"),
        "params": {
            "runs": len(runs),
            "max_total_tags": total_limit,
            "max_scene_tags": scene_limit,
            "max_candidates_per_scene": scene_request_limit,
            "max_film_candidates": film_request_limit,
            "modalities": sorted(modalities),
            "frames_per_scene": settings.mtg_frames_per_scene,
            "max_frames": settings.mtg_max_frames,
            "merge_threshold": settings.mtg_merge_threshold,
            "judge_confidence_floor": _JUDGE_CONFIDENCE_FLOOR,
            "judge_score_floors": dict(_JUDGE_SCORE_FLOORS),
            "max_final_tags": limit,
            "library_enabled": bool(settings.open_library_enabled),
            "library_hint_count": len(hints),
            "suspect_cosine": settings.open_library_suspect_cosine,
            "name_overlap": settings.open_library_name_overlap,
            "verify_enabled": bool(settings.open_library_verify_enabled),
            "replace_enabled": bool(settings.open_library_replace_enabled),
            "dir_hint_max": settings.open_dir_hint_max,
            "dir_hint_count": len(dir_hint),
        },
        "scenes": len(contents.content.scenes),
        "frames": frame_count,
        "runs": runs,
        "usage": usage,
        "judge_usage": judge_usage,
        "judge_trace": judge_trace or {},
        "merged": [{"tag": m.tag, "aliases": list(m.aliases),
                    "runs": m.runs, "dimension": m.dimension,
                    "scope": m.scope}
                   for m in merged],
        "verdicts": list(verdicts.values()),
        "accepted": [{"tag": row["name"], "candidate": row["tag"],
                      "origin": row["origin"],
                      "dir_id": row.get("dir_id"),
                      "dir_source": row.get("dir_source"),
                      "dir_model_raw": row.get("dir_model_raw"),
                      "dir_reason": row.get("dir_reason"),
                      "confidence": row["confidence"], "band": row["band"],
                      "evidence": row["evidence"]}
                     for row in tag_rows],
        "dir_hint": [entry["dir_id"] for entry in dir_hint],
        "library": {
            "enabled": bool(library),
            "size": len(library),
            "hints": hints,
            "bands": summarize_bands(alignment),
            "align": [
                {"tag": name, "band": item.band, "reason": item.reason,
                 "matched": item.matched_name,
                 "cosine": None if item.cosine is None else round(item.cosine, 3),
                 "same_concept": item.same_concept}
                for name, item in alignment.items()
            ],
            "replacements": replacements,
            "verify_raw": verify_trace.get("raw"),
            "truncated": [
                {"tag": candidate.tag,
                 "band": (alignment[candidate.tag].band
                          if candidate.tag in alignment else None)}
                for candidate, _, _ in truncated
            ],
        },
        "verify_usage": verify_usage,
        "descriptions": descriptions_summary,
        "dropped": dropped,
        "elapsed_s": round(elapsed, 1),
    }


def run_open_generation(spec: GenerationSpec, session: Session, llm, embedder,
                        judge_client=None,
                        settings: Settings | None = None) -> list[ProducedTag]:
    """跑一次开放式生成，返回落库前的 ProducedTag 列表（不写库）。

    `settings` 传 run 级副本（开放评测用：文本模态 + 重定向审计目录）；
    None 时回落进程级 `get_settings()`，生产执行器不传参，行为不变。
    """
    settings = settings or get_settings()
    started = time.monotonic()
    contents = spec.content

    # 输入模态（默认三种全开）。消融实验只改这一项，其余参数一律不动——
    # 见 `eval/ablation.py` 的纪律说明。
    modalities = settings.mtg_modality_set
    frames = (keyframes_mod.plan_scene_frames(
        contents, settings.dataset_root,
        per_scene=settings.mtg_frames_per_scene,
        max_frames=settings.mtg_max_frames,
        movie_asset_root=settings.movie_asset_root,
        extra_roots=settings.movie_asset_extra_roots,
    ) if "video" in modalities else {})
    spec.frames_by_scene = frames
    frame_count = sum(len(v) for v in frames.values())
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    # ---- 0. 标签库：给模型看的"已有标签"清单（只读，绝不写库）----
    tag_library, library, hints, dir_hint = _load_library_context(
        settings, spec, embedder, contents)

    # ---- 1. 逐场生成 → 全片综合 ----
    limits = _plan_generation_limits(settings, spec, contents)
    if limits is None:
        return []
    expandable_dirs = tag_library.new_tag_dirs(spec.policy.dir_ids) if tag_library else []
    catalog_only = tag_library is not None and (
        not spec.policy.allow_new_tags or not expandable_dirs
    )
    runs, scene_entries, film_entries = _run_generation_rounds(
        settings, spec, llm, contents, frames, modalities, usage, hints,
        dir_hint, limits, catalog_only=catalog_only)

    # ---- 2. 两级候选合并 ----
    merged = _merge_two_level_candidates(settings, embedder, film_entries,
                                         scene_entries)

    # ---- 3. judge ----
    judge_trace: dict = {}
    verdicts, judge_usage = _judge_merged_candidates(
        settings, spec, llm, judge_client, contents, merged, modalities,
        usage, frame_count, started, trace=judge_trace,
        tag_library=tag_library, dir_hint=dir_hint)

    # ---- 4. 过滤 + 证据 + 目录匹配 ----
    accepted, dropped = _filter_candidates(settings, contents, verdicts,
                                           modalities, merged)

    # ---- 5. 库内对齐、复核、排序、截断、落库归属 ----
    # 对齐放在截断之前：分档要参与排序（新词优先占名额），也要在审计里
    # 记录"被挤掉的是哪一档"，截断后再对齐就只剩 15 条、看不到全貌。
    alignment, verify_usage, verify_trace = _align_and_verify_candidates(
        settings, spec, llm, embedder, library, accepted)
    accepted, truncated, plans = _rank_and_truncate(settings, alignment,
                                                    accepted, limits)
    # 单分支或显式选择目录时，向量排序只会从受限列表硬选一个目录，
    # 无法判断新词是否属于该主题。此时只接收有效的模型目录。
    single_branch = (len(dir_hint) == len(expandable_dirs)
                     and len({entry["path"].split("/", 1)[0] for entry in dir_hint}) == 1)
    require_model_dir = bool(dir_hint) and (bool(spec.policy.dir_ids) or single_branch)
    tags, tag_rows, replacements = _assign_tag_dirs(
        spec, contents, embedder, library, tag_library, accepted, plans, dropped,
        require_model_dir=require_model_dir,
    )

    # ---- 5.5 描述通用化 ----
    # 生成侧只天然知道"本片哪里有依据"（reason），而库里要的是"这个词是什么"
    # 的通用释义。模型照提示给了 desc 就直接用；没给或写得像剧情（含片名/
    # 人名/场景/“本片”）的，这里批量补写一次；补不出来就留空——宁可没有描述，
    # 也不把某一部片的剧情当标签释义落库。
    descriptions_summary = _generalize_descriptions(spec, llm, contents, tags,
                                                    tag_library)

    # ---- 6. 审计落盘 ----
    elapsed = time.monotonic() - started
    report = _build_report(settings, spec, llm, contents, elapsed, modalities,
                           frame_count, runs, limits, library, hints, dir_hint,
                           usage, judge_usage, merged, verdicts, tag_rows,
                           alignment, replacements, verify_trace, verify_usage,
                           truncated, descriptions_summary, dropped,
                           judge_trace=judge_trace)
    _write_report(settings, spec.task_id, report)
    logger.info("mtg 完成：%d 轮 / %d 帧 / 合并 %d / 接受 %d / %.1fs",
                len(runs), frame_count, len(merged), len(tags), elapsed)
    return tags


def _write_report(settings: Settings, task_id: str, report: dict) -> None:
    """审计报告落盘；写失败只告警，不影响生成结果。"""
    try:
        target = Path(settings.mtg_report_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{task_id}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:  # 磁盘问题不该让任务失败
        logger.warning("审计报告写入失败 %s：%s", task_id, exc)
