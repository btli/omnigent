#!/usr/bin/env python3
"""
End-to-end test: `sandbox.host_config` injection on the Kubernetes provider.

Runs against an EXISTING omnigent server already configured with
``sandbox.provider: kubernetes`` and a ``sandbox.host_config:`` block (see
``deploy/kubernetes/overlays/sandbox-runners/`` — apply the overlay, put a
``host_config:`` block in ``sandbox-config.yaml``, and make the server URL
reachable from where this script runs, e.g. via ``kubectl port-forward``).

The script creates a managed session, waits for the runner Pod's host to
register, then ``kubectl exec``'s into the Pod and asserts the injected
config landed at ``/home/omnigent/.omnigent/config.yaml`` before the host
came up. It needs ``kubectl`` on PATH with access to the runner namespace.

    python tests/e2e/integrations/deploy/kubernetes/e2e_managed_host_config.py \
        --server http://localhost:8080 \
        --expect "kind: gateway"
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time

import httpx

# Constants pinned by the kubernetes launcher (see
# omnigent/onboarding/sandboxes/kubernetes.py): the Pod's fixed HOME, the
# main container name, and the labels stamped on every runner Pod.
POD_HOME = "/home/omnigent"
HOST_CONTAINER = "host"
POD_SELECTOR = "app.kubernetes.io/managed-by=omnigent,omnigent.ai/role=sandbox-host"


def log(msg: str) -> None:
    print(msg, flush=True)


def check_server(base: str) -> None:
    log(f"[1/5] checking {base}/v1/info")
    info = httpx.get(f"{base}/v1/info", timeout=10.0).json()
    if not info.get("managed_sandboxes_enabled"):
        raise SystemExit("server does not advertise managed sandboxes — is sandbox: configured?")
    if info.get("sandbox_provider") != "kubernetes":
        raise SystemExit(
            f"server's sandbox provider is {info.get('sandbox_provider')!r}, not 'kubernetes'"
        )
    log("      ✓ managed sandboxes enabled (kubernetes)")


def pick_agent(base: str, agent_id: str | None) -> str:
    resp = httpx.get(f"{base}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    agents = resp.json()["data"]
    if not agents:
        raise SystemExit("no agents registered on the server to bind a session to")
    if agent_id:
        if not any(a.get("id") == agent_id for a in agents):
            raise SystemExit(f"agent_id {agent_id!r} not found on the server")
        return agent_id
    chosen = agents[0]
    log(f"      agent_id={chosen['id']} ({chosen.get('name')})")
    return chosen["id"]


def create_managed_session(base: str, agent_id: str) -> str:
    log("[2/5] creating managed session")
    r = httpx.post(
        f"{base}/v1/sessions",
        json={"agent_id": agent_id, "host_type": "managed"},
        timeout=180.0,
    )
    if r.status_code >= 300:
        raise SystemExit(f"create session failed: HTTP {r.status_code}: {r.text[:600]}")
    conv_id = r.json()["id"]
    log(f"      session={conv_id}")
    return conv_id


def wait_host_online(base: str, conv_id: str, timeout_s: float) -> None:
    log("[3/5] waiting for the runner Pod's host to register")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        d = httpx.get(f"{base}/v1/sessions/{conv_id}", timeout=10.0).json()
        if d.get("host_id"):
            log(f"      ✓ host online: host_id={d['host_id']}")
            return
        status = d.get("sandbox_status") or {}
        if status.get("stage") == "failed":
            raise SystemExit(f"managed launch failed: {status.get('error')}")
        time.sleep(5.0)
    raise SystemExit(f"host did not come online within {timeout_s:.0f}s")


def newest_runner_pod(kubectl: str, namespace: str) -> str:
    out = subprocess.run(
        [
            *shlex.split(kubectl),
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            POD_SELECTOR,
            "--sort-by=.metadata.creationTimestamp",
            "-o",
            "name",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if not out:
        raise SystemExit(f"no runner Pods matching '{POD_SELECTOR}' in namespace {namespace!r}")
    return out[-1]


def assert_injected_config(kubectl: str, namespace: str, pod: str, expect: str) -> str:
    log(f"[4/5] reading {POD_HOME}/.omnigent/config.yaml from {pod}")
    proc = subprocess.run(
        [
            *shlex.split(kubectl),
            "exec",
            "-n",
            namespace,
            pod,
            "-c",
            HOST_CONTAINER,
            "--",
            "cat",
            f"{POD_HOME}/.omnigent/config.yaml",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"config.yaml missing in the runner Pod — host_config was not injected?\n"
            f"{proc.stderr.strip()}"
        )
    content = proc.stdout
    log("      --- config.yaml ---")
    log(content.rstrip())
    if expect not in content:
        raise SystemExit(f"config.yaml does not contain the expected fragment {expect!r}")
    log(f"      ✓ contains {expect!r}")
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Omnigent server base URL")
    parser.add_argument("--agent-id", default=None, help="Agent to bind (default: first)")
    parser.add_argument("--namespace", default="omnigent-sandboxes", help="Runner-Pod namespace")
    parser.add_argument(
        "--expect",
        default="providers:",
        help="Substring the injected config.yaml must contain (default: 'providers:')",
    )
    parser.add_argument(
        "--kubectl",
        default="kubectl",
        help="kubectl command, split shell-style (e.g. 'kubectl --context my-cluster')",
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="Host-online wait (s)")
    parser.add_argument("--keep", action="store_true", help="Skip session cleanup")
    args = parser.parse_args()
    base = args.server.rstrip("/")

    check_server(base)
    agent_id = pick_agent(base, args.agent_id)
    conv_id = create_managed_session(base, agent_id)
    try:
        wait_host_online(base, conv_id, args.timeout)
        pod = newest_runner_pod(args.kubectl, args.namespace)
        assert_injected_config(args.kubectl, args.namespace, pod, args.expect)
    finally:
        if args.keep:
            log(f"[5/5] --keep: leaving session {conv_id} (and its Pod) running")
        else:
            log(f"[5/5] deleting session {conv_id} (terminates the runner Pod)")
            try:
                httpx.delete(f"{base}/v1/sessions/{conv_id}", timeout=60.0)
            except httpx.HTTPError as exc:
                log(f"      cleanup failed (Pod may linger): {exc}")
    log("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
