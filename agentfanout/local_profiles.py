"""Exact Local identities and attested request profiles; no lifecycle authority.

Namespaced identifiers are JSON data. Legacy LocalEndpointRequestV1 is unchanged.
Profile declarations never establish that a backend is loaded or conformed.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any
from .project_binding import bounded_content_is_sensitive

AEON_REPOSITORY = 'AEON-7/Qwen3.8-27B-AEON-ULTIMATE-UNCENSORED-BF16'
MODEL_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]*(?:/[A-Za-z0-9][A-Za-z0-9._:-]*)?$')
SLUG = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
DIGEST = re.compile(r'^[a-f0-9]{64}$')

class LocalProfileError(ValueError):
    pass

class LocalSensitiveResponseError(LocalProfileError):
    """Reject disclosure/result acceptance while retaining verified numeric usage."""
    def __init__(self, usage: dict[str, Any]):
        super().__init__('local_response_sensitive')
        self.usage_observation = {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
        for key in ('prompt_tokens_details', 'completion_tokens_details'):
            details = usage.get(key)
            if isinstance(details, dict):
                self.usage_observation[key] = {name: number for name, number in details.items() if name in {'cached_tokens', 'reasoning_tokens'} and type(number) is int and number >= 0}
        self.provider_cost_usd = None

def _text(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise LocalProfileError('local_identity_text_invalid')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise LocalProfileError('local_identity_text_invalid') from exc
    return value

def model_identifier(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 256 or not MODEL_ID.fullmatch(value) or any(part in {'.', '..'} for part in value.split('/')):
        raise LocalProfileError('local_model_identifier_invalid')
    return value

def identity_digest(identity: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(validate_identity(identity), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def validate_identity(value: Any) -> dict[str, Any]:
    fields = {'schema', 'logical_selector', 'artifact_repository', 'artifact_revision', 'artifact_sha256', 'precision', 'served_name', 'backend', 'backend_version', 'config_sha256', 'template_sha256', 'endpoint_identity', 'resource_domain', 'serving_epoch', 'inference_profile_sha256', 'evaluation_capability'}
    if not isinstance(value, dict) or set(value) != fields or value['schema'] != 'LocalModelIdentityV1':
        raise LocalProfileError('local_identity_schema_invalid')
    for key in fields - {'schema'}:
        _text(value[key])
    if not SLUG.fullmatch(value['logical_selector']):
        raise LocalProfileError('local_selector_invalid')
    if not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', value['artifact_revision']):
        raise LocalProfileError('local_artifact_revision_unpinned')
    model_identifier(value['artifact_repository'])
    model_identifier(value['served_name'])
    for key in ('artifact_sha256', 'config_sha256', 'template_sha256', 'inference_profile_sha256'):
        if not DIGEST.fullmatch(value[key]):
            raise LocalProfileError('local_identity_digest_invalid')
    if value['artifact_repository'] == AEON_REPOSITORY and value['precision'] != 'BF16':
        raise LocalProfileError('local_aeon_bf16_required')
    if value['precision'].lower() in {'unknown', 'auto', 'unspecified'}:
        raise LocalProfileError('local_precision_unidentified')
    return copy.deepcopy(value)

def validate_profile(profile: Any) -> dict[str, Any]:
    fields = {'schema', 'profile_id', 'identity_sha256', 'attestation_sha256', 'thinking_control', 'sampling', 'constrained_output', 'reasoning_field', 'max_output_tokens'}
    if not isinstance(profile, dict) or set(profile) != fields or profile['schema'] != 'LocalInferenceProfileV1':
        raise LocalProfileError('local_profile_schema_invalid')
    if not SLUG.fullmatch(_text(profile['profile_id'])):
        raise LocalProfileError('local_profile_id_invalid')
    for key in ('identity_sha256', 'attestation_sha256'):
        if not isinstance(profile[key], str) or not DIGEST.fullmatch(profile[key]):
            raise LocalProfileError('local_profile_attestation_required')
    control = profile['thinking_control']
    if not isinstance(control, dict) or set(control) != {'mechanism', 'value'}:
        raise LocalProfileError('local_thinking_control_invalid')
    if control['mechanism'] == 'chat_template_kwargs.enable_thinking':
        if not isinstance(control['value'], bool):
            raise LocalProfileError('local_thinking_value_invalid')
    elif control['mechanism'] == 'reasoning_effort':
        if control['value'] not in {'low', 'medium', 'high'}:
            raise LocalProfileError('local_thinking_value_invalid')
    elif control['mechanism'] != 'unsupported' or control['value'] is not None:
        raise LocalProfileError('local_thinking_control_invalid')
    sampling = profile['sampling']
    if not isinstance(sampling, dict) or set(sampling) - {'temperature', 'top_p', 'top_k'}:
        raise LocalProfileError('local_sampling_invalid')
    for key, number in sampling.items():
        maximum = {'temperature': 2, 'top_p': 1, 'top_k': 1000}[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= maximum:
            raise LocalProfileError('local_sampling_invalid')
        if key == 'top_k' and not isinstance(number, int):
            raise LocalProfileError('local_sampling_invalid')
    if profile['constrained_output'] not in {'unsupported', 'json_schema'} or profile['reasoning_field'] not in {'unsupported', 'reasoning', 'reasoning_content'}:
        raise LocalProfileError('local_profile_capability_invalid')
    limit = profile['max_output_tokens']
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 32768:
        raise LocalProfileError('local_profile_output_limit_invalid')
    return copy.deepcopy(profile)

def build_inference_request(identity: dict[str, Any], profile: dict[str, Any], prompt: str, packet_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    identity = validate_identity(identity)
    profile = validate_profile(profile)
    if profile['identity_sha256'] != identity_digest(identity):
        raise LocalProfileError('local_profile_identity_mismatch')
    # Profile digest binds profile material excluding the reciprocal identity hash.
    material = {k: v for k, v in profile.items() if k != 'identity_sha256'}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != identity['inference_profile_sha256']:
        raise LocalProfileError('local_profile_digest_mismatch')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 300000:
        raise LocalProfileError('local_prompt_invalid')
    if bounded_content_is_sensitive(prompt):
        raise LocalProfileError('local_prompt_sensitive')
    body = {'model': identity['logical_selector'], 'messages': [{'role': 'user', 'content': prompt}], 'stream': False, 'max_tokens': profile['max_output_tokens'], **profile['sampling']}
    control = profile['thinking_control']
    if control['mechanism'] == 'chat_template_kwargs.enable_thinking':
        body['chat_template_kwargs'] = {'enable_thinking': control['value']}
    elif control['mechanism'] == 'reasoning_effort':
        body['reasoning_effort'] = control['value']
    if packet_schema is not None:
        if profile['constrained_output'] != 'json_schema':
            raise LocalProfileError('local_constrained_output_unavailable')
        if not isinstance(packet_schema, dict) or packet_schema.get('title') not in {'WorkerResultV1', 'ValidatorResultV1'}:
            raise LocalProfileError('local_packet_schema_invalid')
        schema_path = Path(__file__).resolve().parent.parent / 'schemas' / (packet_schema['title'] + '.schema.json')
        try:
            approved_schema = json.loads(schema_path.read_text())
        except (OSError, ValueError) as exc:
            raise LocalProfileError('local_packet_schema_unavailable') from exc
        if packet_schema != approved_schema:
            raise LocalProfileError('local_packet_schema_unapproved')
        body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': packet_schema['title'], 'strict': True, 'schema': copy.deepcopy(packet_schema)}}
    return body

def parse_inference_response(response: Any, identity: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    identity, profile = validate_identity(identity), validate_profile(profile)
    if not isinstance(response, dict) or response.get('model') != identity['served_name']:
        raise LocalProfileError('local_response_identity_mismatch')
    choices = response.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict) or choices[0].get('finish_reason') != 'stop':
        raise LocalProfileError('local_response_incomplete')
    message = choices[0].get('message')
    if not isinstance(message, dict) or message.get('tool_calls') or message.get('function_call') or not isinstance(message.get('content'), str):
        raise LocalProfileError('local_response_tool_or_content_invalid')
    reasoning_fields = set(message) & {'reasoning', 'reasoning_content'}
    if reasoning_fields - {profile['reasoning_field']}:
        raise LocalProfileError('local_reasoning_field_unattested')
    usage = response.get('usage')
    if not isinstance(usage, dict) or any(isinstance(usage.get(k), bool) or not isinstance(usage.get(k), int) or usage[k] < 0 for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')) or usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
        raise LocalProfileError('local_usage_invalid')
    reasoning = message.get(profile['reasoning_field'])
    if reasoning is not None and not isinstance(reasoning, str):
        raise LocalProfileError('local_reasoning_invalid')
    if bounded_content_is_sensitive(message['content']) or (reasoning is not None and bounded_content_is_sensitive(reasoning)):
        raise LocalSensitiveResponseError(usage)
    return {'content': message['content'], 'reasoning': reasoning, 'usage': copy.deepcopy(usage), 'provider_cost_usd': None, 'identity_sha256': identity_digest(identity)}
