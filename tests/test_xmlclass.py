"""Online discovery invariants; no network, model download, or production writes."""
import json
from dataclasses import asdict

import pytest

from tagplatform.embed.embedding import MockEmbedder
from tagplatform.generation.base import GenerationPolicy, GenerationSpec
from tagplatform.generation.mtg import pipeline as mtg
from tagplatform.generation.mtg.prompts import build_generation_messages
from tagplatform.generation.xmlclass import timing
from tagplatform.generation.xmlclass.nli import entailment_scores
from tagplatform.generation.xmlclass.pipeline import (
    Candidate, DiscoveryState, Label, llm_tokens, rank_vote, run_generation,
)
from tagplatform.library import Directory, TagDef, TagLibrary
from tagplatform.llm.client import LLMResponse
from tagplatform.schemas.content import Scene, TimeRange
from tagplatform.settings import Settings

TEXT = '两人在河岸认真观察候鸟迁徙，并记录鸟群飞行路线。'


class KeywordLLM:
    def __init__(self, term='候鸟迁徙', same=True):
        self.term, self.same = term, same
        self.calls = []

    def complete(self, messages, run_id):
        payload = request_payload(messages)
        self.calls.append(payload)
        data = ({'keywords': [{'term': self.term, 'description': '鸟类季节性迁徙', 'dir_id': 'd', 'quote': TEXT}]}
                if 'scene_id' in payload else {'same': self.same, 'reason': '两个名称表达同一迁徙概念' if self.same else '不同概念'})
        return LLMResponse(json.dumps(data, ensure_ascii=False), {'total_tokens': 10}, {}, True)


def request_payload(messages):
    content = messages[1]['content']
    return json.loads(content[-1]['text'] if isinstance(content, list) else content)


class FakeNLI:
    def __init__(self, supported=('候鸟迁徙',)):
        self.supported = supported
        self.calls = []

    def score(self, pairs):
        self.calls.append(pairs)
        return [0.95 if any(term in h for term in self.supported) else 0.1 for _, h in pairs]


def spec(content_fixture, asset_id='movie1', scenes=2):
    content = content_fixture.model_copy(deep=True)
    content.content.scenes = [Scene(scene_id=f's{i}', time_range=TimeRange(start_ms=i*100, end_ms=i*100+99),
                                    description=TEXT) for i in range(scenes)]
    library = TagLibrary([Directory('d', '题材', dir_type='dir_dynamic')], [])
    return GenerationSpec(asset_id, asset_id, content, GenerationPolicy(), '', asset_id, library)


def settings(**kwargs):
    return Settings(**kwargs)


def test_pair_scores_independent_of_candidate_set_and_label_order():
    mapping = {'contradiction': 0, 'neutral': 1, 'entailment': 2}
    single = entailment_scores([[0, 1, 3]], mapping)[0]
    batch = entailment_scores([[0, 1, 3], [1000, 1001, 1002]], mapping)
    assert batch[0] == single
    assert 0 < batch[1] < 1
    binary = entailment_scores([[3, 1]], {"entailment": 0, "not_entailment": 1})
    assert binary == pytest.approx([0.880797])
    assert entailment_scores([[3, 1, 0]], {'entailment': 0, 'neutral': 1, 'contradiction': 2})[0] == single
    with pytest.raises(ValueError, match='entailment'):
        entailment_scores([[1, 2, 3]], {'LABEL_0': 0})
    with pytest.raises(ValueError):
        entailment_scores([[float('nan'), 1, 3]], mapping)


