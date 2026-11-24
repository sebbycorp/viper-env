# kagent Enterprise + Agent Substrate (omni-viper)

This is the **Omni** path for kagent and Agent Substrate on cluster
`omni-viper`. It is **not** the older k3s / Docker install documented on
[sebbycorp/k8s-viper](https://github.com/sebbycorp/k8s-viper). Use that
repo only as a cross-link for historical k3s-viper notes or for FortiGate
manifest originals ([fortigate-agent-omni.md](fortigate-agent-omni.md)).

## Cluster / access

| Fact | Value |
| --- | --- |
| Cluster | `omni-viper` |
| Omni UI | https://maniak.na-west-1.omni.siderolabs.io/ |
| Node | `talos-cyv-ekb` @ `172.16.10.59` (moved from `172.16.10.178` on 2026-11-02) |
| Versions | Talos **v1.13.10**, Kubernetes **v1.36.4** (upgrade 2026-09-16) |
| Feature gates | `PodCertificateRequest` + `ClusterTrustBundle` (required for Agent Substrate) |
| kagent | Solo Enterprise for kagent **2026.11.2** (2026-11-23, fresh DB schemas; was 1.0.0-alpha12, alpha10, alpha3) |
| Agent Substrate | Substrate Enterprise **1.1.0-alpha1-7354c972** (Argo CD Application `substrate`, reinstalled 2026-11-23; was 0.6.0-b15382d4) |
| Argo CD | https://172.16.10.59:30443 and :30080 |
| kagent UI (HTTPS, preferred) | https://viper.maniak.lab:30501/ |
| kagent UI (HTTP, fallback) | http://172.16.10.59:30500 |
| Host placement | Should live on Proxmox **iceman** (`172.16.10.5`); guests are migrating off **viper** (`172.16.10.6`) |

### Namespaces

| Namespace | Role |
| --- | --- |
| `kagent` | kagent controller, agents, MCP servers, WorkerPools |
| `ate-system` | Agent Substrate (ATE) |
| `podcertificate-controller-system` | Substrate pod-certificate signer (`substrate-podcert`) |
| `postgres` | In-cluster Postgres for kagent + Substrate (lab grade, **no backups**) |

Kubeconfigs and Omni service-account tokens stay on the operator
workstation. **Never commit them.**

## kagent UI (HTTPS)

**Primary URL:** https://viper.maniak.lab:30501/

hosts / DNS (required for the hostname URL):

```text
172.16.10.59 viper.maniak.lab
```

Use **`viper.maniak.lab`**, not `kagent.viper.lab` or `kagent.maniak.lab`.
The live `kagent-ui-tls` certificate has SANs DNS `viper.maniak.lab` and IP
`172.16.10.178`, which is the node's **old** address (checked 2026-11-24, valid
to 2027-09-17). The hostname URL is therefore fine, but
https://172.16.10.59:30501/ gives a name mismatch until infra re-issues the
cert with the new IP SAN. Do not re-issue it from this repo.

This is an **intentional lab self-signed / internal CA**, not Let’s
Encrypt. The UI needs a browser **secure context** so
`crypto.randomUUID` works (for example `/schedules/new`). A
`crypto.randomUUID` polyfill on plain HTTP was a temporary workaround;
HTTPS is the proper fix.

HTTP NodePort **30500** remains as a fallback (plain HTTP; not a
secure context). Prefer HTTPS for UI features that need a secure
context. The chart cannot pin a nodePort, so the chart Service
`kagent-ui` stays ClusterIP and
[apps/kagent/ui-nodeport.yaml](../apps/kagent/ui-nodeport.yaml) pins 30500.

### Already live (do not redeploy)

These objects are already on the cluster. This page is operator docs
only — do not reinstall cert-manager or re-issue the lab CA from this
repo.

| Object | Where / what |
| --- | --- |
| cert-manager | Namespace `cert-manager` |
| ClusterIssuer | `selfsigned-cluster-issuer` |
| Lab CA Certificate | `kagent-lab-ca` |
| Lab CA ClusterIssuer | `kagent-lab-ca-issuer` |
| UI Certificate | `kagent-ui-tls` in namespace `kagent` (secret `kagent-ui-tls`). Live SANs: DNS `viper.maniak.lab`, IP `172.16.10.178` (old node IP; should become `172.16.10.59` when infra re-issues) |
| TLS front | Deployment `kagent-ui-tls` (nginx) + Service NodePort **30501** terminating TLS → `kagent-ui:8080` |
| HTTP front | Service `kagent-ui-nodeport` NodePort **30500** (GitOps, `apps/kagent/ui-nodeport.yaml`) |

### Trust the lab CA

Browsers warn on first visit because this is a lab CA, not a public CA.

