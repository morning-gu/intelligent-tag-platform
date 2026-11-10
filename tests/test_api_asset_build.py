"""「上传即生成资产」链路测试（2026-11）。

两条链路各自验证：
1. HTTP 层：文件名命中数据集 → 建构建任务、响应带匹配信息与
   `build_task_id`、任务行入库（queued）、进度端点可查、未命中不建任务、
   未知 task_id → 404（后台线程被 monkeypatch 掉，测试不烧 token）；
2. 执行层：`asset_build.execute` 真跑一遍（假 LLM + 假数据集），断言任务从
   running 走到 succeeded、内容写进**上传那条资产**的 `content_json`、
   场景行标题是 `{片名}_{scene_id}`、上传的 video_uri 不被生成结果抹掉。

同一部片重复上传复用同一个 asset_id（资产不重复、内容覆盖），任务行可以
多条——这是"任务重复、资产不重复"的落点。

数据集构造与 `tests/test_assets_builder.py` 同构（meta + story + 帧 + 字幕 zip）。
"""

import json
import zipfile
from pathlib import Path

import pytest

from tagplatform import asset_build
from tagplatform.assets import source_tag
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
                        lambda task_id, settings=None, **kw: submitted.append(task_id))
    resp = _upload(dataset_client, "Test.Movie.1999.1080p.BluRay.x264.mkv")
    assert resp.status_code == 200
    body = resp.json()
    # 资产 id 与影片 id 无字面关系：统一是 asset_ + 8 位 hex
    assert body["asset_id"].startswith("asset_") and len(body["asset_id"]) == 14
    assert body["movie_id"] == MOVIE_ID
    assert body["matched_title"] == "Test Movie"
    assert body["match_how"] in ("exact", "contains")
    assert body["build_task_id"] and submitted == [body["build_task_id"]]
    with dataset_client.app.state.session_factory() as s:
        asset = s.get(MediaAsset, body["asset_id"])
        assert asset is not None and asset.asset_type == "movie"
        assert asset.title == "Test Movie"      # 命中数据集时用片名，不用文件名
        assert asset.content_json is None       # 生成前只有占位行
        task = s.get(AssetBuildTask, body["build_task_id"])
        assert task.status == "queued" and task.movie_id == MOVIE_ID


def test_reupload_reuses_asset_row(dataset_client, monkeypatch):
    """同一部片再传一次：资产不重复（同一 asset_id），任务多一条。"""
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    first = _upload(dataset_client, "Test.Movie.1999.mkv").json()
    second = _upload(dataset_client, "Test.Movie.1999.1080p.mkv").json()
    assert first["asset_id"] == second["asset_id"]
    assert first["build_task_id"] != second["build_task_id"]
    with dataset_client.app.state.session_factory() as s:
        assert s.query(MediaAsset).filter_by(asset_type="movie").count() == 1
        assert s.query(AssetBuildTask).count() == 2


def test_build_task_endpoints(dataset_client, monkeypatch):
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    upload = _upload(dataset_client, "Test.Movie.1999.mkv").json()
    task_id = upload["build_task_id"]
    one = dataset_client.get(f"/api/v1/media/build-tasks/{task_id}")
    assert one.status_code == 200
    assert set(one.json()) == {"task_id", "asset_id", "movie_id", "status",
                               "total_scenes", "done_scenes", "error"}
    assert one.json()["status"] == "queued"
    many = dataset_client.get(
        f"/api/v1/media/build-tasks?asset_id={upload['asset_id']}")
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


def test_execute_success_writes_context(dataset_client, monkeypatch):
    """执行层：跑完生成 → 任务 succeeded、内容落到上传的资产上。"""
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
        # 内容落在资产行上（v1 的 media_context 已并入），口径与更新时间同时写
        assert asset.content_json is not None and asset.source == source_tag(True)
        assert asset.updated_at is not None
        stored = json.loads(asset.content_json)
        first_scene = stored["content"]["scenes"][0]
        # 场景行是物理切片：标题 {片名}_{scene_id}，不存内容
        scene_row = s.get(MediaAsset, f"{asset_id}::{first_scene['scene_id']}")
        assert scene_row is not None and scene_row.asset_type == "scene"
        assert scene_row.title == f"Test Movie_{first_scene['scene_id']}"
        assert scene_row.content_json is None and scene_row.source is None
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
def test_upload_infer_passes_to_submit(dataset_client, monkeypatch):
    """上传时的 infer 只是执行参数：透传给后台任务，不落任务行。

    表结构与 main 完全一致（没有 infer 列），前端勾选框照旧可用。
    """
    seen: dict = {}
    monkeypatch.setattr(asset_build, "submit",
                        lambda task_id, settings=None, **kw: seen.update(kw))
    body = dataset_client.post(
        "/api/v1/media/upload",
        files={"file": ("Test.Movie.1999.mkv", PAYLOAD,
                        "application/octet-stream")},
        data={"infer": "false"},
    ).json()
    assert seen == {"infer": False}
    with dataset_client.app.state.session_factory() as s:
        cols = {
            row[1]
            for row in s.connection().exec_driver_sql(
                "PRAGMA table_info(asset_build_task)")
        }
        task = s.get(AssetBuildTask, body["build_task_id"])
        assert task is not None and task.status == "queued"
    assert "infer" not in cols


