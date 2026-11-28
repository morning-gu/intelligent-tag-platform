"""closed 产出标签的置信度计算测试（阶段 2.5，2026-12）。

覆盖契约（用户口径）：
- Cmod 本地算术：s_m=(强度−1)/4、dij=|si−sj|、Cs=1−(d12+d13+d23)/3、
  Cc=max(dij)、Cmod=j·Cs+(1−j)(1−Cc)；全同分 → 1.0，极端分歧 → 低位；
- Ccons：三次 Sevi（原判定 + 2 次重评）总体方差 → exp(−As/b)；有帧时
  重评消息带多模态 parts（三次同条件测量），无帧纯文本；重评失败降级
  为原分补位（As=0 → Ccons=1.0，partial 标注）；
- Cver（v2 模态两维）：LLM 输出三模态判定 JSON（Rn/充分度/T/V）；解析
  宽容（围栏/键名漂移/分值钳 1–5/缺模态补 Rn=0）；权重由充分度组合查
  固定菜单（本地），Cver = Σ Rn·Qn·(0.6T′+0.4V′)；全 Rn=0 / 失败降级
  0.5 占位；
- Craw 加权（默认 0.45/0.30/0.25，Settings 层归一化）并覆写
  tags[*].confidence；MD 报告含总览表、三张分量表与 partial 标注；
- apply_confidence 永不抛错（内部降级红线）：LLM 炸 / 材料缺全兜住，
  任务不落 failed；
- Mock 端到端（execute 路径）：同 run_id 哈希 → 三次分数相同 →
  Ccons=1.0；核验分支给确定性三模态判定（content 充分 / scene 支持 /
  frame 辅助，分值哈希采样）；落库 confidence=Craw ∈ [0.5, 1.0]。

Mock 技巧：_ScriptedLLM 按调用序分流（组判定 / 核验 / 重评），桩契约
与 test_generation_closed 的 raw_decode 组解析同口径。
"""

import json
import math

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM
from sqlalchemy.orm import Session

from tagplatform.capabilities.llm.client import LLMResponse
from tagplatform.capabilities.llm.mock import _extract_text
from tagplatform.services.generation import Evidence, GenerationSpec, ProducedTag
from tagplatform.services.generation.assemble import _render_content_text
from tagplatform.services.generation.closed.confidence import (
    ModalityVerdict,
    TagConfidence,
    _assign_modality_weights,
    _compute_c_mod,
    _compute_c_ver,
    _parse_verify_score,
    _reconsistency_scores,
    _render_report,
    _sanitize_name,
    _sevi_from_scores,
    apply_confidence,
)
from tagplatform.services.generation.closed.parse import LLMParseError
from tagplatform.settings import get_settings
from tests.library_fixture import write_library

from tests.test_task_runner import _make_task, _seed_catalog, _CONTENT_DATA


# ---- 纯数学层：Cmod / Sevi ----

def test_c_mod_extreme_split():
    """(1,5,1)：s=(0,1,0) → Cs=1/3、Cc=1 → Cmod=1/6。"""
    r = _compute_c_mod({"content": 1, "scene": 5, "frame": 1}, j=0.5)
    assert r.c_mod == pytest.approx(1.0 / 6.0)


def test_sevi_matches_producer_formula():
    weights = (0.3, 0.3, 0.4)
    assert _sevi_from_scores({"content": 5, "scene": 5, "frame": 5}, weights) == 1.0
    assert _sevi_from_scores({"content": 1, "scene": 1, "frame": 1}, weights) == 0.0
    # (5,5,1)：0.3*1 + 0.3*1 + 0.4*0 = 0.6
    assert _sevi_from_scores(
        {"content": 5, "scene": 5, "frame": 1}, weights) == pytest.approx(0.6)


# ---- Cver 解析层（v2 模态契约）----

def _v(r=1, suf="充分", t=None, v=None):
    return ModalityVerdict(r=r, sufficiency=suf, t=t, v=v)


