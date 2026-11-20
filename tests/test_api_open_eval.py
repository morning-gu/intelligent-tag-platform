import json
import time
from pathlib import Path

import pytest
from sqlalchemy import event

from tagplatform.llm import MockLLMClient

BASE = "/api/v1/eval/open"
TT = "tt0000001"


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "MovieTagsV2"
    movie = root / "data" / TT
    frames = movie / "frames"
    frames.mkdir(parents=True)
    frame = frames / "scene_001_frame_1.jpg"
    frame.write_bytes(b"test-frame")
    (movie / "content.json").write_text(json.dumps({
        "asset": {"asset_id": TT, "title": "样本影片"},
        "content": {"description": "样本影片的整体描述", "scenes": [{
            "scene_id": "scene_001", "start_ms": 0, "end_ms": 60000,
            "description": "两人在昏暗的剧场内交换一份失散多年的家书。",
            "frames": [f"frames/{frame.name}"],
        }]},
    }, ensure_ascii=False), encoding="utf-8")
    return root


def _start(client, dataset):
    response = client.post(f"{BASE}/dataset-root", json={"root": str(dataset)})
    assert response.status_code == 200
    assert client.get(f"{BASE}/datasets").json()[0]["id"] == dataset.name
    assert client.get(f"{BASE}/datasets/{dataset.name}/movies").json() == [
        {"tt": TT, "title": "样本影片"},
    ]
    response = client.post(f"{BASE}/runs", json={
        "dataset": dataset.name, "movies": {"mode": "custom", "ids": [TT]},
        "quality_model": "test-quality-model",
    })
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    for _ in range(200):
        state = client.get(f"{BASE}/runs/{run_id}").json()
        if state["status"] != "running":
            return run_id, state
        time.sleep(0.01)
    pytest.fail(f"{run_id} 未完成")


@pytest.mark.empty_library
def test_open_eval_reads_dataset_frames_and_scores_without_database_access(client, dataset, monkeypatch):
    image_urls = []
    statements = []

    class RecordingQualityClient(MockLLMClient):
        def complete(self, messages, run_id):
            image_urls.extend(
                part["image_url"]["url"] for part in messages[1]["content"]
                if part["type"] == "image_url"
            )
            return super().complete(messages, run_id)

    monkeypatch.setattr(
        "tagplatform.open_eval.make_quality_client",
        lambda settings, model: RecordingQualityClient(model),
    )

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        run_id, state = _start(client, dataset)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert state["status"] == "succeeded" and state["failed"] == 0
    # 只有 API 登录态校验的查询。评估线程没有连接业务数据库。
    assert statements and all("FROM user_account" in sql for sql in statements)
    report = client.get(f"{BASE}/runs/{run_id}/report").json()
    assert report["meta"]["quality_model"] == "test-quality-model"
    assert report["film_counts"] == {"completed": 1}
    assert report["metrics"]["coverage"] == 1
    assert report["metrics"]["total_tags"] > 0
    film = report["films"][0]
    assert film["frame_count"] == 1 and len(image_urls) == 1
    assert all(label["score"]["value"]["score"] == 2 and label["level"] == "scene"
               for label in film["produced"])
    assert (Path(client.app.state.settings.open_eval_output_root)
            / run_id / "generation" / f"{TT}.json").is_file()
    assert client.get(f"{BASE}/runs/{run_id}/files/report.json").json() == report
    assert client.get(f"{BASE}/runs").json()[0]["quality_coverage"] == 1
    assert client.get(f"/api/v1/eval/closed/runs/{run_id}/report").status_code == 404


def test_open_eval_keeps_generated_labels_when_scoring_fails(client, dataset, monkeypatch):
    monkeypatch.setattr(
        "tagplatform.open_eval.evaluate_labels",
        lambda *_args: ({}, "测试评分失败", None),
    )
    run_id, state = _start(client, dataset)
    assert state["done"] == state["failed"] == 1
    report = client.get(f"{BASE}/runs/{run_id}/report").json()
    assert report["incomplete"] and report["film_counts"] == {"failed": 1}
    assert report["metrics"]["coverage"] == 0
    assert report["films"][0]["error"] == "测试评分失败"
    labels = report["films"][0]["produced"]
    assert labels and all(label["score"] is None for label in labels)
