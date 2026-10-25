"""Unit tests for verdict_provenance() — the UI-safe safety provenance block.

The badge in the chat timeline is only as accurate as this function, so the
mapping from a pipeline verdict to ``{source, mode, decision}`` is pinned here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.tools.lib.safety_pipeline import verdict_provenance


def _dmss(decision, level, confidence=0.91, event_id='ev-1'):
    return {
        'level': level,
        'score': 15 if level == 'dangerous' else 0,
        'reasons': ['Decim Safety decided.'],
        'blocked_patterns': ['decim_block'] if level == 'dangerous' else [],
        'decision_source': 'decim',
        'decim_safety': {
            'decision_source': 'decim',
            'model_decision': decision,
            'model_confidence': confidence,
            'event_id': event_id,
            'provider_key': 'secret-provider',
            'model_id': 'internal-product-name',
        },
    }


def test_dmss_allow_is_attributed_to_dmss():
    prov = verdict_provenance(_dmss('allow', 'safe'))
    assert prov['source'] == 'dmss'
    assert prov['mode'] == 'enforce'
    assert prov['decision'] == 'allow'
    assert prov['level'] == 'safe'
    assert prov['model_decision'] == 'allow'
    assert prov['confidence'] == 0.91
    assert prov['event_id'] == 'ev-1'


def test_dmss_block_maps_to_block_decision():
    prov = verdict_provenance(_dmss('block', 'dangerous'))
    assert prov['source'] == 'dmss'
    assert prov['decision'] == 'block'
    assert prov['level'] == 'dangerous'


def test_dmss_review_maps_to_requires_approval():
    prov = verdict_provenance(_dmss('review', 'requires_approval'))
    assert prov['decision'] == 'review'
    assert prov['level'] == 'requires_approval'


def test_shadow_mode_is_attributed_to_rules_with_shadow_marker():
    prov = verdict_provenance({
        'level': 'safe', 'score': 0, 'reasons': [], 'blocked_patterns': [],
        'decision_source': 'deterministic_shadow',
        'decim_safety': {'decision_source': 'deterministic_shadow',
                         'model_decision': 'block', 'model_confidence': 0.5},
    })
    assert prov['source'] == 'rules'
    assert prov['mode'] == 'shadow'
    assert prov['decision'] == 'allow'
    assert prov['model_decision'] == 'block'


def test_deterministic_fallback_is_attributed_to_rules():
    prov = verdict_provenance({
        'level': 'requires_approval', 'score': 9,
        'reasons': ['rm -rf detected'], 'blocked_patterns': ['file_destruction'],
        'decision_source': 'deterministic_fallback',
        'decim_safety': {'decision_source': 'deterministic_fallback',
                         'fallback_reason': 'provider_error'},
    })
    assert prov['source'] == 'rules'
    assert prov['mode'] == 'fallback'
    assert prov['decision'] == 'review'


def test_plain_rules_verdict_has_no_mode():
    prov = verdict_provenance({
        'level': 'safe', 'score': 0, 'reasons': [], 'blocked_patterns': [],
    })
    assert prov == {
        'source': 'rules', 'mode': None, 'level': 'safe', 'decision': 'allow',
        'model_decision': None, 'confidence': None, 'event_id': None,
        'blocked_patterns': [], 'reasons': [],
    }


def test_never_leaks_provider_or_model_identity():
    prov = verdict_provenance(_dmss('allow', 'safe'))
    blob = repr(prov)
    assert 'secret-provider' not in blob
    assert 'internal-product-name' not in blob
    for key in ('provider_key', 'model_id', 'provider_endpoint', 'api_key'):
        assert key not in prov


def test_patterns_and_reasons_are_capped():
    prov = verdict_provenance({
        'level': 'dangerous', 'score': 20,
        'reasons': ['r%d' % i for i in range(10)],
        'blocked_patterns': ['p%d' % i for i in range(10)],
    })
    assert len(prov['blocked_patterns']) == 6
    assert len(prov['reasons']) == 4


def test_non_dict_input_is_safe():
    assert verdict_provenance(None) == {}
    assert verdict_provenance('nope') == {}
