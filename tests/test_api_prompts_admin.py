"""Prompt 版本管理 API（admin）与激活原子性测试。

阶段 4 Task 2：改提示词零代码变更的最后一环——
- POST /api/v1/prompts：新版本插入 + 同 mode 旧激活行去激活（恰一个 active）；
- POST /api/v1/prompts/{prompt_id}/activate：激活切换（幂等，404 未知 id）；
- 校验失败 422（空文本/缺占位符）、非 admin 403；
- 原子性（Review Focus 1）：事务中途失败不留半行、同 mode 恒恰一个 active
  （服务级测试以"flush 已发出语句、commit 前抛错"的最险时序注入失败钉死）。
"""

import sqlite3

import pytest

# 各 mode 合法的新模板正文（占位符齐全）
CLOSED_V2_TEXT = "内容：{content}\n候选清单：{candidates}\n只输出 JSON 数组。"
OPEN_V2_TEXT = "内容：{content}\n自由生成 5 到 15 个标签。"


# ---------- POST /prompts：新建版本 ----------


def test_create_new_version_deactivates_old_exactly_one_active(client, tmp_path):
    """新建 closed v2：旧 v1 去激活、恰一个 active（直查库计数）；open 不受影响。"""
    seed_rows = client.get("/api/v1/prompts?mode=closed").json()
    assert len(seed_rows) == 1 and seed_rows[0]["version"] == 1

    resp = client.post("/api/v1/prompts", json={
        "mode": "closed", "template_text": CLOSED_V2_TEXT})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "closed"
    assert body["version"] == 2  # 该 mode 现最大版本 + 1
    assert body["is_active"] is True
    assert body["created_at"] is not None

    # API 视角：v1 去激活、v2 激活；列表直接带正文（新建预填依赖）
    rows = client.get("/api/v1/prompts?mode=closed").json()
    assert len(rows) == 2
    by_version = {r["version"]: r for r in rows}
    assert by_version[1]["is_active"] is False
    assert by_version[2]["is_active"] is True
    assert by_version[2]["template_text"] == CLOSED_V2_TEXT  # 新版逐字落库
    assert "{content}" in by_version[1]["template_text"]  # 种子 v1 正文原样带出

    # 直查库计数（绕过 API 的最硬断言）：同 mode 恰一个 active
    conn = sqlite3.connect(tmp_path / "tagplatform.db")
    try:
        counts = conn.execute(
            "SELECT mode, COUNT(*) FROM prompt_template WHERE is_active = 1"
            " GROUP BY mode").fetchall()
    finally:
        conn.close()
    assert counts == [("closed", 1), ("open", 1)]

    # open 模式不受影响：v1 仍激活
    open_rows = client.get("/api/v1/prompts?mode=open").json()
    assert len(open_rows) == 1 and open_rows[0]["is_active"] is True


def test_activate_switch_and_idempotent(client):
    """激活切换：建 v2 后激活 v1（v2 去激活）；重复激活幂等、状态不变。"""
    client.post("/api/v1/prompts", json={
        "mode": "closed", "template_text": CLOSED_V2_TEXT})
    rows = {r["version"]: r
            for r in client.get("/api/v1/prompts?mode=closed").json()}
    v1_id = rows[1]["id"]

    resp = client.post(f"/api/v1/prompts/{v1_id}/activate")
    assert resp.status_code == 200
    body = resp.json()
    # 契约字段精确：mode/version/is_active 三键
    assert body == {"mode": "closed", "version": 1, "is_active": True}

    by_version = {r["version"]: r
                  for r in client.get("/api/v1/prompts?mode=closed").json()}
    assert by_version[1]["is_active"] is True
    assert by_version[2]["is_active"] is False

    # 幂等：已激活再激活 → 返回不变、库状态不变
    resp2 = client.post(f"/api/v1/prompts/{v1_id}/activate")
    assert resp2.status_code == 200
    assert resp2.json() == body
    by_version2 = {r["version"]: r
                   for r in client.get("/api/v1/prompts?mode=closed").json()}
    assert by_version2[1]["is_active"] is True
    assert by_version2[2]["is_active"] is False


def test_activate_unknown_404(client):
    """激活未知 id → 404。"""
    resp = client.post("/api/v1/prompts/99999/activate")
    assert resp.status_code == 404


# ---------- POST /prompts：422 校验 ----------


