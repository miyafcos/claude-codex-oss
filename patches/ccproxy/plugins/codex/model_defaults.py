"""Default model metadata and mapping rules for the Codex provider.

Generated from runtime/models.json by scripts/generate-model-configs.py.
"""

from __future__ import annotations

from ccproxy.models.provider import ModelCard, ModelMappingRule


DEFAULT_CODEX_MODEL_CARDS: list[ModelCard] = [
    ModelCard(
        id='gpt-6.1-sol',
        created=1790726400,
        owned_by='openai',
        permission=[],
        root='gpt-6.1-sol',
        parent=None,
    ),
    ModelCard(
        id='gpt-6.1-terra',
        created=1792972800,
        owned_by='openai',
        permission=[],
        root='gpt-6.1-terra',
        parent=None,
    ),
    ModelCard(
        id='gpt-6-astra',
        created=1788393600,
        owned_by='openai',
        permission=[],
        root='gpt-6-astra',
        parent=None,
    ),
    ModelCard(
        id='gpt-6-luna',
        created=1788393600,
        owned_by='openai',
        permission=[],
        root='gpt-6-luna',
        parent=None,
    ),
    ModelCard(
        id='gpt-6-sol',
        created=1788393600,
        owned_by='openai',
        permission=[],
        root='gpt-6-sol',
        parent=None,
    ),
    ModelCard(
        id='gpt-5.6-sol',
        created=1782864000,
        owned_by='openai',
        permission=[],
        root='gpt-5.6-sol',
        parent=None,
    ),
    ModelCard(
        id='gpt-5.6-terra',
        created=1782864000,
        owned_by='openai',
        permission=[],
        root='gpt-5.6-terra',
        parent=None,
    ),
    ModelCard(
        id='gpt-5.6-luna',
        created=1782864000,
        owned_by='openai',
        permission=[],
        root='gpt-5.6-luna',
        parent=None,
    ),
]


DEFAULT_CODEX_MODEL_MAPPINGS: list[ModelMappingRule] = [
    ModelMappingRule(match='gpt-6.1-sol', target='gpt-6.1-sol', kind='exact'),
    ModelMappingRule(match='gpt-6.1-terra', target='gpt-6.1-terra', kind='exact'),
    ModelMappingRule(match='gpt-6-astra', target='gpt-6-astra', kind='exact'),
    ModelMappingRule(match='gpt-6-sol', target='gpt-6-sol', kind='exact'),
    ModelMappingRule(match='gpt-6-luna', target='gpt-6-luna', kind='exact'),
    ModelMappingRule(match='gpt-5.6-sol', target='gpt-5.6-sol', kind='exact'),
    ModelMappingRule(match='gpt-5.6-terra', target='gpt-5.6-terra', kind='exact'),
    ModelMappingRule(match='gpt-5.6-luna', target='gpt-5.6-luna', kind='exact'),
    ModelMappingRule(match='claude-', target='gpt-6-astra', kind='prefix'),
]


__all__ = [
    "DEFAULT_CODEX_MODEL_CARDS",
    "DEFAULT_CODEX_MODEL_MAPPINGS",
]
