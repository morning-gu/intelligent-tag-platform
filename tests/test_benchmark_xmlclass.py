import json
import sqlite3
from contextlib import closing, nullcontext
from types import SimpleNamespace

import pytest

from tagplatform import benchmark
from tagplatform.api.routers import benchmarks as api
from tagplatform.embed.embedding import MockEmbedder
from tagplatform.generation.base import ProducedTag
from tagplatform.generation.mtg.prompts import render_scene_lines
from tagplatform.generation.xmlclass import XMLCLASS_VERSION
from tagplatform.library import Directory, TagDef, TagLibrary
from tagplatform.llm.client import LLMResponse
from tagplatform.settings import Settings
from tests.test_xmlclass import FakeNLI, KeywordLLM, spec


def library():
    return TagLibrary([Directory('d', '题材')], [TagDef('old', 'd', '汽车追逐')], version='frozen')


class Evaluator:
    def __init__(self, **kwargs):
        pass

    def complete(self, messages, run_id):
        payload = json.loads(messages[1]['content'])
        rows = [dict(id=t['id'], verdict='keep', concept=t['name'], reason='观众可理解', suggestion=None) for t in payload['tags']]
        return LLMResponse(json.dumps({'judgments': rows}, ensure_ascii=False), {'total_tokens': 9}, {}, True)


@pytest.mark.parametrize('algorithm', ['mtg-v3', XMLCLASS_VERSION])
def test_final_dedup_runs_before_cap_for_both_algorithms(tmp_path, content_fixture, monkeypatch, algorithm):
    settings = Settings(mtg_report_dir=str(tmp_path), mtg_merge_threshold=-1.0)
    report = api.new_report(settings, library(), algorithm, 2)
    report['items'] = [dict(asset_id='a', title='影片', status='queued',
                            content_json=content_fixture.model_dump_json())]
    tags = [ProducedTag('catalog', str(i), name, None, None, .9, .9, [])
            for i, name in enumerate(('悬疑', '悬疑感', '偷窥式叙事'))]

    def mtg_generation(spec, *args, **kwargs):
        (tmp_path / f'{spec.run_id}.json').write_text('{}', encoding='utf-8')
        return tags

    def xmlclass_generation(spec, state, llm, embedder, nli, settings, audit, cache):
        audit.update(vocabulary_before=1, vocabulary_after=1, candidates_after=0, added_labels=[],
                     low_coverage_scenes=[], rescored=False, nli_pairs=0, rejected_keywords=[])
        return tags, state

    class DedupLLM:
        def complete(self, messages, run_id):
            assert run_id.endswith('-dedup')
            pairs = json.loads(messages[1]['content'])
            assert len(pairs) == 3
            decisions = [dict(id=pair['id'], same=pair['tags'] == ['悬疑', '悬疑感']) for pair in pairs]
            return LLMResponse(json.dumps({'results': decisions}), {'total_tokens': 11}, {}, True)

    monkeypatch.setattr(benchmark, 'run_open_generation', mtg_generation)
    monkeypatch.setattr(benchmark, 'run_generation', xmlclass_generation)
    monkeypatch.setattr(benchmark, 'OpenAIClient', Evaluator)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, llm=DedupLLM(), embedder=MockEmbedder(),
                                                session_factory=nullcontext))
    benchmark.run(report, tmp_path, library(), app)
    item = report['items'][0]
    assert report['status'] == 'succeeded', item.get('error')
    assert item['raw_tag_count'] == 3
    assert [tag['tag_name'] for tag in item['tags']] == ['悬疑', '偷窥式叙事']
    assert item['audit']['final_dedup']['dropped'] == [{'tag': '悬疑感', 'kept': '悬疑'}]
    assert item['generation_tokens'] == 11


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
    assert [len(i['tags']) for i in saved['items']] == [1, 1, 1]
    assert [i['discovery']['rescored'] for i in saved['items']] == [True, True, False]
    first = saved['items'][0]
    assert {'recall_s', 'nli_initial_s', 'materials_s', 'llm_keywords_s', 'nli_rescore_s',
            'dedup_s', 'other_s'} <= first['generation_timings_s'].keys()
    assert sum(first['generation_timings_s'].values()) == pytest.approx(first['generation_elapsed_s'], abs=0.05)
    assert 'nli_rescore_s' not in saved['items'][2]['generation_timings_s']
    assert 'llm_keywords_s' not in saved['items'][2]['generation_timings_s']
    assert saved['discovery_state']['processed_assets'] == ['a', 'b', 'c']
    public = benchmark.public_report(saved)
    assert 'discovery_state' not in public and 'library' not in public
    assert all('audit' not in i and 'content_json' not in i for i in public['items'])
    assert public['items'][0]['generation_timings_s'] == first['generation_timings_s']
    assert public['summary']['evaluated'] == 3


