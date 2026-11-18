import json
import time
from copy import deepcopy
from pathlib import Path
from threading import Event

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session

from tagplatform.db import create_engine_for
from tagplatform.db.models import MediaAsset, TagGenerationTask, TagQualityEvaluation
from tagplatform.eval.tag_quality import evaluate
from tagplatform.generation.base import ProducedTag
from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.llm.client import LLMResponse
from tagplatform.llm.mock import MockLLMClient
from tagplatform.settings import get_settings
from tests.test_api_generation import CONTENT_FIXTURE_DICT

_URL = "/api/v1/generation/tasks"


def _emit(items: list[dict]) -> LLMResponse:
    return LLMResponse(json.dumps({"tags": items}), {}, {}, True)


def _item(label_id: int, relevance=2, semantics=2) -> dict:
    return {
        "id": label_id,
        "relevance": {"score": relevance, "reason": "内容评分"},
        "semantics": {"score": semantics, "reason": "语义评分"},
        "evidence": [{"scene_id": "scene_001", "description": "仓库争执"}],
    }


def _request_payload(messages):
    body = messages[1]["content"]
    if isinstance(body, list):
        return body[0]["text"], json.loads(body[-1]["text"].rsplit("\n", 1)[1])
    material, _, labels = body.rpartition("\n\n## 待评估标签\n")
    return material, json.loads(labels)


def _wait_report(client, task_id, status):
    deadline = time.monotonic() + 5
    while True:
        response = client.get(f"{_URL}/{task_id}")
        assert response.status_code == 200, response.text
        report = response.json()["quality_evaluation"]
        if report["status"] == status:
            return report
        assert time.monotonic() < deadline, report
        time.sleep(0.025)


def test_production_snapshot_async_review_and_retry(client, monkeypatch):
    first_started, release_first = Event(), Event()
    requests = {}

    def produce(self, spec, session, llm, embedder):
        if spec.asset_id == "movie_empty":
            return []
        catalog = ProducedTag("catalog", "tag_action", "动作", None, None, 1, 1, [])
        if spec.asset_id != "movie_001":
            return [catalog]
        return [
            catalog,
            ProducedTag("generated", None, "待审核原名", "主题", "待审核原释义", 1, 1, [],
                        dir_id="dir_theme", generation_reason="模型提供的线索"),
            ProducedTag("generated", None, "无释义标签", "主题", None, 1, 1, [],
                        dir_id="dir_theme"),
        ]

    def complete(self, messages, run_id):
        material, labels = _request_payload(messages)
        asset = labels[0]["target_id"]
        requests.setdefault(asset, []).append((self.model, material, labels))
        if asset == "movie_001" and len(requests[asset]) == 1:
            first_started.set()
            assert release_first.wait(10)
            return _emit([_item(labels[0]["id"]), _item(labels[2]["id"], None)])
        if asset == "movie_bad" and self.model != "replacement":
            return LLMResponse("", {}, {"error": "评估端点超时"}, False)
        return _emit([_item(label["id"], 0, 1) for label in labels])

    monkeypatch.setattr(OpenTagProducer, "produce", produce)
    monkeypatch.setattr(MockLLMClient, "complete", complete)
    assets = ["movie_001", "movie_other", "movie_empty", "movie_bad"]
    for asset in assets:
        content = deepcopy(CONTENT_FIXTURE_DICT)
        content["asset"].update(asset_id=asset, title=asset)
        assert client.post("/api/v1/content/import", json={
            "kind": "platform", "data": content,
        }).status_code == 200

    try:
        response = client.post(_URL, json={
            "asset_ids": assets, "mode": "open", "quality_model": " custom-model ",
        })
        assert response.status_code == 200, response.text
        created = {row["asset_id"]: row for row in response.json()["tasks"]}
        assert {row["status"] for row in created.values()} == {"succeeded"}
        assert first_started.wait(3)
        task_id = created["movie_001"]["task_id"]
        task_url = f"{_URL}/{task_id}"
        detail = client.get(task_url).json()
        assert detail["quality_evaluation"]["status"] == "running"
        assert detail["quality_evaluation"]["film_counts"]["not_needed"] == 1
        assert len(client.get("/api/v1/assets/movie_001/tags").json()) == 1
        pre_id = next(row["id"] for row in detail["results"] if row["candidate_name"] == "待审核原名")
        review = client.post(f"/api/v1/asset-tags/{pre_id}/review", json={
            "action": "modify",
            "payload": {"tag_name": "审核后名称", "description": "审核后释义", "dir_id": "dir_theme"},
        })
        assert review.status_code == 200, review.text
        with client.app.state.session_factory() as session:
            session.get(MediaAsset, "movie_001").content_json = None
            session.commit()
    finally:
        release_first.set()

    report = _wait_report(client, task_id, "partial")
    assert report["film_counts"]["failed"] == 1
    assert report["film_counts"]["completed"] == 1
    assert report["film_counts"]["partial"] == 1
    assert report["incomplete"] is True
    assert report["metrics"]["total_tags"] == 5
    assert report["metrics"]["coverage"] == pytest.approx(3 / 5)
    assert report["metrics"]["joint_high_rate"] == pytest.approx(1 / 2)
    assert report["metrics"]["relevance"]["distribution"] == [1, 0, 1]
    assert report["metrics"]["semantics"]["average"] == 1.5
    assert report["metrics"]["undetermined_tags"] == 1
    first = next(film for film in report["films"] if film["asset_id"] == "movie_001")
    assert [label["disposition"] for label in first["labels"]] == ["mounted", "pending", "pending"]
    assert first["labels"][0]["desc"] == "以打斗追逐为主要看点"
    assert first["labels"][0]["score"]["evidence"][0]["start_ms"] == 120000
    assert first["labels"][1]["name"] == "待审核原名"
    assert first["labels"][1]["score"] is None
    assert first["labels"][2]["score"]["semantics"]["score"] is None
    assert requests["movie_001"][0][0] == "custom-model"

    assert client.post(f"{task_url}/quality/retry").status_code == 200
    deadline = time.monotonic() + 5
    while True:
        report = client.get(task_url).json()["quality_evaluation"]
        if report["film_counts"]["completed"] == 2:
            break
        assert time.monotonic() < deadline, report
        time.sleep(0.025)
    assert requests["movie_001"][1][1] == requests["movie_001"][0][1]
    assert len(requests["movie_001"][1][2]) == 1
    assert requests["movie_001"][1][2][0]["desc"] == "待审核原释义"
    assert client.post(f"{task_url}/quality/retry").status_code == 409
    bad_id = created["movie_bad"]["task_id"]
    assert client.post(f"{_URL}/{bad_id}/quality/retry",
                       json={"model": "replacement"}).status_code == 200
    report = _wait_report(client, task_id, "completed")
    assert report["metrics"]["coverage"] == 1
    assert report["incomplete"] is False
    assert requests["movie_bad"][-1][0] == "replacement"


