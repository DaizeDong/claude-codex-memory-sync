"""Admission for the optional installed-wheel test; never resolve llmcall from PyPI."""
import importlib
import importlib.util
from importlib import metadata
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def load_reviewed_llmcall(expected_sha256):
    """Require a reviewed 0.3.1 wheel receipt and the distribution's own module."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256):
        raise ValueError('llmcall_integration_unverified:expected_wheel_sha256_required')
    try:
        distribution = metadata.distribution('llmcall')
    except metadata.PackageNotFoundError as exc:
        raise ValueError('llmcall_integration_unverified:distribution_missing') from exc
    if distribution.metadata['Name'] != 'llmcall' or distribution.version != '0.3.1':
        raise ValueError('llmcall_integration_unverified:distribution_must_be_llmcall_0.3.1')
    try:
        receipt = json.loads(distribution.read_text('direct_url.json') or '{}')
        digest = receipt['archive_info']['hashes']['sha256']
        wheel_path = urlsplit(receipt['url']).path
        if not wheel_path.endswith('.whl') or digest.lower() != expected_sha256.lower():
            raise ValueError('wheel receipt mismatch')
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError('llmcall_integration_unverified:wheel_receipt_mismatch') from exc
    recorded_files = {Path(distribution.locate_file(file)).resolve() for file in distribution.files or ()}
    expected_module = Path(distribution.locate_file('llmcall/__init__.py')).resolve()

    def matches_expected(path):
        return path is not None and Path(path).resolve() == expected_module

    cached = sys.modules.get('llmcall')
    if expected_module not in recorded_files or (cached is not None and not matches_expected(getattr(cached, '__file__', None))):
        raise ValueError('llmcall_integration_unverified:module_outside_distribution')
    try:
        spec = importlib.util.find_spec('llmcall')
    except (ImportError, ValueError) as exc:
        raise ValueError('llmcall_integration_unverified:module_outside_distribution') from exc
    if spec is None or not matches_expected(spec.origin):
        raise ValueError('llmcall_integration_unverified:module_outside_distribution')
    module = importlib.import_module('llmcall')
    if not matches_expected(getattr(module, '__file__', None)):
        raise ValueError('llmcall_integration_unverified:module_outside_distribution')
    if not all(callable(getattr(module, name, None)) for name in ('Result', 'Attempt', 'RecordingFailure')):
        raise ValueError('llmcall_integration_unverified:required_api_missing')
    return module
