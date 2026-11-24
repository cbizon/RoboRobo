"""Agent runs as pods of their own: the Job they get, running one against a
stand-in Kubernetes API, and forwarding cluster commands to the service."""

import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from test_program import isolated_git, program, run  # noqa: F401

from roborobo import kube, service_api


def settings(api="http://127.0.0.1:1"):
    return kube.Settings(api=api, token="t", namespace="bizon", image="img@sha256:abc",
                         volume="roborobo-home", home="/Users/bizon",
                         service_url="http://roborobo-api:8081",
                         claude_secret="claude-provider-credentials", git_name="RoboRobo service",
                         git_email="r@example.org",
                         service_labels={"app.kubernetes.io/component": "service"},
                         pull_secrets=[{"name": "registry-credentials"}])


def test_the_agent_pod_holds_no_keys():
    job = kube.manifest(settings(), "roborobo-x", ["claude", "-p", "go"],
                        "/Users/bizon/Projects/experiments/sequel", 60, "run-token")
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert pod["imagePullSecrets"] == [{"name": "registry-credentials"}]  # the image is private
    assert all("secret" not in v for v in pod["volumes"])  # no ssh keys, no deploy key
    assert [m["mountPath"] for m in container["volumeMounts"]] == [
        "/Users/bizon", "/Users/bizon/Projects", "/Users/bizon/.cache/uv"]
    assert [m.get("subPath") for m in container["volumeMounts"]] == [None, "Projects", ".cache/uv"]
    env = {e["name"]: e for e in container["env"]}
    assert env["CLAUDE_CODE_OAUTH_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == \
        "claude-provider-credentials"
    assert env["ROBOROBO_RUN_TOKEN"]["value"] == "run-token"
    assert env["ROBOROBO_SERVICE_URL"]["value"] == "http://roborobo-api:8081"
    assert not any("SSH" in name or "GITHUB" in name for name in env)
    assert job["spec"]["activeDeadlineSeconds"] == 3600 and job["spec"]["backoffLimit"] == 0
    assert container["image"] == "img@sha256:abc"
    assert container["command"][:2] == ["sh", "-c"] and container["command"][3:] == [
        "agent", "claude", "-p", "go"]
    term = pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
    assert term["topologyKey"] == "kubernetes.io/hostname"


class StandIn:
    """A stand-in for the few Kubernetes API calls an agent run makes."""

    def __init__(self, outcome="succeeded", exit_code=0, log="the agent's output\n",
                 gone=False):
        self.outcome, self.exit_code, self.log, self.gone = outcome, exit_code, log, gone
        self.created, self.deleted, self.gets = [], [], 0
        self.token_live_at_create = None
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def reply(self, payload, raw=False):
                body = payload.encode() if raw else json.dumps(payload).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                job = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stand_in.created.append(job)
                env = {e["name"]: e.get("value")
                       for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
                stand_in.token = env["ROBOROBO_RUN_TOKEN"]
                stand_in.token_live_at_create = service_api.ACTIVE.repo(stand_in.token)
                self.reply(job)

            def do_DELETE(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))  # else: a reset
                stand_in.deleted.append(urlparse(self.path).path.rsplit("/", 1)[1])
                if stand_in.gone:
                    self.send_response(404)
                    self.end_headers()
                    return self.wfile.write(b'{"reason": "NotFound"}')
                self.reply({})

            def do_GET(self):  # noqa: N802
                path = urlparse(self.path).path
                if path.endswith("/log"):
                    return self.reply(stand_in.log, raw=True)
                if path.endswith("/pods"):
                    if stand_in.outcome == "never":
                        return self.reply({"items": []})
                    if stand_in.outcome == "pull":
                        return self.reply({"items": [{"metadata": {"name": "pod-1"}, "status": {
                            "phase": "Pending", "containerStatuses": [{"state": {"waiting": {
                                "reason": "ImagePullBackOff",
                                "message": "Back-off pulling image"}}}]}}]})
                    return self.reply({"items": [{"metadata": {"name": "pod-1"}, "status": {
                        "containerStatuses": [{"state": {"terminated": {
                            "exitCode": stand_in.exit_code}}}]}}]})
                stand_in.gets += 1
                if stand_in.gets < 2 or stand_in.outcome in ("never", "pull"):
                    return self.reply({"status": {"active": 1}})
                if stand_in.outcome == "deadline":
                    return self.reply({"status": {"failed": 1, "conditions": [
                        {"type": "Failed", "reason": "DeadlineExceeded"}]}})
                return self.reply({"status": {stand_in.outcome: 1}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.mark.parametrize("outcome, code, expected", [
    ("succeeded", 0, None),
    ("failed", 1, "exited with code 1"),
    ("deadline", 1, "stopped after its limit of 60 minutes"),
])
def test_an_agent_pod_runs_to_the_end(tmp_path, outcome, code, expected):
    stand_in = StandIn(outcome, code)
    log = tmp_path / "logs" / "worker.log"
    problem = kube.run(settings(stand_in.url), "roborobo-x", ["claude", "-p", "go"], "/repo", 60,
                       "tok", log, sleep=lambda s: None)
    assert (problem is None) if expected is None else (expected in problem)
    assert log.read_text() == "the agent's output\n"
    assert stand_in.deleted == ["roborobo-x"]  # the Job is always cleaned up


def test_run_agent_uses_a_pod_in_the_service(program, tmp_path, monkeypatch):
    from roborobo import config, night
    stand_in = StandIn()
    monkeypatch.setenv("ROBOROBO_RUNNER", "kubernetes")
    monkeypatch.setattr(kube, "in_cluster", lambda: settings(stand_in.url))
    monkeypatch.setattr(kube, "POLL_SECONDS", 0)
    problem = night.run_agent(config.load(program), "Run line L0001.", 60,
                              tmp_path / "logs" / "2026-11-23-L0001-worker.log")
    assert problem is None
    command = stand_in.created[0]["spec"]["template"]["spec"]["containers"][0]["command"]
    assert command[4:] == ["claude", "-p", "Run line L0001."]
    # the run's token works while it runs, for its own folder only, and not after
    assert stand_in.token_live_at_create == program.resolve()
    assert service_api.ACTIVE.repo(stand_in.token) is None


@pytest.fixture
def api(program):
    server = service_api.serve("127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield url
    server.shutdown()
    service_api.ACTIVE.remove("run-token")


def test_a_cluster_command_is_run_by_the_service(program, api, monkeypatch, capsys):
    service_api.ACTIVE.add("run-token", program)
    monkeypatch.setenv("ROBOROBO_SERVICE_URL", api)
    monkeypatch.setenv("ROBOROBO_RUN_TOKEN", "run-token")
    assert run("jobs", "--repo", program) == 0
    assert "no jobs in flight" in capsys.readouterr().out


def test_the_service_refuses_a_wrong_token(program, api, monkeypatch, capsys):
    service_api.ACTIVE.add("run-token", program)
    monkeypatch.setenv("ROBOROBO_SERVICE_URL", api)
    monkeypatch.setenv("ROBOROBO_RUN_TOKEN", "a guess")
    assert run("jobs", "--repo", program) == 2
    assert "no run with that token is in progress" in capsys.readouterr().err


def post(url, body):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + "/program", data=json.dumps(body).encode(), method="POST")
    try:
        urllib.request.urlopen(req)
        return 200, ""
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_the_service_runs_only_allowed_commands_for_its_programs(program, api, tmp_path):
    service_api.ACTIVE.add("run-token", program)
    code, why = post(api, {"argv": ["approve"], "repo": str(program), "token": "run-token"})
    assert code == 403 and "only check, fetch, jobs, next-id, submit" in why
    code, why = post(api, {"argv": ["delete", "A0001", "--reason", "x"], "repo": str(program),
                           "token": "run-token"})
    assert code == 403
    code, why = post(api, {"argv": ["jobs"], "repo": str(tmp_path), "token": "run-token"})
    assert code == 403 and "isn't this run's folder" in why
    service_api.ACTIVE.remove("run-token")
    code, why = post(api, {"argv": ["jobs"], "repo": str(program), "token": "run-token"})
    assert code == 403  # nothing is running, so no token works


def test_a_pod_that_never_starts_fails_the_run(tmp_path):
    """On Nov 23 the first agent pod exceeded the namespace's quota and was never
    created; the run waited for its time limit instead of saying so."""
    stand_in = StandIn("never")
    ticks = iter(range(0, 10000, 100))
    problem = kube.run(settings(stand_in.url), "roborobo-x", ["claude"], "/repo", 60, "tok",
                       tmp_path / "worker.log", sleep=lambda s: None, clock=lambda: next(ticks))
    assert "its pod didn't start within 5 minutes (no pod was created" in problem
    assert "kubectl describe job roborobo-x" in problem
    assert stand_in.deleted == ["roborobo-x"]


def test_a_job_deleted_by_hand_is_not_an_error(tmp_path):
    stand_in = StandIn(gone=True)
    assert kube.run(settings(stand_in.url), "roborobo-x", ["claude"], "/repo", 60, "tok",
                    tmp_path / "worker.log", sleep=lambda s: None) is None


def test_the_agent_pod_fits_the_namespaces_disk_quota():
    job = kube.manifest(settings(), "roborobo-x", ["claude"], "/repo", 60, "tok")
    limits = job["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert limits["ephemeral-storage"] == "1Gi"  # the quota is 4Gi in all, ~2.75Gi in use


def test_a_pod_that_cannot_pull_its_image_fails_the_run(tmp_path):
    """The second try on Nov 23: the pod existed but couldn't pull the private
    image, and the run would have waited out its hour."""
    stand_in = StandIn("pull")
    ticks = iter(range(0, 10000, 100))
    problem = kube.run(settings(stand_in.url), "roborobo-x", ["claude"], "/repo", 60, "tok",
                       tmp_path / "worker.log", sleep=lambda s: None, clock=lambda: next(ticks))
    assert "ImagePullBackOff: Back-off pulling image" in problem
    assert stand_in.deleted == ["roborobo-x"]


def test_the_pod_trusts_the_program_folder_then_runs_the_agent(tmp_path, monkeypatch):
    """Claude Code ignores a project's settings until the folder is trusted, and
    the pod's home starts empty (the first runs on Sterling, Nov 23). For a
    line's worktree it looks trust up under the main repository's path."""
    import subprocess
    job = kube.manifest(settings(), "x", ["printf", "%s|", "an argument with spaces", "two"],
                        "/repo", 1, "t")
    command = job["spec"]["template"]["spec"]["containers"][0]["command"]
    repo, tree = tmp_path / "sequel", tmp_path / "sequel.L0001"
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin",
           "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
    (tmp_path / "gitconfig").write_text("[user]\n\tname = T\n\temail = t@example.org\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "start"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "line/L0001",
                    str(tree)], check=True, env=env)
    done = subprocess.run(command, cwd=tree, env=env, capture_output=True, text=True, check=True)
    assert done.stdout == "an argument with spaces|two|"
    trusted = json.loads((tmp_path / ".claude.json").read_text())["projects"]
    assert {Path(p).resolve() for p in trusted} == {tree.resolve(), repo.resolve()}
    assert all(v == {"hasTrustDialogAccepted": True} for v in trusted.values())