@pytest.mark.parametrize("bad_text", ["", "   \n\t "])
def test_create_422_empty_text(client, bad_text):
    """空文本 / 纯空白（strip 后非空规则）→ 422，且不落库。"""
    resp = client.post("/api/v1/prompts", json={
        "mode": "closed", "template_text": bad_text})
    assert resp.status_code == 422
    # 失败不留行：closed 仍是 v1 单行激活
    rows = client.get("/api/v1/prompts?mode=closed").json()
    assert len(rows) == 1 and rows[0]["is_active"] is True


def test_create_422_missing_placeholders(client):
    """缺必需占位符 → 422（open 缺 {content}；closed 缺 {content}/{candidates}）。"""
    # open 缺 {content}
    assert client.post("/api/v1/prompts", json={
        "mode": "open", "template_text": "无占位符的模板正文"
    }).status_code == 422
    # closed 有 {content} 但缺 {candidates}
    assert client.post("/api/v1/prompts", json={
        "mode": "closed", "template_text": "内容 {content}，无候选清单"
    }).status_code == 422
    # closed 连 {content} 也缺
    assert client.post("/api/v1/prompts", json={
        "mode": "closed", "template_text": "只有正文没有占位符"
    }).status_code == 422
    # 三次失败都不落库：closed/open 仍各恰一行 v1 激活
    closed = client.get("/api/v1/prompts?mode=closed").json()
    open_rows = client.get("/api/v1/prompts?mode=open").json()
    assert len(closed) == 1 and closed[0]["is_active"] is True
    assert len(open_rows) == 1 and open_rows[0]["is_active"] is True


def test_create_bad_mode_422(client):
    """mode 取值 closed|open 之外 → 422。"""
    assert client.post("/api/v1/prompts", json={
        "mode": "hybrid", "template_text": "{content}"
    }).status_code == 422


# ---------- 非 admin 403 ----------


def test_non_admin_403_on_prompt_admin(client):
    """reviewer 访问版本管理写端点 → 403；只读列表放行（已登录合法用户）。"""
    assert client.post("/api/v1/users", json={
        "username": "bob", "password": "pw-bob-1", "role": "reviewer"}
    ).status_code == 200
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login",
                json={"username": "bob", "password": "pw-bob-1"})

    assert client.post("/api/v1/prompts", json={
        "mode": "closed", "template_text": CLOSED_V2_TEXT}).status_code == 403
    assert client.post("/api/v1/prompts/1/activate").status_code == 403
    # reviewer 是合法登录用户：只读列表放行
    assert client.get("/api/v1/prompts").status_code == 200


# ---------- 原子性（Review Focus 1，服务级注入失败） ----------


def test_create_prompt_version_atomic_on_failure(session, monkeypatch):
    """事务中途失败不留半行：flush 已发出语句、commit 前抛错 → 回滚后旧状态完整。

    以实例级替换 commit（先 flush 再抛错）模拟最险时序：去激活 UPDATE 与
    新行 INSERT 语句均已发出但未提交。回滚后同 mode 仍恰一个 active（种子 v1）。
    """
    from tagplatform.prompts import create_prompt_version

    def half_commit():
        session.flush()  # 让 UPDATE/INSERT 语句真实发出
        raise RuntimeError("注入：flush 后提交前失败")

    monkeypatch.setattr(session, "commit", half_commit)
    with pytest.raises(RuntimeError):
        create_prompt_version(session, "closed", CLOSED_V2_TEXT)
    session.rollback()

    rows = session.connection().exec_driver_sql(
        "SELECT version, is_active FROM prompt_template"
        " WHERE mode = 'closed' ORDER BY version").fetchall()
    assert rows == [(1, 1)]  # 无半行、恰一个 active（种子 v1 原样）


def test_activate_prompt_atomic_on_failure(session, monkeypatch):
    """激活切换事务中途失败：回滚后旧激活行原样（v2 仍激活、v1 未被误激活）。"""
    from tagplatform.db.models import PromptTemplate
    from tagplatform.prompts import activate_prompt_version, create_prompt_version

    create_prompt_version(session, "closed", CLOSED_V2_TEXT)  # v2 激活
    seed_v1 = session.query(PromptTemplate).filter_by(
        mode="closed", version=1).one()

    def half_commit():
        session.flush()
        raise RuntimeError("注入：flush 后提交前失败")

    monkeypatch.setattr(session, "commit", half_commit)
    with pytest.raises(RuntimeError):
        activate_prompt_version(session, seed_v1.id)
    session.rollback()

    rows = session.connection().exec_driver_sql(
        "SELECT version, is_active FROM prompt_template"
        " WHERE mode = 'closed' ORDER BY version").fetchall()
    assert rows == [(1, 0), (2, 1)]  # v2 仍激活，恰一个 active 不变
