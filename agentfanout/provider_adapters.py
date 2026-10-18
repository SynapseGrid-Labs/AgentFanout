"""Host-owned, non-interactive Codex and Grok process adapters.

The adapters accept a bounded prompt over stdin, never invoke a shell, and
return host-side launch/usage evidence. They deliberately do not know provider
credentials or change AgentFanout routing policy.
"""

from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import queue
import secrets
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence

try:  # POSIX-only executable sealing; importability is retained elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - AgentFanout's hosted runtime is POSIX-first.
    fcntl = None  # type: ignore[assignment]

from .project_binding import ProjectBindingError, validate_project_root


class ProviderAdapterError(ValueError):
    """A provider process cannot be safely discovered or launched."""


SENSITIVE_PROMPT = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[a-z0-9._~+/=-]{12,}|api[_-]?key\s*[=:]|"
    r"secret\s*[=:]|password\s*[=:]|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bsk-[a-z0-9_-]{12,}|\bghp_[a-z0-9_]{12,}|"
    r"\bxai-[a-z0-9_-]{12,}|\bglpat-[a-z0-9_-]{12,}|\bnpm_[a-z0-9_-]{12,}|\bxox[baprs]-[a-z0-9-]{12,})"
)
EFFORTS = {"low", "medium", "high", "xhigh", "ultra"}
MODEL_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
GROK_DIRECT_ENDPOINT = "https://api.x.ai/v1/chat/completions"
GROK_DIRECT_ENDPOINT_SHA256 = hashlib.sha256(GROK_DIRECT_ENDPOINT.encode()).hexdigest()
GROK_CHAT_COMPLETIONS_REASONING_MODEL = re.compile(r"^grok-4\.5$")
SESSION_UUID = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
MAX_CHILD_OUTPUT_BYTES = 1_000_000
ACP_PROTOCOL_VERSION = 1
ACP_MAX_MESSAGE_BYTES = 256_000
ACP_PREFLIGHT_QUIET_SECONDS = 0.2

# Raw tokens and generic configuration are never inherited by delegated
# workers. Auth may travel only through a host-managed directory or socket.
CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
    "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "CODEX_HOME", "CODEX_AUTH_SOCK", "GROK_HOME", "GROK_AUTH_SOCK",
})
CODEX_CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
    "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "CODEX_HOME", "CODEX_AUTH_SOCK",
})
GROK_CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
})
LOCAL_CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
})
CREDENTIAL_ENV_NAME = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|password|credential|private[_-]?key|access[_-]?key|auth(?!_sock$))"
)