def test_distinct_films_accumulate_promote_rescore_and_keep_evidence(content_fixture):
    initial = DiscoveryState([Label('old', '汽车追逐', tag_id='old')])
    nli = FakeNLI()
    audit1 = {}
    tags1, first = run_generation(spec(content_fixture), initial, KeywordLLM(), MockEmbedder(), nli, settings(), audit1)
    assert [tag.tag_name for tag in tags1] == ['候鸟迁徙']
    assert len(initial.candidates) == 0  # input state is never mutated
    assert first.candidates[0].support == 1  # two scenes, one film
    assert len(first.candidates[0].occurrences) == 2
    assert len(first.labels) == 1 and audit1['added_labels'] == []
    assert audit1['local_labels'] == [first.candidates[0].label.key]
    assert audit1['rescored'] and len(nli.calls) == 2
    assert audit1['nli_pairs'] == 2 and audit1['nli_reused_pairs'] == 4
    assert set(nli.calls[0]).isdisjoint(nli.calls[1])
    # Reprocessing the same film must not inflate support.
    _, repeated = run_generation(spec(content_fixture), first, KeywordLLM(), MockEmbedder(), nli, settings())
    assert repeated.candidates[0].support == 1
    audit2 = {}
    tags2, second = run_generation(spec(content_fixture, 'movie2'), repeated, KeywordLLM(), MockEmbedder(), nli, settings(), audit2)
    assert [t.tag_name for t in tags2] == ['候鸟迁徙']
    assert len(second.labels) == 2 and not second.candidates
    assert audit2['rescored'] and len(audit2['added_labels']) == 1
    assert len(audit2['admissions'][0]['occurrences']) == 4
    assert tags2[0].origin == 'generated' and tags2[0].tag_id is None
    assert [(e.scene_id, e.start_ms, e.end_ms) for e in tags2[0].evidence] == [('s0', 0, 99), ('s1', 100, 199)]
    assert audit2['initial_scores'][0]['max_support'] == 0.1
    assert audit2['final_scores'][0]['max_support'] == 0.95
    # A third film starts from the admitted vocabulary and needs no extraction.
    llm = KeywordLLM()
    third_audit = {}
    run_generation(spec(content_fixture, 'movie3'), second, llm, MockEmbedder(), nli, settings(), third_audit)
    assert not llm.calls and not third_audit['rescored']


