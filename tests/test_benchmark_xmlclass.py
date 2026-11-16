import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from tagplatform import benchmark
from tagplatform.api.routers import benchmarks as api
from tagplatform.embed.embedding import MockEmbedder
from tagplatform.generation.xmlclass import XMLCLASS_VERSION
from tagplatform.library import Directory, TagDef, TagLibrary
from tagplatform.llm.client import LLMResponse
from tagplatform.settings import Settings
from tests.test_xmlclass import FakeNLI, KeywordLLM, TEXT, spec


def library():
    return TagLibrary([Directory('d', '题材')], [TagDef('old', 'd', '汽车追逐')], version='frozen')


class Evaluator:
    def __init__(self, **kwargs):
        self.inputs = []

    def complete(self, messages, run_id):
        payload = json.loads(messages[1]['content'])
        self.inputs.append(payload)
        rows = [dict(id=t['id'], supported=True, concept=t['name'], reason='原文支持',
                     evidence=[dict(scene_id=t['evidence'][0]['scene_id'], quote=TEXT)]) for t in payload['tags']]
        return LLMResponse(json.dumps({'judgments': rows}, ensure_ascii=False), {'total_tokens': 9}, {}, True)


def test_ordered_runner_state_and_common_cap(tmp_path, content_fixture, monkeypatch):
    settings = Settings(mtg_report_dir=str(tmp_path), xmlclass_min_support_documents=2)
    report = api.new_report(settings, library(), XMLCLASS_VERSION, 1)
    report['items'] = [dict(asset_id=a, title=a, content_json=spec(content_fixture, a).content.model_dump_json(), status='queued')
                       for a in ('a', 'b', 'c')]
    monkeypatch.setattr(benchmark, 'LocalNLI', lambda settings: FakeNLI())
    monkeypatch.setattr(benchmark, 'OpenAIClient', Evaluator)
    monkeypatch.setattr(benchmark, 'run_open_generation', lambda *a, **k: pytest.fail('MTG must not be called'))
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, llm=KeywordLLM(), embedder=MockEmbedder()))
    benchmark.run(report, tmp_path, library(), app)
    saved = json.loads((tmp_path / f'{report["id"]}.json').read_text(encoding='utf-8'))
    assert saved['status'] == 'succeeded'
    assert [len(i['tags']) for i in saved['items']] == [0, 1, 1]
    assert [i['discovery']['rescored'] for i in saved['items']] == [False, True, False]
    assert saved['discovery_state']['processed_assets'] == ['a', 'b', 'c']
    public = benchmark.public_report(saved)
    assert 'discovery_state' not in public and 'library' not in public
    assert all('audit' not in i and 'content_json' not in i for i in public['items'])
    assert public['summary']['evaluated'] == 3


def test_evaluation_failure_keeps_generated_vocabulary(tmp_path, content_fixture, monkeypatch):
    class BrokenEvaluator(Evaluator):
        def complete(self, *a, **k):
            return LLMResponse('{}', {'total_tokens': 9}, {}, True)
    settings = Settings(xmlclass_min_support_documents=1)
    report = api.new_report(settings, library(), XMLCLASS_VERSION, 15)
    report['items'] = [dict(asset_id='a', title='a', content_json=spec(content_fixture).content.model_dump_json(), status='queued')]
    monkeypatch.setattr(benchmark, 'LocalNLI', lambda settings: FakeNLI())
    monkeypatch.setattr(benchmark, 'OpenAIClient', BrokenEvaluator)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, llm=KeywordLLM(), embedder=MockEmbedder()))
    benchmark.run(report, tmp_path, library(), app)
    assert report['status'] == 'failed' and report['items'][0]['stage'] == 'evaluating'
    assert len(report['discovery_state']['labels']) == 2
    assert len(report['items'][0]['tags']) == 1
    assert report['items'][0]['evaluation_trace']['usage']['total_tokens'] == 9


def test_rescore_failure_rolls_back_and_next_film_continues(tmp_path, content_fixture, monkeypatch):
    class FailRescore(FakeNLI):
        def score(self, pairs):
            if len(self.calls) == 1:
                self.calls.append(pairs)
                raise RuntimeError('rescore failed')
            return super().score(pairs)
    settings = Settings(xmlclass_min_support_documents=1)
    report = api.new_report(settings, library(), XMLCLASS_VERSION, 15)
    report['items'] = [dict(asset_id=a, title=a, content_json=spec(content_fixture, a).content.model_dump_json(), status='queued')
                       for a in ('a', 'b')]
    monkeypatch.setattr(benchmark, 'LocalNLI', lambda settings: FailRescore())
    monkeypatch.setattr(benchmark, 'OpenAIClient', Evaluator)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, llm=KeywordLLM(), embedder=MockEmbedder()))
    benchmark.run(report, tmp_path, library(), app)
    assert [i['status'] for i in report['items']] == ['failed', 'succeeded']
    assert report['items'][1]['audit']['vocabulary_before'] == 1
    assert report['discovery_state']['processed_assets'] == ['b']
    assert report['status'] == 'failed'


