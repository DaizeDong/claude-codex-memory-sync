"""Narrow Windows credential/HTTP policy for the installed mcp-proxy runners.

One bridge per process. No credential files, provider discovery, or MCP protocol
implementation. --check is offline and never reads the Windows credential store.
A runtime generation ships this file as a source resource and starts it through launch.py --script.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from functools import partial
from importlib import import_module
import inspect
import json
import logging
import os
from pathlib import Path
import re
import ssl
import sys
from types import MappingProxyType
from typing import NamedTuple

GITHUB_URL = 'https://api.githubcopilot.com/mcp/'
SSE_URLS = frozenset({'https://mcp.api.coingecko.com/sse'})
ERROR_CODES = frozenset({
    'invalid_configuration', 'invalid_endpoint', 'invalid_account', 'invalid_gh_binding',
    'unsupported_platform', 'unsupported_dependency', 'credential_failed',
    'credential_timeout', 'credential_cancelled', 'credential_cleanup_failed',
    'credential_output_limit', 'credential_io_failed', 'credential_launch_failed',
    'credential_exit_nonzero', 'credential_output_invalid',
    'credential_destination_rejected', 'redirect_rejected', 'remote_http_failed',
    'transport_failed', 'interrupted',
})


class BridgeError(Exception):
    """Only fixed, credential-free status codes cross the diagnostic boundary."""

    def __init__(self, code):
        self.code = code if code in ERROR_CODES else 'transport_failed'
        super().__init__(self.code)


class Config(NamedTuple):
    service: str
    url: str
    account: str | None
    gh_executable: str | None
    check: bool
    check_auth: bool


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unrecognized arguments, including secrets.
        raise BridgeError('invalid_configuration')


def parse_config(argv):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--service', choices=('github', 'sse'), required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--account')
    parser.add_argument('--gh-executable')
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument('--check', action='store_true')
    checks.add_argument('--check-auth', action='store_true')
    args = parser.parse_args(argv)
    if args.service == 'github':
        if args.url != GITHUB_URL:
            raise BridgeError('invalid_endpoint')
        if (not args.account or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', args.account)
                or '--' in args.account):
            raise BridgeError('invalid_account')
        if not args.gh_executable:
            raise BridgeError('invalid_gh_binding')
        path = Path(args.gh_executable)
        if (not path.is_absolute() or path.name.lower() != 'gh.exe'
                or not path.is_file() or str(path).startswith(('\\\\', '//'))):
            raise BridgeError('invalid_gh_binding')
    elif args.url not in SSE_URLS:
        raise BridgeError('invalid_endpoint')
    elif args.account is not None or args.gh_executable is not None or args.check_auth:
        raise BridgeError('invalid_configuration')
    return Config(args.service, args.url, args.account, args.gh_executable, args.check, args.check_auth)


def check_runtime():
    """Fail before key acquisition if the reviewed library seam is unavailable."""
    if sys.platform != 'win32':
        raise BridgeError('unsupported_platform')
    try:
        from llmcall import process
        for name in ('max_output_bytes', 'max_input_bytes', 'context'):
            if name not in inspect.signature(process.run).parameters:
                raise BridgeError('unsupported_dependency')
        for kind in ('streamablehttp', 'sse'):
            module = import_module('mcp_proxy.' + kind + '_client')
            transport = getattr(module, kind + '_client')
            if 'httpx_client_factory' not in inspect.signature(transport).parameters:
                raise BridgeError('unsupported_dependency')
            if not callable(getattr(module, 'run_' + kind + '_client')):
                raise BridgeError('unsupported_dependency')
    except Exception:
        raise BridgeError('unsupported_dependency') from None


def resolve_github_token(config):
    """Read exactly one account through the common contained process owner."""
    from llmcall import process
    # Deliberate allowlist: GH_TOKEN/GITHUB_TOKEN, GH_CONFIG_DIR, GH_DEBUG,
    # proxy variables and provider variables cannot change this resolution.
    allowed = {'SYSTEMROOT', 'WINDIR', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
               'HOMEDRIVE', 'HOMEPATH', 'TEMP', 'TMP'}
    env = {k.upper(): v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1',
               GH_NO_EXTENSION_UPDATE_NOTIFIER='1', NO_COLOR='1', TERM='dumb')
    context = process.CallContext(str(Path(config.gh_executable).parent), MappingProxyType(env))
    try:
        result = process.run(
            [config.gh_executable, 'auth', 'token', '--hostname', 'github.com', '--user', config.account],
            b'', 10, context=context, max_output_bytes=1024, max_input_bytes=0,
        )
    except Exception:
        raise BridgeError('credential_failed') from None
    try:
        if result.error is not None:
            code = 'credential_' + result.error
            raise BridgeError(code if code in ERROR_CODES else 'credential_failed')
        if result.returncode != 0 or result.outcome != 'success' or result.execution_started is not True:
            raise BridgeError('credential_failed')
        if not isinstance(result.stdout_bytes, bytes) or result.stderr_bytes != b'':
            raise BridgeError('credential_output_invalid')
        raw = result.stdout_bytes
        if len(raw) > 1024:
            raise BridgeError('credential_output_invalid')
        # One optional terminal newline, no trimming of arbitrary output.
        match = re.fullmatch(r'(gh[op]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})(?:\r?\n)?',
                             raw.decode('ascii'))
        if match is None:
            raise BridgeError('credential_output_invalid')
        return match[1]
    except (UnicodeError, AttributeError, TypeError):
        raise BridgeError('credential_output_invalid') from None
    finally:
        # Drop all ProcessOutput copies; Python strings cannot promise zeroization.
        del result


def http_client_factory(url, token=None):
    """SDK factory hook: no redirects, ambient proxies/TLS key logs, or auth URLs."""
    import certifi
    import httpx
    endpoint = httpx.URL(url)
    if (token is not None and url != GITHUB_URL) or (token is None and url not in SSE_URLS):
        raise BridgeError('credential_destination_rejected')
    # ssl.create_default_context reads SSLKEYLOGFILE even with trust_env=False.
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_verify_locations(certifi.where())

    async def request_policy(request):
        target = request.url
        if token is not None:
            if str(target) != GITHUB_URL:
                raise BridgeError('credential_destination_rejected')
            request.headers['Authorization'] = 'Bearer ' + token
        elif (target.scheme, target.host, target.port) != (endpoint.scheme, endpoint.host, endpoint.port):
            raise BridgeError('credential_destination_rejected')
        if target.username or target.password:
            raise BridgeError('credential_destination_rejected')

    async def response_policy(response):
        if 300 <= response.status_code < 400:
            raise BridgeError('redirect_rejected')
        if response.status_code >= 400 and response.status_code not in (404, 405):
            # Preserve SDK session termination handling without printing URL/body.
            raise BridgeError('remote_http_failed')

    def factory(headers=None, timeout=None, auth=None):
        if auth is not None or any(key.lower() in ('authorization', 'proxy-authorization') for key in (headers or {})):
            raise BridgeError('credential_destination_rejected')
        return httpx.AsyncClient(
            headers=headers, timeout=timeout or httpx.Timeout(30.0),
            follow_redirects=False, trust_env=False, verify=tls,
            event_hooks={'request': [request_policy], 'response': [response_policy]},
        )
    return factory


@contextmanager
def _transport_binding(module, name, factory):
    """mcp-proxy 0.9 runners omit factory injection; bind their SDK import only.

    Scoped to a dedicated stdio process, restored on every exit. The installed
    SDK still owns HTTP/SSE framing, sessions, cancellation and MCP messages.
    """
    original = getattr(module, name)
    setattr(module, name, partial(original, httpx_client_factory=factory))
    try:
        yield
    finally:
        setattr(module, name, original)


async def run_bridge(config, token=None):
    kind = 'streamablehttp' if config.service == 'github' else 'sse'
    module = import_module('mcp_proxy.' + kind + '_client')
    factory = http_client_factory(config.url, token)
    with _transport_binding(module, kind + '_client', factory):
        await getattr(module, 'run_' + kind + '_client')(config.url, headers=None)


def _status(ok, code):
    print(json.dumps({'ok': ok, 'code': code}), file=sys.stdout if ok else sys.stderr, flush=True)


def main(argv=None):
    # The CLI owns this process. Library DEBUG output can contain message bodies,
    # response URLs and exception data; none is an acceptable diagnostic here.
    logging.disable(sys.maxsize)
    try:
        config = parse_config(argv)
        check_runtime()
        if config.check:
            _status(True, 'configuration_ready')
            return 0
        token = resolve_github_token(config) if config.service == 'github' else None
        if config.check_auth:
            _status(True, 'credential_ready')
            return 0
        asyncio.run(run_bridge(config, token))
        return 0
    except BridgeError as exc:
        _status(False, exc.code)
    except KeyboardInterrupt:
        _status(False, 'interrupted')
    except Exception:
        _status(False, 'transport_failed')
    return 1


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
