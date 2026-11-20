"""在 MovieTags 素材上独立运行开放生成和生产质量评分。"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from tagplatform.eval.tag_quality import (
    DEFAULT_MODEL,
    MAX_SCORE,
    PROMPT_VERSION,
    evaluate_labels,
    make_quality_client,
    quality_metrics,
)
from tagplatform.eval_dataset import parse_movie_content, read_movie_data
from tagplatform.eval_runs import (
    DEFAULT_CONCURRENCY,
    RETRY_PER_MOVIE,
    EvalJob,
    EvalRunService,
    EvalValidationError,
    MovieResult,
    dump_json,
    select_movies,
)
from tagplatform.generation.base import GenerationPolicy, GenerationSpec, ProducedTag
from tagplatform.generation.mtg.keyframes import plan_dataset_frames
from tagplatform.generation.mtg.pipeline import MTG_VERSION, run_open_generation
from tagplatform.settings import Settings

_MOVIE_ID = re.compile(r"tt[0-9]+")


def dataset_movies(root: str) -> list[str]:
    """发现具备内容的影片。开放生成的标签词表不依赖固定 ID 的 GT。"""
    data_dir = Path(root) / "data"
    if not data_dir.is_dir():
        return []
    return sorted(
        movie.name for movie in data_dir.iterdir()
        if _MOVIE_ID.fullmatch(movie.name) and (movie / "content.json").is_file()
    )


def dataset_movie_entries(root: str) -> list[dict[str, str]]:
    entries = []
    for tt in dataset_movies(root):
        try:
            raw = read_movie_data(Path(root) / "data" / tt)
            title = str(raw["asset"].get("title") or tt)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            title = tt
        entries.append({"tt": tt, "title": title})
    return entries


def _label(tag: ProducedTag, label_id: int) -> dict:
    return {
        "id": label_id, "name": tag.tag_name, "desc": tag.candidate_description,
        "target_type": tag.target_type, "target_id": tag.target_id, "level": tag.level,
        "scene_ids": list(dict.fromkeys(item.scene_id for item in tag.evidence)),
        "dimension": tag.candidate_type, "dir_id": None, "dir_path": None,
        "reason": tag.generation_reason,
    }


def _label_metrics(labels: list[dict]) -> dict:
    scores = [label["score"] for label in labels if label["score"] is not None]
    return quality_metrics(len(labels), scores, MAX_SCORE).model_dump()


@dataclass(frozen=True)
class _OpenJob(EvalJob):
    generation_settings: Settings
    quality_model: str


@dataclass
class _OpenMovieResult(MovieResult):
    status: str
    note: str | None = None
    frame_count: int = 0


class OpenEvalService(EvalRunService[_OpenJob, _OpenMovieResult]):
    """仅从数据集构造生成和评估输入，结果写入开放评估运行目录。"""

    def __init__(self, llm, embedder, settings: Settings) -> None:
        super().__init__(settings.open_eval_output_root, settings.open_eval_dataset_root)
        self._llm = llm
        self._embedder = embedder
        self.settings = settings

    def start(self, dataset_id: str, movies: dict, quality_model: str = DEFAULT_MODEL,
              concurrency: int = DEFAULT_CONCURRENCY, *, wait: bool = False) -> str:
        root = Path(self.effective_root).resolve()
        usable = dataset_movies(str(root))
        if dataset_id != root.name or not usable:
            raise EvalValidationError("开放评估数据集不存在或没有可用影片")
        try:
            selected = select_movies(usable, movies.get("mode"), movies.get("ids"))
        except ValueError as exc:
            raise EvalValidationError(str(exc)) from None
        if not selected:
            raise EvalValidationError("评测影片集为空：选片结果 0 部")
        if concurrency < 1:
            raise EvalValidationError("并发数必须至少为 1")

        def make_job(run_id: str, started_at: str) -> _OpenJob:
            settings = self.settings.model_copy(update={
                "mtg_report_dir": str(self.output_root / run_id / "generation"),
            })
            meta = {
                "run_id": run_id,
                "dataset": {"id": dataset_id, "root": str(root)},
                "prompt": {"mode": "open", "version": MTG_VERSION},
                "quality_prompt_version": PROMPT_VERSION,
                "quality_model": quality_model,
                "llm_model": getattr(self._llm, "model", ""),
                "embedding_model": getattr(self._embedder, "name", ""),
                "catalog_enabled": False,
                "judge_model": (settings.mtg_judge_model if settings.mtg_judge_enabled else None),
                "mock_llm": settings.use_mock_llm,
                "mock_embedder": settings.use_mock_embedder,
                "modalities": sorted(settings.mtg_modality_set),
                "frames_per_scene": settings.mtg_frames_per_scene,
                "max_frames": settings.mtg_max_frames,
                "max_final_tags": settings.mtg_max_final_tags,
                "movies": {"mode": movies.get("mode"), "ids": selected},
                "concurrency": concurrency,
            }
            return _OpenJob(run_id, selected, concurrency, started_at, meta, str(root),
                            settings, quality_model)

        return self._launch(selected, make_job, wait=wait)

    def _run_movie(self, job: _OpenJob, tt: str) -> _OpenMovieResult:
        started = time.perf_counter()
        title = tt
        frame_count = 0

        def result(status: str, produced: list[dict], error: str | None = None,
                   note: str | None = None) -> _OpenMovieResult:
            return _OpenMovieResult(
                tt=tt, title=title, ok=status not in ("failed", "generation_failed"),
                error=error, produced=produced,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                status=status, note=note, frame_count=frame_count,
            )

        settings = job.generation_settings
        movie_dir = Path(job.dataset_root) / "data" / tt
        try:
            raw = read_movie_data(movie_dir)
            frame_lists = {
                scene["scene_id"]: [path for path in (scene.get("frames") or []) if isinstance(path, str)]
                for scene in raw["content"]["scenes"]
            }
            content = parse_movie_content(raw)
            title = content.asset.title or tt
            frames = (plan_dataset_frames(content, movie_dir, frame_lists,
                                          settings.mtg_frames_per_scene, settings.mtg_max_frames)
                      if "video" in settings.mtg_modality_set else {})
            frame_count = sum(map(len, frames.values()))
        except Exception as exc:
            return result("generation_failed", [], f"{type(exc).__name__}: {exc}"[:1000])

        run_id = f"{job.run_id}:{tt}"
        spec = GenerationSpec(task_id=tt, asset_id=tt, content=content,
                              policy=GenerationPolicy(), prompt_text="", run_id=run_id)
        for attempt in range(1 + RETRY_PER_MOVIE):
            try:
                tags = run_open_generation(spec, None, self._llm, self._embedder,
                                           settings=settings, dataset_frames=frames)
                break
            except Exception as exc:
                if attempt == RETRY_PER_MOVIE:
                    return result("generation_failed", [], f"{type(exc).__name__}: {exc}"[:1000])

        if not tags:
            return result("not_needed", [])
        labels = [_label(tag, index) for index, tag in enumerate(tags, 1)]
        try:
            scores, error, note = evaluate_labels(
                make_quality_client(settings, job.quality_model), content, labels,
                {scene_id: [str(path) for path in paths]
                 for scene_id, paths in spec.frames_by_scene.items()}, run_id,
            )
        except Exception as exc:
            scores, error, note = {}, f"{type(exc).__name__}: {exc}"[:1000], None
        status = "completed" if len(scores) == len(labels) else "partial" if scores else "failed"
        produced = [{**label, "score": scores.get(str(label["id"])),
                     "confidence": tag.confidence, "evidence": [asdict(item) for item in tag.evidence]}
                    for label, tag in zip(labels, tags, strict=True)]
        return result(status, produced, error, note)

    @staticmethod
    def _read_summary(run_dir: Path) -> dict:
        try:
            metrics = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))["metrics"]
            return {"quality_coverage": metrics["coverage"],
                    "quality_full_rate": metrics["three_dimension_high_rate"]}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _write_artifacts(self, job: _OpenJob, results: list[_OpenMovieResult],
                         status: str, error: str | None, finished_at: str) -> None:
        run_dir = self.output_root / job.run_id
        results = sorted(results, key=lambda movie: movie.tt)
        meta = {
            **job.meta, "status": status, "error": error, "started_at": job.started_at,
            "finished_at": finished_at, "done": len(results), "total": len(job.movies),
            "failed": sum(not movie.ok for movie in results),
        }
        films = [asdict(movie) for movie in results]
        dump_json(run_dir / "raw_results.json", films)
        labels = [label for movie in results for label in movie.produced]
        counts = Counter(movie.status for movie in results)
        report = {
            "meta": meta,
            "incomplete": (status != "succeeded" or len(results) < len(job.movies)
                           or any(key in counts for key in ("partial", "failed", "generation_failed"))),
            "film_counts": dict(counts),
            "metrics": _label_metrics(labels),
            "films": [{**film, "metrics": _label_metrics(film["produced"]) if film["produced"] else None}
                      for film in films],
        }
        report_tmp = run_dir / "report.json.tmp"
        dump_json(report_tmp, report)
        report_tmp.replace(run_dir / "report.json")
        dump_json(run_dir / "meta.json", meta)