def test_migration_preserves_existing_production_results(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    db_path = str(tmp_path / "quality.db")
    monkeypatch.setenv("TAGPLT_DATABASE_PATH", db_path)
    get_settings.cache_clear()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    engine = create_engine_for(db_path)
    try:
        command.upgrade(cfg, "0012")
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO media_asset (asset_id, asset_type, title) VALUES ('a', 'movie', 'old')"
            ))
            connection.execute(text(
                "INSERT INTO tag_generation_task (task_id, asset_id, mode, status) "
                "VALUES ('old_task', 'a', 'open', 'succeeded')"
            ))
            connection.execute(text(
                "INSERT INTO pre_asset_tag (asset_id, task_id, target_id, origin, candidate_name) "
                "VALUES ('a', 'old_task', 'a', 'open', '旧标签')"
            ))
        command.upgrade(cfg, "head")
        with Session(engine) as session:
            task = session.get(TagGenerationTask, "old_task")
            assert task.status == "succeeded"
            assert task.batch_id is None and task.quality_model is None
            assert session.get(TagQualityEvaluation, "old_task") is None
            assert session.scalar(text("SELECT candidate_name FROM pre_asset_tag")) == "旧标签"
    finally:
        get_settings.cache_clear()
        engine.dispose()


def test_label_batches_split_after_truncation_and_retry_without_unsupported_frames(tmp_path):
    path = tmp_path / "keyframe.jpg"
    path.write_bytes(b"keyframe")
    evaluation = TagQualityEvaluation(
        task_id="task", content_json=json.dumps(CONTENT_FIXTURE_DICT),
        labels=[{"id": i, "desc": "释义"} for i in range(3)],
        frame_paths={"scene_001": [str(path)]}, scores={},
    )
    calls = []

    class Client:
        def complete(self, messages, run_id):
            frames = isinstance(messages[1]["content"], list)
            _, labels = _request_payload(messages)
            ids = [label["id"] for label in labels]
            calls.append((frames, ids))
            if frames:
                assert any(part["type"] == "image_url" for part in messages[1]["content"])
                return LLMResponse("", {}, {"error": "image_url is not supported"}, False)
            if len(labels) == 3:
                return LLMResponse("", {}, {"choices": [{"finish_reason": "length"}]}, True)
            if ids == [0]:
                return _emit([_item(0, False), _item(99)])
            return _emit([_item(1), _item(1), _item(2)])

    scores, error, note = evaluate(Client(), evaluation)
    assert calls == [(True, [0, 1, 2]), (False, [0, 1, 2]), (False, [0]), (False, [1, 2])]
    assert set(scores) == {"2"}
    assert error == "2 个标签未返回有效评分"
    assert "未接受关键帧" in note
