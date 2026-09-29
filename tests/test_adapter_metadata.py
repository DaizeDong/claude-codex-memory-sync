"""Generated adapter metadata stays usable without changing source templates."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from profile_sync import render_adapter
from profile_bridge.metadata import source_description
from tools.make_fixtures import (make_adapter_descriptions, make_adapter_description_source,
                                 make_adapter_overlay, make_raw_metadata_home)


def description_of(payload):
    line = payload.decode("utf-8").splitlines()[2]
    return json.loads(line.removeprefix("description: "))


@pytest.mark.parametrize("case", ["escaped_examples", "block_marker", "folded_marker"])
def test_generated_adapter_removes_source_serialization_from_description(tmp_path, case):
    cases = make_adapter_descriptions()
    source = make_adapter_description_source(tmp_path, case)
    payload = render_adapter("example-reviewer", "example-reviewer", source_description(source, 'Fallback.'),
                             source, tmp_path)
    assert description_of(payload) == cases["summary"]


def test_generated_adapter_description_is_bounded_without_serialized_examples():
    cases = make_adapter_descriptions()
    payload = render_adapter("example-reviewer", "example-reviewer", cases["long_summary"],
                             Path("synthetic/agents/reviewer.md"), Path("synthetic"))
    description = description_of(payload)
    assert 0 < len(description) <= 400
    assert description.endswith("correctness.")


def test_description_comparisons_keep_meaning_without_invalid_angle_brackets():
    cases = make_adapter_descriptions()
    payload = render_adapter('example-reviewer', 'example-reviewer', cases['comparisons'],
                             Path('synthetic/agents/reviewer.md'), Path('synthetic'))
    assert description_of(payload) == cases['comparison_summary']


@pytest.mark.parametrize('case,expected', [
    ('unspaced_comparisons', 'unspaced_summary'), ('code_type', 'code_type_summary'), ('markup', 'markup_summary'),
    ('leading_operator', 'leading_operator_summary'), ('absolute_value', 'absolute_value_summary'),
    ('chained_comparison', 'chained_comparison_summary'), ('inline_example', 'inline_example_summary'),
    ('literal_newline', 'literal_newline_summary'),
])
def test_description_preserves_conditions_and_only_strips_known_markup(case, expected):
    cases = make_adapter_descriptions()
    payload = render_adapter('example-reviewer', 'example-reviewer', cases[case],
                             Path('synthetic/agents/reviewer.md'), Path('synthetic'))
    assert description_of(payload) == cases[expected]


@pytest.mark.parametrize('case,allowed', [
    ('colon', False), ('list', False), ('quote', False), ('boolean', False),
    ('quoted', True), ('literal', True), ('folded', True),
    ('duplicate_name', False), ('duplicate_description', False),
    ('duplicate_nested', False), ('unique_nested', True),
])
def test_cli_validates_raw_frontmatter_before_planning_installation(tmp_path, case, allowed):
    fixture = make_raw_metadata_home(tmp_path, case)
    before = fixture['source'].read_bytes()
    result = subprocess.run([sys.executable, '-m', 'profile_sync', '--claude-home', str(fixture['claude']),
        '--codex-home', str(fixture['codex']), '--skills-home', str(fixture['skills']), '--dry-run', '--json'],
        capture_output=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    row = next(item for item in report['skills'] if item['source'] == str(fixture['source'].parent.resolve()))
    assert row['status'] == ('link' if allowed else 'unsupported')
    if not allowed:
        assert row['reason'].startswith('invalid_skill_frontmatter')
        assert not any(Path(change['path']).parent == fixture['skills'] for change in report['changes'])
    assert fixture['source'].read_bytes() == before
    assert not fixture['codex'].exists() and not fixture['skills'].exists()


def test_runtime_overlay_keeps_examples_in_payload_and_shortens_discovery(tmp_path):
    from profile_bridge.overlays import bundle
    cases = make_adapter_descriptions()
    descriptor = make_adapter_overlay(tmp_path)
    target = tmp_path / 'installed/SKILL.md'
    files = bundle('synthetic', cases['escaped_examples'], target, descriptor)
    assert description_of(files[target]) == cases['summary']
    assert b'<example>Keep the full example here.</example>' in files[target.parent / 'payload/SKILL.md']
