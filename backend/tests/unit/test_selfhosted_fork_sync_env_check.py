"""scripts/fork_sync_env_check.py finds upstream switches the fork misses. [fork-only]

The case it exists for: upstream set OMI_SHAPED_AGENT_MODE=on in its servers
and removed the code path for "off". The fork did not set it, so every summary
failed after a sync. Needs no git: report() takes plain dicts.
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts' / 'fork_sync_env_check.py'
spec = importlib.util.spec_from_file_location('fork_sync_env_check', SCRIPT)
env_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env_check)


def _doc(service_env):
    return {'environments': {'prod': {'gke': {s: {'env': env} for s, env in service_env.items()}}}}


def test_a_new_switch_the_fork_does_not_set_is_reported():
    old = env_check.switches(_doc({'backend-listen': {}}))
    new = env_check.switches(
        _doc({'backend-listen': {'OMI_SHAPED_AGENT_MODE': {'value': 'on', 'category': 'rollout'}}})
    )
    rows = env_check.report(old, new, ours={}, default=lambda name: 'off')
    assert [(name, value) for name, value, _ in rows] == [('OMI_SHAPED_AGENT_MODE', 'on')]
    assert 'not set here' in rows[0][2] and env_check.is_switch('OMI_SHAPED_AGENT_MODE', 'on')


def test_quiet_when_the_fork_or_the_code_default_already_matches():
    new = env_check.switches(
        _doc(
            {
                'backend-listen': {
                    'A_MODE': {'value': 'on', 'category': 'rollout'},
                    'B_ENABLED': {'value': 'false', 'category': 'rollout'},
                },
                'pusher': {'A_MODE': {'value': 'on', 'category': 'rollout'}},
            }
        )
    )
    assert new['A_MODE'] == ('on', ['backend-listen', 'pusher'])
    assert env_check.report({}, new, ours={'A_MODE': 'true'}, default=lambda name: '') == []


def test_unchanged_and_non_switch_settings_are_left_out():
    entry = {'X_ENABLED': {'value': 'true', 'category': 'rollout'}}
    same = env_check.switches(_doc({'backend-listen': entry}))
    assert env_check.report(same, same, ours={}) == []
    secrets = _doc({'backend-listen': {'API_KEY': {'secret': 'k', 'category': 'secret'}}})
    assert env_check.switches(secrets) == {}


def test_numbers_and_user_lists_are_tuning():
    assert not env_check.is_switch('X_PERCENT', '5')
    assert not env_check.is_switch('X_UID_ALLOWLIST', 'abc')
    assert env_check.is_switch('X_PERCENT', '100')
    assert env_check.is_switch('X_DIARIZER', 'pyannote_shadow')


def test_fork_values_reads_the_chart_env_list():
    chart = {'env': [{'name': 'A', 'value': 'on'}, {'name': 'B', 'valueFrom': {}}]}
    assert env_check.fork_values(chart) == {'A': 'on'}
