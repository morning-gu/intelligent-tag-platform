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
        rows = [dict(id=t['id'], verdict='keep', concept=t['name'], reason='观众可理解', suggestion=None) for t in payload['tags']]
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
    assert all(set(t) == {'id', 'name'} for t in evaluator.inputs[0]['tags'])


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


def test_custom_prompt_replayed_exactly(client, tmp_path, content_fixture, monkeypatch):
    source = baseline(client, tmp_path, content_fixture)
    source.update(evaluation_prompt='按观众理解程度评估', custom_evaluation_prompt=True,
                  evaluation_format_instruction=benchmark.CUSTOM_PROMPT_OUTPUT_FORMAT)
    benchmark.save(tmp_path / 'benchmarks', source)
    captured = []
    monkeypatch.setattr(api, 'run', lambda *args: captured.append(args))
    response = client.post(f'/api/v1/benchmarks/{source["id"]}/replay-xmlclass')
    assert response.status_code == 200
    replay = captured[0][0]
    for key in ('evaluation_prompt', 'evaluation_format_instruction', 'custom_evaluation_prompt'):
        assert replay[key] == source[key]


def test_viewer_quality_and_comparison_allow_non_keep_without_concept(content_fixture):
    class MixedEvaluator:
        def complete(self, messages, run_id):
            assert messages[0]['content'] == 'custom-format'
            payload = json.loads(messages[1]['content'])
            assert set(payload) == {'film', 'tags'}
            rows = [dict(id=t['id'], verdict=t['name'], reason='展示理由',
                         concept='独立概念' if t['name'] == 'keep' else None,
                         suggestion='可读的改写' if t['name'] == 'rewrite' else None)
                    for t in payload['tags']]
            return LLMResponse(json.dumps({'judgments': rows}), {'total_tokens': 9}, {}, True)
    tags = [dict(tag_name=name) for name in ('keep', 'rewrite', 'omit')]
    result = benchmark.evaluate(content_fixture, tags, MixedEvaluator(), 'eval', prompt='custom', output_format='-format')
    assert result['quality'] == dict(keep=1, rewrite=1, omit=1, distinct_keep=1, display_rate=1/3)
    comparison = benchmark.compare(content_fixture, tags[:1], tags[1:], MixedEvaluator(), 'compare',
                                   prompt='custom', output_format='-format')
    assert len(comparison['rows']) == 3
    assert len(comparison['a_only']) == 1 and not comparison['b_only']


def test_prompt_endpoint_and_report_deletion(client, tmp_path, content_fixture):
    source = baseline(client, tmp_path, content_fixture)
    assert client.get('/api/v1/benchmarks/evaluation-prompt').json()['prompt'] == benchmark.EVALUATION_PROMPT
    path = f'/api/v1/benchmarks/{source["id"]}'
    source['status'] = 'running'
    benchmark.save(tmp_path / 'benchmarks', source)
    assert client.delete(path).status_code == 409
    assert client.get(path).status_code == 200
    source['status'] = 'succeeded'
    benchmark.save(tmp_path / 'benchmarks', source)
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404


def test_legacy_evidence_metrics_remain_readable():
    report = dict(evaluation_version='evidence-concept-v2', items=[dict(status='succeeded', tags=[{}],
                  evaluation=dict(independent_count=1, judgments=[dict(supported=True)]))])
    public = benchmark.public_report(report)
    assert public['summary']['mean_independent_count'] == 1
    assert 'quality' not in public['summary']


def test_create_custom_prompt_for_both_algorithms(client, tmp_path, content_fixture, monkeypatch):
    baseline(client, tmp_path, content_fixture)
    captured = []
    monkeypatch.setattr(api, 'run', lambda *args: captured.append(args))
    monkeypatch.setattr(api, '_resolve_target_asset', lambda *args: SimpleNamespace(
        asset_id='a', title='影片', content_json=content_fixture.model_dump_json()))
    for algorithm in ('mtg-v3', XMLCLASS_VERSION):
        response = client.post('/api/v1/benchmarks', json=dict(asset_ids=['a'], algorithm=algorithm,
                              evaluation_prompt='  自定义观众评测  '))
        assert response.status_code == 200, response.text
        report = captured[-1][0]
        assert report['evaluation_prompt'] == '自定义观众评测'
        assert report['custom_evaluation_prompt']
        assert report['evaluation_format_instruction'] == benchmark.CUSTOM_PROMPT_OUTPUT_FORMAT
    assert client.post('/api/v1/benchmarks', json=dict(asset_ids=['a'], evaluation_prompt=' ')).status_code == 422
