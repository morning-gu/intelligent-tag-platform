"""上传占位端点 API 测试（阶段 4 任务 4）。

覆盖契约（POST /api/v1/media/upload，multipart `file`，登录即可上传）：
- 成功上传三断言组：响应形状 {asset_id, title, video_uri} / 文件真实落盘
  （uuid 存盘名 + 字节一致）/ media_asset 行入库（asset_type=movie、
  duration_ms 为空、created_at 显式）+ 占位语义（无 media_context，资产
  列表可见、详情 contexts 为空）；
- 后缀白名单 {".mp4", ".webm", ".mkv"} 大小写不敏感；非白名单 → 422
  {"detail": "不支持的视频格式"}，零副作用（目录不建、行不入）；
- 大小 >2GB → 413。两条拦截路径各自独立测试：
  - 早拒路径：伪造超大 Content-Length 头（TestClient 的 httpx
    ASGITransport 允许头体不一致，服务端按声明值先于 multipart 解析拒绝）；
  - 流式兜底路径：monkeypatch 收紧 media.MAX_UPLOAD_BYTES，伪造小于上限的
    Content-Length 骗过早拒，实际体超限 → 落盘中止、半成品清理、零入库；
  - 边界：content-length 恰等于 2GB 不拒（仅严格大于才 413）；
- 穿越与中文名免疫：uuid 重命名与原名零关联，存盘名不含 `..`/`evil`/
  中文等任何原名成分；
- 401 未登录（protected_deps 统一挂载覆盖 media router）。

播种模式：无——本文件全部经端点驱动（上传即播种），库断言经
client.app.state.session_factory 直查临时文件库。
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tagplatform.db.models import MediaAsset, MediaContext

# 上传内容体（任意字节即可，测试零真实视频依赖）
PAYLOAD = b"fake-video-bytes"


@pytest.fixture
def media_client(client, tmp_path):
    """media_root 指向 tmp_path 的 client（其余配置同 conftest client）。

    经 model_copy 替换 app.state.settings 的 media_root，上传落盘进测试
    临时目录，仓库工作区零污染。
    """
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"media_root": str(tmp_path / "media")})
    return client


def _upload(media_client, name="clip.mp4", content=PAYLOAD, **kwargs):
    """POST /api/v1/media/upload 快捷方式（multipart 字段名固定 file）。"""
    return media_client.post(
        "/api/v1/media/upload",
        files={"file": (name, content, "application/octet-stream")},
        **kwargs,
    )


def _count_rows(media_client) -> int:
    """直查临时文件库的 media_asset 行数（副作用断言用）。"""
    with media_client.app.state.session_factory() as s:
        return s.query(MediaAsset).count()


# ---------- 成功上传（三断言组） ----------


def test_upload_success(media_client, tmp_path):
    """成功上传：响应形状 / 文件真实落盘 / 行入库 + 占位语义（无 context）。"""
    resp = _upload(media_client, name="clip.mp4", content=PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    # 断言组一：响应形状（恰三键；asset_id 形如 asset_+8位hex；title 去后缀）
    # 2026-11 起响应多带数据集匹配与生成任务字段（未命中时全为 None）
    assert set(body) == {"asset_id", "title", "video_uri", "movie_id",
                         "matched_title", "match_how", "build_task_id"}
    assert body["movie_id"] is None and body["build_task_id"] is None
    assert re.fullmatch(r"asset_[0-9a-f]{8}", body["asset_id"])
    assert body["title"] == "clip"
    # 断言组二：文件真实落盘（绝对路径、按 asset_id 命名、字节一致）
    stored = Path(body["video_uri"])
    assert stored.is_absolute()
    assert stored.exists()
    assert stored.read_bytes() == PAYLOAD
    assert stored.parent == (tmp_path / "media").resolve()
    assert stored.name == f"{body['asset_id']}.mp4"
    # 断言组三：media_asset 行入库（占位字段 + 占位语义无 context）
    with media_client.app.state.session_factory() as s:
        row = s.get(MediaAsset, body["asset_id"])
        assert row is not None
        assert row.asset_type == "movie"
        assert row.title == "clip"
        assert row.duration_ms is None
        assert row.video_uri == body["video_uri"]
        assert row.created_at is not None
        assert s.query(MediaContext).filter(
            MediaContext.asset_id == body["asset_id"]).count() == 0
    # 占位语义的前端证据：资产列表可见、详情 contexts 为空（未导入识别内容）
    listing = media_client.get("/api/v1/assets").json()
    assert body["asset_id"] in {a["asset_id"] for a in listing}
    detail = media_client.get(f"/api/v1/assets/{body['asset_id']}").json()
    assert detail["contexts"] == []


def test_upload_suffix_case_insensitive(media_client):
    """白名单大小写不敏感：.MP4 放行，存盘名后缀归一小写。"""
    resp = _upload(media_client, name="movie.MP4")
    assert resp.status_code == 200
    body = resp.json()
    assert Path(body["video_uri"]).name == f"{body['asset_id']}.mp4"


# ---------- 422 后缀白名单 ----------


def test_upload_422_unsupported_suffix(media_client, tmp_path):
    """非白名单后缀 → 422 {"detail": "不支持的视频格式"}，零副作用。"""
    resp = _upload(media_client, name="movie.avi")
    assert resp.status_code == 422
    assert resp.json() == {"detail": "不支持的视频格式"}
    # 零副作用：目录未建（后缀校验先于任何写盘）、行未入库
    assert not (tmp_path / "media").exists()
    assert _count_rows(media_client) == 0


# ---------- 413 大小上限（早拒路径 + 流式兜底路径 + 边界） ----------


def test_upload_413_content_length_header(media_client, tmp_path):
    """伪造超大 Content-Length 头（2GB+1）→ 413 早拒，零副作用。

    构造方式（报告注明）：超大 Content-Length 头——TestClient 的 httpx
    ASGITransport 允许头体不一致（声明 2GB+1，实际 multipart 体仅数十
    字节），服务端按声明值在落盘 media_root / 入库之前拒绝（multipart
    本身已被 FastAPI 收完解析进磁盘 spool，此路径不验收收包时机）。
    """
    too_big = 2 * 1024**3 + 1
    resp = _upload(media_client, headers={"content-length": str(too_big)})
    assert resp.status_code == 413
    # 零副作用：目录未建、行未入库
    assert not (tmp_path / "media").exists()
    assert _count_rows(media_client) == 0


def test_upload_content_length_exactly_2gb_not_rejected(media_client):
    """边界：Content-Length 恰等于 2GB 不拒（仅严格大于才 413）。"""
    resp = _upload(media_client, headers={"content-length": str(2 * 1024**3)})
    assert resp.status_code == 200


def test_upload_413_streaming_early_reject(media_client, tmp_path, monkeypatch):
    """流式兜底：骗过早拒后实际体超限 → 中止落盘、半成品清理、零入库。

    monkeypatch 收紧上限为 64 字节（核心层调用期读模块全局，补丁生效）；
    伪造 Content-Length: 50（≤ 64）骗过早拒路径，实际文件体 100 字节
    超限，由核心层流式累计拦截。
    """
    from tagplatform import media  # 延迟 import：路由未实现时其余用例先红

    monkeypatch.setattr(media, "MAX_UPLOAD_BYTES", 64)
    resp = _upload(media_client, content=b"x" * 100,
                   headers={"content-length": "50"})
    assert resp.status_code == 413
    # 半成品已清理：目录不存在或为空
    root = tmp_path / "media"
    assert not root.exists() or list(root.iterdir()) == []
    assert _count_rows(media_client) == 0


# ---------- 穿越与中文名免疫 ----------


def test_upload_traversal_and_chinese_name_immunity(media_client, tmp_path):
    """穿越/中文名免疫：存盘名只用 asset_id，不含原名任何成分。"""
    for name in ("../../evil/危险影片.mp4", "..\\..\\evil\\电影.webm",
                 "影片 标签.mkv"):
        resp = _upload(media_client, name=name)
        assert resp.status_code == 200, f"{name!r} 应上传成功"
        body = resp.json()
        stored = Path(body["video_uri"])
        suffix = Path(name).suffix.lower()
        assert stored.name == f"{body['asset_id']}{suffix}"
        # 原名成分（穿越段/中文/空格名）一律不出现在存盘名
        for fragment in ("..", "evil", "危险", "电影", "影片", "标签"):
            assert fragment not in stored.name
        assert stored.exists()


def test_reupload_same_asset_overwrites_one_file(media_client, tmp_path):
    """重复上传同一资产：仍然只有一个文件，路径不变、内容被替换。"""
    first = _upload(media_client, name="clip.mp4", content=b"old",
                    data={"asset_id": "asset_00000001"})
    assert first.status_code == 200
    old_uri = first.json()["video_uri"]

    second = _upload(media_client, name="clip.mp4", content=b"new-bytes",
                     data={"asset_id": "asset_00000001"})
    # 已绑定视频的资产走 409 保护，这里换一条"预留主键"的重复上传路径
    assert second.status_code == 409

    # 用同一 asset_id 但先清掉绑定，验证覆盖语义本身
    with media_client.app.state.session_factory() as s:
        s.get(MediaAsset, "asset_00000001").video_uri = None
        s.commit()
    third = _upload(media_client, name="clip.mp4", content=b"new-bytes",
                    data={"asset_id": "asset_00000001"})
    assert third.status_code == 200
    assert Path(third.json()["video_uri"]).name == "asset_00000001.mp4"
    files = [p.name for p in (tmp_path / "media").iterdir()]
    assert files == ["asset_00000001.mp4"]
    assert Path(third.json()["video_uri"]).read_bytes() == b"new-bytes"
    assert old_uri == third.json()["video_uri"]


def test_upload_413_keeps_previous_file_intact(media_client, tmp_path, monkeypatch):
    """覆盖上传中途 413：旧文件必须完好（先写 .part 再原子替换）。"""
    from tagplatform import media

    ok = _upload(media_client, name="clip.mp4", content=b"original",
                 data={"asset_id": "asset_00000002"})
    assert ok.status_code == 200
    path = Path(ok.json()["video_uri"])
    with media_client.app.state.session_factory() as s:
        s.get(MediaAsset, "asset_00000002").video_uri = None
        s.commit()

    monkeypatch.setattr(media, "MAX_UPLOAD_BYTES", 8)
    bad = _upload(media_client, content=b"x" * 100,
                  data={"asset_id": "asset_00000002"},
                  headers={"content-length": "4"})
    assert bad.status_code == 413
    assert path.read_bytes() == b"original"          # 旧文件没被截断
    assert [p.name for p in (tmp_path / "media").iterdir()] == [path.name]


# ---------- 401 未登录 ----------


def test_upload_unauthenticated_401(client):
    """未登录 401：新 TestClient 不带 cookie（protected_deps 统一挂载验证
    ——media router 未被遗漏）。"""
    with TestClient(client.app) as fresh:
        resp = fresh.post("/api/v1/media/upload",
                          files={"file": ("clip.mp4", b"x")})
    assert resp.status_code == 401


# ---------- asset_id 绑定（2026-11 上游对接） ----------


def _seed_asset(media_client, asset_id: str, title: str = "数据集影片",
                video_uri: str | None = None) -> None:
    """播种一枚已登记资产（模拟数据集/识别服务先给出标识）。"""
    from datetime import datetime, timezone

    with media_client.app.state.session_factory() as s:
        s.add(MediaAsset(asset_id=asset_id, asset_type="movie", title=title,
                         video_uri=video_uri,
                         created_at=datetime.now(timezone.utc)))
        s.commit()


def test_upload_binds_existing_asset(media_client):
    """asset_id 指向已存在资产：只补 video_uri，不新建行、不改标题。"""
    _seed_asset(media_client, "movie_tt0032138", title="绿野仙踪")
    before = _count_rows(media_client)

    resp = _upload(media_client, name="oz.mp4",
                   data={"asset_id": "movie_tt0032138"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["asset_id"] == "movie_tt0032138"
    assert body["title"] == "绿野仙踪"          # 数据集标题保留
    assert _count_rows(media_client) == before  # 零新增行

    with media_client.app.state.session_factory() as s:
        row = s.get(MediaAsset, "movie_tt0032138")
        assert row.video_uri == body["video_uri"]
        assert Path(body["video_uri"]).exists()


def test_upload_creates_asset_with_given_id(media_client):
    """asset_id 指向不存在的资产：以该标识新建（上传先于识别的预留主键）。"""
    resp = _upload(media_client, name="new.mp4", data={"asset_id": "movie_new_001"})
    assert resp.status_code == 200
    assert resp.json()["asset_id"] == "movie_new_001"
    with media_client.app.state.session_factory() as s:
        assert s.get(MediaAsset, "movie_new_001") is not None


def test_upload_refuses_to_overwrite_bound_asset(media_client):
    """已绑定视频的资产不能被上传覆盖：否则任何人可换掉别人的视频。"""
    _seed_asset(media_client, "movie_tt0111161", title="肖申克",
                video_uri="data/media/old.mp4")

    resp = _upload(media_client, name="new.mp4",
                   data={"asset_id": "movie_tt0111161"})
    assert resp.status_code == 409

    with media_client.app.state.session_factory() as s:
        assert s.get(MediaAsset, "movie_tt0111161").video_uri == "data/media/old.mp4"