def test_parse_verify_score_full_contract():
    raw = json.dumps({"modalities": {
        "content": {"r": 1, "sufficiency": "充分", "t": 4, "v": 5},
        "scene": {"r": 1, "sufficiency": "支持", "t": 3, "v": 4},
        "frame": {"r": 0, "sufficiency": "辅助"},
    }}, ensure_ascii=False)
    verdicts = _parse_verify_score(raw)
    assert verdicts["content"] == _v(1, "充分", 4, 5)
    assert verdicts["scene"] == _v(1, "支持", 3, 4)
    assert verdicts["frame"] == _v(0, "辅助")  # Rn=0 → t/v 置 None


def test_parse_verify_score_fenced_and_clamped():
    fenced = "```json\n" + json.dumps({"modalities": {
        "content": {"r": 1, "sufficiency": "充分", "t": 9, "v": 0},
    }}) + "\n```"
    verdicts = _parse_verify_score(fenced)
    assert verdicts["content"].t == 5   # 钳 1–5
    assert verdicts["content"].v == 1
    # 缺失模态补 Rn=0；键名漂移容忍（"模态"）
    drift = json.dumps({"模态": {"content": {"r": 1, "sufficiency": "充分",
                                             "t": 2, "v": 2}}})
    verdicts = _parse_verify_score(drift)
    assert verdicts["content"] == _v(1, "充分", 2, 2)
    assert verdicts["scene"] == _v(0, "") and verdicts["frame"] == _v(0, "")


def test_parse_verify_score_rejects_garbage():
    with pytest.raises(LLMParseError):
        _parse_verify_score("完全不是 JSON")
    with pytest.raises(LLMParseError):
        _parse_verify_score('{"other": 3}')
    # 旧契约 {"score": N} 已退役：缺 modalities 键 → 解析失败
    with pytest.raises(LLMParseError):
        _parse_verify_score('{"score": 4}')


def test_parse_verify_score_bad_sufficiency_falls_back():
    """异常充分度文案归「辅助」（最保守档），不抛错。"""
    raw = json.dumps({"modalities": {
        "content": {"r": 1, "sufficiency": "特别充分", "t": 4, "v": 4},
    }})
    verdicts = _parse_verify_score(raw)
    assert verdicts["content"].sufficiency == "充分"
    raw_bad = json.dumps({"modalities": {
        "content": {"r": 1, "sufficiency": "???", "t": 4, "v": 4},
    }})
    assert _parse_verify_score(raw_bad)["content"].sufficiency == "辅助"


# ---- Cver 权重菜单（本地映射）----

def test_weights_two_relevant_modalities():
    """二模态菜单 6 组合 + 大权重归充分模态。"""
    def weights(suf_a, suf_b):
        verdicts = {"content": _v(1, suf_a, 4, 4),
                    "scene": _v(1, suf_b, 4, 4), "frame": _v(0)}
        return _assign_modality_weights(verdicts)
    assert weights("充分", "充分") == {"content": 0.5, "scene": 0.5, "frame": 0.0}
    assert weights("充分", "支持") == {"content": 0.7, "scene": 0.3, "frame": 0.0}
    assert weights("充分", "辅助") == {"content": 0.9, "scene": 0.1, "frame": 0.0}
    assert weights("支持", "支持") == {"content": 0.5, "scene": 0.5, "frame": 0.0}
    assert weights("支持", "辅助") == {"content": 0.7, "scene": 0.3, "frame": 0.0}
    assert weights("辅助", "辅助") == {"content": 0.5, "scene": 0.5, "frame": 0.0}


def test_weights_two_modalities_ranking():
    """同级并列：大权重给 V 高者；V 平比 T；再平按 content→scene→frame。"""
    verdicts = {"content": _v(1, "充分", 4, 5), "scene": _v(1, "充分", 5, 4),
                "frame": _v(0)}
    w = _assign_modality_weights(verdicts)
    assert w["content"] == 0.5 and w["scene"] == 0.5  # 同级 0.5:0.5 无排序问题
    # 不同级（充分 vs 辅助）→ 0.9:0.1，充分方拿大权重
    verdicts = {"content": _v(1, "辅助", 5, 5), "scene": _v(1, "充分", 2, 2),
                "frame": _v(0)}
    w = _assign_modality_weights(verdicts)
    assert w["scene"] == 0.9 and w["content"] == 0.1


