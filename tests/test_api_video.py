"""视频流端点 API 测试（Task 6 [B6]）。

覆盖契约：
- GET /api/v1/assets/{asset_id}/video：
  - 无 Range → 200 全量（FileResponse 磁盘流式）+ Accept-Ranges: bytes；
  - 合法 Range: bytes=start-end → 206 + Content-Range: bytes start-end/total
    + Accept-Ranges，分块内容逐字节精确断言；end 越界截到文件尾；开放式
    bytes=start- 与后缀 bytes=-N 同样按 RFC 7233 标准语义支持；
  - start 越界（>= 文件大小）→ 416 + Content-Range: bytes */total
    （计划 Review Focus 3 允许"忽略回 200 或 416"两可——定 416，语义更
    标准）；
  - Range 语法非法（非 bytes= 前缀 / 解析失败 / start > end / 多区间）→
    忽略 Range 回 200 全量；
  - asset 不存在 → 404；无 video_uri / 文件 is_file() False → 404
    {"detail": "视频文件不存在"}；
  - Content-Type 按后缀映射（.mp4/.webm/.mkv，大小写不敏感，其余
    application/octet-stream）；
  - video_uri 经 resolve_video_path 解析（file:// 取路径部分 / 绝对路径
    原样）——file:// 用例钉死复用而非本层重写；
  - 端点受全 API 保护：未登录 401（protected_deps 挂载覆盖）。

播种模式（同 test_api_review）：经 client.app.state.session_factory 直接
向 client fixture 的临时文件库插 MediaAsset 行，video_uri 指向 tmp_path
临时二进制文件（任意字节模式，测试零真实视频依赖）。
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tagplatform.db.models import MediaAsset

# 1024 字节已知模式：content[i] = i % 256（分块字节级断言的期望值来源）
CONTENT = bytes(range(256)) * 4
TOTAL = len(CONTENT)

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed_video(client, tmp_path, name="video.mp4", data=CONTENT,
                video_uri=None) -> tuple[str, object]:
    """写临时二进制文件并播种 MediaAsset 行，返回 (asset_id, 文件路径)。

    video_uri 缺省取文件绝对路径（resolve_video_path 绝对路径原样规则）；
    传 file:// 等自定义 URI 时按原值播种。
    """
    file_path = tmp_path / name
    file_path.write_bytes(data)
    asset_id = "asset_" + name.replace(".", "_")
    with client.app.state.session_factory() as s:
        s.add(MediaAsset(asset_id=asset_id, asset_type="movie",
                         title="测试影片",
                         video_uri=str(file_path) if video_uri is None
                         else video_uri,
                         created_at=_NOW))
        s.commit()
    return asset_id, file_path


# ---------- 200 全量 ----------


def test_video_200_full_without_range(client, tmp_path):
    """无 Range 头 → 200 全量，字节与文件一致，带 Accept-Ranges 声明。"""
    asset_id, _ = _seed_video(client, tmp_path)
    resp = client.get(f"/api/v1/assets/{asset_id}/video")
    assert resp.status_code == 200
    assert resp.content == CONTENT
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-type"] == "video/mp4"


# ---------- 206 分块与 Content-Range ----------


def test_video_206_partial_chunk(client, tmp_path):
    """合法 bytes=start-end → 206，Content-Range 闭区间/总长，分块逐字节一致。"""
    asset_id, _ = _seed_video(client, tmp_path)
    resp = client.get(f"/api/v1/assets/{asset_id}/video",
                      headers={"Range": "bytes=10-19"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 10-19/{TOTAL}"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == CONTENT[10:20]


def test_video_206_end_beyond_size_truncated(client, tmp_path):
    """end 越界 → 截到文件尾（206 而非 416，RFC 7233 语义）。"""
    asset_id, _ = _seed_video(client, tmp_path)
    resp = client.get(f"/api/v1/assets/{asset_id}/video",
                      headers={"Range": f"bytes={TOTAL - 24}-999999"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes {TOTAL - 24}-{TOTAL - 1}/{TOTAL}"
    assert resp.content == CONTENT[-24:]


def test_video_206_open_ended_range(client, tmp_path):
    """开放式 bytes=start- → 206 到文件尾（浏览器视频拖动的常见首请求形）。"""
    asset_id, _ = _seed_video(client, tmp_path)
    resp = client.get(f"/api/v1/assets/{asset_id}/video",
                      headers={"Range": "bytes=1000-"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 1000-{TOTAL - 1}/{TOTAL}"
    assert resp.content == CONTENT[1000:]


def test_video_206_suffix_range(client, tmp_path):
    """后缀 bytes=-N → 最后 N 字节；N 超文件长取全文件（RFC 7233 语义）。"""
    asset_id, _ = _seed_video(client, tmp_path)
    resp = client.get(f"/api/v1/assets/{asset_id}/video",
                      headers={"Range": "bytes=-10"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes {TOTAL - 10}-{TOTAL - 1}/{TOTAL}"
    assert resp.content == CONTENT[-10:]

    resp = client.get(f"/api/v1/assets/{asset_id}/video",
                      headers={"Range": f"bytes=-{TOTAL * 5}"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 0-{TOTAL - 1}/{TOTAL}"
    assert resp.content == CONTENT


# ---------- 416 起始越界 ----------


def test_video_416_start_beyond_size(client, tmp_path):
    """start 越界（>= 文件大小）→ 416 + Content-Range: bytes */total。

    计划 Review Focus 3 允许"忽略回 200 或 416 取一致者"——本实现定 416。
    """
    asset_id, _ = _seed_video(client, tmp_path)
    resp = client.get(f"/api/v1/assets/{asset_id}/video",
                      headers={"Range": f"bytes={TOTAL + 100}-{TOTAL + 200}"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{TOTAL}"

    # start 恰等于文件大小同样越界（无可服务字节）
    resp = client.get(f"/api/v1/assets/{asset_id}/video",
                      headers={"Range": f"bytes={TOTAL}-{TOTAL}"})
    assert resp.status_code == 416


# ---------- 非法 Range 忽略回 200 ----------


@pytest.mark.parametrize("bad_range", [
    "foo=0-10",           # 非 bytes= 单位
    "bytes",              # 缺 =
    "bytes=",             # 空区间
    "bytes=-",            # 空区间
    "bytes=abc",          # 解析失败
    "bytes=10-5",         # start > end
    "bytes=0-10,20-30",   # 多区间不支持 → 整头忽略
])
def test_video_invalid_range_ignored_200(client, tmp_path, bad_range):
    """Range 语法非法 → 忽略 Range 回 200 全量（而非报错或部分分块）。"""
    asset_id, _ = _seed_video(client, tmp_path)
    resp = client.get(f"/api/v1/assets/{asset_id}/video",
                      headers={"Range": bad_range})
    assert resp.status_code == 200
    assert resp.content == CONTENT
    assert resp.headers["accept-ranges"] == "bytes"


# ---------- 404 两类 ----------


def test_video_404_unknown_asset(client):
    """asset_id 不存在 → 404。"""
    resp = client.get("/api/v1/assets/asset_none/video")
    assert resp.status_code == 404


def test_video_404_no_video_uri(client, tmp_path):
    """asset 存在但 video_uri 为空 → 404 {"detail": "视频文件不存在"}。"""
    with client.app.state.session_factory() as s:
        s.add(MediaAsset(asset_id="asset_nouri", asset_type="movie",
                         title="无视频资产", created_at=_NOW))
        s.commit()
    resp = client.get("/api/v1/assets/asset_nouri/video")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "视频文件不存在"


def test_video_404_file_missing(client, tmp_path):
    """video_uri 指向不存在文件（is_file() False）→ 404 同 detail。"""
    with client.app.state.session_factory() as s:
        s.add(MediaAsset(asset_id="asset_missing", asset_type="movie",
                         title="文件缺失资产",
                         video_uri=str(tmp_path / "不存在.mp4"),
                         created_at=_NOW))
        s.commit()
    resp = client.get("/api/v1/assets/asset_missing/video")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "视频文件不存在"


# ---------- Content-Type 映射 ----------


@pytest.mark.parametrize("suffix,expected", [
    (".mp4", "video/mp4"),
    (".webm", "video/webm"),
    (".mkv", "video/x-matroska"),
    (".bin", "application/octet-stream"),
    (".MP4", "video/mp4"),  # 后缀大小写不敏感
])
def test_video_content_type_mapping(client, tmp_path, suffix, expected):
    """Content-Type 按后缀映射：契约三种视频格式 + 大小写 + 兜底 octet-stream。"""
    asset_id, _ = _seed_video(client, tmp_path, name=f"clip{suffix}")
    resp = client.get(f"/api/v1/assets/{asset_id}/video")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == expected


# ---------- video_uri 解析复用 ----------


def test_video_file_uri_resolution(client, tmp_path, monkeypatch):
    """video_uri 经 resolve_video_path(video_uri, None) 解析（复用钉死）。

    resolve_video_path 的 file:// 分支产出 POSIX 风格路径（阶段 1 契约，
    其单测断言 file:///data/a.mp4 → /data/a.mp4），Windows 盘符 file:///
    C:/... 解析出的 /C:/... 不指向真实文件——故以桩替其返回值，用入参
    断言钉死 (video_uri, None) 复用契约，再服务真实临时文件验证端到端。
    """
    import tagplatform.api.routers.video as video_router

    file_path = tmp_path / "video.mp4"
    file_path.write_bytes(CONTENT)
    with client.app.state.session_factory() as s:
        s.add(MediaAsset(asset_id="asset_video_mp4", asset_type="movie",
                         title="测试影片",
                         video_uri="file:///data/movie_002.mp4",
                         created_at=_NOW))
        s.commit()
    calls = {}

    def fake_resolve(uri, video_root):
        calls["args"] = (uri, video_root)
        return str(file_path)

    monkeypatch.setattr(video_router, "resolve_video_path", fake_resolve)
    resp = client.get("/api/v1/assets/asset_video_mp4/video")
    assert resp.status_code == 200
    assert resp.content == CONTENT
    # 复用契约：file:// 原样透传解析器，video_root 恒为 None
    assert calls["args"] == ("file:///data/movie_002.mp4", None)


# ---------- 全 API 保护 ----------


def test_video_unauthenticated_401(client):
    """未登录 401：新 TestClient 不带 cookie（protected_deps 统一挂载验证
    ——video router 未被遗漏）。"""
    with TestClient(client.app) as fresh:
        assert fresh.get("/api/v1/assets/asset_x/video").status_code == 401
