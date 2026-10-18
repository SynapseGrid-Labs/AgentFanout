"""Source-shaped wire fixtures; never installed CLI or account conformance."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from agentfanout.grok_abi import (CORE_METHODS, MAPPING_SHA256, WireBinding, ReferenceWireMapping,
    MappedUsageReconciler, decode_usage, decode_billing, verify_auth, selection,
    validate_isolated_auth_profile, isolated_auth_toml)
from agentfanout.grok_cli import ACPClient, GrokCLIUnavailable, HostAuthorization, normalize_usage, UsageReconciler
from tests.test_grok_cli import evidence, FakeTransport
from tests import test_grok_cli_hosted as hosted_fixture
PacketTransport=hosted_fixture.PacketTransport


def binding():
    return WireBinding('GrokACPWireBindingV1','a'*64,MAPPING_SHA256,CORE_METHODS,'fixture-wire-proof')


def options(model='grok-fixture', effort='low'):
    return [{'id':'model','type':'select','currentValue':model,'options':[{'value':m} for m in ['grok-fixture','initial-model']]},
            {'id':'reasoning_effort','type':'select','currentValue':effort,'options':[{'value':v} for v in ['low','high']]}]


def usage(zero=False, ticks=50000000):
    n={'inputTokens':0 if zero else 35,'outputTokens':0 if zero else 13,'totalTokens':0 if zero else 48,
       'cachedReadTokens':0 if zero else 6,'cacheCreationTokens':0 if zero else 4,
       'reasoningTokens':0 if zero else 3,'modelCalls':0 if zero else 2,'apiDurationMs':0 if zero else 12}
    if not zero and ticks is not None:n['costUsdTicks']=ticks
    return {**n,'numTurns':0 if zero else 2,'modelUsage':{} if zero else {'grok-fixture':deepcopy(n)}}


class WireTransport(PacketTransport):
    def __init__(self,index=1,role='worker',partial=False):
        super().__init__(index,role,partial)
        self.model='grok-fixture';self.effort='low';self.prompted=False
        self.modify=lambda m,r:r

    def send(self, frame):
        if 'method' not in frame:
            self.sent.append(frame);return
        method=frame['method'].removeprefix('_')
        if method=='session/prompt':
            self.prompted=True
            try:json.loads(frame['params']['prompt'][0]['text'])
            except ValueError:FakeTransport.send(self,frame)
            else:super().send(frame)
            self.queue[-1]['result']=self.modify(method,{'stopReason':'end_turn','_meta':{
                'sessionId':self.session,'modelId':self.model,'requestId':'fixture-prompt-'+self.session,
                'promptId':'fixture-prompt-'+self.session,'usage':usage(),'inputTokens':1,'outputTokens':1}})
            self.queue.insert(0,{'jsonrpc':'2.0','method':'session/update','params':{
                'sessionId':self.session,'update':{'sessionUpdate':'agent_thought_chunk',
                    'content':{'type':'text','text':'private fixture reasoning'}}}})
            return
        self.sent.append(frame)
        if method=='initialize':result={'protocolVersion':1,'authMethods':[{'id':'cached_token'}]}
        elif method=='authenticate':result={'_meta':{'auth_mode':'SessionToken','backend_billed':False,'gate':None}}
        elif method=='x.ai/auth/info':result={'methodId':'cached_token','email':'not-retained@example.invalid'}
        elif method=='session/new':result={'sessionId':self.session,'models':{'currentModelId':self.model},'configOptions':options(self.model,self.effort)}
        elif method=='session/set_config_option':
            key=frame['params']['configId'];value=frame['params']['value']
            if key=='model':self.model=value
            else:self.effort=value
            result={'configOptions':options(self.model,self.effort)}
        elif method=='x.ai/session/usage':result={'usage':usage(zero=not self.prompted)}
        elif method=='x.ai/billing':result={'config':{'creditUsagePercent':22,'onDemandCap':{}},'on_demand_enabled':False}
        else:raise AssertionError('unexpected method '+method)
        self.queue.append({'jsonrpc':'2.0','id':frame['id'],'result':self.modify(method,result)})


class WireTests(unittest.TestCase):
    def client(self,t=None,b=None,verified=True):
        t=t or WireTransport();journal=[]
        wire=ReferenceWireMapping(b or binding(),lambda x:verified)
        client=ACPClient(t,evidence(),lambda x:True,lambda k,v:(journal.append((k,v)),True)[1],wire_mapping=wire)
        return client,t,journal

    def prepare(self,c):return c.prepare('task_1','bounded','worker','/empty')

    def test_observed_fresh_bootstrap_is_not_cached_auth_admission(self):
        observed=json.loads((Path(__file__).parent/'fixtures/grok-bootstrap-installed.json').read_text())
        t=WireTransport()
        t.modify=lambda method,result:deepcopy(observed['initialize']['result']) if method=='initialize' else result
        c,_,_=self.client(t)
        with self.assertRaisesRegex(GrokCLIUnavailable,'cached_session_auth_unavailable'):
            self.prepare(c)
        self.assertEqual([f.get('method') for f in t.sent],['initialize'])
        self.assertEqual(observed['initialize']['result']['authMethods'][0]['id'],'grok.com')
        self.assertFalse(observed['admission_allowed'])

    def test_observed_global_mcp_notification_remains_unadmitted(self):
        observed=json.loads((Path(__file__).parent/'fixtures/grok-bootstrap-installed.json').read_text())
        class BootstrapTransport(WireTransport):
            def send(self,frame):
                super().send(frame)
                if frame.get('method')=='initialize':
                    self.queue.insert(0,deepcopy(observed['notifications'][0]))
        c,t,_=self.client(BootstrapTransport())
        with self.assertRaisesRegex(GrokCLIUnavailable,'unexpected_notification'):
            self.prepare(c)
        self.assertFalse(t.prompted)

    def test_binary_mapping_and_host_evidence_required_before_any_request(self):
        t=WireTransport()
        for b in [replace(binding(),binary_sha256='b'*64),replace(binding(),mapping_sha256='b'*64),replace(binding(),methods=CORE_METHODS-{'x.ai/auth/info'})]:
            with self.assertRaises(GrokCLIUnavailable):self.client(t,b)
        with self.assertRaisesRegex(GrokCLIUnavailable,'installed_wire_binding_unverified'):self.client(t,verified=False)
        self.assertEqual(t.sent,[])

    def test_auth_readback_rejects_api_expired_and_wrong_metadata(self):
        for auth,info in [({}, {'methodId':'cached_token'}),({'_meta':{'auth_mode':'ApiKey','backend_billed':False}}, {'methodId':'cached_token'}),
            ({'_meta':{'auth_mode':'SessionToken','backend_billed':False}}, {'methodId':'xai.api_key'}),
            ({'_meta':{'auth_mode':'SessionToken','backend_billed':True}}, {'methodId':'cached_token'})]:
            with self.assertRaisesRegex(GrokCLIUnavailable,'subscription_auth_not_confirmed'):verify_auth(auth,info)

    def test_explicit_profile_pin_and_override_rejection(self):
        import tomllib
        config=tomllib.loads(isolated_auth_toml());self.assertEqual(validate_isolated_auth_profile(config),config)
        for bad in [dict(config,models={'other':{'api_key':'synthetic'}}),{'auth':{'preferred_method':'api_key','disable_api_key_auth':True}},
                    {'auth':{**config['auth'],'auth_provider_command':'synthetic-command'}},dict(config,provider='API')]:
            with self.assertRaises(GrokCLIUnavailable):validate_isolated_auth_profile(bad)

    def test_api_auth_advertised_under_pin_refuses_before_authentication(self):
        t=WireTransport();t.modify=lambda m,r:{**r,'authMethods':[{'id':'cached_token'},{'id':'xai.api_key'}]} if m=='initialize' else r
        c,_,_=self.client(t)
        with self.assertRaisesRegex(GrokCLIUnavailable,'api_auth_advertised'):self.prepare(c)
        self.assertFalse(any(x.get('method')=='authenticate' for x in t.sent))

    def test_expired_cached_auth_error_with_synthetic_ambient_key_never_falls_back(self):
        from agentfanout.grok_cli import isolated_environment
        self.assertNotIn('XAI_API_KEY',isolated_environment({'XAI_API_KEY':'fixture-api-key','PATH':'/usr/bin'}))
        t=WireTransport();original=t.send
        def send(frame):
            original(frame)
            if frame.get('method')=='authenticate':t.queue[-1]={'jsonrpc':'2.0','id':frame['id'],'error':{'code':-32000,'message':'expired'}}
        t.send=send;c,_,_=self.client(t)
        with self.assertRaises(GrokCLIUnavailable):self.prepare(c)
        self.assertEqual([x['params']['methodId'] for x in t.sent if x.get('method')=='authenticate'],['cached_token'])
        self.assertFalse(t.prompted)

    def test_actual_option_readback_and_setter_acknowledgement(self):
        t=WireTransport();t.model='initial-model';t.effort='high'
        c,_,j=self.client(t);p=self.prepare(c)
        self.assertEqual(p.schema,'GrokCLISessionV2');self.assertEqual(p.wire_mapping_sha256,MAPPING_SHA256)
        self.assertEqual((p.model,p.effort),('grok-fixture','low'))
        setters=[x['params'] for x in t.sent if x.get('method')=='session/set_config_option']
        self.assertEqual([x['configId'] for x in setters],['model','reasoning_effort'])
        self.assertEqual(j[0][0],'wire_session_prepared')
        self.assertNotIn('not-retained',json.dumps(j))

    def test_wrong_model_acknowledgement_never_prompts(self):
        t=WireTransport();t.model='initial-model';t.modify=lambda m,r:{'configOptions':options('initial-model')} if m=='session/set_config_option' else r
        c,_,_=self.client(t)
        with self.assertRaisesRegex(GrokCLIUnavailable,'effective_selection_mismatch'):self.prepare(c)
        self.assertFalse(t.prompted)

    def test_missing_duplicated_and_unlisted_effort_options_refuse(self):
        for v in [options()[:1],options()+options(),options(effort='unsupported')]:
            with self.assertRaises(GrokCLIUnavailable):selection(v)

    def test_nonzero_bootstrap_usage_blocks_authorization(self):
        t=WireTransport();t.modify=lambda m,r:{'usage':usage()} if m=='x.ai/session/usage' else r
        c,_,_=self.client(t)
        with self.assertRaisesRegex(GrokCLIUnavailable,'bootstrap_inference_observed'):self.prepare(c)
        self.assertFalse(t.prompted)

    def test_explicit_billing_probe_not_automatic_or_spending_authority(self):
        c,t,_=self.client(b=replace(binding(),methods=CORE_METHODS|{'x.ai/billing'}));self.prepare(c)
        self.assertNotIn('_x.ai/billing',[x.get('method') for x in t.sent])
        with self.assertRaises(GrokCLIUnavailable):c.wire_mapping.billing(c,lambda *a:False)
        v=c.wire_mapping.billing(c,lambda *a:True)
        self.assertEqual(v['amounts_usd_cents']['onDemandCap'],0)
        self.assertFalse(v['spending_authorized']);self.assertFalse(v['zero_marginal_charge_proved'])

    def test_billing_unsupported_is_typed_and_never_uses_http_or_export_methods(self):
        c,t,_=self.client();self.prepare(c)
        with self.assertRaisesRegex(GrokCLIUnavailable,'billing_extension_unproved'):c.wire_mapping.billing(c,lambda *a:True)
        with self.assertRaises(GrokCLIUnavailable):c._request('x.ai/auth/getBearerToken',{})
        self.assertFalse(any('Bearer' in x.get('method','') for x in t.sent))


    def test_config_drift_and_callback_during_prompt_quarantine(self):
        for kind in ('callback','model-drift'):
            c,t,j=self.client();prepared=self.prepare(c);original=t.send
            def send(frame):
                original(frame)
                if frame.get('method')=='session/prompt':
                    event={'jsonrpc':'2.0','id':999,'method':'fs/read_text_file','params':{}} if kind=='callback' else {
                        'jsonrpc':'2.0','method':'session/update','params':{'sessionId':t.session,
                        'update':{'sessionUpdate':'config_option_update','configOptions':options('initial-model')}}}
                    t.queue.insert(0,event)
            t.send=send
            with self.assertRaises(GrokCLIUnavailable):c.execute('bounded',HostAuthorization('receipt',prepared,.1),lambda a:True)
            self.assertTrue(c.uncertain)
            self.assertTrue(any(k=='inference_effect_uncertain' for k,v in j))

    def test_attested_session_totals_only_never_uses_last_call_fields(self):
        c,t,j=self.client(b=replace(binding(),usage_surface='session_totals_only'));prepared=self.prepare(c)
        def modify(method,result):
            if method=='session/prompt':
                result=deepcopy(result);result['_meta'].pop('usage')
            return result
        t.modify=modify
        r=c.execute('bounded',HostAuthorization('receipt',prepared,.1),lambda a:True)
        self.assertTrue(r['mapped_usage']['semantics']['cumulative'])
        self.assertEqual(r['mapped_usage']['raw']['input_tokens'],35)
        self.assertEqual(r['mapped_usage']['audit']['model_calls'],2)

    def test_required_whole_turn_metadata_cannot_silently_fall_into_session_only(self):
        c,t,j=self.client();prepared=self.prepare(c)
        t.modify=lambda m,r:{**r,'_meta':{k:v for k,v in r['_meta'].items() if k!='usage'}} if m=='session/prompt' else r
        with self.assertRaisesRegex(GrokCLIUnavailable,'whole_turn_usage_unavailable'):
            c.execute('bounded',HostAuthorization('receipt',prepared,.1),lambda a:True)
        self.assertTrue(c.uncertain)

    def test_extension_wire_prefix_and_concurrent_session_isolation(self):
        from concurrent.futures import ThreadPoolExecutor
        shared=ReferenceWireMapping(binding(),lambda b:True)
        def run(index):
            t=WireTransport(index=index);journal=[]
            c=ACPClient(t,evidence(),lambda e:True,lambda k,v:(journal.append((k,v)),True)[1],wire_mapping=shared)
            p=self.prepare(c);r=c.execute('bounded',HostAuthorization('receipt-'+str(index),p,.1),lambda a:True)
            self.assertIn('_x.ai/auth/info',[f.get('method') for f in t.sent])
            self.assertIn('_x.ai/session/usage',[f.get('method') for f in t.sent])
            self.assertNotIn('x.ai/auth/info',[f.get('method') for f in t.sent])
            return p.session_id,r['mapped_usage']['audit']['provider_prompt_id']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[1,2]))
        self.assertEqual(len(set(results)),2)

    def test_prompt_identity_and_session_counter_conflict_refuse(self):
        c,t,j=self.client();prepared=self.prepare(c)
        t.modify=lambda m,r:{**r,'_meta':{**r['_meta'],'sessionId':'foreign-session'}} if m=='session/prompt' else r
        with self.assertRaisesRegex(GrokCLIUnavailable,'prompt_identity_readback_conflict'):
            c.execute('bounded',HostAuthorization('receipt',prepared,.1),lambda a:True)
        self.assertTrue(c.uncertain)

class UsageMappingTests(unittest.TestCase):
    def test_cache_creation_whole_turn_reasoning_and_exact_ticks(self):
        mapped=decode_usage(usage(),'grok-fixture',cumulative=False)
        self.assertEqual(mapped['raw']['input_tokens'],35);self.assertEqual(mapped['audit']['noncached_noncreation_input_tokens'],25)
        self.assertEqual(mapped['audit']['cost_usd_ticks'],50000000)
        self.assertEqual(mapped['raw']['provider_cost_usd'],.005);self.assertFalse(mapped['raw']['provider_cost_verified'])

    def test_missing_cost_stays_unknown_and_partial_bill_never_becomes_zero(self):
        for raw in [usage(ticks=None),{**usage(),'costIsPartial':True}]:
            v=decode_usage(raw,'grok-fixture',cumulative=False)
            self.assertIsNone(v['raw']['provider_cost_usd']);self.assertIsNone(v['audit']['cost_usd_ticks'])

    def test_missing_counters_bad_flags_model_drift_and_sum_conflict_refuse(self):
        samples=[]
        for field in ('cachedReadTokens','cacheCreationTokens','reasoningTokens','modelCalls'):
            v=usage();v.pop(field);samples.append(v)
        samples += [{**usage(),'usageIsIncomplete':True},{**usage(),'costIsPartial':'false'},
                    {**usage(),'modelUsage':{'other-model':usage()['modelUsage']['grok-fixture']}},
                    {**usage(),'inputTokens':1}]
        for raw in samples:
            with self.subTest(raw=raw),self.assertRaises(GrokCLIUnavailable):decode_usage(raw,'grok-fixture',cumulative=False)

    def test_last_call_only_metadata_cannot_be_normalized_as_whole_turn(self):
        with self.assertRaises(GrokCLIUnavailable):decode_usage({'prompt_tokens':1,'completion_tokens':2},'grok-fixture',cumulative=False)

    def test_exact_tick_dedup_restart_conflict_and_v1_reader_refusal(self):
        def obs(ticks):
            m=decode_usage(usage(ticks=ticks),'grok-fixture',cumulative=False)
            m['audit']['provider_prompt_id']='fixture-prompt'
            return normalize_usage(m['raw'],process_epoch='p',session_id='s',request_id='r',semantics=m['semantics']),m['audit']
        r=MappedUsageReconciler();o,a=obs(2**62);self.assertFalse(r.reconcile(o,a)['duplicate'])
        recovered=MappedUsageReconciler(r.state);self.assertTrue(recovered.reconcile(o,a)['duplicate'])
        prior=deepcopy(recovered.state);o2,a2=obs(2**62+1)
        self.assertEqual(o.provider_cost_usd,o2.provider_cost_usd)
        with self.assertRaisesRegex(GrokCLIUnavailable,'duplicate_wire_usage_conflict'):recovered.reconcile(o2,a2)
        self.assertEqual(recovered.state,prior)
        with self.assertRaises(GrokCLIUnavailable):UsageReconciler(r.state)
        with self.assertRaises(GrokCLIUnavailable):MappedUsageReconciler(UsageReconciler().state)

    def test_billing_missing_on_demand_unified_and_prepaid_are_unknown(self):
        b=decode_billing({'config':None});self.assertIsNone(b['on_demand_enabled']);self.assertIsNone(b['is_unified_billing_user'])
        self.assertTrue(all(x is None for x in b['amounts_usd_cents'].values()))
        self.assertFalse(b['entitlement_verified'])


    def test_same_provider_prompt_cannot_charge_a_second_host_receipt(self):
        mapped=decode_usage(usage(),'grok-fixture',cumulative=False);mapped['audit']['provider_prompt_id']='same-prompt'
        o=normalize_usage(mapped['raw'],process_epoch='p',session_id='s',request_id='r1',semantics=mapped['semantics'])
        r=MappedUsageReconciler();r.reconcile(o,mapped['audit']);prior=deepcopy(r.state)
        with self.assertRaisesRegex(GrokCLIUnavailable,'duplicate_provider_prompt_conflict'):
            r.reconcile(replace(o,request_id='r2'),mapped['audit'])
        self.assertEqual(prior,r.state)

    def test_corrupted_wire_recovery_index_refuses(self):
        mapped=decode_usage(usage(),'grok-fixture',cumulative=False);mapped['audit']['provider_prompt_id']='q'
        o=normalize_usage(mapped['raw'],process_epoch='p',session_id='s',request_id='r',semantics=mapped['semantics'])
        r=MappedUsageReconciler();r.reconcile(o,mapped['audit']);state=deepcopy(r.state)
        state['provider_seen'][json.dumps(['other-epoch','s','q'])]=state['provider_seen'].pop(json.dumps(['p','s','q']))
        with self.assertRaisesRegex(GrokCLIUnavailable,'wire_accounting_state_inconsistent'):MappedUsageReconciler(state)

class HostedWireTests(unittest.TestCase):
    setUp=hosted_fixture.HostedACPTests.setUp
    tearDown=hosted_fixture.HostedACPTests.tearDown
    append=hosted_fixture.HostedACPTests.append
    events=hosted_fixture.HostedACPTests.events
    authorize=hosted_fixture.HostedACPTests.authorize
    durable_check=hosted_fixture.HostedACPTests.durable_check
    # Inherit the proven host-ledger/residual harness; new tests select an explicit
    # source-shaped wire mapping, never a production transport or identity proof.
    def adapters(self,*args,**kwargs):
        worker,validator=hosted_fixture.HostedACPTests.adapters(self,*args,**kwargs)
        def factory(role,task,ev):
            count=sum(s.role=='worker' for s in self.sessions)
            t=WireTransport(len(self.sessions)+1,role,partial=role=='worker' and count==0)
            self.sessions.append(t);return t
        for adapter in (worker,validator):
            adapter.wire_mapping=ReferenceWireMapping(binding(),lambda b:True)
            adapter._usage=MappedUsageReconciler()
            adapter.transport_factory=factory
        return worker,validator

    # Only these tests override the wire; inherited tests retain their original
    # prototype harness via a separate delegating adapter helper below.
    def test_source_wire_worker_validator_residual_usage_and_authorization(self):
        worker,validator=self.adapters();output=HostedFanoutRunner(self.state_file,self.ledger_file,
            {'GrokCLI':worker},validator,approved_providers=['GrokCLI']).run()
        self.assertEqual(output['status'],'complete',output)
        self.assertEqual(len(self.sessions),4)
        self.assertEqual(len({s.session for s in self.sessions}),4)
        events=self.events('grok_cli_wire_usage_observed');self.assertEqual(len(events),4)
        self.assertTrue(all(e['payload']['audit']['model_calls']==2 for e in events))
        self.assertEqual(len(self.events('grok_cli_session_authorized')),4)
        self.assertTrue(all(e['payload']['prepared']['schema']=='GrokCLISessionV2' for e in self.events('grok_cli_session_authorized')))
        self.assertGreater(output['state']['usage']['cost_usd'],0)
        self.assertNotIn('private fixture reasoning',json.dumps(events))

    def test_partial_usage_retains_numeric_reservation_and_cannot_restart(self):
        worker,validator=self.adapters();original=worker.transport_factory
        def factory(*a):
            t=original(*a)
            t.modify=lambda m,r:{**r,'_meta':{**r['_meta'],'usage':{**usage(),'usageIsIncomplete':True}}} if m=='session/prompt' else r
            return t
        worker.transport_factory=factory
        run=lambda:HostedFanoutRunner(self.state_file,self.ledger_file,{'GrokCLI':worker},validator,approved_providers=['GrokCLI']).run()
        output=run();self.assertEqual(output['status'],'blocked',output)
        self.assertGreater(output['state']['usage']['reserved_cost_usd'],0)
        observed=self.events('grok_cli_wire_usage_unaccepted')
        self.assertEqual(observed[0]['payload']['known_counters']['inputTokens'],35)
        self.assertFalse(observed[0]['payload']['usage_complete'])
        before=len(self.sessions);run();self.assertEqual(len(self.sessions),before)
        self.assertFalse(self.events('validator_result'))

from agentfanout.runner import HostedFanoutRunner

if __name__=='__main__':unittest.main()
