"""按目标目录生成候选，保留逐场出处，并在过滤后选出聚类代表。"""
from __future__ import annotations

import logging
from dataclasses import replace

import numpy as np

from tagplatform.capabilities.llm.usage import accumulate_usage
from tagplatform.services.generation.open.mtg.merge import MergedCandidate
from tagplatform.services.generation.open.mtg.parse import parse_generation_items
from tagplatform.services.generation.open.mtg.prompts import (
    build_generation_messages, build_scene_generation_messages, target_generation_directory,
)
from tagplatform.services.tags.library import SCOPE_CONTENT, parse_scope

logger = logging.getLogger(__name__)

SCENE_REQUEST_LIMIT = 3
FILM_REQUEST_LIMIT = 5


def request_generation_items(client, messages, *, run_id, suffix, usage,
                             exact_names: bool = False) -> list[dict] | None:
    response = client.complete(messages, f"{run_id}:{suffix}")
    accumulate_usage(usage, response.usage)
    if not response.ok:
        raise RuntimeError(f"LLM 调用失败（{suffix}）: {response.raw.get('error')}")
    try:
        return parse_generation_items(response.content, max_items=None, exact_names=exact_names)
    except ValueError as exc:
        logger.warning("生成输出解析失败（%s）：%s", suffix, exc)
        return None


def _run_audit(scope, stage, requests, items, usage_before, usage) -> dict:
    return {
        "run": stage, "scope": scope, "items": len(items),
        "tags": [item["tag"] for item in items], "requests": requests,
        **{key: value - usage_before[key] for key, value in usage.items()},
    }


def _candidate(item: dict, *, scope: str, index: int) -> MergedCandidate:
    return MergedCandidate(
        tag=item["tag"], dimension=item["dimension"], scenes=tuple(item["scenes"]),
        runs=1, aliases=(item["tag"],), reason=item["reason"], desc=item["desc"],
        scope=scope, dir_raw=item["dir"], candidate_id=f"{scope}-{index:06d}",
    )


