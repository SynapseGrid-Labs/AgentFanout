"""Hosted offline execution with injected synthetic cloud adapters only."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import os
import tempfile
import time
import threading
import unittest

from agentfanout.host_runtime import HostLedger
from agentfanout.lifecycle import build_task_units, initialize_run, load_run_state
from agentfanout.mixed_contracts import MixedContractError, canonical_digest, freeze_contract
from agentfanout.mixed_state import load_hosted_state, save_hosted_state, wrap_hosted_state
from agentfanout.profiled_runner import ProfiledHostedRunner
from agentfanout.runner import HostedRunnerError
from tests.test_hosted_runner import FakeWorker, FakeValidator, capability
from tests.test_mixed_contracts import policy, runtime, resource


class BoundWorker(FakeWorker):
    supports_context_reuse = False

    def __init__(self, provider, model, partial=False):
        super().__init__(partial_first=partial, provider=provider)
        self.model = model
        self.seen_tasks = []

    def identity(self):
        return {"adapter_id": "synthetic-" + self.provider, "adapter_version":"fixture-v1", "model":self.model, "executable_sha256":"f"*64, "input_modes":["bounded_file_text"]}

    def prepare(self, assignment, tool_policy_sha256):
        self.prepare_calls += 1
        task = assignment["host_task"]
        return {"resolved_model":self.model,"capability":capability(self.provider,self.model,task,tool_policy_sha256)}

    def run(self, assignment, receipt, heartbeat):
        self.seen_tasks.append(deepcopy(assignment["task"]))
        value = super().run(assignment, receipt, heartbeat)
        value["resolved_model"] = self.model
        value["result"]["result_id"] = self.model + "-" + str(self.calls)
        return value


class BoundValidator(FakeValidator):
    def __init__(self, provider, model):
        super().__init__()
        self.provider,self.model = provider,model
        self.seen_tasks = []

    def identity(self):
        return {"adapter_id": "synthetic-validator-" + self.provider, "adapter_version":"fixture-v1", "model":self.model,"executable_sha256":"f"*64,"input_modes":["bounded_file_text"]}

    def prepare(self, task, result, tool_policy_sha256):
        return {"resolved_model":self.model,"capability":capability(self.provider,self.model,task,tool_policy_sha256)}

    def run(self, task, result):
        self.seen_tasks.append({"reasoning_effort":task["reasoning_effort"],"provider_model":deepcopy(task["provider_model"]),"peers":deepcopy(task.get("_peer_worker_results",[]))})
        output = super().run(task,result)
        output["resolved_model"] = self.model
        output["result"]["validator_id"] = self.model + "-" + str(self.calls)
        return output


class ProfiledRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        os.chmod(self.tmp.name,0o700)
        self.directory=Path(self.tmp.name)
        self.project=self.directory/"project"
        self.project.mkdir()
        self.state_file=self.directory/"state.json"
        self.ledger_file=self.directory/"ledger.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def setup_run(self, count=1, providers=None, partial=False, policies=None):
        providers=providers or ["GrokCLI"]*count
        tasks=build_task_units("Review bounded input", "bulk-read", "explorer", "Codex", "default", "low", ["request_item_"+str(i) for i in range(count)],allowed_paths=[str(self.project)])
        state=initialize_run("Review bounded input",tasks,{"max_wave_size":count,"max_total_cost_usd":10,"max_waves":6})
        workers,validators,assignments=[],[],[]
        for i,task in enumerate(state["tasks"]):
            provider=providers[i]
            w=BoundWorker(provider,"worker-model-"+str(i),partial=partial and i==0)
            v=BoundValidator("Grok" if provider=="GrokCLI" else "GrokCLI","validator-model-"+str(i))
            workers.append(w);validators.append(v)
            task["provider_model"].update(provider=provider,selector="worker-selector-"+str(i),resolved_model=w.model)
            task["reasoning_effort"]="low" if i%2==0 else "high"
            task["budgets"]["max_cost_usd"]=0.1
            p=policy()
            p["destinations"]["worker-"+str(i)]={"kind":"cloud","provider":provider}
            p["destinations"]["validator-"+str(i)]={"kind":"cloud","provider":v.provider}
            for allowed in p["allowed"].values():allowed.extend(["worker-"+str(i),"validator-"+str(i)])
            # Source policy explicitly authorizes selected cross-task validators.
            for j in range(count):
                validator_provider = "Grok" if providers[j] == "GrokCLI" else "GrokCLI"
                p["destinations"]["validator-"+str(j)] = {"kind":"cloud", "provider":validator_provider}
                if "validator-"+str(j) not in p["allowed"]["peer_evidence"]:
                    p["allowed"]["peer_evidence"].append("validator-"+str(j))
            if policies:p=policies(i,p)
            def b(adapter,role):
                return {"provider":adapter.provider,"mode":"grok_cli_subscription_acp" if adapter.provider=="GrokCLI" else "grok_direct_https","model_selector":("worker-selector-" if role=="worker" else "validator-selector-")+str(i),"resolved_model":adapter.model,"reasoning_effort":task["reasoning_effort"] if role=="worker" else ("high" if i%2==0 else "medium"),"identity_sha256":canonical_digest(adapter.identity()),"resource":None,"reservation_cost_usd":0.1 if role=="worker" else 0.2,"destination_id":("worker-" if role=="worker" else "validator-")+str(i)}
            assignments.append({"assignment_id":task["task_id"],"worker":b(w,"worker"),"validator":b(v,"validator"),"validation_required":True,"egress_policy":p})
        controls={"mixed_provider_runs":True,"grok_cli_subscription":True,"coordinator_local":True}
        contract=freeze_contract(state["run_id"],runtime(),assignments,feature_controls=controls)
        save_hosted_state(self.state_file,wrap_hosted_state(state,contract,runtime()),runtime())
        kwargs={"contract":contract,"runtime_identity":runtime(),"worker_bindings":{canonical_digest(a["worker"]):w for a,w in zip(assignments,workers)},"validator_bindings":{canonical_digest(a["validator"]):v for a,v in zip(assignments,validators)},"host_conformance_check":lambda b:True,"observe_resources":lambda assignment_id:{},"admission_controls":controls,"host_destination":"parent.host","parent_destination":"grok.cloud"}
        return workers,validators,kwargs

    def runner(self,kwargs):
        return ProfiledHostedRunner(self.state_file,self.ledger_file,{},None,approved_providers=["GrokCLI","Grok","Local"],max_workers=2,**kwargs)

    def test_mixed_cloud_residual_second_wave_validator_identity_and_accounting(self):
        workers,validators,kwargs=self.setup_run(partial=True)
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"complete",output)
        self.assertEqual(workers[0].calls,2)
        self.assertEqual(validators[0].calls,2)
        state=load_hosted_state(self.state_file,runtime())["legacy_state"]
        self.assertEqual(state["wave_index"],2)
        self.assertAlmostEqual(state["usage"]["cost_usd"],0.6)
        self.assertEqual(validators[0].seen_tasks[0]["reasoning_effort"],"high")
        self.assertEqual(validators[0].seen_tasks[0]["provider_model"]["resolved_model"],"validator-model-0")
        with self.assertRaisesRegex(ValueError,"invalid_run_schema"):load_run_state(self.state_file)
        ledger=HostLedger(self.ledger_file)
        try:
            self.assertEqual(len(ledger.events(state["run_id"],"mixed_contract_frozen")),1)
            costs=ledger.events(state["run_id"],"validator_accounting_reserved")
            self.assertEqual([event["payload"]["reserved_cost_usd"] for event in costs],[0.2,0.2])
        finally:ledger.close()

    def test_distinct_models_and_efforts_for_same_provider_assignments(self):
        workers,validators,kwargs=self.setup_run(count=2)
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"complete",output)
        self.assertEqual([w.calls for w in workers],[1,1])
        self.assertEqual([v.calls for v in validators],[1,1])
        self.assertEqual([v.seen_tasks[0]["reasoning_effort"] for v in validators],["high","medium"])
        self.assertEqual([w.seen_tasks[0]["reasoning_effort"] for w in workers],["low","high"])
        state=load_hosted_state(self.state_file,runtime())["legacy_state"]
        self.assertAlmostEqual(state["usage"]["cost_usd"],0.6)

    def test_required_validator_unavailable_blocks_before_any_worker(self):
        workers,validators,kwargs=self.setup_run()
        kwargs["validator_bindings"]={}
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked")
        self.assertIn("validator_binding_adapter_unavailable",output["reason"])
        self.assertEqual(workers[0].calls,0)
        self.assertEqual(workers[0].prepare_calls,0)

    def test_peer_policy_from_source_task_filters_before_cloud_validator(self):
        def policies(i,p):
            if i==0:p["allowed"]["peer_evidence"]=["parent.host"]
            return p
        workers,validators,kwargs=self.setup_run(count=2,policies=policies)
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"complete",output)
        self.assertEqual(validators[1].seen_tasks[0]["peers"],[])
        self.assertEqual(len(validators[0].seen_tasks[0]["peers"]),1)
        self.assertEqual(validators[0].seen_tasks[0]["peers"][0]["task_id"],kwargs["contract"]["assignments"][1]["assignment_id"])

    def test_local_only_parent_refusal_does_not_expose_status_ids_or_errors(self):
        def policies(i,p):
            p["allowed"]["parent_synthesis"]=["parent.host"]
            p["allowed"]["error"]=["parent.host"]
            return p
        workers,validators,kwargs=self.setup_run(policies=policies)
        with self.assertRaisesRegex(MixedContractError,"parent_error_destination_denied"):
            self.runner(kwargs).run()
        self.assertEqual(workers[0].calls,1)
        state=load_hosted_state(self.state_file,runtime())["legacy_state"]
        self.assertEqual(state["status"],"complete")

    def test_feature_disable_and_adapter_identity_drift_block_before_launch(self):
        workers,validators,kwargs=self.setup_run()
        kwargs["admission_controls"]=dict(kwargs["admission_controls"],grok_cli_subscription=False)
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked")
        self.assertEqual(workers[0].calls,0)
        kwargs["admission_controls"]["grok_cli_subscription"]=True
        workers[0].model="changed"
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked")
        self.assertIn("identity_drift",output["reason"])
        self.assertEqual(workers[0].calls,0)

    def test_restart_requires_pinned_runtime_and_frozen_contract(self):
        workers,validators,kwargs=self.setup_run()
        self.assertEqual(self.runner(kwargs).run()["status"],"complete")
        self.assertEqual(self.runner(kwargs).run()["status"],"complete")
        self.assertEqual(workers[0].calls,1)
        kwargs["runtime_identity"]=dict(runtime(),release_id="other-release")
        with self.assertRaisesRegex(MixedContractError,"runtime_mismatch"):
            self.runner(kwargs)

    def local_fixture(self, workers, kwargs):
        """Synthetic coordinator observations; never a positive Local launch."""
        value=load_hosted_state(self.state_file,runtime())
        state=value["legacy_state"]
        assignment=deepcopy(kwargs["contract"]["assignments"][0])
        w=workers[0]
        w.provider="Local"
        b=assignment["worker"]
        b.update(provider="Local",mode="coordinator_local",identity_sha256=canonical_digest(w.identity()),resource=resource("worker"))
        b["resource"]["expires_at"]=time.time()+60
        assignment["egress_policy"]["destinations"][b["destination_id"]]={"kind":"local","provider":"Local"}
        state["tasks"][0]["provider_model"]["provider"]="Local"
        value["contract"]=freeze_contract(state["run_id"],runtime(),[assignment],feature_controls=kwargs["admission_controls"])
        state.pop("_loaded_revision",None)
        # Fixture reset is only before any ledger/effect exists; no runtime rollback.
        self.state_file.unlink()
        save_hosted_state(self.state_file,value,runtime())
        kwargs["contract"]=value["contract"]
        kwargs["worker_bindings"]={canonical_digest(b):w}
        kwargs["observe_resources"]=lambda assignment_id:{"worker":deepcopy(b["resource"])}
        return b

    def test_complete_worker_validator_budget_checked_before_worker_bootstrap(self):
        workers,validators,kwargs=self.setup_run()
        value=load_hosted_state(self.state_file,runtime())
        value["legacy_state"]["budget"]["max_total_cost_usd"]=0.25
        save_hosted_state(self.state_file,value,runtime())
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked")
        self.assertEqual(output["reason"],"worker_validator_reservations_insufficient")
        self.assertEqual(workers[0].prepare_calls,0)
        self.assertEqual(workers[0].calls,0)

    def test_model_changes_after_prepare_do_not_reach_inference(self):
        workers,validators,kwargs=self.setup_run()
        original=workers[0].prepare
        def prepare(assignment,digest):
            prepared=original(assignment,digest)
            workers[0].model="changed-after-preparation"
            return prepared
        workers[0].prepare=prepare
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked")
        self.assertEqual(workers[0].calls,0)
        state=load_hosted_state(self.state_file,runtime())["legacy_state"]
        self.assertNotEqual(state["status"],"complete")

    def test_stale_local_observation_at_admission_or_preparation_blocks(self):
        workers,validators,kwargs=self.setup_run()
        b=self.local_fixture(workers,kwargs)
        calls=[]
        def observer(assignment_id):
            calls.append(assignment_id)
            observed=deepcopy(b["resource"])
            # First admission snapshot is fresh; preparation gets a new epoch.
            if len(calls)>=3:observed["serving_epoch"]="new-epoch"
            return {"worker":observed}
        kwargs["observe_resources"]=observer
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked")
        self.assertEqual(workers[0].prepare_calls,0)
        self.assertEqual(workers[0].calls,0)
        self.assertGreaterEqual(len(calls),3)

    def test_stale_resource_on_last_execution_check_refuses_inference(self):
        workers,validators,kwargs=self.setup_run()
        b=self.local_fixture(workers,kwargs)
        runner=self.runner(kwargs)
        state=runner._load()
        runner._execution_state=state
        observed=deepcopy(b["resource"])
        observed["fencing_token"]="new-owner"
        runner.observe_resources=lambda assignment_id:{"worker":observed}
        with self.assertRaisesRegex(MixedContractError,"epoch_or_lease_drift"):
            runner._before_worker_execute({"host_task":state["tasks"][0]}, {})
        self.assertEqual(workers[0].calls,0)

    def test_ready_local_observation_cannot_reinterpret_missing_coordinator_receipt(self):
        workers,validators,kwargs=self.setup_run()
        self.local_fixture(workers,kwargs)
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked",output)
        self.assertEqual(workers[0].calls,0)
        state=load_hosted_state(self.state_file,runtime())["legacy_state"]
        self.assertNotEqual(state["status"],"complete")

    def test_unproved_conformance_blocks_before_session_or_model_work(self):
        workers,validators,kwargs=self.setup_run()
        kwargs["host_conformance_check"]=lambda binding:False
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked")
        self.assertEqual(output["reason"],"host_provider_conformance_unproved")
        self.assertEqual(workers[0].prepare_calls,0)
        self.assertEqual(workers[0].calls,0)

    def test_provider_freeze_ignores_broad_caller_and_ambient_adapter_map(self):
        workers,validators,kwargs=self.setup_run()
        kwargs["approved_providers"]=["Codex","Local","GrokCLI","Grok"]
        ambient=BoundWorker("Codex","ambient-api-model")
        runner=ProfiledHostedRunner(self.state_file,self.ledger_file,{"Codex":ambient},None,**kwargs)
        self.assertEqual(runner.approved_providers,["Grok","GrokCLI"])
        output=runner.run()
        self.assertEqual(output["status"],"complete",output)
        self.assertEqual(ambient.calls,0)
        state=load_hosted_state(self.state_file,runtime())["legacy_state"]
        ledger=HostLedger(self.ledger_file)
        try:
            events=ledger.events(state["run_id"],"approved_providers")
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]["payload"],{"providers":["Grok","GrokCLI"]})
        finally:ledger.close()

    def test_authored_transition_hypothesis_refused_by_execution_runner(self):
        workers,validators,kwargs=self.setup_run()
        contract=kwargs["contract"]
        worker=contract["assignments"][0]["worker"]
        alternative=deepcopy(worker)
        alternative["resolved_model"]="alternate-approved-model"
        alternative["identity_sha256"]="a"*64
        transition={"assignment_id":contract["assignments"][0]["assignment_id"],"role":"worker","from_binding_sha256":canonical_digest(worker),"to_binding":alternative}
        kwargs["contract"]=freeze_contract(contract["run_id"],runtime(),contract["assignments"],allowed_transitions=[transition],feature_controls=contract["feature_controls"])
        with self.assertRaisesRegex(MixedContractError,"persisted_transition_execution_unsupported"):
            self.runner(kwargs)
        self.assertEqual(workers[0].prepare_calls,0)
        self.assertEqual(workers[0].calls,0)
        self.assertFalse(self.ledger_file.exists())

    def test_concurrent_controller_refused_before_ledger_or_session_effects(self):
        workers,validators,kwargs=self.setup_run()
        entered=threading.Event()
        release=threading.Event()
        original_run=workers[0].run
        def held_run(assignment,receipt,heartbeat):
            entered.set()
            if not release.wait(10):raise RuntimeError("fixture_release_timeout")
            return original_run(assignment,receipt,heartbeat)
        workers[0].run=held_run
        first=self.runner(kwargs)
        outcomes=[]
        errors=[]
        def drive():
            try:outcomes.append(first.run())
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=drive)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            state=load_hosted_state(self.state_file,runtime())["legacy_state"]
            ledger=HostLedger(self.ledger_file)
            try:before=ledger.events(state["run_id"])
            finally:ledger.close()
            with self.assertRaisesRegex(MixedContractError,"profiled_driver_already_active"):
                self.runner(kwargs).run()
            ledger=HostLedger(self.ledger_file)
            try:self.assertEqual(ledger.events(state["run_id"]),before)
            finally:ledger.close()
            self.assertEqual(workers[0].prepare_calls,1)
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(outcomes[0]["status"],"complete")
        self.assertEqual(workers[0].calls,1)
        self.assertEqual(validators[0].calls,1)
        self.assertEqual(self.runner(kwargs).run()["status"],"complete")
        self.assertEqual(workers[0].calls,1)

    def test_profiled_guard_exact_recovery_binding_and_immutable_tamper_refusal(self):
        workers,validators,kwargs=self.setup_run()
        self.assertEqual(self.runner(kwargs).run()["status"],"complete")
        state=load_hosted_state(self.state_file,runtime())["legacy_state"]
        ledger=HostLedger(self.ledger_file)
        try:
            guards=ledger.events(state["run_id"],"profiled_session_guard")
            self.assertEqual(len(guards),1)
            self.assertEqual(guards[0]["payload"],self.runner(kwargs)._guard_binding(state))
            before=ledger.events(state["run_id"])
        finally:ledger.close()
        changed=dict(kwargs,parent_destination="parent.host")
        output=self.runner(changed).run()
        self.assertEqual(output["status"],"blocked")
        self.assertEqual(output["reason"],"profiled_session_guard_mismatch")
        ledger=HostLedger(self.ledger_file)
        try:
            self.assertEqual(ledger.events(state["run_id"]),before)
            forged=dict(guards[0]["payload"],runtime_sha256="0"*64)
            ledger.append(state["run_id"],"profiled_session_guard",forged)
            recorded=ledger.events(state["run_id"])
        finally:ledger.close()
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked")
        self.assertEqual(output["reason"],"profiled_session_guard_mismatch")
        ledger=HostLedger(self.ledger_file)
        try:self.assertEqual(ledger.events(state["run_id"]),recorded)
        finally:ledger.close()
        self.assertEqual(workers[0].calls,1)

    def test_lifetime_lock_rejects_symlink_and_unsafe_permissions(self):
        workers,validators,kwargs=self.setup_run()
        lock=self.state_file.with_name(self.state_file.name+".driver.lock")
        target=self.directory/"unrelated.txt"
        target.write_text("unrelated evidence")
        target.chmod(0o600)
        lock.symlink_to(target)
        with self.assertRaisesRegex(MixedContractError,"profiled_driver_lock_unsafe"):
            self.runner(kwargs).run()
        self.assertEqual(target.read_text(),"unrelated evidence")
        self.assertFalse(self.ledger_file.exists())
        lock.unlink()
        lock.write_text("existing owner data")
        lock.chmod(0o644)
        with self.assertRaisesRegex(MixedContractError,"profiled_driver_lock_unsafe"):
            self.runner(kwargs).run()
        self.assertFalse(self.ledger_file.exists())
        self.assertEqual(workers[0].prepare_calls,0)

    def test_existing_mixed_history_without_guard_is_not_reinterpreted(self):
        workers,validators,kwargs=self.setup_run()
        ledger=HostLedger(self.ledger_file)
        try:
            ledger.append(kwargs["contract"]["run_id"],"mixed_contract_frozen",kwargs["contract"])
            before=ledger.events(kwargs["contract"]["run_id"])
        finally:ledger.close()
        output=self.runner(kwargs).run()
        self.assertEqual(output["status"],"blocked")
        self.assertEqual(output["reason"],"active_run_profiled_guard_missing")
        ledger=HostLedger(self.ledger_file)
        try:self.assertEqual(ledger.events(kwargs["contract"]["run_id"]),before)
        finally:ledger.close()
        self.assertEqual(workers[0].prepare_calls,0)
