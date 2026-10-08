"""Synthetic controls for the optional real-wheel test's provenance checks."""
from importlib import metadata
import json

import pytest

import llmcall_wheel_contract as contract
from tools.make_fixtures import make_llmcall_wheel_installation


@pytest.fixture
def installed(tmp_path, monkeypatch):
    distribution, module, digest, directory = make_llmcall_wheel_installation(tmp_path)
    monkeypatch.setattr(contract.metadata, 'distribution', lambda name: distribution)
    monkeypatch.setattr(contract.importlib, 'import_module', lambda name: module)
    monkeypatch.setattr(contract.importlib.util, 'find_spec', lambda name: module.__spec__)
    monkeypatch.delitem(contract.sys.modules, 'llmcall', raising=False)
    return module, digest, directory


def test_reviewed_wheel_receipt_admits_its_recorded_module(installed):
    module, digest, _ = installed
    assert contract.load_reviewed_llmcall(digest.upper()) is module


@pytest.mark.parametrize('digest', [None, '', 'unreviewed', 'a' * 63, 'g' * 64])
def test_missing_or_invalid_expected_hash_fails(digest):
    with pytest.raises(ValueError, match='expected_wheel_sha256_required'):
        contract.load_reviewed_llmcall(digest)


def test_missing_distribution_fails(installed, monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(contract.metadata, 'distribution', missing)
    with pytest.raises(ValueError, match='distribution_missing'):
        contract.load_reviewed_llmcall(installed[1])


@pytest.mark.parametrize('name, version', [('llmcall', '1.0.2'), ('other-project', '0.3.1')])
def test_wrong_distribution_fails(installed, name, version):
    _, digest, directory = installed
    (directory / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='distribution_must_be_llmcall_0.3.1'):
        contract.load_reviewed_llmcall(digest)


@pytest.mark.parametrize('receipt', [None, 'invalid json', '{}', 'null',
    json.dumps({'url': 'file:///synthetic/package.whl', 'archive_info': {'hashes': {'sha256': 'b' * 64}}}),
    json.dumps({'url': 'file:///synthetic/source.tar.gz', 'archive_info': {'hashes': {'sha256': 'a' * 64}}}),
    json.dumps({'url': 'file:///synthetic/source', 'dir_info': {'editable': True}})])
def test_missing_or_wrong_wheel_receipt_fails(installed, receipt):
    _, digest, directory = installed
    path = directory / 'direct_url.json'
    if receipt is None:
        path.unlink()
    else:
        path.write_text(receipt, encoding='utf-8')
    with pytest.raises(ValueError, match='wheel_receipt_mismatch'):
        contract.load_reviewed_llmcall(digest)


@pytest.mark.parametrize('failure', ['shadow', 'no_file', 'unrecorded'])
def test_module_outside_distribution_fails(installed, failure):
    module, digest, directory = installed
    if failure == 'shadow':
        module.__file__ = str(directory.parent / 'shadow/llmcall/__init__.py')
    elif failure == 'no_file':
        module.__file__ = None
    else:
        (directory / 'RECORD').unlink()
    with pytest.raises(ValueError, match='module_outside_distribution'):
        contract.load_reviewed_llmcall(digest)


@pytest.mark.parametrize('failure', ['shadow_spec', 'cached_shadow', 'missing_spec'])
def test_shadow_module_is_rejected_before_import(installed, monkeypatch, failure):
    module, digest, directory = installed
    shadow = str(directory.parent / 'shadow/llmcall/__init__.py')
    if failure == 'shadow_spec':
        module.__spec__.origin = shadow
    elif failure == 'cached_shadow':
        module.__file__ = shadow
        monkeypatch.setitem(contract.sys.modules, 'llmcall', module)
    else:
        module.__spec__ = None
    monkeypatch.setattr(contract.importlib, 'import_module', lambda name: pytest.fail('unverified module was imported'))
    with pytest.raises(ValueError, match='module_outside_distribution'):
        contract.load_reviewed_llmcall(digest)


@pytest.mark.parametrize('name', ['Result', 'Attempt', 'RecordingFailure'])
def test_missing_api_fails(installed, name):
    module, digest, _ = installed
    setattr(module, name, None)
    with pytest.raises(ValueError, match='required_api_missing'):
        contract.load_reviewed_llmcall(digest)
