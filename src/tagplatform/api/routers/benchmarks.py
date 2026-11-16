"""Authenticated benchmark API. Inputs and results live alongside MTG audit reports."""
import logging
import re
import uuid
from typing import Literal
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.routers.generation import _resolve_target_asset
from tagplatform.benchmark import (
    delete_finished_report, load_report, run, save, public_report, DEFAULT_EVALUATION_MODEL,
    EVALUATION_VERSION, EVALUATION_PROMPT, MAX_EVALUATION_PROMPT_LENGTH, CUSTOM_PROMPT_OUTPUT_FORMAT,
)
from tagplatform.library import try_load_library, TagLibrary, Directory, TagDef
from tagplatform.generation.xmlclass import XMLCLASS_VERSION
from dataclasses import asdict

router = APIRouter(prefix='/api/v1/benchmarks')


def root(request):
    return Path(request.app.state.settings.mtg_report_dir) / 'benchmarks'


class Create(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=50)
    max_tags: int = Field(default=15, ge=1, le=100)
    algorithm: Literal['mtg-v3', 'x-mlclass-online-v1'] = 'mtg-v3'
    evaluation_prompt: str | None = Field(default=None, max_length=MAX_EVALUATION_PROMPT_LENGTH)


@router.get('')
def listing(request: Request):
    directory = root(request)
    reports = []
    for path in directory.glob('*.json'):
        try:
            report = public_report(load_report(path))
            if 'created_at' not in report or 'id' not in report:
                raise ValueError('基准批次标识缺失')
            reports.append(report)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logging.getLogger(__name__).exception('无法读取基准报告 %s', path)
    return sorted(reports, key=lambda r: r['created_at'], reverse=True)


@router.get('/evaluation-prompt')
def evaluation_prompt():
    return {'prompt': EVALUATION_PROMPT, 'max_length': MAX_EVALUATION_PROMPT_LENGTH}


def _report_path(request: Request, report_id: str) -> Path:
    if not re.fullmatch(r'bench_[0-9a-f]{32}', report_id):
        raise HTTPException(404, '基准批次不存在')
    return root(request) / f'{report_id}.json'


@router.get('/{report_id}')
def detail(report_id: str, request: Request):
    path = _report_path(request, report_id)
    try:
        return load_report(path)
    except FileNotFoundError:
        raise HTTPException(404, '基准批次不存在') from None
    except (OSError, ValueError):
        raise HTTPException(500, '基准报告无法读取') from None


@router.delete('/{report_id}', status_code=204, response_class=Response)
def delete(report_id: str, request: Request):
    path = _report_path(request, report_id)
    try:
        removed = delete_finished_report(path)
    except FileNotFoundError:
        raise HTTPException(404, '基准批次不存在') from None
    except (OSError, ValueError):
        raise HTTPException(500, '无法删除基准报告，请重试') from None
    if not removed:
        raise HTTPException(409, '请等待批次结束后再删除')


@router.post('')
def create(body: Create, request: Request, background: BackgroundTasks, session: Session = Depends(get_session)):
    settings = request.app.state.settings
    validate_settings(settings, body.algorithm)
    library = try_load_library()
    if library is None or not library.dirs() or (body.algorithm == XMLCLASS_VERSION and not library.tags()):
        raise HTTPException(422, '标签库不可用')
    assets = {}
    for aid in body.asset_ids:
        asset = _resolve_target_asset(session, aid)
        assets[asset.asset_id] = asset
    report = new_report(settings, library, body.algorithm, body.max_tags, body.evaluation_prompt)
    report['items'] = [dict(asset_id=a.asset_id, title=a.title, content_json=a.content_json,
                            status='queued') for a in assets.values()]
    save(root(request), report)
    background.add_task(run, report, root(request), library, request.app)
    return public_report(report)