def test_new_jobs_restore_pending_candidates_and_shared_labels(tmp_path, content_fixture, monkeypatch):
    settings = Settings(xmlclass_recall_top_k=1)
    monkeypatch.setattr(benchmark, 'LocalNLI', lambda settings: FakeNLI())
    monkeypatch.setattr(benchmark, 'OpenAIClient', Evaluator)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, llm=KeywordLLM(), embedder=MockEmbedder()))

    def run_film(asset_id, llm):
        app.state.llm = llm
        report = api.new_report(settings, library(), XMLCLASS_VERSION, 15)
        report['items'] = [dict(asset_id=asset_id, title=asset_id, status='queued',
                                content_json=spec(content_fixture, asset_id).content.model_dump_json())]
        benchmark.run(report, tmp_path, library(), app)
        assert report['status'] == 'succeeded', report['items'][0].get('error')
        return report, report['items'][0]

    first, item = run_film('a', KeywordLLM(same=False))
    assert [tag['tag_name'] for tag in item['tags']] == ['候鸟迁徙']
    assert item['audit']['added_labels'] == []

    _, unrelated = run_film('b', KeywordLLM(term='观鸟记录', same=False))
    assert unrelated['audit']['candidates_before'] == 1
    assert unrelated['tags'] == []
    assert all(entry['name'] != '候鸟迁徙' for row in unrelated['audit']['final_scores'] for entry in row['scores'])

    repeated_report, repeated = run_film('a', KeywordLLM(same=False))
    assert repeated['audit']['added_labels'] == []

    promoted_report, promoted = run_film('c', KeywordLLM(same=False))
    assert len(promoted['audit']['added_labels']) == 1
    assert {row['asset_id'] for row in promoted['audit']['admissions'][0]['occurrences']} == {'a', 'c'}

    next_report, next_item = run_film('d', KeywordLLM(term='观鸟记录', same=False))
    assert next_item['audit']['vocabulary_before'] == 2
    assert '候鸟迁徙' in {label['name'] for label in next_report['discovery_state']['labels']}

    assert benchmark.delete_finished_report(tmp_path / f'{promoted_report["id"]}.json')
    demoted, _ = run_film('e', KeywordLLM(term='观鸟记录', same=False))
    assert '候鸟迁徙' not in {label['name'] for label in demoted['discovery_state']['labels']}
    candidates = demoted['discovery_state']['candidates']
    migration, = [candidate for candidate in candidates if candidate['label']['name'] == '候鸟迁徙']
    assert {row['asset_id'] for row in migration['occurrences']} == {'a'}

    for deleted in (first, repeated_report):
        assert benchmark.delete_finished_report(tmp_path / f'{deleted["id"]}.json')
    without_source, _ = run_film('f', KeywordLLM(term='观鸟记录', same=False))
    assert '候鸟迁徙' not in {candidate['label']['name'] for candidate in without_source['discovery_state']['candidates']}


def test_first_new_job_inherits_candidates_from_old_report(tmp_path, content_fixture, monkeypatch):
    settings = Settings()
    catalog = library()
    with closing(sqlite3.connect(tmp_path / 'xmlclass-state.sqlite3')) as connection, connection:
        connection.execute('CREATE TABLE discovery (id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL)')
        connection.execute("INSERT INTO discovery VALUES (1, 'stale state from deleted reports')")
    old = api.new_report(settings, catalog, XMLCLASS_VERSION, 15)
    occurrence = dict(asset_id='a', scene_id='s0', raw_term='候鸟迁徙', quote='历史引文')
    old.update(status='succeeded', discovery_state={
        'labels': [],
        'candidates': [{'label': {'key': 'old-candidate', 'name': '候鸟迁徙'},
                        'occurrences': [occurrence]}],
        'processed_assets': ['a'],
    })
    old['items'] = [dict(asset_id='a', status='succeeded', stage='completed',
                         audit={'keywords': [dict(occurrence, candidate='old-candidate')]})]
    benchmark.save(tmp_path, old)
    report = api.new_report(settings, catalog, XMLCLASS_VERSION, 15)
    report['items'] = [dict(asset_id='b', title='b', status='queued',
                            content_json=spec(content_fixture, 'b').content.model_dump_json())]
    monkeypatch.setattr(benchmark, 'LocalNLI', lambda settings: FakeNLI())
    monkeypatch.setattr(benchmark, 'OpenAIClient', Evaluator)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, llm=KeywordLLM(), embedder=MockEmbedder()))
    benchmark.run(report, tmp_path, catalog, app)
    assert report['status'] == 'succeeded', report['items'][0].get('error')
    assert report['items'][0]['audit']['added_labels'] == ['old-candidate']
    assert report['discovery_state']['processed_assets'] == ['a', 'b']


