"""Tests for :mod:`omnigent.onboarding.sandboxes.kubernetes`."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field

import click
import pytest

from omnigent.onboarding.sandboxes import available_providers
from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.kubernetes import (
    HOST_IMAGE_ENV_VAR,
    NAMESPACE_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    SANDBOX_SECRET_ENV_VAR,
    SERVICE_ACCOUNT_ENV_VAR,
    KubernetesSandboxLauncher,
    _new_pod_name,
    _parse_exec_status,
    build_pod_manifest,
)

# ── PURE: build_pod_manifest ────────────────────────────────


def _manifest(
    *,
    pod_name: str = "omnigent-host-abc123",
    namespace: str = "omnigent",
    image: str = "ghcr.io/omnigent-ai/omnigent-host:latest",
    service_account: str = "omnigent-runner",
    harness_secret: str | None = None,
    env_literals: dict[str, str] | None = None,
    node_selector: dict[str, str] | None = None,
) -> dict[str, object]:
    """
    Build a manifest with sensible defaults, overridable per test.

    Mirrors ``build_pod_manifest``'s keyword signature so each override
    stays type-checked.

    :returns: The Pod manifest dict.
    """
    return build_pod_manifest(
        pod_name=pod_name,
        namespace=namespace,
        image=image,
        service_account=service_account,
        harness_secret=harness_secret,
        env_literals=env_literals or {},
        node_selector=node_selector,
    )


def _spec(manifest: dict[str, object]) -> dict[str, object]:
    """Return the manifest's ``spec`` block (typed for the asserts)."""
    spec = manifest["spec"]
    assert isinstance(spec, dict)
    return spec


def _container(manifest: dict[str, object]) -> dict[str, object]:
    """Return the manifest's sole container block."""
    containers = _spec(manifest)["containers"]
    assert isinstance(containers, list)
    container = containers[0]
    assert isinstance(container, dict)
    return container


def _node_selector(manifest: dict[str, object]) -> dict[str, object]:
    """Return the manifest's node selector mapping (typed for asserts)."""
    selector = _spec(manifest)["nodeSelector"]
    assert isinstance(selector, dict)
    return selector


def test_manifest_never_restarts_and_disables_token_automount() -> None:
    """
    A crashed host must not restart with a stale token (the managed
    machinery relaunches), and the sandbox must never carry the server
    SA token (codex M4) — a compromised agent could otherwise wield its
    pods/exec rights.
    """
    spec = _spec(_manifest())
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False


def test_manifest_sets_service_account() -> None:
    """The Pod runs as the resolved sandbox ServiceAccount."""
    spec = _spec(_manifest(service_account="custom-runner"))
    assert spec["serviceAccountName"] == "custom-runner"


def test_manifest_node_selector_pins_amd64_and_merges_operator_labels() -> None:
    """
    The host image is amd64-only, so arch is always pinned; operator
    node selector labels merge on top (a mixed-arch homelab needs both).
    """
    spec = _spec(_manifest(node_selector={"disktype": "ssd", "pool": "agents"}))
    assert spec["nodeSelector"] == {
        "kubernetes.io/arch": "amd64",
        "disktype": "ssd",
        "pool": "agents",
    }


def test_manifest_node_selector_defaults_arch_amd64() -> None:
    """
    ``kubernetes.io/arch: amd64`` is the default node selector entry (the
    host image is amd64-only). Operator-supplied labels merge on top per
    the spec's ``{arch, **operator}`` ordering, so an operator running a
    multi-arch image build CAN override arch deliberately — the default
    just spares everyone else from having to set it.
    """
    # No arch override: the default is present.
    assert _node_selector(_manifest())["kubernetes.io/arch"] == "amd64"
    # Operator override wins (spread last), matching the spec.
    override = _manifest(node_selector={"kubernetes.io/arch": "arm64"})
    assert _node_selector(override)["kubernetes.io/arch"] == "arm64"


def test_manifest_pod_security_context_runs_as_uid_gid_1000() -> None:
    """
    Least privilege: non-root uid/gid 1000 with fsGroup 1000 (so the
    HOME emptyDir is group-writable) and OnRootMismatch (skip a costly
    recursive chown when ownership already matches).
    """
    sec = _spec(_manifest())["securityContext"]
    assert sec == {
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "fsGroupChangePolicy": "OnRootMismatch",
    }


def test_manifest_writable_home_volume_mount_env_and_workingdir() -> None:
    """
    The image WORKDIR /root is unwritable to uid 1000, so the Pod must
    provide a writable HOME: an emptyDir at /home/omnigent, mounted into
    the container, exported as $HOME, and set as workingDir (codex M2 —
    _start_host_in_sandbox does `mkdir -p $HOME/workspace`).
    """
    manifest = _manifest()
    spec = _spec(manifest)
    container = _container(manifest)
    assert spec["volumes"] == [{"name": "home", "emptyDir": {}}]
    assert container["volumeMounts"] == [{"name": "home", "mountPath": "/home/omnigent"}]
    assert container["workingDir"] == "/home/omnigent"
    env = container["env"]
    assert isinstance(env, list)
    assert {"name": "HOME", "value": "/home/omnigent"} in env


