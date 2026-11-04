#!/usr/bin/env bash
# Agent Substrate enablement - quick demo (about 5 min live).
# omni-viper, kagent Enterprise 1.0.0-alpha12 + Agent Substrate 0.6.
#
#   export KUBECONFIG=...    # omnictl --omniconfig ~/.config/omni/config kubeconfig -c omni-viper <file>
#   ./demo.sh prep           # BEFORE the session: models + WorkerPool/Harness tier (about 30s)
#   ./demo.sh                # LIVE: template -> agent -> chat -> snapshot/fork -> model swap -> agents-as-code
#   ./demo.sh extras         # optional: inline Claude agent, move tiers, scheduled agents
#   ./demo.sh cleanup        # delete everything labelled demo=substrate-enablement
#
# Press Enter to advance (NONINTERACTIVE=1 to skip pauses).
set -euo pipefail
cd "$(dirname "$0")"
NS=kagent
SEL=demo=substrate-enablement
UI=${KAGENT_UI:-https://viper.maniak.lab:30501}

bold() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
run()  { printf '\033[1;33m$ %s\033[0m\n' "$*"; eval "$@"; }
show() { if command -v bat >/dev/null; then bat --style=plain --language=yaml "$@"; else cat "$@"; fi; }
pause(){ [ -n "${NONINTERACTIVE:-}" ] || read -r -p $'\n[enter] ' _; }
rev()  { kubectl -n $NS get agent "$1" -o jsonpath='{.status.latestSuccessfulRevision}' 2>/dev/null || true; }

# wait_ready <agent> [previous-revision]: Ready=True, desired==latest, and (if given) a NEW revision.
wait_ready() {
  local a=$1 old=${2:-} i s
  for i in $(seq 1 80); do
    s=$(kubectl -n $NS get agent "$a" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status} {.status.desiredRevision} {.status.latestSuccessfulRevision}' 2>/dev/null || true)
    set -- $s
    if [ "${1:-}" = True ] && [ -n "${3:-}" ] && [ "${2:-}" = "${3:-}" ] && [ "${3:-}" != "$old" ]; then
      echo "agent/$a Ready - golden snapshot ${3:0:12} after $((i*3))s"; return; fi
    sleep 3
  done
  echo "agent/$a not Ready after 4m"; kubectl -n $NS describe agent "$a" | tail -30; return 1
}

case "${1:-live}" in
cleanup)
  uv run agents_as_code.py --delete 2>/dev/null || true
  run "kubectl -n $NS delete agents,agenttemplates,harnesses,modelconfigs -l $SEL --ignore-not-found"
  run "kubectl -n $NS delete workerpools.ate.dev -l $SEL --ignore-not-found"
  ;;

prep)
  run "kubectl apply -f manifests/01-models.yaml -f manifests/03-tier.yaml"
  run "kubectl -n $NS rollout status deploy/kagent-demo --timeout=180s"
  run "kubectl -n $NS get modelconfigs,workerpools.ate.dev,harnesses"
  uv run --quiet agents_as_code.py --help >/dev/null 2>&1 || true   # warm uv's cache
  echo; echo "Prep done. Live demo: ./demo.sh"
  ;;

live)
  bold "0. The platform: Substrate worker pools + harnesses (pre-staged)"
  run "kubectl -n $NS get workerpools.ate.dev"
  run "kubectl -n $NS get harness demo -o yaml | sed -n '/^spec:/,/^status:/p'"
  pause

  bold "1. Models - one API, three backends"
  run "kubectl -n $NS get modelconfigs -l $SEL -o custom-columns=NAME:.metadata.name,MODEL:.spec.model,URL:.spec.openAI.baseUrl"
  pause

  bold "2. AgentTemplate (behaviour) + Agent (template + harness)"
  show manifests/02-agenttemplate.yaml manifests/04-agent.yaml; pause
  run "kubectl apply -f manifests/02-agenttemplate.yaml -f manifests/04-agent.yaml"
  wait_ready demo-k8s-helper
  run "kubectl -n $NS get agent demo-k8s-helper -o jsonpath='{range .status.conditions[*]}{.type}={.status}  {.message}{\"\\n\"}{end}'"
  pause

  bold "3. Chat - every session forks the golden snapshot into its own sandbox"
  echo "Open:  $UI/agents/$NS/demo-k8s-helper/new"
  echo "Ask:   How many pods are running in ate-system?"
  pause

  bold "4. Snapshot + fork - save a turn boundary, branch the conversation from it"
  echo "In the chat from step 3:"
  echo "  1. Click the snapshot button next to Send ("Take a snapshot")."
  echo "  2. Rename it, e.g. ate-system-baseline   (Snapshots page: $UI/snapshots)"
  echo "  3. Click Fork: a NEW chat is restored from the snapshot's runtime copy."
  echo "  4. Ask the fork: 'What did I ask you before, and what did you find?' - it remembers."
  echo "Talking point: a snapshot is the sandbox's runtime saved to S3 (rustfs), not just chat text."
  pause

  bold "5. Swap the model on the live agent (Spark -> OpenAI via agentgateway)"
  old=$(rev demo-k8s-helper)
  run "kubectl -n $NS patch agenttemplate demo-k8s-helper --type merge -p '{\"spec\":{\"modelConfig\":{\"name\":\"demo-openai-gpt-4o-mini\"}}}'"
  echo "New revision = new golden snapshot (~15s; longer if every worker is busy with a session). Existing sessions keep their revision."
  wait_ready demo-k8s-helper "$old"
  echo "Chat again (new session), then show the call in the Solo UI: http://172.16.10.59:30400/age/"
  pause

  bold "6. Agents as code - one Agent per model, from Python"
  run "sed -n '/^MODELS = {/,/^}/p' agents_as_code.py"
  run "uv run --quiet agents_as_code.py"
  run "kubectl -n $NS get agents -o custom-columns=NAME:.metadata.name,HARNESS:.spec.harnessRef.name,MODEL:.spec.template.inline.modelConfig.name"
  bold "Done. Extras: ./demo.sh extras   Clean up: ./demo.sh cleanup"
  ;;

extras)
  bold "A. Inline template on Bedrock Claude"
  show manifests/05-agent-inline.yaml; pause
  run "kubectl apply -f manifests/05-agent-inline.yaml"
  wait_ready demo-claude-helper
  echo "Chat:  $UI/agents/$NS/demo-claude-helper/new"
  pause

  bold "B. Move an agent to another tier - change harnessRef"
  old=$(rev demo-claude-helper)
  run "kubectl -n $NS patch agent demo-claude-helper --type merge -p '{\"spec\":{\"harnessRef\":{\"name\":\"high\"}}}'"
  wait_ready demo-claude-helper "$old"
  run "kubectl -n $NS get agents -o custom-columns=NAME:.metadata.name,HARNESS:.spec.harnessRef.name"

  bold "C. Scheduled agents - same agents, on a timer, publishing to GitHub"
  run "kubectl -n $NS get agents k8s-cve-checker fortigate-cve-watch fortigate"
  run "kubectl -n $NS get egresspolicies"
  echo "Schedules: $UI/schedules   (fortigate-daily-resource-usage 09:00, k8s-daily-cve-check 09:15,"
  echo "           fortigate-daily-fortiguard-cve 09:30, America/Toronto). Press Run on one to demo."
  echo "Reports land in https://github.com/sebbycorp/agent-summary/tree/main/reports"
  ;;

*) echo "usage: $0 [prep|extras|cleanup]"; exit 2 ;;
esac