def test_execute_facts_mode_skips_llm(dataset_client, monkeypatch):
    """infer=False 的一次执行：一次模型都不调，内容口径落成 v5-facts。"""
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

    body = dataset_client.post(
        "/api/v1/media/upload",
        files={"file": ("Test.Movie.1999.mkv", PAYLOAD,
                        "application/octet-stream")},
        data={"infer": "false"},
    ).json()

    asset_build.execute(body["build_task_id"], settings, infer=False)

    with dataset_client.app.state.session_factory() as s:
        task = s.get(AssetBuildTask, body["build_task_id"])
        assert task.status == "succeeded", task.error
        asset = s.get(MediaAsset, body["asset_id"])
        assert asset.source == source_tag(False)
    assert calls == []


def test_execute_unknown_task_is_noop(dataset_client):
    asset_build.execute("build_nope", dataset_client.app.state.settings)


def test_execute_overwrites_stale_content(dataset_client, monkeypatch):
    """旧口径（source 不是当前编码）的内容被整块覆盖，旧场景行一并清掉。"""
    monkeypatch.setattr("tagplatform.llm.make_llm_client",
                        lambda settings=None: FakeClient())
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    settings = dataset_client.app.state.settings.model_copy(update={
        "asset_workers": 1, "asset_frames": 1})
    body = _upload(dataset_client, "Test.Movie.1999.mkv").json()
    asset_id = body["asset_id"]

    with dataset_client.app.state.session_factory() as s:
        s.add(MediaAsset(asset_id=f"{asset_id}::s0", asset_type="scene",
                         parent_id=asset_id, title="s0"))
        stale = s.get(MediaAsset, asset_id)
        stale.content_json = "{}"
        stale.source = "video_understanding"
        s.commit()

    asset_build.execute(body["build_task_id"], settings)

    with dataset_client.app.state.session_factory() as s:
        asset = s.get(MediaAsset, asset_id)
        assert asset.source == source_tag(True)
        stored = json.loads(asset.content_json)
        assert stored["content"]["description"] == LONG_SUMMARY
        # 父资产只有一份内容（就是这一行），没有历史堆积
        assert s.query(MediaAsset).filter(
            MediaAsset.asset_type == "movie",
            MediaAsset.content_json.isnot(None)).count() == 1
        # 新内容里没有 s0：旧场景行被一并清掉，不留孤儿
        assert s.get(MediaAsset, f"{asset_id}::s0") is None


def test_execute_overwrites_instead_of_reusing(dataset_client, monkeypatch):
    """第二次执行不再"秒回"：真跑一遍、内容覆盖，库里仍只有一份。"""
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
    assert second["asset_id"] == first["asset_id"]     # 资产不重复
    asset_build.execute(second["build_task_id"], settings)

    assert len(calls) > used                            # 没有秒回，模型真调了
    with dataset_client.app.state.session_factory() as s:
        assert s.get(AssetBuildTask, second["build_task_id"]).status == "succeeded"
        asset = s.get(MediaAsset, first["asset_id"])
        assert asset.content_json is not None
        # 一条资产只有一份内容（就在这一行上，没有 context 表可堆）
        assert asset.source == source_tag(True) and asset.updated_at is not None


# ---------- 选文件预检：/media/match ----------


