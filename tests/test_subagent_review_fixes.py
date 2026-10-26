import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import subagent_models as sm
DATA = json.loads((REPO / 'runtime/models.json').read_text(encoding='utf-8'))
spec = importlib.util.spec_from_file_location('review_guard', REPO / 'scripts/agent-model-guard.py')
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)


def prepared(tmp_path, models=None, args=None, env=None):
    models = DATA if models is None else models
    path = tmp_path / 'models.json'; path.write_text(json.dumps(models), encoding='utf-8')
    return sm.prepare(sm.build_registry(models), ['deepseek=high'], args or [], models_path=path,
                      registry_path=tmp_path / 'registry.json', config_dir=tmp_path / 'config',
                      session_dir=tmp_path / 'sessions', env=env or {})


@pytest.mark.parametrize('backend', ['fcc', 'fugu', 'gemini', 'unknown'])
def test_fix01_default_backend_is_rejected_before_record(tmp_path, backend):
    models = copy.deepcopy(DATA); models['defaultBackend'] = backend
    with pytest.raises(ValueError):
        prepared(tmp_path, models)
    assert not (tmp_path / 'sessions').exists()


@pytest.mark.parametrize('backend', ['gpt', 'claude', 'nanogpt'])
def test_fix01_supported_default_backend_and_precedence(tmp_path, backend):
    models = copy.deepcopy(DATA); models['defaultBackend'] = backend
    assert prepared(tmp_path, models)['parent']['backend'] == backend
    models['defaultBackend'] = 'fcc'
    assert prepared(tmp_path, models, env={'CLAUDE_CODEX_BACKEND': backend})['parent']['backend'] == backend
    assert prepared(tmp_path, models, ['--backend=' + backend], {'CLAUDE_CODEX_BACKEND': 'fugu'})['parent']['backend'] == backend


@pytest.mark.parametrize('model', [
    'fugu-ultra', 'gemini-3.5-pro', 'fcc-default', 'open_router/model', 'ollama/model', 'unknown-model',
    'anthropic/gateway/fugu/fugu-ultra', 'anthropic/gateway/fcc/default',
    'anthropic/gateway/unknown/gpt-6.1-sol',
])
@pytest.mark.parametrize('inline', [False, True])
def test_fix02_restricted_or_unknown_actual_parent_route(tmp_path, model, inline):
    args = ['--model=' + model] if inline else ['--model', model]
    with pytest.raises(ValueError):
        prepared(tmp_path, args=args)
    assert not (tmp_path / 'sessions').exists()


@pytest.mark.parametrize('model', ['gpt-6-astra', 'claude-sonnet-5-5', 'nanogpt/z-ai/glm-5.3-thinking',
    'anthropic/gateway/codex/gpt-6-astra', 'anthropic/gateway/claude/claude-sonnet-5-5',
    'anthropic/gateway/nanogpt/z-ai/glm-5.3-thinking', 'sonnet'])
def test_fix02_registered_routes_and_family_aliases(tmp_path, model):
    assert prepared(tmp_path, args=['--model', model])['parent']['route'] in {'gpt', 'claude', 'nanogpt'}


@pytest.mark.parametrize('field', ['defaultModel', 'alias'])
def test_fix02_backend_default_or_alias_cannot_hide_restricted_model(tmp_path, field):
    models = copy.deepcopy(DATA)
    if field == 'defaultModel':
        models['backends']['gpt']['defaultModel'] = 'fugu-ultra'; args = []
    else:
        models['backends']['gpt']['aliases']['sonnet'] = 'fugu-ultra'; args = ['--model', 'sonnet']
    with pytest.raises(ValueError):
        prepared(tmp_path, models, args)


def test_fix03_old_haiku_not_an_effort_child():
    assert 'claude-haiku-4-5' in DATA['backends']['claude']['availableModels']
    assert not any(r['model'] == 'claude-haiku-4-5' for r in DATA['subagentModels'])


@pytest.mark.parametrize('model', ['claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-sonnet-5'])
@pytest.mark.parametrize('effort', ['low', 'medium', 'high', 'xhigh', 'max'])
def test_fix04_five_efforts(model, effort):
    assert sm.select(sm.build_registry(DATA), model + '=' + effort, {})['effort'] == effort


def test_fix06_guard_mode_noops_without_explicit_context(monkeypatch):
    monkeypatch.delenv('CLAUDE_CODEX_SUBAGENT_CONTEXT', raising=False)
    assert guard.main(['--ccx-only']) == 0


def test_fix06_hook_is_exec_form_and_fails_closed():
    hook = sm.child_guard_hook()
    assert hook['matcher'] == 'Agent|Task'
    assert hook['hooks'][0]['onFailure'] == 'block'
    assert hook['hooks'][0]['args'] and hook['hooks'][0]['command'].endswith('python.exe')


@pytest.mark.parametrize('inherited', ['fugu-ultra', 'claude-sonnet-5-5'])
def test_fix07_inherited_model_is_ignored_without_cli_model(tmp_path, inherited):
    result = prepared(tmp_path, env={'ANTHROPIC_MODEL': inherited})
    expected = {'backend': 'gpt', 'model': DATA['backends']['gpt']['defaultModel'], 'route': 'gpt'}
    assert result['parent'] == expected
    record = json.loads(Path(result['sessionPath']).read_text(encoding='utf-8'))
    assert record['parent'] == expected


@pytest.mark.parametrize('inherited', ['fugu-ultra', 'claude-sonnet-5-5'])
@pytest.mark.parametrize('explicit,route', [('gpt-6-astra', 'gpt'), ('claude-haiku-5-5', 'claude')])
@pytest.mark.parametrize('inline', [False, True])
def test_fix07_explicit_cli_model_has_priority(tmp_path, inherited, explicit, route, inline):
    args = ['--model=' + explicit] if inline else ['--model', explicit]
    result = prepared(tmp_path, args=args, env={'ANTHROPIC_MODEL': inherited})
    assert result['parent'] == {'backend': 'gpt', 'model': explicit, 'route': route}


@pytest.mark.parametrize('inherited', ['gpt-6-astra', 'claude-sonnet-5-5'])
@pytest.mark.parametrize('inline', [False, True])
def test_fix07_explicit_restricted_cli_model_remains_rejected(tmp_path, inherited, inline):
    args = ['--model=fugu-ultra'] if inline else ['--model', 'fugu-ultra']
    with pytest.raises(ValueError):
        prepared(tmp_path, args=args, env={'ANTHROPIC_MODEL': inherited})
    assert not (tmp_path / 'sessions').exists()
