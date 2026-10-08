"""Optional codec integration with reviewed llmcall types; no provider is invoked."""
import os

import pytest

from llmcall_wheel_contract import load_reviewed_llmcall
from test_t10_workflow_context import assert_recording_failure_round_trip


def test_reviewed_llmcall_wheel_recording_failure_round_trip(tmp_path, monkeypatch):
    enabled = os.environ.get('PROFILE_SYNC_LLM_CALL_INTEGRATION', '')
    if not enabled:
        pytest.skip('NOT_RUN: reviewed llmcall 0.3.1 wheel integration is not enabled')
    if enabled != '1':
        pytest.fail('PROFILE_SYNC_LLM_CALL_INTEGRATION must be 1 or unset')
    client = load_reviewed_llmcall(os.environ.get('PROFILE_SYNC_LLM_CALL_WHEEL_SHA256'))
    monkeypatch.chdir(tmp_path)
    assert_recording_failure_round_trip(tmp_path, client)