def test_manifest_marks_is_sandbox() -> None:
    """IS_SANDBOX=1 lets in-sandbox code detect the managed sandbox."""
    env = _container(_manifest())["env"]
    assert isinstance(env, list)
    assert {"name": "IS_SANDBOX", "value": "1"} in env


def test_manifest_includes_env_literals() -> None:
    """
    Resolved server-env passthrough lands as literal container env
    entries (in addition to HOME / IS_SANDBOX).
    """
    env = _container(_manifest(env_literals={"OMNIGENT_GATEWAY_URL": "https://gw"}))["env"]
    assert isinstance(env, list)
    assert {"name": "OMNIGENT_GATEWAY_URL", "value": "https://gw"} in env


def test_manifest_envfrom_secret_ref_when_secret_set() -> None:
    """
    A configured Secret is projected via envFrom secretRef — this is how
    harness LLM credentials reach the Pod without living in the Pod spec.
    """
    container = _container(_manifest(harness_secret="omnigent-creds"))
    assert container["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]


def test_manifest_no_envfrom_key_when_secret_absent() -> None:
    """
    No configured Secret → the envFrom key is omitted entirely (an empty
    list would be harmless but the absent key is cleaner).
    """
    assert "envFrom" not in _container(_manifest(harness_secret=None))


def test_manifest_resources_from_sizing_constants() -> None:
    """Requests/limits come from the module sizing constants."""
    resources = _container(_manifest())["resources"]
    assert resources == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "4Gi"},
    }


def test_manifest_container_disallows_privilege_escalation_but_keeps_rw_root() -> None:
    """
    The container blocks privilege escalation but must NOT set
    readOnlyRootFilesystem — the host writes /tmp and ~/.omnigent.
    """
    sec = _container(_manifest())["securityContext"]
    assert sec == {"allowPrivilegeEscalation": False}
    assert isinstance(sec, dict)
    assert "readOnlyRootFilesystem" not in sec


def test_manifest_command_is_pid1_reaper_supervising_sleep_infinity() -> None:
    """
    PID 1 must reap orphaned runner procs the host re-parents to it, so
    the command is a python reaper that supervises `sleep infinity`
    (codex M3) — a bare `sleep infinity` would leak zombies.
    """
    command = _container(_manifest())["command"]
    assert isinstance(command, list)
    assert command[:2] == ["bash", "-lc"]
    reaper = command[2]
    assert "exec python3 -c " in reaper
    assert "sleep" in reaper and "infinity" in reaper
    # The reaper must actually reap (os.wait loop) and forward signals,
    # not just spawn-and-block.
    assert "os.wait" in reaper
    assert "SIGTERM" in reaper


