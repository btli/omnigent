"""
Kubernetes sandbox launcher.

Implements the managed-launch subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for an
agent-runner Pod spawned on demand in a Kubernetes cluster. This module
ships in the OSS build; the official ``kubernetes`` Python client is an
optional dependency (``pip install 'omnigent[kubernetes]'``) imported
lazily, so the provider can be listed and the module probed without it.

The model is **Option A** (sleep-infinity + ``pods/exec``): ``provision``
creates a Pod that boots ``sleep infinity`` under a tiny PID-1 reaper and
waits for its container to become *ready*; the server then execs into it
(:meth:`KubernetesSandboxLauncher.run`) to start ``omnigent host``, which
dials back over the existing managed launch-token tunnel. The shared
``_start_host_in_sandbox`` orchestration is reused unchanged — the reason
Option A was chosen — so this launcher implements only
``prepare`` / ``provision`` / ``run`` / ``terminate``.

Platform notes that shape this launcher:

- **Writable HOME.** The host image's WORKDIR is ``/root`` (root-owned),
  but the Pod runs as uid 1000 for least privilege, so ``$HOME`` would
  be unwritable. The Pod therefore exposes a writable HOME: ``HOME`` is
  set to :data:`_HOME_DIR`, an ``emptyDir`` is mounted there,
  ``fsGroup`` 1000 makes it group-writable, and ``workingDir`` points at
  it. ``_start_host_in_sandbox`` (unchanged) reads ``$HOME`` and creates
  ``$HOME/workspace`` inside it.
- **PID-1 reaper.** A bare ``sleep infinity`` as PID 1 has no zombie
  reaper, but the in-sandbox host re-parents orphaned runner processes
  to PID 1. The Pod ``command`` is therefore a tiny supervisor that
  spawns ``sleep infinity``, reaps any children, and forwards SIGTERM
  for prompt, graceful termination.
- **Least privilege.** ``automountServiceAccountToken: false`` keeps the
  server ServiceAccount's ``pods/exec`` rights out of the sandbox, the
  Pod runs as a non-root user, and the container disables privilege
  escalation. The root filesystem stays writable (the host writes
  ``/tmp`` and ``~/.omnigent``).
- **No local port forwarding.** Like Modal/Daytona/Islo, the launcher
  exists for server-managed hosts only, so ``supports_cli_bootstrap`` /
  ``supports_local_port_forward`` stay ``False``.
- **Credentials via Secret.** Harness LLM credentials are attached as an
  ``envFrom`` reference to a pre-created Kubernetes Secret (named by
  ``sandbox.kubernetes.secret_name`` / :data:`SANDBOX_SECRET_ENV_VAR`),
  so secret values never transit the server config file. A small set of
  non-secret server-env values may additionally be injected by name via
  :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
"""

from __future__ import annotations

import importlib
import os
import re
import shlex
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

import click
import yaml

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
)

if TYPE_CHECKING:
    from kubernetes import client as k8s_client


# ── Constants ──────────────────────────────────────────

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_KUBERNETES_HOST_IMAGE"
"""Environment variable overriding
:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` for
Kubernetes sandbox Pods, e.g. an org-internal copy of the host image
(``ghcr.io/<your-org>/omnigent-host:latest``). amd64-only."""

NAMESPACE_ENV_VAR: str = "OMNIGENT_KUBERNETES_NAMESPACE"
"""Environment variable naming the namespace sandbox Pods are created in.
Defaults to :data:`_DEFAULT_NAMESPACE`. The server's managed-host
``sandbox.kubernetes.namespace`` config takes precedence when set."""

SANDBOX_SECRET_ENV_VAR: str = "OMNIGENT_KUBERNETES_SECRET"
"""Environment variable naming a pre-created Kubernetes ``Secret`` whose
keys are projected into every sandbox Pod's environment via ``envFrom``
— typically the harness LLM credentials (``ANTHROPIC_API_KEY``,
``OPENAI_API_KEY``, …) and ``GIT_TOKEN`` the in-sandbox host forwards to
runners. Unset means no Secret is attached. The server's managed-host
``sandbox.kubernetes.secret_name`` config takes precedence when set."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_KUBERNETES_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the SERVER-process
environment variables whose values are injected as literal ``env`` into
every sandbox Pod this launcher creates. Names, not values: the values
are read from the server's own environment at provision time, so they
never live in config files. Prefer :data:`SANDBOX_SECRET_ENV_VAR` for
actual credentials (Secret keys are not stored in the Pod spec); this is
for non-secret config a deployment wants threaded through. The server's
managed-host ``sandbox.kubernetes.env`` config takes precedence when
set."""