def generate_directory_candidates(scene_client, film_client, contents, frames, modalities, *,
                                  directories, hints, run_id, usage) -> tuple[list[dict], list[MergedCandidate]]:
    """一阶段按目录和原文去重，二阶段保留每次出现时的证据线索。"""
    scenes = contents.content.scenes
    per_scene: dict[str, list[dict]] = {scene.scene_id: [] for scene in scenes}
    grouped: dict[tuple[str, str], MergedCandidate] = {}
    requests = []
    parsed_calls = 0
    before = usage.copy()

    for scene_number, scene in enumerate(scenes, 1):
        scene_id = scene.scene_id
        base = build_scene_generation_messages(
            scene, frames.get(scene_id, ()), content=contents,
            max_tags=SCENE_REQUEST_LIMIT, dir_hint=[], modalities=modalities)
        for index, directory in enumerate(directories, 1):
            dir_id = directory["dir_id"]
            messages = target_generation_directory(
                base, directory, scope="scene", max_tags=SCENE_REQUEST_LIMIT)
            items = request_generation_items(
                scene_client, messages, run_id=run_id,
                suffix=f"scene-{scene_number:03d}-dir-{index:03d}",
                usage=usage, exact_names=True)
            parsed_calls += items is not None
            accepted = [{**item, "dir": dir_id, "scenes": [scene_id]}
                        for item in (items or [])[:SCENE_REQUEST_LIMIT]]
            requests.append({"scene_id": scene_id, "dir_id": dir_id,
                             "parsed": items is not None, "items": len(accepted)})
            per_scene[scene_id].extend(accepted)
            for item in accepted:
                key = (dir_id, item["tag"])
                previous = grouped.get(key)
                if previous is None:
                    grouped[key] = _candidate(item, scope="scene", index=len(grouped) + 1)
                    continue
                reason = item["reason"]
                if reason and reason != previous.reason:
                    reason = "\n".join(filter(None, (previous.reason, f"{scene_id}：{reason}")))
                else:
                    reason = previous.reason
                grouped[key] = replace(previous, scenes=(*previous.scenes, scene_id),
                                       reason=reason, desc=previous.desc or item["desc"])

    scene_items = [item for items in per_scene.values() for item in items]
    runs = [_run_audit("scene", 0, requests, scene_items, before, usage)]
    runs[0]["scenes"] = {scene_id: [item["tag"] for item in items]
                         for scene_id, items in per_scene.items()}
    runs[0]["after_exact_dedup"] = len(grouped)

    film_items: list[dict] = []
    film_dirs = [entry for entry in directories if SCOPE_CONTENT in parse_scope(entry["scope"])]
    if len(scenes) >= 2 and film_dirs:
        before = usage.copy()
        requests = []
        base = build_generation_messages(
            contents, frames, max_tags=FILM_REQUEST_LIMIT, scene_tags=per_scene,
            library_hint=hints, modalities=modalities)
        for index, directory in enumerate(film_dirs, 1):
            messages = target_generation_directory(
                base, directory, scope="film", max_tags=FILM_REQUEST_LIMIT)
            items = request_generation_items(
                film_client, messages, run_id=run_id, suffix=f"film-dir-{index:03d}",
                usage=usage, exact_names=True)
            parsed_calls += items is not None
            accepted = []
            for item in items or []:
                if item["scope"] not in (None, "film"):
                    continue
                evidence = list(dict.fromkeys(s for s in item["scenes"] if s in per_scene))
                if len(evidence) < 2:
                    continue
                accepted.append({**item, "dir": directory["dir_id"], "scenes": evidence})
                if len(accepted) == FILM_REQUEST_LIMIT:
                    break
            requests.append({"dir_id": directory["dir_id"], "parsed": items is not None,
                             "items": len(accepted)})
            film_items.extend(accepted)
        runs.append(_run_audit("film", 1, requests, film_items, before, usage))

    if not parsed_calls:
        raise RuntimeError("各轮生成均无法解析为 JSON")
    return runs, [*grouped.values(), *(
        _candidate(item, scope="film", index=i) for i, item in enumerate(film_items, 1)
    )]


def cluster_scene_candidates(accepted: list, embedder, *, scene_count: int,
                             directory_count: int) -> tuple[list, dict, dict[str, np.ndarray]]:
    """只聚类通过过滤的逐场候选，每簇选择距离中心最近的原始候选。"""
    scene_rows = [row for row in accepted if row[0].scope == "scene"]
    target = min(len(scene_rows), max(1, scene_count * directory_count // 3))
    audit = {"target": target, "input": len(scene_rows), "clusters": []}
    if not scene_rows:
        return accepted, audit, {}

    from sklearn.cluster import KMeans

    names = list(dict.fromkeys(row[0].tag for row in scene_rows))
    by_name = dict(zip(names, embedder.embed(names, role="query"), strict=True))
    vectors = np.asarray([by_name[row[0].tag] for row in scene_rows], dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors /= np.where(norms > 0.0, norms, 1.0)
    n_clusters = min(target, len(np.unique(vectors, axis=0)))
    model = KMeans(n_clusters=n_clusters, random_state=0, n_init=10).fit(vectors)

    representatives = set()
    for cluster in range(n_clusters):
        members = np.flatnonzero(model.labels_ == cluster)
        if not len(members):
            continue
        offsets = vectors[members] - model.cluster_centers_[cluster]
        representative = members[int(np.argmin(np.sum(offsets * offsets, axis=1)))]
        candidate_id = scene_rows[int(representative)][0].candidate_id
        representatives.add(candidate_id)
        audit["clusters"].append({
            "representative": candidate_id,
            "members": [scene_rows[int(i)][0].candidate_id for i in members],
        })
    kept = [row for row in accepted if row[0].scope == "film"
            or row[0].candidate_id in representatives]
    return kept, audit, by_name