def test_evaluation_failure_keeps_generated_vocabulary(tmp_path, content_fixture, monkeypatch):
    class BrokenEvaluator(Evaluator):
        def complete(self, *a, **k):
            return LLMResponse('{}', {'total_tokens': 9}, {}, True)
    settings = Settings()
    report = api.new_report(settings, library(), XMLCLASS_VERSION, 15)
    report['items'] = [dict(asset_id='a', title='a', content_json=spec(content_fixture).content.model_dump_json(), status='queued')]
    monkeypatch.setattr(benchmark, 'LocalNLI', lambda settings: FakeNLI())
    monkeypatch.setattr(benchmark, 'OpenAIClient', BrokenEvaluator)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, llm=KeywordLLM(), embedder=MockEmbedder()))
    benchmark.run(report, tmp_path, library(), app)
    assert report['status'] == 'failed' and report['items'][0]['stage'] == 'evaluating'
    assert len(report['discovery_state']['candidates']) == 1
    assert len(report['items'][0]['tags']) == 1
    assert report['items'][0]['evaluation_trace']['usage']['total_tokens'] == 9


def test_rescore_failure_rolls_back_and_next_film_continues(tmp_path, content_fixture, monkeypatch):
    class FailRescore(FakeNLI):
        def score(self, pairs):
            if len(self.calls) == 1:
                self.calls.append(pairs)
                raise RuntimeError('rescore failed')
            return super().score(pairs)
    settings = Settings()
    report = api.new_report(settings, library(), XMLCLASS_VERSION, 15)
    report['items'] = [dict(asset_id=a, title=a, content_json=spec(content_fixture, a).content.model_dump_json(), status='queued')
                       for a in ('a', 'b')]
    monkeypatch.setattr(benchmark, 'LocalNLI', lambda settings: FailRescore())
    monkeypatch.setattr(benchmark, 'OpenAIClient', Evaluator)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, llm=KeywordLLM(), embedder=MockEmbedder()))
    benchmark.run(report, tmp_path, library(), app)
    assert [i['status'] for i in report['items']] == ['failed', 'succeeded']
    assert 'nli_rescore_s' in report['items'][0]['generation_timings_s']
    assert report['items'][1]['audit']['vocabulary_before'] == 1
    assert report['discovery_state']['processed_assets'] == ['b']
    assert report['status'] == 'failed'


def saved_report(client, tmp_path, content_fixture):
    cfg = client.app.state.settings
    cfg.mtg_report_dir = str(tmp_path)
    cfg.use_mock_llm = cfg.use_mock_embedder = False
    report = api.new_report(cfg, library(), 'mtg-v3', 7)
    report['status'] = 'succeeded'
    report['items'] = [dict(asset_id=a, title=a, content_json=spec(content_fixture, a).content.model_dump_json(),
                            status='succeeded', tags=[]) for a in ('old-b', 'old-a')]
    benchmark.save(tmp_path / 'benchmarks', report)
    return report


def test_removed_replay_endpoint_returns_not_found(client, tmp_path, content_fixture):
    report = saved_report(client, tmp_path, content_fixture)
    directory = tmp_path / 'benchmarks'
    response = client.post(f'/api/v1/benchmarks/{report["id"]}/replay-xmlclass')
    assert response.status_code == 404
    assert [path.stem for path in directory.glob('*.json')] == [report['id']]


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


def test_viewer_quality_allows_non_keep_without_concept(content_fixture):
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


def test_prompt_endpoint_and_report_deletion(client, tmp_path, content_fixture):
    source = saved_report(client, tmp_path, content_fixture)
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


def test_existing_reports_show_prompt_scene_descriptions(client, tmp_path, content_fixture):
    report = saved_report(client, tmp_path, content_fixture)
    content = spec(content_fixture).content
    scenes = content.content.scenes
    scenes[0].description = '短场景描述。'
    scenes[1].description = '场景长描述。' * 45
    report['items'][0].update(content_json=content.model_dump_json(), tags=[{
        'tag_name': '测试标签',
        'evidence': [{'scene_id': s.scene_id, 'description': s.description[:120] + '…'} for s in scenes],
    }])
    benchmark.save(tmp_path / 'benchmarks', report)
    expected = [render_scene_lines(s)[0].split('] ', 1)[1] for s in scenes]
    assert expected == [scenes[0].description, scenes[1].description[:240]]

    listing = client.get('/api/v1/benchmarks').json()[0]
    detail = client.get(f'/api/v1/benchmarks/{report["id"]}').json()
    for result in (listing, detail):
        evidence = result['items'][0]['tags'][0]['evidence']
        assert [row['description'] for row in evidence] == expected


def test_listing_includes_only_current_standalone_reports(client, tmp_path, content_fixture):
    report = saved_report(client, tmp_path, content_fixture)
    root = tmp_path / 'benchmarks'
    old = dict(report, id='bench_' + '1' * 32, evaluation_version='evidence-concept-v2')
    replay = dict(report, id='bench_' + '2' * 32, baseline_id=report['id'])
    benchmark.save(root, old)
    benchmark.save(root, replay)

    response = client.get('/api/v1/benchmarks')
    assert response.status_code == 200
    assert [row['id'] for row in response.json()] == [report['id']]
    assert (root / f'{old["id"]}.json').exists()
    assert (root / f'{replay["id"]}.json').exists()


def test_create_custom_prompt_for_both_algorithms(client, tmp_path, content_fixture, monkeypatch):
    saved_report(client, tmp_path, content_fixture)
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