def test_manifest_metadata_labels_and_namespace() -> None:
    """Pod metadata carries the managed-by / role labels and namespace."""
    manifest = _manifest(pod_name="omnigent-x-1", namespace="agents")
    metadata = manifest["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["name"] == "omnigent-x-1"
    assert metadata["namespace"] == "agents"
    assert metadata["labels"] == {
        "app.kubernetes.io/managed-by": "omnigent",
        "omnigent.ai/role": "sandbox-host",
    }


# ── PURE: _new_pod_name ─────────────────────────────────────


def test_new_pod_name_is_dns_label_safe() -> None:
    """
    Pod names must be DNS labels: lowercased, illegal chars collapsed to
    '-', and within the 63-char limit.
    """
    name = _new_pod_name("Managed_Session #42!!")
    assert name.startswith("omnigent-managed-session-42-")
    assert name == name.lower()
    assert len(name) <= 63
    # Only [a-z0-9-] survive.
    assert all(ch.isalnum() or ch == "-" for ch in name)


def test_new_pod_name_truncates_long_labels() -> None:
    """A very long label is truncated so the full name stays ≤ 63 chars."""
    name = _new_pod_name("x" * 200)
    assert len(name) <= 63


def test_new_pod_name_empty_label_falls_back_to_host() -> None:
    """A label with no usable characters falls back to 'host'."""
    name = _new_pod_name("!!!")
    assert name.startswith("omnigent-host-")


def test_new_pod_name_unique_suffix() -> None:
    """Two calls for the same label differ (the random suffix prevents
    collisions across relaunches)."""
    assert _new_pod_name("managed-a") != _new_pod_name("managed-a")


# ── PURE: _parse_exec_status ────────────────────────────────


def test_parse_exec_status_success_is_zero() -> None:
    """A Success status frame means exit 0."""
    frame = '{"metadata":{},"status":"Success"}'
    assert _parse_exec_status([frame], "pod-1") == 0


def test_parse_exec_status_reads_exit_code_cause() -> None:
    """
    A non-zero exit carries the code in a details.causes ExitCode entry —
    WSClient.returncode is unreliable, so this frame is the truth
    (codex M1).
    """
    frame = (
        '{"metadata":{},"status":"Failure",'
        '"reason":"NonZeroExitCode",'
        '"details":{"causes":[{"reason":"ExitCode","message":"7"}]}}'
    )
    assert _parse_exec_status([frame], "pod-1") == 7


def test_parse_exec_status_joins_split_frames() -> None:
    """The status frame can arrive in chunks; they must be joined first."""
    chunks = ['{"metadata":{},"status":', '"Success"}']
    assert _parse_exec_status(chunks, "pod-1") == 0


def test_parse_exec_status_raises_when_no_frame() -> None:
    """No status frame is a transport fault — must raise, not pass as 0."""
    with pytest.raises(RuntimeError, match="no status frame"):
        _parse_exec_status([], "pod-1")


def test_parse_exec_status_raises_when_no_exit_code() -> None:
    """
    A failure frame without an ExitCode cause carries no usable code —
    raise rather than invent a status.
    """
    frame = '{"metadata":{},"status":"Failure","reason":"InternalError"}'
    with pytest.raises(RuntimeError, match="no exit code"):
        _parse_exec_status([frame], "pod-1")


# ── Fake Kubernetes client ──────────────────────────────────
#
# The kubernetes client is an optional dependency the base test env does
# not install, and real cluster objects only exist server-side anyway —
# so these are hand-rolled stub classes (never MagicMock: the launcher's
# attribute access must hit explicitly defined recorders, not silently
# succeed). The fake package + submodules are injected via sys.modules so
# the launcher's function-local `from kubernetes ... import` resolves to
# them.


class _FakeApiException(Exception):
    """Stands in for ``kubernetes.client.rest.ApiException``."""

    def __init__(
        self, *, status: int = 500, reason: str = "error", body: str | None = None
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.body = body


class _FakeConfigException(Exception):
    """Stands in for ``kubernetes.config.config_exception.ConfigException``."""


# ── status object stand-ins (mirror V1Pod's attribute shape) ──


@dataclass
class _Waiting:
    """Stands in for ``V1ContainerStateWaiting``."""

    reason: str | None = None
    message: str | None = None


@dataclass
class _ContainerState:
    """Stands in for ``V1ContainerState`` (only `waiting` is read)."""

    waiting: _Waiting | None = None


@dataclass
class _ContainerStatus:
    """Stands in for ``V1ContainerStatus``."""

    ready: bool = False
    state: _ContainerState = field(default_factory=_ContainerState)


@dataclass
class _Condition:
    """Stands in for ``V1PodCondition``."""

    type: str
    status: str
    reason: str | None = None
    message: str | None = None


@dataclass
class _PodStatus:
    """Stands in for ``V1PodStatus``."""

    phase: str | None = None
    container_statuses: list[_ContainerStatus] | None = None
    conditions: list[_Condition] | None = None


@dataclass
class _Pod:
    """Stands in for a ``V1Pod`` (only `status` is read)."""

    status: _PodStatus


def _ready_pod(_name: str) -> _Pod:
    """A Pod that is Running with a ready container (the happy path)."""
    return _Pod(
        status=_PodStatus(
            phase="Running",
            container_statuses=[_ContainerStatus(ready=True)],
        )
    )


@dataclass
class _Event:
    """Stands in for ``CoreV1Event``."""

    reason: str
    message: str


@dataclass
class _EventList:
    """Stands in for ``CoreV1EventList``."""

    items: list[_Event] = field(default_factory=list)


@dataclass
class _CreateCall:
    """One recorded ``create_namespaced_pod`` invocation."""

    namespace: str
    manifest: dict[str, object]


@dataclass
class _ExecCall:
    """One recorded exec invocation."""

    pod: str
    namespace: str
    command: list[str]


class _FakeWSClient:
    """
    Canned stand-in for the exec ``WSClient``.

    Serves one frame of channel data per ``is_open()``/``update`` cycle,
    then closes — mirroring the real read loop the launcher drives.

    :param channels: Per-channel text the stream delivers (channel id →
        text), e.g. ``{1: "out", 3: status_frame}``.
    """

    def __init__(self, channels: dict[int, str]) -> None:
        self._pending = dict(channels)
        self._open = True
        self.closed = False

    def is_open(self) -> bool:
        """Open until every channel has been read out."""
        return self._open

    def update(self, timeout: float = 0) -> None:
        """No-op: the canned channels are already buffered."""
        del timeout

    def read_channel(self, channel: int, timeout: float = 0) -> str:
        """Pop a channel's buffered text, then close once all drained."""
        del timeout
        data = self._pending.pop(channel, "")
        if not self._pending:
            self._open = False
        return data

    def close(self, **kwargs: object) -> None:
        """Record the teardown."""
        del kwargs
        self.closed = True


class _FakeCoreV1Api:
    """Recording stand-in for ``CoreV1Api`` bound to one fake config."""

    def __init__(self, state: _FakeK8sState) -> None:
        self._state = state

    def create_namespaced_pod(self, namespace: str, body: dict[str, object]) -> object:
        """Record creation and register the resulting Pod."""
        if self._state.create_raises:
            raise self._state.create_raises.pop(0)
        metadata = body["metadata"]
        assert isinstance(metadata, dict)
        pod_name = metadata["name"]
        assert isinstance(pod_name, str)
        self._state.create_calls.append(_CreateCall(namespace=namespace, manifest=body))
        self._state.pods[pod_name] = self._state.pod_factory(pod_name)
        return object()

    def read_namespaced_pod(self, name: str, namespace: str) -> _Pod:
        """
        Resolve a registered Pod or raise the fake 404.

        When ``read_sequence`` is set, successive reads walk it (staying
        on the last element once exhausted) — used to model a Pod that
        becomes ready only after a few polls.
        """
        del namespace
        if self._state.read_sequence:
            index = min(self._state.read_index, len(self._state.read_sequence) - 1)
            self._state.read_index += 1
            return self._state.read_sequence[index]
        pod = self._state.pods.get(name)
        if pod is None:
            raise _FakeApiException(status=404, reason="Not Found")
        return pod

    def list_namespaced_event(self, namespace: str, *, field_selector: str) -> _EventList:
        """Return canned events for the pod-ready failure surface."""
        del namespace, field_selector
        return _EventList(items=list(self._state.events))

    def delete_namespaced_pod(
        self, name: str, namespace: str, *, grace_period_seconds: int
    ) -> object:
        """Record the deletion (with grace period) or raise as configured."""
        del namespace
        self._state.delete_calls.append((name, grace_period_seconds))
        if self._state.delete_raises:
            raise self._state.delete_raises.pop(0)
        self._state.pods.pop(name, None)
        return object()

    def connect_get_namespaced_pod_exec(self, *args: object, **kwargs: object) -> str:
        """Sentinel: ``stream`` intercepts this method, never calls it."""
        raise AssertionError("connect_get_namespaced_pod_exec must go through stream()")


@dataclass
class _FakeK8sState:
    """
    Recorder the fake client package writes into.

    :param create_calls: Every ``create_namespaced_pod`` invocation.
    :param delete_calls: ``(pod_name, grace_period_seconds)`` per delete.
    :param exec_calls: Every exec invocation (via the fake ``stream``).
    :param pods: Registered Pods by name (``read`` resolves here).
    :param events: Canned events the failure surface reports.
    :param create_raises: Exceptions successive creates raise (popped
        front-first) before succeeding.
    :param delete_raises: Exceptions successive deletes raise.
    :param exec_channels: Per-channel text the next exec stream serves.
    :param exec_raises: Exception ``stream`` raises instead of returning
        a WSClient (models a pod-deleted-mid-run ApiException).
    :param ws_clients: Every WSClient ``stream`` handed back (for the
        websocket-close assertion).
    :param incluster_raises: Whether ``load_incluster_config`` raises the
        fake ConfigException (models running off-cluster).
    :param kubeconfig_raises: Whether ``load_kube_config`` raises the fake
        ConfigException (models no kubeconfig available either).
    :param incluster_calls / kubeconfig_calls: Recorded config loads (the
        Configuration each was handed, for the isolation assert).
    :param pod_factory: Builds the Pod registered on create — defaults to
        an immediately-ready Pod; tests override for fast-fail cases.
    :param read_sequence: When set, successive ``read_namespaced_pod``
        calls walk this list (models a Pod that becomes ready after a few
        polls); empty falls back to the registered-pods lookup.
    :param read_index: Cursor into ``read_sequence``.
    :param configurations: Every ``Configuration()`` constructed.
    """

    create_calls: list[_CreateCall] = field(default_factory=list)
    delete_calls: list[tuple[str, int]] = field(default_factory=list)
    exec_calls: list[_ExecCall] = field(default_factory=list)
    pods: dict[str, _Pod] = field(default_factory=dict)
    events: list[_Event] = field(default_factory=list)
    create_raises: list[Exception] = field(default_factory=list)
    delete_raises: list[Exception] = field(default_factory=list)
    exec_channels: dict[int, str] = field(default_factory=dict)
    exec_raises: Exception | None = None
    ws_clients: list[_FakeWSClient] = field(default_factory=list)
    incluster_raises: bool = False
    kubeconfig_raises: bool = False
    incluster_calls: list[object] = field(default_factory=list)
    kubeconfig_calls: list[tuple[str | None, object]] = field(default_factory=list)
    configurations: list[object] = field(default_factory=list)
    pod_factory: Callable[[str], _Pod] = _ready_pod
    read_sequence: list[_Pod] = field(default_factory=list)
    read_index: int = 0


class _FakeConfiguration:
    """Stands in for ``client.Configuration`` (an isolated config bag)."""


class _FakeApiClient:
    """Stands in for ``client.ApiClient`` — records the config it wraps."""

    def __init__(self, configuration: object) -> None:
        self.configuration = configuration


def _install_fake_kubernetes(
    monkeypatch: pytest.MonkeyPatch, state: _FakeK8sState
) -> _FakeK8sState:
    """
    Inject a fake ``kubernetes`` package (with the submodules the
    launcher imports) into ``sys.modules``.

    :param monkeypatch: pytest monkeypatch (restores sys.modules after
        the test).
    :param state: The recorder the fakes write into.
    :returns: The same *state*, for convenience.
    """

    def _make_configuration() -> _FakeConfiguration:
        cfg = _FakeConfiguration()
        state.configurations.append(cfg)
        return cfg

    def _make_api_client(configuration: object) -> _FakeApiClient:
        return _FakeApiClient(configuration)

    def _make_core(api_client: object) -> _FakeCoreV1Api:
        del api_client
        return _FakeCoreV1Api(state)

    def _load_incluster(*, client_configuration: object, **_kw: object) -> None:
        state.incluster_calls.append(client_configuration)
        if state.incluster_raises:
            raise _FakeConfigException("no service account token")

    def _load_kubeconfig(
        *, config_file: str | None = None, client_configuration: object, **_kw: object
    ) -> None:
        state.kubeconfig_calls.append((config_file, client_configuration))
        if state.kubeconfig_raises:
            raise _FakeConfigException("no kubeconfig")

    def _stream(api_method: object, *args: object, **kwargs: object) -> _FakeWSClient:
        del api_method
        if state.exec_raises is not None:
            raise state.exec_raises
        pod = str(args[0])
        namespace = str(args[1])
        command = kwargs["command"]
        assert isinstance(command, list)
        state.exec_calls.append(_ExecCall(pod=pod, namespace=namespace, command=list(command)))
        ws = _FakeWSClient(state.exec_channels)
        state.ws_clients.append(ws)
        return ws

    # kubernetes.client (+ rest submodule)
    client_mod = types.ModuleType("kubernetes.client")
    client_mod.Configuration = _make_configuration  # type: ignore[attr-defined]
    client_mod.ApiClient = _make_api_client  # type: ignore[attr-defined]
    client_mod.CoreV1Api = _make_core  # type: ignore[attr-defined]
    client_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    rest_mod = types.ModuleType("kubernetes.client.rest")
    rest_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    client_mod.rest = rest_mod  # type: ignore[attr-defined]

    # kubernetes.config (+ config_exception submodule)
    config_mod = types.ModuleType("kubernetes.config")
    config_mod.ConfigException = _FakeConfigException  # type: ignore[attr-defined]
    config_mod.load_incluster_config = _load_incluster  # type: ignore[attr-defined]
    config_mod.load_kube_config = _load_kubeconfig  # type: ignore[attr-defined]
    config_exc_mod = types.ModuleType("kubernetes.config.config_exception")
    config_exc_mod.ConfigException = _FakeConfigException  # type: ignore[attr-defined]
    config_mod.config_exception = config_exc_mod  # type: ignore[attr-defined]

    # kubernetes.stream (+ ws_client submodule with channel constants)
    stream_mod = types.ModuleType("kubernetes.stream")
    stream_mod.stream = _stream  # type: ignore[attr-defined]
    ws_client_mod = types.ModuleType("kubernetes.stream.ws_client")
    ws_client_mod.STDOUT_CHANNEL = 1  # type: ignore[attr-defined]
    ws_client_mod.STDERR_CHANNEL = 2  # type: ignore[attr-defined]
    ws_client_mod.ERROR_CHANNEL = 3  # type: ignore[attr-defined]
    stream_mod.ws_client = ws_client_mod  # type: ignore[attr-defined]

    # kubernetes (top-level package tying the submodules together)
    pkg = types.ModuleType("kubernetes")
    pkg.client = client_mod  # type: ignore[attr-defined]
    pkg.config = config_mod  # type: ignore[attr-defined]
    pkg.stream = stream_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "kubernetes", pkg)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.config", config_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.config.config_exception", config_exc_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.stream", stream_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.stream.ws_client", ws_client_mod)
    return state


