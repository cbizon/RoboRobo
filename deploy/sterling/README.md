# The RoboRobo service on Sterling

`program serve` in one pod in the `bizon` namespace. Every few minutes it
merges what you pushed to each program, records ended Hatteras jobs, and
runs any line with something new to do (see `src/roborobo/service.py` and
`docs/always-on.md`). The pod's home is a volume laid out like your Mac
(`/Users/bizon/Projects/experiments/...`), so the paths `program sync`
renders into program files are the same here.

Turn off the Mac's night runner before starting the service: two runners
would act on the same lines.

```bash
launchctl bootout gui/$(id -u)/org.renci.roborobo.sequel
```

## Once: keys and secrets

1. **A key for Hatteras**, the service's own, with no passphrase:

   ```bash
   ssh-keygen -t ed25519 -N "" -C roborobo-service -f roborobo-hatteras
   ssh-copy-id -i roborobo-hatteras.pub hatteras
   ```

2. **A deploy key per program**, with write access, since the service pushes
   (GitHub → the repository → Settings → Deploy keys → Allow write access):

   ```bash
   ssh-keygen -t ed25519 -N "" -C roborobo-service-sequel -f roborobo-github-sequel
   ```

3. **Host keys**, checked against GitHub's published fingerprints and the
   key your Mac already trusts for `ht1.renci.org`:

   ```bash
   ssh-keyscan github.com ht1.renci.org > known_hosts
   ssh-keygen -lf known_hosts
   ```

4. **The secret** the pod reads them from:

   ```bash
   kubectl create secret generic roborobo-ssh -n bizon \
     --from-file=hatteras=roborobo-hatteras \
     --from-file=github-sequel=roborobo-github-sequel \
     --from-file=known_hosts=known_hosts
   ```

   Then delete the private key files from your Mac, or keep them somewhere
   safe; the cluster has its copy.

5. **Claude**: the agent runs use `CLAUDE_CODE_OAUTH_TOKEN` from the existing
   `claude-provider-credentials` secret (made for miner2049er from `claude
   setup-token`). To use a separate token, create another secret the same
   way and change the name in `deployment.yaml`.

6. **Your commit identity**: set `GIT_EMAIL` in `deployment.yaml`.

## Build and deploy

```bash
docker buildx build --platform linux/amd64 -t ghcr.io/cbizon/roborobo:0.1-$(git rev-parse --short HEAD) --push deploy/sterling
```

Put the tag and digest the push prints into `deployment.yaml`, then:

```bash
kubectl apply -n bizon -f deploy/sterling/rbac.yaml -f deploy/sterling/pvc.yaml -f deploy/sterling/deployment.yaml -f deploy/sterling/service.yaml
kubectl logs -n bizon -f deploy/roborobo-service
```

The image holds only tools. RoboRobo runs from its clone on the volume, at
`ROBOROBO_REF`, updated each time the pod starts: to pick up a merged
RoboRobo change, restart the pod.

```bash
kubectl rollout restart -n bizon deploy/roborobo-service
```

## Day to day

- **The dashboard:** what needs you (a pause, proposed lines, the latest
  questions, doubted claims, failed jobs, lines held by the limits), then a
  row per line with its last run, the checker's verdicts, jobs in flight and
  a Discuss link that opens a Claude Code thread on that line in your clone.
  It links to the full program map. From the Mac:

  ```bash
  kubectl port-forward -n bizon svc/roborobo-dashboard 8080:80
  ```

  then open <http://localhost:8080/>. It refreshes every minute.

- **What it is doing:** the log, or the status file.

  ```bash
  kubectl exec -n bizon deploy/roborobo-service -- cat /Users/bizon/.roborobo/status.md
  ```

- **A paused program** (a run left the repository dirty, `program check`
  failed, or a merge conflicted): fix the cause in the pod's clone, then

  ```bash
  kubectl exec -n bizon deploy/roborobo-service -- uv run --project /Users/bizon/Projects/experiments/research-programs program serve --resume sequel
  ```

- **Instructions and approvals** work as now: write and push from your Mac.
  The service merges them at its next look and runs the lines they name.
  Pull on your Mac to see what the runs did.

## How runs are kept apart from the keys

Each agent run is a Job of its own (`roborobo-<day>-<line>-...`), from the
service's image, on the service's node. It mounts only `Projects/` and uv's
cache from the volume and gets the Claude token; the service's keys are in a
memory-only folder no run mounts. `program submit`, `jobs` and `fetch` in a
run are sent to the service (`roborobo-api`), which runs them with its keys
for the run holding the current token. Watch runs with

```bash
kubectl get jobs -n bizon -l app.kubernetes.io/component=agent
```

## Limits

- Data recorded on your Mac (artifacts with host `local`) isn't visible in
  the pod. Runs here see the program's records, Hatteras artifacts, and
  data they make in the pod.
- The namespace has 8 CPUs in all, so long compute stays on Hatteras; the
  pod requests half a CPU.