SERVICE_ACCOUNT_ENV_VAR: str = "OMNIGENT_KUBERNETES_SERVICE_ACCOUNT"
"""Environment variable naming the ServiceAccount sandbox Pods run as.
Defaults to :data:`_DEFAULT_SERVICE_ACCOUNT`. The sandbox SA needs no
API access (token automounting is disabled); it exists so cluster RBAC
can target sandbox Pods distinctly from the server. The server's
managed-host ``sandbox.kubernetes.service_account`` config takes
precedence when set."""

KUBECONFIG_ENV_VAR: str = "OMNIGENT_KUBERNETES_KUBECONFIG"
"""Environment variable naming an explicit kubeconfig file path for the
out-of-cluster fallback. Unset falls back to the ambient
``KUBECONFIG`` / ``~/.kube/config`` resolution. Ignored when the
launcher loads in-cluster ServiceAccount config (the primary path)."""

# Default namespace / ServiceAccount, matching deploy/kubernetes/.
_DEFAULT_NAMESPACE: str = "omnigent"
_DEFAULT_SERVICE_ACCOUNT: str = "omnigent-runner"

# Pod resource sizing. Matches the other launchers' 2 vCPU / 4 GiB
# ceiling (enough for a host running one interactive session); a low
# request keeps the Pod schedulable on modest homelab nodes while the
# limit caps a runaway runner.
_SANDBOX_CPU_REQUEST: str = "500m"
_SANDBOX_CPU_LIMIT: str = "2"
_SANDBOX_MEMORY_REQUEST: str = "1Gi"
_SANDBOX_MEMORY_LIMIT: str = "4Gi"

# Non-root identity the Pod runs as. uid/gid 1000 is the conventional
# first non-system user; fsGroup makes the HOME emptyDir group-writable.
_RUN_AS_UID: int = 1000
_RUN_AS_GID: int = 1000

# Writable HOME for the uid-1000 Pod (the image's /root is unwritable to
# it). Mounted as an emptyDir and exported as $HOME / workingDir.
_HOME_DIR: str = "/home/omnigent"

# Pod-ready wait budget. Consumed inside provision() BEFORE the shared
# _wait_for_host_online 120s poll, so it is kept tight; transient image
# pulls on a cold node are the usual reason a Pod takes the full window.
_POD_READY_TIMEOUT_S: int = 90
_POD_READY_POLL_S: float = 2.0

# Container ``waiting.reason`` values that will never resolve on their
# own — fail the ready wait immediately rather than burning the budget.
_FATAL_WAITING_REASONS: frozenset[str] = frozenset(
    {
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "CreateContainerConfigError",
    }
)

# PID-1 reaper run as the Pod's entrypoint (codex M3). It spawns
# ``sleep infinity`` as a child, forwards SIGTERM/SIGINT to it for prompt
# graceful shutdown, and loops os.wait() to reap every child (including
# runner processes the in-sandbox host re-parents to PID 1) until the
# sleep child exits. Kept dependency-free (stdlib only) so it runs under
# the image's bare python3.
_REAPER_SRC: str = """\
import os, signal, subprocess, sys

child = subprocess.Popen(["sleep", "infinity"])


def _forward(signum, _frame):
    try:
        child.send_signal(signum)
    except ProcessLookupError:
        pass


signal.signal(signal.SIGTERM, _forward)
signal.signal(signal.SIGINT, _forward)

while True:
    try:
        pid, status = os.wait()
    except ChildProcessError:
        break
    if pid == child.pid:
        if os.WIFSIGNALED(status):
            sys.exit(128 + os.WTERMSIG(status))
        sys.exit(os.WEXITSTATUS(status))
"""


def _ensure_sdk() -> None:
    """
    Verify the Kubernetes client is importable, with an install hint
    when not.

    Called at the top of every launcher entry point because the client
    is an optional dependency — the base ``omnigent`` install does not
    pull it in.

    :raises click.ClickException: When the ``kubernetes`` package is not
        installed.
    """
    # import_module (not a bare ``import``) is the presence probe: it
    # raises ImportError exactly like ``import`` when the package is
    # absent, but returns the module unbound so there is no unused name
    # to suppress.
    try:
        importlib.import_module("kubernetes")
    except ImportError as exc:
        raise click.ClickException(
            "The Kubernetes client is required for the 'kubernetes' "
            "sandbox provider. Install it with "
            "`pip install 'omnigent[kubernetes]'`."
        ) from exc