def test_match_endpoint_unmatched(dataset_client):
    """文件名匹配不到数据集：matched=false，不影响任何东西。"""
    resp = dataset_client.get("/api/v1/media/match",
                              params={"filename": "完全无关.mp4"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched"] is False
    assert body["has_content"] is False and body["asset_id"] is None


def test_match_endpoint_matched_without_content(dataset_client):
    """命中影片但还没生成过资产：matched=true / has_content=false / 无 asset_id。"""
    body = dataset_client.get(
        "/api/v1/media/match",
        params={"filename": "Test.Movie.1999.1080p.mkv"}).json()
    assert body["matched"] is True
    assert body["movie_id"] == MOVIE_ID
    assert body["matched_title"] == "Test Movie"
    assert body["asset_id"] is None       # 还没有资产行
    assert body["has_content"] is False
    assert body["scene_count"] == 0


def test_match_endpoint_reports_built_content(dataset_client, monkeypatch):
    """已生成过：has_content=true + 场景数 + 口径与更新时间（前端据此问重新生成）。"""
    monkeypatch.setattr("tagplatform.llm.make_llm_client",
                        lambda settings=None: FakeClient())
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    settings = dataset_client.app.state.settings.model_copy(update={
        "asset_workers": 1, "asset_frames": 1})
    upload = _upload(dataset_client, "Test.Movie.1999.mkv").json()
    asset_build.execute(upload["build_task_id"], settings)

    body = dataset_client.get(
        "/api/v1/media/match",
        params={"filename": "Test.Movie.1999.mkv"}).json()
    assert body["matched"] is True
    assert body["asset_id"] == upload["asset_id"]
    assert body["has_content"] is True
    assert body["content_source"] == source_tag(True)
    assert body["scene_count"] == 1
    assert body["built_at"] is not None


def test_match_endpoint_contentless_asset(dataset_client, monkeypatch):
    """资产行在、内容为空（上传后未生成/生成失败）：按"没有内容"处理。"""
    monkeypatch.setattr(asset_build, "submit", lambda *a, **k: None)
    upload = _upload(dataset_client, "Test.Movie.1999.mkv").json()

    body = dataset_client.get(
        "/api/v1/media/match",
        params={"filename": "Test.Movie.1999.mkv"}).json()
    assert body["asset_id"] == upload["asset_id"]
    assert body["has_content"] is False
    assert body["scene_count"] == 0
    assert body["content_source"] is None


# ---------- 数据集认不出时的标题兜底（中文名/改名不该静默重复） ----------
#
# 事故背景：预检只查数据集索引，文件名一旦认不出（中文名、改名、数据集目录
# 不可读）就等于"没这部片"，重复上传静默通过、还多出一条占位资产。


def _seed_titled_asset(client, *, title: str, asset_id: str = "asset_dupe01",
                       content: bool = True) -> str:
    with client.app.state.session_factory() as s:
        s.add(MediaAsset(
            asset_id=asset_id, asset_type="movie", title=title,
            content_json=json.dumps(
                {"schema_version": 1,
                 "content": {"scenes": [{"scene_id": "s1"}]}},
                ensure_ascii=False) if content else None,
            source="v5-infer" if content else None))
        s.commit()
    return asset_id


def test_match_endpoint_falls_back_to_asset_title(dataset_client):
    """数据集认不出，但库里已有同名资产 → 仍然提示"已有资产"（不可重建）。"""
    asset_id = _seed_titled_asset(dataset_client, title="沉默的羔羊")

    body = dataset_client.get(
        "/api/v1/media/match",
        params={"filename": "沉默的羔羊.1080p.mp4"}).json()

    assert body["matched"] is True
    assert body["match_how"] == "asset_title"
    assert body["asset_id"] == asset_id
    assert body["has_content"] is True and body["scene_count"] == 1
    assert body["can_rebuild"] is False      # 没 movie_id，重建要改文件名
    assert body["movie_id"] is None


def test_match_endpoint_dataset_hit_can_rebuild(dataset_client):
    """数据集命中时 can_rebuild=true（前端才允许"重新生成"）。"""
    body = dataset_client.get(
        "/api/v1/media/match",
        params={"filename": "Test.Movie.1999.mkv"}).json()

    assert body["matched"] is True and body["can_rebuild"] is True


def test_upload_duplicate_title_without_dataset_match_conflicts(dataset_client):
    """认不出片名又已有同名资产 → 409，不再静默多建一条占位资产。"""
    _seed_titled_asset(dataset_client, title="沉默的羔羊")

    resp = _upload(dataset_client, "沉默的羔羊.1080p.mp4")

    assert resp.status_code == 409
    assert "已有同名资产" in resp.json()["detail"]
    with dataset_client.app.state.session_factory() as s:
        assert s.query(MediaAsset).filter(
            MediaAsset.asset_type == "movie").count() == 1


def test_name_tokens_strips_release_noise():
    """归一化：年份/分辨率/编码/`&`→and 都不参与片名比对。"""
    cases = {
        "Pride.and.Prejudice.2005.1080p.mkv": "pride and prejudice",
        "Pride & Prejudice (2005).mp4": "pride and prejudice",
        "The.Godfather.1972.BluRay.x264.mp4": "the godfather",
        "Memento [2160p] WEB-DL DDP5.1.mkv": "memento",
    }
    for name, expected in cases.items():
        assert asset_build.name_tokens(name) == expected
