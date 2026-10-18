#!/usr/bin/env bun
/* Host-only AgentFanout → VLLMWorker bridge issuer. Never expose correlation in request. */
import { pathToFileURL } from "node:url";

const raw = await new Response(Bun.stdin).text();
const input = JSON.parse(raw);
if (!input || input.schema !== "AgentFanoutLocalBridgeLaunchInputV1" || !input.request || !input.observation || !input.correlation_id) {
  throw new Error("invalid_agentfanout_bridge_input");
}
const modulePath = process.env.AGENT_FANOUT_LOCAL_BRIDGE_MODULE
  || `${process.env.PAI_DIR || process.env.PAI_HOME || `${process.env.HOME}/.pai`}/skills/VLLMWorker/Tools/broker/host_bridge.ts`;
const bridge = await import(pathToFileURL(modulePath).href);
const request = structuredClone(input.request);
request.capability = {};
bridge.issueHostCapabilityAttestation(request, input.observation);
const usageCorrelation = bridge.createHostUsageCorrelation(request, input.correlation_id);
process.stdout.write(JSON.stringify({
  schema: "AgentFanoutLocalBridgeLaunchV1",
  request,
  usage_correlation: usageCorrelation,
}));
