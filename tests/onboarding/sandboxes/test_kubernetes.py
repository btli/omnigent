"""
Tests for the Kubernetes (entrypoint-as-host) sandbox launcher.

The official ``kubernetes`` client is an optional dependency, so the SDK-driven
tests inject a small fake package into ``sys.modules`` (no real cluster, no real
client). The entrypoint model needs only ``CoreV1Api`` create/read/delete/log
fakes — there is no exec transport to fake.
"""

from __future__ import annotations

import base64
import json
import sys
import traceback
import types
from types import SimpleNamespace

import click
import pytest

import omnigent.onboarding.sandboxes.kubernetes as k8s
from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.onboarding.sandboxes.kubernetes import (
    KubernetesSandboxLauncher,
    build_clone_secret_manifest,
    build_pod_manifest,
    build_token_secret_manifest,
)

_TOKEN = "launch-token-xyz"
_MANIFEST_KW = {
    "pod_name": "omnigent-managed-abc-1a2b3c",
    "namespace": "omnigent-sandboxes",
    "image": "ghcr.io/omnigent-ai/omnigent-host:latest",
    "service_account": "omnigent-runner",
    "host_id": "host_abcdef",
    "host_name": "managed-abcdef",
    "server_url": "http://srv.example.com",
    "token_secret_name": "omnigent-managed-abc-1a2b3c-token",
    "harness_secret": "omnigent-creds",
    "env_literals": {},
    "node_selector": None,
    "workspace": "/home/omnigent/workspace",
}


# ── pure manifest / rendering tests (no SDK) ────────────────


def test_build_pod_manifest_runs_host_under_reaper_as_container_command() -> None:
    """The main container's command execs the PID-1 reaper, which runs the host."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    containers = manifest["spec"]["containers"]
    assert len(containers) == 1
    host = containers[0]
    assert host["name"] == "host"
    command = host["command"]
    assert command[:2] == ["bash", "-lc"]
    script = command[2]
    # exec the reaper (so it is PID 1) which then runs `omnigent host`.
    assert "exec python3 -c" in script
    assert "omnigent host --server http://srv.example.com" in script
    # The reaper source rides the command (spawns sys.argv[1:] + reaps children).
    assert "os.wait()" in script


def test_build_pod_manifest_init_container_prepares_and_clones_workspace() -> None:
    """The init container makes the workspace and clones the repo before the host."""
    manifest = build_pod_manifest(
        **{**_MANIFEST_KW, "clone_dir": "/home/omnigent/workspace/repo"},
        repo_url="https://github.com/org/repo.git",
        repo_branch="main",
    )
    init = manifest["spec"]["initContainers"]
    assert len(init) == 1
    assert init[0]["name"] == "workspace-prep"
    script = init[0]["command"][2]
    assert "mkdir -p /home/omnigent/workspace" in script
    assert "git clone --branch main --single-branch -- " in script
    assert "https://github.com/org/repo.git /home/omnigent/workspace/repo" in script


def test_build_pod_manifest_without_repo_has_no_clone() -> None:
    """No repo → the init container only makes the workspace, no git clone."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    script = manifest["spec"]["initContainers"][0]["command"][2]
    assert "mkdir -p /home/omnigent/workspace" in script
    assert "git clone" not in script