def test_joint_evaluation_hides_source_and_preserves_unique_reasons(content_fixture):
    content = spec(content_fixture).content
    def tag(name):
        return dict(tag_name=name, confidence=.95, origin='catalog', evidence=[dict(scene_id='s0', description=TEXT, start_ms=0, end_ms=99)])
    evaluator = Evaluator()
    result = benchmark.compare(content, [tag('共同'), tag('A独有')], [tag('共同'), tag('B独有')], evaluator, 'blind')
    assert [r['tag_name'] for r in result['a_only']] == ['A独有']
    assert [r['tag_name'] for r in result['b_only']] == ['B独有']
    assert all(r['reason'] for r in result['rows'])
    assert all(set(t) == {'id', 'name', 'evidence'} for t in evaluator.inputs[0]['tags'])


def baseline(client, tmp_path, content_fixture):
    cfg = client.app.state.settings
    cfg.mtg_report_dir = str(tmp_path)
    cfg.use_mock_llm = cfg.use_mock_embedder = False
    report = api.new_report(cfg, library(), 'mtg-v3', 7)
    report['status'] = 'succeeded'
    report['items'] = [dict(asset_id=a, title=a, content_json=spec(content_fixture, a).content.model_dump_json(),
                            status='succeeded', tags=[]) for a in ('old-b', 'old-a')]
    benchmark.save(tmp_path / 'benchmarks', report)
    return report


def test_replay_uses_only_snapshot_not_live_library_or_assets(client, tmp_path, content_fixture, monkeypatch):
    source = baseline(client, tmp_path, content_fixture)
    def forbidden(*args, **kwargs):
        pytest.fail('Replay must not access current assets/library or MTG')
    monkeypatch.setattr(api, 'try_load_library', forbidden)
    monkeypatch.setattr(api, '_resolve_target_asset', forbidden)
    captured = []
    monkeypatch.setattr(api, 'run', lambda *args: captured.append(args))
    response = client.post(f'/api/v1/benchmarks/{source["id"]}/replay-xmlclass')
    assert response.status_code == 200, response.text
    replay, _, frozen, _ = captured[0]
    assert replay['algorithm'] == XMLCLASS_VERSION and replay['max_tags'] == 7
    assert replay['baseline_id'] == source['id']
    assert [i['asset_id'] for i in replay['items']] == ['old-b', 'old-a']
    assert replay['items'][0]['content_json'] == source['items'][0]['content_json']
    assert asdict(frozen.tags()[0]) == asdict(library().tags()[0])
    assert replay['library'] == source['library']
    assert 'baseline_tags' not in response.json()['items'][0]
    assert client.get(f'/api/v1/benchmarks/{source["id"]}').json() == json.loads(json.dumps(source))


@pytest.mark.parametrize('field,value', [('status', 'running'), ('evaluation_version', 'old'), ('embedding_model', 'changed')])
def test_replay_rejects_incomparable_baseline(client, tmp_path, content_fixture, monkeypatch, field, value):
    source = baseline(client, tmp_path, content_fixture)
    source[field] = value
    benchmark.save(tmp_path / 'benchmarks', source)
    monkeypatch.setattr(api, 'run', lambda *a: pytest.fail('Invalid baseline must not run'))
    response = client.post(f'/api/v1/benchmarks/{source["id"]}/replay-xmlclass')
    assert response.status_code == 422


def test_xmlclass_validation_does_not_require_mtg_judge():
    cfg = Settings(mtg_runs=1, mtg_judge_enabled=False, open_library_enabled=False)
    api.validate_settings(cfg, XMLCLASS_VERSION)
    with pytest.raises(Exception) as error:
        api.validate_settings(cfg, 'mtg-v3')
    assert error.value.status_code == 422


def test_public_unknown_usage_and_restart_state(tmp_path):
    report = dict(id='bench_test', status='running', discovery_state={'labels': ['kept']},
                  items=[dict(asset_id='a', status='running', generation_tokens=None)])
    benchmark.save(tmp_path, report)
    benchmark.recover_interrupted(tmp_path)
    saved = json.loads((tmp_path / 'bench_test.json').read_text(encoding='utf-8'))
    assert saved['status'] == 'failed' and saved['discovery_state']['labels'] == ['kept']
    assert benchmark.public_report(saved)['summary']['generation_tokens'] == 0
