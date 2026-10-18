"""Real hosted dispatch with synthetic fixed coordinator transport; no GPU calls."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from agentfanout.coordinator_hosted import CoordinatorLocalWorkerAdapter, CoordinatorLocalValidatorAdapter
from agentfanout.coordinator_local import CoordinatorBroker, mixed_resource_from_reservation
from agentfanout.host_runtime import HostLedger
from agentfanout.lifecycle import build_task_units, initialize_run
from agentfanout.local_profiles import identity_digest, build_inference_request
from agentfanout.mixed_contracts import canonical_digest, freeze_contract
from agentfanout.mixed_state import save_hosted_state, wrap_hosted_state, load_hosted_state
from agentfanout.profiled_runner import ProfiledHostedRunner
from tests.test_hosted_runner import capability
from tests.test_local_profiles import configured
from tests.test_mixed_contracts import policy, runtime

class Transport:
    timeout_seconds = .1
    def __init__(self, model):
        self.model = model
        self.requests = {}
        self.calls = []
        self.unknown = False
        self.unknown_role = None
        self.bad_model = False
        self.hidden_tools = False
        self.partial_usage = False
        self.on_inference = None

    def call(self, args, raw):
        self.calls.append(args[0])
        if args[0] == 'fanout-inference':
            request = json.loads(raw)
            if self.on_inference:self.on_inference(request)
            packet = json.loads(request['payload']['messages'][0]['content'])
            task = packet['task']
            if request['role'] == 'worker':
                result = {'schema':'WorkerResultV1','result_id':task['expected_result_id'],'task_id':task['task_id'],
                    'launch_correlation_id':task['launch_correlation_id'],'status':'success','summary':'12',
                    'evidence':['2+4+6=12'],'remaining_work':[],'changed_files':[],'commands':[],'exit_codes':[]}
            else:
                result = {'schema':'ValidatorResultV1','task_id':task['task_id'],'validator_id':'fixture-validator',
                    'verdict':'pass','evidence_refs':['arithmetic checked'],'material_findings':[],
                    'contradictions':[],'checked_at':datetime.now(timezone.utc).isoformat()}
            usage = {'prompt_tokens':20,'completion_tokens':10,'total_tokens':30,
                'prompt_tokens_details':{'cached_tokens':3},'completion_tokens_details':{'reasoning_tokens':2}}
            if self.partial_usage:usage.pop('completion_tokens_details')
            message = {'content':json.dumps(result),'reasoning_content':'bounded arithmetic'}
            if self.hidden_tools:message['tool_calls']=[{'name':'shell'}]
            response = {'model':'wrong-alias' if self.bad_model else self.model['served_name'],
                'choices':[{'finish_reason':'stop','message':message}],'usage':usage}
            unknown = self.unknown or self.unknown_role == request['role']
            self.requests[request['request_id']] = {**request,'schema':'CoordinatorInferenceRequestV1',
                'serving_epoch':self.model['serving_epoch'],'stage':'running' if unknown else 'completed',
                'effects_absent':not unknown,'accounting_reconciled':not unknown,
                'response':response,'usage':None,'started':True,'cancel_requested':False}
            return {'request_id':request['request_id']}
        value = self.requests[args[1]]
        if args[0] == 'fanout-cancel':
            value.update(stage='cancelling',cancel_requested=True)
        return deepcopy(value)

class CoordinatorHostedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.chmod(self.tmp.name,0o700)
        self.path = Path(self.tmp.name)
        self.state_path = self.path/'state.json'
        self.ledger_path = self.path/'ledger.sqlite3'
        model, profile = configured()
        self.model,self.profile = model,profile
        tasks = build_task_units('Add bounded integers','bulk-read','explorer','Local','default','low',
            ['bounded arithmetic'],allowed_paths=[str(self.path)])
        self.state = initialize_run('Add bounded integers',tasks,{'max_total_cost_usd':3,'max_waves':2,'max_wave_size':1})
        task = self.state['tasks'][0]
        task['provider_model'].update(provider='Local',selector=model['logical_selector'],resolved_model=model['served_name'])
        task['budgets']['max_cost_usd']=.1
        self.transport = Transport(model)
        self.broker = CoordinatorBroker(self.transport,frozenset({model['logical_selector']}))
        self.evidence = {}
        self.adapters = {}
        bindings = {}
        for role,cls,cost in [('worker',CoordinatorLocalWorkerAdapter,.1),('validator',CoordinatorLocalValidatorAdapter,.2)]:
            record = {'schema':'CoordinatorRoleReservationV1','run_id':self.state['run_id'],
                'assignment_id':task['task_id'],'state':'reserved','role':role,'reservation_id':str(uuid.uuid4()),
                'identity':model,'identity_sha256':identity_digest(model),'owner_token':'owner',
                'fencing_token':'fence','serving_epoch':model['serving_epoch'],'expires_at':time.time()+300,
                'cancellation_state':'none','coordinator_attestation_sha256':'f'*64,'reservation_cost_usd':cost}
            observation = {'identity':model,'owner_token':'owner','fencing_token':'fence','attestation_sha256':'f'*64,
                'ready':True,'certain':True,'observed_at':time.time()}
            body=build_inference_request(model,profile,'fixture')
            observation['request_controls']={k:v for k,v in body.items() if k not in {'model','messages','stream'}}
            observation['schema_sha256']={r:canonical_digest(json.loads((Path(__file__).resolve().parents[1]/'schemas'/ (name+'.schema.json')).read_text())) for r,name in [('worker','WorkerResultV1'),('validator','ValidatorResultV1')]}
            self.evidence[role] = {'reservation':record,'observation':observation,'profile':profile}
            adapter = cls(broker=self.broker,adapter_identity={'adapter_id':'synthetic-coordinator-'+role,
                'adapter_version':'1','local_identity_sha256':identity_digest(model),'executable_sha256':'f'*64,'input_modes':['bounded_file_text']},
                reservation_observer=lambda t,r:deepcopy(self.evidence[r]),host_verify=lambda e,r:True,
                capability_factory=lambda t,p,e:capability('Local',model['served_name'],t,p),
                authorize=self.authorize,journal=self.journal,
                bounded_task_factory=lambda t:{**t,'bounded_input':{'content':'Add 2, 4, and 6.'}},timeout_seconds=.01)
            self.adapters[role] = adapter
            bindings[role] = {'provider':'Local','mode':'coordinator_local','model_selector':model['logical_selector'],
                'resolved_model':model['served_name'],'reasoning_effort':'low','identity_sha256':canonical_digest(adapter.identity()),
                'resource':mixed_resource_from_reservation(record,observation,role=role),
                'reservation_cost_usd':cost,'destination_id':role+'.local'}
        controls = {'coordinator_local':True,'mixed_provider_runs':True,'grok_cli_subscription':False}
        self.contract = freeze_contract(self.state['run_id'],runtime(),[{'assignment_id':task['task_id'],
            **bindings,'validation_required':True,'egress_policy':policy(True)}],feature_controls=controls)
        save_hosted_state(self.state_path,wrap_hosted_state(self.state,self.contract,runtime()),runtime())
        self.kwargs = dict(contract=self.contract,runtime_identity=runtime(),
            worker_bindings={canonical_digest(bindings['worker']):self.adapters['worker']},
            validator_bindings={canonical_digest(bindings['validator']):self.adapters['validator']},
            host_conformance_check=lambda b:True,
            observe_resources=lambda a:{r:b['resource'] for r,b in bindings.items()},admission_controls=controls,
            host_destination='parent.host',parent_destination='parent.host')
        self.authorizations = []

    def tearDown(self):self.tmp.cleanup()

    def runner(self):return ProfiledHostedRunner(self.state_path,self.ledger_path,{},None,**self.kwargs)

    def authorize(self,task,role,receipt,operation,digest):
        ledger = HostLedger(self.ledger_path)
        try:
            event = 'launch_receipt' if role == 'worker' else 'validator_attempt_started'
            self.assertTrue(any(e['payload'] == receipt for e in ledger.events(self.state['run_id'],event)))
            ledger.append(self.state['run_id'],'fixture_host_effect_authorized',
                {'operation':operation,'request_id':receipt['coordinator_hosted_boundary']['request_id'],
                 'payload_sha256':digest,'role':role},task['task_id'])
        finally:ledger.close()
        self.authorizations.append((role,operation))
        return True

    def journal(self,task,role,kind,data):
        ledger=HostLedger(self.ledger_path)
        try:ledger.append(self.state['run_id'],kind,data,task['task_id'])
        finally:ledger.close()
        return True

    def events(self,kind):
        ledger=HostLedger(self.ledger_path)
        try:return ledger.events(self.state['run_id'],kind)
        finally:ledger.close()

    def test_hosted_worker_validator_distinct_durable_receipts_and_numeric_accounting(self):
        output=self.runner().run()
        self.assertEqual(output['status'],'complete',output)
        self.assertEqual(self.authorizations,[('worker','fanout-inference'),('validator','fanout-inference')])
        worker=self.events('launch_receipt')[0]['payload']['coordinator_hosted_boundary']
        validator=self.events('validator_attempt_started')[0]['payload']['coordinator_hosted_boundary']
        self.assertNotEqual(worker['request_id'],validator['request_id'])
        self.assertNotEqual(worker['reservation']['reservation_id'],validator['reservation']['reservation_id'])
        state=load_hosted_state(self.state_path,runtime())['legacy_state']
        self.assertAlmostEqual(state['usage']['cost_usd'],.3)
        self.assertEqual(state['usage']['input_tokens'],40)
        self.assertEqual(len(self.events('validator_launch_receipt')),1)
        before=list(self.transport.calls)
        self.assertEqual(self.runner().run()['status'],'complete')
        self.assertEqual(self.transport.calls,before)

    def test_unknown_remote_cancellation_blocks_restart_without_replay_or_validator(self):
        self.transport.unknown=True
        output=self.runner().run()
        self.assertEqual(output['status'],'blocked',output)
        self.assertEqual(self.transport.calls.count('fanout-inference'),1)
        self.assertEqual(self.transport.calls.count('fanout-cancel'),1)
        self.assertEqual(len(self.events('coordinator_request_reconciliation_required')),1)
        self.assertEqual(len(self.events('validator_attempt_started')),0)
        before=list(self.transport.calls)
        self.runner().run()
        self.assertEqual(self.transport.calls,before)
        self.assertFalse(self.events('controller_terminal'))

    def test_wrong_validator_role_blocks_validator_effect(self):
        self.evidence['validator']['reservation']['role']='worker'
        output=self.runner().run()
        self.assertEqual(output['status'],'blocked')
        self.assertEqual(self.transport.calls.count('fanout-inference'),1)
        self.assertFalse(self.events('validator_attempt_started'))

    def test_legacy_receipt_fields_cannot_admit_coordinator_local(self):
        original=self.adapters['worker'].prepare
        def tampered(*args):
            p=original(*args);p.pop('coordinator_hosted_boundary');p['local_no_tool_boundary']={'schema':'LocalNoToolBoundaryV1'};return p
        self.adapters['worker'].prepare=tampered
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertEqual(self.transport.calls,[])

    def test_host_authorization_denial_never_dispatches(self):
        self.adapters['worker'].authorize=lambda *a:False
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertEqual(self.transport.calls,[])

    def test_wrong_served_alias_is_not_success(self):
        self.transport.bad_model=True
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertFalse(self.events('accepted_result_packet'))

    def test_hidden_tools_rejected(self):
        self.transport.hidden_tools=True
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertFalse(self.events('accepted_result_packet'))

    def test_missing_cache_or_reasoning_observation_never_fabricates_zero(self):
        self.transport.partial_usage=True
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertFalse(self.events('accepted_result_packet'))
        state=load_hosted_state(self.state_path,runtime())['legacy_state']
        self.assertGreaterEqual(state['usage']['reserved_cost_usd'],.1)

    def test_stale_observation_blocks_all_effects(self):
        self.evidence['worker']['observation']['observed_at']=0
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertEqual(self.transport.calls,[])

    def test_unavailable_required_validator_conformance_blocks_worker(self):
        self.kwargs['host_conformance_check']=lambda b:b['destination_id']!='validator.local'
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertEqual(self.transport.calls,[])

    def test_frozen_numeric_role_reservation_cannot_be_erased(self):
        self.evidence['worker']['reservation']['reservation_cost_usd']=0
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertEqual(self.transport.calls,[])

    def test_installed_schema_and_control_drift_block_before_effect(self):
        self.evidence['worker']['observation']['request_controls']['max_tokens']=1
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertEqual(self.transport.calls,[])

    def test_lost_inference_acknowledgement_cannot_replay_after_restart(self):
        def lost(request):raise RuntimeError('synthetic lost acknowledgement')
        self.transport.on_inference=lost
        self.assertEqual(self.runner().run()['status'],'blocked')
        before=list(self.transport.calls)
        self.runner().run()
        self.assertEqual(self.transport.calls,before)
        self.assertEqual(before.count('fanout-inference'),1)
        self.assertFalse(self.events('validator_attempt_started'))

    def test_same_served_alias_cannot_hide_artifact_identity_drift(self):
        model=deepcopy(self.model);model['artifact_sha256']='c'*64
        self.evidence['worker']['reservation']['identity']=model
        self.evidence['worker']['reservation']['identity_sha256']=identity_digest(model)
        self.evidence['worker']['observation']['identity']=model
        self.assertEqual(self.runner().run()['status'],'blocked')
        self.assertEqual(self.transport.calls,[])

    def test_worker_success_with_uncertain_validator_retains_role_charge_and_blocks_restart(self):
        self.transport.unknown_role='validator'
        output=self.runner().run()
        self.assertEqual(output['status'],'blocked',output)
        self.assertEqual(len(self.events('accepted_result_packet')),1)
        self.assertEqual(len(self.events('validator_attempt_started')),1)
        self.assertFalse(self.events('validator_result'))
        self.assertEqual(self.transport.calls.count('fanout-inference'),2)
        reservation=self.events('validator_accounting_reserved')[0]['payload']
        self.assertAlmostEqual(reservation['reserved_cost_usd'],.2)
        state=load_hosted_state(self.state_path,runtime())['legacy_state']
        self.assertAlmostEqual(state['usage']['cost_usd'],.1)
        before=list(self.transport.calls)
        self.runner().run()
        self.assertEqual(before,self.transport.calls)

if __name__=='__main__':unittest.main()
