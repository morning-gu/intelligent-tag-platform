"""影片评估队列、单片重试与生产批次汇总。"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session, undefer

from tagplatform.db.models import MediaAsset, TagGenerationTask, TagQualityEvaluation
from tagplatform.eval.tag_quality import DIMENSIONS, PROMPT_VERSION, ScoreDetails, evaluate
from tagplatform.llm import MockLLMClient, OpenAIClient
from tagplatform.settings import Settings

logger = logging.getLogger(__name__)


class QualityExecutor:
    def __init__(self, session_factory, settings: Settings):
        self._session_factory = session_factory
        self._settings = settings
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.generation_workers), thread_name_prefix="tag-quality",
        )

    def _client(self, model: str):
        settings = self._settings
        if settings.use_mock_llm:
            return MockLLMClient(model=model)
        return OpenAIClient(
            model=model, base_url_env=settings.llm_base_url_env,
            api_key_env=settings.llm_api_key_env, temperature=0,
            request_interval=settings.llm_request_interval, max_tokens=8192,
        )

    def resume_pending(self) -> None:
        """恢复进程中断前已落库的评估工作。"""
        with self._session_factory() as session:
            session.execute(update(TagQualityEvaluation)
                            .where(TagQualityEvaluation.status == "running")
                            .values(status="queued"))
            pending = session.scalars(select(TagQualityEvaluation.task_id)
                                      .where(TagQualityEvaluation.status == "queued")).all()
            session.commit()
        for task_id in pending:
            self.submit(task_id)

    def submit(self, task_id: str) -> None:
        self._pool.submit(self._run, task_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)

    def _run(self, task_id: str) -> None:
        try:
            self._evaluate_task(task_id)
        except Exception:
            logger.exception("标签质量评估线程异常 task_id=%s", task_id)

    def _evaluate_task(self, task_id: str) -> None:
        with self._session_factory() as session:
            claimed = session.execute(update(TagQualityEvaluation).where(
                TagQualityEvaluation.task_id == task_id,
                TagQualityEvaluation.status == "queued",
            ).values(status="running")).rowcount
            session.commit()
            if not claimed:
                return
            evaluation = session.get(TagQualityEvaluation, task_id, options=[
                undefer(TagQualityEvaluation.content_json),
                undefer(TagQualityEvaluation.frame_paths),
            ])

        # 模型执行期间无需占用数据库连接，审核和生成可以继续写入。
        if evaluation.prompt_version != PROMPT_VERSION:
            evaluation.scores, evaluation.note = {}, None
        try:
            scores, error, note = evaluate(self._client(evaluation.model), evaluation)
        except Exception as exc:
            logger.exception("标签质量评估失败 task_id=%s", task_id)
            scores, error, note = evaluation.scores, str(exc)[:1000], evaluation.note

        status = "completed" if len(scores) == len(evaluation.labels) else (
            "partial" if scores else "failed")
        with self._session_factory() as session:
            session.execute(update(TagQualityEvaluation).where(
                TagQualityEvaluation.task_id == task_id,
                TagQualityEvaluation.status == "running",
            ).values(
                status=status, scores=scores, error=error, note=note,
                prompt_version=PROMPT_VERSION,
            ))
            session.commit()


def queue_retry(session: Session, task_id: str, model: str | None) -> bool:
    evaluation = session.get(TagQualityEvaluation, task_id)
    if evaluation is None or evaluation.status not in ("failed", "partial"):
        return False
    values = {"status": "queued", "error": None}
    if model and model != evaluation.model:
        values.update(model=model, scores={}, note=None, prompt_version=PROMPT_VERSION)
    # 比较读取时的终态，避免并发重试重复提交。
    updated = session.execute(update(TagQualityEvaluation).where(
        TagQualityEvaluation.task_id == task_id,
        TagQualityEvaluation.status == evaluation.status,
    ).values(**values)).rowcount
    session.commit()
    return bool(updated)


class DimensionMetrics(BaseModel):
    average: float | None
    high_rate: float | None
    distribution: list[int]
    undetermined: int
    undetermined_rate: float | None


class QualityMetrics(BaseModel):
    total_tags: int
    evaluated_tags: int
    coverage: float | None
    joint_high_rate: float | None
    undetermined_tags: int
    undetermined_rate: float | None
    relevance: DimensionMetrics
    semantics: DimensionMetrics


class LabelQuality(BaseModel):
    id: int
    name: str
    desc: str | None
    disposition: Literal["mounted", "pending"]
    dir_path: str | None
    score: ScoreDetails | None


class FilmQuality(BaseModel):
    task_id: str
    asset_id: str
    title: str
    status: str
    model: str | None = None
    prompt_version: str | None = None
    error: str | None = None
    note: str | None = None
    metrics: QualityMetrics | None = None
    labels: list[LabelQuality]


class QualityReport(BaseModel):
    status: str
    incomplete: bool
    film_counts: dict[str, int]
    metrics: QualityMetrics
    films: list[FilmQuality]


def _ratio(count: int, total: int) -> float | None:
    return count / total if total else None


def _dimension_metrics(values: list[int | None]) -> DimensionMetrics:
    distribution = [values.count(value) for value in range(3)]
    judged = sum(distribution)
    unknown = values.count(None)
    return DimensionMetrics(
        average=_ratio(distribution[1] + 2 * distribution[2], judged),
        high_rate=_ratio(distribution[2], judged), distribution=distribution,
        undetermined=unknown, undetermined_rate=_ratio(unknown, len(values)),
    )


def _metrics(total: int, scores: list[dict]) -> QualityMetrics:
    pairs = [(score["relevance"]["score"], score["semantics"]["score"]) for score in scores]
    jointly_judged = sum(left is not None and right is not None for left, right in pairs)
    unknown = len(pairs) - jointly_judged
    return QualityMetrics(
        total_tags=total, evaluated_tags=len(pairs), coverage=_ratio(len(pairs), total),
        joint_high_rate=_ratio(pairs.count((2, 2)), jointly_judged),
        undetermined_tags=unknown, undetermined_rate=_ratio(unknown, len(pairs)),
        **{dim: _dimension_metrics([pair[index] for pair in pairs])
           for index, dim in enumerate(DIMENSIONS)},
    )


def quality_report(session: Session, task: TagGenerationTask) -> QualityReport:
    same_batch = (TagGenerationTask.batch_id == task.batch_id if task.batch_id
                  else TagGenerationTask.task_id == task.task_id)
    rows = session.execute(
        select(TagGenerationTask, MediaAsset.title, TagQualityEvaluation)
        .join(MediaAsset, MediaAsset.asset_id == TagGenerationTask.asset_id)
        .outerjoin(TagQualityEvaluation, TagQualityEvaluation.task_id == TagGenerationTask.task_id)
        .where(same_batch)
        .order_by(TagGenerationTask.task_id)
    ).all()
    films, scores = [], []
    total = 0
    counts = dict.fromkeys((
        "queued", "running", "completed", "partial", "failed", "not_needed",
        "generation_failed", "unavailable",
    ), 0)
    for film, title, evaluation in rows:
        if evaluation is None:
            status = {"queued": "queued", "running": "queued",
                      "failed": "generation_failed"}.get(film.status, "unavailable")
            films.append(FilmQuality(task_id=film.task_id, asset_id=film.asset_id,
                                     title=title, status=status, labels=[],
                                     model=film.quality_model, error=film.error))
        else:
            status = evaluation.status
            labels = evaluation.labels
            film_scores = list(evaluation.scores.values())
            total += len(labels)
            scores.extend(film_scores)
            films.append(FilmQuality(
                task_id=film.task_id, asset_id=film.asset_id, title=title, status=status,
                model=evaluation.model, prompt_version=evaluation.prompt_version,
                error=evaluation.error, note=evaluation.note,
                metrics=_metrics(len(labels), film_scores) if labels else None,
                labels=[{**label, "score": evaluation.scores.get(str(label["id"]))}
                        for label in labels],
            ))
        counts[status] += 1

    incomplete = counts["completed"] + counts["not_needed"] < len(films)
    if counts["running"]:
        status = "running"
    elif counts["queued"]:
        status = "running" if scores else "queued"
    elif not incomplete:
        status = "completed" if counts["completed"] else "not_needed"
    elif scores or counts["not_needed"]:
        status = "partial"
    elif counts["failed"] or counts["generation_failed"]:
        status = "failed"
    else:
        status = "unavailable"
    return QualityReport(status=status, incomplete=incomplete, film_counts=counts,
                         metrics=_metrics(total, scores), films=films)