def test_weights_three_relevant_modalities():
    """三模态菜单归并四类：全有效 / 两有效+辅助 / 一有效+两辅助 / 全辅助。"""
    def weights(sufs, t_v=(4, 4)):
        verdicts = {m: _v(1, s, t_v[0], t_v[1]) for m, s in
                   zip(("content", "scene", "frame"), sufs)}
        return _assign_modality_weights(verdicts)
    # 三个都有效（充分/支持任一组合）→ {0.4, 0.3, 0.3}
    for sufs in (("充分", "充分", "充分"), ("充分", "支持", "支持"),
                 ("支持", "支持", "支持"), ("充分", "充分", "支持")):
        w = weights(sufs)
        assert sorted(w.values()) == [0.3, 0.3, 0.4]
        assert sum(w.values()) == pytest.approx(1.0)
    # 两个有效 + 一个辅助 → {0.5, 0.4, 0.1}
    for sufs in (("充分", "充分", "辅助"), ("充分", "支持", "辅助"),
                 ("支持", "支持", "辅助")):
        w = weights(sufs)
        assert sorted(w.values()) == [0.1, 0.4, 0.5]
    # 一个有效 + 两个辅助 → {0.7, 0.2, 0.1}
    for sufs in (("充分", "辅助", "辅助"), ("支持", "辅助", "辅助")):
        w = weights(sufs)
        assert sorted(w.values()) == [0.1, 0.2, 0.7]
    # 全辅助 → {0.4, 0.3, 0.3}
    w = weights(("辅助", "辅助", "辅助"))
    assert sorted(w.values()) == [0.3, 0.3, 0.4]


def test_weights_three_modalities_ranking():
    """三模态同级并列：大权重给更充分/V 更高模态。"""
    # 充分×2+支持 → 0.4 给充分（V 高者），0.3/0.3 分给其余
    verdicts = {"content": _v(1, "充分", 4, 4), "scene": _v(1, "充分", 4, 5),
                "frame": _v(1, "支持", 5, 5)}
    w = _assign_modality_weights(verdicts)
    assert w["scene"] == 0.4       # 充分 + V 高
    assert w["content"] == 0.3    # 充分但 V 低
    assert w["frame"] == 0.3
    # 充分+辅助×2 → 0.7:0.2:0.1：充分拿 0.7，辅助按 V 分 0.2/0.1
    verdicts = {"content": _v(1, "辅助", 4, 5), "scene": _v(1, "辅助", 4, 4),
                "frame": _v(1, "充分", 2, 2)}
    w = _assign_modality_weights(verdicts)
    assert w["frame"] == 0.7 and w["content"] == 0.2 and w["scene"] == 0.1


def test_weights_single_relevant_modality():
    """两模态 Rn=0 → 剩余模态权重 1。"""
    verdicts = {"content": _v(0), "scene": _v(0), "frame": _v(1, "充分", 3, 3)}
    assert _assign_modality_weights(verdicts) == {
        "content": 0.0, "scene": 0.0, "frame": 1.0}


def test_c_ver_computation_and_all_zero_degrades():
    """Cver = Σ Rn·Qn·(0.6T′+0.4V′)；全 Rn=0 抛错（上层降级）。"""
    verdicts = {"content": _v(1, "充分", 5, 5), "scene": _v(1, "充分", 5, 5),
                "frame": _v(1, "充分", 5, 5)}
    c_ver, w = _compute_c_ver(verdicts)
    assert c_ver == pytest.approx(1.0)          # 全满分
    assert sorted(w.values()) == [0.3, 0.3, 0.4]
    # 单模态：Qn=1，Cver,n = 0.6·0.75+0.4·0.75 = 0.75
    verdicts = {"content": _v(1, "充分", 4, 4), "scene": _v(0), "frame": _v(0)}
    c_ver, w = _compute_c_ver(verdicts)
    assert c_ver == pytest.approx(0.75)
    assert w["content"] == 1.0
    # 全 Rn=0 → ValueError（调用方降级 0.5 + partial）
    with pytest.raises(ValueError):
        _compute_c_ver({"content": _v(0), "scene": _v(0), "frame": _v(0)})


# ---- 文件名清洗 ----

def test_sanitize_name():
    assert _sanitize_name("流浪地球 2 / EP:01") == "流浪地球_2_EP_01"
    assert _sanitize_name("") == "asset"
    assert _sanitize_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


