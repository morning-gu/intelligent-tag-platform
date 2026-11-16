"""Authenticated benchmark API. Inputs and results live alongside MTG audit reports."""
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.routers.generation import _resolve_target_asset
from tagplatform.benchmark import run, save, public_report
from tagplatform.library import try_load_library
from dataclasses import asdict

router = APIRouter(prefix='/api/v1/benchmarks')


def root(request):
    return Path(request.app.state.settings.mtg_report_dir) / 'benchmarks'


class Create(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=50)
    max_tags: int = Field(default=15, ge=1, le=100)
    evaluation_model: str = Field(min_length=1, max_length=200)


@router.get('')
def listing(request: Request):
    directory = root(request)
    reports = []
    for path in directory.glob('*.json'):
        try:
            report = public_report(json.loads(path.read_text(encoding='utf-8')))
            if 'created_at' not in report or 'id' not in report:
                raise ValueError('基准批次标识缺失')
            reports.append(report)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logging.getLogger(__name__).exception('无法读取基准报告 %s', path)
    return sorted(reports, key=lambda r: r['created_at'], reverse=True)


@router.get('/{report_id}')
def detail(report_id: str, request: Request):
    if not re.fullmatch(r'bench_[0-9a-f]{32}', report_id):
        raise HTTPException(404, '基准批次不存在')
    try:
        return json.loads((root(request) / f'{report_id}.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise HTTPException(404, '基准批次不存在') from None
    except (OSError, ValueError):
        raise HTTPException(500, '基准报告无法读取') from None


@router.post('')
def create(body: Create, request: Request, background: BackgroundTasks, session: Session = Depends(get_session)):
    settings = request.app.state.settings
    model = body.evaluation_model.strip()
    if not model or model in (settings.llm_model, settings.mtg_judge_model):
        raise HTTPException(422, '请选择与生成模型、内部 judge 不同的独立评估模型')
    if (settings.use_mock_llm or settings.use_mock_embedder or settings.mtg_runs != 3
            or not settings.mtg_judge_enabled or not settings.open_library_enabled):
        raise HTTPException(422, 'mtg-v3 基准要求真实 LLM/嵌入器、mtg_runs=3，并启用 judge 和标签库')
    library = try_load_library()
    if library is None or not library.dirs():
        raise HTTPException(422, '标签库不可用')
    assets = {}
    for aid in body.asset_ids:
        asset = _resolve_target_asset(session, aid)
        assets[asset.asset_id] = asset
    report = dict(id='bench_'+uuid.uuid4().hex, schema_version=1, algorithm='mtg-v3',
                  created_at=datetime.now(timezone.utc).isoformat(), status='queued',
                  evaluation_model=model, max_tags=body.max_tags,
                  evaluation_version='evidence-concept-v1',
                  generation_model=settings.llm_model, embedding_model=settings.embedding_model,
                  generation_temperature=settings.llm_temperature,
                  library_version=library.version,
                  settings={k: v for k, v in settings.model_dump(mode='json').items() if k.startswith(('mtg_', 'open_'))},
                  library={'dirs': [asdict(d) for d in library.dirs()], 'tags': [asdict(t) for t in library.tags()]},
                  items=[dict(asset_id=a.asset_id, title=a.title, content_json=a.content_json, status='queued') for a in assets.values()])
    save(root(request), report)
    background.add_task(run, report, root(request), library, request.app)
    return public_report(report)
