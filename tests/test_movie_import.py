"""影片资产目录导入（内容已备好，直接注册资产；与上传视频并行）。

覆盖点：
1. 扫描：目录名→imdb、缺 content.json 记错误、`_` 开头与非目录跳过；
2. 单部导入：建影片行 + 场景行、content_json 落库、asset_id 沿用 content.json；
3. 幂等：内容一致 → skipped；overwrite=True 才重写；已绑视频 → 默认跳过；
4. 批量：未知名过滤、坏目录跳过并如实报告；
5. 帧：`plan_scene_frames` 优先用资产目录里的帧（scene_XXX_frame_N 排序）；
6. HTTP：movie-scan / movie-bulk 两个端点（临时根目录，不碰真实路径）。
"""

import json
from pathlib import Path

import pytest

from tagplatform import movie_import
from tagplatform.adapters.vision import to_content_json
from tagplatform.db.models import MediaAsset
from tagplatform.generation.mtg import keyframes
from tagplatform.schemas.upstream import parse_upstream

PAYLOAD = {
    "asset": {
        "asset_id": "movie_tt1234567",
        "asset_type": "movie",
        "title": "Rear Window",
        "duration_ms": 6_000_000,
        "video_uri": None,
    },
    "content": {
        "description": "一名摄影记者因腿伤被困家中，靠观察邻居打发时间。",
        "characters": [
            {"character_id": "character_001", "name": "Jeff", "description": "记者"}
        ],
        "scenes": [
            {"scene_id": "scene_001", "start_ms": 0, "end_ms": 120_000,
             "description": "开场，窗帘拉开，院子全景。",
             "character_ids": ["character_001"], "frames": ["scene_001_frame_1.jpg"]},
            {"scene_id": "scene_002", "start_ms": 120_000, "end_ms": 240_000,
             "description": "邻居浇花，主角在窗后观察。",
             "character_ids": ["character_001"], "frames": []},
        ],
    },
}


@pytest.fixture
def movies(tmp_path):
    """影片目录根单独一层：tmp_path 里还有 conftest 写的标签库夹具。"""
    root = tmp_path / "movies"
    root.mkdir()
    return root


