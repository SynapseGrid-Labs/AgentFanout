"""Versioned Grok ACP source mappings; installed observations remain host-owned.

The reviewed public revision is a reference, not the installed binary's revision.
No mapping, auth response or billing observation authorizes admission or spending.
No credential-export extension or private HTTP path is implemented here.
"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json

from .grok_cli import GrokCLIUnavailable, PreparedSession, _canonical

REFERENCE_REVISION = '2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8'
MAPPING_ID = 'grok-acp-reference-v1'
CORE_METHODS = frozenset({'initialize','authenticate','session/new','session/prompt',
                         'session/set_config_option','x.ai/auth/info','x.ai/session/usage'})
OPTIONAL_METHODS = frozenset({'x.ai/billing'})
MAPPING_CONTRACT = {
    'schema':'GrokACPMappingV1','id':MAPPING_ID,'reference_revision':REFERENCE_REVISION,
    'methods':sorted(CORE_METHODS | OPTIONAL_METHODS),'extension_wire_prefix':'_',
    'usage_surfaces':['turn_and_session','session_totals_only'],
    'auth':{'request':'cached_token','preference':'oidc','disable_api_key_auth':True,
            'metadata':'_meta.auth_mode','mode':'SessionToken','readback':'methodId'},
    'selection':{'model':'models.currentModelId','options':'configOptions',
                 'current':'currentValue','effort_id':'reasoning_effort'},
    'prompt_identity':['sessionId','modelId','requestId','promptId'],
    'usage':{'turn':'_meta.usage','cumulative':'usage','cache':'included','reasoning':'included',
             'ticks_per_usd':10000000000,'fields':['inputTokens','outputTokens','totalTokens',
                 'cachedReadTokens','cacheCreationTokens','reasoningTokens','modelCalls','apiDurationMs']},
}
MAPPING_SHA256 = hashlib.sha256(_canonical(MAPPING_CONTRACT)).hexdigest()


def _uint(value, code='wire_usage_incomplete'):
    if type(value) is not int or not 0 <= value <= 2**63-1:
        raise GrokCLIUnavailable(code)
    return value


def _digest(value):
    return isinstance(value,str) and len(value)==64 and all(x in '0123456789abcdef' for x in value)


@dataclass(frozen=True)
class WireBinding:
    schema: str
    binary_sha256: str
    mapping_sha256: str
    methods: frozenset[str]
    evidence_id: str
    usage_surface: str = "turn_and_session"

    def verify(self, binary_sha256, host_verify):
        if self.schema != 'GrokACPWireBindingV1' or not _digest(self.binary_sha256) or self.binary_sha256 != binary_sha256:
            raise GrokCLIUnavailable('wire_binary_binding_invalid')
        if self.mapping_sha256 != MAPPING_SHA256:
            raise GrokCLIUnavailable('wire_mapping_unsupported')
        if (not isinstance(self.methods,frozenset) or not CORE_METHODS <= self.methods
                or self.methods - (CORE_METHODS | OPTIONAL_METHODS)):
            raise GrokCLIUnavailable('wire_methods_unproved')
        if self.usage_surface not in {'turn_and_session','session_totals_only'}:
            raise GrokCLIUnavailable('wire_usage_surface_unproved')
        if not isinstance(self.evidence_id,str) or not 0 < len(self.evidence_id) <= 200 or not callable(host_verify) or host_verify(self) is not True:
            raise GrokCLIUnavailable('installed_wire_binding_unverified')

    def digest(self):
        return hashlib.sha256(_canonical({'schema':self.schema,'binary_sha256':self.binary_sha256,
            'mapping_sha256':self.mapping_sha256,'methods':sorted(self.methods),'evidence_id':self.evidence_id,'usage_surface':self.usage_surface})).hexdigest()


@dataclass(frozen=True)
class PreparedWireSession(PreparedSession):
    wire_mapping_sha256: str
    wire_binding_sha256: str


def validate_isolated_auth_profile(value):
    # This validates explicitly supplied profile data, never reads/writes an
    # operator's global config or establishes its effective installed behavior.
    if (not isinstance(value,dict) or set(value)!={'auth'} or not isinstance(value['auth'],dict)
            or value['auth'] != {'preferred_method':'oidc','disable_api_key_auth':True}
            or type(value['auth'].get('disable_api_key_auth')) is not bool):
        raise GrokCLIUnavailable('subscription_profile_override_denied')
    return deepcopy(value)


def isolated_auth_toml():
    return '[auth]\npreferred_method = "oidc"\ndisable_api_key_auth = true\n'


def verify_auth(auth, info):
    meta = auth.get('_meta') if isinstance(auth,dict) else None
    if (not isinstance(meta,dict) or meta.get('auth_mode') != 'SessionToken'
            or meta.get('backend_billed') is not False or meta.get('gate') is not None
            or not isinstance(info,dict) or info.get('methodId') != 'cached_token'):
        raise GrokCLIUnavailable('subscription_auth_not_confirmed')
    # Return no user profile, token, subscription claim or spending authority.
    return 'cached_token'


def selection(options):
    if not isinstance(options,list) or not 1 <= len(options) <= 64:
        raise GrokCLIUnavailable('effective_selection_readback_missing')
    found = {}
    seen = set()
    for option in options:
        if not isinstance(option,dict) or not isinstance(option.get('id'),str) or option['id'] in seen:
            raise GrokCLIUnavailable('effective_selection_readback_invalid')
        seen.add(option['id'])
        if option['id'] not in {'model','reasoning_effort'}:
            continue
        current, choices = option.get('currentValue'), option.get('options')
        if option.get('type') != 'select' or not isinstance(current,str) or not 0 < len(current) <= 200 or not isinstance(choices,list) or not 1 <= len(choices) <= 256:
            raise GrokCLIUnavailable('effective_selection_readback_invalid')
        values = []
        for choice in choices:
            if not isinstance(choice,dict) or not isinstance(choice.get('value'),str):
                raise GrokCLIUnavailable('effective_selection_readback_invalid')
            values.append(choice['value'])
        if len(values)!=len(set(values)) or current not in values:
            raise GrokCLIUnavailable('effective_selection_unsupported')
        found[option['id']] = current
    if set(found)!= {'model','reasoning_effort'}:
        raise GrokCLIUnavailable('effective_selection_readback_missing')
    return found['model'],found['reasoning_effort']


class ReferenceWireMapping:
    """Strict request/readback orchestration selected only by verified host data."""
    def __init__(self, binding, host_verify):
        self.binding, self.host_verify = binding, host_verify

    def verify(self, evidence):
        self.binding.verify(evidence.binary_sha256,self.host_verify)

    def prepare(self, client, task_id, text, role, workspace, evidence):
        self.verify(evidence)
        init,_ = client._request('initialize',{'protocolVersion':1,
            'clientCapabilities':{'fs':{'readTextFile':False,'writeTextFile':False},'terminal':False}})
        methods = init.get('authMethods')
        if type(init.get('protocolVersion')) is not int or init.get('protocolVersion') != 1 or not isinstance(methods,list) or not 1 <= len(methods) <= 32:
            raise GrokCLIUnavailable('protocol_or_auth_capabilities_unsupported')
        ids=[m.get('id') if isinstance(m,dict) else None for m in methods]
        if 'cached_token' not in ids or any(not isinstance(x,str) for x in ids) or len(ids)!=len(set(ids)):
            raise GrokCLIUnavailable('cached_session_auth_unavailable')
        if 'xai.api_key' in ids:
            raise GrokCLIUnavailable('api_auth_advertised_under_session_pin')
        auth,_=client._request('authenticate',{'methodId':'cached_token'})
        info,_=client._request('x.ai/auth/info',{})
        accepted=verify_auth(auth,info)
        session,_=client._request('session/new',{'cwd':workspace,'mcpServers':[]})
        sid=session.get('sessionId')
        if not isinstance(sid,str) or not 0 < len(sid) <= 200:
            raise GrokCLIUnavailable('session_missing')
        client._wire_session_id=sid
        model,effort=selection(session.get('configOptions'))
        if not isinstance(session.get('models'),dict) or session['models'].get('currentModelId')!=model:
            raise GrokCLIUnavailable('effective_model_readback_conflict')
        for config_id,desired in [('model',evidence.model),('reasoning_effort',evidence.effort)]:
            current=model if config_id=='model' else effort
            if current!=desired:
                reply,_=client._request('session/set_config_option',{'sessionId':sid,'configId':config_id,'value':desired})
                model,effort=selection(reply.get('configOptions'))
                if (model if config_id=='model' else effort)!=desired:
                    raise GrokCLIUnavailable('effective_selection_mismatch')
        if (model,effort)!=(evidence.model,evidence.effort):
            raise GrokCLIUnavailable('effective_selection_mismatch')
        client._wire_selection_locked=True
        baseline,_=client._request('x.ai/session/usage',{'sessionId':sid})
        decoded=decode_usage(baseline.get('usage'),model,cumulative=True)
        if any(decoded['audit'][k] for k in ('input_tokens','output_tokens','model_calls','num_turns')) or decoded['audit']['cost_usd_ticks'] not in (None,0):
            raise GrokCLIUnavailable('bootstrap_inference_observed')
        client._journal('wire_session_prepared',{'session_id':sid,'process_epoch':client.transport.epoch,
            'wire_mapping_sha256':MAPPING_SHA256,'wire_binding_sha256':self.binding.digest(),
            'baseline_sha256':hashlib.sha256(_canonical(baseline)).hexdigest()})
        from .grok_cli import input_digest
        return PreparedWireSession('GrokCLISessionV2',sid,client.transport.epoch,task_id,input_digest(text),
            model,effort,evidence.binary_sha256,evidence.config_sha256,accepted,evidence.evidence_id,role,
            MAPPING_SHA256,self.binding.digest())

    def observe_turn(self, client, result):
        turn_meta=result.get('_meta')
        if not isinstance(turn_meta,dict) or ('usage' not in turn_meta and self.binding.usage_surface=='turn_and_session'):
            raise GrokCLIUnavailable('whole_turn_usage_unavailable')
        prompt_id=turn_meta.get('promptId')
        if (turn_meta.get('sessionId')!=client.prepared.session_id or turn_meta.get('modelId')!=client.prepared.model
                or not isinstance(prompt_id,str) or not 0 < len(prompt_id) <= 200
                or turn_meta.get('requestId')!=prompt_id):
            raise GrokCLIUnavailable('prompt_identity_readback_conflict')
        if any(turn_meta.get(k) is not None for k in ('cancellationCategory','cancelTrigger','completionKind','structuredOutputError')):
            raise GrokCLIUnavailable('prompt_completion_unproved')
        try:
            turn=decode_usage(turn_meta['usage'],client.prepared.model,cumulative=False) if 'usage' in turn_meta else None
        except GrokCLIUnavailable:
            raw=turn_meta['usage']
            known={k:raw[k] for k in ('inputTokens','outputTokens','cachedReadTokens','cacheCreationTokens','reasoningTokens','modelCalls')
                if isinstance(raw,dict) and type(raw.get(k)) is int and 0 <= raw[k] <= 2**63-1}
            client._journal('wire_usage_unaccepted',{'session_id':client.prepared.session_id,
                'known_counters':known,'usage_complete':False,'provider_cost_usd':None})
            raise
        snapshot,_=client._request('x.ai/session/usage',{'sessionId':client.prepared.session_id})
        cumulative=decode_usage(snapshot.get('usage'),client.prepared.model,cumulative=True)
        # A fresh process/session executes exactly one prompt. Two complete views
        # must agree; last-call ACP sibling counters are intentionally ignored.
        for k in ('input_tokens','output_tokens','cached_read_tokens','cache_creation_tokens','reasoning_tokens','model_calls','num_turns','cost_usd_ticks'):
            if turn is not None and turn['audit'][k] != cumulative['audit'][k]:
                raise GrokCLIUnavailable('whole_turn_session_usage_conflict')
        if cumulative['audit']['model_calls'] < 1:
            raise GrokCLIUnavailable('whole_turn_usage_empty')
        if self.binding.usage_surface=='session_totals_only':
            turn=cumulative
        turn['audit']['provider_prompt_id']=prompt_id
        return turn

    def billing(self, client, authorize_observation):
        self.verify(client.evidence)
        if 'x.ai/billing' not in self.binding.methods:
            raise GrokCLIUnavailable('billing_extension_unproved')
        # Even this read is not automatic: it can contact the account backend.
        if not callable(authorize_observation) or authorize_observation('x.ai/billing',self.binding.digest()) is not True:
            raise GrokCLIUnavailable('billing_observation_authorization_required')
        client._journal('billing_observation_start_intent',{'wire_binding_sha256':self.binding.digest()})
        result,_=client._request('x.ai/billing',{})
        return decode_billing(result)


def decode_usage(value, model, *, cumulative):
    if not isinstance(value,dict) or type(cumulative) is not bool:
        raise GrokCLIUnavailable('wire_usage_incomplete')
    for flag in ('usageIsIncomplete','costIsPartial'):
        if flag in value and type(value[flag]) is not bool:
            raise GrokCLIUnavailable('wire_usage_flag_invalid')
    if value.get('usageIsIncomplete') is True:
        raise GrokCLIUnavailable('wire_usage_incomplete')
    wire=['inputTokens','outputTokens','totalTokens','cachedReadTokens','cacheCreationTokens','reasoningTokens','modelCalls','apiDurationMs']
    numbers={k:_uint(value.get(k)) for k in wire}
    turns=_uint(value.get('numTurns'))
    if (numbers['totalTokens']!=numbers['inputTokens']+numbers['outputTokens'] or
            numbers['cachedReadTokens']+numbers['cacheCreationTokens']>numbers['inputTokens'] or
            numbers['reasoningTokens']>numbers['outputTokens']):
        raise GrokCLIUnavailable('wire_usage_inconsistent')
    rows=value.get('modelUsage',{})
    if not isinstance(rows,dict) or len(rows)>1 or (rows and set(rows)!={model}):
        raise GrokCLIUnavailable('usage_model_identity_conflict')
    if numbers['modelCalls'] and set(rows)!={model}:
        raise GrokCLIUnavailable('usage_model_identity_missing')
    if rows:
        row=rows[model]
        if not isinstance(row,dict) or any(_uint(row.get(k))!=numbers[k] for k in wire):
            raise GrokCLIUnavailable('wire_model_usage_inconsistent')
        if row.get('costUsdTicks') is not None:_uint(row['costUsdTicks'],'provider_cost_invalid')
        if 'costIsPartial' in row and type(row['costIsPartial']) is not bool:
            raise GrokCLIUnavailable('wire_usage_flag_invalid')
    partial=value.get('costIsPartial',False) or (rows and rows[model].get('costIsPartial',False))
    ticks=value.get('costUsdTicks')
    if ticks is not None:_uint(ticks,'provider_cost_invalid')
    if rows and not partial and rows[model].get('costUsdTicks') != ticks:
        raise GrokCLIUnavailable('wire_model_cost_inconsistent')
    if partial:ticks=None
    price=float(Decimal(ticks)/Decimal(10000000000)) if ticks is not None else None
    raw={'input_tokens':numbers['inputTokens'],'cached_input_tokens':numbers['cachedReadTokens'],
         'output_tokens':numbers['outputTokens'],'reasoning_tokens':numbers['reasoningTokens'],
         'provider_cost_usd':price,'provider_cost_verified':False,'cost_is_partial':bool(partial)}
    return {'raw':raw,'semantics':{'scope':'whole_turn','cache':'included','reasoning':'included',
                                 'cumulative':cumulative,'price_evidence_verified':False},
            'audit':{'schema':'GrokCLIWireUsageV1','model_id':model,'provider_prompt_id':None,'input_tokens':numbers['inputTokens'],
                'output_tokens':numbers['outputTokens'],'cached_read_tokens':numbers['cachedReadTokens'],
                'cache_creation_tokens':numbers['cacheCreationTokens'],
                'noncached_noncreation_input_tokens':numbers['inputTokens']-numbers['cachedReadTokens']-numbers['cacheCreationTokens'],
                'reasoning_tokens':numbers['reasoningTokens'],'model_calls':numbers['modelCalls'],
                'num_turns':turns,'api_duration_ms':numbers['apiDurationMs'],'cost_usd_ticks':ticks,
                'cost_is_partial':bool(partial),'cumulative':cumulative,
                'provider_cost_authority':'unverified_marginal_charge','mapping_sha256':MAPPING_SHA256}}


def decode_billing(value):
    if not isinstance(value,dict) or not isinstance(value.get('config'),(dict,type(None))):
        raise GrokCLIUnavailable('billing_shape_unsupported')
    config=value.get('config') or {}
    percent=config.get('creditUsagePercent')
    if percent is not None and (type(percent) not in (int,float) or not 0 <= percent <= 1000000):
        raise GrokCLIUnavailable('billing_percentage_invalid')
    def cents(key):
        v=config.get(key)
        if v is None:return None
        if not isinstance(v,dict) or set(v)-{'val'}:
            raise GrokCLIUnavailable('billing_money_invalid')
        # Reference proto3 JSON Cent{} means an explicitly present zero amount.
        return _uint(v.get('val',0),'billing_money_invalid')
    enabled=value.get('on_demand_enabled')
    unified=config.get('isUnifiedBillingUser')
    if any(x is not None and type(x) is not bool for x in (enabled,unified)):
        raise GrokCLIUnavailable('billing_flag_invalid')
    period=config.get('currentPeriod')
    if period is not None:
        if not isinstance(period,dict) or set(period)-{'type','start','end'}:
            raise GrokCLIUnavailable('billing_period_invalid')
        for key,text in period.items():
            if text is not None and (not isinstance(text,str) or not 0 < len(text) <= 100):
                raise GrokCLIUnavailable('billing_period_invalid')
    return {'schema':'GrokCLIBillingObservationV1','credit_usage_percent':percent,
            'on_demand_enabled':enabled,'is_unified_billing_user':unified,
            'amounts_usd_cents':{k:cents(k) for k in ('monthlyLimit','used','onDemandCap','onDemandUsed','prepaidBalance')},
            'current_period':deepcopy(period),
            'spending_authorized':False,'entitlement_verified':False,'zero_marginal_charge_proved':False}


class MappedUsageReconciler:
    """Additive exact wire fingerprints around the unchanged V1 token ledger.

    V1 readers refuse the new envelope. No prior event/hash or accounting state
    is edited or silently migrated. Callers persist this state through host journals.
    """
    def __init__(self, state=None):
        from .grok_cli import UsageReconciler
        self.state=deepcopy(state) if state is not None else {'schema':'GrokCLIWireAccountingV1',
            'mapping_sha256':MAPPING_SHA256,'accounting_v1':UsageReconciler().state,'wire_seen':{},'provider_seen':{}}
        if (not isinstance(self.state,dict) or set(self.state)!={'schema','mapping_sha256','accounting_v1','wire_seen','provider_seen'}
                or self.state['schema']!='GrokCLIWireAccountingV1' or self.state['mapping_sha256']!=MAPPING_SHA256
                or any(not isinstance(self.state[k],dict) or len(self.state[k])>UsageReconciler.MAX_RECORDS for k in ('wire_seen','provider_seen'))):
            raise GrokCLIUnavailable('wire_accounting_schema_unsupported')
        inner=UsageReconciler(deepcopy(self.state['accounting_v1']))
        if set(self.state['wire_seen']) != set(inner.state['seen']) or any(not _digest(v) for v in self.state['wire_seen'].values()):
            raise GrokCLIUnavailable('wire_accounting_state_inconsistent')
        if len(self.state['provider_seen']) != len(inner.state['seen']) or set(self.state['provider_seen'].values()) != set(inner.state['seen']):
            raise GrokCLIUnavailable('wire_accounting_state_inconsistent')
        for key,receipt_key in self.state['provider_seen'].items():
            try:parts=json.loads(key)
            except (TypeError,ValueError):raise GrokCLIUnavailable('wire_accounting_state_inconsistent')
            if not isinstance(parts,list) or len(parts)!=3 or any(not isinstance(v,str) or not 0 < len(v) <= 200 for v in parts) or parts[:2]!=json.loads(receipt_key)[:2]:
                raise GrokCLIUnavailable('wire_accounting_state_inconsistent')

    def reconcile(self, observation, audit):
        from .grok_cli import UsageReconciler
        keys={'schema','model_id','provider_prompt_id','input_tokens','output_tokens','cached_read_tokens','cache_creation_tokens',
              'noncached_noncreation_input_tokens','reasoning_tokens','model_calls','num_turns','api_duration_ms',
              'cost_usd_ticks','cost_is_partial','cumulative','provider_cost_authority','mapping_sha256'}
        if (not isinstance(audit,dict) or set(audit)!=keys or audit['schema']!='GrokCLIWireUsageV1'
                or audit['mapping_sha256']!=MAPPING_SHA256 or audit['provider_cost_authority']!='unverified_marginal_charge'
                or not isinstance(audit['model_id'],str) or not 0 < len(audit['model_id']) <= 200
                or not isinstance(audit['provider_prompt_id'],str) or not 0 < len(audit['provider_prompt_id']) <= 200
                or type(audit['cost_is_partial']) is not bool or type(audit['cumulative']) is not bool):
            raise GrokCLIUnavailable('wire_accounting_observation_invalid')
        for k in ('input_tokens','output_tokens','cached_read_tokens','cache_creation_tokens',
                  'noncached_noncreation_input_tokens','reasoning_tokens','model_calls','num_turns','api_duration_ms'):
            _uint(audit[k],'wire_accounting_observation_invalid')
        ticks=audit['cost_usd_ticks']
        if ticks is not None:_uint(ticks,'wire_accounting_observation_invalid')
        if audit['cost_is_partial'] and ticks is not None:
            raise GrokCLIUnavailable('wire_accounting_observation_invalid')
        if (audit['input_tokens']!=observation.input_tokens or audit['output_tokens']!=observation.output_tokens
                or audit['cached_read_tokens']!=observation.cached_input_tokens or audit['reasoning_tokens']!=observation.reasoning_tokens
                or audit['cumulative']!=observation.cumulative or observation.provider_cost_verified is not False
                or audit['noncached_noncreation_input_tokens']+audit['cache_creation_tokens']+audit['cached_read_tokens']!=audit['input_tokens']
                or observation.provider_cost_usd != (float(Decimal(ticks)/Decimal(10000000000)) if ticks is not None else None)):
            raise GrokCLIUnavailable('wire_accounting_observation_invalid')
        key=json.dumps([observation.process_epoch,observation.session_id,observation.request_id])
        provider_key=json.dumps([observation.process_epoch,observation.session_id,audit['provider_prompt_id']])
        if provider_key in self.state['provider_seen'] and self.state['provider_seen'][provider_key]!=key:
            raise GrokCLIUnavailable('duplicate_provider_prompt_conflict')
        fingerprint=hashlib.sha256(_canonical(audit)).hexdigest()
        prior=self.state['wire_seen'].get(key)
        if prior is not None and prior!=fingerprint:
            raise GrokCLIUnavailable('duplicate_wire_usage_conflict')
        trial=UsageReconciler(deepcopy(self.state['accounting_v1']))
        result=trial.reconcile(observation)
        self.state['accounting_v1']=trial.state
        self.state['wire_seen'][key]=fingerprint
        self.state['provider_seen'][provider_key]=key
        return result
