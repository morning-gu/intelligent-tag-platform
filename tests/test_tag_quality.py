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

from tagplatform.capabilities.storage import create_engine_for
from tagplatform.capabilities.storage.models import MediaAsset, TagGenerationTask, TagQualityEvaluation
from tagplatform.eval.tag_quality import _parse_scores, evaluate
from tagplatform.services.generation.base import ProducedTag
from tagplatform.services.generation.open.mtg.prompt_samples import PromptSampleBatch
from tagplatform.services.generation.open.producer import OpenTagProducer
from tagplatform.capabilities.llm.client import LLMResponse
from tagplatform.capabilities.llm.mock import MockLLMClient
from tagplatform.settings import get_settings
from tests.test_api_generation import CONTENT_FIXTURE_DICT

_URL = "/api/v1/generation/tasks"


def _emit(items: list[dict]) -> LLMResponse:
    return LLMResponse(json.dumps({"tags": items}), {}, {}, True)


def _item(label_id: int, relevance=2, semantics=2, value=2) -> dict:
    return {
        "id": label_id,
        "relevance": {"score": relevance, "reason": "内容评分"},
        "semantics": {"score": semantics, "reason": "语义评分"},
        "value": {"score": value, "reason": "标签价值评分"},
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


def test_production_snapshot_async_review_and_retry(client, monkeypatch, tmp_path):
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
            missing_value = _item(labels[1]["id"])
            del missing_value["value"]
            return _emit([
                _item(labels[0]["id"], value=0), missing_value,
                _item(labels[2]["id"], None, value=None),
            ])
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
    assert report["metrics"]["max_score"] == 4
    assert report["metrics"]["joint_high_rate"] == 0
    assert report["metrics"]["three_dimension_high_rate"] == 0
    assert report["metrics"]["relevance"]["distribution"] == [1, 0, 1, 0, 0]
    assert report["metrics"]["semantics"]["average"] == 1.5
    assert report["metrics"]["undetermined_tags"] == 1
    assert report["metrics"]["value"]["distribution"] == [1, 0, 1, 0, 0]
    assert report["metrics"]["value"]["evaluated_tags"] == 3
    first = next(film for film in report["films"] if film["asset_id"] == "movie_001")
    assert [label["disposition"] for label in first["labels"]] == ["mounted", "pending", "pending"]
    assert first["labels"][0]["desc"] == "以打斗追逐为主要看点"
    assert first["labels"][0]["score"]["evidence"][0]["start_ms"] == 120000
    assert first["labels"][0]["score"]["value"] == {"score": 0, "reason": "标签价值评分"}
    assert first["labels"][1]["name"] == "待审核原名"
    assert first["labels"][1]["score"] is None
    assert first["labels"][2]["score"]["semantics"]["score"] is None
    assert requests["movie_001"][0][0] == "custom-model"

    sample_dir = tmp_path / "prompt-samples"
    example = json.loads((sample_dir / "quality_example.json").read_text(encoding="utf-8"))
    assert example["run_id"] == f"{created['movie_other']['task_id']}:tag-quality"
    assert example["model"] == "custom-model"
    assert _request_payload(example["messages"])[1] == requests["movie_other"][0][2]
    raw = json.loads((sample_dir / "quality_result.txt").read_text(encoding="utf-8"))
    assert raw["tags"] == [_item(requests["movie_other"][0][2][0]["id"], 0, 1)]
    template = json.loads((sample_dir / "quality_template.json").read_text(encoding="utf-8"))
    assert template["prompt_version"] == "tag-quality-v4"
    assert "恰有一项限定" in template["messages"][0]["content"]
    assert "image_url" in {part["type"] for part in template["messages"][1]["content"]}
    assert isinstance(template["messages_without_keyframes"][1]["content"], str)

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
    retry_example = json.loads((sample_dir / "quality_example.json").read_text(encoding="utf-8"))
    assert retry_example["run_id"] == f"{bad_id}:tag-quality"
    assert retry_example["model"] == "replacement"


def test_scene_evidence_is_limited_to_the_source_scene():
    item = _item(1)
    item["evidence"].append({"scene_id": "scene_002", "description": "别的场景"})
    labels = [{
        "id": 1, "desc": "对峙", "level": "scene", "target_type": "content",
        "target_id": "movie_001", "scene_ids": ["scene_001"],
    }]
    scores, complete = _parse_scores(
        json.dumps({"tags": [item]}), labels, {"scene_001": 100, "scene_002": 200})
    assert complete
    assert scores["1"]["evidence"] == [
        {"scene_id": "scene_001", "start_ms": 100, "description": "仓库争执"}
    ]


def test_high_relevance_requires_scoped_evidence():
    valid = _item(1, relevance=4, semantics=3, value=4)
    wrong_scene = _item(2, relevance=4)
    one_film_scene = _item(3, relevance=3)
    excessive = _item(4, value=5)
    labels = [
        {"id": 1, "desc": "对峙", "level": "scene", "scene_ids": ["scene_001"]},
        {"id": 2, "desc": "对峙", "level": "scene", "scene_ids": ["scene_002"]},
        {"id": 3, "desc": "对峙", "level": "film"},
        {"id": 4, "desc": "对峙", "level": "scene", "scene_ids": ["scene_001"]},
    ]
    scores, complete = _parse_scores(
        json.dumps({"tags": [valid, wrong_scene, one_film_scene, excessive]}),
        labels, {"scene_001": 100, "scene_002": 200})
    assert not complete
    assert list(scores) == ["1"]
    assert scores["1"]["relevance"]["score"] == 4
    assert scores["1"]["semantics"]["score"] == 3


def test_legacy_and_current_scores_have_separate_value_coverage(client):
    historical = _item(1)
    del historical["value"]
    results = {
        "legacy": ("tag-quality-v1", [historical]),
        "current": ("tag-quality-v2", [
            _item(index, value=rating) for index, rating in enumerate([0, 1, 2, None], start=2)
        ]),
    }
    with client.app.state.session_factory() as session:
        for task_id, (version, items) in results.items():
            session.add(MediaAsset(asset_id=task_id, asset_type="movie", title=task_id))
            session.flush()
            session.add(TagGenerationTask(task_id=task_id, asset_id=task_id, mode="open",
                                          status="succeeded", batch_id="mixed"))
            session.flush()
            session.add(TagQualityEvaluation(
                task_id=task_id, status="completed", model="test", prompt_version=version,
                labels=[{"id": item["id"], "name": "标签", "desc": "释义",
                         "dir_path": None, "disposition": "pending"} for item in items],
                scores={str(item["id"]): item for item in items}, frame_paths={},
            ))
        session.commit()

    report = _wait_report(client, "legacy", "completed")
    assert report["metrics"]["evaluated_tags"] == 5
    assert report["metrics"]["value"] == {
        "evaluated_tags": 4, "average": 1.0, "high_rate": pytest.approx(1 / 3),
        "distribution": [1, 1, 1], "undetermined": 1, "undetermined_rate": 0.25,
    }
    assert report["metrics"]["joint_high_rate"] == 1
    assert report["metrics"]["three_dimension_high_rate"] == pytest.approx(1 / 3)
    assert report["metrics"]["undetermined_tags"] == 1
    legacy = next(film for film in report["films"] if film["task_id"] == "legacy")
    assert legacy["labels"][0]["score"]["value"] is None
    assert legacy["metrics"]["value"]["evaluated_tags"] == 0
    assert legacy["metrics"]["value"]["undetermined"] == 0
    assert legacy["metrics"]["three_dimension_high_rate"] is None


def test_batch_keeps_three_and_five_level_scores_separate(client):
    ratings = [("old", "tag-quality-v3", 2, 2), ("new", "tag-quality-v4", 4, 3)]
    with client.app.state.session_factory() as session:
        for task_id, version, relevance, value in ratings:
            session.add(MediaAsset(asset_id=task_id, asset_type="movie", title=task_id))
            session.flush()
            session.add(TagGenerationTask(task_id=task_id, asset_id=task_id, mode="open",
                                          status="succeeded", batch_id="scale-comparison"))
            session.flush()
            session.add(TagQualityEvaluation(
                task_id=task_id, status="completed", model="test", prompt_version=version,
                labels=[{"id": 1, "name": "标签", "desc": "释义",
                         "dir_path": None, "disposition": "pending"}],
                scores={"1": _item(1, relevance, relevance, value)}, frame_paths={},
            ))
        session.commit()

    report = _wait_report(client, "new", "completed")
    assert report["metrics"]["coverage"] == 1
    assert report["metrics"]["max_score"] is None
    assert report["metrics"]["relevance"]["distribution"] == []
    assert report["metrics"]["relevance"]["average"] is None
    assert report["metrics"]["joint_high_rate"] is None
    films = {film["task_id"]: film["metrics"] for film in report["films"]}
    assert films["old"]["max_score"] == 2
    assert films["old"]["three_dimension_high_rate"] == 1
    assert films["new"]["max_score"] == 4
    assert films["new"]["joint_high_rate"] == 1
    assert films["new"]["three_dimension_high_rate"] == 0
    assert films["new"]["value"]["distribution"] == [0, 0, 0, 1, 0]
    assert films["new"]["value"]["average"] == 3


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
                return LLMResponse('{"tags":[', {}, {"choices": [{"finish_reason": "length"}]}, True)
            if ids == [0]:
                return _emit([_item(0, False), _item(99)])
            return _emit([_item(1), _item(1), _item(2)])

    scores, error, note = evaluate(Client(), evaluation, prompt_samples=PromptSampleBatch(str(tmp_path)))
    assert calls == [(True, [0, 1, 2]), (False, [0, 1, 2]), (False, [0]), (False, [1, 2])]
    assert set(scores) == {"2"}
    assert error == "2 个标签未返回有效评分"
    assert "未接受关键帧" in note
    assert not list(tmp_path.glob("quality_*"))


def test_prompt_sample_uses_first_complete_response_after_split(tmp_path):
    evaluation = TagQualityEvaluation(
        task_id="task", content_json=json.dumps(CONTENT_FIXTURE_DICT),
        labels=[{"id": i, "desc": "释义"} for i in range(4)], frame_paths={}, scores={},
    )
    responses = []

    class Client:
        model = "test-model"

        def complete(self, messages, run_id):
            _, labels = _request_payload(messages)
            if len(labels) == 4:
                return LLMResponse('{"tags":[', {}, {"choices": [{"finish_reason": "length"}]}, True)
            response = _emit([_item(label["id"]) for label in labels])
            responses.append((messages, response.content))
            return response

    scores, error, _ = evaluate(Client(), evaluation,
                               prompt_samples=PromptSampleBatch(str(tmp_path)))
    assert error is None
    assert set(scores) == {"0", "1", "2", "3"}
    assert len(responses) == 2
    example = json.loads((tmp_path / "quality_example.json").read_text(encoding="utf-8"))
    assert example["messages"] == responses[0][0]
    assert (tmp_path / "quality_result.txt").read_text(encoding="utf-8") == responses[0][1]