# ---- 桩 LLM：按调用分流（重评组 / 核验） ----

class _ScriptedConfLLM:
    """置信度环节专用桩：按 prompt 特征分流，计数调用次数。

    - 含 "## 候选标签分组"：Ccons 重评请求 → 返回指定 Sevi 的判定 JSON
      （按调用序取 reconsistency_outputs[i]，可实现三次分数不同）；
    - 含 "## 证据核验"：Cver 核验请求 → 返回三模态判定 JSON
      （verify_output，默认单模态 content：Rn=1 充分 T=V=4 → Cver=0.75）；
    - 其余：抛 AssertionError（置信度环节不应发其他请求）。

    消息取文本走 _extract_text 口径（纯文本与多模态 parts 两种形态
    兼容——Ccons/Cver 有帧时消息带关键帧 parts）。
    """

    def __init__(self, reconsistency_outputs: list[dict] | None = None,
                 verify_output: dict | None = None):
        self._reoutputs = reconsistency_outputs or []
        self._verify = (verify_output if verify_output is not None
                        else {"modalities": {
                            "content": {"r": 1, "sufficiency": "充分",
                                        "t": 4, "v": 4}}})
        self.re_calls = 0
        self.verify_calls = 0

    model = "stub-conf"

    def complete(self, messages, run_id):
        text = _extract_text(messages)
        if "## 候选标签分组" in text:
            # 单标签成组：从组 JSON 尾巴取 tag_id，拼进判定
            tail = text.partition("\n\n## 候选标签分组\n")[2]
            group = json.JSONDecoder().raw_decode(tail)[0]
            tag_id = group["tags"][0]["tag_id"]
            output = self._reoutputs[self.re_calls % len(self._reoutputs)]
            item = {
                "tag_id": tag_id,
                "applicability": output["applicability"],
                "evidence": output["evidence"],
            }
            self.re_calls += 1
            return LLMResponse(content=json.dumps([item], ensure_ascii=False),
                               usage={}, raw={}, ok=True)
        if "## 证据核验" in text:
            self.verify_calls += 1
            return LLMResponse(
                content=json.dumps(self._verify, ensure_ascii=False),
                usage={}, raw={}, ok=True)
        raise AssertionError(f"置信度环节收到意外请求：{text[:200]!r}")


def _evi(ec: int, es: int, ef: int) -> dict:
    return {m: {"evidence_strength": s, "evidences": []}
            for m, s in (("content", ec), ("scene", es), ("frame", ef))}