1. **Browser exception** — accept the warning on
   https://viper.maniak.lab:30501/ (enough for most lab use).
2. **Import the lab CA** — export the public CA cert from the cluster
   and add it to the OS / browser trust store:

```bash
kubectl -n cert-manager get secret kagent-lab-ca-secret \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > kagent-lab-ca.crt
```

That file is a **public CA certificate** for trust, not a private key.
Do not commit it (`.gitignore` already excludes `*.crt`). Do not export
or commit TLS private keys.

## Install (Argo CD)

kagent 1.0.0-alpha10 was a re-platform of alpha3: the API group moved from
`kagent.dev` to **`api.kagent.dev/v1alpha3`**, Postgres is no longer bundled
and Agent Substrate is no longer a subchart.

Argo CD installs the charts (CRDs, Substrate, kagent) from
[values/](../values/). The licenses and connection strings come from Vault
through ESO. See [agentic-stack.md](agentic-stack.md) for the Applications,
the Vault layout and the one-time move off the hand-installed Helm releases.
Postgres (StatefulSet `postgres/postgres`, `pgvector/pgvector:pg16`, DBs
`kagent` and `substrate`) is still created by hand and has no backups.

### Upgrading kagent (alpha10 → alpha12, 2026-10-30)

- alpha11 split the database login: the controller takes
  `kagent-postgres-owner` (migrations, `SET ROLE kagent_owner`) and
  `kagent-postgres-readwrite` (`SET ROLE kagent_readwrite`). The old
  `connectionStringSecretRef` is ignored silently. Both Secrets carry the
  `kagent_user` login, so Vault needed no new value.
