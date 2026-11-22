"""对生产分流时的最终标签进行批量 LLM 评分。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, StrictInt, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tagplatform.services.content.content_io import load_content
from tagplatform.capabilities.prompts.quality import PROMPT_VERSION, RUBRIC_TEXT
from tagplatform.capabilities.storage.models import TagDefine, TagGenerationTask, TagQualityEvaluation
from tagplatform.services.generation.base import GenerationSpec
from tagplatform.services.generation.open.mtg.prompt_samples import PromptSampleBatch
from tagplatform.services.generation.open.mtg.prompts import encode_image, keyframe_parts, render_content
from tagplatform.services.tags.library import TagLibrary
from tagplatform.services.generation.persist import PersistSummary
from tagplatform.schemas import ContentJSON

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek-v4.1-flash"
MAX_SCORE = 4
DIMENSIONS = ("relevance", "semantics", "value")

# 评分 rubric（原 _PROMPT）与 PROMPT_VERSION 自 2026-11-22 文案集中起移驻
# tagplatform.capabilities.prompts.quality（本模块 import 消费，
# tasks/tag_quality 从本模块 import PROMPT_VERSION 的路径继续成立）。


class DimensionScore(BaseModel):
    score: Literal[0, 1, 2, 3, 4] | None
    reason: str = Field(min_length=1)


class EvaluationEvidence(BaseModel):
    scene_id: str | None = None
    start_ms: int | None = None
    description: str = Field(min_length=1)


class ScoreDetails(BaseModel):
    relevance: DimensionScore
    semantics: DimensionScore
    value: DimensionScore | None = None
    evidence: list[EvaluationEvidence] = Field(default_factory=list)


class TagScore(ScoreDetails):
    id: StrictInt
    value: DimensionScore


def snapshot(
    session: Session, task: TagGenerationTask, spec: GenerationSpec,
    summary: PersistSummary, content_json: str, library: TagLibrary | None,
) -> TagQualityEvaluation:
    """与生产分流同事务持久化，避免审核或重跑改变评估样本。"""
    session.flush()
    tag_ids = {row.tag_id for row, _ in summary.inserted_tags if row.tag_id}
    definitions = {
        item.tag_id: item for item in session.scalars(
            select(TagDefine).where(TagDefine.tag_id.in_(tag_ids))
        )
    } if tag_ids else {}
    labels = []
    for row, tag in summary.inserted_tags:
        definition = definitions.get(row.tag_id)
        dir_id = definition.dir_id if definition else row.candidate_dir_id
        labels.append({
            "id": row.id,
            "name": definition.tag_name if definition else tag.tag_name,
            "desc": definition.description if definition else row.candidate_description,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "level": (tag.level if task.mode == "open" and row.target_type != "character"
                      else {"content": "film"}.get(row.target_type, row.target_type)),
            "scene_ids": list(dict.fromkeys(item.scene_id for item in tag.evidence)),
            "dimension": tag.candidate_type,
            "dir_id": dir_id,
            "dir_path": "/".join(library.path_of(dir_id)) if dir_id and library else None,
            "disposition": "mounted" if row.review_status == "approved" else "pending",
            "reason": tag.generation_reason,
        })
    evaluation = TagQualityEvaluation(
        task_id=task.task_id,
        status="queued" if labels else "not_needed",
        model=task.quality_model or DEFAULT_MODEL,
        prompt_version=PROMPT_VERSION,
        labels=labels, scores={},
        content_json=content_json if labels else None,
        frame_paths={
            scene_id: [str(path.absolute()) for path in paths]
            for scene_id, paths in spec.frames_by_scene.items() if paths and labels
        },
    )
    session.add(evaluation)
    return evaluation


def _parse_scores(
    raw: str, labels: list[dict], scene_starts: dict[str, int],
) -> tuple[dict[str, dict], bool]:
    raw = raw.strip()
    first, separator, rest = raw.partition("\n")
    if separator and first.lower() in ("```json", "```") and rest.endswith("```"):
        raw = rest[:-3]
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("tags"), list):
        raise ValueError("评估响应缺少 tags 列表")
    expected = {label["id"]: label for label in labels}
    results = {}
    seen = set()
    for item in data["tags"]:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            continue
        label_id = item["id"]
        if label_id not in expected:
            continue
        key = str(label_id)
        if label_id in seen:
            results.pop(key, None)
            continue
        seen.add(label_id)
        # null 表示模型主动给出无法判断。缺字段、布尔值或非法分值属于未完成。
        if any(
            not isinstance(item.get(axis), dict)
            or "score" not in item[axis]
            or (item[axis]["score"] is not None and type(item[axis]["score"]) is not int)
            for axis in DIMENSIONS
        ):
            continue
        try:
            score = TagScore.model_validate(item)
        except ValidationError:
            continue
        if any(not getattr(score, axis).reason.strip() for axis in DIMENSIONS):
            continue
        if not (expected[label_id]["desc"] or "").strip():
            score.semantics = DimensionScore(score=None, reason="缺少标签最终释义")
        label = expected[label_id]
        local_scenes = label.get("scene_ids") or (
            [label.get("target_id")] if label.get("target_type") == "scene" else [])
        score.evidence = [
            item.model_copy(update={"start_ms": scene_starts.get(item.scene_id)})
            for item in score.evidence
            if (item.scene_id is None or item.scene_id in scene_starts)
            and (label.get("level") != "scene" or item.scene_id in local_scenes)
        ]
        # 高档需要可核验的场景锚点。片段是否独立、证据是否支持概念仍由评估员判断。
        minimum_scenes = {"scene": 1, "film": 2}.get(label.get("level"), 0)
        if score.relevance.score in (3, 4):
            cited = {item.scene_id for item in score.evidence if item.scene_id is not None}
            if len(cited) < minimum_scenes:
                continue
        results[key] = score.model_dump(exclude={"id"})
    return results, len(results) == len(labels) == len(data["tags"])


def _load_frame_parts(frame_paths: dict[str, list[str]]) -> tuple[list[dict], bool]:
    parts = []
    missing = False
    for scene_id, paths in frame_paths.items():
        for index, path in enumerate(paths, 1):
            try:
                url = encode_image(Path(path))
            except OSError:
                missing = True
                continue
            parts.extend(keyframe_parts(scene_id, index, len(paths), url))
    return parts, missing


def _build_messages(text: str, labels_json: str, frames: list[dict]) -> list[dict]:
    material = text if frames else text + "\n\n本次提供的影片材料仅含文本。"
    candidates = "\n\n## 待评估标签\n" + labels_json
    user_input = ([{"type": "text", "text": material}, *frames,
                   {"type": "text", "text": candidates}]
                  if frames else material + candidates)
    return [
        {"role": "system", "content": RUBRIC_TEXT},
        {"role": "user", "content": user_input},
    ]


def build_quality_template() -> dict:
    text = "{{影片摘要、人物、场景描述、台词与人物挂载点资料}}"
    labels = "{{分流后最终标签列表 JSON}}"
    frames = keyframe_parts("{{场景 ID}}", "{{帧序号}}", "{{该场景帧数}}",
                           "{{关键帧 data URL}}")
    return {
        "prompt_version": PROMPT_VERSION,
        "messages": _build_messages(text, labels, frames),
        "messages_without_keyframes": _build_messages(text, labels, []),
    }


def _render_material(content: ContentJSON, labels: list[dict]) -> str:
    material = render_content(content, full_cast=True)
    targets = {item["target_id"] for item in labels if item.get("target_type") == "character"}
    if not targets:
        return material
    characters = []
    for character in content.content.characters:
        if character.character_id in targets:
            appearances = [scene.scene_id for scene in content.content.scenes
                           if character.character_id in scene.character_ids]
            characters.append({"id": character.character_id, "name": character.name,
                               "description": character.description, "scenes": appearances})
    return material + "\n\n人物挂载点资料\n" + json.dumps(characters, ensure_ascii=False)


def _input_limit(error: str) -> bool:
    return any(term in error for term in (
        "context_length_exceeded", "maximum context length", "context window",
        "input is too long", "input length", "payload too large", "request too large",
        "too many images", "413 request", "上下文长度", "输入过长",
    ))


def _image_rejected(error: str) -> bool:
    return any(term in error for term in ("image", "vision", "multimodal", "图片", "图像")) and any(
        term in error for term in ("unsupported", "not support", "only support", "invalid",
                                    "not allowed", "不支持", "仅支持")
    )


def evaluate(
    client, evaluation: TagQualityEvaluation, *, prompt_samples: PromptSampleBatch | None = None,
) -> tuple[dict, str | None, str | None]:
    """默认一片一请求，长度超限才切分；保留成功批次及其分数。"""
    content = load_content(evaluation.content_json)
    text = _render_material(content, evaluation.labels)
    scene_starts = {scene.scene_id: scene.time_range.start_ms for scene in content.content.scenes}
    frames, missing = _load_frame_parts(evaluation.frame_paths)
    scores = dict(evaluation.scores)
    errors: list[str] = []
    notes = [evaluation.note] if evaluation.note else []
    run_id = f"{evaluation.task_id}:tag-quality"
    if missing:
        notes.append("部分生成关键帧已无法读取。")

    def batch(labels: list[dict], images: list[dict]) -> None:
        try:
            messages = _build_messages(text, json.dumps(labels, ensure_ascii=False), images)
            response = client.complete(messages, run_id)
        except Exception as exc:
            errors.append(f"评估调用失败：{exc}")
            return
        error = str(response.raw.get("error") or "模型未返回成功响应")
        input_limit = not response.ok and _input_limit(error.lower())
        if images and not response.ok and _image_rejected(error.lower()):
            notes.append("评估模型未接受关键帧，已使用文本材料评分。")
            frames.clear()
            batch(labels, [])
            return
        choices = response.raw.get("choices") or [{}]
        truncated = response.ok and choices[0].get("finish_reason") == "length"
        if (truncated or input_limit) and len(labels) > 1:
            middle = len(labels) // 2
            logger.info("标签质量评估分批重试 task_id=%s reason=%s batch_sizes=%d,%d",
                        evaluation.task_id, "output_length" if truncated else "input_length",
                        middle, len(labels) - middle)
            batch(labels[:middle], images)
            batch(labels[middle:], images)
            return
        if input_limit and images:
            notes.append("部分标签因请求超出长度限制，使用文本材料评分。")
            batch(labels, [])
            return
        if not response.ok or truncated:
            errors.append("模型输出超出长度限制" if truncated else f"评估调用失败：{error}")
            return
        try:
            batch_scores, complete = _parse_scores(response.content, labels, scene_starts)
        except ValueError as exc:
            errors.append(f"评估响应无法解析：{exc}")
            return
        scores.update(batch_scores)
        if complete and prompt_samples is not None:
            prompt_samples.capture("quality", build_quality_template, client, messages, run_id, response)

    batch([label for label in evaluation.labels if str(label["id"]) not in scores], frames)
    missing_scores = len(evaluation.labels) - len(scores)
    if missing_scores:
        errors.insert(0, f"{missing_scores} 个标签未返回有效评分")
    return (scores, "；".join(dict.fromkeys(errors))[:1000] or None,
            " ".join(dict.fromkeys(notes)) or None)