def test_apply_confidence_full_flow(session, tmp_path, monkeypatch):
    """全流程：重评两轮 → Ccons/Cver/Cmod → Craw 覆写 + 报告落盘。"""
    from tagplatform.services.content.content_io import load_content
    from tagplatform.services.generation.assemble import build_spec, load_assembly_context

    _seed_catalog(session)
    content = load_content(json.dumps(_CONTENT_DATA, ensure_ascii=False))
    context = load_assembly_context("closed", "{}")
    spec = build_spec(context, content=content, task_id="conf_t1",
                      asset_id="asset_x", run_id="conf_t1:run1")
    # 标签 A 原分 0.75，重评两轮给 (4,5,4) → Sevi=0.6、(4,5,4) → 0.6：
    # 三次 [0.75, 0.6, 0.6] → mean=0.65
    # 标签 B 原分 0.6，重评给 (5,5,5) → 1.0、(1,1,1) → 0.0
    llm = _ScriptedConfLLM(
        reconsistency_outputs=[
            # tag A 第 2、3 次
            {"applicability": 5, "evidence": _evi(4, 5, 4)},
            {"applicability": 5, "evidence": _evi(4, 5, 4)},
            # tag B 第 2、3 次
            {"applicability": 5, "evidence": _evi(5, 5, 5)},
            {"applicability": 5, "evidence": _evi(1, 1, 1)},
        ],
        verify_output={"modalities": {
            "content": {"r": 1, "sufficiency": "充分", "t": 4, "v": 4}}},
    )
    tag_a = ProducedTag(
        origin="catalog", tag_id="tag_g_00", tag_name="类型标签0",
        candidate_type=None, candidate_description=None,
        relevance_score=0.9, confidence=0.75,
        evidence=[Evidence(scene_id="scene_001", start_ms=1, end_ms=2,
                           description="证据A")],
        dir_id="dir_genre", evidence_scores={"content": 4, "scene": 5, "frame": 4},
    )
    tag_b = ProducedTag(
        origin="catalog", tag_id="tag_g_01", tag_name="类型标签1",
        candidate_type=None, candidate_description=None,
        relevance_score=0.8, confidence=0.6,
        evidence=[Evidence(scene_id="scene_001", start_ms=1, end_ms=2,
                           description="证据B")],
        dir_id="dir_genre", evidence_scores={"content": 5, "scene": 5, "frame": 5},
    )
    report_dir = tmp_path / "conf-out"
    monkeypatch.setenv("TAGPLT_CLOSED_CONFIDENCE_REPORT_DIR", str(report_dir))
    # 重置 settings 缓存使 env 生效
    get_settings.cache_clear()

    results = apply_confidence([tag_a, tag_b], spec, "测试资产", session, llm)

    assert len(results) == 2
    ra, rb = results
    # --- 标签 A：Ccons 三次 [0.75, 0.6, 0.6] ---
    sevi_weights = get_settings().closed_evi_weight_values
    sevi_454 = _sevi_from_scores({"content": 4, "scene": 5, "frame": 4}, sevi_weights)
    assert ra.sevi_scores == pytest.approx([0.75, sevi_454, sevi_454])
    mean = (0.75 + sevi_454 + sevi_454) / 3
    var = ((0.75 - mean) ** 2 + 2 * (sevi_454 - mean) ** 2) / 3
    assert ra.variance == pytest.approx(var)
    assert ra.c_cons == pytest.approx(math.exp(-var / 0.2))
    assert not ra.cons_partial
    # Cver：单模态判定 content(充分 T4 V4) → Qn=1，
    # Cver = 0.6·(3/4) + 0.4·(3/4) = 0.75
    assert ra.verify_score["content"] == _v(1, "充分", 4, 4)
    assert ra.ver_weights == {"content": 1.0, "scene": 0.0, "frame": 0.0}
    assert ra.c_ver == pytest.approx(0.75)
    assert not ra.ver_partial
    # Cmod (4,5,4)
    assert ra.c_mod == pytest.approx(0.5 * (1 - 0.5 / 3) + 0.5 * 0.75)
    assert not ra.mod_partial
    # Craw 加权覆写
    w = get_settings().closed_confidence_weight_values
    expected_a = (w[0] * ra.c_cons + w[1] * 0.75 + w[2] * ra.c_mod)
    assert ra.c_raw == pytest.approx(expected_a)
    assert tag_a.confidence == pytest.approx(expected_a)

    # --- 标签 B：三次 [0.6, 1.0, 0.0] → 大方差 → 低 Ccons ---
    assert rb.sevi_scores == pytest.approx([0.6, 1.0, 0.0])
    mean_b = (0.6 + 1.0 + 0.0) / 3
    var_b = ((0.6 - mean_b) ** 2 + (1.0 - mean_b) ** 2 + (0.0 - mean_b) ** 2) / 3
    assert rb.variance == pytest.approx(var_b)
    assert rb.c_cons == pytest.approx(math.exp(-var_b / 0.2))
    assert rb.c_mod == 1.0  # (5,5,5) 全同
    expected_b = (w[0] * rb.c_cons + w[1] * 0.75 + w[2] * 1.0)
    assert tag_b.confidence == pytest.approx(expected_b)

    # 调用计数：重评 2 轮 × 2 标签 = 4 次；核验 2 次
    assert llm.re_calls == 4
    assert llm.verify_calls == 2

    # 报告落盘
    reports = list(report_dir.glob("*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "测试资产" in text
    assert "类型标签0" in text and "类型标签1" in text
    assert "partial" not in text  # 全分量成功，无降级标注


def test_apply_confidence_degrades_when_llm_fails(session, tmp_path,
                                                   monkeypatch):
    """降级红线：LLM 炸 + 核验炸 → 占位分量 + partial，绝不抛错。"""
    from tagplatform.services.content.content_io import load_content
    from tagplatform.services.generation.assemble import build_spec, load_assembly_context

    _seed_catalog(session)
    content = load_content(json.dumps(_CONTENT_DATA, ensure_ascii=False))
    context = load_assembly_context("closed", "{}")
    spec = build_spec(context, content=content, task_id="conf_t2",
                      asset_id="asset_x", run_id="conf_t2:run1")

    class BrokenLLM:
        model = "broken"
        def complete(self, messages, run_id):
            return LLMResponse(content="", usage={}, raw={"error": "boom"},
                               ok=False)

    tag = ProducedTag(
        origin="catalog", tag_id="tag_g_00", tag_name="类型标签0",
        candidate_type=None, candidate_description=None,
        relevance_score=0.9, confidence=0.75,
        evidence=[], dir_id="dir_genre",
        evidence_scores={"content": 4, "scene": 5, "frame": 4},
    )
    monkeypatch.setenv("TAGPLT_CLOSED_CONFIDENCE_REPORT_DIR",
                       str(tmp_path / "broken-out"))
    get_settings.cache_clear()

    results = apply_confidence([tag], spec, "炸了也稳", session, BrokenLLM())

    assert len(results) == 1
    r = results[0]
    # Ccons：重评失败 → 原分补位（三同分 → As=0 → 1.0）+ partial
    assert r.cons_partial
    assert r.sevi_scores == [0.75, 0.75, 0.75]
    assert r.c_cons == pytest.approx(1.0)
    # Cver：失败 → 0.5 占位 + partial
    assert r.ver_partial
    assert r.c_ver == pytest.approx(0.5)
    # Cmod：evidence_scores 在 → 正常算
    assert not r.mod_partial
    w = get_settings().closed_confidence_weight_values
    expected = (w[0] * 1.0 + w[1] * 0.5
                + w[2] * (0.5 * (1 - 0.5 / 3) + 0.5 * 0.75))
    assert r.c_raw == pytest.approx(expected)
    assert tag.confidence == pytest.approx(expected)
    # 报告写盘正常（partial 标注可见）
    reports = list((tmp_path / "broken-out").glob("*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "partial" in text


def test_apply_confidence_missing_evidence_scores_degrades(session,
                                                            tmp_path,
                                                            monkeypatch):
    """缺 evidence_scores（旧数据/直构造）→ Cmod 占位 0.5 + partial。"""
    from tagplatform.services.content.content_io import load_content
    from tagplatform.services.generation.assemble import build_spec, load_assembly_context

    _seed_catalog(session)
    content = load_content(json.dumps(_CONTENT_DATA, ensure_ascii=False))
    context = load_assembly_context("closed", "{}")
    spec = build_spec(context, content=content, task_id="conf_t3",
                      asset_id="asset_x", run_id="conf_t3:run1")
    llm = _ScriptedConfLLM(
        reconsistency_outputs=[{"applicability": 5, "evidence": _evi(5, 5, 5)}],
        verify_output={"modalities": {
            "content": {"r": 1, "sufficiency": "充分", "t": 5, "v": 5}}},
    )
    tag = ProducedTag(
        origin="catalog", tag_id="tag_g_00", tag_name="类型标签0",
        candidate_type=None, candidate_description=None,
        relevance_score=0.9, confidence=1.0,
        evidence=[], dir_id="dir_genre",
        # evidence_scores 缺省（None）
    )
    monkeypatch.setenv("TAGPLT_CLOSED_CONFIDENCE_REPORT_DIR",
                       str(tmp_path / "noscore-out"))
    get_settings.cache_clear()

    results = apply_confidence([tag], spec, "缺读数", session, llm)
    r = results[0]
    assert r.mod_partial
    assert r.c_mod == pytest.approx(0.5)
    # Ccons 重评正常（同 5 分 → Sevi=1.0；原分 1.0 → 三次全 1.0）
    assert r.c_cons == pytest.approx(1.0)
    # Cver：单模态满分 → Cver=1.0
    assert r.c_ver == pytest.approx(1.0)
    w = get_settings().closed_confidence_weight_values
    assert r.c_raw == pytest.approx(w[0] * 1.0 + w[1] * 1.0 + w[2] * 0.5)
    assert tag.confidence == pytest.approx(r.c_raw)


def test_apply_confidence_never_raises(session, tmp_path, monkeypatch):
    """内部整体异常（如 settings 解析炸）也绝不抛错、confidence 不动。"""
    from tagplatform.services.content.content_io import load_content
    from tagplatform.services.generation.assemble import build_spec, load_assembly_context

    _seed_catalog(session)
    content = load_content(json.dumps(_CONTENT_DATA, ensure_ascii=False))
    context = load_assembly_context("closed", "{}")
    spec = build_spec(context, content=content, task_id="conf_t4",
                      asset_id="asset_x", run_id="conf_t4:run1")
    tag = ProducedTag(
        origin="catalog", tag_id="tag_g_00", tag_name="类型标签0",
        candidate_type=None, candidate_description=None,
        relevance_score=0.9, confidence=0.42, evidence=[], dir_id="dir_genre",
    )

    def explode(*args, **kwargs):
        raise RuntimeError("settings 层炸了")

    monkeypatch.setattr(
        "tagplatform.services.generation.closed.confidence.get_settings",
        explode)
    results = apply_confidence([tag], spec, "整体炸", session, _ScriptedConfLLM())
    assert results == []
    assert tag.confidence == 0.42  # 原值保持


def test_apply_confidence_empty_tags(session, tmp_path):
    """空标签列表直接返回 []，零 LLM 调用。"""
    from tagplatform.services.content.content_io import load_content
    from tagplatform.services.generation.assemble import build_spec, load_assembly_context

    content = load_content(json.dumps(_CONTENT_DATA, ensure_ascii=False))
    context = load_assembly_context("closed", "{}")
    spec = build_spec(context, content=content, task_id="conf_t5",
                      asset_id="asset_x", run_id="conf_t5:run1")
    llm = _ScriptedConfLLM()
    assert apply_confidence([], spec, "空", session, llm) == []
    assert llm.re_calls == 0 and llm.verify_calls == 0


def test_reconsistency_scores_with_frames_multimodal(session):
    """有帧时重评消息走多模态 parts（用户口径：三次判定同条件带图）。

    帧列表注入后：content = [text part, *frame_parts]，text part 仍是
    完整组 JSON 拼法正文，帧 parts 原样跟在后面；无帧调用方不受影响。
    """
    from tagplatform.services.content.content_io import load_content
    from tagplatform.services.generation.assemble import build_spec, load_assembly_context

    _seed_catalog(session)
    content = load_content(json.dumps(_CONTENT_DATA, ensure_ascii=False))
    context = load_assembly_context("closed", "{}")
    spec = build_spec(context, content=content, task_id="conf_t7",
                      asset_id="asset_x", run_id="conf_t7:run1")
    captured = []

    class CaptureLLM:
        model = "cap"
        def complete(self, messages, run_id):
            captured.append(messages[0]["content"])
            return LLMResponse(content=json.dumps([{
                "tag_id": "tag_g_00", "applicability": 5,
                "evidence": _evi(4, 4, 4)}], ensure_ascii=False),
                usage={}, raw={}, ok=True)

    tag = ProducedTag(
        origin="catalog", tag_id="tag_g_00", tag_name="类型标签0",
        candidate_type=None, candidate_description=None,
        relevance_score=0.9, confidence=0.75, evidence=[], dir_id="dir_genre",
    )
    frame_parts = [
        {"type": "text", "text": "〔关键帧〕scene_001 第 1/3 张"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}},
    ]
    scores = _reconsistency_scores(spec, tag, CaptureLLM(),
                                   (0.3, 0.3, 0.4), frame_parts)
    assert scores == [0.75, 0.75]
    assert len(captured) == 2
    for msg_content in captured:
        # 多模态形态：首 part 为 text（完整组拼法正文），帧 parts 依序跟后
        assert isinstance(msg_content, list)
        assert msg_content[0]["type"] == "text"
        prompt = msg_content[0]["text"]
        head, sep, tail = prompt.partition("\n\n## 候选标签分组\n")
        assert sep and head
        group = json.JSONDecoder().raw_decode(tail)[0]
        assert group["tags"][0]["tag_id"] == "tag_g_00"
        rest = msg_content[1:]
        assert rest == frame_parts
