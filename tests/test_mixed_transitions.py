"""Actual profiled host transaction/state/ledger execution with synthetic adapters."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import uuid

from agentfanout.host_runtime import HostLedger
from agentfanout.mixed_contracts import MixedContractError, freeze_contract, canonical_digest
from agentfanout.mixed_state import load_hosted_state, save_hosted_state, wrap_hosted_state
from tests import test_profiled_runner as fixtures
from tests.test_profiled_runner import BoundWorker, BoundValidator
from agentfanout.runner import HostedRunnerError
from tests.test_mixed_contracts import runtime


def reconcile(state, digest, tip):
    return {'schema':'MixedTransitionReconciliationV1','run_id':state['run_id'],'contract_sha256':digest,
            'state_revision':state['revision'],'ledger_tip':tip,'effects_absent':True,
            'accounting_reconciled':True,'ownership':'certain','cancellation':'not_requested'}


class TransitionTests(unittest.TestCase):
    setUp = fixtures.ProfiledRunnerTests.setUp
    tearDown = fixtures.ProfiledRunnerTests.tearDown
    runner = fixtures.ProfiledRunnerTests.runner
    setup_run = fixtures.ProfiledRunnerTests.setup_run

    def setup_transition(self, role='worker'):
        workers,validators,k = self.setup_run()
        a = deepcopy(k['contract']['assignments'][0])
        old = a[role]
        adapter = BoundWorker(old['provider'],'alternate-worker') if role=='worker' else BoundValidator(old['provider'],'alternate-validator')
        alternative = {**deepcopy(old),'resolved_model':adapter.model,'model_selector':'alternate','identity_sha256':canonical_digest(adapter.identity())}
        edge = {'assignment_id':a['assignment_id'],'role':role,'from_binding_sha256':canonical_digest(old),'to_binding':alternative,'reason':'explicit_model_choice','max_count':1}
        c = freeze_contract(k['contract']['run_id'],runtime(),[a],allowed_transitions=[edge],feature_controls=k['admission_controls'],schema='MixedRunContractV2')
        state=load_hosted_state(self.state_file,runtime())['legacy_state'];state.pop('_loaded_revision')
        self.state_file.unlink()  # Only replace this fresh fixture before any host history.
        save_hosted_state(self.state_file,wrap_hosted_state(state,c,runtime()),runtime())
        k['contract']=c;k[role+'_bindings'][canonical_digest(alternative)]=adapter
        return workers,validators,k,alternative,adapter

    def change(self, runner,k,b,role='worker',**extra):
        return runner.transition(k['contract']['assignments'][0]['assignment_id'],role,b,'explicit_model_choice',reconcile=reconcile,authorize=lambda p:True,**extra)

    def test_persisted_worker_transition_restart_executes_exact_model_and_preserves_guard(self):
        workers,validators,k,b,alternative=self.setup_transition()
        record=self.change(self.runner(k),k,b)
        out=self.runner(k).run()
        self.assertEqual(out['status'],'complete',out)
        self.assertEqual(workers[0].calls,0);self.assertEqual(alternative.calls,1)
        e=load_hosted_state(self.state_file,runtime())
        self.assertEqual(e['schema'],'HostedRunStateV4');self.assertEqual(e['contract'],k['contract'])
        self.assertEqual(e['transition_history'],[record])
        ledger=HostLedger(self.ledger_file)
        try:
            self.assertEqual(len(ledger.events(e['contract']['run_id'],'profiled_session_guard')),1)
            self.assertEqual(len(ledger.events(e['contract']['run_id'],'mixed_transition_committed')),1)
            self.assertAlmostEqual(e['legacy_state']['usage']['cost_usd'],0.3)
        finally:ledger.close()

    def test_validator_transition_independent_binding_and_numeric_reservation(self):
        workers,validators,k,b,alternative=self.setup_transition('validator')
        self.change(self.runner(k),k,b,'validator')
        out=self.runner(k).run();self.assertEqual(out['status'],'complete',out)
        self.assertEqual(workers[0].calls,1);self.assertEqual(validators[0].calls,0);self.assertEqual(alternative.calls,1)

    def test_crash_after_authorization_before_state_replays_once_without_inference(self):
        workers,validators,k,b,alternative=self.setup_transition()
        runner=self.runner(k)
        with patch.object(runner,'_save',side_effect=SystemExit('injected crash')):
            with self.assertRaises(SystemExit):self.change(runner,k,b)
        self.assertEqual(alternative.calls,0)
        self.assertEqual(load_hosted_state(self.state_file,runtime())['transition_history'],[])
        out=self.runner(k).run();self.assertEqual(out['status'],'complete',out)
        self.assertEqual(alternative.calls,1)

    def test_crash_after_state_before_commit_recovers_without_duplicate_history_or_authorization(self):
        workers,validators,k,b,alternative=self.setup_transition()
        original=HostLedger.append
        def crash(ledger,run_id,kind,*args,**kwargs):
            if kind=='mixed_transition_committed':raise SystemExit('injected crash')
            return original(ledger,run_id,kind,*args,**kwargs)
        with patch.object(HostLedger,'append',crash):
            with self.assertRaises(SystemExit):self.change(self.runner(k),k,b)
        e=load_hosted_state(self.state_file,runtime());self.assertEqual(len(e['transition_history']),1)
        out=self.runner(k).run();self.assertEqual(out['status'],'complete',out)
        ledger=HostLedger(self.ledger_file)
        try:self.assertEqual(len(ledger.events(e['contract']['run_id'],'mixed_transition_authorized')),1)
        finally:ledger.close()

    def test_idempotent_uuid_rejects_changed_target_and_ledger_hashes_are_unchanged(self):
        workers,validators,k,b,alternative=self.setup_transition()
        identifier=str(uuid.uuid4());r=self.change(self.runner(k),k,b,transition_id=identifier)
        ledger=HostLedger(self.ledger_file)
        try:before=ledger.events(k['contract']['run_id'])
        finally:ledger.close()
        self.assertEqual(self.change(self.runner(k),k,b,transition_id=identifier),r)
        changed=deepcopy(b);changed['resolved_model']='unapproved'
        with self.assertRaisesRegex(MixedContractError,'id_reused'):self.change(self.runner(k),k,changed,transition_id=identifier)
        ledger=HostLedger(self.ledger_file)
        try:self.assertEqual(before,ledger.events(k['contract']['run_id']))
        finally:ledger.close()

    def test_unknown_cancellation_or_accounting_and_stale_proof_never_authorize(self):
        for key,value in [('cancellation','uncertain'),('effects_absent',False),('accounting_reconciled',False),('ownership','uncertain'),('state_revision',0),('ledger_tip','stale')]:
            with self.subTest(key=key):
                workers,validators,k,b,alternative=self.setup_transition()
                calls=[]
                def bad(s,d,t):return {**reconcile(s,d,t),key:value}
                with self.assertRaises(MixedContractError):self.runner(k).transition(k['contract']['assignments'][0]['assignment_id'],'worker',b,'explicit_model_choice',reconcile=bad,authorize=lambda p:calls.append(p))
                self.assertEqual(calls,[]);self.assertEqual(alternative.calls,0)
                self.assertEqual(load_hosted_state(self.state_file,runtime())['transition_history'],[])
                # Fresh fixture for next subcase; no production data involved.
                self.state_file.unlink();self.ledger_file.unlink()

    def test_unapproved_target_reason_and_unavailable_required_validator_block(self):
        workers,validators,k,b,alternative=self.setup_transition()
        with self.assertRaisesRegex(MixedContractError,'not_authorized'):
            self.runner(k).transition(k['contract']['assignments'][0]['assignment_id'],'worker',b,'unapproved_reason',reconcile=reconcile,authorize=lambda p:True)
        k['validator_bindings']={}
        with self.assertRaisesRegex(HostedRunnerError,'validator_binding_adapter_unavailable'):self.change(self.runner(k),k,b)
        self.assertEqual(load_hosted_state(self.state_file,runtime())['transition_history'],[])

    def test_old_contract_cannot_silently_execute_transitions(self):
        workers,validators,k=self.setup_run()
        with self.assertRaisesRegex(MixedContractError,'reader_unsupported'):
            self.change(self.runner(k),k,k['contract']['assignments'][0]['worker'])

    def test_history_cannot_be_removed_reordered_or_edited(self):
        workers,validators,k,b,alternative=self.setup_transition();self.change(self.runner(k),k,b)
        e=load_hosted_state(self.state_file,runtime());e['transition_history']=[]
        # Binding still drifted; even restoring original task cannot remove journal.
        t=e['legacy_state']['tasks'][0];old=k['contract']['assignments'][0]['worker']
        t['provider_model'].update(provider=old['provider'],selector=old['model_selector'],resolved_model=old['resolved_model'])
        with self.assertRaisesRegex(MixedContractError,'history_rewrite'):save_hosted_state(self.state_file,e,runtime())

    def test_v4_schemas_refuse_old_readers_and_validate_real_transition(self):
        from tests import test_mixed_schemas as schemas
        if schemas.jsonschema is None:self.skipTest('optional jsonschema unavailable')
        workers,validators,k,b,alternative=self.setup_transition();self.change(self.runner(k),k,b)
        e=load_hosted_state(self.state_file,runtime());e['legacy_state'].pop('_loaded_revision')
        validator=schemas.MixedSchemaTests().validator
        validator('HostedRunStateV4').validate(e)
        validator('MixedTransitionV1').validate(e['transition_history'][0])
        self.assertFalse(validator('HostedRunStateV3').is_valid(e))
        self.assertFalse(validator('MixedRunContractV1').is_valid(e['contract']))

    def test_frozen_cycle_counts_are_bounded_and_unknown_reasons_refuse(self):
        from agentfanout.mixed_transitions import validate_history
        workers,validators,k,b,alternative=self.setup_transition()
        original=k['contract']['assignments'][0]['worker'];edge=k['contract']['allowed_transitions'][0]
        reverse={**deepcopy(edge),'from_binding_sha256':canonical_digest(b),'to_binding':original}
        c=freeze_contract(k['contract']['run_id'],runtime(),k['contract']['assignments'],allowed_transitions=[edge,reverse],feature_controls=k['admission_controls'],schema='MixedRunContractV2')
        def record(n,source,target):return {'schema':'MixedTransitionV1','transition_id':str(uuid.uuid4()),'sequence':n,'assignment_id':edge['assignment_id'],'role':'worker','from_binding_sha256':canonical_digest(source),'to_binding':target,'reason':edge['reason'],'proof_sha256':'a'*64}
        history=[record(1,original,b),record(2,b,original)]
        validate_history(c,history)
        with self.assertRaisesRegex(MixedContractError,'limit_exceeded'):validate_history(c,[*history,record(3,original,b)])

    def test_local_role_reservation_wait_and_uncertain_cancel_refuse_transition_before_authorization(self):
        from tests.test_mixed_contracts import resource
        workers,validators,k,b,alternative=self.setup_transition()
        # A fully frozen Local alternative is allowed as data but never admitted
        # without an independent current reservation for its exact role.
        original=k['contract']['assignments'][0]
        local={**b,'provider':'Local','mode':'coordinator_local','resource':resource('worker'),'destination_id':'worker.local'}
        a=deepcopy(original);a['egress_policy']['destinations']['worker.local']={'kind':'local','provider':'Local'}
        for allowed in a['egress_policy']['allowed'].values():
            if 'worker.local' not in allowed:allowed.append('worker.local')
        edge={**k['contract']['allowed_transitions'][0],'to_binding':local}
        c=freeze_contract(k['contract']['run_id'],runtime(),[a],allowed_transitions=[edge],feature_controls=k['admission_controls'],schema='MixedRunContractV2')
        e=load_hosted_state(self.state_file,runtime());e['legacy_state'].pop('_loaded_revision');self.state_file.unlink()
        save_hosted_state(self.state_file,wrap_hosted_state(e['legacy_state'],c,runtime()),runtime());k['contract']=c
        calls=[]
        with self.assertRaisesRegex(MixedContractError,'not_independently_observed'):
            self.runner(k).transition(a['assignment_id'],'worker',local,'explicit_model_choice',reconcile=reconcile,authorize=lambda p:calls.append(p))
        self.assertEqual(calls,[])

    def test_crashed_intent_cannot_rebase_over_later_external_effects(self):
        workers,validators,k,b,alternative=self.setup_transition();runner=self.runner(k)
        with patch.object(runner,'_save',side_effect=SystemExit('injected crash')):
            with self.assertRaises(SystemExit):self.change(runner,k,b)
        ledger=HostLedger(self.ledger_file)
        try:ledger.append(k['contract']['run_id'],'external_effect_started',{'effects':'unknown'})
        finally:ledger.close()
        out=self.runner(k).run();self.assertEqual(out['status'],'blocked');self.assertIn('later_effects_exist',out['reason'])
        self.assertEqual(alternative.calls,0);self.assertEqual(load_hosted_state(self.state_file,runtime())['transition_history'],[])

    def test_worker_transition_cannot_change_existing_numeric_reservation_authority(self):
        workers,validators,k,b,alternative=self.setup_transition()
        for cost in (0.0,0.2):
            edge=deepcopy(k['contract']['allowed_transitions'][0]);edge['to_binding']['reservation_cost_usd']=cost
            with self.subTest(cost=cost),self.assertRaisesRegex(MixedContractError,'reservation_change_unsupported'):
                freeze_contract(k['contract']['run_id'],runtime(),k['contract']['assignments'],allowed_transitions=[edge],feature_controls=k['admission_controls'],schema='MixedRunContractV2')
        self.assertEqual(alternative.calls,0)

if __name__=='__main__':unittest.main()