# Codex does not currently expose one authoritative "no tools" switch.  The
# hosted adapter therefore disables every current native feature that can add
# a tool, ambient integration, external action, delegation path, workspace
# discovery path, or approval/elicitation side channel.  `features list` is
# checked immediately before every initial or resumed launch.  New *active*
# features fail closed until classified below.
CODEX_DENIED_TOOL_FEATURES = (
    "apply_patch_freeform", "apply_patch_streaming_events", "apps", "artifact",
    "auth_elicitation", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "chronicle", "code_mode", "code_mode_host",
    "code_mode_only", "computer_use", "default_mode_request_user_input",
    "deferred_executor", "enable_fanout", "enable_mcp_apps",
    "exec_permission_approvals", "goals", "guardian_approval", "hooks",
    "image_generation", "in_app_browser", "js_repl", "js_repl_tools_only",
    "memories", "multi_agent", "multi_agent_v2", "network_proxy",
    "non_prefixed_mcp_tool_names", "plugin_hooks", "plugin_sharing", "plugins",
    "remote_plugin", "request_permissions_tool", "respect_system_proxy",
    "search_tool", "secret_auth_storage", "shell_snapshot", "shell_tool",
    "shell_zsh_fork", "skill_mcp_dependency_install", "standalone_web_search",
    "tool_call_mcp_elicitation", "tool_search",
    "tool_search_always_defer_mcp_tools", "tool_suggest",
    "unavailable_dummy_tools", "unified_exec", "unified_exec_zsh_fork",
    "web_search_cached", "web_search_request", "workspace_dependencies",
)
CODEX_ALLOWED_ACTIVE_NON_TOOL_FEATURES = frozenset({
    "concurrent_reasoning_summaries", "current_time_reminder",
    "enable_request_compression", "fast_mode", "item_ids",
    "local_thread_store_compression", "mentions_v2", "personality",
    "prevent_idle_sleep", "realtime_conversation", "remote_compaction_v2",
    "resize_all_images", "rollout_budget", "runtime_metrics", "sqlite",
    "steer", "terminal_resize_reflow", "terminal_visualization_instructions",
    "token_budget", "tui_app_server", "use_agent_identity",
    "use_legacy_landlock", "workspace_owner_usage_nudge",
})
CODEX_FEATURE_LINE = re.compile(
    r"^([A-Za-z0-9_]+)\s+(stable|under development|experimental|deprecated|removed)\s+(true|false)$"
)
CODEX_NO_TOOL_CONFIG_ARGUMENTS = ("--config", 'web_search="disabled"')
CODEX_SAFE_ITEM_TYPES = frozenset({"agent_message", "reasoning"})
CODEX_FORBIDDEN_EVENT_MARKERS = (
    "tool", "command", "shell", "exec", "file_change", "patch", "mcp",
    "web_search", "browser", "computer", "image_generation", "subagent",
    "collaboration", "spawn_agent",
)
MAX_PROVIDER_BINARY_BYTES = 512 * 1024 * 1024
_CODEX_TRANSPORT_PROBE_CACHE: dict[str, dict[str, Any]] = {}
_CODEX_TRANSPORT_PROBE_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def minimal_child_environment(
    source: dict[str, str] | None = None,
    allowlist: frozenset[str] | None = None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return a minimal, auditable provider environment without raw credentials."""

    inherited = source if source is not None else dict(os.environ)
    selected_allowlist = allowlist if allowlist is not None else CHILD_ENV_ALLOWLIST
    environment: dict[str, str] = {}
    stripped: list[str] = []
    for key, value in inherited.items():
        if key not in selected_allowlist:
            if CREDENTIAL_ENV_NAME.search(key):
                stripped.append(key)
            continue
        if not isinstance(value, str) or (CREDENTIAL_ENV_NAME.search(key) and not key.endswith("_AUTH_SOCK")):
            stripped.append(key)
            continue
        environment[key] = value
    return environment, {
        "allowlist": sorted(selected_allowlist),
        "preserved": sorted(environment),
        "stripped_credential_names": sorted(set(stripped)),
    }


def _binary_argv(binary: str | Sequence[str]) -> list[str]:
    if isinstance(binary, str):
        if not binary.strip():
            raise ProviderAdapterError("provider_binary_required")
        argv = [binary]
    elif not isinstance(binary, Sequence) or isinstance(binary, (bytes, bytearray)):
        raise ProviderAdapterError("provider_binary_invalid")
    else:
        argv = list(binary)
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ProviderAdapterError("provider_binary_invalid")
    selected = argv[0]
    located = shutil.which(selected) if os.sep not in selected else selected
    if not located:
        raise ProviderAdapterError("provider_binary_unavailable")
    try:
        path = Path(located).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProviderAdapterError("provider_binary_unavailable") from exc
    argv[0] = str(path)
    # Provider launch prefixes may explicitly name an interpreter script.  If
    # so, freeze the actual file rather than carrying a cwd-relative spelling
    # into a later empty execution directory.
    for index, argument in enumerate(argv[1:], 1):
        candidate = Path(argument).expanduser()
        try:
            if candidate.is_file():
                argv[index] = str(candidate.resolve(strict=True))
        except OSError as exc:
            raise ProviderAdapterError("provider_argument_file_unavailable") from exc
    return argv


def _file_observation(candidate: Path, error: str = "provider_binary_unreadable") -> dict[str, Any]:
    """Hash one resolved regular file and retain replacement-sensitive identity."""

    try:
        resolved = candidate.expanduser().resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise ProviderAdapterError(error) from exc
    if not resolved.is_file() or info.st_size < 0 or info.st_size > MAX_PROVIDER_BINARY_BYTES:
        raise ProviderAdapterError(error)
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ProviderAdapterError(error) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
            or opened.st_size != info.st_size
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise ProviderAdapterError("provider_executable_identity_changed")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return {
        "realpath": str(resolved), "sha256": digest.hexdigest(),
        "bytes": info.st_size, "device": info.st_dev, "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
    }


def _codex_platform_target() -> tuple[str, str] | None:
    machine = platform.machine().lower()
    if sys.platform.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "x86_64-unknown-linux-musl", "codex-linux-x64"
        if machine in {"aarch64", "arm64"}:
            return "aarch64-unknown-linux-musl", "codex-linux-arm64"
    if sys.platform == "darwin":
        if machine in {"x86_64", "amd64"}:
            return "x86_64-apple-darwin", "codex-darwin-x64"
        if machine in {"aarch64", "arm64"}:
            return "aarch64-apple-darwin", "codex-darwin-arm64"
    return None


def _shebang_chain(path: Path) -> list[dict[str, Any]]:
    """Bind the interpreter path used by a directly executed script."""

    try:
        with path.open("rb") as handle:
            raw = handle.readline(4096)
        if not raw.startswith(b"#!"):
            return []
        first = raw.decode("utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ProviderAdapterError("provider_binary_unreadable") from exc
    try:
        tokens = shlex.split(first[2:].strip())
    except ValueError as exc:
        raise ProviderAdapterError("provider_shebang_invalid") from exc
    if not tokens:
        raise ProviderAdapterError("provider_shebang_invalid")
    interpreter = Path(tokens[0]).expanduser()
    if not interpreter.is_absolute():
        raise ProviderAdapterError("provider_shebang_interpreter_not_absolute")
    observations = [{"role": "shebang_interpreter", **_file_observation(interpreter)}]
    if interpreter.name == "env":
        command_tokens = [token for token in tokens[1:] if token not in {"-S", "--split-string"}]
        if not command_tokens or command_tokens[0].startswith("-"):
            raise ProviderAdapterError("provider_env_shebang_unsupported")
        located = shutil.which(command_tokens[0])
        if not located:
            raise ProviderAdapterError("provider_shebang_runtime_unavailable")
        observations.append({
            "role": "shebang_runtime", "command": command_tokens[0],
            **_file_observation(Path(located)),
        })
    return observations


def _official_codex_wrapper_chain(wrapper: Path) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Resolve the native target referenced by the official npm Codex wrapper.

    Arbitrary JavaScript wrappers are not guessed.  The package name, wrapper
    markers, platform package manifest, and final native executable must all
    match the official on-disk layout or the wrapper is treated as unsupported.
    """

    if wrapper.name != "codex.js" or wrapper.parent.name != "bin":
        return None
    try:
        source = wrapper.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProviderAdapterError("codex_wrapper_unreadable") from exc
    required_markers = (
        "PLATFORM_PACKAGE_BY_TARGET", "findCodexExecutable", "@openai/codex-linux-x64",
        "targetTriple", "vendor", "bin", "spawn(binaryPath",
    )
    if any(marker not in source for marker in required_markers):
        return None
    package_root = wrapper.parent.parent
    package_manifest = package_root / "package.json"
    try:
        package_data = json.loads(package_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderAdapterError("codex_wrapper_package_invalid") from exc
    if not isinstance(package_data, dict) or package_data.get("name") != "@openai/codex":
        return None
    target = _codex_platform_target()
    if target is None:
        raise ProviderAdapterError("codex_wrapper_platform_unsupported")
    target_triple, platform_package = target
    platform_root = package_root / "node_modules" / "@openai" / platform_package
    platform_manifest = platform_root / "package.json"
    native = platform_root / "vendor" / target_triple / "bin" / "codex"
    if not platform_manifest.is_file() or not native.is_file():
        platform_manifest = package_manifest
        native = package_root / "vendor" / target_triple / "bin" / "codex"
    if not native.is_file():
        raise ProviderAdapterError("codex_wrapper_native_target_unavailable")
    chain = [
        {"role": "codex_package_manifest", **_file_observation(package_manifest)},
        {"role": "codex_platform_package_manifest", **_file_observation(platform_manifest)},
        {"role": "codex_native_target", **_file_observation(native)},
    ]
    return chain, dict(chain[-1])


def executable_identity(binary: str | Sequence[str]) -> dict[str, Any]:
    """Return a replacement-sensitive identity for the full launch prefix.

    For the official npm Codex entrypoint this includes `/usr/bin/env`, Node,
    both package manifests, and the referenced native binary.  That closes the
    former wrapper-only identity gap while keeping direct native/interpreter
    launch prefixes portable.
    """

    argv = _binary_argv(binary)
    path = Path(argv[0])
    primary = _file_observation(path)
    argument_files: list[dict[str, Any]] = []
    for index, argument in enumerate(argv[1:], 1):
        candidate = Path(argument).expanduser()
        try:
            if not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ProviderAdapterError("provider_argument_file_unavailable") from exc
        argument_files.append({"argv_index": index, **_file_observation(resolved, "provider_argument_file_invalid")})

    launcher_chain: list[dict[str, Any]] = _shebang_chain(path)
    codex_native: dict[str, Any] | None = None
    codex_wrapper_argv_index: int | None = None
    wrapper_candidates = [(0, path), *[(item["argv_index"], Path(item["realpath"])) for item in argument_files]]
    for index, candidate in wrapper_candidates:
        detected = _official_codex_wrapper_chain(candidate)
        if detected is None:
            continue
        chain, codex_native = detected
        codex_wrapper_argv_index = index
        launcher_chain.extend(_shebang_chain(candidate))
        launcher_chain.extend(chain)
        break
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in launcher_chain:
        marker = (str(item.get("role")), str(item.get("realpath")))
        if marker not in seen:
            seen.add(marker)
            deduplicated.append(item)
    return {
        "executable_realpath": primary["realpath"], "executable_sha256": primary["sha256"],
        "executable_bytes": primary["bytes"], "executable_device": primary["device"],
        "executable_inode": primary["inode"],
        "launch_argv_sha256": hashlib.sha256(
            json.dumps(argv, sort_keys=False, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest(),
        "argument_file_identities": argument_files,
        "launcher_chain_identities": deduplicated,
        "codex_native_executable_identity": codex_native,
        "codex_wrapper_argv_index": codex_wrapper_argv_index,
    }


def verify_executable_identity(binary: str | Sequence[str], expected: dict[str, Any]) -> dict[str, Any]:
    """Recheck an operator-selected executable against one frozen host observation."""

    observed = executable_identity(binary)
    fields = (
        "executable_realpath", "executable_sha256", "executable_bytes",
        "executable_device", "executable_inode", "launch_argv_sha256",
        "argument_file_identities", "launcher_chain_identities",
        "codex_native_executable_identity", "codex_wrapper_argv_index",
    )
    if not isinstance(expected, dict) or any(observed.get(field) != expected.get(field) for field in fields):
        raise ProviderAdapterError("provider_executable_identity_changed")
    return observed


def exact_project_root(project_root: str | Path) -> Path:
    """Use the shared project authority; keep the historical adapter error type."""
    try:
        root = validate_project_root(project_root)
    except ProjectBindingError as exc:
        raise ProviderAdapterError(str(exc)) from exc
    configured = os.getenv("AGENT_FANOUT_PROJECT_ROOT", "").strip()
    if configured:
        try:
            configured_root = validate_project_root(configured)
        except ProjectBindingError as exc:
            raise ProviderAdapterError(str(exc)) from exc
        if root != configured_root:
            raise ProviderAdapterError("project_root_not_host_approved")
    return root


def sanitized_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 128_000:
        raise ProviderAdapterError("delegated_prompt_invalid")
    if SENSITIVE_PROMPT.search(prompt):
        raise ProviderAdapterError("delegated_prompt_sensitive")
    return prompt


def _parse_json_lines(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _parse_json_output(output: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return _parse_json_lines(output)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _models_from_output(output: str) -> list[dict[str, Any]]:
    """Parse JSON/JSONL model listings without accepting arbitrary prose slugs."""

    values: list[Any] = []
    try:
        values.append(json.loads(output))
    except json.JSONDecodeError:
        values.extend(_parse_json_lines(output))
    models: list[dict[str, Any]] = []
    for value in values:
        raw_models = value.get("models", []) if isinstance(value, dict) else value
        if not isinstance(raw_models, list):
            continue
        for item in raw_models:
            if isinstance(item, str):
                if MODEL_SLUG.fullmatch(item):
                    models.append({"id": item, "profiles": []})
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("slug") or item.get("name")
                if isinstance(model_id, str) and MODEL_SLUG.fullmatch(model_id):
                    profiles = item.get("profiles", item.get("profile", item.get("tier", [])))
                    if isinstance(profiles, str):
                        profiles = [profiles]
                    models.append({"id": model_id, "profiles": [str(profile) for profile in profiles] if isinstance(profiles, list) else []})
    # `codex debug models` may render a human table on older hosts. Accept only
    # slug-shaped tokens that carry a known routing label; never infer a model
    # from arbitrary prose or an environment default.
    if not models:
        labels = {"luna", "terra", "sol", "fast", "balanced", "frontier"}
        for line in output.splitlines():
            tokens = re.findall(r"[A-Za-z][A-Za-z0-9._:-]{1,200}", line)
            matches = [token for token in tokens if any(label in token.lower() for label in labels)]
            if matches:
                model_id = max(matches, key=len)
                profiles = [label for label in labels if label in model_id.lower()]
                models.append({"id": model_id, "profiles": profiles})
    if not models:
        raise ProviderAdapterError("provider_model_discovery_unparseable")
    return models


def _discover(
    binary: str | Sequence[str], arguments: list[str], timeout_seconds: float = 15,
    *, provider: str, expected_executable_identity: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    output = _run_bounded_discovery(
        binary, arguments, timeout_seconds, provider=provider,
        expected_executable_identity=expected_executable_identity,
    )
    return _models_from_output(output)


def _run_bounded_discovery(
    binary: str | Sequence[str], arguments: list[str], timeout_seconds: float = 15,
    *, provider: str, expected_executable_identity: dict[str, Any] | None = None,
    grok_home: str | Path | None = None,
    expected_grok_auth_file_identity: dict[str, Any] | None = None,
    expected_grok_helper_identity: dict[str, Any] | None = None,
) -> str:
    """Run discovery only from one verified, descriptor-pinned launch chain."""

    observed = (
        verify_executable_identity(binary, expected_executable_identity)
        if expected_executable_identity is not None else executable_identity(binary)
    )
    if provider == "Codex":
        prefix, pinned_executable, pinned_arguments = _codex_effective_launch_prefix(binary, observed)
        environment_allowlist = CODEX_CHILD_ENV_ALLOWLIST
    elif provider == "Grok":
        # Fresh user homes cannot override system-managed login/auth-provider
        # policy. This adapter has no authority to reinterpret those policies.
        if Path("/etc/grok").exists() or Path("/etc/grok").is_symlink():
            raise ProviderAdapterError("grok_system_configuration_requires_review")
        prefix, pinned_executable, pinned_arguments = _generic_effective_launch_prefix(binary, observed, "grok")
        environment_allowlist = GROK_CHILD_ENV_ALLOWLIST
    else:
        raise ProviderAdapterError("provider_discovery_kind_invalid")
    if provider == "Grok" and grok_home is not None:
        if (
            not isinstance(expected_grok_auth_file_identity, dict)
            or expected_grok_auth_file_identity.get("schema")
            != "GrokAuthFileIdentityV1"
        ):
            raise ProviderAdapterError("grok_auth_file_identity_required")
        if not isinstance(expected_grok_helper_identity, dict):
            raise ProviderAdapterError("grok_http_helper_identity_required")
        # Validate the bound API credential before a third-party CLI starts.
        # Session/OIDC stores are not accepted by the direct API transport.
        grok_direct_transport_preflight(
            grok_home,
            expected_grok_auth_file_identity,
            expected_grok_helper_identity,
            timeout_seconds=max(1, min(float(timeout_seconds), 60)),
        )
    with tempfile.TemporaryDirectory(prefix=f"agentfanout-{provider.lower()}-discovery-") as directory:
        execution_root = Path(directory)
        environment_overrides: dict[str, str] | None = None
        if provider == "Grok":
            # Omitting HOME does not isolate it: runtimes can recover the
            # account home through passwd. Force both homes into this
            # disposable root. Supply only the bound API key through the
            # documented XAI_API_KEY interface, never a CLI session store.
            user_home = execution_root / "user-home"
            isolated_grok_home = execution_root / "grok-home"
            user_home.mkdir(mode=0o700)
            if grok_home is not None:
                if (
                    not isinstance(expected_grok_auth_file_identity, dict)
                    or expected_grok_auth_file_identity.get("schema") != "GrokAuthFileIdentityV1"
                ):
                    raise ProviderAdapterError("grok_auth_file_identity_required")
                credential_copy = execution_root / "credential"
                _prepare_ephemeral_grok_home(
                    _secure_grok_home(grok_home), credential_copy,
                    expected_grok_auth_file_identity,
                )
                from .grok_http_worker import GrokHttpError, _read_auth_token

                try:
                    api_key = _read_auth_token(
                        str(credential_copy / "auth.json"),
                        expected_grok_auth_file_identity["auth_file_sha256"],
                    )
                except GrokHttpError as exc:
                    raise ProviderAdapterError(str(exc)) from exc
                # The CLI has its own auth-store format and precedence. An
                # empty root plus an explicit key prevents session fallback.
                (credential_copy / "auth.json").unlink()
                credential_copy.rmdir()
                isolated_grok_home.mkdir(mode=0o700)
            else:
                isolated_grok_home.mkdir(mode=0o700)
            environment_overrides = {
                "HOME": str(user_home),
                "GROK_HOME": str(isolated_grok_home),
            }
            if grok_home is not None:
                environment_overrides["XAI_API_KEY"] = api_key
        try:
            result = run_json_process(
                [*prefix, *arguments], "", execution_root,
                timeout_seconds=max(1, min(float(timeout_seconds), 60)),
                environment_allowlist=environment_allowlist,
                environment_overrides=environment_overrides,
                pinned_executable_identity=pinned_executable,
                pinned_argument_file_identities=pinned_arguments,
            )
            if provider == "Grok" and grok_home is not None:
                if any(_grok_credential_echo(result.get(name, ""), api_key) for name in ("stdout", "stderr")):
                    raise ProviderAdapterError("grok_discovery_credential_echo_denied")
        finally:
            if environment_overrides is not None:
                environment_overrides.pop("XAI_API_KEY", None)
            api_key = ""
    if result["status"] in {"timeout", "output_limit", "cancelled"}:
        raise ProviderAdapterError("provider_model_discovery_unavailable")
    if result["status"] != "completed" or result["returncode"] != 0:
        raise ProviderAdapterError("provider_model_discovery_failed")
    return result["stdout"]


def _grok_credential_echo(output: str, api_key: str) -> bool:
    """Reject raw and JSON-decoded secrets before catalog normalization.

    Inspect string tokens independently so duplicate object keys, JSONL, and
    discarded fields cannot hide an encoded credential from this check.
    This does not claim to detect every possible encoding or secret fragment.
    """

    if api_key in output:
        return True
    for match in re.finditer(r'"(?:[^"\\\x00-\x1f]|\\[^\x00-\x1f])*"', output):
        try:
            decoded = json.loads(match.group())
        except ValueError:
            continue
        if isinstance(decoded, str) and api_key in decoded:
            return True
    return False


def discover_codex_models(
    binary: str | Sequence[str] = "codex", timeout_seconds: float = 15,
    *, expected_executable_identity: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Discover currently listed Codex models using `codex debug models`."""

    return _discover(
        binary, ["debug", "models"], timeout_seconds, provider="Codex",
        expected_executable_identity=expected_executable_identity,
    )


def _codex_feature_states(output: str) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for raw in output.splitlines():
        match = CODEX_FEATURE_LINE.fullmatch(raw.strip())
        if match:
            name, lifecycle, enabled = match.groups()
            states[name] = {"lifecycle": lifecycle, "enabled": enabled == "true"}
    if not states:
        raise ProviderAdapterError("codex_feature_inventory_unparseable")
    return states


def _codex_feature_overrides(states: dict[str, dict[str, Any]]) -> list[str]:
    """Disable only supported live features; never pass removed/deprecated flags."""

    arguments: list[str] = []
    for feature in CODEX_DENIED_TOOL_FEATURES:
        observed = states.get(feature)
        if observed and observed.get("lifecycle") not in {"removed", "deprecated"}:
            arguments.extend(["--disable", feature])
    return arguments


def _codex_no_tool_feature_preflight(
    binary: str | Sequence[str], timeout_seconds: float = 15, *,
    expected_executable_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive supported denies, then prove the effective no-tool feature set."""

    baseline_output = _run_bounded_discovery(
        binary, [*CODEX_NO_TOOL_CONFIG_ARGUMENTS, "features", "list"], timeout_seconds,
        provider="Codex", expected_executable_identity=expected_executable_identity,
    )
    baseline_states = _codex_feature_states(baseline_output)
    feature_overrides = _codex_feature_overrides(baseline_states)
    effective_output = _run_bounded_discovery(
        binary,
        [*CODEX_NO_TOOL_CONFIG_ARGUMENTS, *feature_overrides, "features", "list"],
        timeout_seconds, provider="Codex",
        expected_executable_identity=expected_executable_identity,
    )
    states = _codex_feature_states(effective_output)
    unified = states.get("unified_exec")
    if not isinstance(unified, dict) or unified.get("lifecycle") == "removed" or unified.get("enabled") is not False:
        raise ProviderAdapterError("codex_unified_exec_not_denied")
    for name in CODEX_DENIED_TOOL_FEATURES:
        observed = states.get(name)
        if observed and observed["lifecycle"] != "removed" and observed["enabled"] is not False:
            raise ProviderAdapterError(f"codex_tool_feature_not_denied:{name}")
    unknown_active = sorted(
        name for name, observed in states.items()
        if observed["lifecycle"] != "removed"
        and observed["enabled"] is True
        and name not in CODEX_DENIED_TOOL_FEATURES
        and name not in CODEX_ALLOWED_ACTIVE_NON_TOOL_FEATURES
    )
    if unknown_active:
        raise ProviderAdapterError(f"codex_unknown_active_feature:{unknown_active[0]}")
    canonical = json.dumps(states, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {
        "inventory_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "feature_count": len(states),
        "unified_exec": False,
        "denied_features": list(CODEX_DENIED_TOOL_FEATURES),
        "launch_feature_overrides": feature_overrides,
        "launch_config_arguments": list(CODEX_NO_TOOL_CONFIG_ARGUMENTS),
        "skipped_deprecated_or_removed_features": sorted(
            name for name in CODEX_DENIED_TOOL_FEATURES
            if baseline_states.get(name, {}).get("lifecycle") in {"removed", "deprecated"}
        ),
        "legacy_landlock_enabled": False,
        "unknown_active_features": [],
    }


def discover_grok_models(
    binary: str | Sequence[str], timeout_seconds: float = 15,
    *, expected_executable_identity: dict[str, Any] | None = None,
    grok_home: str | Path | None = None,
    expected_grok_auth_file_identity: dict[str, Any] | None = None,
    expected_grok_helper_identity: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Discover currently available Grok worker models from the host CLI."""

    output = _run_bounded_discovery(
        binary, ["models"], timeout_seconds, provider="Grok",
        expected_executable_identity=expected_executable_identity,
        grok_home=grok_home,
        expected_grok_auth_file_identity=expected_grok_auth_file_identity,
        expected_grok_helper_identity=expected_grok_helper_identity,
    )
    models: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = re.match(r"^\s*([*-])\s+([A-Za-z0-9._:-]+)(?:\s+\(default\))?\s*$", line)
        if not match:
            continue
        model_id = match.group(2)
        # AgentFanout's typed packet lane is currently proven only on exact
        # grok-4.5.  One model can serve fast/balanced/frontier by varying its
        # supported reasoning effort.  Discovery-visible Composer models are
        # not labeled capable: live strict-schema probes returned nonconforming
        # evidence packets even when the request was accepted.
        profiles = ["fast", "balanced", "frontier"] if model_id == "grok-4.5" else []
        models.append({"id": model_id, "profiles": profiles})
    if not models:
        return _models_from_output(output)
    return models


def _resolve_profile(models: list[dict[str, Any]], aliases: set[str], provider: str) -> str:
    for model in models:
        profiles = {item.lower() for item in model.get("profiles", [])}
        model_id = str(model["id"])
        if not MODEL_SLUG.fullmatch(model_id):
            continue
        lowered = model_id.lower()
        if profiles & aliases or any(alias in lowered for alias in aliases):
            return model_id
    raise ProviderAdapterError(f"{provider}_requested_profile_not_listed")


def _configured_model(env_name: str, models: list[dict[str, Any]], provider: str) -> str | None:
    configured = os.getenv(env_name, "").strip()
    if not configured:
        return None
    if not MODEL_SLUG.fullmatch(configured):
        raise ProviderAdapterError(f"{provider}_configured_model_invalid")
    if configured not in {str(item.get("id")) for item in models}:
        raise ProviderAdapterError(f"{provider}_configured_model_not_listed")
    return configured


def resolve_codex_model(selector: str, models: list[dict[str, Any]]) -> str:
    aliases = {
        "luna": {"luna"}, "current-fast-mini": {"luna"},
        "terra": {"terra"}, "current-balanced": {"terra"},
        "sol": {"sol"}, "current-frontier": {"sol"},
        "astra": {"astra", "gpt-6-astra"}, "astra-explicit": {"astra", "gpt-6-astra"},
    }
    if selector not in aliases:
        raise ProviderAdapterError("codex_selector_unsupported")
    environment = {
        "luna": "AGENT_FANOUT_CODEX_FAST_MODEL", "current-fast-mini": "AGENT_FANOUT_CODEX_FAST_MODEL",
        "terra": "AGENT_FANOUT_CODEX_BALANCED_MODEL", "current-balanced": "AGENT_FANOUT_CODEX_BALANCED_MODEL",
        "sol": "AGENT_FANOUT_CODEX_FRONTIER_MODEL", "current-frontier": "AGENT_FANOUT_CODEX_FRONTIER_MODEL",
        "astra": "AGENT_FANOUT_CODEX_ASTRA_MODEL", "astra-explicit": "AGENT_FANOUT_CODEX_ASTRA_MODEL",
    }
    configured = _configured_model(environment[selector], models, "codex")
    resolved = configured or _resolve_profile(models, aliases[selector], "codex")
    if selector in {"sol", "current-frontier"} and resolved == "gpt-6-astra":
        raise ProviderAdapterError("codex_astra_requires_explicit_selector")
    if selector in {"astra", "astra-explicit"} and resolved != "gpt-6-astra":
        raise ProviderAdapterError("codex_astra_exact_model_required")
    return resolved


def resolve_grok_model(selector: str, models: list[dict[str, Any]]) -> str:
    aliases = {
        "fast": {"fast"}, "grok-fast": {"fast"},
        "balanced": {"balanced"}, "grok-balanced": {"balanced"},
        "frontier": {"frontier"}, "grok-frontier": {"frontier"},
    }
    if selector not in aliases:
        raise ProviderAdapterError("grok_selector_unsupported")
    environment = {
        "fast": "AGENT_FANOUT_GROK_FAST_MODEL", "grok-fast": "AGENT_FANOUT_GROK_FAST_MODEL",
        "balanced": "AGENT_FANOUT_GROK_BALANCED_MODEL", "grok-balanced": "AGENT_FANOUT_GROK_BALANCED_MODEL",
        "frontier": "AGENT_FANOUT_GROK_FRONTIER_MODEL", "grok-frontier": "AGENT_FANOUT_GROK_FRONTIER_MODEL",
    }
    configured = _configured_model(environment[selector], models, "grok")
    if configured:
        if not GROK_CHAT_COMPLETIONS_REASONING_MODEL.fullmatch(configured):
            raise ProviderAdapterError("grok_configured_model_packet_contract_unsupported")
        return configured
    if any(item.get("id") == "grok-4.5" for item in models):
        return "grok-4.5"
    raise ProviderAdapterError("grok_packet_capable_model_not_listed")


def _process_group_exists(process_group_id: int) -> bool:
    """Return whether a POSIX process group still has any members."""

    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A group that exists but cannot be inspected is not proof of exit.
        return True
    return True


def _wait_for_process_group_exit(
    process: subprocess.Popen[bytes], process_group_id: int, timeout_seconds: float,
) -> bool:
    """Reap the direct child while waiting for every process-group member to exit."""

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        # poll() reaps the direct child when it has exited. Descendants are
        # reaped by their new parent after the session leader exits.
        process.poll()
        if not _process_group_exists(process_group_id):
            return process.returncode is not None
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def terminate_process_group(
    process: subprocess.Popen[bytes], process_group_id: int,
    *, term_grace_seconds: float = 0.25, kill_grace_seconds: float = 1.5,
) -> bool:
    """Terminate and verify the original provider process group only.

    ``start_new_session=True`` makes the launched process PID the process-group
    ID. The caller captures that ID at launch so termination remains possible
    after the group leader exits. Returning ``False`` means exit could not be
    proven and callers must fail closed.
    """

    if os.name != "posix" or not hasattr(os, "killpg"):
        # AgentFanout's hosted adapters are POSIX-first. Retain a bounded
        # single-process fallback for importability on other platforms, but do
        # not claim descendant-process confirmation there.
        try:
            process.terminate()
            process.wait(timeout=term_grace_seconds)
        except ProcessLookupError:
            process.poll()
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=kill_grace_seconds)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                return False
        return process.returncode is not None

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        process.poll()
        return process.returncode is not None and not _process_group_exists(process_group_id)
    except (OSError, ValueError):
        return False

    if _wait_for_process_group_exit(process, process_group_id, term_grace_seconds):
        return True

    # The leader may already be reaped while a child ignores SIGTERM. Signal
    # the original group ID rather than falling back to the leader PID.
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (OSError, ValueError):
        return False

    return _wait_for_process_group_exit(process, process_group_id, kill_grace_seconds)


def _primary_executable_observation(identity: dict[str, Any]) -> dict[str, Any]:
    required = {
        "realpath": identity.get("executable_realpath"),
        "sha256": identity.get("executable_sha256"),
        "bytes": identity.get("executable_bytes"),
        "device": identity.get("executable_device"),
        "inode": identity.get("executable_inode"),
    }
    if (
        not isinstance(required["realpath"], str)
        or not isinstance(required["sha256"], str)
        or len(required["sha256"]) != 64
        or any(isinstance(required[key], bool) or not isinstance(required[key], int) for key in ("bytes", "device", "inode"))
    ):
        raise ProviderAdapterError("provider_executable_identity_invalid")
    return required


def _generic_effective_launch_prefix(
    binary: str | Sequence[str], identity: dict[str, Any], provider: str,
) -> tuple[list[str], dict[str, Any], list[dict[str, Any]]]:
    """Return one supported direct-native or fully pinned Python launch chain."""

    argv = _binary_argv(binary)
    primary_path = Path(argv[0])
    shebang = _shebang_chain(primary_path)
    if shebang:
        runtime = next((item for item in reversed(shebang) if item.get("role") in {"shebang_runtime", "shebang_interpreter"}), None)
        runtime_name = Path(str(runtime.get("realpath", ""))).name if isinstance(runtime, dict) else ""
        # Direct Python provider fixtures/adapters have no hidden second
        # launcher: pin both the interpreter and complete script. Shell and
        # arbitrary Node wrappers can select unbound native/package targets
        # and are rejected unless recognized as the official Codex wrapper.
        if not runtime_name.startswith("python"):
            raise ProviderAdapterError(f"{provider}_unsupported_interpreted_wrapper")
        script_identity = {"argv_index": 1, **_primary_executable_observation(identity)}
        adjusted_arguments = [
            {**item, "argv_index": int(item["argv_index"]) + 1}
            for item in identity.get("argument_file_identities", [])
        ]
        return [str(runtime["realpath"]), str(primary_path), *argv[1:]], runtime, [script_identity, *adjusted_arguments]
    if any(Path(item.get("realpath", "")).suffix == ".js" for item in identity.get("argument_file_identities", [])):
        raise ProviderAdapterError(f"{provider}_unsupported_interpreted_wrapper")
    return argv, _primary_executable_observation(identity), list(identity.get("argument_file_identities", []))


def _codex_effective_launch_prefix(
    binary: str | Sequence[str], identity: dict[str, Any],
) -> tuple[list[str], dict[str, Any], list[dict[str, Any]]]:
    """Bypass the official npm wrapper and execute its frozen native target."""

    argv = _binary_argv(binary)
    native = identity.get("codex_native_executable_identity")
    wrapper_index = identity.get("codex_wrapper_argv_index")
    if isinstance(native, dict):
        if isinstance(wrapper_index, bool) or not isinstance(wrapper_index, int) or not 0 <= wrapper_index < len(argv):
            raise ProviderAdapterError("codex_wrapper_identity_invalid")
        # Everything before the wrapper belongs to its interpreter. The
        # native program receives only the arguments that followed codex.js.
        return [str(native.get("realpath")), *argv[wrapper_index + 1:]], native, []
    return _generic_effective_launch_prefix(binary, identity, "codex")


def _prepare_pinned_executable(identity: dict[str, Any]) -> tuple[int, str, str]:
    """Copy one verified executable into an anonymous, immutable descriptor.

    Use a sealed memfd where available; otherwise use an unlinked, owner-only
    read-only descriptor through the available self-fd path. Unsupported
    platforms fail closed. Both remove pathname replacement between
    verification and `exec`; a process already controlling the same uid remains
    outside this boundary.
    """

    if os.name != "posix":
        raise ProviderAdapterError("provider_descriptor_execution_posix_required")
    realpath = identity.get("realpath")
    digest_expected = identity.get("sha256")
    expected_fields = (identity.get("bytes"), identity.get("device"), identity.get("inode"))
    if (
        not isinstance(realpath, str)
        or not isinstance(digest_expected, str)
        or len(digest_expected) != 64
        or any(isinstance(value, bool) or not isinstance(value, int) for value in expected_fields)
    ):
        raise ProviderAdapterError("provider_pinned_executable_identity_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source = os.open(realpath, flags)
    except OSError as exc:
        raise ProviderAdapterError("provider_pinned_executable_unavailable") from exc
    target = -1
    try:
        source_info = os.fstat(source)
        if (
            not stat.S_ISREG(source_info.st_mode)
            or (source_info.st_size, source_info.st_dev, source_info.st_ino) != expected_fields
        ):
            raise ProviderAdapterError("provider_executable_identity_changed")
        descriptor_root = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
        if not Path(descriptor_root).is_dir():
            raise ProviderAdapterError("provider_descriptor_execution_unavailable")
        if hasattr(os, "memfd_create") and sys.platform.startswith("linux"):
            memfd_flags = getattr(os, "MFD_CLOEXEC", 0) | getattr(os, "MFD_ALLOW_SEALING", 0)
            target = os.memfd_create("agentfanout-provider", memfd_flags)
            method = "sealed_memfd"
        else:
            directory = tempfile.mkdtemp(prefix="agentfanout-exec-")
            path = Path(directory) / "provider"
            target = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o500)
            os.unlink(path)
            os.rmdir(directory)
            method = "unlinked_descriptor"
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            view = memoryview(chunk)
            written = 0
            while written < len(view):
                written += os.write(target, view[written:])
        if total != source_info.st_size or digest.hexdigest() != digest_expected:
            raise ProviderAdapterError("provider_executable_identity_changed")
        os.fchmod(target, 0o500)
        os.fsync(target)
        os.lseek(target, 0, os.SEEK_SET)
        if method == "sealed_memfd":
            if fcntl is None or not all(hasattr(fcntl, name) for name in (
                "F_ADD_SEALS", "F_SEAL_SEAL", "F_SEAL_SHRINK", "F_SEAL_GROW", "F_SEAL_WRITE",
            )):
                raise ProviderAdapterError("provider_memfd_sealing_unavailable")
            seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
            try:
                fcntl.fcntl(target, fcntl.F_ADD_SEALS, seals)
            except OSError as exc:
                raise ProviderAdapterError("provider_memfd_sealing_failed") from exc
        else:
            # An open writable descriptor prevents exec on Linux (ETXTBSY).
            # Reopen our anonymous copy read-only before dropping that writer.
            # Some /dev/fd implementations duplicate flags rather than reopen;
            # fail closed unless the returned descriptor really is read-only.
            if fcntl is None:
                raise ProviderAdapterError("provider_readonly_descriptor_unavailable")
            readonly = os.open(
                f"{descriptor_root}/{target}",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                copied = os.fstat(target)
                reopened = os.fstat(readonly)
                if (
                    not stat.S_ISREG(reopened.st_mode)
                    or (reopened.st_dev, reopened.st_ino, reopened.st_size)
                    != (copied.st_dev, copied.st_ino, copied.st_size)
                    or fcntl.fcntl(readonly, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
                ):
                    raise ProviderAdapterError("provider_readonly_descriptor_unavailable")
            except BaseException:
                os.close(readonly)
                raise
            writer = target
            target = readonly
            os.close(writer)
        return target, f"{descriptor_root}/{target}", method
    except BaseException:
        if target >= 0:
            os.close(target)
        raise
    finally:
        os.close(source)


def _prepare_pinned_launch_argv(
    argv: Sequence[str], pinned_executable_identity: dict[str, Any] | None,
    pinned_argument_file_identities: Sequence[dict[str, Any]],
) -> tuple[list[str], list[int], dict[str, Any]]:
    """Replace executable/script paths with verified anonymous descriptors."""

    launch_argv = list(argv)
    pinned_descriptors: list[int] = []
    pinning_method: str | None = None
    try:
        if pinned_executable_identity is not None:
            descriptor, descriptor_path, pinning_method = _prepare_pinned_executable(
                pinned_executable_identity,
            )
            pinned_descriptors.append(descriptor)
            launch_argv[0] = descriptor_path
        for identity in pinned_argument_file_identities:
            index = identity.get("argv_index") if isinstance(identity, dict) else None
            if isinstance(index, bool) or not isinstance(index, int) or not 0 < index < len(launch_argv):
                raise ProviderAdapterError("provider_pinned_argument_identity_invalid")
            descriptor, descriptor_path, argument_method = _prepare_pinned_executable(identity)
            if pinning_method not in (None, argument_method):
                os.close(descriptor)
                raise ProviderAdapterError("provider_pinning_method_mismatch")
            pinning_method = argument_method
            pinned_descriptors.append(descriptor)
            launch_argv[index] = descriptor_path
    except Exception:
        for descriptor in pinned_descriptors:
            os.close(descriptor)
        raise
    return launch_argv, pinned_descriptors, {
        "enabled": pinned_executable_identity is not None,
        "method": pinning_method,
        "argument_file_count": len(pinned_argument_file_identities),
        "same_uid_process_isolation": False,
    }


def _codex_no_tool_transport_attestation(
    binary: str | Sequence[str], executable_identity_record: dict[str, Any],
    feature_preflight: dict[str, Any], resolved_model: str, *,
    session_mode: str = "ephemeral", timeout_seconds: float = 8,
) -> dict[str, Any]:
    """Inspect a real Codex request through a loopback-only dummy transport.

    Feature flags are not proof of the final Responses payload.  This probe
    launches the exact pinned binary with dummy credentials and a numeric
    loopback provider, captures the first request, and accepts only an empty
    `tools` array with no tool choice. It never forwards a request or uses the
    operator's provider credentials.
    """

    if not isinstance(resolved_model, str) or not MODEL_SLUG.fullmatch(resolved_model):
        raise ProviderAdapterError("codex_transport_probe_model_invalid")
    if session_mode not in {"ephemeral", "persistent", "resumed"}:
        raise ProviderAdapterError("codex_transport_probe_session_mode_invalid")
    # The exact resume request has not yet been independently captured.  Do
    # not infer its authority from the initial ephemeral command even when the
    # shared flag prefix is identical.
    if session_mode != "ephemeral":
        raise ProviderAdapterError(
            f"codex_native_session_transport_attestation_unavailable:{session_mode}"
        )
    prefix, pinned_executable, pinned_arguments = _codex_effective_launch_prefix(
        binary, executable_identity_record,
    )
    cache_material = {
        "executable_sha256": executable_identity_record.get("executable_sha256"),
        "launch_argv_sha256": executable_identity_record.get("launch_argv_sha256"),
        "argument_file_identities": executable_identity_record.get("argument_file_identities"),
        "native_sha256": (
            executable_identity_record.get("codex_native_executable_identity") or {}
        ).get("sha256"),
        "inventory_sha256": feature_preflight.get("inventory_sha256"),
        "feature_overrides": feature_preflight.get("launch_feature_overrides"),
        "config_arguments": feature_preflight.get("launch_config_arguments"),
        "resolved_model": resolved_model,
        "session_mode": session_mode,
    }
    cache_key = hashlib.sha256(
        json.dumps(cache_material, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    with _CODEX_TRANSPORT_PROBE_LOCK:
        cached = _CODEX_TRANSPORT_PROBE_CACHE.get(cache_key)
    if cached is not None:
        if cached.get("tools_empty") is not True:
            raise ProviderAdapterError(str(cached.get("block_reason")))
        return dict(cached)

    captured: list[dict[str, Any]] = []
    capture_event = threading.Event()
    capture_error: list[str] = []
    probe_bearer = secrets.token_urlsafe(32)
    rejected_unauthenticated_requests = 0

    class ProbeHandler(BaseHTTPRequestHandler):
        server_version = "AgentFanoutCodexProbe/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            nonlocal rejected_unauthenticated_requests
            authorization = self.headers.get("Authorization", "")
            if not secrets.compare_digest(authorization, f"Bearer {probe_bearer}"):
                rejected_unauthenticated_requests += 1
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path != "/v1/responses":
                capture_error.append("codex_transport_probe_path_invalid")
                self.send_error(404)
                capture_event.set()
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_CHILD_OUTPUT_BYTES:
                capture_error.append("codex_transport_probe_request_size_invalid")
                self.send_error(400)
                capture_event.set()
                return
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if not isinstance(value, dict):
                capture_error.append("codex_transport_probe_request_invalid")
            else:
                captured.append(value)
            # Signal before replying.  A client is free to close as soon as it
            # has transmitted the request; BrokenPipe must never turn a valid
            # capture into a false timeout.
            capture_event.set()
            payload = b'{"error":{"message":"agentfanout offline transport probe complete"}}'
            try:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(
        target=server.serve_forever, name="agentfanout-codex-probe-http", daemon=True,
    )
    server_thread.start()
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="agentfanout-codex-transport-probe-") as directory:
            execution_root = Path(directory)
            codex_home = execution_root / "codex-home"
            user_home = execution_root / "user-home"
            codex_home.mkdir(mode=0o700)
            user_home.mkdir(mode=0o700)
            base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
            provider_config = (
                '{name="AgentFanout Offline Probe",base_url="' + base_url
                + '",env_key="AGENT_FANOUT_CODEX_PROBE_KEY",wire_api="responses"}'
            )
            argv = [
                *prefix, "--strict-config", "--sandbox", "read-only",
                "--ask-for-approval", "never", "--cd", str(execution_root),
                *feature_preflight["launch_config_arguments"],
                *feature_preflight["launch_feature_overrides"],
                "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--json", "--model", resolved_model,
                "--config", 'model_reasoning_effort="low"',
                "--config", 'model_provider="agentfanout_probe"',
                "--config", f"model_providers.agentfanout_probe={provider_config}",
                "Return the word probe.",
            ]
            launch_argv, descriptors, pinning = _prepare_pinned_launch_argv(
                argv, pinned_executable, pinned_arguments,
            )
            environment, _audit = minimal_child_environment(allowlist=CODEX_CHILD_ENV_ALLOWLIST)
            environment.update({
                "HOME": str(user_home), "CODEX_HOME": str(codex_home),
                "AGENT_FANOUT_CODEX_PROBE_KEY": probe_bearer,
            })
            environment.pop("CODEX_AUTH_SOCK", None)
            environment.pop("XDG_RUNTIME_DIR", None)
            try:
                process = subprocess.Popen(
                    launch_argv, cwd=execution_root, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True, shell=False, env=environment,
                    pass_fds=tuple(descriptors),
                )
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)
            if not capture_event.wait(max(1.0, min(float(timeout_seconds), 20.0))):
                raise ProviderAdapterError("codex_transport_probe_request_missing")
            if capture_error:
                raise ProviderAdapterError(capture_error[0])
            if not captured:
                raise ProviderAdapterError("codex_transport_probe_request_missing")
            request = captured[0]
            observed_request_model = request.get("model")
            if observed_request_model != resolved_model:
                reason = "codex_transport_probe_model_mismatch"
                failure = {
                    "schema": "CodexNoToolTransportAttestationV1",
                    "tools_empty": False, "web_search_present": False,
                    "tool_names": [], "loopback_only": True,
                    "provider_credentials_used": False,
                    "exact_model_id": resolved_model,
                    "observed_request_model": observed_request_model,
                    "session_mode": session_mode,
                    "rejected_unauthenticated_requests": rejected_unauthenticated_requests,
                    "executable_pinning": pinning, "block_reason": reason,
                }
                with _CODEX_TRANSPORT_PROBE_LOCK:
                    _CODEX_TRANSPORT_PROBE_CACHE[cache_key] = failure
                raise ProviderAdapterError(reason)
            tools = request.get("tools")
            tool_choice = request.get("tool_choice")
            tool_names: list[str] = []
            if isinstance(tools, list):
                for item in tools:
                    if not isinstance(item, dict):
                        tool_names.append("invalid")
                        continue
                    candidate = item.get("name") or item.get("type") or "unknown"
                    rendered = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(candidate))[:80]
                    tool_names.append(rendered or "unknown")
            if tools != [] or tool_choice not in (None, "none"):
                reason = "codex_native_no_tool_transport_unavailable"
                if tool_names:
                    reason += ":" + ",".join(sorted(set(tool_names)))
                elif tools != []:
                    reason += ":tools_payload_not_empty_array"
                else:
                    rendered_choice = re.sub(
                        r"[^A-Za-z0-9_.:-]", "_", str(tool_choice),
                    )[:80]
                    reason += ":tool_choice_" + (rendered_choice or "invalid")
                failure = {
                    "schema": "CodexNoToolTransportAttestationV1",
                    "tools_empty": False, "web_search_present": any(
                        name == "web_search" for name in tool_names
                    ),
                    "tool_names": sorted(set(tool_names)),
                    "tool_choice": tool_choice,
                    "loopback_only": True, "provider_credentials_used": False,
                    "exact_model_id": resolved_model,
                    "observed_request_model": observed_request_model,
                    "session_mode": session_mode,
                    "rejected_unauthenticated_requests": rejected_unauthenticated_requests,
                    "executable_pinning": pinning, "block_reason": reason,
                }
                with _CODEX_TRANSPORT_PROBE_LOCK:
                    _CODEX_TRANSPORT_PROBE_CACHE[cache_key] = failure
                raise ProviderAdapterError(reason)
            attestation = {
                "schema": "CodexNoToolTransportAttestationV1",
                "tools_empty": True, "web_search_present": False, "tool_names": [],
                "tool_choice": tool_choice, "loopback_only": True,
                "provider_credentials_used": False,
                "exact_model_id": resolved_model,
                "observed_request_model": observed_request_model,
                "session_mode": session_mode,
                "rejected_unauthenticated_requests": rejected_unauthenticated_requests,
                "request_sha256": hashlib.sha256(
                    json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
                ).hexdigest(),
                "executable_pinning": pinning,
            }
            with _CODEX_TRANSPORT_PROBE_LOCK:
                _CODEX_TRANSPORT_PROBE_CACHE[cache_key] = attestation
            return dict(attestation)
    finally:
        if process is not None:
            if process.poll() is None or (
                os.name == "posix" and hasattr(os, "killpg") and _process_group_exists(process.pid)
            ):
                if terminate_process_group(process, process.pid) is not True:
                    raise ProviderAdapterError("codex_transport_probe_termination_unconfirmed")
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                if terminate_process_group(process, process.pid) is not True:
                    raise ProviderAdapterError("codex_transport_probe_termination_unconfirmed")
                process.communicate(timeout=1)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def codex_no_tool_runtime_attestation(
    binary: str | Sequence[str] = "codex", timeout_seconds: float = 15, *,
    resolved_model: str,
    session_mode: str = "ephemeral",
    expected_executable_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove that an exact Codex runtime emits a zero-tool request.

    This is the public compatibility gate used by doctor/run/launch.  It is an
    offline loopback probe: no operator credential and no provider endpoint is
    contacted.  A CLI whose final Responses payload still advertises any
    native tool is incompatible with the strict worker lane and fails closed.
    """

    observed = (
        verify_executable_identity(binary, expected_executable_identity)
        if expected_executable_identity is not None else executable_identity(binary)
    )
    feature_preflight = _codex_no_tool_feature_preflight(
        binary, timeout_seconds=timeout_seconds,
        expected_executable_identity=observed,
    )
    transport = _codex_no_tool_transport_attestation(
        binary, observed, feature_preflight, resolved_model,
        session_mode=session_mode,
        timeout_seconds=min(float(timeout_seconds), 20.0),
    )
    # This diagnostic intentionally uses a synthetic custom provider so it
    # cannot disclose operator credentials or contact OpenAI. Provider
    # capability bounds can change the emitted tool set, however, so even an
    # empty synthetic request is not proof that the real ChatGPT/OpenAI path is
    # empty. No executable type, wrapper, or fixture is exempt from this rule.
    # A separately trusted real-provider attestation mechanism is not bundled
    # yet, therefore native Codex admission always fails closed after the
    # diagnostic probe.
    raise ProviderAdapterError("codex_native_real_provider_attestation_required")


def run_json_process(
    argv: Sequence[str], prompt: str, project_root: Path, timeout_seconds: float,
    heartbeat: Callable[[str], None] | None = None, heartbeat_interval_seconds: float = 15,
    cancellation_requested: Callable[[], bool] | None = None, max_output_bytes: int = MAX_CHILD_OUTPUT_BYTES,
    environment_allowlist: frozenset[str] | None = None,
    environment_overrides: dict[str, str] | None = None,
    pinned_executable_identity: dict[str, Any] | None = None,
    pinned_argument_file_identities: Sequence[dict[str, Any]] = (),
    on_process_group_exit_confirmed: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run a JSON-producing child without a shell, heartbeating and cancelling safely."""

    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ProviderAdapterError("launch_argv_invalid")
    timeout_seconds = max(1, min(float(timeout_seconds), 1800))
    heartbeat_interval_seconds = max(0.05, min(float(heartbeat_interval_seconds), 900))
    max_output_bytes = max(1024, min(int(max_output_bytes), MAX_CHILD_OUTPUT_BYTES))
    started = time.monotonic()
    environment, environment_audit = minimal_child_environment(allowlist=environment_allowlist)
    if environment_overrides is not None:
        if (
            not isinstance(environment_overrides, dict)
            or any(
                not isinstance(key, str) or not key or "=" in key or "\x00" in key
                or not isinstance(value, str) or "\x00" in value
                for key, value in environment_overrides.items()
            )
        ):
            raise ProviderAdapterError("provider_environment_override_invalid")
        environment.update(environment_overrides)
        environment_audit["forced_names"] = sorted(environment_overrides)
    else:
        environment_audit["forced_names"] = []
    launch_argv, pinned_descriptors, executable_pinning = _prepare_pinned_launch_argv(
        argv, pinned_executable_identity, pinned_argument_file_identities,
    )
    received_parent_signals: list[int] = []
    prior_sigterm: Any = None
    signal_handler_installed = threading.current_thread() is threading.main_thread()
    if signal_handler_installed:
        prior_sigterm = signal.getsignal(signal.SIGTERM)

        def _request_parent_signal_cleanup(signum: int, _frame: Any) -> None:
            received_parent_signals.append(signum)

        signal.signal(signal.SIGTERM, _request_parent_signal_cleanup)

    def restore_parent_signal_handler() -> None:
        nonlocal signal_handler_installed
        if signal_handler_installed:
            signal.signal(signal.SIGTERM, prior_sigterm)
            signal_handler_installed = False

    popen_extra: dict[str, Any] = {}
    if pinned_descriptors:
        popen_extra["pass_fds"] = tuple(pinned_descriptors)
    try:
        try:
            process = subprocess.Popen(
                launch_argv, cwd=project_root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, shell=False, env=environment, **popen_extra,
            )
        finally:
            for descriptor in pinned_descriptors:
                os.close(descriptor)
    except OSError as exc:
        restore_parent_signal_handler()
        raise ProviderAdapterError("provider_launch_unavailable") from exc
    except BaseException:
        restore_parent_signal_handler()
        raise
    # With start_new_session=True on POSIX the initial PID is also the process
    # group ID. Preserve it even if the group leader exits before descendants.
    process_group_id = process.pid
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    output_limit = threading.Event()
    stream_lock = threading.Lock()

    def write_prompt() -> None:
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            process.stdin.close()

    def drain(stream: Any, target: bytearray) -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            with stream_lock:
                remaining = max_output_bytes - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_limit.set()

    writer = threading.Thread(target=write_prompt, name="agentfanout-provider-stdin", daemon=True)
    stdout_reader = threading.Thread(target=drain, args=(process.stdout, stdout), name="agentfanout-provider-stdout", daemon=True)
    stderr_reader = threading.Thread(target=drain, args=(process.stderr, stderr), name="agentfanout-provider-stderr", daemon=True)
    try:
        writer.start()
        stdout_reader.start()
        stderr_reader.start()
    except BaseException:
        terminate_process_group(process, process_group_id)
        restore_parent_signal_handler()
        process.stdout.close()
        process.stderr.close()
        raise
    next_heartbeat = started
    state = "completed"
    termination_required = False
    termination_confirmed: bool | None = None
    callback_error: Exception | None = None
    pending_exception: BaseException | None = None
    try:
        while True:
            now = time.monotonic()
            if received_parent_signals:
                state = "interrupted"
                termination_required = True
                termination_confirmed = terminate_process_group(process, process_group_id)
                break
            if output_limit.is_set():
                state = "output_limit"
                termination_required = True
                termination_confirmed = terminate_process_group(process, process_group_id)
                break
            if cancellation_requested and cancellation_requested():
                state = "cancelled"
                termination_required = True
                termination_confirmed = terminate_process_group(process, process_group_id)
                break
            if now - started >= timeout_seconds:
                state = "timeout"
                termination_required = True
                termination_confirmed = terminate_process_group(process, process_group_id)
                break
            if process.poll() is not None:
                # A provider leader must not detach background work. If a
                # descendant outlives the leader, terminate the original process
                # group and treat the launch as failed even when the leader
                # returned zero.
                if os.name == "posix" and hasattr(os, "killpg") and _process_group_exists(process_group_id):
                    state = "failed"
                    termination_required = True
                    termination_confirmed = terminate_process_group(process, process_group_id)
                break
            if heartbeat and now >= next_heartbeat:
                try:
                    heartbeat(_now())
                except Exception as exc:
                    termination_required = True
                    termination_confirmed = terminate_process_group(process, process_group_id)
                    callback_error = exc
                    break
                next_heartbeat = now + heartbeat_interval_seconds
            time.sleep(min(0.05, heartbeat_interval_seconds / 2))
    except BaseException as exc:
        # SIGINT/KeyboardInterrupt and host-specific signal exceptions must not
        # strand a start_new_session provider outside the caller's process
        # group. Reap the entire original group before propagating the signal.
        state = "interrupted"
        termination_required = True
        termination_confirmed = terminate_process_group(process, process_group_id)
        pending_exception = exc
    restore_parent_signal_handler()
    if output_limit.is_set() and state == "completed":
        state = "output_limit"
    writer.join(timeout=1)
    stdout_reader.join(timeout=2)
    stderr_reader.join(timeout=2)
    # Reap a leader that exited while stream threads were draining, including
    # the fail-closed path where group termination could not be confirmed.
    process.poll()
    process.stdout.close()
    process.stderr.close()
    if termination_required and termination_confirmed is not True:
        raise ProviderAdapterError("provider_process_group_termination_unconfirmed")
    if callback_error is not None:
        raise ProviderAdapterError("heartbeat_callback_failed") from callback_error
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    process_group_exit_confirmed = bool(
        process.returncode is not None
        and (
            os.name != "posix"
            or not hasattr(os, "killpg")
            or not _process_group_exists(process_group_id)
        )
    )
    # Report the process-boundary fact before any caller parses or validates
    # provider output.  A cleanly exited worker can still return malformed or
    # policy-invalid data; that later error must not be misreported as an
    # unconfirmed live process merely because the function raises afterward.
    if process_group_exit_confirmed and on_process_group_exit_confirmed is not None:
        try:
            on_process_group_exit_confirmed()
        except Exception as exc:
            raise ProviderAdapterError(
                "process_group_exit_confirmation_callback_failed"
            ) from exc
    if pending_exception is not None:
        raise pending_exception
    if received_parent_signals:
        signum = received_parent_signals[-1]
        # The provider group is gone before an outer entrypoint or hosted
        # runner sees the signal. Re-dispatch the prior handler only after
        # restoring it so existing shutdown semantics remain intact.
        if callable(prior_sigterm):
            prior_sigterm(signum, None)
        raise ProviderAdapterError(
            f"provider_parent_signal:{signal.Signals(signum).name}"
        )
    return {
        "status": state if state != "completed" else ("completed" if process.returncode == 0 else "failed"),
        "returncode": process.returncode,
        "stdout": bytes(stdout).decode("utf-8", errors="replace"),
        "stderr": bytes(stderr).decode("utf-8", errors="replace"),
        "output_truncated": output_limit.is_set(), "duration_ms": duration_ms,
        "process_group_termination_confirmed": termination_confirmed,
        "termination_scope": "process_group_only",
        "detached_session_descendants_covered": False,
        "process_group_exit_confirmed": process_group_exit_confirmed,
        "environment_audit": environment_audit,
        "executable_pinning": executable_pinning,
    }


def _usage_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Accept one authoritative final usage event; never sum partial turns."""

    final_usage: dict[str, Any] | None = None
    for event in events:
        usage = event.get("usage")
        event_type = str(event.get("type", "")).lower()
        if isinstance(usage, dict) and event_type in {"turn.completed", "response.completed", "task.completed", "result.completed"}:
            final_usage = usage
    unknown = {"input_tokens": None, "cached_tokens": None, "uncached_input_tokens": None,
               "output_tokens": None, "cost_usd": None, "usage_verified": False, "cost_verified": False}
    if not final_usage:
        return unknown
    input_tokens = final_usage.get("input_tokens")
    output_tokens = final_usage.get("output_tokens")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (input_tokens, output_tokens)):
        return unknown
    cached_values = [final_usage[field] for field in ("cached_input_tokens", "cached_tokens") if field in final_usage]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > input_tokens for value in cached_values):
        return unknown
    if len(set(cached_values)) > 1:
        return unknown
    cached_tokens = cached_values[0] if cached_values else 0
    cost = final_usage.get("cost_usd")
    price_card_version = final_usage.get("price_card_version")
    cost_source = final_usage.get("cost_source")
    free_price_approved = final_usage.get("approved_free_price") is True
    cost_is_number = isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
    cost_verified = bool(
        cost_is_number
        and isinstance(price_card_version, str) and price_card_version
        and isinstance(cost_source, str) and cost_source
        and (cost > 0 or free_price_approved)
    )
    result = {
        "input_tokens": input_tokens, "cached_tokens": cached_tokens,
        "uncached_input_tokens": input_tokens - cached_tokens, "output_tokens": output_tokens,
        "cost_usd": round(float(cost), 8) if cost_verified else None,
        "usage_verified": True, "cost_verified": cost_verified,
    }
    if cost_verified:
        result["price_provenance"] = f"{price_card_version}:{cost_source}"
    return result


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _aware_time(value: Any, error: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ProviderAdapterError(error)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderAdapterError(error) from exc
    if parsed.tzinfo is None:
        raise ProviderAdapterError(error)
    return parsed.astimezone(timezone.utc)


def validate_local_host_evidence(
    capability_attestation: dict[str, Any],
    bridge_observation: dict[str, Any],
    *,
    selector: str,
    exact_model_id: str,
    endpoint: str,
    task_class: str,
    reasoning_effort: str,
    required_mode: str,
    allow_loopback_http: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate exact, fresh host evidence for the credential-free Local lane."""

    from .local_endpoint_worker import normalize_local_endpoint
    from .provider_registry import normalize_reasoning, risk_for

    if (
        not isinstance(selector, str) or not MODEL_SLUG.fullmatch(selector)
        or not isinstance(exact_model_id, str) or not MODEL_SLUG.fullmatch(exact_model_id)
    ):
        raise ProviderAdapterError("local_model_binding_invalid")
    if not isinstance(required_mode, str) or not MODEL_SLUG.fullmatch(required_mode):
        raise ProviderAdapterError("local_required_mode_invalid")
    if reasoning_effort not in EFFORTS:
        raise ProviderAdapterError("local_reasoning_effort_invalid")
    canonical_endpoint, endpoint_policy = normalize_local_endpoint(endpoint, allow_loopback_http)
    attestation_fields = {
        "schema", "attestation_id", "source", "issuer", "selector_alias", "exact_model_id",
        "observed_at", "expires_at", "active_mode", "context_window",
        "evaluated_task_classes", "supported_reasoning_efforts", "max_risk", "tools",
        "supports_file_editing",
    }
    if not isinstance(capability_attestation, dict) or set(capability_attestation) != attestation_fields:
        raise ProviderAdapterError("local_capability_attestation_shape_invalid")
    if (
        capability_attestation.get("schema") != "LocalCapabilityAttestationV1"
        or capability_attestation.get("source") != "host"
        or capability_attestation.get("issuer") != "host-runtime"
        or not isinstance(capability_attestation.get("attestation_id"), str)
        or not re.fullmatch(r"localcap_[a-f0-9]{32}", capability_attestation["attestation_id"])
    ):
        raise ProviderAdapterError("local_capability_attestation_identity_invalid")
    if (
        capability_attestation.get("selector_alias") != selector
        or capability_attestation.get("exact_model_id") != exact_model_id
        or capability_attestation.get("active_mode") != required_mode
        or not required_mode
    ):
        raise ProviderAdapterError("local_capability_attestation_binding_mismatch")
    context_window = capability_attestation.get("context_window")
    if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window < 1:
        raise ProviderAdapterError("local_capability_context_window_invalid")
    task_classes = capability_attestation.get("evaluated_task_classes")
    efforts = capability_attestation.get("supported_reasoning_efforts")
    normalized_effort = normalize_reasoning(reasoning_effort)
    if (
        not isinstance(task_classes, list) or not 1 <= len(task_classes) <= 64
        or not all(isinstance(item, str) and item for item in task_classes)
        or len(set(task_classes)) != len(task_classes)
        or (task_class not in task_classes and "*" not in task_classes)
    ):
        raise ProviderAdapterError("local_task_class_not_attested")
    if (
        not isinstance(efforts, list) or not 1 <= len(efforts) <= len(EFFORTS)
        or len(set(efforts)) != len(efforts) or normalized_effort not in efforts
        or any(item not in EFFORTS for item in efforts)
    ):
        raise ProviderAdapterError("local_reasoning_effort_not_attested")
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    required_risk = risk_for(task_class, normalized_effort)
    maximum_risk = capability_attestation.get("max_risk")
    if maximum_risk not in risk_order or risk_order[maximum_risk] < risk_order[required_risk]:
        raise ProviderAdapterError("local_risk_not_attested")
    # Any editing or tool claim belongs to the separately signed sandbox lane.
    tools = capability_attestation.get("tools")
    if tools != [] or capability_attestation.get("supports_file_editing") is not False:
        raise ProviderAdapterError("local_no_tool_capability_required")

    bridge_fields = {
        "model_alias", "model_id", "endpoint", "context_window", "profiles",
        "checked_at", "expires_at", "source",
    }
    if not isinstance(bridge_observation, dict) or set(bridge_observation) != bridge_fields:
        raise ProviderAdapterError("local_bridge_observation_shape_invalid")
    try:
        bridge_endpoint, bridge_policy = normalize_local_endpoint(
            bridge_observation.get("endpoint"), allow_loopback_http,
        )
    except ValueError as exc:
        raise ProviderAdapterError("local_bridge_endpoint_invalid") from exc
    if (
        bridge_observation.get("model_alias") != selector
        or bridge_observation.get("model_id") != exact_model_id
        or bridge_endpoint != canonical_endpoint
        or bridge_policy != endpoint_policy
        or bridge_observation.get("context_window") != context_window
        or not isinstance(bridge_observation.get("profiles"), list)
        or not 1 <= len(bridge_observation["profiles"]) <= 64
        or not all(isinstance(item, str) and item for item in bridge_observation["profiles"])
        or len(set(bridge_observation["profiles"])) != len(bridge_observation["profiles"])
        or not isinstance(bridge_observation.get("source"), str)
        or not bridge_observation["source"].strip()
        or len(bridge_observation["source"]) > 200
    ):
        raise ProviderAdapterError("local_bridge_observation_binding_mismatch")

    current_source = now or datetime.now(timezone.utc)
    if current_source.tzinfo is None:
        raise ProviderAdapterError("local_evidence_clock_invalid")
    current = current_source.astimezone(timezone.utc)
    max_future_skew = timedelta(minutes=1)
    max_age = timedelta(minutes=15)
    attested_at = _aware_time(capability_attestation["observed_at"], "local_capability_time_invalid")
    attestation_expires = _aware_time(capability_attestation["expires_at"], "local_capability_time_invalid")
    bridge_checked = _aware_time(bridge_observation["checked_at"], "local_bridge_time_invalid")
    bridge_expires = _aware_time(bridge_observation["expires_at"], "local_bridge_time_invalid")
    for observed, expires, error in (
        (attested_at, attestation_expires, "local_capability_attestation_stale"),
        (bridge_checked, bridge_expires, "local_bridge_observation_stale"),
    ):
        if (
            observed > current + max_future_skew
            or current - observed > max_age
            or expires <= current
            or expires <= observed
            or expires - observed > max_age + max_future_skew
        ):
            raise ProviderAdapterError(error)
    return {
        "endpoint": canonical_endpoint,
        "endpoint_policy": endpoint_policy,
        "selector": selector,
        "exact_model_id": exact_model_id,
        "reasoning_effort": normalized_effort,
        "required_mode": required_mode,
        "context_window": context_window,
        "capability_attestation_sha256": _canonical_sha256(capability_attestation),
        "bridge_observation_sha256": _canonical_sha256(bridge_observation),
        "observed_at": max(attested_at, bridge_checked).isoformat(),
        "expires_at": min(attestation_expires, bridge_expires).isoformat(),
    }


def local_host_evidence_from_environment(
    *, selector: str, task_class: str, reasoning_effort: str,
) -> dict[str, Any]:
    endpoint = os.getenv("AGENT_FANOUT_LOCAL_ENDPOINT", "").strip()
    exact_model_id = os.getenv("AGENT_FANOUT_LOCAL_MODEL_ID", "").strip()
    configured_selector = os.getenv("AGENT_FANOUT_LOCAL_MODEL_ALIAS", "").strip()
    required_mode = os.getenv("AGENT_FANOUT_LOCAL_REQUIRED_MODE", "").strip()
    allow_loopback_http = os.getenv("AGENT_FANOUT_LOCAL_ALLOW_LOOPBACK_HTTP", "").strip().lower() in {
        "1", "true", "yes",
    }
    if any(os.getenv(name, "").strip() for name in (
        "AGENT_FANOUT_LOCAL_API_KEY", "AGENT_FANOUT_LOCAL_TOKEN",
        "AGENT_FANOUT_LOCAL_AUTHORIZATION", "AGENT_FANOUT_LOCAL_PASSWORD",
    )):
        raise ProviderAdapterError("local_no_tool_credentials_unsupported")
    if configured_selector != selector:
        raise ProviderAdapterError("local_configured_selector_mismatch")
    try:
        attestation = json.loads(os.getenv("AGENT_FANOUT_LOCAL_ATTESTATION_JSON", ""))
        bridge = json.loads(os.getenv("AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON", ""))
    except json.JSONDecodeError as exc:
        raise ProviderAdapterError("local_host_evidence_json_invalid") from exc
    validated = validate_local_host_evidence(
        attestation, bridge, selector=selector, exact_model_id=exact_model_id,
        endpoint=endpoint, task_class=task_class, reasoning_effort=reasoning_effort,
        required_mode=required_mode, allow_loopback_http=allow_loopback_http,
    )
    return {
        **validated,
        "allow_loopback_http": allow_loopback_http,
        "capability_attestation": attestation,
        "bridge_observation": bridge,
    }


def _secure_grok_home(value: str | Path | None) -> Path:
    """Return an explicitly isolated Grok state root.

    Grok discovers user MCP servers, plugins, skills, rules, and memory from its
    normal home.  A no-tool adapter must therefore never reuse ``~/.grok``.
    Operators provision a separate root with a ``GrokApiCredentialV1`` file.
    Model discovery receives its bound API key through ``XAI_API_KEY`` in a
    fresh empty CLI home; inference uses a launch-scoped credential copy.
    Official CLI session stores and subscription tokens are never migrated.
    """

    selected = str(value or os.getenv("AGENT_FANOUT_GROK_HOME", "")).strip()
    if not selected:
        raise ProviderAdapterError("grok_isolated_home_required")
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        raise ProviderAdapterError("grok_isolated_home_must_be_absolute")
    try:
        candidate_info = candidate.lstat()
        root = candidate.resolve(strict=True)
        info = root.stat()
    except OSError as exc:
        raise ProviderAdapterError("grok_isolated_home_unavailable") from exc
    if not root.is_dir() or stat.S_ISLNK(candidate_info.st_mode):
        raise ProviderAdapterError("grok_isolated_home_invalid")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ProviderAdapterError("grok_isolated_home_owner_invalid")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ProviderAdapterError("grok_isolated_home_permissions_too_broad")
    try:
        default_home = (Path.home() / ".grok").resolve(strict=False)
    except OSError:
        default_home = Path.home() / ".grok"
    if root == default_home:
        raise ProviderAdapterError("grok_default_home_forbidden")
    return root


def _read_grok_auth_file(
    source: Path, expected_identity: dict[str, Any] | None = None,
) -> tuple[bytearray, dict[str, Any]]:
    """Read and bind the dedicated Grok auth file without following links."""

    auth_source = source / "auth.json"
    try:
        source_info = auth_source.lstat()
    except FileNotFoundError:
        raise ProviderAdapterError("grok_dedicated_auth_file_required")
    except OSError as exc:
        raise ProviderAdapterError("grok_auth_file_unavailable") from exc
    if (
        stat.S_ISLNK(source_info.st_mode)
        or not stat.S_ISREG(source_info.st_mode)
        or source_info.st_nlink != 1
        or (hasattr(os, "getuid") and source_info.st_uid != os.getuid())
        or stat.S_IMODE(source_info.st_mode) & 0o077
        or source_info.st_size <= 0
        or source_info.st_size > 512_000
    ):
        raise ProviderAdapterError("grok_auth_file_security_invalid")
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(auth_source, read_flags)
    except OSError as exc:
        raise ProviderAdapterError("grok_auth_file_unavailable") from exc
    try:
        opened_info = os.fstat(source_fd)
        if (
            opened_info.st_dev != source_info.st_dev
            or opened_info.st_ino != source_info.st_ino
            or opened_info.st_size != source_info.st_size
            or opened_info.st_nlink != 1
            or opened_info.st_uid != source_info.st_uid
            or opened_info.st_mode != source_info.st_mode
        ):
            raise ProviderAdapterError("grok_auth_file_identity_changed")
        payload = bytearray()
        while len(payload) <= 512_000:
            chunk = os.read(source_fd, min(64 * 1024, 512_001 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != source_info.st_size or not payload:
            raise ProviderAdapterError("grok_auth_file_read_invalid")
    finally:
        os.close(source_fd)
    identity = {
        "schema": "GrokAuthFileIdentityV1",
        "auth_file_path_sha256": hashlib.sha256(str(auth_source).encode()).hexdigest(),
        "auth_file_sha256": hashlib.sha256(payload).hexdigest(),
        "auth_file_bytes": source_info.st_size,
        "auth_file_device": source_info.st_dev,
        "auth_file_inode": source_info.st_ino,
        "auth_file_mode": stat.S_IMODE(source_info.st_mode),
        "auth_file_uid": source_info.st_uid,
    }
    if expected_identity is not None and identity != expected_identity:
        for index in range(len(payload)):
            payload[index] = 0
        raise ProviderAdapterError("grok_auth_file_identity_changed")
    return payload, identity


def grok_auth_file_identity(grok_home: str | Path) -> dict[str, Any]:
    """Freeze the secure auth-only identity used by a guarded Grok run."""

    source = _secure_grok_home(grok_home)
    payload, identity = _read_grok_auth_file(source)
    for index in range(len(payload)):
        payload[index] = 0
    return identity


def _prepare_ephemeral_grok_home(
    source: Path, destination: Path, expected_auth_file_identity: dict[str, Any] | None,
) -> dict[str, Any]:
    """Create an auth-only launch home without importing user configuration."""

    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    payload, auth_identity = _read_grok_auth_file(source, expected_auth_file_identity)
    destination_path = destination / "auth.json"
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        destination_fd = os.open(destination_path, write_flags, 0o600)
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                written += os.write(destination_fd, view[written:])
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    except OSError as exc:
        raise ProviderAdapterError("grok_ephemeral_auth_write_failed") from exc
    finally:
        for index in range(len(payload)):
            payload[index] = 0
    identity_sha256 = hashlib.sha256(
        json.dumps(auth_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    return {
        "auth_file_copied": True, "configuration_copied": False,
        "auth_file_identity_sha256": identity_sha256,
        "auth_identity_prebound": expected_auth_file_identity is not None,
        "ambient_auth_socket_allowed": False,
    }


class _AcpProtocolAbort(RuntimeError):
    """Internal bounded ACP state-machine abort."""


def _grok_model_acknowledgement(
    initialize_result: dict[str, Any], model: str, reasoning_effort: str,
) -> dict[str, Any]:
    """Verify Grok acknowledged the exact CLI-selected model and effort."""

    metadata = initialize_result.get("_meta")
    model_state = metadata.get("modelState") if isinstance(metadata, dict) else None
    if not isinstance(model_state, dict) or model_state.get("currentModelId") != model:
        raise ProviderAdapterError("grok_acp_model_acknowledgement_mismatch")
    available = model_state.get("availableModels")
    if not isinstance(available, list):
        raise ProviderAdapterError("grok_acp_model_catalog_missing")
    selected = next(
        (item for item in available if isinstance(item, dict) and item.get("modelId") == model),
        None,
    )
    if selected is None:
        raise ProviderAdapterError("grok_acp_selected_model_not_available")
    model_meta = selected.get("_meta") if isinstance(selected.get("_meta"), dict) else {}
    supports_effort = model_meta.get("supportsReasoningEffort") is True
    if supports_effort:
        efforts = model_meta.get("reasoningEfforts")
        acknowledged = {
            str(item.get("value", item.get("id")))
            for item in efforts if isinstance(item, dict)
        } if isinstance(efforts, list) else set()
        if reasoning_effort not in acknowledged or model_meta.get("reasoningEffort") != reasoning_effort:
            raise ProviderAdapterError("grok_acp_reasoning_effort_acknowledgement_mismatch")
        effort_basis = "acp_model_state_exact"
    else:
        # Models without an effort control have one fixed inference path.  They
        # may enter only the low-effort lane; medium+ would be an overclaim.
        if reasoning_effort != "low":
            raise ProviderAdapterError("grok_model_reasoning_effort_unsupported")
        effort_basis = "fixed_model_path_cli_low_only"
    active_mcp = metadata.get("mcpServers")
    if active_mcp not in (None, []):
        raise ProviderAdapterError("grok_acp_active_mcp_servers_forbidden")
    return {
        "current_model_id": model,
        "reasoning_effort": reasoning_effort,
        "reasoning_effort_basis": effort_basis,
        "available_model_count": len(available),
    }


def _grok_usage_from_prompt_response(
    response: dict[str, Any], assistant_text: str,
) -> dict[str, Any]:
    """Use final ACP totalTokens plus a conservative output upper bound.

    A new one-turn ACP session makes ``totalTokens`` a conservative total for
    this launch.  ACP does not expose a trustworthy split, so the host records
    all of ``totalTokens`` in the input bucket and the UTF-8 byte length of the
    assembled assistant text as a conservative output-token upper bound. This
    intentionally double-counts some usage but preserves both prompt and output
    acceptance gates. Cost remains unverified and is reconciled against the
    host reservation.
    """

    metadata = response.get("_meta")
    total = metadata.get("totalTokens") if isinstance(metadata, dict) else None
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise ProviderAdapterError("grok_acp_total_tokens_missing")
    if not isinstance(assistant_text, str) or not assistant_text:
        raise ProviderAdapterError("grok_acp_assistant_text_missing")
    output_upper_bound = len(assistant_text.encode("utf-8"))
    return {
        "input_tokens": total,
        "cached_tokens": 0,
        "uncached_input_tokens": total,
        "output_tokens": output_upper_bound,
        "total_tokens": total,
        "token_split_verified": False,
        "output_token_basis": "utf8_byte_upper_bound",
        "usage_verified": True,
        "usage_provenance": "acp_prompt_response._meta.totalTokens:new_single_turn_session",
        "cost_usd": None,
        "cost_verified": False,
    }


def _run_rejected_grok_acp_diagnostic(
    argv: Sequence[str], prompt: str, execution_root: Path, grok_home: Path,
    model: str, reasoning_effort: str, timeout_seconds: float,
    heartbeat: Callable[[str], None] | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
    max_output_bytes: int = MAX_CHILD_OUTPUT_BYTES,
    pinned_executable_identity: dict[str, Any] | None = None,
    pinned_argument_file_identities: Sequence[dict[str, Any]] = (),
    on_process_group_exit_confirmed: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Regression-only monitor for the rejected Grok CLI agent transport.

    Production ``launch_grok`` never calls this function. It remains private so
    the test suite can prove that historical ACP tool/MCP activity is denied
    and process groups are reaped without accidentally presenting that
    transport as an admissible adapter.
    """

    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ProviderAdapterError("launch_argv_invalid")
    timeout_seconds = max(1, min(float(timeout_seconds), 1800))
    max_output_bytes = max(4096, min(int(max_output_bytes), MAX_CHILD_OUTPUT_BYTES))
    started = time.monotonic()
    deadline = started + timeout_seconds
    environment, environment_audit = minimal_child_environment(allowlist=GROK_CHILD_ENV_ALLOWLIST)
    isolated_user_home = execution_root / "user-home"
    isolated_user_home.mkdir(mode=0o700)
    environment.update({
        "HOME": str(isolated_user_home),
        "GROK_HOME": str(grok_home),
        "GROK_SUBAGENTS": "0",
        "GROK_WEB_FETCH": "0",
        "GROK_CURSOR_SKILLS_ENABLED": "false",
        "GROK_CLAUDE_SKILLS_ENABLED": "false",
        "GROK_CURSOR_MCPS_ENABLED": "false",
        "GROK_CLAUDE_MCPS_ENABLED": "false",
        "GROK_CURSOR_HOOKS_ENABLED": "false",
        "GROK_CLAUDE_HOOKS_ENABLED": "false",
        "GROK_CURSOR_RULES_ENABLED": "false",
        "GROK_CLAUDE_RULES_ENABLED": "false",
    })
    environment_audit["forced_hardening_names"] = sorted(
        key for key in environment if key.startswith("GROK_") and key != "GROK_HOME"
    )
    environment_audit["isolated_home"] = True
    environment_audit["ambient_auth_channels_denied"] = ["GROK_AUTH_SOCK", "XDG_RUNTIME_DIR"]
    if pinned_executable_identity is None:
        raise ProviderAdapterError("grok_pinned_executable_identity_required")
    launch_argv, pinned_descriptors, executable_pinning = _prepare_pinned_launch_argv(
        argv, pinned_executable_identity, pinned_argument_file_identities,
    )
    popen_extra: dict[str, Any] = {"pass_fds": tuple(pinned_descriptors)}
    try:
        try:
            process = subprocess.Popen(
                launch_argv, cwd=execution_root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True, shell=False, env=environment,
                **popen_extra,
            )
        finally:
            for descriptor in pinned_descriptors:
                os.close(descriptor)
    except OSError as exc:
        raise ProviderAdapterError("provider_launch_unavailable") from exc
    process_group_id = process.pid
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    chunks: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
    output_bytes = 0
    stdout_buffer = bytearray()
    stderr = bytearray()
    events: list[dict[str, Any]] = []
    assistant_chunks: list[str] = []
    next_heartbeat = started
    session_id: str | None = None
    cancel_sent = False

    def drain(name: str, stream: Any) -> None:
        while True:
            try:
                # BufferedReader.read(size) may wait for the full size on a
                # live pipe. ACP is interactive, so consume whatever the OS
                # currently made available instead.
                chunk = os.read(stream.fileno(), 8192)
            except OSError:
                chunk = b""
            if not chunk:
                chunks.put((name, None))
                return
            chunks.put((name, chunk))

    stdout_thread = threading.Thread(
        target=drain, args=("stdout", process.stdout),
        name="agentfanout-grok-acp-stdout", daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain, args=("stderr", process.stderr),
        name="agentfanout-grok-acp-stderr", daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    def send(value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        if len(encoded) > ACP_MAX_MESSAGE_BYTES:
            raise _AcpProtocolAbort("grok_acp_client_message_too_large")
        try:
            process.stdin.write(encoded)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise _AcpProtocolAbort("grok_acp_stdin_closed") from exc

    def protocol_violation(reason: str, message: dict[str, Any] | None = None) -> None:
        if isinstance(message, dict) and "id" in message:
            try:
                send({
                    "jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": -32601, "message": "AgentFanout client exposes no agent-callable methods"},
                })
            except _AcpProtocolAbort:
                pass
        raise _AcpProtocolAbort(reason)

    def observe(message: dict[str, Any]) -> None:
        nonlocal session_id
        method = message.get("method")
        if isinstance(method, str) and "id" in message:
            protocol_violation(f"grok_acp_agent_request_forbidden:{method}", message)
        if method == "_x.ai/mcp/servers_updated":
            params = message.get("params")
            servers = params.get("mcpServers") if isinstance(params, dict) else None
            if servers not in (None, []):
                protocol_violation("grok_acp_mcp_servers_forbidden")
            return
        if method == "session/update":
            params = message.get("params")
            if not isinstance(params, dict) or params.get("sessionId") != session_id:
                protocol_violation("grok_acp_session_update_binding_invalid")
            update = params.get("update")
            update_type = update.get("sessionUpdate") if isinstance(update, dict) else None
            if update_type in {"tool_call", "tool_call_update"}:
                protocol_violation("grok_acp_tool_call_forbidden")
            if update_type in {"agent_message_chunk", "usage_update"}:
                events.append({"type": "session.update", "update": update})
            if update_type == "agent_message_chunk":
                content = update.get("content") if isinstance(update, dict) else None
                text = content.get("text") if isinstance(content, dict) else None
                if not isinstance(text, str):
                    protocol_violation("grok_acp_agent_message_chunk_invalid")
                assistant_chunks.append(text)
            return
        # Grok emits announcements and implementation-private reload notices.
        # They are never passed to the worker result parser and confer no
        # capability. Any request, tool, or MCP activation was handled above.

    def parse_stdout_line() -> dict[str, Any] | None:
        newline = stdout_buffer.find(b"\n")
        if newline < 0:
            if len(stdout_buffer) > ACP_MAX_MESSAGE_BYTES:
                raise _AcpProtocolAbort("grok_acp_message_too_large")
            return None
        raw = bytes(stdout_buffer[:newline])
        del stdout_buffer[: newline + 1]
        if len(raw) > ACP_MAX_MESSAGE_BYTES:
            raise _AcpProtocolAbort("grok_acp_message_too_large")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _AcpProtocolAbort("grok_acp_json_invalid") from exc
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise _AcpProtocolAbort("grok_acp_message_invalid")
        observe(value)
        return value

    def poll_message(wait_seconds: float = 0.05) -> dict[str, Any] | None:
        nonlocal output_bytes, next_heartbeat, cancel_sent
        buffered = parse_stdout_line()
        if buffered is not None:
            return buffered
        now = time.monotonic()
        if cancellation_requested and cancellation_requested():
            if session_id is not None and not cancel_sent:
                try:
                    send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
                except _AcpProtocolAbort:
                    pass
                cancel_sent = True
            raise _AcpProtocolAbort("grok_acp_cancelled")
        if now >= deadline:
            raise _AcpProtocolAbort("grok_acp_timeout")
        if heartbeat and now >= next_heartbeat:
            try:
                heartbeat(_now())
            except Exception as exc:
                raise _AcpProtocolAbort("heartbeat_callback_failed") from exc
            next_heartbeat = now + 15
        if process.poll() is not None and chunks.empty():
            raise _AcpProtocolAbort("grok_acp_process_exited_early")
        try:
            stream_name, chunk = chunks.get(timeout=min(wait_seconds, max(0.01, deadline - now)))
        except queue.Empty:
            return None
        if chunk is None:
            return None
        output_bytes += len(chunk)
        if output_bytes > max_output_bytes:
            raise _AcpProtocolAbort("grok_acp_output_limit")
        if stream_name == "stderr":
            stderr.extend(chunk)
            return None
        stdout_buffer.extend(chunk)
        return parse_stdout_line()

    def wait_response(request_id: int) -> dict[str, Any]:
        while True:
            message = poll_message()
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                raise _AcpProtocolAbort(f"grok_acp_request_failed:{request_id}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise _AcpProtocolAbort("grok_acp_response_invalid")
            return result

    abort_reason: str | None = None
    prompt_response: dict[str, Any] | None = None
    model_ack: dict[str, Any] | None = None
    try:
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "agentfanout", "title": "AgentFanout no-tool host", "version": "1"},
            },
        })
        initialize_result = wait_response(1)
        if initialize_result.get("protocolVersion") != ACP_PROTOCOL_VERSION:
            raise _AcpProtocolAbort("grok_acp_protocol_version_mismatch")
        model_ack = _grok_model_acknowledgement(initialize_result, model, reasoning_effort)
        quiet_deadline = min(deadline, time.monotonic() + ACP_PREFLIGHT_QUIET_SECONDS)
        while time.monotonic() < quiet_deadline:
            poll_message(min(0.03, quiet_deadline - time.monotonic()))
        send({
            "jsonrpc": "2.0", "id": 2, "method": "session/new",
            "params": {"cwd": str(execution_root), "additionalDirectories": [], "mcpServers": []},
        })
        session_result = wait_response(2)
        candidate_session = session_result.get("sessionId")
        if not isinstance(candidate_session, str) or not candidate_session or len(candidate_session) > 200:
            raise _AcpProtocolAbort("grok_acp_session_id_invalid")
        session_id = candidate_session
        session_meta = session_result.get("_meta")
        if isinstance(session_meta, dict):
            current = session_meta.get("currentModelId")
            if current not in (None, model):
                raise _AcpProtocolAbort("grok_acp_session_model_mismatch")
        send({
            "jsonrpc": "2.0", "id": 3, "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": sanitized_prompt(prompt)}]},
        })
        prompt_response = wait_response(3)
        if prompt_response.get("stopReason") != "end_turn":
            raise _AcpProtocolAbort("grok_acp_turn_not_complete")
    except ProviderAdapterError as exc:
        abort_reason = str(exc)
    except _AcpProtocolAbort as exc:
        abort_reason = str(exc)
    finally:
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        # Grok may flush its session journal after the final ACP response.
        # Give that bounded flush time to exit cleanly before terminating the
        # entire group; a completed turn is accepted only on clean exit.
        graceful = _wait_for_process_group_exit(process, process_group_id, 5.0)
        termination_confirmed = graceful or terminate_process_group(process, process_group_id)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        process.poll()
        process.stdout.close()
        process.stderr.close()
    if termination_confirmed is not True:
        raise ProviderAdapterError("provider_process_group_termination_unconfirmed")
    # Preserve the verified exit fact even when ACP or usage validation below
    # rejects the response.  The hosted runner can then report the real
    # provider/protocol failure without authorizing replay or parent fallback.
    if on_process_group_exit_confirmed is not None:
        try:
            on_process_group_exit_confirmed()
        except Exception as exc:
            raise ProviderAdapterError(
                "process_group_exit_confirmation_callback_failed"
            ) from exc
    if abort_reason is not None:
        if abort_reason == "grok_acp_cancelled":
            status = "cancelled"
        elif abort_reason == "grok_acp_timeout":
            status = "timeout"
        elif abort_reason == "grok_acp_output_limit":
            status = "output_limit"
        else:
            raise ProviderAdapterError(abort_reason)
        return {
            "status": status, "returncode": process.returncode,
            "events": events, "stderr": bytes(stderr).decode("utf-8", errors="replace"),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "process_group_exit_confirmed": True,
            "process_group_termination_confirmed": not graceful,
            "termination_scope": "process_group_only",
            "environment_audit": environment_audit,
            "executable_pinning": executable_pinning,
        }
    assert prompt_response is not None and model_ack is not None
    if not graceful or process.returncode != 0:
        raise ProviderAdapterError("grok_acp_graceful_shutdown_unconfirmed")
    assistant_text = "".join(assistant_chunks)
    if not assistant_text:
        raise ProviderAdapterError("grok_acp_assistant_text_missing")
    events.append({"type": "assistant.completed", "text": assistant_text})
    events.append({"type": "session.prompt.completed", "result": prompt_response})
    return {
        "status": "completed", "returncode": 0,
        "events": events,
        "usage": _grok_usage_from_prompt_response(prompt_response, assistant_text),
        "model_acknowledgement": model_ack,
        "session_id": session_id,
        "stderr": bytes(stderr).decode("utf-8", errors="replace"),
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "process_group_exit_confirmed": True,
        "process_group_termination_confirmed": not graceful,
        "termination_scope": "process_group_only",
        "environment_audit": environment_audit,
        "executable_pinning": executable_pinning,
        "acp_policy": {
            "protocol_version": ACP_PROTOCOL_VERSION,
            "filesystem": False, "terminal": False, "mcp_servers": [],
            "builtin_tools": False, "project_root_mounted": False,
            "memory": False, "subagents": False, "web": False,
        },
    }


def local_helper_binary() -> list[str]:
    helper = Path(__file__).with_name("local_endpoint_worker.py").resolve(strict=True)
    # `-I` strips PYTHON* influence and user site packages; `-S` prevents site
    # initialization. The helper uses only the standard library and package
    # relative imports are deliberately absent.
    return [sys.executable, "-I", "-S", str(helper)]


def grok_http_helper_binary() -> list[str]:
    """Return the isolated standard-library Grok HTTPS helper command."""

    helper = Path(__file__).with_name("grok_http_worker.py").resolve(strict=True)
    return [sys.executable, "-I", "-S", str(helper)]


def grok_http_helper_identity() -> dict[str, Any]:
    """Freeze the interpreter and helper source used for strict Grok inference."""

    return executable_identity(grok_http_helper_binary())


def grok_reasoning_effort_mode(model: str) -> str:
    """Select effort semantics from the exact model, never from a route alias."""

    if not isinstance(model, str) or not MODEL_SLUG.fullmatch(model):
        raise ProviderAdapterError("grok_resolved_model_invalid")
    if not GROK_CHAT_COMPLETIONS_REASONING_MODEL.fullmatch(model):
        raise ProviderAdapterError("grok_model_packet_contract_unsupported")
    return "api_parameter"


def grok_supported_reasoning_efforts(model: str) -> frozenset[str]:
    """Return the conservative exact transport effort set for admitted models."""

    grok_reasoning_effort_mode(model)
    return frozenset({"low", "medium", "high"})


def grok_direct_transport_preflight(
    grok_home: str | Path,
    expected_auth_file_identity: dict[str, Any],
    expected_helper_identity: dict[str, Any],
    *,
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    """Validate the auth-only direct transport locally without inference."""

    isolated_home = _secure_grok_home(grok_home)
    if (
        not isinstance(expected_auth_file_identity, dict)
        or expected_auth_file_identity.get("schema") != "GrokAuthFileIdentityV1"
    ):
        raise ProviderAdapterError("grok_auth_file_identity_required")
    helper_argv = grok_http_helper_binary()
    if not isinstance(expected_helper_identity, dict):
        raise ProviderAdapterError("grok_http_helper_identity_required")
    helper_identity = verify_executable_identity(helper_argv, expected_helper_identity)
    with tempfile.TemporaryDirectory(prefix="agentfanout-grok-https-preflight-") as directory:
        execution_root = Path(directory)
        provider_home = execution_root / "provider-home"
        _prepare_ephemeral_grok_home(
            isolated_home, provider_home, expected_auth_file_identity,
        )
        request = {
            "schema": "GrokDirectAuthPreflightV1",
            "auth_file": str(provider_home / "auth.json"),
            "auth_file_sha256": expected_auth_file_identity["auth_file_sha256"],
        }
        launched = run_json_process(
            helper_argv,
            json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            execution_root,
            max(1, min(float(timeout_seconds), 30)),
            environment_allowlist=GROK_CHILD_ENV_ALLOWLIST,
            pinned_executable_identity=_primary_executable_observation(helper_identity),
            pinned_argument_file_identities=helper_identity.get("argument_file_identities", []),
        )
    stdout = launched.pop("stdout")
    launched.pop("stderr")
    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        response = None
    if launched.get("status") != "completed" or launched.get("returncode") != 0:
        if (
            isinstance(response, dict)
            and response.get("schema") == "GrokDirectErrorV1"
            and isinstance(response.get("error"), str)
            and re.fullmatch(r"grok_http_[a-z0-9_]{1,120}", response["error"])
        ):
            raise ProviderAdapterError(response["error"])
        raise ProviderAdapterError("grok_http_preflight_process_not_clean")
    if (
        not isinstance(response, dict)
        or response.get("schema") != "GrokDirectAuthPreflightResponseV1"
        or response.get("auth_entry_unambiguous") is not True
        or response.get("credential_type") != "xai_api_key"
        or response.get("network_verified") is not False
        or response.get("tools_field_sent") is not False
        or response.get("redirects_allowed") is not False
        or response.get("ambient_proxy_allowed") is not False
        or response.get("endpoint_sha256") != GROK_DIRECT_ENDPOINT_SHA256
    ):
        raise ProviderAdapterError("grok_http_preflight_response_invalid")
    return {
        **response,
        "helper_identity_sha256": hashlib.sha256(
            json.dumps(
                helper_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            ).encode()
        ).hexdigest(),
    }


def launch_local_endpoint(
    *, project_root: str | Path, endpoint: str, allow_loopback_http: bool,
    selector: str, resolved_model: str, reasoning_effort: str, prompt: str,
    timeout_seconds: float = 300, heartbeat: Callable[[str], None] | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
    binary: str | Sequence[str] | None = None,
    expected_executable_identity: dict[str, Any] | None = None,
    on_process_group_exit_confirmed: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Launch the credential-free Local HTTP helper under process supervision."""

    from .local_endpoint_worker import normalize_local_endpoint

    root = exact_project_root(project_root)
    if not MODEL_SLUG.fullmatch(selector) or not MODEL_SLUG.fullmatch(resolved_model):
        raise ProviderAdapterError("local_model_binding_invalid")
    if reasoning_effort not in EFFORTS:
        raise ProviderAdapterError("local_reasoning_effort_invalid")
    canonical_endpoint, endpoint_policy = normalize_local_endpoint(endpoint, allow_loopback_http)
    argv = binary or local_helper_binary()
    observed_identity = (
        verify_executable_identity(argv, expected_executable_identity)
        if expected_executable_identity is not None else executable_identity(argv)
    )
    request = {
        "schema": "LocalEndpointRequestV1",
        "endpoint": canonical_endpoint,
        "allow_loopback_http": allow_loopback_http,
        "model": resolved_model,
        "reasoning_effort": reasoning_effort,
        "prompt": sanitized_prompt(prompt),
        "max_output_tokens": 8192,
        "timeout_seconds": max(1, min(float(timeout_seconds) / 2, 120)),
    }
    # The verified project root is provenance only.  The helper runs in an
    # empty host directory and the child environment contains no raw secrets.
    with tempfile.TemporaryDirectory(prefix="agentfanout-local-worker-") as execution_directory:
        launched = run_json_process(
            _binary_argv(argv), json.dumps(request, sort_keys=True, separators=(",", ":")),
            Path(execution_directory), timeout_seconds,
            heartbeat, cancellation_requested=cancellation_requested,
            environment_allowlist=LOCAL_CHILD_ENV_ALLOWLIST,
            pinned_executable_identity=_primary_executable_observation(observed_identity),
            pinned_argument_file_identities=observed_identity.get("argument_file_identities", []),
            on_process_group_exit_confirmed=on_process_group_exit_confirmed,
        )
    stdout = launched.pop("stdout")
    stderr = launched.pop("stderr")
    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        response = None
    if (
        launched.get("status") == "completed"
        and launched.get("returncode") == 0
        and (
            not isinstance(response, dict)
            or response.get("schema") != "LocalEndpointResponseV1"
            or response.get("model") != resolved_model
            or response.get("reasoning_effort") != reasoning_effort
            or response.get("endpoint_policy") != endpoint_policy
        )
    ):
        raise ProviderAdapterError("local_endpoint_response_binding_invalid")
    usage = response.get("usage") if isinstance(response, dict) else {
        "input_tokens": None, "output_tokens": None, "usage_verified": False,
        "cost_usd": None, "cost_verified": False,
    }
    return {
        "schema": "ProviderAdapterLaunchV1", "provider": "Local",
        "project_root": str(root), "selector": selector,
        "resolved_model": resolved_model, "reasoning_effort": reasoning_effort,
        "endpoint_sha256": hashlib.sha256(canonical_endpoint.encode()).hexdigest(),
        "endpoint_policy": endpoint_policy, "auth_mode": "none",
        "sandbox": {
            "mode": "no_tools", "tools_enabled": False,
            "workspace": "isolated_empty_ephemeral", "project_root_mounted": False,
            "network_target": "single_exact_endpoint",
        },
        "content": response.get("content") if isinstance(response, dict) else None,
        "usage": usage, "stderr": stderr, **launched,
    }


def _validate_codex_no_tool_events(events: list[dict[str, Any]]) -> None:
    """Reject any native tool/event surface that escaped feature denial."""

    for event in events:
        event_type = str(event.get("type", "")).strip().lower()
        if any(marker in event_type for marker in CODEX_FORBIDDEN_EVENT_MARKERS):
            raise ProviderAdapterError(f"codex_tool_event_forbidden:{event_type or 'unknown'}")
        item = event.get("item")
        if item is None:
            continue
        if not isinstance(item, dict):
            raise ProviderAdapterError("codex_item_event_invalid")
        item_type = str(item.get("type", "")).strip().lower()
        if item_type not in CODEX_SAFE_ITEM_TYPES:
            raise ProviderAdapterError(f"codex_tool_item_forbidden:{item_type or 'unknown'}")


def launch_codex(
    *, binary: str | Sequence[str] = "codex", project_root: str | Path, selector: str,
    reasoning_effort: str, prompt: str, timeout_seconds: float = 300,
    heartbeat: Callable[[str], None] | None = None, cancellation_requested: Callable[[], bool] | None = None,
    legacy_landlock_fallback: bool = True,
    resolved_model: str | None = None,
    expected_executable_identity: dict[str, Any] | None = None,
    persist_session: bool = False,
    resume_session_id: str | None = None,
    on_process_group_exit_confirmed: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Launch a no-tool Codex worker with a frozen model and capability set."""

    if reasoning_effort not in EFFORTS:
        raise ProviderAdapterError("reasoning_effort_invalid")
    bounded_prompt = sanitized_prompt(prompt)
    observed_identity = (
        verify_executable_identity(binary, expected_executable_identity)
        if expected_executable_identity is not None else executable_identity(binary)
    )
    effective_prefix, pinned_identity, pinned_arguments = _codex_effective_launch_prefix(binary, observed_identity)
    root = exact_project_root(project_root)
    if resolved_model is not None and not MODEL_SLUG.fullmatch(resolved_model):
        raise ProviderAdapterError("codex_resolved_model_invalid")
    if resume_session_id is not None and (
        not persist_session or not SESSION_UUID.fullmatch(resume_session_id)
    ):
        raise ProviderAdapterError("codex_resume_session_invalid")
    model = resolved_model or resolve_codex_model(
        selector,
        discover_codex_models(binary, expected_executable_identity=observed_identity),
    )
    session_mode = "resumed" if resume_session_id else "persistent" if persist_session else "ephemeral"
    compatibility = codex_no_tool_runtime_attestation(
        binary, timeout_seconds=min(float(timeout_seconds), 15.0),
        resolved_model=model, session_mode=session_mode,
        expected_executable_identity=observed_identity,
    )
    feature_preflight = dict(compatibility["feature_preflight"])
    feature_preflight["legacy_landlock_requested"] = bool(
        legacy_landlock_fallback and os.name == "posix"
    )
    feature_overrides = list(feature_preflight["launch_feature_overrides"])
    config_arguments = list(feature_preflight["launch_config_arguments"])
    environment_flag = bool(feature_preflight["legacy_landlock_enabled"])
    transport_attestation = dict(compatibility["transport_attestation"])
    # Discovery/preflight used the selected wrapper path. Recheck the entire
    # transitive chain immediately before creating the sealed launch copy.
    if expected_executable_identity is not None:
        observed_identity = verify_executable_identity(binary, expected_executable_identity)
        effective_prefix, pinned_identity, pinned_arguments = _codex_effective_launch_prefix(binary, observed_identity)
    # The worker receives its complete bounded input over stdin and has no
    # tools. Running it from the project root would still make Codex discover
    # project instructions and repository metadata, wasting context and
    # expanding ambient authority. Use an empty host-owned directory while
    # retaining the separately verified project root only as provenance.
    with tempfile.TemporaryDirectory(prefix="agentfanout-codex-worker-") as execution_directory:
        execution_root = Path(execution_directory)
        # Every sandbox/tool/config flag is in this shared prefix.  Initial and
        # resumed sessions differ only in session mechanics (`resume`, UUID,
        # and ephemeral persistence), never in authority.
        args = [
            *effective_prefix, "--strict-config", "--sandbox", "read-only",
            "--ask-for-approval", "never", "--cd", str(execution_root),
            *config_arguments, *feature_overrides, "exec",
        ]
        if resume_session_id is not None:
            args.append("resume")
        common = [
            "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            "--json", "--model", model,
            "--config", f"model_reasoning_effort={reasoning_effort}",
        ]
        if resume_session_id is None and not persist_session:
            common.append("--ephemeral")
        args.extend(common)
        if resume_session_id is not None:
            args.extend([resume_session_id, "-"])
        result = run_json_process(
            args, bounded_prompt, execution_root, timeout_seconds,
            heartbeat, cancellation_requested=cancellation_requested,
            environment_allowlist=CODEX_CHILD_ENV_ALLOWLIST,
            pinned_executable_identity=pinned_identity,
            pinned_argument_file_identities=pinned_arguments,
            on_process_group_exit_confirmed=on_process_group_exit_confirmed,
        )
    events = _parse_json_output(result.pop("stdout"))
    _validate_codex_no_tool_events(events)
    stderr = result.pop("stderr")
    session_ids = [
        event.get("thread_id") or event.get("session_id")
        for event in events
        if isinstance(event, dict) and event.get("type") in {"thread.started", "session.started"}
    ]
    session_ids = [
        value for value in session_ids
        if isinstance(value, str) and SESSION_UUID.fullmatch(value)
    ]
    provider_session_id = session_ids[-1] if session_ids else None
    if persist_session:
        if provider_session_id is None:
            raise ProviderAdapterError("codex_persistent_session_id_missing")
        if resume_session_id is not None and provider_session_id != resume_session_id:
            raise ProviderAdapterError("codex_resume_session_mismatch")
    return {
        "schema": "ProviderAdapterLaunchV1", "provider": "Codex", "project_root": str(root),
        "selector": selector, "resolved_model": model, "reasoning_effort": reasoning_effort,
        "provider_session_id": provider_session_id,
        "session_mode": session_mode,
        "argv": args, "sandbox": {
            "mode": "read-only", "tools_enabled": False,
            "tool_policy": "deny_all_known_native_surfaces_and_reject_tool_events",
            "denied_tool_features": list(CODEX_DENIED_TOOL_FEATURES),
            "feature_preflight": feature_preflight,
            "transport_attestation": transport_attestation,
            "strict_config": True, "approval_policy": "never",
            "tool_event_monitor": "fail_closed",
            "legacy_landlock_fallback": environment_flag,
            "workspace": "isolated_empty_ephemeral",
            "project_root_mounted": False,
        },
        "events": events, "usage": _usage_from_events(events), "stderr": stderr, **result,
    }


def validate_grok_request_budget(
    prompt: str, max_prompt_tokens: int, max_output_tokens: int,
) -> str:
    for value, reason, maximum in (
        (max_prompt_tokens, "grok_prompt_token_cap_invalid", 1_000_000),
        (max_output_tokens, "grok_output_token_cap_invalid", 32_768),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ProviderAdapterError(reason)
    bounded_prompt = sanitized_prompt(prompt)
    # For the admitted byte-tokenized transport, UTF-8 bytes are a
    # conservative upper bound on user-content tokens. Reserve a fixed 512
    # tokens for the helper's system/message framing. Provider-hidden usage is
    # still checked against measured totals after the call.
    if len(bounded_prompt.encode("utf-8")) + 512 > max_prompt_tokens:
        raise ProviderAdapterError("grok_prompt_exceeds_prelaunch_token_ceiling")
    return bounded_prompt


def launch_grok(
    *, binary: str | Sequence[str], project_root: str | Path, selector: str,
    reasoning_effort: str, prompt: str, output_contract: str,
    timeout_seconds: float = 300,
    max_prompt_tokens: int = 32_768, max_output_tokens: int = 1_400,
    heartbeat: Callable[[str], None] | None = None, cancellation_requested: Callable[[], bool] | None = None,
    resolved_model: str | None = None,
    expected_executable_identity: dict[str, Any] | None = None,
    grok_home: str | Path | None = None,
    expected_auth_file_identity: dict[str, Any] | None = None,
    expected_helper_identity: dict[str, Any] | None = None,
    on_process_group_exit_confirmed: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Launch one isolated Grok HTTPS turn whose request has no tool schema.

    The Grok CLI remains the authenticated model-discovery source bound by the
    run guard.  Inference does not use its agent harness: current CLI builds
    can prepare or execute built-in tools even when a profile says otherwise.
    A pinned ``python -I -S`` helper instead calls the fixed xAI HTTPS endpoint
    with no ``tools`` field and validates exact model, SSE, usage, and finish
    evidence before returning any content.
    """

    if reasoning_effort not in EFFORTS:
        raise ProviderAdapterError("reasoning_effort_invalid")
    if output_contract not in {"WorkerResultV1", "ValidatorResultV1"}:
        raise ProviderAdapterError("grok_output_contract_invalid")
    bounded_prompt = validate_grok_request_budget(
        prompt, max_prompt_tokens, max_output_tokens,
    )
    observed_identity = (
        verify_executable_identity(binary, expected_executable_identity)
        if expected_executable_identity is not None else executable_identity(binary)
    )
    root = exact_project_root(project_root)
    isolated_home = _secure_grok_home(grok_home)
    if (
        not isinstance(expected_auth_file_identity, dict)
        or expected_auth_file_identity.get("schema") != "GrokAuthFileIdentityV1"
    ):
        raise ProviderAdapterError("grok_auth_file_identity_required")
    if resolved_model is None:
        raise ProviderAdapterError("grok_resolved_model_required")
    if not MODEL_SLUG.fullmatch(resolved_model):
        raise ProviderAdapterError("grok_resolved_model_invalid")
    model = resolved_model
    # The exact admitted model receives the exact requested effort. Route
    # aliases never decide this transport property.
    effort_mode = grok_reasoning_effort_mode(model)
    if reasoning_effort not in grok_supported_reasoning_efforts(model):
        raise ProviderAdapterError("grok_model_reasoning_effort_unsupported")
    helper_argv = grok_http_helper_binary()
    if not isinstance(expected_helper_identity, dict):
        raise ProviderAdapterError("grok_http_helper_identity_required")
    helper_identity = verify_executable_identity(helper_argv, expected_helper_identity)
    helper_identity_sha256 = hashlib.sha256(
        json.dumps(
            helper_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode()
    ).hexdigest()
    with tempfile.TemporaryDirectory(prefix="agentfanout-grok-https-") as execution_directory:
        execution_root = Path(execution_directory)
        provider_home = execution_root / "provider-home"
        auth_isolation = _prepare_ephemeral_grok_home(
            isolated_home, provider_home, expected_auth_file_identity,
        )
        request = {
            "schema": "GrokDirectRequestV1",
            "auth_file": str(provider_home / "auth.json"),
            "auth_file_sha256": expected_auth_file_identity["auth_file_sha256"],
            "model": model,
            "reasoning_effort": reasoning_effort,
            "reasoning_effort_mode": effort_mode,
            "prompt": bounded_prompt,
            "output_contract": output_contract,
            "max_output_tokens": max_output_tokens,
            "timeout_seconds": max(1, min(float(timeout_seconds) / 2, 300)),
        }
        launched = run_json_process(
            helper_argv,
            json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            execution_root,
            timeout_seconds,
            heartbeat,
            cancellation_requested=cancellation_requested,
            environment_allowlist=GROK_CHILD_ENV_ALLOWLIST,
            pinned_executable_identity=_primary_executable_observation(helper_identity),
            pinned_argument_file_identities=helper_identity.get("argument_file_identities", []),
            on_process_group_exit_confirmed=on_process_group_exit_confirmed,
        )
        stdout = launched.pop("stdout")
        stderr = launched.pop("stderr")
        try:
            response = json.loads(stdout, parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"nonfinite:{value}")
            ))
        except (json.JSONDecodeError, ValueError):
            response = None
        if launched.get("status") != "completed" or launched.get("returncode") != 0:
            if (
                isinstance(response, dict)
                and response.get("schema") == "GrokDirectErrorV1"
                and isinstance(response.get("error"), str)
                and re.fullmatch(r"grok_http_[a-z0-9_]{1,120}", response["error"])
            ):
                raise ProviderAdapterError(response["error"])
            raise ProviderAdapterError("grok_http_helper_process_not_clean")
        if (
            not isinstance(response, dict)
            or response.get("schema") != "GrokDirectResponseV1"
            or response.get("model") != model
            or response.get("reasoning_effort") != reasoning_effort
            or response.get("reasoning_effort_mode") != effort_mode
            or response.get("output_contract") != output_contract
            or response.get("tools_field_sent") is not False
            or response.get("response_format_type") != "json_schema"
            or response.get("response_schema_name") != output_contract
            or response.get("redirects_allowed") is not False
            or response.get("ambient_proxy_allowed") is not False
            or response.get("endpoint_sha256") != GROK_DIRECT_ENDPOINT_SHA256
            or not re.fullmatch(r"[a-f0-9]{64}", str(response.get("response_id_sha256", "")))
            or isinstance(response.get("event_count"), bool)
            or not isinstance(response.get("event_count"), int)
            or not 1 <= response["event_count"] <= 10_000
            or isinstance(response.get("response_bytes"), bool)
            or not isinstance(response.get("response_bytes"), int)
            or not 1 <= response["response_bytes"] <= 2_000_000
            or not isinstance(response.get("content"), str)
            or not response["content"].strip()
            or not isinstance(response.get("usage"), dict)
            or response["usage"].get("usage_verified") is not True
        ):
            raise ProviderAdapterError("grok_http_response_binding_invalid")
        auth_isolation["transport_credential_scope"] = "fixed_api_x_ai_https_only"
        auth_isolation["agent_harness_used"] = False
    return {
        "schema": "ProviderAdapterLaunchV1", "provider": "Grok", "project_root": str(root),
        "selector": selector, "resolved_model": model, "reasoning_effort": reasoning_effort,
        "argv": helper_argv,
        "sandbox": {
            "mode": "direct_https_no_tools", "memory": False, "subagents": False, "web": False,
            "filesystem_capability": False, "terminal_capability": False,
            "mcp_servers": [], "workspace": "isolated_empty_ephemeral",
            "project_root_mounted": False, "dedicated_grok_home": True,
            "effective_grok_home": "auth_only_ephemeral",
            "os_sandbox_attested": False,
            "boundary_assurance": "no_tool_schema_fixed_https_helper_and_empty_workspace",
            "network_target": "single_exact_api_x_ai_https_no_redirects_no_ambient_proxy",
        },
        "events": [{"type": "assistant.completed", "text": response["content"]}],
        "usage": response["usage"],
        "model_acknowledgement": {
            "current_model_id": model,
            "reasoning_effort": reasoning_effort,
            "reasoning_effort_basis": effort_mode,
            "transport": "direct_xai_chat_completions_sse",
        },
        "auth_isolation": auth_isolation,
        "transport_attestation": {
            key: response[key] for key in (
                "endpoint_sha256", "tools_field_sent", "redirects_allowed",
                "ambient_proxy_allowed", "response_format_type", "response_schema_name",
                "response_id_sha256", "event_count",
                "response_bytes",
            )
        } | {"helper_identity_sha256": helper_identity_sha256},
        "discovery_executable_identity": _primary_executable_observation(observed_identity),
        "stderr": stderr,
        **launched,
    }
