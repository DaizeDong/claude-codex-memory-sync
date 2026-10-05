"""Sanitized profile health; local MCP initialization is optional and bounded."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import time
import urllib.parse
import urllib.request


EXCLUSIONS = {
    "existing_legacy_codex_skill_preserved", "native_codex_server_preserved",
    "recursive_codex_mcp_server_not_imported", "sse_transport_not_supported_by_codex",
    "claude_file_path_payload_and_document_globs_require_porting",
    "claude_plugin_cache_mutation_and_pruning_not_portable",
    "tool_restrictions_not_enforceable",
}
PROBLEM_STATUSES = {"conflict", "unsupported", "unavailable", "broken", "ambiguous",
                    "unresolved", "missing_source", "error", "invalid", "blocked", "partial"}


def assess(report, mcp_checks=()):
    findings, exclusions = [], []
    if report.get("change_count", 0):
        findings.append({"area": "sync", "reason": "pending_changes", "count": report["change_count"]})
    rows = [("skills", x) for x in report.get("skills", [])]
    rows += [("mcp", x) for x in report.get("config", {}).get("mcp", [])]
    rows += [("hooks", x) for x in report.get("hooks", {}).get("hooks", [])]
    rows += [("instructions", report.get("instructions", {}))]
    rows += [("project_docs", report.get("config", {}).get("project_docs", {}))]
    rows += [("skill_routing", report.get("config", {}).get("skill_routing", {}))]
    rows += [("memory", report.get("memory", {}))]
    agent_report = report.get("agents", {})
    if isinstance(agent_report, dict):
        rows += [("agents", x) for x in agent_report.get("agents", agent_report.get("roles", []))]
    for area, row in rows:
        if row.get("status") not in PROBLEM_STATUSES:
            continue
        item = {"area": area, **{k: row[k] for k in ("name", "status", "reason") if k in row}}
        (exclusions if row.get("reason") in EXCLUSIONS else findings).append(item)
    for row in report.get("plugins_skipped", []):
        if row.get("reason") != "disabled_in_claude":
            findings.append({"area": "plugins", "name": row.get("name"), "reason": row.get("reason")})
    hook_feature = report.get("hooks", {})
    if hook_feature.get("requires_hooks_feature") and hook_feature.get("feature") != "enabled":
        item = {"area": "hook_feature", "reason": hook_feature.get("feature", "feature_status_missing")}
        (exclusions if item["reason"] == "explicitly_disabled_preserved" else findings).append(item)
    for area in ("config", "hooks", "inventory", "agents"):
        section = report.get(area, {})
        if isinstance(section, dict):
            for row in section.get("warnings", []):
                findings.append({"area": area, "reason": row.get("reason", "reported_warning")})
    for row in report.get("config", {}).get("settings", []):
        if row.get("status") == "unsupported":
            exclusions.append({"area": "settings", "key": row.get("key"), "reason": row.get("reason")})
    for row in mcp_checks:
        if row["status"] in {"unavailable", "missing_dependency"}:
            findings.append({"area": "mcp_runtime", **row})
    inventory = report.get("inventory", {})
    if isinstance(inventory, dict):
        for row in inventory.get("skills", []):
            if row.get("status") in PROBLEM_STATUSES or row.get("broken"):
                findings.append({"area": "inventory", "name": row.get("name"),
                                 "status": row.get("status"), "reason": row.get("reason", "broken_skill_link")})
            for dependency in row.get("dependencies", {}).get("executables", []):
                if not dependency.get("available"):
                    findings.append({"area": "skill_dependency", "name": row.get("name"),
                                     "dependency": dependency.get("name"), "reason": "executable_not_found"})
    return {"status": "degraded" if findings else "healthy", "findings": findings,
            "exclusions": exclusions, "mcp_checks": list(mcp_checks)}


class _LocalRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before, after = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if code not in {307, 308} or (before.scheme, before.netloc) != (after.scheme, after.netloc) or not _local_url(newurl):
            raise OSError("MCP probe redirect refused")
        return urllib.request.Request(newurl, data=req.data, headers=dict(req.headers), method=req.get_method())


def _local_url(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def probe_local(name, server, timeout=4):
    """Initialize loopback only; allow its 307/308 canonical path redirects."""
    row = {"name": name, "status": "unavailable", "check": "initialize"}
    url = server.get("url", "")
    if not _local_url(url):
        return {"name": name, "status": "not_checked", "reason": "remote_transport"}
    request = urllib.request.Request(url, data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "profile-sync-health", "version": "1"}},
    }).encode(), headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}, method="POST")
    try:
        # No proxy forwarding of loopback probes, no redirects to an external service.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _LocalRedirect())
        deadline = time.monotonic() + timeout
        with opener.open(request, timeout=timeout) as response:
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                body = b""
                for _ in range(128):
                    if time.monotonic() > deadline:
                        raise TimeoutError("MCP event deadline")
                    line = response.readline(65537)
                    if len(line) > 65536:
                        raise ValueError("oversize")
                    if line.startswith(b"data:"):
                        body = line[5:].strip()
                        break
                    if not line:
                        break
            else:
                body = response.read(65537)
            if len(body) > 65536:
                raise ValueError("oversize")
            message = json.loads(body)
            result = message.get("result", {})
            if message.get("id") == 1 and result.get("protocolVersion") and "capabilities" in result:
                row.update(status="available", reason="initialize_succeeded")
            else:
                row["reason"] = "invalid_initialize_response"
    except Exception as exc:
        row["reason"] = type(exc).__name__
    return row


def check_mcp(servers, probe=False):
    rows, pending = [], []
    for name, server in sorted(servers.items()):
        if server.get("enabled") is False:
            rows.append({"name": name, "status": "excluded", "reason": "disabled"})
        elif server.get("url"):
            if probe and _local_url(server["url"]):
                pending.append((name, server))
            else:
                rows.append({"name": name, "status": "not_checked", "reason": "remote_transport" if not _local_url(server["url"]) else "probe_not_requested"})
        else:
            command = os.path.expandvars(server.get("command", ""))
            exists = bool(command and (shutil.which(command) or Path(command).is_file()))
            rows.append({"name": name, "status": "present" if exists else "missing_dependency",
                         "check": "executable_only", "reason": "not_a_server_handshake" if exists else "command_not_found"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows.extend(pool.map(lambda item: probe_local(*item), pending))
    return sorted(rows, key=lambda x: x["name"])
