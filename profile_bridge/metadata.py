"""Validate raw skill metadata before offering a source for installation."""
from pathlib import Path

import yaml


class _UniqueKeyLoader(yaml.SafeLoader):
    """Use safe YAML types and reject ambiguous keys in every mapping."""

    def construct_mapping(self, node, deep=False):
        mapping = super().construct_mapping(node, deep=deep)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in keys:
                raise yaml.constructor.ConstructorError(problem='duplicate mapping key')
            keys.add(key)
        return mapping


def _read_metadata(entrypoint):
    """Parse source serialization without relying on flattened catalog summaries."""
    try:
        lines = Path(entrypoint).read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeError):
        return None, 'unreadable_skill_frontmatter'
    if not lines or lines[0] != '---':
        return None, 'missing_skill_frontmatter'
    try:
        end = lines.index('---', 1)
    except ValueError:
        return None, 'invalid_skill_frontmatter_yaml'
    try:
        metadata = yaml.load('\n'.join(lines[1:end]), Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, RecursionError):
        return None, 'invalid_skill_frontmatter_yaml'
    if not isinstance(metadata, dict):
        return None, 'invalid_skill_frontmatter_type'
    return metadata, None


def source_description(entrypoint, fallback):
    """Read the parsed scalar so literal operators cannot become YAML markers."""
    metadata, error = _read_metadata(entrypoint)
    description = metadata.get('description') if error is None else None
    return description if isinstance(description, str) and description.strip() else fallback


def skill_metadata_error(entrypoint):
    """Return a stable rejection reason, without echoing source contents."""
    metadata, error = _read_metadata(entrypoint)
    if error is not None:
        return error
    for field in ('name', 'description'):
        if field not in metadata or metadata[field] is None:
            return 'missing_skill_frontmatter'
        if not isinstance(metadata[field], str):
            return 'invalid_skill_frontmatter_type'
        if not metadata[field].strip():
            return 'missing_skill_frontmatter'
    return None
