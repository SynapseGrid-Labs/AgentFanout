"""Bounded future fixed-client ABI, synthetic responses; no service calls."""
from copy import deepcopy
import json
import time
import unittest
import uuid
from agentfanout.coordinator_local import (CoordinatorBroker, CoordinatorError, CoordinatorUncertain,
    validate_operation, resource_from_reservation, reconcile_identified_request, mixed_resource_from_reservation)
from agentfanout.local_profiles import identity_digest, build_inference_request
from tests.test_local_profiles import configured

class Transport:
    def __init__(self,response):self.response=response;self.calls=[]
    def call(self,args,raw):self.calls.append((args,raw));return deepcopy(self.response)

class DurableBrokerTests(unittest.TestCase):
    def reservation(self):
        model,profile=configured()
        return {'schema':'CoordinatorRoleReservationV1','state':'reserved','role':'worker','reservation_id':str(uuid.uuid4()),'identity':model,'identity_sha256':identity_digest(model),'owner_token':'owner','serving_epoch':model['serving_epoch'],'expires_at':time.time()+300,'cancellation_state':'none','coordinator_attestation_sha256':'f'*64},profile

    def test_exact_identified_inference_is_authorized_before_fixed_transport(self):
        r,p=self.reservation();b=resource_from_reservation(r,role='worker',current_identity=r['identity'])
        req={'request_id':str(uuid.uuid4()),'reservation_id':r['reservation_id'],'identity_sha256':r['identity_sha256'],'role':'worker','payload':build_inference_request(r['identity'],p,'bounded fixture')}
        t=Transport({'request_id':req['request_id']});broker=CoordinatorBroker(t,frozenset({r['identity']['logical_selector']}))
        journal=[]
        def authorize(op,d):self.assertEqual(t.calls,[]);journal.append((op,d));return True
        broker.execute('fanout-inference',req,authorize_effect=authorize,binding=b,current_identity=r['identity'],profile=p)
        self.assertEqual(json.loads(t.calls[0][1]),req);self.assertEqual(len(journal),1)

    def test_wrong_role_resource_or_profile_never_spawns(self):
        r,p=self.reservation();b=resource_from_reservation(r,role='worker',current_identity=r['identity'])
        req={'request_id':str(uuid.uuid4()),'reservation_id':r['reservation_id'],'identity_sha256':r['identity_sha256'],'role':'worker','payload':build_inference_request(r['identity'],p,'fixture')}
        for key,value in [('role','validator'),('reservation_id',str(uuid.uuid4())),('identity_sha256','a'*64)]:
            t=Transport({});broker=CoordinatorBroker(t,frozenset({r['identity']['logical_selector']}))
            with self.assertRaises(CoordinatorError):broker.execute('fanout-inference',{**req,key:value},authorize_effect=lambda *a:True,binding=b,current_identity=r['identity'],profile=p)
            self.assertEqual(t.calls,[])

    def test_uncorrelated_effect_acknowledgement_quarantines_durable_evidence(self):
        r,p=self.reservation();b=resource_from_reservation(r,role='worker',current_identity=r['identity'])
        req={'request_id':str(uuid.uuid4()),'reservation_id':r['reservation_id'],'identity_sha256':r['identity_sha256'],'role':'worker','payload':build_inference_request(r['identity'],p,'fixture')}
        broker=CoordinatorBroker(Transport({'request_id':str(uuid.uuid4())}),frozenset({r['identity']['logical_selector']}))
        with self.assertRaises(CoordinatorUncertain):broker.execute('fanout-inference',req,authorize_effect=lambda *a:True,binding=b,current_identity=r['identity'],profile=p)
        self.assertEqual(b.evidence['cancellation_state'],'uncertain')
        with self.assertRaises(CoordinatorError):resource_from_reservation({**r,'cancellation_state':'uncertain'},role='worker',current_identity=r['identity'])

    def test_identified_observation_cannot_follow_another_request(self):
        r,p=self.reservation();broker=CoordinatorBroker(Transport({'request_id':str(uuid.uuid4())}),frozenset({r['identity']['logical_selector']}))
        with self.assertRaises(CoordinatorError):broker.observe('fanout-request',{'identifier':str(uuid.uuid4())})

    def test_cancellation_acknowledgement_alone_never_proves_absence(self):
        r,p=self.reservation();b=resource_from_reservation(r,role='worker',current_identity=r['identity'])
        response={'schema':'CoordinatorInferenceRequestV1','request_id':str(uuid.uuid4()),'reservation_id':r['reservation_id'],'identity_sha256':r['identity_sha256'],'role':'worker','serving_epoch':r['serving_epoch'],'stage':'cancelling','effects_absent':False,'accounting_reconciled':False}
        with self.assertRaises(CoordinatorUncertain):reconcile_identified_request(b,response,request_id=response['request_id'])
        self.assertTrue(b.quarantined)

    def test_operation_allowlist_uuid_and_admin_guards_still_apply(self):
        caps=frozenset({'worker'})
        for op in ('fanout-release','fanout-cancel','fanout-request','fanout-reservation'):
            with self.assertRaises(CoordinatorError):validate_operation(op,{'identifier':'--endpoint'},caps)
        with self.assertRaises(CoordinatorError):validate_operation('fanout-attest',{'capability':'off'},caps)
        with self.assertRaises(CoordinatorError):validate_operation('fanout-reserve',{'command':'shell'},caps)

    def test_fresh_role_translation_checks_lease_fence_attestation_and_epoch(self):
        r,p=self.reservation();r['fencing_token']='fence'
        o={'identity':r['identity'],'owner_token':r['owner_token'],'fencing_token':'fence','attestation_sha256':r['coordinator_attestation_sha256'],'ready':True,'certain':True,'observed_at':time.time()}
        result=mixed_resource_from_reservation(r,o,role='worker')
        self.assertEqual(result['lease_id'],r['owner_token']);self.assertEqual(result['fencing_token'],'fence')
        for field,value in [('owner_token','other'),('fencing_token','other'),('attestation_sha256','a'*64),('observed_at',0),('certain',False)]:
            with self.subTest(field=field),self.assertRaises(CoordinatorError):mixed_resource_from_reservation(r,{**o,field:value},role='worker')
        with self.assertRaises(CoordinatorError):mixed_resource_from_reservation(r,o,role='validator')

if __name__=='__main__':unittest.main()
