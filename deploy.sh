#!/usr/bin/env bash
# deploy.sh — build Myco, push it to Scaleway Container Registry and install it on a
# Scaleway Kubernetes Kapsule cluster with Helm. Same command for first install and updates.
#
#   cp .env.example .env    # fill sources + Scaleway settings
#   ./deploy.sh
#
# What it does:
#   1. Reads .env (variables already exported in the shell win).
#   2. Generates MYCO_AGENT__DEFAULT__KEY into .env if it is missing.
#   3. Optionally installs the kubeconfig for SCW_K8S_CLUSTER_ID with the scw CLI.
#   4. Builds a linux/amd64 image, pushes :<git-sha> and :latest to rg.<region>.scw.cloud.
#   5. Creates the namespace, the registry pull secret and the "<release>-env" Secret
#      from every MYCO_* variable in .env.
#   6. Ships ./config as a ConfigMap (or uses git-sync, see DEPLOY_GIT_REPO) and runs
#      helm upgrade --install --wait.
#
# Overrides (environment or .env):
#   SKIP_BUILD=1                 helm only; redeploys the running image tag
#   IMAGE_TAG=abc123             deploy a specific tag
#   IMAGE_PLATFORM=linux/arm64   build for ARM node pools
#   DEPLOY_DRY_RUN=1             render the manifests instead of installing them
# Extra arguments are passed to helm, e.g.  ./deploy.sh --set replicaCount=1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
CHART_DIR="$ROOT/helm/myco"

