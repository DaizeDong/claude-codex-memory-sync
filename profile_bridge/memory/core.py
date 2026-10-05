"""Pure, versioned identities. No path decoding, clocks, or publication claims."""
import hashlib
import json
import re
import unicodedata

NORMALIZATION = 'ccms-nfc-lf-v1'


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + '\n').encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical_text(value, *, strict=False):
    if isinstance(value, bytes):
        value = value.decode('utf-16' if value.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    for separator in ('\x85', '\u2028', '\u2029'):
        value = value.replace(separator, '\n')
    result = []
    for character in value:
        if unicodedata.category(character) == 'Cc' and character not in '\t\n':
            if strict:
                raise ValueError('unsafe_memory_control')
            result.append(f'\\u{ord(character):04x}')
        else:
            result.append(character)
    return unicodedata.normalize('NFC', ''.join(result)).rstrip('\n') + '\n'


def content_digest(value):
    return sha(canonical_text(value).encode('utf-8'))


def relative_path(value):
    value = unicodedata.normalize('NFC', value.replace('\\', '/'))
    if (not value or value.startswith('/') or any(p in {'', '.', '..'} for p in value.split('/'))
            or any(ord(c) < 32 or c in ':\u0085\u2028\u2029' for c in value)):
        raise ValueError('invalid_memory_relative_path')
    # Windows compatibility identities are case insensitive. Colliding spelling
    # is rejected by the registry, never resolved by enumeration order.
    return value.casefold()


def source_id(namespace, project, relative):
    return sha(encoded({'version': 1, 'namespace': namespace, 'project': project,
                        'path': relative_path(relative)}))


def increment_id(source, predecessor, operation, digest):
    if operation not in {'add', 'update', 'delete', 'reappear'}:
        raise ValueError('invalid_memory_operation')
    for value in (source, digest, predecessor):
        if value is not None and not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('invalid_memory_digest')
    return sha(encoded({'version': 1, 'algorithm': 'sha256', 'normalization': NORMALIZATION,
                        'source_id': source, 'predecessor': predecessor,
                        'operation': operation, 'content_digest': digest}))