def test_build_pod_manifest_token_rides_secret_ref_not_the_spec() -> None:
    """The launch token is referenced via secretKeyRef, never written into the spec."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    host_env = manifest["spec"]["containers"][0]["env"]
    token_entry = next(e for e in host_env if e["name"] == HOST_TOKEN_ENV_VAR)
    assert token_entry["valueFrom"]["secretKeyRef"] == {
        "name": "omnigent-managed-abc-1a2b3c-token",
        "key": HOST_TOKEN_ENV_VAR,
    }
    assert "value" not in token_entry
    # Identity is plain env; the raw token appears nowhere in the manifest.
    assert {e["name"]: e.get("value") for e in host_env}[HOST_ID_ENV_VAR] == "host_abcdef"
    assert {e["name"]: e.get("value") for e in host_env}[HOST_NAME_ENV_VAR] == "managed-abcdef"
    assert _TOKEN not in json.dumps(manifest)


def test_build_token_secret_manifest_carries_token_in_stringdata() -> None:
    """The token Secret holds the raw token under the host-token key, labeled for GC."""
    secret = build_token_secret_manifest(
        secret_name="omnigent-pod-token", namespace="omnigent-sandboxes", token=_TOKEN
    )
    assert secret["stringData"] == {HOST_TOKEN_ENV_VAR: _TOKEN}
    assert secret["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "omnigent"
    assert secret["type"] == "Opaque"


def test_clone_secret_manifest_shape() -> None:
    """Opaque, GC-labeled like the token Secret, stringData = the env pairs."""
    manifest = build_clone_secret_manifest(
        secret_name="omnigent-pod-1-clone-cred",
        namespace="ns",
        clone_env={"GIT_TOKEN": "tok-value", "GIT_USERNAME": "alice"},
    )
    assert manifest["type"] == "Opaque"
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "omnigent",
        "omnigent.ai/role": "sandbox-host",
    }
    assert manifest["stringData"] == {"GIT_TOKEN": "tok-value", "GIT_USERNAME": "alice"}


def test_build_pod_manifest_harness_secret_projects_into_both_containers() -> None:
    """The harness creds Secret is projected via envFrom on init (for clone) + host."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    init = manifest["spec"]["initContainers"][0]
    host = manifest["spec"]["containers"][0]
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]
    assert host["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]


def test_build_pod_manifest_omits_envfrom_without_harness_secret() -> None:
    """No harness Secret → no envFrom key on either container."""
    manifest = build_pod_manifest(**{**_MANIFEST_KW, "harness_secret": None})
    assert "envFrom" not in manifest["spec"]["initContainers"][0]
    assert "envFrom" not in manifest["spec"]["containers"][0]


def test_pod_manifest_clone_secret_projection() -> None:
    """Init container swaps to the clone Secret; main container is untouched."""
    manifest = build_pod_manifest(
        **{
            **_MANIFEST_KW,
            "env_literals": {"HTTPS_PROXY": "http://proxy:3128", "GIT_USERNAME": "operator-lit"},
        },
        clone_secret_name="omnigent-pod-1-clone-cred",
        clone_env_keys=("GIT_TOKEN", "GIT_USERNAME"),
    )
    init = manifest["spec"]["initContainers"][0]
    main = manifest["spec"]["containers"][0]
    # Init: clone Secret ref REPLACES the harness ref.
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-pod-1-clone-cred"}}]
    # Init env: HOME + env_literals MINUS clone_env_keys (collision rule).
    init_names = [e["name"] for e in init["env"]]
    assert "HTTPS_PROXY" in init_names
    assert "GIT_USERNAME" not in init_names
    # Main: harness ref intact, ALL env_literals intact (colliding name included),
    # and no reference to the clone Secret anywhere in the container spec.
    assert main["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]
    assert {"name": "GIT_USERNAME", "value": "operator-lit"} in main["env"]
    assert "clone-cred" not in json.dumps(manifest["spec"]["containers"])


def test_pod_manifest_without_clone_secret_is_unchanged() -> None:
    """clone_secret_name=None keeps today's exact init shape (regression)."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    init = manifest["spec"]["initContainers"][0]
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]
    assert init["env"] == [{"name": "HOME", "value": "/home/omnigent"}]


def test_build_pod_manifest_defaults_to_amd64_node_selector() -> None:
    """No node_selector → Pods keep the amd64 default placement."""
    manifest = build_pod_manifest(**{**_MANIFEST_KW, "node_selector": None})
    assert manifest["spec"]["nodeSelector"] == {"kubernetes.io/arch": "amd64"}


def test_build_pod_manifest_node_selector_can_override_arch() -> None:
    """An operator kubernetes.io/arch entry overrides the amd64 default."""
    manifest = build_pod_manifest(
        **{**_MANIFEST_KW, "node_selector": {"disktype": "ssd", "kubernetes.io/arch": "arm64"}}
    )
    selector = manifest["spec"]["nodeSelector"]
    assert selector["kubernetes.io/arch"] == "arm64"
    assert selector["disktype"] == "ssd"


def test_build_pod_manifest_is_restricted_and_least_privilege() -> None:
    """The Pod satisfies Pod Security 'restricted' and mounts no SA token."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    spec = manifest["spec"]
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    host = spec["containers"][0]
    assert host["securityContext"]["allowPrivilegeEscalation"] is False
    assert host["securityContext"]["capabilities"] == {"drop": ["ALL"]}


@pytest.mark.parametrize(
    ("clone_dir", "repo_url", "repo_branch", "expect_clone", "expect_branch"),
    [
        (None, None, None, False, False),
        ("/ws/repo", "https://x/y.git", None, True, False),
        ("/ws/repo", "https://x/y.git", "release-1.2", True, True),
    ],
)
def test_render_workspace_prep_command(
    clone_dir: str | None,
    repo_url: str | None,
    repo_branch: str | None,
    expect_clone: bool,
    expect_branch: bool,
) -> None:
    """The init command always mkdir's the workspace and clones only when asked."""
    command = k8s._render_workspace_prep_command("/ws", clone_dir, repo_url, repo_branch)
    script = command[2]
    assert "mkdir -p /ws" in script
    assert ("git clone" in script) is expect_clone
    assert ("--branch release-1.2 --single-branch" in script) is expect_branch


def test_new_pod_name_and_token_secret_name() -> None:
    """Pod names are DNS-label-safe and the token Secret is the pod name + suffix."""
    name = k8s._new_pod_name("Managed-ABC_123!")
    assert name.startswith("omnigent-managed-abc-123-")
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)
    assert k8s._token_secret_name(name) == f"{name}-token"


def test_resolve_sandbox_env_rejects_reserved_and_credential_and_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env passthrough rejects reserved names, credential-looking names, and unset vars."""
    monkeypatch.setenv("PLAIN_CONFIG", "value")
    assert KubernetesSandboxLauncher(env=["PLAIN_CONFIG"])._resolve_sandbox_env() == {
        "PLAIN_CONFIG": "value"
    }
    with pytest.raises(click.ClickException, match="reserved"):
        KubernetesSandboxLauncher(env=["HOME"])._resolve_sandbox_env()
    with pytest.raises(click.ClickException, match="credential"):
        KubernetesSandboxLauncher(env=["MY_API_KEY"])._resolve_sandbox_env()
    with pytest.raises(click.ClickException, match="not set"):
        KubernetesSandboxLauncher(env=["DEFINITELY_UNSET_VAR_XYZ"])._resolve_sandbox_env()


def test_env_var_name_override_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env-var namespace override that isn't a valid RFC 1123 name fails fast."""
    monkeypatch.setenv(k8s.NAMESPACE_ENV_VAR, "Not_A_Valid_NS")
    with pytest.raises(click.ClickException, match="not a valid Kubernetes name"):
        KubernetesSandboxLauncher()._resolve_namespace()


# ── SDK-driven tests (fake kubernetes client) ───────────────


class _FakeApiException(Exception):
    """Stands in for ``kubernetes.client.rest.ApiException``.

    ``__str__`` mirrors the real class (status + reason + body), not the
    ``Exception`` default — the real class's body lands in a chained
    traceback exactly as F1 guards against, so a fake that dropped it would
    let a regressed ``from exc`` pass unnoticed.
    """

    def __init__(self, *, status: int | None = None, reason: str = "", body: str = "") -> None:
        super().__init__(reason or body or str(status))
        self.status = status
        self.reason = reason
        self.body = body

    def __str__(self) -> str:
        message = f"({self.status})\nReason: {self.reason}\n"
        if self.body:
            message += f"HTTP response body: {self.body}\n"
        return message


class _FakeConfigException(Exception):
    """Stands in for ``kubernetes.config.ConfigException``."""


class _FakeCore:
    """Recording stand-in for ``CoreV1Api`` (entrypoint model: no exec)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created_secrets: list[dict[str, object]] = []
        self.created_pods: list[dict[str, object]] = []
        self.deleted_pods: list[str] = []
        self.deleted_secrets: list[str] = []
        self.patched_secrets: list[tuple[str, dict[str, object]]] = []
        self.events: list[object] = []
        self.logs: dict[str, str] = {}
        self.read_queue: list[object] = []
        self.read_default: object = _pod(phase="Pending")
        self.create_secret_errors: list[Exception | None] = []
        self.create_pod_error: Exception | None = None
        self.delete_pod_errors: list[Exception | None] = []
        self.delete_secret_errors: list[Exception | None] = []
        self.patch_secret_error: Exception | None = None

    def create_namespaced_secret(self, namespace, body, _request_timeout=None):
        self.calls.append("create_secret")
        if self.create_secret_errors:
            err = self.create_secret_errors.pop(0)
            if err is not None:
                raise err
        self.created_secrets.append(body)

    def create_namespaced_pod(self, namespace, body, _request_timeout=None):
        self.calls.append("create_pod")
        if self.create_pod_error is not None:
            raise self.create_pod_error
        self.created_pods.append(body)
        return SimpleNamespace(metadata=SimpleNamespace(uid="pod-uid-123"))

    def read_namespaced_pod(self, name, namespace, _request_timeout=None):
        self.calls.append("read_pod")
        resp = self.read_queue.pop(0) if self.read_queue else self.read_default
        if isinstance(resp, Exception):
            raise resp
        return resp

    def delete_namespaced_pod(
        self, name, namespace, grace_period_seconds=None, _request_timeout=None
    ):
        self.calls.append("delete_pod")
        if self.delete_pod_errors:
            err = self.delete_pod_errors.pop(0)
            if err is not None:
                raise err
        self.deleted_pods.append(name)

    def delete_namespaced_secret(self, name, namespace, _request_timeout=None):
        self.calls.append("delete_secret")
        if self.delete_secret_errors:
            err = self.delete_secret_errors.pop(0)
            if err is not None:
                raise err
        self.deleted_secrets.append(name)

    def patch_namespaced_secret(self, name, namespace, body, _request_timeout=None):
        self.calls.append("patch_secret")
        if self.patch_secret_error is not None:
            raise self.patch_secret_error
        self.patched_secrets.append((name, body))

    def list_namespaced_event(self, namespace, field_selector=None, _request_timeout=None):
        return SimpleNamespace(items=self.events)

    def read_namespaced_pod_log(
        self, name, namespace, container=None, tail_lines=None, _request_timeout=None
    ):
        return self.logs.get(container, "")


def _pod(phase=None, init_statuses=None, container_statuses=None, conditions=None):
    """Build a ``V1Pod`` stand-in (the launcher reads only ``status`` via getattr)."""
    return SimpleNamespace(
        status=SimpleNamespace(
            phase=phase,
            init_container_statuses=init_statuses,
            container_statuses=container_statuses,
            conditions=conditions,
        )
    )


def _terminated(exit_code, *, name, reason="Error"):
    """A container status in the terminated state."""
    return SimpleNamespace(
        name=name,
        state=SimpleNamespace(
            terminated=SimpleNamespace(exit_code=exit_code, reason=reason), waiting=None
        ),
    )


@pytest.fixture
def fake_core(monkeypatch: pytest.MonkeyPatch) -> _FakeCore:
    """Inject a fake ``kubernetes`` package and return the recording CoreV1Api."""
    core = _FakeCore()

    client_mod = types.ModuleType("kubernetes.client")
    client_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    client_mod.Configuration = lambda: SimpleNamespace()  # type: ignore[attr-defined]
    client_mod.ApiClient = lambda cfg=None: SimpleNamespace(  # type: ignore[attr-defined]
        close=lambda: None
    )
    client_mod.CoreV1Api = lambda api_client=None: core  # type: ignore[attr-defined]
    rest_mod = types.ModuleType("kubernetes.client.rest")
    rest_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    config_mod = types.ModuleType("kubernetes.config")
    config_mod.load_incluster_config = lambda client_configuration=None: None  # type: ignore[attr-defined]
    config_mod.load_kube_config = (  # type: ignore[attr-defined]
        lambda config_file=None, client_configuration=None: None
    )
    config_mod.ConfigException = _FakeConfigException  # type: ignore[attr-defined]
    pkg = types.ModuleType("kubernetes")
    pkg.client = client_mod  # type: ignore[attr-defined]
    pkg.config = config_mod  # type: ignore[attr-defined]

    for name, mod in (
        ("kubernetes", pkg),
        ("kubernetes.client", client_mod),
        ("kubernetes.client.rest", rest_mod),
        ("kubernetes.config", config_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    # No-op the poll/backoff sleeps so the readiness/retry loops run instantly.
    monkeypatch.setattr(k8s.time, "sleep", lambda _s: None)
    return core


def _launcher() -> KubernetesSandboxLauncher:
    """A launcher pinned to in-cluster config with explicit, env-free settings."""
    return KubernetesSandboxLauncher(
        in_cluster=True, namespace="omnigent-sandboxes", secret_name="omnigent-creds", env=()
    )


def test_launch_host_creates_secret_then_pod_and_returns_workspace(
    fake_core: _FakeCore,
) -> None:
    """The happy path creates the token Secret BEFORE the Pod and returns the workspace."""
    fake_core.read_queue = [_pod(phase="Running")]
    workspace = _launcher().start_host(
        "omnigent-pod-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    assert workspace == "/home/omnigent/workspace"
    # Secret is created before the Pod (so the secretKeyRef resolves immediately).
    assert fake_core.calls.index("create_secret") < fake_core.calls.index("create_pod")
    assert fake_core.created_secrets[0]["stringData"] == {HOST_TOKEN_ENV_VAR: _TOKEN}
    assert fake_core.created_pods[0]["metadata"]["name"] == "omnigent-pod-1"
    # Nothing torn down on success.
    assert fake_core.deleted_pods == []


_CLONE_ENV = {"GIT_TOKEN": "tok-secret-value", "GIT_USERNAME": "alice"}


def _start_with_clone(fake_core: _FakeCore, *, clone_env: dict[str, str] | None = None) -> str:
    # Default to an immediate Running pod; a caller that pre-populates
    # read_queue (e.g. to inject a failure) keeps its own setup.
    if not fake_core.read_queue:
        fake_core.read_queue = [_pod(phase="Running")]
    return _launcher().start_host(
        "omnigent-pod-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
        repo_url="https://forge.example/org/repo.git",
        repo_name="repo",
        clone_env=dict(clone_env if clone_env is not None else _CLONE_ENV),
    )


def test_clone_env_lifecycle_happy_path(fake_core: _FakeCore) -> None:
    """token Secret → clone Secret → Pod → ownerRef patch → delete clone Secret."""
    workspace = _start_with_clone(fake_core)
    assert workspace == "/home/omnigent/workspace/repo"
    assert fake_core.calls.index("create_secret") < fake_core.calls.index("create_pod")
    assert fake_core.calls.count("create_secret") == 2
    assert fake_core.calls.index("create_pod") < fake_core.calls.index("patch_secret")
    clone = fake_core.created_secrets[1]
    assert clone["metadata"]["name"] == "omnigent-pod-1-clone-cred"
    assert clone["stringData"] == _CLONE_ENV
    name, body = fake_core.patched_secrets[0]
    assert name == "omnigent-pod-1-clone-cred"
    assert body["metadata"]["ownerReferences"][0]["uid"] == "pod-uid-123"
    assert body["metadata"]["ownerReferences"][0]["name"] == "omnigent-pod-1"
    # Deleted right after Running; the token Secret and Pod survive.
    assert fake_core.calls.index("delete_secret") > fake_core.calls.index("read_pod")
    assert fake_core.deleted_secrets == ["omnigent-pod-1-clone-cred"]
    assert fake_core.deleted_pods == []
    init = fake_core.created_pods[0]["spec"]["initContainers"][0]
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-pod-1-clone-cred"}}]


def test_clone_env_values_never_leave_secret_body(
    fake_core: _FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    _start_with_clone(fake_core)
    assert "tok-secret-value" not in json.dumps(fake_core.created_pods)
    assert "alice" not in json.dumps(fake_core.created_pods)
    captured_out = capsys.readouterr().out
    assert "tok-secret-value" not in captured_out
    assert "alice" not in captured_out


def test_clone_secret_create_failure_cleans_up(fake_core: _FakeCore) -> None:
    """Failure creating the SECOND secret still tears down the first + any Pod."""
    token_b64 = base64.b64encode(b"tok-secret-value").decode()
    # Go's encoding/json (the apiserver) HTML-escapes <, >, & by default —
    # unlike Python's json.dumps — so an echoed value can surface in this form.
    username = "a<b&c"
    username_go_escaped = "a\\u003cb\\u0026c"
    # Go also leaves non-ASCII as raw UTF-8 (unlike Python's default
    # ensure_ascii=True, which \uXXXX-escapes it), so a value combining both
    # surfaces with the non-ASCII character raw and only the HTML char escaped.
    email = "é<c"
    email_go_escaped = "é\\u003cc"
    fake_core.create_secret_errors = [
        None,
        _FakeApiException(
            status=500,
            reason="boom",
            body=(
                "admission webhook denied: tok-secret-value leaked "
                f"(data: {token_b64}); reflected username: {username_go_escaped}; "
                f"reflected email: {email_go_escaped}"
            ),
        ),
    ]
    with pytest.raises(click.ClickException) as excinfo:
        _start_with_clone(
            fake_core,
            clone_env={
                "GIT_TOKEN": "tok-secret-value",
                "GIT_USERNAME": username,
                "GIT_EMAIL": email,
            },
        )
    assert "tok-secret-value" not in str(excinfo.value)
    assert token_b64 not in str(excinfo.value)
    assert username not in str(excinfo.value)
    assert username_go_escaped not in str(excinfo.value)
    assert email not in str(excinfo.value)
    assert email_go_escaped not in str(excinfo.value)
    assert "***" in str(excinfo.value)
    assert "tok-secret-value" not in "".join(traceback.format_exception(excinfo.value))
    assert "omnigent-pod-1-token" in fake_core.deleted_secrets
    assert "omnigent-pod-1-clone-cred" in fake_core.deleted_secrets


def test_manifest_build_error_cleans_up_both_secrets(
    fake_core: _FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception between the Secret creates and the Pod create (e.g. a
    manifest-build error) must not orphan either Secret."""

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(k8s, "build_pod_manifest", _boom)
    with pytest.raises(RuntimeError):
        _start_with_clone(fake_core)
    assert "omnigent-pod-1-token" in fake_core.deleted_secrets
    assert "omnigent-pod-1-clone-cred" in fake_core.deleted_secrets
    assert fake_core.created_pods == []


def test_wait_failure_redacts_and_reaps_all_three(fake_core: _FakeCore) -> None:
    """A failed init clone reaps Pod + both Secrets; the log tail is scrubbed."""
    fake_core.read_queue = [
        _pod(phase="Pending", init_statuses=[_terminated(128, name="workspace-init")])
    ]
    fake_core.logs["workspace-init"] = "fatal: auth failed for tok-secret-value"
    with pytest.raises(click.ClickException) as excinfo:
        _start_with_clone(fake_core)
    assert "tok-secret-value" not in str(excinfo.value)
    assert "***" in str(excinfo.value)
    assert "tok-secret-value" not in "".join(traceback.format_exception(excinfo.value))
    assert "omnigent-pod-1" in fake_core.deleted_pods
    assert set(fake_core.deleted_secrets) == {
        "omnigent-pod-1-token",
        "omnigent-pod-1-clone-cred",
    }


def test_owner_ref_patch_failure_warns_and_continues(
    fake_core: _FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_core.patch_secret_error = _FakeApiException(status=403, reason="forbidden")
    workspace = _start_with_clone(fake_core)
    assert workspace.endswith("/repo")
    assert "could not set owner reference" in capsys.readouterr().err
    assert fake_core.deleted_secrets == ["omnigent-pod-1-clone-cred"]


def test_delete_after_running_failure_warns_not_fails(
    fake_core: _FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_core.delete_secret_errors = [_FakeApiException(status=500, reason="hiccup")]
    workspace = _start_with_clone(fake_core)
    assert workspace.endswith("/repo")
    assert "could not delete clone credential secret" in capsys.readouterr().err


def test_invalid_or_colliding_clone_env_keys_fail_before_api(fake_core: _FakeCore) -> None:
    for bad in ({"BAD-KEY": "v"}, {HOST_TOKEN_ENV_VAR: "v"}):
        with pytest.raises(click.ClickException):
            _launcher().start_host(
                "omnigent-pod-x",
                token=_TOKEN,
                host_id="h",
                host_name="m",
                server_url="http://srv.example.com",
                repo_url="https://forge.example/org/repo.git",
                repo_name="repo",
                clone_env=bad,
            )
    assert fake_core.calls == []


def test_no_clone_env_makes_zero_clone_secret_calls(fake_core: _FakeCore) -> None:
    fake_core.read_queue = [_pod(phase="Running")]
    _launcher().start_host(
        "omnigent-pod-1",
        token=_TOKEN,
        host_id="h",
        host_name="m",
        server_url="http://srv.example.com",
    )
    assert fake_core.calls.count("create_secret") == 1
    assert "patch_secret" not in fake_core.calls
    assert fake_core.deleted_secrets == []


def test_terminate_deletes_clone_secret_even_when_pod_delete_raises(
    fake_core: _FakeCore,
) -> None:
    fake_core.delete_pod_errors = [_FakeApiException(status=500, reason="boom")]
    with pytest.raises(click.ClickException):
        _launcher().terminate("omnigent-pod-1")
    assert "omnigent-pod-1-clone-cred" in fake_core.deleted_secrets


def test_launch_host_with_repo_returns_clone_dir(fake_core: _FakeCore) -> None:
    """With a repo, the returned workspace is the cloned directory under the workspace."""
    fake_core.read_queue = [_pod(phase="Running")]
    workspace = _launcher().start_host(
        "omnigent-pod-2",
        token=_TOKEN,
        host_id="host_2",
        host_name="managed-2",
        server_url="http://srv.example.com",
        repo_url="https://github.com/org/repo.git",
        repo_name="repo",
    )
    assert workspace == "/home/omnigent/workspace/repo"


def test_launch_host_cleans_up_on_create_failure(fake_core: _FakeCore) -> None:
    """A failed Pod create reaps the already-created token Secret and raises."""
    fake_core.create_pod_error = _FakeApiException(status=500, reason="Internal Server Error")
    with pytest.raises(click.ClickException, match="create sandbox pod"):
        _launcher().start_host(
            "omnigent-pod-3",
            token=_TOKEN,
            host_id="host_3",
            host_name="managed-3",
            server_url="http://srv.example.com",
        )
    assert "omnigent-pod-3-token" in fake_core.deleted_secrets
    assert "omnigent-pod-3" in fake_core.deleted_pods


def test_launch_host_fast_fails_on_clone_failure_with_log_tail(
    fake_core: _FakeCore,
) -> None:
    """A non-zero init container (clone failed) fails fast with the git error log tail."""
    fake_core.read_queue = [
        _pod(
            phase="Pending",
            init_statuses=[_terminated(128, name="workspace-prep")],
        )
    ]
    fake_core.logs["workspace-prep"] = "fatal: repository 'https://x/y.git' not found"
    with pytest.raises(click.ClickException) as exc:
        _launcher().start_host(
            "omnigent-pod-4",
            token=_TOKEN,
            host_id="host_4",
            host_name="managed-4",
            server_url="http://srv.example.com",
            repo_url="https://x/y.git",
            repo_name="y",
        )
    assert "workspace prep failed (exit 128" in exc.value.message
    assert "repository 'https://x/y.git' not found" in exc.value.message
    # The orphaned Pod + Secret are reaped.
    assert "omnigent-pod-4" in fake_core.deleted_pods
    assert "omnigent-pod-4-token" in fake_core.deleted_secrets


def test_launch_host_times_out_with_reason(
    fake_core: _FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Pod that never runs times out fast, surfacing the last waiting reason."""
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    fake_core.read_default = _pod(
        phase="Pending",
        container_statuses=[
            SimpleNamespace(
                name="host",
                state=SimpleNamespace(
                    waiting=SimpleNamespace(reason="ImagePullBackOff", message="back-off"),
                    terminated=None,
                ),
            )
        ],
    )
    with pytest.raises(click.ClickException, match="did not start within"):
        _launcher().start_host(
            "omnigent-pod-5",
            token=_TOKEN,
            host_id="host_5",
            host_name="managed-5",
            server_url="http://srv.example.com",
        )


def test_terminate_deletes_pod_and_secret(fake_core: _FakeCore) -> None:
    """Terminate deletes the Pod, its token Secret, and the derivable clone Secret."""
    _launcher().terminate("omnigent-pod-6")
    assert fake_core.deleted_pods == ["omnigent-pod-6"]
    assert fake_core.deleted_secrets == ["omnigent-pod-6-clone-cred", "omnigent-pod-6-token"]


def test_terminate_is_idempotent_on_404(fake_core: _FakeCore) -> None:
    """A Pod that no longer exists (404) is treated as success, and the Secrets too."""
    fake_core.delete_pod_errors = [_FakeApiException(status=404, reason="Not Found")]
    _launcher().terminate("omnigent-pod-7")  # must not raise
    assert fake_core.deleted_secrets == ["omnigent-pod-7-clone-cred", "omnigent-pod-7-token"]


def test_terminate_retries_transient_then_gives_up_best_effort(
    fake_core: _FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    """A persistent transient delete error is retried, then warned (not raised)."""
    from urllib3.exceptions import HTTPError

    fake_core.delete_pod_errors = [HTTPError("timeout")] * k8s._POD_DELETE_MAX_ATTEMPTS
    _launcher().terminate("omnigent-pod-8")  # best-effort: must not raise
    assert "could not delete Kubernetes pod 'omnigent-pod-8'" in capsys.readouterr().err
    # The token Secret delete still runs after the Pod gives up.
    assert fake_core.deleted_secrets == ["omnigent-pod-8-clone-cred", "omnigent-pod-8-token"]


def test_provision_reserves_pod_name_and_run_is_unsupported() -> None:
    """provision reserves a Pod name (no Pod created); run has no exec transport."""
    launcher = _launcher()
    # provision reserves the id — it does NOT create a Pod and does NOT raise.
    name = launcher.provision("managed-abc")
    assert name.startswith("omnigent-managed-abc-")
    # run is unsupported: the host is the Pod entrypoint, there is no exec-in.
    with pytest.raises(SandboxCapabilityError):
        launcher.run("sb", "echo hi")