def build_pod_manifest(
    *,
    pod_name: str,
    namespace: str,
    image: str,
    service_account: str,
    harness_secret: str | None,
    env_literals: dict[str, str],
    node_selector: dict[str, str] | None,
) -> dict[str, object]:
    """
    Build the sandbox Pod manifest as a plain dict.

    Pure: no SDK import, no I/O — the manifest is a literal dict the
    caller hands to ``create_namespaced_pod`` (the client accepts a dict
    body), which makes it the primary unit-test surface for every
    security / lifecycle decision baked into a sandbox Pod.

    The encoded hardening:

    - ``restartPolicy: Never`` — a crashed host should not silently
      restart with a stale launch token; the managed machinery
      provisions a replacement.
    - ``automountServiceAccountToken: false`` — a compromised agent must
      not be able to reach the API with the server SA's ``pods/exec``
      rights (codex M4).
    - Pod ``securityContext`` runs as uid/gid 1000 with ``fsGroup`` 1000
      and ``fsGroupChangePolicy: OnRootMismatch`` (only chown the volume
      when needed — cheap on the small HOME emptyDir).
    - A writable HOME: an ``emptyDir`` mounted at :data:`_HOME_DIR`, with
      ``HOME`` exported and ``workingDir`` pointed at it (codex M2).
    - ``IS_SANDBOX=1`` so in-sandbox code can detect it runs in a
      managed sandbox.
    - ``envFrom`` projects the harness Secret's keys when one is
      configured (and is omitted entirely otherwise — an empty list is
      harmless but the absent key is cleaner).
    - The container disables ``allowPrivilegeEscalation`` but keeps the
      root filesystem writable (the host writes ``/tmp`` + ``~/.omnigent``).
    - The container ``command`` is the PID-1 reaper (codex M3).

    :param pod_name: DNS-label-safe Pod name (see :func:`_new_pod_name`).
    :param namespace: Namespace the Pod is created in.
    :param image: Host image reference to run.
    :param service_account: ServiceAccount the Pod runs as.
    :param harness_secret: Name of the Secret to project via ``envFrom``,
        or ``None`` for no attached Secret.
    :param env_literals: Literal name → value env entries to add (the
        resolved server-env passthrough). Secret values should ride
        *harness_secret* instead, not this map.
    :param node_selector: Extra node selector labels merged on top of
        the mandatory ``kubernetes.io/arch: amd64`` constraint, or
        ``None`` for none.
    :returns: The Pod manifest dict.
    """
    env: list[dict[str, str]] = [
        {"name": "HOME", "value": _HOME_DIR},
        {"name": "IS_SANDBOX", "value": "1"},
    ]
    env.extend({"name": name, "value": value} for name, value in env_literals.items())

    container: dict[str, object] = {
        "name": "host",
        "image": image,
        "workingDir": _HOME_DIR,
        # PID-1 reaper (codex M3): a login shell so the image's
        # /etc/profile.d venv activation runs, then exec python3 so the
        # reaper becomes PID 1 (no intermediate bash to leak).
        "command": ["bash", "-lc", "exec python3 -c " + shlex.quote(_REAPER_SRC)],
        "env": env,
        "resources": {
            "requests": {
                "cpu": _SANDBOX_CPU_REQUEST,
                "memory": _SANDBOX_MEMORY_REQUEST,
            },
            "limits": {
                "cpu": _SANDBOX_CPU_LIMIT,
                "memory": _SANDBOX_MEMORY_LIMIT,
            },
        },
        # Not readOnlyRootFilesystem: the host writes /tmp and ~/.omnigent.
        "securityContext": {"allowPrivilegeEscalation": False},
        "volumeMounts": [{"name": "home", "mountPath": _HOME_DIR}],
    }
    if harness_secret:
        container["envFrom"] = [{"secretRef": {"name": harness_secret}}]

    spec: dict[str, object] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "serviceAccountName": service_account,
        "nodeSelector": {"kubernetes.io/arch": "amd64", **(node_selector or {})},
        "securityContext": {
            "runAsUser": _RUN_AS_UID,
            "runAsGroup": _RUN_AS_GID,
            "fsGroup": _RUN_AS_GID,
            "fsGroupChangePolicy": "OnRootMismatch",
        },
        "volumes": [{"name": "home", "emptyDir": {}}],
        "containers": [container],
    }

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "omnigent",
                "omnigent.ai/role": "sandbox-host",
            },
        },
        "spec": spec,
    }