die() { echo "error: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }
step() { echo "==> $*" >&2; }
info() { echo "$*" >&2; }

# ---------------------------------------------------------------------------- .env
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found. Run: cp .env.example .env"

# Print KEY=VALUE lines from .env with quotes removed, without sourcing it as shell.
parse_env() {
  python3 - "$1" <<'PY'
import re, shlex, sys
key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
with open(sys.argv[1], encoding="utf-8") as f:
    for n, raw in enumerate(f, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            sys.exit(f"{sys.argv[1]}:{n}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key_re.match(key):
            sys.exit(f"{sys.argv[1]}:{n}: invalid variable name {key!r}")
        lexer = shlex.shlex(value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        parts = list(lexer)
        value = " ".join(parts) if parts else ""
        if "\n" in value:
            sys.exit(f"{sys.argv[1]}:{n}: multi-line values are not supported")
        print(f"{key}={value}")
PY
}

ENV_LINES="$(parse_env "$ENV_FILE")"

env_get() {  # env_get KEY [default] — shell environment first, then .env
  local key="$1" default="${2:-}" value
  if [[ -n "${!key:-}" ]]; then printf '%s' "${!key}"; return; fi
  value="$(printf '%s\n' "$ENV_LINES" | awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); v = $0 } END { print v }')"
  printf '%s' "${value:-$default}"
}

# ---------------------------------------------------------------------------- settings
SCW_REGION="$(env_get SCW_REGION fr-par)"
SCW_SECRET_KEY="$(env_get SCW_SECRET_KEY)"
SCW_REGISTRY_NAMESPACE="$(env_get SCW_REGISTRY_NAMESPACE myco)"
SCW_K8S_CLUSTER_ID="$(env_get SCW_K8S_CLUSTER_ID)"
IMAGE_REPOSITORY="$(env_get IMAGE_REPOSITORY "rg.${SCW_REGION}.scw.cloud/${SCW_REGISTRY_NAMESPACE}/myco")"
IMAGE_PLATFORM="$(env_get IMAGE_PLATFORM linux/amd64)"
IMAGE_TAG="$(env_get IMAGE_TAG)"
SKIP_BUILD="$(env_get SKIP_BUILD)"

RELEASE="$(env_get DEPLOY_RELEASE myco)"
NAMESPACE="$(env_get DEPLOY_NAMESPACE myco)"
KUBE_CONTEXT="$(env_get DEPLOY_KUBE_CONTEXT)"
HOST="$(env_get DEPLOY_HOST)"
INGRESS_CLASS="$(env_get DEPLOY_INGRESS_CLASS nginx)"
CLUSTER_ISSUER="$(env_get DEPLOY_CLUSTER_ISSUER letsencrypt-prod)"
CONFIG_DIR="$(env_get DEPLOY_CONFIG_DIR "$ROOT/config")"
GIT_REPO="$(env_get DEPLOY_GIT_REPO)"
GIT_REF="$(env_get DEPLOY_GIT_REF main)"
GIT_PATH="$(env_get DEPLOY_GIT_PATH)"
GIT_SECRET="$(env_get DEPLOY_GIT_SECRET)"
DRY_RUN="$(env_get DEPLOY_DRY_RUN)"

PULL_SECRET_NAME="scaleway-registry"
# Same naming rule as the chart's fullname helper.
if [[ "$RELEASE" == *myco* ]]; then FULLNAME="$RELEASE"; else FULLNAME="$RELEASE-myco"; fi
ENV_SECRET_NAME="${FULLNAME}-env"
REGISTRY_HOST="${IMAGE_REPOSITORY%%/*}"
REGISTRY_LOGIN="${IMAGE_REPOSITORY%/*}"  # Scaleway logs in to host + namespace

for tool in docker helm kubectl python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
if [[ "$SCW_SECRET_KEY" == SCW* ]]; then
  die "SCW_SECRET_KEY looks like an access key (SCW...). Use the secret key (a UUID) from Scaleway console → IAM → API keys."
fi
[[ -n "$SCW_SECRET_KEY" || -n "$SKIP_BUILD" ]] || warn "SCW_SECRET_KEY is empty: docker push and image pulls from a private registry will fail."

# ---------------------------------------------------------------------------- default agent key
if [[ -z "$(env_get MYCO_AGENT__DEFAULT__KEY)" ]]; then
  step "Generate MYCO_AGENT__DEFAULT__KEY into $ENV_FILE"
  # Fill an existing empty "MYCO_AGENT__DEFAULT__KEY=" line, or append one.
  python3 - "$ENV_FILE" <<'PY'
import re, secrets, sys
path = sys.argv[1]
key = "myco_" + secrets.token_urlsafe(32)
text = open(path, encoding="utf-8").read()
pattern = re.compile(r"^(export\s+)?MYCO_AGENT__DEFAULT__KEY=\s*(['\"]{2})?\s*$", re.M)
if pattern.search(text):
    text = pattern.sub(f"MYCO_AGENT__DEFAULT__KEY={key}", text, count=1)
else:
    text = text.rstrip("\n") + f"\n\n# Default agent key (generated by deploy.sh)\nMYCO_AGENT__DEFAULT__KEY={key}\n"
open(path, "w", encoding="utf-8").write(text)
PY
  ENV_LINES="$(parse_env "$ENV_FILE")"
  info "    The key is in .env; give it to the agent that should use the default account."
fi

printf '%s\n' "$ENV_LINES" | grep -q '^MYCO_SOURCE__' || warn "no MYCO_SOURCE__* variables in .env: Myco will start without tools."

# ---------------------------------------------------------------------------- cluster access
if [[ -n "$SCW_K8S_CLUSTER_ID" ]]; then
  command -v scw >/dev/null 2>&1 || die "SCW_K8S_CLUSTER_ID is set but the scw CLI is not installed"
  step "Install kubeconfig for Kapsule cluster $SCW_K8S_CLUSTER_ID"
  scw k8s kubeconfig install "$SCW_K8S_CLUSTER_ID" region="$SCW_REGION" >/dev/null
fi
KUBECTL=(kubectl)
HELM=(helm)
if [[ -n "$KUBE_CONTEXT" ]]; then
  KUBECTL+=(--context "$KUBE_CONTEXT")
  HELM+=(--kube-context "$KUBE_CONTEXT")
fi
CURRENT_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context 2>/dev/null || true)}"
[[ -n "$CURRENT_CONTEXT" || -n "$DRY_RUN" ]] || die "no kubectl context. Set SCW_K8S_CLUSTER_ID or DEPLOY_KUBE_CONTEXT."
if [[ -n "$CURRENT_CONTEXT" && -z "$SCW_K8S_CLUSTER_ID" && -z "$KUBE_CONTEXT" && "$CURRENT_CONTEXT" != admin@* ]]; then
  warn "deploying to the current kubectl context '$CURRENT_CONTEXT', which does not look like Scaleway Kapsule."
  warn "Set SCW_K8S_CLUSTER_ID or DEPLOY_KUBE_CONTEXT in .env to pin the target cluster."
fi

# ---------------------------------------------------------------------------- image tag
if [[ -z "$IMAGE_TAG" && -n "$SKIP_BUILD" ]]; then
  live="$("${KUBECTL[@]}" -n "$NAMESPACE" get deploy -l "app.kubernetes.io/instance=$RELEASE" \
    -o jsonpath='{.items[0].spec.template.spec.containers[0].image}' 2>/dev/null || true)"
  [[ "$live" == *:* ]] && IMAGE_TAG="${live##*:}"
fi
if [[ -z "$IMAGE_TAG" ]]; then
  if sha="$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null)"; then
    IMAGE_TAG="$sha"
    [[ -z "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]] || IMAGE_TAG="${sha}-dirty-$(date +%H%M%S)"
  else
    IMAGE_TAG="build-$(date +%Y%m%d%H%M%S)"
  fi
fi

info "Release:   $RELEASE  namespace: $NAMESPACE  context: ${CURRENT_CONTEXT:-none}"
info "Image:     $IMAGE_REPOSITORY:$IMAGE_TAG  ($IMAGE_PLATFORM)"
info "Ingress:   ${HOST:-disabled (port-forward only)}"
info "Config:    ${GIT_REPO:+git-sync $GIT_REPO@$GIT_REF/${GIT_PATH}}${GIT_REPO:-$CONFIG_DIR}"

# ---------------------------------------------------------------------------- build & push
if [[ -z "$SKIP_BUILD" ]]; then
  if [[ -n "$SCW_SECRET_KEY" && -z "$DRY_RUN" ]]; then
    step "Log in to $REGISTRY_LOGIN"
    printf '%s' "$SCW_SECRET_KEY" | docker login "$REGISTRY_LOGIN" -u nologin --password-stdin >/dev/null
    if command -v scw >/dev/null 2>&1 \
      && ! scw registry namespace list name="$SCW_REGISTRY_NAMESPACE" region="$SCW_REGION" -o json 2>/dev/null | grep -q '"id"'; then
      warn "registry namespace '$SCW_REGISTRY_NAMESPACE' not found in $SCW_REGION (or scw is not configured)."
      echo "    Create it once: scw registry namespace create name=$SCW_REGISTRY_NAMESPACE region=$SCW_REGION is-public=false" >&2
    fi
  fi
  step "Build $IMAGE_REPOSITORY:$IMAGE_TAG"
  docker build >&2 --platform "$IMAGE_PLATFORM" -t "$IMAGE_REPOSITORY:$IMAGE_TAG" -t "$IMAGE_REPOSITORY:latest" "$ROOT"
  if [[ -z "$DRY_RUN" ]]; then
    step "Push"
    docker push "$IMAGE_REPOSITORY:$IMAGE_TAG"
    docker push "$IMAGE_REPOSITORY:latest"
  fi
else
  step "SKIP_BUILD=1: deploying existing tag $IMAGE_TAG"
fi

# ---------------------------------------------------------------------------- secrets & values
WORK="$(mktemp -d "${TMPDIR:-/tmp}/myco-deploy.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Only MYCO_* variables reach the cluster; the chart sets host, port and config dir itself.
# When a key appears twice in .env the last value wins, as in env_get.
printf '%s\n' "$ENV_LINES" | grep '^MYCO_' | grep -vE '^MYCO_(HOST|PORT|CONFIG_DIR)=' \
  | awk -F= '{ last[$1] = $0; if (!($1 in seen)) { seen[$1] = 1; order[++n] = $1 } } END { for (i = 1; i <= n; i++) print last[order[i]] }' \
  > "$WORK/myco.env" || true
empty="$(grep -E '^MYCO_[A-Z0-9_]+=$' "$WORK/myco.env" | cut -d= -f1 | tr '\n' ' ' || true)"
[[ -z "$empty" ]] || warn "empty in .env (remove unused source blocks, or Myco will refuse to start): $empty"
ENV_CHECKSUM="$( (sha256sum 2>/dev/null || shasum -a 256) < "$WORK/myco.env" | cut -c1-16)"

VALUES="$WORK/values.yaml"
{
  echo "image:"
  echo "  repository: \"$IMAGE_REPOSITORY\""
  echo "  tag: \"$IMAGE_TAG\""
  echo "  pullPolicy: IfNotPresent"
  [[ -z "$SCW_SECRET_KEY" ]] || printf 'imagePullSecrets:\n  - name: %s\n' "$PULL_SECRET_NAME"
  echo "existingSecret: \"$ENV_SECRET_NAME\""
  echo "podAnnotations:"
  echo "  myco.dev/env-checksum: \"$ENV_CHECKSUM\""  # rolls the pod when .env changes
  if [[ -n "$HOST" ]]; then
    echo "ingress:"
    echo "  enabled: true"
    echo "  className: \"$INGRESS_CLASS\""
    echo "  host: \"$HOST\""
    echo "  annotations:"
    echo "    cert-manager.io/cluster-issuer: \"$CLUSTER_ISSUER\""
    echo "    nginx.ingress.kubernetes.io/proxy-buffering: \"off\""
    echo "    nginx.ingress.kubernetes.io/proxy-read-timeout: \"3600\""
    echo "    nginx.ingress.kubernetes.io/proxy-send-timeout: \"3600\""
  fi
  if [[ -n "$GIT_REPO" ]]; then
    echo "config:"
    echo "  source: gitSync"
    echo "gitSync:"
    echo "  repo: \"$GIT_REPO\""
    echo "  ref: \"$GIT_REF\""
    echo "  path: \"$GIT_PATH\""
    echo "  existingSecret: \"$GIT_SECRET\""
  else
    [[ -d "$CONFIG_DIR" ]] || die "config folder $CONFIG_DIR not found"
    echo "config:"
    echo "  source: configMap"
    echo "  files:"
    while IFS= read -r file; do
      rel="${file#"$CONFIG_DIR"/}"
      echo "    \"$rel\": |"
      sed 's/^/      /' "$file"
      echo
    done < <(find "$CONFIG_DIR" -type f \( -name '*.yaml' -o -name '*.yml' \) | LC_ALL=C sort)
  fi
} > "$VALUES"

if [[ -n "$DRY_RUN" ]]; then
  step "DEPLOY_DRY_RUN=1: rendering manifests only"
  "${HELM[@]}" template "$RELEASE" "$CHART_DIR" --namespace "$NAMESPACE" -f "$VALUES" "$@"
  exit 0
fi

step "Namespace $NAMESPACE"
"${KUBECTL[@]}" create namespace "$NAMESPACE" --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null

if [[ -n "$SCW_SECRET_KEY" ]]; then
  step "Image pull secret $PULL_SECRET_NAME"
  "${KUBECTL[@]}" -n "$NAMESPACE" create secret docker-registry "$PULL_SECRET_NAME" \
    --docker-server="$REGISTRY_HOST" --docker-username=nologin --docker-password="$SCW_SECRET_KEY" \
    --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null
fi

step "Secret $ENV_SECRET_NAME ($(wc -l < "$WORK/myco.env" | tr -d ' ') MYCO_* variables)"
"${KUBECTL[@]}" -n "$NAMESPACE" create secret generic "$ENV_SECRET_NAME" --from-env-file="$WORK/myco.env" \
  --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null

step "helm upgrade --install $RELEASE"
"${HELM[@]}" upgrade --install "$RELEASE" "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  -f "$VALUES" \
  --wait --timeout 5m \
  "$@"

echo
"${KUBECTL[@]}" -n "$NAMESPACE" get pods -l "app.kubernetes.io/instance=$RELEASE"
echo
if [[ -n "$HOST" ]]; then
  echo "MCP endpoint: https://$HOST/mcp"
  echo "Health:       https://$HOST/health"
  echo "DNS: point $HOST at the ingress load balancer:"
  echo "  kubectl get svc -A -l app.kubernetes.io/component=controller"
else
  echo "No DEPLOY_HOST set; reach Myco with:"
  echo "  kubectl -n $NAMESPACE port-forward svc/$FULLNAME 8080:80   # then http://127.0.0.1:8080/mcp"
fi
echo "Agents send: Authorization: Bearer <MYCO_AGENT__<NAME>__KEY from .env>"