@pytest.fixture()
def fake_k8s(monkeypatch: pytest.MonkeyPatch) -> _FakeK8sState:
    """
    Install the fake client with a clean environment.

    A developer's ambient overrides must not leak into the default
    assertions.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: The fake's recorder state.
    """
    for var in (
        HOST_IMAGE_ENV_VAR,
        NAMESPACE_ENV_VAR,
        SANDBOX_SECRET_ENV_VAR,
        SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
        SERVICE_ACCOUNT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    return _install_fake_kubernetes(monkeypatch, _FakeK8sState())


# ── prepare ─────────────────────────────────────────────────


def test_prepare_raises_with_install_hint_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without the optional client, prepare must fail with the exact extras
    install hint, not a raw ImportError. ``sys.modules[name] = None``
    makes ``import kubernetes`` raise ImportError.
    """
    monkeypatch.setitem(sys.modules, "kubernetes", None)
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().prepare()
    assert "omnigent[kubernetes]" in str(exc.value)


def test_prepare_loads_cluster_config(fake_k8s: _FakeK8sState) -> None:
    """A reachable cluster (in-cluster config loads) passes preflight."""
    KubernetesSandboxLauncher().prepare()
    # In-cluster was tried (and succeeded), so kubeconfig was untouched.
    assert len(fake_k8s.incluster_calls) == 1
    assert fake_k8s.kubeconfig_calls == []


def test_prepare_falls_back_to_kubeconfig_off_cluster(fake_k8s: _FakeK8sState) -> None:
    """
    Off-cluster (no SA token → ConfigException), prepare falls back to
    kubeconfig instead of failing (codex S3 fallback).
    """
    fake_k8s.incluster_raises = True
    KubernetesSandboxLauncher().prepare()
    assert len(fake_k8s.incluster_calls) == 1
    assert len(fake_k8s.kubeconfig_calls) == 1


def test_config_is_loaded_into_isolated_configuration(fake_k8s: _FakeK8sState) -> None:
    """
    Config must load into a fresh Configuration wired through ApiClient,
    never the library's global default (codex S3): the same Configuration
    instance the loader received is the one ApiClient wraps.
    """
    KubernetesSandboxLauncher().prepare()
    assert len(fake_k8s.configurations) == 1
    loaded_into = fake_k8s.incluster_calls[0]
    assert loaded_into is fake_k8s.configurations[0]


def test_prepare_honors_explicit_kubeconfig_path(fake_k8s: _FakeK8sState) -> None:
    """in_cluster=False routes straight to kubeconfig with the given path."""
    KubernetesSandboxLauncher(in_cluster=False, kubeconfig="/tmp/kc").prepare()
    assert fake_k8s.incluster_calls == []
    assert fake_k8s.kubeconfig_calls[0][0] == "/tmp/kc"


def test_prepare_wraps_config_failure_with_remediation(fake_k8s: _FakeK8sState) -> None:
    """
    No usable config (in-cluster fails AND no kubeconfig) surfaces a
    remediation naming both paths, not a raw ConfigException.
    """
    fake_k8s.incluster_raises = True
    fake_k8s.kubeconfig_raises = True

    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().prepare()
    assert "KUBECONFIG" in str(exc.value)


# ── provision ───────────────────────────────────────────────


def test_provision_creates_pod_and_returns_name(fake_k8s: _FakeK8sState) -> None:
    """
    Provision creates one Pod from the default image in the default
    namespace and returns the generated (DNS-safe) Pod name.
    """
    pod_name = KubernetesSandboxLauncher().provision("managed-abc")

    assert pod_name.startswith("omnigent-managed-abc-")
    [create] = fake_k8s.create_calls
    assert create.namespace == "omnigent"
    assert _container(create.manifest)["image"] == DEFAULT_HOST_IMAGE
    # The created Pod is the one returned.
    assert pod_name in fake_k8s.pods


def test_provision_waits_for_readiness_before_returning(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Provision must not return until the container is ready (codex S2):
    a Pod that is Running but not-yet-ready, then flips ready, resolves
    the wait without error (rather than execing into a not-yet-up host).
    """
    # No real poll sleeps in tests.
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    pending = _Pod(
        status=_PodStatus(phase="Running", container_statuses=[_ContainerStatus(ready=False)])
    )
    # Two not-ready reads, then ready — the wait must keep polling.
    fake_k8s.read_sequence = [pending, pending, _ready_pod("x")]

    pod_name = KubernetesSandboxLauncher().provision("managed-abc")

    assert len(fake_k8s.create_calls) == 1
    # The wait polled past the not-ready reads (≥3 reads consumed).
    assert fake_k8s.read_index >= 3
    assert pod_name.startswith("omnigent-managed-abc-")


def test_provision_fast_fails_on_image_pull_backoff(fake_k8s: _FakeK8sState) -> None:
    """
    A container stuck in ImagePullBackOff will never start — fail fast
    with the reason (and a describe hint), not after the full budget.
    """

    def _bad_image(name: str) -> _Pod:
        return _Pod(
            status=_PodStatus(
                phase="Pending",
                container_statuses=[
                    _ContainerStatus(
                        ready=False,
                        state=_ContainerState(
                            waiting=_Waiting(
                                reason="ImagePullBackOff", message="back-off pulling image"
                            )
                        ),
                    )
                ],
            )
        )

    fake_k8s.pod_factory = _bad_image
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    assert "ImagePullBackOff" in str(exc.value)
    assert "kubectl describe pod" in str(exc.value)


def test_provision_fast_fails_on_unschedulable_and_surfaces_events(
    fake_k8s: _FakeK8sState,
) -> None:
    """
    An Unschedulable Pod fails fast, and the error surfaces the
    scheduler's event (the operator-actionable reason).
    """

    def _unschedulable(name: str) -> _Pod:
        return _Pod(
            status=_PodStatus(
                phase="Pending",
                container_statuses=[_ContainerStatus(ready=False)],
                conditions=[
                    _Condition(
                        type="PodScheduled",
                        status="False",
                        reason="Unschedulable",
                        message="0/3 nodes are available: insufficient cpu",
                    )
                ],
            )
        )

    fake_k8s.pod_factory = _unschedulable
    fake_k8s.events = [_Event(reason="FailedScheduling", message="insufficient cpu")]
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    message = str(exc.value)
    assert "cannot be scheduled" in message
    assert "FailedScheduling" in message


def test_provision_fast_fails_on_terminal_phase(fake_k8s: _FakeK8sState) -> None:
    """A Pod that lands in a terminal Failed phase fails fast."""

    def _failed(name: str) -> _Pod:
        return _Pod(status=_PodStatus(phase="Failed"))

    fake_k8s.pod_factory = _failed
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    assert "terminal phase 'Failed'" in str(exc.value)


def test_provision_image_resolution_order(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Explicit constructor image wins over the env override, which wins
    over the default — the precedence the server's
    ``sandbox.kubernetes.image`` config relies on.
    """
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "ghcr.io/env/host:1")

    KubernetesSandboxLauncher(image="ghcr.io/explicit/host:2").provision("a")
    KubernetesSandboxLauncher().provision("b")

    first, second = fake_k8s.create_calls
    assert _container(first.manifest)["image"] == "ghcr.io/explicit/host:2"
    assert _container(second.manifest)["image"] == "ghcr.io/env/host:1"


def test_provision_resolves_namespace_secret_and_service_account(
    fake_k8s: _FakeK8sState,
) -> None:
    """
    Constructor namespace / secret_name / service_account thread into the
    created Pod (the managed-host config's path to a custom deployment).
    """
    KubernetesSandboxLauncher(
        namespace="agents",
        secret_name="omnigent-creds",
        service_account="custom-runner",
    ).provision("a")

    [create] = fake_k8s.create_calls
    assert create.namespace == "agents"
    assert _spec(create.manifest)["serviceAccountName"] == "custom-runner"
    assert _container(create.manifest)["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]


def test_provision_env_passthrough_resolves_from_server_env(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Constructor env NAMES resolve to values from the server process
    environment at provision time (config carries names only).
    """
    monkeypatch.setenv("OMNIGENT_GATEWAY_URL", "https://gw")

    KubernetesSandboxLauncher(env=["OMNIGENT_GATEWAY_URL"]).provision("a")

    [create] = fake_k8s.create_calls
    env = _container(create.manifest)["env"]
    assert isinstance(env, list)
    assert {"name": "OMNIGENT_GATEWAY_URL", "value": "https://gw"} in env


def test_provision_env_passthrough_missing_var_fails_loud(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A configured name unset in the server environment is an operator
    error — fail loud and create nothing (it would otherwise surface
    later as an opaque in-sandbox failure).
    """
    monkeypatch.delenv("OMNIGENT_GATEWAY_URL", raising=False)

    with pytest.raises(click.ClickException, match="OMNIGENT_GATEWAY_URL"):
        KubernetesSandboxLauncher(env=["OMNIGENT_GATEWAY_URL"]).provision("a")
    assert fake_k8s.create_calls == []


def test_provision_regenerates_name_on_conflict(fake_k8s: _FakeK8sState) -> None:
    """
    A 409 name collision (two launches raced the same slug) is recovered
    once with a fresh suffix, rather than failing the whole launch.
    """
    fake_k8s.create_raises = [_FakeApiException(status=409, reason="AlreadyExists")]

    pod_name = KubernetesSandboxLauncher().provision("a")

    # First create conflicted; the retry created the (different) Pod.
    assert len(fake_k8s.create_calls) == 1
    assert pod_name in fake_k8s.pods


def test_provision_wraps_api_errors_with_reason_and_rbac_hint(
    fake_k8s: _FakeK8sState,
) -> None:
    """
    A 403 (the server SA lacks the sandbox-manager Role) surfaces the
    API reason AND an RBAC remediation — the most common misconfig.
    """
    fake_k8s.create_raises = [
        _FakeApiException(status=403, reason="Forbidden", body="pods is forbidden")
    ]

    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("a")
    message = str(exc.value)
    assert "Forbidden" in message
    assert "pods is forbidden" in message
    assert "rbac.yaml" in message


# ── run ─────────────────────────────────────────────────────


def _provisioned(fake_k8s: _FakeK8sState) -> tuple[KubernetesSandboxLauncher, str]:
    """Provision a launcher and return it with the created Pod name."""
    launcher = KubernetesSandboxLauncher()
    pod_name = launcher.provision("a")
    return launcher, pod_name


def test_run_execs_bash_lc_and_parses_returncode(fake_k8s: _FakeK8sState) -> None:
    """
    ``run`` execs via ``bash -lc`` (codex S5, for the login-shell venv
    PATH) and returns the exit code parsed from the STATUS frame plus the
    captured streams.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {
        1: "remote-out\n",
        2: "remote-err\n",
        3: '{"metadata":{},"status":"Success"}',
    }

    result = launcher.run(pod_name, "echo hi")

    [call] = fake_k8s.exec_calls
    assert call.command == ["bash", "-lc", "echo hi"]
    assert call.pod == pod_name
    assert result.returncode == 0
    assert result.stdout == "remote-out\n"
    assert result.stderr == "remote-err\n"


def test_run_parses_nonzero_exit_and_raises_when_checked(fake_k8s: _FakeK8sState) -> None:
    """
    A non-zero STATUS frame yields the real code; check=True (the managed
    default) raises with the command named.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {
        3: (
            '{"metadata":{},"status":"Failure",'
            '"details":{"causes":[{"reason":"ExitCode","message":"3"}]}}'
        ),
    }

    with pytest.raises(click.ClickException, match="exit 3"):
        launcher.run(pod_name, "false")


def test_run_nonzero_returns_when_unchecked(fake_k8s: _FakeK8sState) -> None:
    """check=False returns the failing result for the caller to inspect."""
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {
        2: "boom\n",
        3: (
            '{"metadata":{},"status":"Failure",'
            '"details":{"causes":[{"reason":"ExitCode","message":"1"}]}}'
        ),
    }

    result = launcher.run(pod_name, "false", check=False)
    assert result.returncode == 1
    assert result.stderr == "boom\n"


def test_run_closes_websocket(fake_k8s: _FakeK8sState) -> None:
    """The exec websocket is always closed (no leaked connections)."""
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {3: '{"metadata":{},"status":"Success"}'}

    launcher.run(pod_name, "true")

    [ws] = fake_k8s.ws_clients
    assert ws.closed is True


def test_run_wraps_api_error(fake_k8s: _FakeK8sState) -> None:
    """
    An exec ApiException (e.g. the Pod was deleted mid-run) surfaces the
    API reason through the launcher contract, not a raw client error.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_raises = _FakeApiException(status=404, reason="Not Found")

    with pytest.raises(click.ClickException, match="Not Found"):
        launcher.run(pod_name, "true")


def test_run_raises_when_status_frame_missing(fake_k8s: _FakeK8sState) -> None:
    """
    An exec that yields no STATUS frame is a transport fault — raise (do
    not silently report success).
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {1: "some output\n"}

    with pytest.raises(click.ClickException, match="no status frame"):
        launcher.run(pod_name, "true")


# ── terminate ───────────────────────────────────────────────


def test_terminate_deletes_with_zero_grace_period(fake_k8s: _FakeK8sState) -> None:
    """Terminate deletes the Pod with grace_period_seconds=0 (prompt)."""
    launcher, pod_name = _provisioned(fake_k8s)

    launcher.terminate(pod_name)

    assert fake_k8s.delete_calls == [(pod_name, 0)]
    assert pod_name not in fake_k8s.pods


def test_terminate_is_idempotent_on_404(fake_k8s: _FakeK8sState) -> None:
    """
    Deleting an already-gone Pod (404) is a no-op success — cleanup
    paths race the provider's own deletion.
    """
    launcher = KubernetesSandboxLauncher()
    fake_k8s.delete_raises = [_FakeApiException(status=404, reason="Not Found")]

    launcher.terminate("omnigent-gone-1")  # must not raise


def test_terminate_wraps_non_404_errors(fake_k8s: _FakeK8sState) -> None:
    """A non-404 delete failure surfaces the API reason."""
    launcher = KubernetesSandboxLauncher()
    fake_k8s.delete_raises = [_FakeApiException(status=500, reason="ServerError")]

    with pytest.raises(click.ClickException, match="ServerError"):
        launcher.terminate("omnigent-x-1")


# ── registration ────────────────────────────────────────────


def test_available_providers_includes_kubernetes() -> None:
    """
    The provider is registered (its module exists in the build), so
    ``available_providers`` lists it — gating the CLI/config on its
    presence.
    """
    assert "kubernetes" in available_providers()