def _new_pod_name(label: str) -> str:
    """
    Derive a DNS-label-safe Pod name from a human label.

    Mirrors :func:`omnigent.onboarding.sandboxes.islo._new_sandbox_name`:
    lowercase, non-``[a-z0-9-]`` runs collapse to ``-``, leading/trailing
    ``-`` stripped, empty falls back to ``host``, truncated to keep the
    full name within the 63-char DNS label limit, and a 6-hex random
    suffix guarantees uniqueness across relaunches of the same session.

    :param label: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
    :returns: A Pod name like ``"omnigent-managed-a1b2c3d4-1a2b3c"``.
    """
    base = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    base = re.sub(r"-+", "-", base) or "host"
    return f"omnigent-{base[:40]}-{uuid.uuid4().hex[:6]}"


def _parse_exec_status(status_frames: list[str], pod: str) -> int:
    """
    Parse the exit code from an exec STATUS frame (codex M1).

    The Kubernetes exec websocket reports the real exit status on the
    error channel (channel 3) as a serialized ``v1.Status`` object —
    ``WSClient.returncode`` is unreliable, so the STATUS frame is the
    source of truth. ``status: Success`` means exit 0; a failure carries
    the code in a ``details.causes[*]`` entry whose ``reason`` is
    ``ExitCode`` (its ``message`` is the integer code).

    :param status_frames: Raw error-channel text chunks collected from
        the exec stream.
    :param pod: Pod name, for the error message.
    :returns: The remote command's exit code.
    :raises RuntimeError: When the STATUS frame is missing, unparseable,
        or carries no exit code (a transport fault rather than a clean
        command exit — must not be silently treated as success).
    """
    raw = "".join(status_frames).strip()
    if not raw:
        raise RuntimeError(
            f"exec on pod '{pod}' returned no status frame — cannot "
            "determine the command's exit code"
        )
    try:
        status = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RuntimeError(
            f"exec on pod '{pod}' returned an unparseable status frame: {raw!r}"
        ) from exc
    if not isinstance(status, dict):
        raise RuntimeError(f"exec on pod '{pod}' returned a non-object status frame: {raw!r}")
    if status.get("status") == "Success":
        return 0
    details = status.get("details")
    causes = details.get("causes") if isinstance(details, dict) else None
    if isinstance(causes, list):
        for cause in causes:
            if isinstance(cause, dict) and cause.get("reason") == "ExitCode":
                message = cause.get("message")
                try:
                    return int(str(message))
                except ValueError as exc:
                    raise RuntimeError(
                        f"exec on pod '{pod}' returned a non-integer exit "
                        f"code in its status frame: {message!r}"
                    ) from exc
    raise RuntimeError(f"exec on pod '{pod}' returned a status frame with no exit code: {raw!r}")


class KubernetesSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for on-demand Kubernetes Pods.

    Server-managed only: ``provision`` creates a Pod and waits for its
    container to be ready, ``run`` execs commands through
    ``pods/exec``, and ``terminate`` deletes the Pod. All transport
    rides the official ``kubernetes`` client's ``CoreV1Api`` built into
    an isolated :class:`~kubernetes.client.Configuration` (no mutation of
    global client state), preferring in-cluster ServiceAccount config and
    falling back to a kubeconfig out of cluster.
    """

    provider: ClassVar[str] = "kubernetes"
    # Managed-only provider: it implements just provision/run/terminate,
    # so the CLI bootstrap flow is unsupported.
    supports_cli_bootstrap: ClassVar[bool] = False
    # No local→sandbox port forward path (the in-sandbox App OAuth flow
    # would need one); managed servers that need it use another provider.
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(
        self,
        *,
        image: str | None = None,
        namespace: str | None = None,
        env: Sequence[str] | None = None,
        secret_name: str | None = None,
        node_selector: dict[str, str] | None = None,
        service_account: str | None = None,
        kubeconfig: str | None = None,
        in_cluster: bool | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param image: Optional host image reference to run, e.g.
            ``"ghcr.io/me/omnigent-host:latest"`` — the server's
            ``sandbox.kubernetes.image`` config. ``None`` resolves
            :data:`HOST_IMAGE_ENV_VAR` and falls back to
            :data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`.
        :param namespace: Namespace to create Pods in — the server's
            ``sandbox.kubernetes.namespace`` config. ``None`` resolves
            :data:`NAMESPACE_ENV_VAR` and falls back to
            :data:`_DEFAULT_NAMESPACE`.
        :param env: Optional names of server-process environment
            variables to inject as literal env — the server's
            ``sandbox.kubernetes.env`` config. ``None`` resolves
            :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param secret_name: Optional Kubernetes Secret to project via
            ``envFrom`` — the server's ``sandbox.kubernetes.secret_name``
            config. ``None`` resolves :data:`SANDBOX_SECRET_ENV_VAR` and
            falls back to no attached Secret.
        :param node_selector: Optional extra node selector labels merged
            with the mandatory ``kubernetes.io/arch: amd64`` constraint —
            the server's ``sandbox.kubernetes.node_selector`` config.
        :param service_account: Optional ServiceAccount Pods run as —
            the server's ``sandbox.kubernetes.service_account`` config.
            ``None`` resolves :data:`SERVICE_ACCOUNT_ENV_VAR` and falls
            back to :data:`_DEFAULT_SERVICE_ACCOUNT`.
        :param kubeconfig: Optional kubeconfig path for the
            out-of-cluster fallback. ``None`` resolves
            :data:`KUBECONFIG_ENV_VAR` then the ambient kubeconfig.
        :param in_cluster: Force the config source: ``True`` for
            in-cluster ServiceAccount only, ``False`` for kubeconfig
            only, ``None`` (default) to try in-cluster then fall back to
            kubeconfig.
        """
        self._image_ref = image
        self._namespace = namespace
        self._env_names = tuple(env) if env is not None else None
        self._secret_name = secret_name
        self._node_selector = dict(node_selector) if node_selector is not None else None
        self._service_account = service_account
        self._kubeconfig = kubeconfig
        self._in_cluster = in_cluster
        self._core: k8s_client.CoreV1Api | None = None
        self._api_client: k8s_client.ApiClient | None = None

    # ── config / clients ────────────────────────────────────

    def _load_core(self) -> k8s_client.CoreV1Api:
        """
        Return the (lazily built) ``CoreV1Api``, loading cluster config
        into an isolated :class:`~kubernetes.client.Configuration`
        (codex S3).

        The config never mutates the client library's global default
        configuration: a fresh ``Configuration`` is created, the
        in-cluster ServiceAccount config (primary) or a kubeconfig
        (fallback) is loaded INTO it, and an ``ApiClient`` is built
        around that instance. With ``in_cluster`` unset the in-cluster
        path is tried first and a :class:`~kubernetes.config.ConfigException`
        (no ServiceAccount mounted, i.e. running off-cluster) falls
        through to the kubeconfig path.

        :returns: The cached ``CoreV1Api`` bound to the isolated config.
        :raises click.ClickException: When neither config source is
            available, with remediation naming both paths.
        """
        if self._core is not None:
            return self._core
        from kubernetes import client, config

        cfg = client.Configuration()
        kubeconfig_path = self._kubeconfig or os.environ.get(KUBECONFIG_ENV_VAR) or None
        try:
            if self._in_cluster is True:
                config.load_incluster_config(client_configuration=cfg)
            elif self._in_cluster is False:
                config.load_kube_config(config_file=kubeconfig_path, client_configuration=cfg)
            else:
                try:
                    config.load_incluster_config(client_configuration=cfg)
                except config.ConfigException:
                    config.load_kube_config(config_file=kubeconfig_path, client_configuration=cfg)
        except config.ConfigException as exc:
            raise click.ClickException(
                "Could not load Kubernetes configuration for the "
                "'kubernetes' sandbox provider. In-cluster, mount the "
                "server pod's ServiceAccount token; out of cluster, set "
                f"a kubeconfig (KUBECONFIG or {KUBECONFIG_ENV_VAR}). "
                f"Underlying error: {exc}"
            ) from exc
        self._api_client = client.ApiClient(cfg)
        self._core = client.CoreV1Api(self._api_client)
        return self._core

    # ── resolution helpers ──────────────────────────────────

    def _resolve_image(self) -> str:
        """
        Resolve the host image: constructor → env override → default.

        :returns: The image reference to run.
        """
        return self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE

    def _resolve_namespace(self) -> str:
        """
        Resolve the namespace: constructor → env override → default.

        :returns: The namespace to create Pods in.
        """
        return self._namespace or os.environ.get(NAMESPACE_ENV_VAR) or _DEFAULT_NAMESPACE

    def _resolve_secret(self) -> str | None:
        """
        Resolve the harness Secret name: constructor → env override →
        ``None``.

        :returns: The Secret name to project, or ``None`` for none.
        """
        return self._secret_name or os.environ.get(SANDBOX_SECRET_ENV_VAR) or None

    def _resolve_service_account(self) -> str:
        """
        Resolve the ServiceAccount: constructor → env override →
        default.

        :returns: The ServiceAccount the Pod runs as.
        """
        return (
            self._service_account
            or os.environ.get(SERVICE_ACCOUNT_ENV_VAR)
            or _DEFAULT_SERVICE_ACCOUNT
        )

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the literal env vars to inject into created Pods.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated)
        applies; an empty resolution injects nothing. Values come from
        the server's own environment — a configured name that is unset
        there fails loud (an operator listed a value the deployment
        never provided; silently launching without it would surface much
        later as an opaque failure inside the sandbox).

        :returns: Name → value mapping for literal Pod ``env``.
        :raises click.ClickException: When a configured name is not set
            in the server process environment.
        """
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    f"sandbox.kubernetes.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    # ── lifecycle ───────────────────────────────────────────

    def prepare(self) -> None:
        """
        Local preflight: the client must be installed and the cluster
        reachable via in-cluster or kubeconfig config.

        :raises click.ClickException: When the client is missing or no
            usable configuration can be loaded.
        """
        _ensure_sdk()
        self._load_core()

    def provision(self, name: str) -> str:
        """
        Create a sandbox Pod from the host image and wait for it ready.

        The Pod boots ``sleep infinity`` under the PID-1 reaper; the
        pod-ready wait (:meth:`_wait_for_pod_ready`) consumes its budget
        here, BEFORE the shared ``_wait_for_host_online`` poll, so a Pod
        that can't schedule or pull its image fails fast with a clear
        reason rather than as a generic online timeout.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
            Slugged into a DNS-safe Pod name; the returned name is the
            canonical reference.
        :returns: The created Pod's name.
        :raises click.ClickException: If creation fails or the Pod does
            not become ready in time.
        """
        _ensure_sdk()
        from kubernetes.client.rest import ApiException

        namespace = self._resolve_namespace()
        image = self._resolve_image()
        env_literals = self._resolve_sandbox_env()
        core = self._load_core()

        pod_name = _new_pod_name(name)
        click.echo(
            f"▸ Creating Kubernetes pod '{pod_name}' in namespace '{namespace}' from {image}"
        )
        for attempt in range(2):
            manifest = build_pod_manifest(
                pod_name=pod_name,
                namespace=namespace,
                image=image,
                service_account=self._resolve_service_account(),
                harness_secret=self._resolve_secret(),
                env_literals=env_literals,
                node_selector=self._node_selector,
            )
            try:
                core.create_namespaced_pod(namespace, manifest)
                break
            except ApiException as exc:
                # A name collision (another launch raced the same slug)
                # is recoverable once: regenerate the random suffix and
                # retry. Any other failure surfaces with the API reason.
                if exc.status == 409 and attempt == 0:
                    pod_name = _new_pod_name(name)
                    continue
                raise click.ClickException(_format_api_error("create pod", pod_name, exc)) from exc

        self._wait_for_pod_ready(pod_name)
        click.echo(f"  → pod '{pod_name}' is ready")
        return pod_name

    def _wait_for_pod_ready(self, pod_name: str) -> None:
        """
        Block until the Pod's container is ready, failing fast on
        terminal states (codex S2 + fast-fail).

        Readiness — not merely ``phase == Running`` — gates the first
        exec: a container can be ``Running`` for a moment before its
        process is up. The wait also fails immediately, rather than
        burning the whole budget, on states that will never resolve: a
        ``Failed``/``Succeeded`` phase, a container stuck in an image
        pull / config error (see :data:`_FATAL_WAITING_REASONS`), or an
        ``Unschedulable`` ``PodScheduled`` condition. Every failure
        surfaces recent Pod events and a ``kubectl describe`` hint.

        :param pod_name: The Pod to wait on.
        :raises click.ClickException: On a terminal state or timeout.
        """
        from kubernetes.client.rest import ApiException

        namespace = self._resolve_namespace()
        core = self._load_core()
        deadline = time.monotonic() + _POD_READY_TIMEOUT_S
        while True:
            try:
                pod = core.read_namespaced_pod(pod_name, namespace)
            except ApiException as exc:
                raise click.ClickException(_format_api_error("read pod", pod_name, exc)) from exc

            phase = _pod_phase(pod)
            if phase in ("Failed", "Succeeded"):
                raise click.ClickException(
                    self._pod_failure_message(
                        pod_name,
                        f"pod entered terminal phase '{phase}' before becoming ready",
                    )
                )
            fatal = _fatal_container_reason(pod)
            if fatal is not None:
                reason, message = fatal
                detail = f"{reason}: {message}" if message else reason
                raise click.ClickException(
                    self._pod_failure_message(pod_name, f"container cannot start ({detail})")
                )
            unschedulable = _unschedulable_message(pod)
            if unschedulable is not None:
                raise click.ClickException(
                    self._pod_failure_message(
                        pod_name, f"pod cannot be scheduled ({unschedulable})"
                    )
                )
            if phase == "Running" and _container_ready(pod):
                return
            if time.monotonic() >= deadline:
                raise click.ClickException(
                    self._pod_failure_message(
                        pod_name,
                        f"pod did not become ready within {_POD_READY_TIMEOUT_S}s "
                        f"(last phase '{phase or 'unknown'}')",
                    )
                )
            time.sleep(_POD_READY_POLL_S)

    def _pod_failure_message(self, pod_name: str, summary: str) -> str:
        """
        Build a pod-ready failure message, appending recent Pod events
        and a ``kubectl describe`` pointer.

        Events carry the scheduler/kubelet's own reason (Failed
        Scheduling, Failed pull, …), which is what an operator needs to
        diagnose the failure; best-effort, so an events lookup that
        itself errors is omitted rather than masking the real failure.

        :param pod_name: The failed Pod.
        :param summary: The failure summary (what went wrong).
        :returns: The full error message.
        """
        namespace = self._resolve_namespace()
        message = f"Kubernetes sandbox pod '{pod_name}' {summary}."
        events = self._recent_events(pod_name)
        if events:
            message += f" Recent events: {events}"
        message += f" Inspect with `kubectl describe pod {pod_name} -n {namespace}`."
        return message

    def _recent_events(self, pod_name: str) -> str:
        """
        Return a compact ``reason: message`` summary of the Pod's recent
        events, or empty when none are available.

        :param pod_name: The Pod to fetch events for.
        :returns: A ``"; "``-joined summary, or ``""``.
        """
        from kubernetes.client.rest import ApiException

        namespace = self._resolve_namespace()
        try:
            core = self._load_core()
            event_list = core.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={pod_name}",
            )
        except ApiException:
            return ""
        parts: list[str] = []
        for event in getattr(event_list, "items", None) or []:
            reason = getattr(event, "reason", None)
            message = getattr(event, "message", None)
            if reason or message:
                parts.append(f"{reason or '?'}: {message or ''}".strip())
        return "; ".join(parts)

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the Pod via ``pods/exec`` and capture its
        output (codex M1, S5).

        The command runs under ``["bash", "-lc", command]`` so the
        image's login-shell venv activation puts ``omnigent`` on PATH
        (codex S5). The exec websocket is read with ``_preload_content=
        False``: STDOUT/STDERR are drained channel by channel, and the
        real exit code comes from the error-channel STATUS frame via
        :func:`_parse_exec_status` (``WSClient.returncode`` is
        unreliable).

        :param sandbox_id: Target Pod name.
        :param command: Shell command to execute remotely.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: If the exec transport fails, the
            status frame is unusable, or *check* is ``True`` and the
            command exits non-zero.
        """
        _ensure_sdk()
        from kubernetes.client.rest import ApiException
        from kubernetes.stream import stream
        from kubernetes.stream.ws_client import (
            ERROR_CHANNEL,
            STDERR_CHANNEL,
            STDOUT_CHANNEL,
        )

        namespace = self._resolve_namespace()
        core = self._load_core()
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        error_chunks: list[str] = []

        def _drain() -> None:
            out = ws.read_channel(STDOUT_CHANNEL)
            if out:
                stdout_chunks.append(out)
                click.echo(out, nl=False)
            err = ws.read_channel(STDERR_CHANNEL)
            if err:
                stderr_chunks.append(err)
                click.echo(err, nl=False, err=True)
            status = ws.read_channel(ERROR_CHANNEL)
            if status:
                error_chunks.append(status)

        try:
            ws = stream(
                core.connect_get_namespaced_pod_exec,
                sandbox_id,
                namespace,
                command=["bash", "-lc", command],
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
            try:
                while ws.is_open():
                    ws.update(timeout=1)
                    _drain()
                # Drain any frames buffered between the last update and
                # the socket close.
                _drain()
            finally:
                ws.close()
        except ApiException as exc:
            # The Pod may have been deleted mid-run (a racing terminate),
            # or pods/exec may be forbidden — surface the API reason
            # through the launcher contract, not a raw client traceback.
            raise click.ClickException(_format_api_error("exec in pod", sandbox_id, exc)) from exc

        try:
            returncode = _parse_exec_status(error_chunks, sandbox_id)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on pod '{sandbox_id}' (exit {returncode}): {command}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def terminate(self, sandbox_id: str) -> None:
        """
        Delete a sandbox Pod, releasing its compute.

        Idempotent: a Pod that no longer exists (404) is treated as
        success — the desired end state holds, and managed teardown can
        race the provider's own deletion. ``grace_period_seconds=0``
        deletes promptly (the reaper forwards SIGTERM, but a torn-down
        session host needn't linger).

        :param sandbox_id: The Pod to delete.
        :raises click.ClickException: On any delete failure other than
            not-found.
        """
        _ensure_sdk()
        from kubernetes.client.rest import ApiException

        namespace = self._resolve_namespace()
        try:
            self._load_core().delete_namespaced_pod(sandbox_id, namespace, grace_period_seconds=0)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise click.ClickException(_format_api_error("delete pod", sandbox_id, exc)) from exc


# ── module helpers ─────────────────────────────────────────


def _format_api_error(action: str, pod: str, exc: k8s_client.ApiException) -> str:
    """
    Build a launcher-contract message for a Kubernetes ``ApiException``.

    Includes the HTTP reason and any response body so the managed-launch
    error surface carries the cluster's own explanation, and adds an
    RBAC pointer on 403 (the usual cause: the server ServiceAccount
    lacks the sandbox-manager Role) — the single most common
    misconfiguration of this provider.

    :param action: What was attempted, e.g. ``"create pod"``.
    :param pod: The Pod the action targeted.
    :param exc: The raised ``ApiException``.
    :returns: The error message.
    """
    reason = getattr(exc, "reason", None) or "unknown error"
    message = f"Failed to {action} '{pod}': {reason}"
    body = getattr(exc, "body", None)
    if body:
        message += f" ({body})"
    if getattr(exc, "status", None) == 403:
        message += (
            " — the server ServiceAccount likely lacks the sandbox-manager "
            "Role (pods, pods/exec); apply deploy/kubernetes/rbac.yaml."
        )
    return message


def _pod_phase(pod: object) -> str | None:
    """
    Return the Pod's ``status.phase`` (e.g. ``"Running"``), or ``None``.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The phase string, or ``None`` when status is absent.
    """
    status = getattr(pod, "status", None)
    return getattr(status, "phase", None) if status is not None else None


def _container_ready(pod: object) -> bool:
    """
    Report whether any container status is ``ready`` (codex S2).

    The sandbox Pod has one container, so any ready container means the
    host is execable.

    :param pod: A ``V1Pod`` read from the API.
    :returns: ``True`` when a container reports ``ready``.
    """
    status = getattr(pod, "status", None)
    statuses = getattr(status, "container_statuses", None) if status is not None else None
    if not statuses:
        return False
    return any(getattr(cs, "ready", False) for cs in statuses)


def _fatal_container_reason(pod: object) -> tuple[str, str] | None:
    """
    Return a ``(reason, message)`` for a container stuck in a
    non-recoverable waiting state, or ``None`` (fast-fail).

    Matches a container ``state.waiting.reason`` against
    :data:`_FATAL_WAITING_REASONS` — an unfixable image pull or config
    error that the ready wait should surface immediately rather than
    poll to its deadline.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The fatal ``(reason, message)``, or ``None``.
    """
    status = getattr(pod, "status", None)
    statuses = getattr(status, "container_statuses", None) if status is not None else None
    for cs in statuses or []:
        state = getattr(cs, "state", None)
        waiting = getattr(state, "waiting", None) if state is not None else None
        reason = getattr(waiting, "reason", None) if waiting is not None else None
        if reason in _FATAL_WAITING_REASONS:
            return reason, getattr(waiting, "message", None) or ""
    return None


def _unschedulable_message(pod: object) -> str | None:
    """
    Return the scheduler's message when the Pod is unschedulable, or
    ``None`` (fast-fail).

    Matches a ``PodScheduled`` condition with ``status == "False"`` and
    ``reason == "Unschedulable"`` — no node fits the Pod's resource
    requests / node selector, which won't resolve without operator
    action.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The scheduler message (or the bare reason), or ``None``.
    """
    status = getattr(pod, "status", None)
    conditions = getattr(status, "conditions", None) if status is not None else None
    for cond in conditions or []:
        if (
            getattr(cond, "type", None) == "PodScheduled"
            and getattr(cond, "status", None) == "False"
            and getattr(cond, "reason", None) == "Unschedulable"
        ):
            return getattr(cond, "message", None) or "Unschedulable"
    return None
