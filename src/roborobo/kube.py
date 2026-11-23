"""Agent runs as their own Kubernetes pods, without the service's keys.

In the service (``ROBOROBO_RUNNER=kubernetes``), each agent run is a Job in
the service's namespace, using the service's own image. The run's pod mounts
only what the work needs from the service's volume (``Projects/``, which
holds the program clones, their data folders and RoboRobo, and uv's cache)
and gets a scratch home of its own. It gets the Claude token and nothing
else: no SSH keys, no deploy key, no service-account token. It runs on the
service's node, since the volume can be attached to one node.

Commands that need the service's keys (``program submit``, ``jobs``,
``fetch``, and ``check``, which fetches approval tags) are sent to the service over HTTP with a token made for this run
(see ``roborobo.service_api``), so a run keeps its cluster workflow without
ever holding a key. The service pushes what the run committed, as before.

The service talks to the Kubernetes API with its own service account (a
Role that may create and watch Jobs and read their pods and logs).
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
POLL_SECONDS = 10
# A run whose container hasn't started by then won't (a quota, an image it
# can't pull): fail it, saying so, rather than wait out its time limit.
POD_START_SECONDS = 300


class APIError(RuntimeError):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


@dataclass
class Settings:
    """Where the Kubernetes API is and what an agent pod gets."""
    api: str                       # https://<host>:<port>, or a stand-in in tests
    token: str
    namespace: str
    image: str
    volume: str                    # the service's PersistentVolumeClaim
    home: str                      # HOME in both pods, e.g. /Users/bizon
    service_url: str               # where agent pods send program submit/jobs/fetch
    claude_secret: str             # Secret with CLAUDE_CODE_OAUTH_TOKEN
    git_name: str
    git_email: str
    service_labels: dict[str, str] = field(default_factory=dict)
    pull_secrets: list[dict] = field(default_factory=list)  # the service pod's, e.g. for ghcr.io
    ca: str | None = None          # the cluster's CA file; None for a plain-HTTP stand-in

    def context(self):
        return ssl.create_default_context(cafile=self.ca) if self.ca else None


def in_cluster() -> Settings:
    """Settings for the service pod, from its environment and service account.
    Every value is required; a missing one fails here, naming it."""
    def need(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"{name} isn't set; the service needs it to run agent pods")
        return value

    host, port = need("KUBERNETES_SERVICE_HOST"), need("KUBERNETES_SERVICE_PORT")
    settings = Settings(
        api=f"https://{host}:{port}", token=(ACCOUNT / "token").read_text().strip(),
        namespace=(ACCOUNT / "namespace").read_text().strip(), image="",
        volume=need("ROBOROBO_VOLUME"), home=need("HOME"),
        service_url=need("ROBOROBO_SERVICE_URL"), claude_secret=need("ROBOROBO_CLAUDE_SECRET"),
        git_name=need("GIT_NAME"), git_email=need("GIT_EMAIL"), ca=str(ACCOUNT / "ca.crt"))
    me = request(settings, "GET", f"/api/v1/namespaces/{settings.namespace}/pods/"
                                   f"{need('HOSTNAME')}")
    settings.image = me["spec"]["containers"][0]["image"]  # the agent runs the service's image
    settings.service_labels = {k: v for k, v in me["metadata"]["labels"].items()
                               if k.startswith("app.kubernetes.io/")}
    settings.pull_secrets = me["spec"].get("imagePullSecrets", [])  # the image is private
    return settings


def request(settings: Settings, method: str, path: str, body: dict | None = None,
            raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(settings.api + path, data=data, method=method, headers={
        "Authorization": f"Bearer {settings.token}", "Content-Type": "application/json",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, context=settings.context(), timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        raise APIError(f"Kubernetes API {method} {path} failed ({error.code}): "
                       f"{error.read().decode()[:500]}", error.code) from None
    return payload.decode() if raw else json.loads(payload or b"{}")


def job_name(stem: str) -> str:
    """A valid, unique Job name for a run, e.g. roborobo-2026-11-23-l0013-2-w-1700000000."""
    clean = re.sub(r"[^a-z0-9-]+", "-", stem.lower()).strip("-")[:40]
    return f"roborobo-{clean}-{int(time.time())}"


def manifest(settings: Settings, name: str, command: list[str], workdir: str,
             minutes: float, run_token: str) -> dict:
    """The Job for one agent run: the volume's Projects/ and uv cache, a scratch
    home, the Claude token, and nothing that holds a key."""
    home = settings.home
    env = [{"name": "HOME", "value": home},
           {"name": "ROBOROBO_HOST", "value": "sterling"},
           {"name": "TMPDIR", "value": f"{home}/.tmp"},
           {"name": "UV_CACHE_DIR", "value": f"{home}/.cache/uv"},
           {"name": "ROBOROBO_SERVICE_URL", "value": settings.service_url},
           {"name": "ROBOROBO_RUN_TOKEN", "value": run_token},
           {"name": "GIT_AUTHOR_NAME", "value": settings.git_name},
           {"name": "GIT_COMMITTER_NAME", "value": settings.git_name},
           {"name": "GIT_AUTHOR_EMAIL", "value": settings.git_email},
           {"name": "GIT_COMMITTER_EMAIL", "value": settings.git_email},
           {"name": "CLAUDE_CODE_OAUTH_TOKEN", "valueFrom": {"secretKeyRef": {
               "name": settings.claude_secret, "key": "CLAUDE_CODE_OAUTH_TOKEN"}}}]
    labels = {"app.kubernetes.io/part-of": "roborobo", "app.kubernetes.io/component": "agent"}
    affinity = {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
        "labelSelector": {"matchLabels": settings.service_labels},
        "topologyKey": "kubernetes.io/hostname"}]}} if settings.service_labels else None
    pod = {
        "restartPolicy": "Never",
        "imagePullSecrets": settings.pull_secrets,
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 7474, "runAsGroup": 7474,
                            "fsGroup": 7474, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "agent", "image": settings.image, "command": command,
            "workingDir": workdir, "env": env,
            "volumeMounts": [
                {"name": "scratch", "mountPath": home},
                {"name": "home", "mountPath": f"{home}/Projects", "subPath": "Projects"},
                {"name": "home", "mountPath": f"{home}/.cache/uv", "subPath": ".cache/uv"}],
            # The namespace's quota on ephemeral-storage limits is 4Gi in all.
            "resources": {"requests": {"cpu": "1", "memory": "4Gi", "ephemeral-storage": "100Mi"},
                          "limits": {"cpu": "2", "memory": "8Gi", "ephemeral-storage": "1Gi"}},
            "securityContext": {"allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}}}],
        "volumes": [{"name": "scratch", "emptyDir": {"sizeLimit": "1Gi"}},
                    {"name": "home", "persistentVolumeClaim": {"claimName": settings.volume}}],
    }
    if affinity:
        pod["affinity"] = affinity
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "labels": labels},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": int(minutes * 60),
                     "ttlSecondsAfterFinished": 3600,
                     "template": {"metadata": {"labels": labels}, "spec": pod}}}


def not_started(pods: list[dict]) -> str | None:
    """Why a run's container hasn't started, or None if it has."""
    if not pods:
        return "no pod was created: a quota?"
    statuses = pods[0].get("status", {}).get("containerStatuses") or []
    if any("running" in s.get("state", {}) or "terminated" in s.get("state", {})
           for s in statuses):
        return None
    waiting = next((s["state"]["waiting"] for s in statuses if "waiting" in s.get("state", {})),
                   None)
    if waiting:
        return f"{waiting.get('reason', 'waiting')}: {waiting.get('message', '')[:200]}".strip()
    return f"pod {pods[0].get('status', {}).get('phase', 'Pending')}"


def run(settings: Settings, name: str, command: list[str], workdir: str, minutes: float,
        run_token: str, log: Path, sleep=time.sleep, clock=time.monotonic) -> str | None:
    """Run one agent pod to the end; write its output to ``log``; return what
    went wrong, if anything (the same messages as a local run)."""
    jobs = f"/apis/batch/v1/namespaces/{settings.namespace}/jobs"
    request(settings, "POST", jobs, manifest(settings, name, command, workdir, minutes,
                                             run_token))
    pods_path = f"/api/v1/namespaces/{settings.namespace}/pods?labelSelector=job-name%3D{name}"
    started = clock()
    try:
        while True:
            status = request(settings, "GET", f"{jobs}/{name}").get("status", {})
            if status.get("succeeded") or status.get("failed"):
                break
            if clock() - started > POD_START_SECONDS:
                stuck = not_started(request(settings, "GET", pods_path)["items"])
                if stuck:
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text("")
                    return (f"its pod didn't start within {POD_START_SECONDS // 60} minutes "
                            f"({stuck}; see `kubectl describe job {name}`)")
            sleep(POLL_SECONDS)
        pods = request(settings, "GET", pods_path)["items"]
        log.parent.mkdir(parents=True, exist_ok=True)
        if not pods:
            log.write_text("")
            return f"its pod never started (job {name}: {json.dumps(status)[:300]})"
        pod = pods[0]
        log.write_text(request(settings, "GET", f"/api/v1/namespaces/{settings.namespace}/pods/"
                                                f"{pod['metadata']['name']}/log", raw=True))
        reason = next((c["reason"] for c in status.get("conditions", [])
                       if c.get("type") == "Failed"), "")
        if reason == "DeadlineExceeded":
            return f"stopped after its limit of {minutes:g} minutes (log: {log})"
        terminated = next((s["state"]["terminated"] for s in
                           pod.get("status", {}).get("containerStatuses", [])
                           if "terminated" in s.get("state", {})), None)
        code = terminated.get("exitCode", 1) if terminated else 1
        if status.get("failed") or code:
            return f"exited with code {code} (log: {log})"
        return None
    finally:
        try:
            request(settings, "DELETE", f"{jobs}/{name}", {
                "kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": "Background"})
        except APIError as error:
            if error.code != 404:  # already gone (deleted by hand) is fine; nothing else is
                raise