def validate_settings(settings, algorithm):
    model = DEFAULT_EVALUATION_MODEL
    if model == settings.llm_model or (algorithm == 'mtg-v3' and model == settings.mtg_judge_model):
        raise HTTPException(422, '生成模型或内部 judge 与固定评估模型 glm-5.3-flash 相同，请调整部署配置')
    if settings.use_mock_llm or settings.use_mock_embedder:
        raise HTTPException(422, '算法基准要求真实 LLM 和嵌入器')
    if algorithm == 'mtg-v3' and (settings.mtg_runs != 3 or not settings.mtg_judge_enabled or not settings.open_library_enabled):
        raise HTTPException(422, 'mtg-v3 基准要求真实 LLM/嵌入器、mtg_runs=3，并启用 judge 和标签库')
    if algorithm == XMLCLASS_VERSION:
        try:
            if '{label}' not in settings.xmlclass_nli_hypothesis:
                raise ValueError('missing label')
            settings.xmlclass_nli_hypothesis.format(label='测试')
        except (ValueError, KeyError, IndexError):
            raise HTTPException(422, 'NLI 假设模板必须包含 {label} 且不含其他占位符') from None


def new_report(settings, library, algorithm, max_tags, evaluation_prompt=None):
    prompt = EVALUATION_PROMPT if evaluation_prompt is None else evaluation_prompt.strip()
    if not prompt:
        raise HTTPException(422, '评估提示词不能为空')
    is_custom = prompt != EVALUATION_PROMPT
    return dict(id='bench_'+uuid.uuid4().hex, schema_version=2, algorithm=algorithm,
                  created_at=datetime.now(timezone.utc).isoformat(), status='queued',
                  evaluation_model=DEFAULT_EVALUATION_MODEL, max_tags=max_tags,
                  evaluation_version=EVALUATION_VERSION,
                  evaluation_prompt=prompt, custom_evaluation_prompt=is_custom,
                  evaluation_format_instruction=CUSTOM_PROMPT_OUTPUT_FORMAT if is_custom else '',
                  generation_model=settings.llm_model, embedding_model=settings.embedding_model,
                  generation_temperature=settings.llm_temperature,
                  library_version=library.version,
                  settings={k: v for k, v in settings.model_dump(mode='json').items() if k.startswith(('mtg_', 'open_', 'xmlclass_'))},
                  library={'dirs': [asdict(d) for d in library.dirs()], 'tags': [asdict(t) for t in library.tags()]},
                  items=[])


@router.post('/{report_id}/replay-xmlclass')
def replay_xmlclass(report_id: str, request: Request, background: BackgroundTasks):
    """Replay the immutable A snapshot; never re-read current assets or tag tables."""
    baseline = detail(report_id, request)
    settings = request.app.state.settings
    validate_settings(settings, XMLCLASS_VERSION)
    if baseline.get('algorithm') != 'mtg-v3' or baseline.get('status') != 'succeeded':
        raise HTTPException(422, '请选择已全部完成的 mtg-v3 基准批次')
    if (baseline.get('evaluation_version') != EVALUATION_VERSION
            or baseline.get('evaluation_model') != DEFAULT_EVALUATION_MODEL
            or not isinstance(baseline.get('evaluation_prompt'), str)
            or not baseline['evaluation_prompt'].strip()):
        raise HTTPException(422, '原批次评估口径不同，请先重新运行 mtg-v3')
    if (baseline.get('generation_model') != settings.llm_model
            or baseline.get('embedding_model') != settings.embedding_model
            or baseline.get('generation_temperature') != settings.llm_temperature):
        raise HTTPException(422, '生成模型、温度和嵌入模型须与原批次一致')
    try:
        snapshot = baseline['library']
        library = TagLibrary([Directory(**{**d, 'aliases': tuple(d.get('aliases', []))}) for d in snapshot['dirs']],
                             [TagDef(**{**t, 'aliases': tuple(t.get('aliases', []))}) for t in snapshot['tags']],
                             version=baseline['library_version'])
        if not library.tags() or not baseline['items']:
            raise ValueError('empty snapshot')
        report = new_report(settings, library, XMLCLASS_VERSION, baseline['max_tags'], baseline['evaluation_prompt'])
        report['evaluation_format_instruction'] = baseline.get('evaluation_format_instruction', '')
        report['custom_evaluation_prompt'] = baseline.get('custom_evaluation_prompt', False)
        report['baseline_id'] = baseline['id']
        report['items'] = [dict(asset_id=i['asset_id'], title=i['title'], content_json=i['content_json'],
                                status='queued', baseline_tags=i['tags']) for i in baseline['items']]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, '原批次快照不完整，无法回放') from None
    save(root(request), report)
    background.add_task(run, report, root(request), library, request.app)
    return public_report(report)
