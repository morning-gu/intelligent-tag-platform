"""「上传即生成资产」链路测试（2026-11）。

两条链路各自验证：
1. HTTP 层：文件名命中数据集 → 资产主键变成 `movie_<id>`、响应带匹配信息与
   `build_task_id`、任务行入库（queued）、进度端点可查、未命中不建任务、
   未知 task_id → 404（后台线程被 monkeypatch 掉，测试不烧 token）；
2. 执行层：`asset_build.execute` 真跑一遍（假 LLM + 假数据集），断言任务从
   running 走到 succeeded、content_json/source 落到**上传那条资产**的行上
   （v2 合并语义：重生成直接覆盖，无清理步骤）、上传的 video_uri 不被生成
   结果抹掉、台词进库。

数据集构造与 `tests/test_assets_builder.py` 同构（meta + story + 帧 + 字幕 zip）。
"""

import json
import zipfile
from pathlib import Path

import pytest

from tagplatform import asset_build
from tagplatform.content_io import load_content
from tagplatform.db.models import AssetBuildTask, MediaAsset
from tagplatform.llm import LLMResponse

MOVIE_ID = "tt0000001"
PAYLOAD = b"fake-video-bytes"
LONG_SUMMARY = ("这是整片的中文剧情描述，交代了主要人物、核心冲突与结局走向，"
                "篇幅足够长，可以通过长度闸门；它同时用于验证摘要解析、重试与"
                "兜底逻辑，因此必须超过最小长度阈值，避免被当成残片丢弃。")