def _write_movie(root: Path, dir_name: str, payload: dict | None = None,
                 frames: int = 3) -> Path:
    movie = root / dir_name
    movie.mkdir(parents=True)
    if payload is not None:
        (movie / "content.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if frames:
        frame_dir = movie / "frames"
        frame_dir.mkdir()
        for scene in ("scene_001", "scene_002"):
            for index in range(1, frames + 1):
                (frame_dir / f"{scene}_frame_{index}.jpg").write_bytes(b"jpeg")
    return movie


def test_imdb_of_dir():
    assert movie_import.imdb_of_dir("Rear Window--tt0047396") == "tt0047396"
    assert movie_import.imdb_of_dir("12 Years a Slave--tt2024544") == "tt2024544"
    assert movie_import.imdb_of_dir("没有 imdb 的目录") is None


def test_scene_frames_sorted_and_scoped(movies):
    movie = _write_movie(movies, "Rear Window--tt0047396", PAYLOAD, frames=3)
    # 乱序文件名也要按序号返回
    (movie / "frames" / "scene_001_frame_9.jpg").write_bytes(b"jpeg")
    frames = movie_import.scene_frames(movies, "tt0047396", "scene_001")
    assert [f.name for f in frames] == [
        "scene_001_frame_1.jpg", "scene_001_frame_2.jpg",
        "scene_001_frame_3.jpg", "scene_001_frame_9.jpg"]
    assert movie_import.scene_frames(movies, "tt0000000", "scene_001") == ()


def test_scan_movie_dirs(movies):
    _write_movie(movies, "Rear Window--tt0047396", PAYLOAD)
    _write_movie(movies, "Broken--tt0000002", {"asset": {}}, frames=0)
    _write_movie(movies, "NoJson--tt0000003", None, frames=1)
    (movies / "_raw").mkdir()
    (movies / "not_a_dir.txt").write_text("x", encoding="utf-8")

    items = {item.dir_name: item for item in movie_import.scan_movie_dirs(movies)}
    assert set(items) == {"Rear Window--tt0047396", "Broken--tt0000002",
                          "NoJson--tt0000003"}
    good = items["Rear Window--tt0047396"]
    assert good.title == "Rear Window"
    assert good.asset_id == "movie_tt1234567"
    assert good.scenes == 2 and good.frames == 6
    assert good.imdb_id == "tt0047396" and good.error is None
    assert items["NoJson--tt0000003"].error == "缺少 content.json"
    assert items["Broken--tt0000002"].error is not None


def test_import_creates_asset_and_is_idempotent(session, movies):
    _write_movie(movies, "Rear Window--tt0047396", PAYLOAD)
    first = movie_import.import_movie_dir(session, movies, "Rear Window--tt0047396")
    assert first.status == "imported"
    assert first.asset_id == "movie_tt1234567"
    assert first.scenes == 2

    movie = session.get(MediaAsset, "movie_tt1234567")
    assert movie is not None and movie.content_json
    stored = json.loads(movie.content_json)
    assert stored["asset"]["title"] == "Rear Window"
    assert len(stored["content"]["scenes"]) == 2
    scenes = session.query(MediaAsset).filter_by(asset_type="scene").all()
    assert {row.asset_id for row in scenes} == {
        "movie_tt1234567::scene_001", "movie_tt1234567::scene_002"}

    again = movie_import.import_movie_dir(session, movies, "Rear Window--tt0047396")
    assert again.status == "skipped"
    assert "一致" in (again.reason or "")

    forced = movie_import.import_movie_dir(
        session, movies, "Rear Window--tt0047396", overwrite=True)
    assert forced.status == "imported"


def test_import_skips_asset_with_uploaded_video(session, movies):
    _write_movie(movies, "Rear Window--tt0047396", PAYLOAD)
    session.add(MediaAsset(asset_id="movie_tt1234567", asset_type="movie",
                           title="Rear Window", video_uri="/tmp/x.mp4"))
    session.commit()

    skipped = movie_import.import_movie_dir(session, movies, "Rear Window--tt0047396")
    assert skipped.status == "skipped"
    assert "视频" in (skipped.reason or "")

    forced = movie_import.import_movie_dir(
        session, movies, "Rear Window--tt0047396", overwrite=True)
    assert forced.status == "imported"
    # 覆盖内容但不动视频地址（上传那条路的产物不能被目录导入抹掉）
    assert session.get(MediaAsset, "movie_tt1234567").video_uri == "/tmp/x.mp4"


def test_import_many_filters_unknown_and_reports_failures(session, movies):
    _write_movie(movies, "Rear Window--tt0047396", PAYLOAD)
    _write_movie(movies, "Broken--tt0000002", {"asset": {}}, frames=0)

    outcome = movie_import.import_many(
        session, movies, ["Rear Window--tt0047396", "不存在的目录"])
    # 存在的照常导入，对不上的如实报失败（早前这种情形被静默丢掉）
    assert outcome.counts == {"imported": 1, "skipped": 0, "failed": 1}
    assert "没有名为" in (outcome.failed[0].reason or "")

    # 全量再跑：好目录内容一致被跳过；坏目录在扫描阶段就没有 asset_id，
    # 批量入口不选它（前端也拿不到它的 asset_id）
    all_outcome = movie_import.import_many(session, movies, None)
    assert all_outcome.counts == {"imported": 0, "skipped": 1, "failed": 0}

    bad = movie_import.import_movie_dir(session, movies, "Broken--tt0000002")
    assert bad.status == "failed"


def test_plan_scene_frames_uses_local_frames(movies):
    content = to_content_json(parse_upstream(PAYLOAD))
    _write_movie(movies, "Rear Window--tt1234567", PAYLOAD, frames=2)
    frames = keyframes.plan_scene_frames(
        content, str(movies / "no_dataset"), per_scene=2,
        movie_asset_root=str(movies))
    assert set(frames) == {"scene_001", "scene_002"}
    assert [p.name for p in frames["scene_001"]] == [
        "scene_001_frame_1.jpg", "scene_001_frame_2.jpg"]


def test_plan_scene_frames_without_local_root_falls_back(movies):
    content = to_content_json(parse_upstream(PAYLOAD))
    # 既无本地帧、也没有数据集 → 空字典（退化为纯文本 prompt，不报错）
    assert keyframes.plan_scene_frames(
        content, str(movies / "no_dataset"), movie_asset_root=None) == {}


def test_api_scan_and_bulk(client, movies):
    _write_movie(movies, "Rear Window--tt0047396", PAYLOAD)

    scan = client.get("/api/v1/content/import/movie-scan",
                      params={"root": str(movies)})
    assert scan.status_code == 200, scan.text
    body = scan.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["title"] == "Rear Window" and row["exists"] is False
    assert row["scenes"] == 2 and row["frames"] == 6

    imported = client.post("/api/v1/content/import/movie-bulk",
                           json={"root": str(movies), "dir_names": None})
    assert imported.status_code == 200, imported.text
    summary = imported.json()
    assert summary["imported"] == 1 and summary["failed"] == 0

    again = client.get("/api/v1/content/import/movie-scan",
                       params={"root": str(movies)})
    assert again.json()["items"][0]["exists"] is True


def test_api_scan_missing_root(client):
    response = client.get("/api/v1/content/import/movie-scan",
                          params={"root": "/nonexistent/movie_asset"})
    assert response.status_code == 422


def test_browse_lists_folders_and_movie_counts(tmp_path):
    """浏览父目录：普通文件夹标出"里面藏着几部片"，不是影片目录。"""
    outer = tmp_path / "movie_asset"
    outer.mkdir()
    _write_movie(outer, "Rear Window--tt0047396", PAYLOAD)
    _write_movie(outer, "Taxi Driver--tt0075314", PAYLOAD)
    (tmp_path / "_raw").mkdir()

    current, parent, entries = movie_import.browse(str(tmp_path))
    assert current == tmp_path
    assert parent == tmp_path.parent
    by_name = {entry.name: entry for entry in entries}
    assert by_name["movie_asset"].is_movie is False
    assert by_name["movie_asset"].movies == 2
    assert by_name["_raw"].is_movie is False and by_name["_raw"].movies == 0
    # `_` 开头的运行期目录排在后面
    assert entries[-1].name == "_raw"


def test_browse_movie_dir_lists_movies(tmp_path):
    _write_movie(tmp_path, "Rear Window--tt0047396", PAYLOAD)
    _write_movie(tmp_path, "Broken--tt0000002", {"asset": {}}, frames=0)

    current, parent, entries = movie_import.browse(str(tmp_path))
    by_name = {entry.name: entry for entry in entries}
    good = by_name["Rear Window--tt0047396"]
    assert good.is_movie is True and good.scenes == 2 and good.frames == 6
    assert good.error is None
    assert by_name["Broken--tt0000002"].error is not None
    assert parent == tmp_path.parent


def test_browse_missing_path_raises(tmp_path):
    with pytest.raises(movie_import.MovieAssetError):
        movie_import.browse(str(tmp_path / "nope"))


def test_api_browse_and_import_with_root(client, movies):
    """HTTP 层：先 browse 进目录，再带 root 导入；已导入的 exists=True。"""
    nested = movies / "movie_asset"
    nested.mkdir()
    _write_movie(nested, "Rear Window--tt0047396", PAYLOAD)

    outer = client.get("/api/v1/content/import/browse",
                       params={"path": str(movies)})
    assert outer.status_code == 200, outer.text
    body = outer.json()
    assert body["path"] == str(movies)
    folder = next(e for e in body["entries"] if e["name"] == "movie_asset")
    assert folder["is_movie"] is False and folder["movies"] == 1

    inner = client.get("/api/v1/content/import/browse",
                       params={"path": str(nested)})
    assert inner.status_code == 200, inner.text
    rows = inner.json()["entries"]
    assert rows[0]["is_movie"] is True and rows[0]["exists"] is False
    assert inner.json()["parent"] == str(movies)

    imported = client.post("/api/v1/content/import/movie-bulk",
                           json={"root": str(nested), "dir_names": None})
    assert imported.status_code == 200 and imported.json()["imported"] == 1

    again = client.get("/api/v1/content/import/browse",
                       params={"path": str(nested)})
    assert again.json()["entries"][0]["exists"] is True


def test_api_browse_missing_path(client):
    response = client.get("/api/v1/content/import/browse",
                          params={"path": "/nonexistent/dir"})
    assert response.status_code == 422


def test_asset_index_matches_upload_by_title(session, movies):
    """上传建的 asset_xxx 与目录里的 movie_ttXXXX 是同一部片：按 imdb / 片名认出来。"""
    # 有 imdb 线索的上传资产（内容 JSON 里记着 movie_tt0047396）
    session.add(MediaAsset(asset_id="asset_deadbeef", asset_type="movie",
                           title="Rear Window (1954)", video_uri="/tmp/rw.mp4",
                           content_json=json.dumps({"asset": {"asset_id": "movie_tt0047396"}})))
    # 完全没有 imdb 线索的上传资产（内容 JSON 里只有自己的 asset_id）
    session.add(MediaAsset(asset_id="asset_cafe0000", asset_type="movie",
                           title="Memento", video_uri="/tmp/mm.mp4",
                           content_json=json.dumps({"asset": {"asset_id": "asset_cafe0000"}})))
    session.commit()

    index = movie_import.AssetIndex(session)
    by_imdb = index.match("movie_tt0047396", "tt0047396", "Rear Window")
    assert by_imdb is not None and by_imdb.asset_id == "asset_deadbeef"
    assert by_imdb.matched_by == "imdb"
    assert by_imdb.has_video is True

    # 没有 imdb 线索时按片名认（年份括号不影响）
    by_title = index.match("movie_tt0209144", "tt0209144", "Memento")
    assert by_title is not None and by_title.asset_id == "asset_cafe0000"
    assert by_title.matched_by == "title"

    # imdb 对不上（重拍片）时不能按片名硬认
    assert index.match("movie_tt9999999", "tt9999999", "Rear Window") is None


def test_import_writes_into_matched_upload_asset(session, movies):
    """目录导入认到上传资产时写进那一行：不新增影片行、视频地址保留。"""
    _write_movie(movies, "Rear Window--tt1234567", PAYLOAD)
    session.add(MediaAsset(asset_id="asset_deadbeef", asset_type="movie",
                           title="Rear Window", video_uri="/tmp/rw.mp4"))
    session.commit()

    skipped = movie_import.import_movie_dir(session, movies, "Rear Window--tt1234567")
    assert skipped.status == "skipped"
    assert skipped.asset_id == "asset_deadbeef"
    assert "已有内容" not in (skipped.reason or "")  # 没有内容 → 走视频保护分支

    forced = movie_import.import_movie_dir(
        session, movies, "Rear Window--tt1234567", overwrite=True)
    assert forced.status == "imported"
    assert forced.asset_id == "asset_deadbeef"

    rows = session.query(MediaAsset).filter_by(asset_type="movie").all()
    assert {row.asset_id for row in rows} == {"asset_deadbeef"}  # 没有多出一行
    stored = session.get(MediaAsset, "asset_deadbeef")
    assert stored.video_uri == "/tmp/rw.mp4"          # 视频地址没被抹掉
    assert stored.content_json and json.loads(stored.content_json)["asset"]["asset_id"] == "asset_deadbeef"
    scenes = session.query(MediaAsset).filter_by(asset_type="scene").all()
    assert {row.asset_id for row in scenes} == {
        "asset_deadbeef::scene_001", "asset_deadbeef::scene_002"}


def test_api_browse_reports_matched_by_asset_id(client, movies):
    """导入后回看：exists=True 且 matched_by 说明是靠 asset_id 认出来的。"""
    _write_movie(movies, "Rear Window--tt0047396", PAYLOAD)
    client.post("/api/v1/content/import/movie-bulk",
                json={"root": str(movies), "dir_names": ["Rear Window--tt0047396"]})

    body = client.get("/api/v1/content/import/browse",
                      params={"path": str(movies)}).json()
    row = body["entries"][0]
    assert row["exists"] is True
    assert row["matched_by"] == "asset_id"
    assert row["existing_asset_id"] == row["asset_id"] == "movie_tt1234567"


def test_import_many_reports_unknown_names(session, movies):
    """名字对不上（例如前端误传路径）必须报失败，不能静默 0 成功。"""
    _write_movie(movies, "Rear Window--tt1234567", PAYLOAD)
    outcome = movie_import.import_many(
        session, movies, [str(movies / "Rear Window--tt1234567")])
    assert outcome.imported == [] and outcome.skipped == []
    assert len(outcome.failed) == 1
    assert "没有名为" in (outcome.failed[0].reason or "")


def test_import_many_unknown_root_level_name(session, tmp_path, movies):
    """在上一层选片（名字不在这一层）同样要报失败而不是 0 条。"""
    _write_movie(movies, "Rear Window--tt1234567", PAYLOAD)
    outcome = movie_import.import_many(
        session, tmp_path, ["Rear Window--tt1234567"])
    assert len(outcome.failed) == 1 and not outcome.imported
