from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import subprocess
import unittest
from unittest.mock import patch
from agentfanout.grok_cli import GrokCLIUnavailable
from agentfanout.grok_probe_container import TMPFS, verify_container, run_container_probe


class ContainerContractTests(unittest.TestCase):
    def setUp(self):
        self.image = "sha256:" + "a" * 64
        self.exe, self.helper = Path("/tmp/probe/binary"), Path("/tmp/probe/helper")
        self.name, self.cpus = "agentfanout-abi-synthetic", "0,1"
        self.record = {"Image": self.image, "Name": "/" + self.name, "State": {"Status": "created"},
                       "Config": {"User": "1000:1000", "Tty": False, "OpenStdin": True,
                                  "WorkingDir": "/work", "Entrypoint": ["/probe-boundary"],
                                  "Cmd": ["/pinned-binary", "/work", "--no-auto-update", "agent", "stdio"],
                                  "Healthcheck": {"Test": ["NONE"]}, "Labels": {"agentfanout.diagnostic.owner": self.name}},
                       "HostConfig": {"Runtime": "runc", "NetworkMode": "none", "IpcMode": "none", "PidMode": "",
                                      "CgroupnsMode": "private", "Privileged": False, "ReadonlyRootfs": True,
                                      "Memory": 2147483648, "MemorySwap": 2147483648, "NanoCpus": 2000000000,
                                      "PidsLimit": 32, "CpusetCpus": self.cpus, "AutoRemove": False,
                                      "CapDrop": ["ALL"], "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                                      "LogConfig": {"Type": "none", "Config": {}}, "Tmpfs": {"/work": TMPFS},
                                      "SecurityOpt": ["no-new-privileges:true"]},
                       "Mounts": [{"Source": str(p), "Destination": d, "RW": False, "Type": "bind"}
                                  for p, d in ((self.exe, "/pinned-binary"), (self.helper, "/probe-boundary"))]}

    def verify(self, record):
        verify_container(record, self.image, self.name, self.exe, self.helper, self.cpus)

    def test_exact_contract_accepted_only_before_start(self):
        self.verify(self.record)
        r = deepcopy(self.record); r["State"]["Status"] = "running"
        with self.assertRaises(GrokCLIUnavailable): self.verify(r)

    def test_gpu_network_pid_runtime_and_resources_fail_closed(self):
        for key, value in (("NetworkMode", "host"), ("PidMode", "host"), ("Runtime", "nvidia"),
                           ("Privileged", True), ("ReadonlyRootfs", False), ("Memory", 0),
                           ("DeviceRequests", [{"Capabilities": [["gpu"]]}]), ("Devices", [{"PathOnHost": "/dev/nvidia0"}])):
            with self.subTest(key=key):
                r=deepcopy(self.record);r["HostConfig"][key]=value
                with self.assertRaises(GrokCLIUnavailable): self.verify(r)

    def test_host_paths_docker_socket_credentials_and_writable_mounts_refused(self):
        for mount in ({"Source": "/var/run/docker.sock", "Destination": "/docker.sock", "RW": True, "Type": "bind"},
                      {"Source": "/operator/.grok", "Destination": "/auth", "RW": False, "Type": "bind"}):
            r=deepcopy(self.record);r["Mounts"].append(mount)
            with self.assertRaises(GrokCLIUnavailable): self.verify(r)
        r=deepcopy(self.record);r["Mounts"][0]["RW"]=True
        with self.assertRaises(GrokCLIUnavailable): self.verify(r)

    def test_capabilities_restart_healthcheck_and_logging_refused(self):
        for key,value in (("CapAdd",["SYS_ADMIN"]), ("SecurityOpt",["seccomp:unconfined"]),
                          ("RestartPolicy",{"Name":"always","MaximumRetryCount":0}),
                          ("LogConfig",{"Type":"json-file","Config":{}}), ("Tmpfs",{"/work":"size=1g"})):
            with self.subTest(key=key):
                r=deepcopy(self.record);r["HostConfig"][key]=value
                with self.assertRaises(GrokCLIUnavailable): self.verify(r)
        r=deepcopy(self.record);r["Config"]["Healthcheck"]={"Test":["CMD-SHELL","arbitrary command"]}
        with self.assertRaises(GrokCLIUnavailable): self.verify(r)

    def test_image_user_owner_and_arguments_are_bound(self):
        for key,value in (("User","0"), ("Entrypoint",["/bin/sh"]), ("Cmd",["session/prompt"]), ("Labels",{})):
            r=deepcopy(self.record);r["Config"][key]=value
            with self.assertRaises(GrokCLIUnavailable): self.verify(r)
        r=deepcopy(self.record);r["Image"]="sha256:"+"b"*64
        with self.assertRaises(GrokCLIUnavailable): self.verify(r)

    def test_unpinned_images_or_excessive_bounds_never_touch_engine(self):
        with patch("agentfanout.grok_probe_container.command") as engine:
            for image,timeout in (("python:latest",30), (self.image,31), (self.image,float("nan"))):
                with self.assertRaises(GrokCLIUnavailable):
                    run_container_probe(Path("absent"), "a"*64, Path("absent"), image, lambda _:True, lambda *args:True, timeout=timeout)
            engine.assert_not_called()

    def test_missing_host_authority_or_durability_never_touches_engine(self):
        with patch("agentfanout.grok_probe_container.prepare_intent",return_value={}), patch("agentfanout.grok_probe_container.command") as engine:
            for authorize,journal in ((False,True),(True,False)):
                with self.assertRaises(GrokCLIUnavailable):
                    run_container_probe(Path("absent"), "a"*64, Path("absent"), self.image, lambda _:authorize, lambda *args:journal)
            engine.assert_not_called()

    def simulated_cleanup(self, *, uncertain=False, response_durable=True):
        root = Path(__file__).resolve().parents[1]
        source = root / "scripts/grok-probe-sandbox.c"
        binary = Path("/usr/bin/true")
        expected = hashlib.sha256(binary.read_bytes()).hexdigest()
        events, operations = [], []
        inspections = 0
        record = None
        def engine(argv, check=True):
            nonlocal inspections, record
            operations.append(argv[0])
            if argv[:2] == ["image", "inspect"]:
                body = [{"Id":self.image,"Os":"linux","Architecture":"amd64","Config":{}}]
            elif argv[0] == "create":
                name = argv[argv.index("--name")+1]
                record = deepcopy(self.record)
                record["Name"] = "/" + name
                record["Config"]["Labels"]["agentfanout.diagnostic.owner"] = name
                record["HostConfig"]["CpusetCpus"] = argv[argv.index("--cpuset-cpus")+1]
                mounts = [argv[i+1] for i,a in enumerate(argv) if a == "--mount"]
                for m, value in zip(record["Mounts"], mounts):
                    m["Source"] = value.split("source=",1)[1].split(",",1)[0]
                return subprocess.CompletedProcess(argv,0,"c"*64,"")
            elif argv[0] == "inspect":
                inspections += 1
                if inspections > 1:
                    running = inspections == 2 or uncertain
                    record["State"] = {"Status":"running" if running else "exited","Running":running,"Pid":123 if running else 0}
                body = [deepcopy(record)]
            elif argv[0] == "kill":
                return subprocess.CompletedProcess(argv,1,"","already stopped or unknown")
            elif argv[0] == "rm":
                return subprocess.CompletedProcess(argv,0,"c"*64,"")
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv,0,json.dumps(body),"")
        def journal(kind,payload):
            events.append((kind,deepcopy(payload)))
            return response_durable if kind == "container_response_observed" else True
        response = {"status":"frame_observed","frames":[{"jsonrpc":"2.0","id":1,"result":{}}],
                    "boundary_enforced":True,"process_reaped":True,"admission_allowed":False,"replay_allowed":False}
        patches = (patch("agentfanout.grok_probe_container.prepare_intent",return_value={"sandbox_source_sha256":hashlib.sha256(source.read_bytes()).hexdigest()}),
                   patch("agentfanout.grok_probe_container.command",side_effect=engine),
                   patch("agentfanout.grok_probe_container.supervise",return_value=response))
        with patches[0],patches[1],patches[2]:
            if uncertain or not response_durable:
                with self.assertRaisesRegex(GrokCLIUnavailable,"cancellation_uncertain" if uncertain else "response_not_durable"):
                    run_container_probe(binary,expected,source,self.image,lambda _:True,journal)
                result = None
            else:
                result = run_container_probe(binary,expected,source,self.image,lambda _:True,journal)
        return result,events,operations

    def test_kill_race_requires_fresh_stopped_pid_zero_evidence(self):
        result,events,operations=self.simulated_cleanup()
        self.assertTrue(result["worker_process_reconciled"])
        self.assertTrue(result["container_removed"])
        kinds=[k for k,_ in events]
        self.assertLess(kinds.index("container_response_observed"),kinds.index("container_cleanup_intent"))
        captured=dict(events)["container_response_observed"]
        self.assertFalse(captured["worker_process_reconciled"])
        self.assertTrue(captured["client_process_reaped"])
        self.assertNotIn("process_reaped",captured)
        self.assertEqual(operations[-3:],["kill","inspect","rm"])

    def test_unknown_cancellation_keeps_response_and_refuses_removal(self):
        _,events,operations=self.simulated_cleanup(uncertain=True)
        self.assertIn("container_response_observed",dict(events))
        self.assertNotIn("container_probe_observation",dict(events))
        self.assertNotIn("rm",operations)

    def test_response_durability_failure_still_cleans_exact_worker(self):
        _,events,operations=self.simulated_cleanup(response_durable=False)
        self.assertIn("rm",operations)
        self.assertNotIn("container_probe_observation",dict(events))