def _build_dataset(root: Path) -> None:
    (root / "meta_data" / "meta").mkdir(parents=True, exist_ok=True)
    (root / "annotation_data" / "annotation").mkdir(parents=True, exist_ok=True)
    (root / "meta_data" / "meta" / f"{MOVIE_ID}.json").write_text(json.dumps({
        "title": "Test Movie (1999)", "genres": ["Drama"], "language": "en",
        "synopsis": "An English synopsis.", "imdb_rating": 8.7,
        "cast": [{"name": "Actor A", "character": "Don Vito"}],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "annotation_data" / "annotation" / f"{MOVIE_ID}.json").write_text(
        json.dumps({"story": [
            {"shot": [1, 2], "duration": [0, 10], "description": "Scene one."},
        ]}, ensure_ascii=False), encoding="utf-8")
    frames = root / "frames" / "frames" / MOVIE_ID
    frames.mkdir(parents=True)
    (frames / "shot_0001_img_0.jpg").write_bytes(b"jpg")
    with zipfile.ZipFile(root / "subtitle1K.v1.zip", "w") as z:
        z.writestr(f"subtitle/{MOVIE_ID}.srt",
                   "1\n00:00:01,000 --> 00:00:02,000\nGet in the car.\n")


class FakeClient:
    """假 LLM：场景与摘要各返回合法 JSON。"""

    def complete(self, messages, run_id):
        if run_id.startswith("summary_"):
            return LLMResponse(content=json.dumps({"summary": LONG_SUMMARY},
                                                  ensure_ascii=False),
                               usage={"prompt_tokens": 10, "completion_tokens": 5},
                               raw={}, ok=True)
        return LLMResponse(content=json.dumps({
            "description": "场景描述", "atmosphere": ["紧张"],
                "events": [{"description": "争执", "participants": ["Don Vito"]}],
            "virtual_time": "", "location": "车内",
            "dialogues": [{"line": 1, "speaker": "Don Vito", "emotion": "愤怒"}],
        }, ensure_ascii=False),
            usage={"prompt_tokens": 20, "completion_tokens": 10}, raw={}, ok=True)


@pytest.fixture
def dataset_client(client, tmp_path):
    """client + 临时数据集（dataset_root 已在 conftest 指向 tmp/movienet）。"""
    settings = client.app.state.settings
    _build_dataset(Path(settings.dataset_root))
    return client


def _upload(client, name: str):
    return client.post("/api/v1/media/upload",
                       files={"file": (name, PAYLOAD, "application/octet-stream")})


def test_upload_matches_dataset_and_creates_task(dataset_client, monkeypatch):
    submitted: list[str] = []
    monkeypatch.setattr(asset_build, "submit",
                        lambda task_id, settings=None: submitted.append(task_id))
    resp = _upload(dataset_client, "Test.Movie.1999.1080p.BluRay.x264.mkv")
    assert resp.status_code == 200
    body = resp.json()
    assert body["asset_id"] == f"movie_{MOVIE_ID}"
    assert body["movie_id"] == MOVIE_ID
    assert body["matched_title"] == "Test Movie"
    assert body["match_how"] in ("exact", "contains")
    assert body["build_task_id"] and submitted == [body["build_task_id"]]
    # 资产行用的是数据集 id，不是 asset_xxxxxxxx
    with dataset_client.app.state.session_factory() as s:
        assert s.get(MediaAsset, f"movie_{MOVIE_ID}") is not None
        task = s.get(AssetBuildTask, body["build_task_id"])
        assert task.status == "queued" and task.movie_id == MOVIE_ID


def test_build_task_endpoints(dataset_client, monkeypatch):
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    task_id = _upload(dataset_client,
                      "Test.Movie.1999.mkv").json()["build_task_id"]
    one = dataset_client.get(f"/api/v1/media/build-tasks/{task_id}")
    assert one.status_code == 200
    assert set(one.json()) == {"task_id", "asset_id", "movie_id", "status",
                               "total_scenes", "done_scenes", "error"}
    assert one.json()["status"] == "queued"
    many = dataset_client.get(f"/api/v1/media/build-tasks?asset_id=movie_{MOVIE_ID}")
    assert many.status_code == 200 and len(many.json()) == 1
    assert dataset_client.get("/api/v1/media/build-tasks/nope").status_code == 404


def test_upload_without_match_creates_no_task(dataset_client, monkeypatch):
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    body = _upload(dataset_client, "完全无关.mp4").json()
    assert body["movie_id"] is None and body["build_task_id"] is None
    with dataset_client.app.state.session_factory() as s:
        assert s.query(AssetBuildTask).count() == 0


def test_upload_auto_build_off(dataset_client, monkeypatch):
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    resp = dataset_client.post(
        "/api/v1/media/upload",
        files={"file": ("Test.Movie.1999.mkv", PAYLOAD, "application/octet-stream")},
        data={"auto_build": "false"})
    body = resp.json()
    assert body["movie_id"] == MOVIE_ID      # 仍然报匹配结果
    assert body["build_task_id"] is None     # 但不生成


def test_execute_success_writes_content(dataset_client, monkeypatch):
    """执行层：跑完生成 → 任务 succeeded、内容落到上传的资产行上。"""
    monkeypatch.setattr("tagplatform.llm.make_llm_client",
                        lambda settings=None: FakeClient())
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    settings = dataset_client.app.state.settings.model_copy(update={
        "asset_workers": 1, "asset_frames": 1})

    body = _upload(dataset_client, "Test.Movie.1999.mkv").json()
    task_id, asset_id = body["build_task_id"], body["asset_id"]

    asset_build.execute(task_id, settings)

    with dataset_client.app.state.session_factory() as s:
        task = s.get(AssetBuildTask, task_id)
        assert task.status == "succeeded", task.error
        assert task.done_scenes == task.total_scenes == 1
        asset = s.get(MediaAsset, asset_id)
        assert asset.video_uri and asset.video_uri.endswith(".mkv")
        assert asset.title == "Test Movie"
        assert asset.source.startswith("asset_builder:")
        # 库里存的是交付用的模板原文（description / start_ms），不是内部形态
        raw = json.loads(asset.content_json)
        assert raw["content"]["description"] == LONG_SUMMARY
        assert "summary" not in raw["content"]
        assert "start_ms" in raw["content"]["scenes"][0]
        # 读取时归一化回内部结构
        content = load_content(asset.content_json)
        assert content.content.summary == LONG_SUMMARY
        scene = content.content.scenes[0]
        assert scene.character_ids == ["character_001"]
        assert scene.dialogues[0].speaker_id == "character_001"
        assert scene.dialogues[0].text == "Get in the car."
        # 演员写进 description（模板没有 actor 字段）
        assert content.content.characters[0].description == "由 Actor A 饰演"


def test_progress_page_renders(dataset_client):
    """进度页：不依赖前端构建，浏览器打开自动轮询任务状态。"""
    body = _upload(dataset_client, "Test.Movie.1999.mkv").json()
    task_id = body["build_task_id"]

    resp = dataset_client.get(f"/api/v1/media/build-tasks/{task_id}/view")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert task_id in resp.text
    assert "build-tasks/" in resp.text          # 页面自己在轮询 JSON 端点

    missing = dataset_client.get("/api/v1/media/build-tasks/build_nope/view")
    assert missing.status_code == 404
def test_execute_unknown_task_is_noop(dataset_client):
    asset_build.execute("build_nope", dataset_client.app.state.settings)


def test_execute_overwrites_stale_content(dataset_client, monkeypatch):
    """旧口径（source 不符）的识别结果会被重生成直接覆盖（v2：无清理步骤）。"""
    monkeypatch.setattr("tagplatform.llm.make_llm_client",
                        lambda settings=None: FakeClient())
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    settings = dataset_client.app.state.settings.model_copy(update={
        "asset_workers": 1, "asset_frames": 1})
    body = _upload(dataset_client, "Test.Movie.1999.mkv").json()
    asset_id = body["asset_id"]

    with dataset_client.app.state.session_factory() as s:
        asset = s.get(MediaAsset, asset_id)   # 上传时已登记
        asset.content_json = "{}"
        asset.source = "video_understanding"  # 旧口径
        s.commit()

    asset_build.execute(body["build_task_id"], settings)

    with dataset_client.app.state.session_factory() as s:
        asset = s.get(MediaAsset, asset_id)
        # 覆盖为新口径，不堆历史（资产行只有一份 content_json）
        assert asset.source.startswith("asset_builder:")
        assert json.loads(asset.content_json)["content"]["description"] == LONG_SUMMARY


def test_execute_reuses_same_version(dataset_client, monkeypatch):
    """同口径第二次上传：直接 succeeded，不再调模型。"""
    calls: list[str] = []

    class Counting(FakeClient):
        def complete(self, messages, run_id):
            calls.append(run_id)
            return super().complete(messages, run_id)

    monkeypatch.setattr("tagplatform.llm.make_llm_client",
                        lambda settings=None: Counting())
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    settings = dataset_client.app.state.settings.model_copy(update={
        "asset_workers": 1, "asset_frames": 1})
    first = _upload(dataset_client, "Test.Movie.1999.mkv").json()
    asset_build.execute(first["build_task_id"], settings)
    used = len(calls)
    assert used > 0

    second = _upload(dataset_client, "Test.Movie.1999.mkv").json()
    asset_build.execute(second["build_task_id"], settings)
    assert len(calls) == used
    with dataset_client.app.state.session_factory() as s:
        assert s.get(AssetBuildTask, second["build_task_id"]).status == "succeeded"