- The pre-1.0 migrations are compacted baselines ("databases must be
  recreated when it changes"), so an alpha bump needs fresh `kagent` and
  `enterprise` schemas. Sessions, memory and Postgres evals are lost; CRDs are
  not. Recreate them with the two `setup.sql` scripts from
  `solo-io/enterprise-kagent` at the release tag, as `postgres`:
  `go/core/pkg/postgressetup/setup.sql` (`kagent_owner_user=kagent_user`,
  its existing password; `kagent_readwrite_user=kagent_readwrite_user`, empty
  password), then `GRANT kagent_readwrite TO kagent_user` and
  `GRANT USAGE ON SCHEMA extensions TO kagent_readwrite` (pgvector), then
  `enterprise/services/solo-enterprise/migrations/postgres/setup/setup.sql`
  (`agentevals_user=`).
- Substrate must match the Substrate version in the kagent release's `go/go.mod` (alpha12 → 0.6.0).
- The ApplicationSet app `kagent` (Harnesses, WorkerPools) has no auto-sync:
  sync it by hand after a merge.
- Stop the old controller before the new one migrates: pause auto-sync on
  `kagent-enterprise` and scale `kagent-controller` to 0. During a rolling
  update the old pod keeps running while the new one isn't Ready, and on
  2026-10-30 alpha10 re-migrated the fresh schemas, so they had to be wiped twice.
- alpha12 dropped the `controller.substrate.enabled` alias. Without
  `controller.substrateClientCertificate.enabled: true` the controller dials
  ate-api with no CA or client cert and fails with "context deadline exceeded".
- The ClickHouse warning "schema version 11 is ahead of embedded 1" is expected:
  alpha12 compacted the ClickHouse baseline. Existing data stays and the
  controller runs in compatibility mode.

### Upgrading Substrate (0.5 → 0.6, 2026-10-30)

Values in `values/substrate.yaml`: `ateApi.authorization.bootstrapOwners` must
list `spiffe://cluster.local/ns/kagent/sa/kagent-controller`, because ate-api
authorizes every call through OpenFGA; without it agents fail with
`RuntimePreparationFailed (PermissionDenied)`. Also set
`preview: [PerTemplateSnapshotLocation]`, because the Harnesses set their own
snapshot location. `kagent install` sets both. Other changes:

- The worker image is renamed to `ateworker-gvisor`. WorkerPools take
  `spec.sandboxClasses: [{name: gvisor, configRef: {name: gvisor-default}}]`
  instead of `sandboxClass`.
- Stale Helm field ownership: the pre-Argo Helm releases' `helm` field
  manager keeps fields that 0.6 moved, such as `ATE_STORAGE_BACKEND` and the
  `AWS_*` variables on atelet and ate-api-server, and atelet's old 8085 `grpc`
  port. 0.6 refuses to start with them. Remove the `helm` entries from
  `managedFields` on Argo-tracked workloads, then drop the stale fields once.
- Reset by hand (not in git):
  - Recreate `ate-system/actor-id-jwt-pool` with
    `kubectl ate admin make-jwt-pool` (v0.6.0), which now also needs a `jwks` key.
  - Remove `actorIdentityJWTProvider` from the `ate-api-authentication`
    ConfigMap.
  - Delete `/var/lib/ate/image-cache` on the node (its layout changed from v1 to v2).
  - Recreate the `substrate` schema with the v0.6.0
    `pkg/postgressetup/setup.sql`, because 0.5 worker rows don't parse in 0.6.
- Argo kept a stale schema for the changed CRDs (`ComparisonError ... field not
  declared in schema`) until the `argocd-application-controller` restarted.

### Upgrading kagent and Substrate (alpha12 / 0.6 → 2026.11.2 / 1.1, 2026-11-23)

kagent 2026.11.2 is built against Substrate 1.1.0-alpha1 (chart
`1.1.0-alpha1-7354c972`; the kagent release notes list the pair). Substrate has
no upgrade path from 0.6 ("reinstall"), its CRDs moved from `ate.dev/v1alpha1`
to `ate.dev/v1`, and kagent's baseline migration changed again, so this was a
reinstall of Substrate and fresh `kagent`, `enterprise` and `substrate` schemas.
From 2026.11.x on, the documented in-place upgrade keeps the data
(<https://docs.solo.io/kagent/latest/operations/upgrade/>).

What was done, in order:

1. Back up: `pg_dump --format=custom` of `kagent` and `substrate`, every
   kagent/`ate.dev` object, and the `kagent.scheduled_run` rows. Schedules live
   only in Postgres; their `data` column is the `ScheduledRun` protobuf.
   `kubectl exec ... pg_dump > file` and `kubectl cp` both truncated the 20 MB
   dump, so dump inside the pod and pull it in 2 MB `dd` chunks, then compare
   sha256.
2. Pause auto-sync on `omni-viper-root` and on `kagent-crds`,
   `kagent-enterprise`, `substrate`, `substrate-crds`, `substrate-podcert`
   (remove `spec.syncPolicy.automated`). With the root paused, set each child's
   `targetRevision` by hand and sync it with an `operation` patch. Merge the PR.
3. Scale `kagent-controller` to 0. Delete the WorkerPools, scale the
   `ate-system` Deployments to 0 and delete the `atelet` DaemonSet.
4. Delete the `workerpools`, `sandboxconfigs` and `csidriverconfigs` `ate.dev`
   CRDs. `SandboxConfig/gvisor-default` keeps a
   `sandboxconfig.ate.dev/workerpool-protection` finalizer that nothing removes
   once ate-controller is down; clear it by hand.
5. Recreate the three schemas as `postgres` without touching roles or
   passwords: `DROP SCHEMA x CASCADE; CREATE SCHEMA x AUTHORIZATION <owner>;`
   then the `REVOKE`/`GRANT USAGE`/four `ALTER DEFAULT PRIVILEGES` statements
   from `setup.sql`. Running `setup.sql` itself resets the login passwords to
   its variables, and an empty one clears the password.
6. On the node, delete `/var/lib/ate/image-cache` (required), plus `actors`
   and `snapshot-cache` (orphans of the wiped database).
7. Sync `substrate-podcert`, `substrate-crds`, `substrate`. The sync fails on
   the completed `rustfs-bucket-init` Job (immutable); delete the Job and sync
   again.
8. Sync `kagent-crds`, `kagent-enterprise`, then `kagent` (no auto-sync). All
   Agents went Ready in about a minute.
9. Put `automated` back on `omni-viper-root`; it restores the children.
10. Recreate the schedules through `ScheduledRunService/CreateScheduledRun`
    (gRPC-web on controller 8083, token from the autoauth sidecar on 5556).

Values that changed (`kagent install` 2026.11.2 sets the same):

- `values/substrate.yaml`: `preview` adds `GoldenEgressPolicy`;
  `bootstrapOwners` adds `system:serviceaccount:ate-system:ate-client`;
  `atenetRouter.ingressAllowedSPIFFEIDs` lists the kagent controller, because
  the router serves mTLS on 443 and kagent now dials
  `https://atenet-router.ate-system.svc:443`.
- Harnesses carry no `workload.image`: the controller uses the release's
  built-in runtime images (`config.harnessImages`, pinned by digest).
- The chart no longer deploys the `solo-enterprise-k8sobjects` collector by
  default, and it drops the `runtimes.kagent-enterprise.solo.io` CRD (still on
  the cluster, because `kagent-crds` does not prune).

Left for later: atelet still ships inside the `substrate` release
(`atelet.enabled`, deprecated). New installs use the separate
`substrate-atelet` chart, which the documented upgrade order needs (podcert,
atelet, WorkerPool image, CRDs, substrate, kagent).

## Agent Substrate

| Preference | Value |
| --- | --- |
| Charts | `oci://us-docker.pkg.dev/solo-public/enterprise-substrate-helm` 1.1.0-alpha1-7354c972 |
| Worker image | `us-docker.pkg.dev/solo-public/substrate-enterprise/ateworker-gvisor:v1.1.0-alpha1-7354c972` (renamed from `ateom-gvisor` in 0.6) |
| WorkerPools | `kagent-default` (chart), `kagent-high`, `kagent-coding` (Argo) in namespace `kagent` |
| ate-api | `dns:///api.ate-system.svc:443` |

The old from-source install (`hack/install-ate.sh`, ko images on the
node registry `127.0.0.1:5000`) is retired; do not reference
`127.0.0.1:5000` images any more.

### Agents, Harnesses and tiers

In alpha10 an **Agent** (`api.kagent.dev/v1alpha3`) pairs one Harness
(`spec.harnessRef`) with one AgentTemplate (`spec.template.ref`). The
alpha3 `kagent.dev/harness` label and `Harness.spec.allowedAgentTemplates`
are gone. Substrate has no priority queue: a Harness points at exactly
one WorkerPool, so a "tier" is a Harness + its own WorkerPool. Manifests:
[apps/kagent](../apps/kagent/) (Argo CD app `kagent`) and
[apps/remarkable-diary/k8s-assistant.yaml](../apps/remarkable-diary/k8s-assistant.yaml).

| Tier | Harness | WorkerPool | Workers | PriorityClass | Agents |
| --- | --- | --- | --- | --- | --- |
| High | `high` | `kagent-high` | 2 (reserved) | `substrate-high` (100000, `preemptionPolicy: Never`) | `k8s-assistant-priority` |
| Low | `default` | `kagent-default` (chart `substrateWorkerPool.replicas`) | 3 | none | `k8s-assistant`, `fortigate` |
| Coding | `coding` (Codex runtime) | `kagent-coding` | 3 | none | `codex-coder` (ModelConfig `codex-gpt-5-4`) |

To move an agent to another tier, change `spec.harnessRef` on its Agent.
Worker pods request no CPU, so the extra pools fit on the CPU-bound node;
the PriorityClass only decides eviction order and never preempts other pods.

Harnesses set no `workload.image`. Since 2026.11.x the release ships its own
runtime images (`golang-adk`, `codex-harness`, `claude-harness` in
`us-docker.pkg.dev/solo-public/kagent-enterprise`, pinned by digest in the
controller's `config.harnessImages`), so the runtime always matches the
controller. Codex speaks the OpenAI
Responses API, so ModelConfig `codex-gpt-5-4` uses `apiFormat: responses`
and calls the in-cluster agentgateway over HTTP (`/openai/v1`). The
`/openai` backend has `policies.ai.routes` so `/v1/responses` isn't parsed
as Chat Completions.

## Models (kagent)

| ModelConfig | Provider | Notes |
| --- | --- | --- |
| `default-model-config` | OpenAI-compatible (chart `providers.openAI`) | Spark: Service `kagent/dgx-spark` → `http://172.16.10.173:8000/v1`, served model `qwen3.8-flash-next` = `Mia-AiLab/Qwen3.8-Flash-Next-NVFP4` (DGX Spark) |
| `codex-gpt-5-4` | OpenAI `gpt-5.4` via agentgateway | Gateway injects the real OpenAI key (Vault `secret/lab/oss-gateways/openai` or `secret/lab/openai`) |

Wire keys with ExternalSecret → Kubernetes Secret. Do not paste key
values into this repo. Example Vault *path* (not a value):

```text
secret/lab/oss-gateways/openai
secret/lab/openai
```

If the ESO AppRole policy is scoped to `secret/data/lab/viper/*` only,
either copy a reference object under `secret/lab/viper/...` or extend
the policy out-of-band. Do not commit AppRole ids.

## Agent plans on this cluster

| Agent | Namespace | Docs | Status (docs write) |
| --- | --- | --- | --- |
| TypeSafe / Jev | `kagent` (not `ate-system`) | [typesafe-jev.md](typesafe-jev.md) | Vault key present; apply when cluster Ready |
| FortiGate | `kagent`, Harness `default` (WorkerPool `kagent-default`) | [fortigate-agent-omni.md](fortigate-agent-omni.md) | Applied by hand as `api.kagent.dev/v1alpha3` RemoteMCPServer `fortigate-mcp` + AgentTemplate/Agent `fortigate` (not in Argo) |

## Related surfaces

- Lab Homepage cards: [lab-homepage.md](lab-homepage.md)
- GitOps / Vault model: [root README](../README.md)
- Cluster overlay notes: [clusters/omni-viper](../clusters/omni-viper/README.md)