def test_first_nli_call_separates_model_loading_from_scoring(content_fixture, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(timing, 'monotonic', lambda: clock[0])

    class TimedNLI(FakeNLI):
        model_load_elapsed_s = 0.0

        def score(self, pairs):
            if not self.calls:
                self.model_load_elapsed_s = 3.0
                clock[0] += self.model_load_elapsed_s
            clock[0] += 5.0
            return super().score(pairs)

    audit = {}
    run_generation(spec(content_fixture), DiscoveryState([Label('old', '汽车追逐', tag_id='old')]),
                   KeywordLLM(), MockEmbedder(), TimedNLI(), settings(), audit)
    assert audit['timings_s']['nli_model_load_s'] == 3.0
    assert audit['timings_s']['nli_initial_s'] == 5.0
    assert audit['timings_s']['nli_rescore_s'] == 5.0


def test_candidate_synonyms_combine_document_support_not_scene_count(content_fixture):
    initial = DiscoveryState([Label('old', '汽车追逐', tag_id='old')], [Candidate(Label('candidate', '鸟类迁徙', dir_id='d'),
                                  [{'asset_id': 'movie0', 'scene_id': 's0', 'raw_term': '鸟类迁徙', 'quote': TEXT}])])
    # Force the representative into shortlist; the semantic decision still controls merging.
    cfg = settings(xmlclass_synonym_cosine=-1)
    class Synonyms(KeywordLLM):
        def complete(self, messages, run_id):
            payload = request_payload(messages)
            self.same = payload.get('representative', {}).get('name') == '鸟类迁徙'
            return super().complete(messages, run_id)
    audit = {}
    _, result = run_generation(spec(content_fixture), initial, Synonyms(), MockEmbedder(), FakeNLI(), cfg, audit)
    assert [l.name for l in result.labels] == ['汽车追逐', '鸟类迁徙']
    assert len(audit['admissions']) == 1
    assert {o['raw_term'] for o in audit['admissions'][0]['occurrences']} == {'鸟类迁徙', '候鸟迁徙'}


def test_similarity_without_synonym_confirmation_does_not_merge(content_fixture):
    state = DiscoveryState([Label('old', '汽车追逐', tag_id='old')])
    _, result = run_generation(spec(content_fixture), state, KeywordLLM(same=False), MockEmbedder(), FakeNLI(),
                               settings(xmlclass_synonym_cosine=-1))
    assert result.candidates[0].label.name == '候鸟迁徙'
    assert len(result.labels) == 1


def test_catalog_alias_does_not_enter_pool(content_fixture):
    state = DiscoveryState([Label('known', '鸟类迁徙', tag_id='tag1', aliases=['候鸟迁徙'])])
    _, result = run_generation(spec(content_fixture), state, KeywordLLM(), MockEmbedder(), FakeNLI(), settings())
    assert not result.candidates


def test_low_coverage_budget_skips_empty_content_and_orders_by_support(content_fixture):
    request = spec(content_fixture, scenes=3)
    request.content.content.scenes[0].description = ''
    request.content.content.scenes[2].description += '远处还有一片湿地。'
    class Scores(FakeNLI):
        def score(self, pairs):
            if len(pairs) == 2:
                return [0.4, 0.1]
            return super().score(pairs)
    audit = {}
    llm = KeywordLLM()
    run_generation(request, DiscoveryState([Label('old', '汽车追逐', tag_id='old')]), llm, MockEmbedder(), Scores(),
                   settings(xmlclass_max_discovery_scenes=1), audit)
    assert audit['low_coverage_scenes'] == ['s2']
    assert len(llm.calls) == 1


def test_discovery_receives_same_multimodal_materials_as_mtg(content_fixture, tmp_path, monkeypatch):
    request = spec(content_fixture)
    request.library = TagLibrary(
        [Directory('d', '题材', dir_type='dir_dynamic')],
        [TagDef('old', 'd', '汽车追逐')],
    )
    frame = tmp_path / 'frame.jpg'
    frame.write_bytes(b'frame')
    monkeypatch.setattr(mtg.keyframes_mod, 'plan_scene_frames', lambda *a, **kw: {'s1': (frame,)})
    embedder = MockEmbedder()
    cfg = settings(xmlclass_max_discovery_scenes=1)
    frames, _, _, hints, dirs = mtg.prepare_generation_context(request, embedder, cfg)
    expected = build_generation_messages(request.content, frames, library_hint=hints, dir_hint=dirs)

    class CheckMaterials(KeywordLLM):
        def complete(self, messages, run_id):
            assert messages[1]['content'][:-1] == expected[0]['content'][:-1]
            assert request_payload(messages) == {'scene_id': 's0', 'max_keywords': 3}
            return super().complete(messages, run_id)

    llm = CheckMaterials()
    run_generation(request, DiscoveryState.from_library(request.library), llm, embedder, FakeNLI(), cfg)
    assert hints and dirs and len(llm.calls) == 1


def test_failure_does_not_commit_partial_candidates_and_retains_usage(content_fixture):
    class BrokenLLM(KeywordLLM):
        def complete(self, messages, run_id):
            if self.calls:
                return LLMResponse('{broken', {'total_tokens': 7}, {}, True)
            return super().complete(messages, run_id)
    state = DiscoveryState([])
    before = asdict(state)
    audit = {}
    with pytest.raises(ValueError):
        run_generation(spec(content_fixture), state, BrokenLLM(), MockEmbedder(), FakeNLI(), settings(), audit)
    assert asdict(state) == before
    assert len(audit['calls']) == 2 and llm_tokens(audit) == 17


def test_reject_fabricated_keyword_quote(content_fixture):
    class MixedQuotes(KeywordLLM):
        def complete(self, messages, run_id):
            return LLMResponse(json.dumps({'keywords': [
                {'term': '虚构概念', 'description': '虚构的概念', 'dir_id': 'd', 'quote': '影片中没有出现的台词。'},
                {'term': '候鸟迁徙', 'description': '鸟类季节性迁徙', 'dir_id': 'd', 'quote': TEXT},
            ]}), {'total_tokens': 10}, {}, True)

    request = spec(content_fixture, scenes=1)
    request.content.content.scenes[0].description = TEXT * 10
    nli = FakeNLI()
    audit = {}
    tags, result = run_generation(request, DiscoveryState([]), MixedQuotes(), MockEmbedder(), nli,
                                  settings(xmlclass_min_support_documents=1), audit)
    assert [label.name for label in result.labels] == ['候鸟迁徙']
    assert [k['raw_term'] for k in audit['rejected_keywords']] == ['虚构概念']
    assert audit['rejected_keywords'][0]['reason'] == 'quote_not_in_scene'
    assert nli.calls[0][0][0] == TEXT * 10
    assert tags[0].evidence[0].description == (TEXT * 10)[:120] + '…'


def test_rank_columns_vote_and_filter_unsupported():
    def row(entries):
        return {'scores': [dict(key=k, support=s) for k, s in entries]}
    rows = [row([('a', .9), ('b', .85), ('c', .1)]),
            row([('b', .95), ('a', .8)]), row([('a', .95), ('b', .9)])]
    assert rank_vote(rows, .7) == ['a', 'b']
    assert rank_vote([row([('a', .1)])], .7) == []
    assert rank_vote([], .7) == []


def test_empty_scene_and_library_and_invalid_nli(content_fixture):
    tags, result = run_generation(spec(content_fixture, scenes=0), DiscoveryState([]), KeywordLLM(), MockEmbedder(), FakeNLI(), settings())
    assert tags == [] and result.processed_assets == ['movie1']
    class BadNLI:
        def score(self, pairs):
            return [float('nan')] * len(pairs)
    with pytest.raises(ValueError, match='NLI'):
        run_generation(spec(content_fixture), DiscoveryState([Label('old', '汽车追逐', tag_id='old')]), KeywordLLM(), MockEmbedder(), BadNLI(), settings())


def test_initial_vocabulary_uses_active_tags_and_original_ids():
    library = TagLibrary([Directory('d', '题材')], [TagDef('t', 'd', '迁徙'), TagDef('gone', 'd', '停用', status='deprecated')])
    state = DiscoveryState.from_library(library)
    assert [l.tag_id for l in state.labels] == ['t']
    assert state.labels[0].dir_id == 'd'


def test_usage_unknown_is_not_zero():
    assert llm_tokens({'calls': []}) == 0
    assert llm_tokens({'calls': [{'usage': {}}]}) is None


def test_vocabulary_embeddings_reused_within_batch(content_fixture):
    class RecordingEmbedder(MockEmbedder):
        def __init__(self):
            self.calls = []
        def embed(self, texts, *, role):
            self.calls.append((texts, role))
            return super().embed(texts, role=role)
    embedder = RecordingEmbedder()
    cache = {}
    state = DiscoveryState([Label('known', '候鸟迁徙', tag_id='known')])
    for asset_id in ('first', 'second'):
        _, state = run_generation(spec(content_fixture, asset_id), state, KeywordLLM(), embedder,
                                  FakeNLI(), settings(), vector_cache=cache)
    assert len(embedder.calls) == 2  # label passages and scene queries, once each
    assert embedder.calls[0] == (['候鸟迁徙'], 'passage')



@pytest.mark.parametrize('dir_id', ['static', 'unknown', None])
def test_open_discovery_rejects_disallowed_directories(content_fixture, dir_id):
    request = spec(content_fixture, scenes=1)
    request.library = TagLibrary(
        [Directory('d', '题材', dir_type='dir_dynamic'), Directory('static', '固定', dir_type='dir_static')],
        [],
    )

    class CandidateLLM:
        def complete(self, messages, run_id):
            data = {'keywords': [{'term': '候鸟迁徙', 'description': '鸟类迁徙',
                                  'dir_id': dir_id, 'quote': TEXT}]}
            return LLMResponse(json.dumps(data), {'total_tokens': 1}, {}, True)

    audit = {}
    tags, state = run_generation(request, DiscoveryState([]), CandidateLLM(),
                                 MockEmbedder(), FakeNLI(), settings(), audit)
    assert tags == [] and not state.candidates
    assert audit['rejected_keywords'][0]['reason'] == 'directory_not_allowed'


def test_static_only_still_matches_existing_tags_without_discovery(content_fixture):
    request = spec(content_fixture)
    request.library = TagLibrary(
        [Directory('static', '固定', dir_type='dir_static')],
        [TagDef('match', 'static', '候鸟迁徙')],
    )
    llm = KeywordLLM()
    tags, _ = run_generation(request, DiscoveryState.from_library(request.library), llm,
                             MockEmbedder(), FakeNLI(), settings())
    assert [(tag.origin, tag.tag_id) for tag in tags] == [('catalog', 'match')]
    assert not llm.calls
