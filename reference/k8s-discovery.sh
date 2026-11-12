#!/usr/bin/env bash
# =============================================================================
# k8s-discovery.sh - read-only, anonymised Kubernetes discovery for migration
#                    and capacity planning. Bash + kubectl only.
#
# Requires : bash 4+, kubectl, awk (mawk/gawk/busybox), sed, grep, sort, tr,
#            base64, gzip, tar, and sha256sum (or shasum / openssl).
#            No Python, no jq, no internet access.
#
# Read-only: only `kubectl get`, `kubectl top`, `kubectl api-resources`,
#            `kubectl get --raw` (/metrics, node stats). Nothing is created,
#            changed or exec'd.
#
# Privacy  : raw kubectl output stays in a temporary folder inside
#            --private-dir and is deleted at the end (unless --keep-raw).
#            The shareable output never contains IPs, hostnames, Secret or
#            ConfigMap values, env vars, annotation/label values, NFS paths or
#            private registry hosts. Names (namespaces, workloads, PVCs, nodes,
#            Helm releases, AD groups ...) are replaced by salted SHA-256 hashes.
#            The salt and the hash->name map stay in --private-dir.
#
# Usage    : ./k8s-discovery.sh --help
# =============================================================================
set -u -o pipefail
VERSION="1.0.0"

# ----------------------------------------------------------------------------
# Arguments
# ----------------------------------------------------------------------------
CONTEXTS=(); ALL_CTX=0; KCFG=""; KUBECTL="kubectl"; OUT="discovery-out"
PRIV="discovery-private"; SALT="${K8S_DISCOVERY_SALT:-}"; SENS=(); VOL=0
NOHELM=0; SKIPCR=0; STRICT=0; CLEAR=0; TO=60; NOTAR=0; KEEPRAW=0; QUIET=0

usage() {
cat <<'EOF'
k8s-discovery.sh - read-only, anonymised cluster discovery (bash + kubectl)

  --context NAME            kubeconfig context (repeatable; default: current)
  --all-contexts            run against every context in the kubeconfig
  --kubeconfig FILE         kubeconfig to use
  --kubectl BIN             kubectl binary (default: kubectl)
  --out DIR                 shareable output folder (default: discovery-out)
  --private-dir DIR         salt + name map + temp files (default: discovery-private)
  --salt VALUE              hash salt (or env K8S_DISCOVERY_SALT); reused from
                            <private-dir>/salt.txt when present
  --sensitive-domain STR    string that must never appear in the output, e.g. a
                            DNS suffix or organisation name (repeatable)
  --volume-usage            real used bytes per PVC from kubelet stats
                            (needs get on nodes/proxy)
  --no-helm                 do not read Helm release secrets
  --skip-cr-count           do not count instances per CRD (faster)
  --strict                  also hash image names, chart names, CRD names/groups,
                            StorageClass names
  --clear-names             do NOT hash names (internal use only)
  --timeout SEC             per-request timeout (default 60)
  --no-tar                  do not create the tar.gz
  --keep-raw                keep raw kubectl output in <private-dir>/raw-* (NOT shareable)
  --print-rbac              print a read-only ClusterRole and exit
  --quiet                   less progress output
EOF
}

print_rbac() {
cat <<'EOF'
# Read-only role for k8s-discovery.sh
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: migration-discovery-readonly
rules:
- apiGroups: ["*"]
  resources: ["*"]          # includes nodes/proxy (volume stats) and secrets (Helm metadata only)
  verbs: ["get", "list"]
- nonResourceURLs: ["/metrics", "/version", "/api", "/api/*", "/apis", "/apis/*"]
  verbs: ["get"]
# If Secrets must not be read: use --no-helm and replace "*" resources with an
# explicit list that leaves out secrets.
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: migration-discovery-readonly
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: migration-discovery-readonly
subjects:
- kind: User                 # or Group / ServiceAccount
  name: CHANGE-ME
  apiGroup: rbac.authorization.k8s.io
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --context) CONTEXTS+=("$2"); shift 2 ;;
    --all-contexts) ALL_CTX=1; shift ;;
    --kubeconfig) KCFG="$2"; shift 2 ;;
    --kubectl) KUBECTL="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --private-dir) PRIV="$2"; shift 2 ;;
    --salt) SALT="$2"; shift 2 ;;
    --sensitive-domain) SENS+=("$2"); shift 2 ;;
    --volume-usage) VOL=1; shift ;;
    --no-helm) NOHELM=1; shift ;;
    --skip-cr-count) SKIPCR=1; shift ;;
    --strict) STRICT=1; shift ;;
    --clear-names) CLEAR=1; shift ;;
    --timeout) TO="$2"; shift 2 ;;
    --no-tar) NOTAR=1; shift ;;
    --keep-raw) KEEPRAW=1; shift ;;
    --quiet) QUIET=1; shift ;;
    --print-rbac) print_rbac; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

log() { [ "$QUIET" = 1 ] || printf '%s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

for b in "$KUBECTL" awk sed grep sort tr base64 gzip tar cut wc; do
  command -v "$b" >/dev/null 2>&1 || die "missing required tool: $b"
done
if command -v sha256sum >/dev/null 2>&1; then SHA="sha256sum"
elif command -v shasum >/dev/null 2>&1; then SHA="shasum -a 256"
elif command -v openssl >/dev/null 2>&1; then SHA="openssl dgst -sha256 -r"
else die "need sha256sum, shasum or openssl"; fi
b64d() { base64 -d 2>/dev/null || base64 -D; }

umask 077
mkdir -p "$OUT" "$PRIV" || die "cannot create output folders"
if [ -z "$SALT" ] && [ -f "$PRIV/salt.txt" ]; then SALT="$(cat "$PRIV/salt.txt")"; fi
if [ -z "$SALT" ]; then
  SALT="$(head -c 32 /dev/urandom | $SHA | cut -c1-32)"
  printf '%s\n' "$SALT" > "$PRIV/salt.txt"
  log "New salt written to $PRIV/salt.txt - keep it and reuse it for every cluster."
fi
SENS_LIST="$(printf '%s\n' "${SENS[@]+"${SENS[@]}"}" | tr 'A-Z' 'a-z' | grep -v '^$' | paste -sd'|' -)"

T='{"\t"}'; N='{"\n"}'
NOW_UTC="$(date -u +%Y-%m-%d)"

# ----------------------------------------------------------------------------
# kubectl wrapper: records failures in errors.tsv instead of aborting
# ----------------------------------------------------------------------------
CTX=""
kc() {
  local step="$1"; shift
  local args=()
  [ -n "$KCFG" ] && args+=(--kubeconfig "$KCFG")
  [ -n "$CTX" ] && args+=(--context "$CTX")
  "$KUBECTL" "${args[@]+"${args[@]}"}" --request-timeout="${TO}s" "$@" 2>"$RAW/.err"
  local rc=$?
  if [ $rc -ne 0 ]; then
    printf '%s\t%s\n' "$step" "$(tail -n 1 "$RAW/.err" | tr '\t' ' ' | cut -c1-300)" >> "$RAW/errors.tsv"
  fi
  return $rc
}
jp() {  # jp <step> <resource> <jsonpath-template> <outfile>
  kc "$1" get "$2" -A -o "jsonpath=$3" > "$4" || : > "$4"
}
has_api() { kc "api-resources $1" api-resources --api-group="$1" -o name 2>/dev/null | grep -q .; }

# Shared awk helpers (mawk compatible: no gawk-only functions, no {n} regex)
AWKLIB='
function q(s,  n,u){ if(s==""||s=="<none>") return 0; n=s+0; u=s; sub(/^[-+0-9.]+([eE][-+]?[0-9]+)?/,"",u);
  if(u=="Ki")return n*1024; if(u=="Mi")return n*1048576; if(u=="Gi")return n*1073741824;
  if(u=="Ti")return n*1099511627776; if(u=="Pi")return n*1125899906842624;
  if(u=="n")return n/1e9; if(u=="u")return n/1e6; if(u=="m")return n/1000;
  if(u=="k"||u=="K")return n*1e3; if(u=="M")return n*1e6; if(u=="G")return n*1e9;
  if(u=="T")return n*1e12; if(u=="P")return n*1e15; return n }
function mc(s){ return int(q(s)*1000+0.5) }
function gib(b){ return sprintf("%.2f", b/1073741824) }
function sumlist(s, f,  a,n,i,t){ n=split(s,a,","); t=0; for(i=1;i<=n;i++) if(a[i]!=""){ t+= (f=="cpu"? mc(a[i]) : q(a[i])) } return t }
function cnt(s, re,  t){ t=0; while(match(s,re)){ t++; s=substr(s,RSTART+RLENGTH) } return t }
function words(s,  a){ if(s=="") return 0; return split(s,a," ") }
function jkeys(js,  out,k){ out=""; while(match(js,/[{,]"[^"\\]*":/)){ k=substr(js,RSTART+2,RLENGTH-4); out=out (out==""?"":";") k; js=substr(js,RSTART+RLENGTH) } return out }
function dfc(y,m,d){ if(m<=2){y--; m+=12} return 365*y+int(y/4)-int(y/100)+int(y/400)+int((153*(m-3)+2)/5)+d }
function age(ts,  y,m,d){ if(ts=="") return ""; y=substr(ts,1,4)+0; m=substr(ts,6,2)+0; d=substr(ts,9,2)+0; return dfc(NY,NM,ND)-dfc(y,m,d) }
'
AWKV=(-v NY="${NOW_UTC:0:4}" -v NM="${NOW_UTC:5:2}" -v ND="${NOW_UTC:8:2}")
aw() {  # aw [-v NAME=VAL ...] 'program' files...
  local pre=()
  while [ "${1:-}" = "-v" ]; do pre+=(-v "$2"); shift 2; done
  awk -F'\t' -v OFS='\t' "${AWKV[@]}" "${pre[@]+"${pre[@]}"}" "$AWKLIB$1" "${@:2}"
}

# ----------------------------------------------------------------------------
# Collectors (write raw/*.tsv, then pre/*.tsv with final columns, clear names)
# ----------------------------------------------------------------------------
collect_cluster() {
  local ver uid
  ver="$(kc version version 2>/dev/null | sed -n 's/^Server Version: *//p' | head -1)"
  uid="$(kc kube-system get namespace kube-system -o 'jsonpath={.metadata.uid}' 2>/dev/null)"
  printf 'cluster_id\tcontext\tkubernetes_version\tcollected_utc\ttool_version\n%s\t%s\t%s\t%s\t%s\n' \
    "${uid:-${CTX:-current}}" "${CTX:-current}" "$ver" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$VERSION" > "$PRE/cluster.tsv"
  { printf 'groupversion\n'; { kc api api-versions 2>/dev/null || true; } | sort; } > "$PRE/api_versions_served.tsv"
}

collect_nodes() {
  jp nodes nodes "{range .items[*]}{.metadata.name}$T{.metadata.labels}$T{.status.conditions[?(@.type==\"Ready\")].status}$T{.spec.unschedulable}$T{.status.nodeInfo.kubeletVersion}$T{.status.nodeInfo.osImage}$T{.status.nodeInfo.kernelVersion}$T{.status.nodeInfo.containerRuntimeVersion}$T{.status.nodeInfo.architecture}$T{.status.capacity.cpu}$T{.status.capacity.memory}$T{.status.capacity.pods}$T{.status.capacity.ephemeral-storage}$T{.status.capacity.nvidia\.com/gpu}$T{.status.allocatable.cpu}$T{.status.allocatable.memory}$T{range .spec.taints[*]}{.key}:{.effect};{end}$T{.spec.providerID}$T{.metadata.creationTimestamp}$N{end}" "$RAW/nodes.tsv"
  { kc "top nodes" top nodes --no-headers 2>/dev/null || true; } | awk '{print $1"\t"$2"\t"$4}' > "$RAW/top_nodes.tsv"
}

collect_namespaces() {
  jp namespaces namespaces "{range .items[*]}{.metadata.name}$T{.metadata.labels.pod-security\.kubernetes\.io/enforce}$T{.metadata.labels.pod-security\.kubernetes\.io/warn}$T{.metadata.labels}$T{.metadata.creationTimestamp}$N{end}" "$RAW/namespaces.tsv"
  jp quotas resourcequotas "{range .items[*]}{.metadata.namespace}$T{.spec.hard.requests\.cpu}{.spec.hard.cpu}$T{.spec.hard.requests\.memory}{.spec.hard.memory}$T{.spec.hard.requests\.storage}$N{end}" "$RAW/quotas.tsv"
  jp limitranges limitranges "{range .items[*]}{.metadata.namespace}$N{end}" "$RAW/limitranges.tsv"
}

wl_tpl() {  # $1 pod-spec path, $2 replicas expr, $3 ready expr
  local P="$1"
  echo "{range .items[*]}{.metadata.namespace}$T{.metadata.name}$T$2$T$3$T{.metadata.ownerReferences[0].apiVersion}/{.metadata.ownerReferences[0].kind}$T{.metadata.labels.helm\.sh/chart}$T{.metadata.labels.app\.kubernetes\.io/managed-by}$T{$P.containers[*].name}$T{$P.initContainers[*].name}$T{range $P.containers[*]}{.resources.requests.cpu},{end}$T{range $P.containers[*]}{.resources.requests.memory},{end}$T{range $P.containers[*]}{.resources.limits.cpu},{end}$T{range $P.containers[*]}{.resources.limits.memory},{end}$T{range $P.containers[*]}{.resources.limits.nvidia\.com/gpu},{end}$T{range $P.containers[*]}[{.resources.requests}]{end}$T{range $P.containers[*]}[{.resources.limits}]{end}$T{range $P.containers[*]}[{.readinessProbe}{.livenessProbe}]{end}$T{$P.containers[*].securityContext.privileged}$T{$P.hostNetwork}$T{$P.hostPID}$T{$P.containers[*].ports[*].hostPort}$T{$P.containers[*].securityContext.capabilities.add[*]}$T{$P.volumes[*].persistentVolumeClaim.claimName}$T{$P.volumes[*].hostPath.path}$T{$P.volumes[*].nfs.path}$T{$P.volumes[*].emptyDir}$T{$P.volumes[*].csi.driver}$T{$P.nodeSelector}$T{$P.affinity}$T{$P.tolerations[*].key}$T{$P.priorityClassName}$T{$P.serviceAccountName}$T{.spec.strategy.type}{.spec.updateStrategy.type}$T{range .spec.volumeClaimTemplates[*]}{.spec.resources.requests.storage}/{.spec.storageClassName}/{.spec.accessModes[*]};{end}$T{.spec.schedule}$T{.spec.suspend}$T{.metadata.creationTimestamp}$T{$P.topologySpreadConstraints}$T{$P.containers[*].image} {$P.initContainers[*].image}$N{end}"
}

collect_workloads() {
  : > "$RAW/workloads.tsv"
  local spec=".spec.template.spec"
  jp deployments deployments "$(wl_tpl "$spec" '{.spec.replicas}' '{.status.readyReplicas}')" "$RAW/w.tmp"; sed 's/^/Deployment\t/' "$RAW/w.tmp" >> "$RAW/workloads.tsv"
  jp statefulsets statefulsets "$(wl_tpl "$spec" '{.spec.replicas}' '{.status.readyReplicas}')" "$RAW/w.tmp"; sed 's/^/StatefulSet\t/' "$RAW/w.tmp" >> "$RAW/workloads.tsv"
  jp daemonsets daemonsets "$(wl_tpl "$spec" '{.status.desiredNumberScheduled}' '{.status.numberReady}')" "$RAW/w.tmp"; sed 's/^/DaemonSet\t/' "$RAW/w.tmp" >> "$RAW/workloads.tsv"
  jp jobs jobs "$(wl_tpl "$spec" '{.spec.parallelism}' '{.status.active}')" "$RAW/w.tmp"; sed 's/^/Job\t/' "$RAW/w.tmp" >> "$RAW/workloads.tsv"
  jp cronjobs cronjobs "$(wl_tpl '.spec.jobTemplate.spec.template.spec' '1' '{.status.active[0].name}')" "$RAW/w.tmp"; sed 's/^/CronJob\t/' "$RAW/w.tmp" >> "$RAW/workloads.tsv"
  jp replicationcontrollers replicationcontrollers "$(wl_tpl "$spec" '{.spec.replicas}' '{.status.readyReplicas}')" "$RAW/w.tmp"; sed 's/^/ReplicationController\t/' "$RAW/w.tmp" >> "$RAW/workloads.tsv"
  rm -f "$RAW/w.tmp"
  jp pdbs poddisruptionbudgets "{range .items[*]}{.metadata.namespace}$T{.metadata.name}$T{.spec.minAvailable}$T{.spec.maxUnavailable}$T{.status.expectedPods}$T{.status.disruptionsAllowed}$N{end}" "$RAW/pdbs.tsv"
  jp hpa horizontalpodautoscalers "{range .items[*]}{.metadata.namespace}$T{.spec.scaleTargetRef.kind}$T{.spec.minReplicas}$T{.spec.maxReplicas}$T{.spec.metrics[*].type}$N{end}" "$RAW/hpa.tsv"
  jp priorityclasses priorityclasses "{range .items[*]}{.metadata.name}$T{.value}$T{.globalDefault}$N{end}" "$RAW/priorityclasses.tsv"
}

collect_pods() {
  jp pods pods "{range .items[*]}{.metadata.namespace}$T{.status.phase}$T{.status.qosClass}$T{.spec.nodeName}$T{.metadata.ownerReferences[0].kind}$T{.status.containerStatuses[*].restartCount}$T{range .spec.containers[*]}{.resources.requests.cpu},{end}$T{range .spec.containers[*]}{.resources.requests.memory},{end}$T{range .spec.containers[*]}{.resources.limits.cpu},{end}$T{range .spec.containers[*]}{.resources.limits.memory},{end}$T{range .spec.containers[*]}{.resources.limits.nvidia\.com/gpu},{end}$T{.spec.volumes[*].persistentVolumeClaim.claimName}$T{.spec.containers[*].image} {.spec.initContainers[*].image}$N{end}" "$RAW/pods.tsv"
  { kc "top pods" top pods -A --no-headers 2>/dev/null || true; } | awk '{print $1"\t"$3"\t"$4}' > "$RAW/top_pods.tsv"
}

collect_storage() {
  jp storageclasses storageclasses "{range .items[*]}{.metadata.name}$T{.provisioner}$T{.reclaimPolicy}$T{.volumeBindingMode}$T{.allowVolumeExpansion}$T{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}$T{.parameters}$N{end}" "$RAW/storageclasses.tsv"
  jp csidrivers csidrivers "{range .items[*]}{.metadata.name}$T{.spec.attachRequired}$T{.spec.volumeLifecycleModes[*]}$N{end}" "$RAW/csidrivers.tsv"
  jp pvs persistentvolumes "{range .items[*]}{.metadata.name}$T{.spec.capacity.storage}$T{.spec.accessModes[*]}$T{.spec.storageClassName}$T{.spec.volumeMode}$T{.spec.persistentVolumeReclaimPolicy}$T{.status.phase}$T{.spec.csi.driver}$T{.spec.nfs.path}$T{.spec.hostPath.path}$T{.spec.local.path}$T{.spec.vsphereVolume.volumePath}$T{.spec.iscsi.iqn}$T{.spec.claimRef.namespace}$T{.spec.nodeAffinity}$N{end}" "$RAW/pvs.tsv"
  jp pvcs persistentvolumeclaims "{range .items[*]}{.metadata.namespace}$T{.metadata.name}$T{.spec.storageClassName}$T{.spec.accessModes[*]}$T{.spec.volumeMode}$T{.spec.resources.requests.storage}$T{.status.capacity.storage}$T{.status.phase}$T{.spec.volumeName}$T{.spec.dataSource.kind}$T{.metadata.creationTimestamp}$N{end}" "$RAW/pvcs.tsv"
  local n
  n="$({ kc volumesnapshots get volumesnapshots -A --no-headers 2>/dev/null || true; } | grep -c .)"
  printf '%s\n' "$n" > "$RAW/volumesnapshots.txt"
  : > "$RAW/volstats.tsv"
  if [ "$VOL" = 1 ]; then
    cut -f1 "$RAW/nodes.tsv" | while read -r node; do
      [ -n "$node" ] || continue
      { kc "node stats" get --raw "/api/v1/nodes/$node/proxy/stats/summary" 2>/dev/null || true; } | tr -d '\n ' \
        | grep -o '"usedBytes":[0-9]*[^{}]*"pvcRef":{"name":"[^"]*","namespace":"[^"]*"}' \
        | sed 's/^"usedBytes":\([0-9]*\).*"pvcRef":{"name":"\([^"]*\)","namespace":"\([^"]*\)"}$/\3\t\2\t\1/' >> "$RAW/volstats.tsv"
    done
  fi
}

collect_network() {
  jp services services "{range .items[*]}{.metadata.namespace}$T{.metadata.name}$T{.spec.type}$T{.spec.clusterIP}$T{range .spec.ports[*]}{.port}/{.protocol};{end}$T{.spec.ports[*].nodePort}$T{.spec.externalTrafficPolicy}$T{.spec.loadBalancerClass}$T{.spec.externalIPs[*]}$T{.spec.externalName}$T{.spec.selector}$T{.metadata.annotations}$T{.spec.ipFamilies[*]}$T{.spec.sessionAffinity}$N{end}" "$RAW/services.tsv"
  jp ingresses ingresses "{range .items[*]}{.metadata.namespace}$T{.metadata.name}$T{.spec.ingressClassName}$T{.metadata.annotations.kubernetes\.io/ingress\.class}$T{.spec.rules[*].host}$T{.spec.tls[*].hosts[*]}$T{.spec.rules[*].http.paths[*].pathType}$T{.spec.rules[*].http.paths[*].backend.service.name}$T{.spec.defaultBackend.service.name}$T{.metadata.annotations}$T{.spec.tls[*].secretName}$N{end}" "$RAW/ingresses.tsv"
  jp ingressclasses ingressclasses "{range .items[*]}{.metadata.name}$T{.spec.controller}$T{.metadata.annotations.ingressclass\.kubernetes\.io/is-default-class}$N{end}" "$RAW/ingressclasses.tsv"
  jp networkpolicies networkpolicies "{range .items[*]}{.metadata.namespace}$T{.spec.policyTypes[*]}$T{.spec.podSelector}$T{.spec.ingress[*].from[*].ipBlock.cidr} {.spec.egress[*].to[*].ipBlock.cidr}$N{end}" "$RAW/netpol.tsv"
  : > "$RAW/nginx_cm.tsv"
  cut -f1 "$RAW/namespaces.tsv" | grep -i nginx | while read -r ns; do
    { kc "nginx configmaps" get configmaps -n "$ns" -o "jsonpath={range .items[*]}{.metadata.name}$T{.data}$N{end}" 2>/dev/null || true; } >> "$RAW/nginx_cm.tsv"
  done
}

collect_extensions() {
  jp crds customresourcedefinitions "{range .items[*]}{.metadata.name}$T{.spec.group}$T{.spec.names.kind}$T{.spec.names.plural}$T{.spec.scope}$T{range .spec.versions[*]}{.name}(served={.served},storage={.storage},deprecated={.deprecated});{end}$T{.status.storedVersions[*]}$T{.spec.conversion.strategy}$T{.metadata.labels.app\.kubernetes\.io/managed-by}$T{.metadata.annotations.meta\.helm\.sh/release-namespace}$T{.metadata.annotations.meta\.helm\.sh/release-name}$T{.metadata.creationTimestamp}$N{end}" "$RAW/crds.tsv"
  : > "$RAW/cr_counts.tsv"
  if [ "$SKIPCR" = 0 ]; then
    awk -F'\t' '{print $1"\t"$4"."$2}' "$RAW/crds.tsv" | while IFS=$'\t' read -r crd res; do
      [ -n "$crd" ] || continue
      out="$(kc "count $crd" get "$res" -A --no-headers -o 'custom-columns=NS:.metadata.namespace' 2>/dev/null)" \
        || { printf '%s\t\t\n' "$crd" >> "$RAW/cr_counts.tsv"; continue; }
      printf '%s\n' "$out" | awk -v c="$crd" 'NF{n++; if($1!="<none>") s[$1]=1} END{k=0; for(x in s)k++; print c"\t"n+0"\t"k}' >> "$RAW/cr_counts.tsv"
    done
  fi
  local wtpl="{range .items[*]}{.metadata.name}$T{.webhooks[*].name}$T{.webhooks[*].failurePolicy}$T{.webhooks[*].clientConfig.service.namespace}$T{.webhooks[*].clientConfig.url}$T{range .webhooks[*].rules[*]}{.apiGroups[*]}:{.resources[*]};{end}$T{.webhooks[*].timeoutSeconds}$T{.webhooks[*].namespaceSelector}$T{.metadata.annotations.meta\.helm\.sh/release-name}$N{end}"
  jp mutatingwebhooks mutatingwebhookconfigurations "$wtpl" "$RAW/w.tmp"; sed 's/^/mutating\t/' "$RAW/w.tmp" > "$RAW/webhooks.tsv"
  jp validatingwebhooks validatingwebhookconfigurations "$wtpl" "$RAW/w.tmp"; sed 's/^/validating\t/' "$RAW/w.tmp" >> "$RAW/webhooks.tsv"; rm -f "$RAW/w.tmp"
  jp apiservices apiservices "{range .items[*]}{.metadata.name}$T{.spec.service.namespace}$T{.status.conditions[?(@.type==\"Available\")].status}$N{end}" "$RAW/apiservices.tsv"
}

collect_rbac() {
  jp clusterroles clusterroles "{range .items[*]}{.metadata.name}$T{.metadata.labels.kubernetes\.io/bootstrapping}$T{.aggregationRule}$T{range .rules[*]}x{end}$T{.metadata.annotations.meta\.helm\.sh/release-name}$T{.metadata.labels}$N{end}" "$RAW/clusterroles.tsv"
  jp clusterrolebindings clusterrolebindings "{range .items[*]}{.metadata.namespace}$T{.metadata.name}$T{.roleRef.kind}$T{.roleRef.name}$T{range .subjects[*]}{.kind}={.name};{end}$N{end}" "$RAW/w.tmp"; sed 's/^/ClusterRoleBinding\t/' "$RAW/w.tmp" > "$RAW/bindings.tsv"
  jp rolebindings rolebindings "{range .items[*]}{.metadata.namespace}$T{.metadata.name}$T{.roleRef.kind}$T{.roleRef.name}$T{range .subjects[*]}{.kind}={.name};{end}$N{end}" "$RAW/w.tmp"; sed 's/^/RoleBinding\t/' "$RAW/w.tmp" >> "$RAW/bindings.tsv"; rm -f "$RAW/w.tmp"
  jp roles roles "{range .items[*]}{.metadata.namespace}$N{end}" "$RAW/roles.tsv"
  { kc serviceaccounts get serviceaccounts -A --no-headers 2>/dev/null || true; } | grep -c . > "$RAW/sa_count.txt"
  # server-side table: names/types only, no secret data is transferred
  { kc "secret types" get secrets -A --no-headers 2>/dev/null || true; } | awk '{c[$3]++} END{for(t in c) print t"\t"c[t]}' > "$RAW/secret_types.tsv"
}

collect_helm() {
  : > "$RAW/helm.tsv"; : > "$RAW/helm_kinds.tsv"
  [ "$NOHELM" = 1 ] && return 0
  kc "helm list" get secrets -A --field-selector type=helm.sh/release.v1 \
     -o "jsonpath={range .items[*]}{.metadata.namespace}$T{.metadata.name}$T{.metadata.labels.name}$T{.metadata.labels.version}$N{end}" > "$RAW/helm_list.tsv" 2>/dev/null || : > "$RAW/helm_list.tsv"
  # latest revision per (namespace, release)
  awk -F'\t' '{k=$1"\t"$3; if(!(k in v) || $4+0>v[k]+0){v[k]=$4; s[k]=$2}} END{for(k in v) print k"\t"v[k]"\t"s[k]}' "$RAW/helm_list.tsv" \
  | while IFS=$'\t' read -r ns rel rev sec; do
      js="$(kc "helm decode" get secret -n "$ns" "$sec" -o 'jsonpath={.data.release}' 2>/dev/null | b64d | b64d | gzip -dc 2>/dev/null)" || js=""
      [ -n "$js" ] || { printf '%s\t%s\t%s\t\t\t\t\t\n' "$ns" "$rel" "$rev" >> "$RAW/helm.tsv"; continue; }
      printf '%s' "$js" | awk -v ns="$ns" -v rel="$rel" -v rev="$rev" '
        function grab(s,key,  v){ if(match(s,"\"" key "\":\"[^\"]*\"")){ v=substr(s,RSTART,RLENGTH); sub("^\"" key "\":\"","",v); sub("\"$","",v); return v } return "" }
        { j=j $0 }
        END{ gsub(/": /,"\":",j); gsub(/, "/,",\"",j); gsub(/\{ "/,"{\"",j)
             i=index(j,"\"chart\":{\"metadata\":"); head=(i?substr(j,1,i):j); ch=(i?substr(j,i):"");
             st=grab(head,"status"); ld=substr(grab(head,"last_deployed"),1,10);
             print ns"\t"rel"\t"rev"\t"grab(ch,"name")"\t"grab(ch,"version")"\t"grab(ch,"appVersion")"\t"st"\t"ld }' >> "$RAW/helm.tsv"
      printf '%s' "$js" | sed 's/\\n/\n/g' | awk -v ns="$ns" -v rel="$rel" '
        /^---/{av="";k=""; next}
        /^apiVersion: /{av=$2} /^kind: /{k=$2}
        av!="" && k!=""{ gsub(/["\047]/,"",av); gsub(/["\047]/,"",k); print ns"\t"rel"\t"av"\t"k; av=""; k="" }' >> "$RAW/helm_kinds.tsv"
    done
}

collect_deprecated() {
  { kc metrics get --raw /metrics 2>/dev/null || true; } | grep '^apiserver_requested_deprecated_apis{' \
    | sed -e 's/^[^{]*{//' -e 's/}.*$//' | awk -F',' '{g=v=r=s=rr=""; for(i=1;i<=NF;i++){split($i,a,"="); gsub(/"/,"",a[2]);
        if(a[1]=="group")g=a[2]; if(a[1]=="version")v=a[2]; if(a[1]=="resource")r=a[2]; if(a[1]=="subresource")s=a[2]; if(a[1]=="removed_release")rr=a[2]}
        print g"\t"v"\t"r"\t"s"\t"rr}' > "$RAW/deprecated.tsv"
  { kc last-applied get deployments,statefulsets,daemonsets,cronjobs,jobs,services,ingresses,networkpolicies,poddisruptionbudgets,horizontalpodautoscalers,roles,rolebindings -A \
      -o 'jsonpath={range .items[*]}{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}{"\n"}{end}' 2>/dev/null || true; } \
    | grep -o '^{"apiVersion":"[^"]*","kind":"[^"]*"' | sed 's/^{"apiVersion":"\([^"]*\)","kind":"\([^"]*\)"$/\1\t\2/' > "$RAW/lastapplied.tsv"
}

collect_tanzu() {
  : > "$RAW/tkc.tsv"; : > "$RAW/capi.tsv"; : > "$RAW/vmclass.tsv"; : > "$RAW/vms.tsv"
  if has_api run.tanzu.vmware.com; then
    jp tkc tanzukubernetesclusters.run.tanzu.vmware.com "{range .items[*]}{.metadata.namespace}$T{.metadata.name}$T{.spec.distribution.version}{.spec.topology.controlPlane.tkr.reference.name}$T{.spec.topology.controlPlane.replicas}$T{.spec.topology.controlPlane.vmClass}$T{range .spec.topology.nodePools[*]}{.replicas},{end}$T{.spec.topology.nodePools[*].vmClass}$T{.spec.topology.nodePools[*].storageClass}$T{.status.phase}$N{end}" "$RAW/tkc.tsv"
  fi
  if has_api cluster.x-k8s.io; then
    jp capi clusters.cluster.x-k8s.io "{range .items[*]}{.metadata.namespace}$T{.metadata.name}$T{.spec.topology.version}$T{.spec.topology.controlPlane.replicas}$T{.spec.topology.variables[?(@.name==\"vmClass\")].value}$T{range .spec.topology.workers.machineDeployments[*]}{.replicas},{end}$T{.spec.topology.workers.machineDeployments[*].class}$T{.spec.topology.variables[?(@.name==\"storageClass\")].value}$T{.status.phase}$N{end}" "$RAW/capi.tsv"
  fi
  if has_api vmoperator.vmware.com; then
    jp vmclasses virtualmachineclasses.vmoperator.vmware.com "{range .items[*]}{.metadata.name}$T{.spec.hardware.cpus}$T{.spec.hardware.memory}$N{end}" "$RAW/vmclass.tsv"
    jp vms virtualmachines.vmoperator.vmware.com "{range .items[*]}{.spec.className}$T{.status.powerState}$N{end}" "$RAW/vms.tsv"
  fi
}

# ----------------------------------------------------------------------------
# Shaping raw -> pre (final columns, still clear names, header row first)
# ----------------------------------------------------------------------------
shape() {
  # nodes
  aw '
    FILENAME==ARGV[1]{ uc[$1]=$2; um[$1]=$3; next }
    { roles=""; lk=""; js=$2
      while(match(js,/[{,]"[^"\\]*":/)){ k=substr(js,RSTART+2,RLENGTH-4); js=substr(js,RSTART+RLENGTH)
        if(k ~ /^node-role\.kubernetes\.io\//){ r=k; sub(/^[^\/]*\//,"",r); roles=roles (roles==""?"":";") r }
        else if(k!="kubernetes.io/hostname") lk=lk (lk==""?"":";") k }
      if(roles=="") roles="worker"
      ac=mc($15); am=q($16)
      pr=$18; sub(/:\/\/.*/,"",pr)
      print $1,roles,$3,($4=="true"),$5,$6,$7,$8,$9,mc($10),gib(q($11)),q($12),gib(q($13)),q($14)+0,ac,gib(am),$17,lk,pr,age($19),
            (ac? sprintf("%.1f",100*mc(uc[$1])/ac):""),(am? sprintf("%.1f",100*q(um[$1])/am):"") }' "$RAW/top_nodes.tsv" "$RAW/nodes.tsv" > "$RAW/n.tmp"
  # pod aggregates per node
  aw '$2=="Running"||$2=="Pending"{ if($4!=""){ n[$4]++; c[$4]+=sumlist($7,"cpu"); m[$4]+=sumlist($8,"mem") } }
      END{ for(k in n) print k,n[k],c[k],m[k] }' "$RAW/pods.tsv" > "$RAW/podnode.tmp"
  { printf 'node\troles\tready\tunschedulable\tkubelet\tos_image\tkernel\truntime\tarch\tcpu_capacity_m\tmem_capacity_gib\tpods_capacity\tephemeral_gib\tgpu\tcpu_allocatable_m\tmem_allocatable_gib\ttaints\tlabel_keys\tprovider\tage_days\tcpu_used_pct\tmem_used_pct\tpods_running\tcpu_req_pct\tmem_req_pct\n'
    aw 'FILENAME==ARGV[1]{ n[$1]=$2; c[$1]=$3; m[$1]=$4; next }
        { print $0, n[$1]+0, ($15? sprintf("%.1f",100*c[$1]/$15):""), ($16>0? sprintf("%.1f",100*m[$1]/($16*1073741824)):"") }' "$RAW/podnode.tmp" "$RAW/n.tmp"
  } > "$PRE/nodes.tsv"

  # workloads + images
  { printf 'namespace\tkind\tname\treplicas\tready\towner_kind\thelm_chart\tmanaged_by\tcontainers\tinit_containers\tcpu_req_m_per_pod\tmem_req_mib_per_pod\tcpu_lim_m_per_pod\tmem_lim_mib_per_pod\tgpu_per_pod\tcpu_req_m_total\tmem_req_gib_total\tcontainers_no_requests\tcontainers_no_limits\tcontainers_no_probes\tprivileged\thost_network\thost_pid\thost_ports\tcap_add\tpvc_volumes\thostpath_volumes\tnfs_volumes\temptydir_volumes\tcsi_inline_volumes\tnode_selector_keys\taffinity\ttopology_spread\ttoleration_keys\tpriority_class\tcustom_service_account\tstrategy\tpvc_templates\tpvc_template_gib_total\tschedule\tsuspended\tage_days\n'
    aw '{ kind=$1; if(kind=="Job" && $6 ~ /\/CronJob$/) next
          rep=$4; if(rep=="") rep=(kind=="Job"||kind=="CronJob"||kind=="Deployment"||kind=="StatefulSet"||kind=="ReplicationController")?1:0
          own=$6; if(own=="/" || own ~ /\/(ReplicaSet|Deployment|StatefulSet|DaemonSet|Job|CronJob|ReplicationController)$/) own=""
          cr=sumlist($11,"cpu"); mr=sumlist($12,"mem"); cl=sumlist($13,"cpu"); ml=sumlist($14,"mem"); gp=sumlist($15,"mem")
          vt=""; vg=0; n=split($35,a,";"); for(i=1;i<=n;i++) if(a[i]!=""){ split(a[i],b,"/"); vt=vt (vt==""?"":";") a[i]; vg+=q(b[1]) }
          print $2,kind,$3,rep,$5+0,own,$7,$8,words($9),words($10),cr,int(mr/1048576),cl,int(ml/1048576),gp,cr*rep,gib(mr*rep),
                cnt($16,"\\[\\]"),cnt($17,"\\[\\]"),cnt($18,"\\[\\]"),cnt($19,"true"),($20=="true"),($21=="true"),words($22),$23,
                words($24),words($25),words($26),cnt($27,"\\{"),words($28),jkeys($29),($30!=""),($39!=""),$31,$32,($33!=""&&$33!="default"),$34,
                vt,gib(vg*rep),$36,$37,age($38) }' "$RAW/workloads.tsv"
  } > "$PRE/workloads.tsv"
  # images from running pods (actual images), registry split
  { printf 'registry\tregistry_public\tproject\trepository\ttag\tpinned_by_digest\tpods_using\tworkloads_using\n'
    aw -v PUB="docker.io index.docker.io quay.io gcr.io k8s.gcr.io registry.k8s.io ghcr.io mcr.microsoft.com public.ecr.aws docker.elastic.co nvcr.io registry.gitlab.com projects.registry.vmware.com registry.access.redhat.com registry.redhat.io registry.suse.com" '
      BEGIN{ n=split(PUB,p," "); for(i=1;i<=n;i++) pub[p[i]]=1 }
      FILENAME==ARGV[1]{ n=split($40,im," "); for(i=1;i<=n;i++) if(im[i]!=""){ w[im[i]]++; c[im[i]]+=0 } next }
      { n=split($13,im," "); for(i=1;i<=n;i++) if(im[i]!="") c[im[i]]++ }
      END{ for(ref in c){ r=ref; dg=0; if(index(r,"@")){ dg=1; r=substr(r,1,index(r,"@")-1) }
             tag=""; last=r; sub(/.*\//,"",last); if(index(last,":")){ tag=substr(last,index(last,":")+1); r=substr(r,1,length(r)-length(tag)-1) }
             if(tag=="" && !dg) tag="latest(implicit)"
             np=split(r,pt,"/"); reg="docker.io"; s=1
             if(np>1 && (index(pt[1],".")||index(pt[1],":")||pt[1]=="localhost")){ reg=pt[1]; s=2 }
             path=""; for(i=s;i<=np;i++) path=path (path==""?"":"/") pt[i]
             isp=(tolower(reg) in pub)
             if(isp){ proj=""; repo=path } else { proj=path; sub(/\/?[^\/]*$/,"",proj); repo=pt[np] }
             print reg,isp,proj,repo,tag,dg,c[ref]+0,w[ref]+0 } }' "$RAW/workloads.tsv" "$RAW/pods.tsv" | sort -t$'\t' -k7,7nr -k8,8nr
  } > "$PRE/images.tsv"

  # pvc users from pods
  aw '{ n=split($12,a," "); for(i=1;i<=n;i++){ k=$1"\t"a[i]; o=($5==""?"Pod":$5); if(index(u[k],o)==0) u[k]=u[k] (u[k]==""?"":";") o } }
      END{ for(k in u) print k,u[k] }' "$RAW/pods.tsv" > "$RAW/pvcusers.tmp"
  # pvs
  { printf 'pv\tcapacity_gib\taccess_modes\tstorage_class\tvolume_mode\treclaim\tphase\tsource_type\tclaim_namespace\tnode_affinity_pinned\n'
    aw '{ t="other"; if($8!="")t="csi:"$8; else if($9!="")t="nfs"; else if($10!="")t="hostPath"; else if($11!="")t="local"; else if($12!="")t="vsphereVolume"; else if($13!="")t="iscsi"
          print $1,gib(q($2)),$3,$4,($5==""?"Filesystem":$5),$6,$7,t,$14,($15!="") }' "$RAW/pvs.tsv"
  } > "$PRE/pvs.tsv"
  # pvcs
  { printf 'namespace\tpvc\tstorage_class\taccess_modes\tvolume_mode\trequested_gib\tcapacity_gib\tphase\tused_gib\tsource_type\tused_by\tmounted\tdata_source\tage_days\n'
    aw 'FILENAME==ARGV[1]{ u[$1"\t"$2]=$3; next } FILENAME==ARGV[2]{ us[$1"\t"$2]=$3; next } FILENAME==ARGV[3]{ src[$1]=$8; next }
        { k=$1"\t"$2; print $1,$2,$3,$4,($5==""?"Filesystem":$5),gib(q($6)),gib(q($7)),$8,(k in u? gib(u[k]):""),src[$9],us[k],(us[k]!=""),$10,age($11) }' \
        "$RAW/volstats.tsv" "$RAW/pvcusers.tmp" "$PRE/pvs.tsv.src" "$RAW/pvcs.tsv" 2>/dev/null
  } > "$PRE/pvcs.tsv"
  { printf 'storage_class\tprovisioner\treclaim\tbinding_mode\tallow_expansion\tdefault\tparameter_keys\n'
    aw '{ print $1,$2,$3,$4,($5=="true"),($6=="true"),jkeys($7) }' "$RAW/storageclasses.tsv"; } > "$PRE/storageclasses.tsv"
  { printf 'csi_driver\tattach_required\tmodes\n'; cat "$RAW/csidrivers.tsv"; } > "$PRE/csidrivers.tsv"

  # services
  { printf 'namespace\tservice\ttype\theadless\thas_selector\tports\tnode_ports\texternal_traffic_policy\tlb_class\texternal_ips_count\texternal_name\tannotation_keys\tip_families\tsession_affinity\n'
    aw '{ print $1,$2,($3==""?"ClusterIP":$3),($4=="None"),($11!="" && $11!="{}"),$5,words($6),$7,$8,words($9),$10,jkeys($12),$13,$14 }' "$RAW/services.tsv"; } > "$PRE/services.tsv"
  # ingresses with nginx annotation categories
  { printf 'namespace\tingress\tclass\thosts\twildcard_hosts\tdomains\ttls_hosts\ttls_secrets\tpaths\tpath_types\tbackend_services\tdefault_backend\tnginx_annotations\tnginx_categories\tnginx_snippets\tannotation_keys\n'
    aw 'function cat(k){ k=tolower(k)
          if(k~/snippet/)return "snippet"; if(k~/auth-tls/)return "client-cert"; if(k~/auth-/)return "auth"
          if(k~/rewrite-target|use-regex|app-root|x-forwarded-prefix/)return "rewrite/regex"; if(k~/canary/)return "canary"
          if(k~/limit-/)return "rate-limit"; if(k~/affinity|session-cookie/)return "affinity"; if(k~/cors/)return "cors"
          if(k~/backend-protocol|grpc|secure-backends|proxy-ssl/)return "backend-protocol"; if(k~/ssl-redirect|force-ssl|ssl-passthrough/)return "ssl-redirect"
          if(k~/source-range/)return "ip-allow/deny"; if(k~/proxy-/)return "timeouts/buffers/body"; if(k~/upstream-|load-balance|service-upstream/)return "upstream/lb"
          if(k~/custom-headers|configuration|server-alias|redirect/)return "headers/redirect"; if(k~/mirror/)return "mirror"; return "other" }
        { ak=jkeys($10); n=split(ak,a,";"); na=0; delete cc; cats=""
          for(i=1;i<=n;i++) if(a[i] ~ /nginx/){ na++; cc[cat(a[i])]++ }
          for(x in cc) cats=cats (cats==""?"":";") x "=" cc[x]
          nh=split($5,h," "); doms=""; wc=0; delete seen
          for(i=1;i<=nh;i++){ if(h[i] ~ /^\*/) wc++; d=h[i]; m=split(d,p,"."); if(m>=2) d=p[m-1]"."p[m]; if(!(d in seen)){ seen[d]=1; doms=doms (doms==""?"":";") d } }
          nb=split($8,b," "); delete sb; ub=0; for(i=1;i<=nb;i++) if(!(b[i] in sb)){ sb[b[i]]=1; ub++ }
          pt=""; np=split($7,pp," "); delete cp; for(i=1;i<=np;i++) cp[pp[i]]++; for(x in cp) pt=pt (pt==""?"":";") x "=" cp[x]
          print $1,$2,($3!=""?$3:$4),nh,wc,doms,words($6),words($11),np,pt,ub,($9!=""),na,cats,(("snippet" in cc)?1:0),ak }' "$RAW/ingresses.tsv"
  } > "$PRE/ingresses.tsv"
  { printf 'ingress_class\tcontroller\tdefault\n'; aw '{print $1,$2,($3=="true")}' "$RAW/ingressclasses.tsv"; } > "$PRE/ingressclasses.tsv"
  { printf 'configmap\tkeys\n'; aw '$1 ~ /nginx|controller/{ print $1, jkeys($2) }' "$RAW/nginx_cm.tsv"; } > "$PRE/ingress_nginx_config_keys.tsv"
  { printf 'namespace\tpolicies\tingress_type\tegress_type\tnamespace_wide\tipblock_peers\n'
    aw '{ p[$1]++; if($2 ~ /Ingress/ || $2=="") i[$1]++; if($2 ~ /Egress/) e[$1]++; if($3=="{}"||$3=="") w[$1]++; b[$1]+=words($4) }
        END{ for(k in p) print k,p[k],i[k]+0,e[k]+0,w[k]+0,b[k]+0 }' "$RAW/netpol.tsv"; } > "$PRE/networkpolicies.tsv"

  # CRDs with instance counts and Helm owner
  { printf 'crd\tgroup\tkind\tscope\tversions\tstored_versions\tconversion\tinstances\tnamespaces_with_instances\tin_use\tmanaged_by\thelm_release_namespace\thelm_release\tage_days\n'
    aw 'FILENAME==ARGV[1]{ c[$1]=$2; n[$1]=$3; next }
        { print $1,$2,$3,$5,$6,$7,($8==""?"None":$8),c[$1],n[$1],(c[$1]+0>0),$9,$10,$11,age($12) }' "$RAW/cr_counts.tsv" "$RAW/crds.tsv"; } > "$PRE/crds.tsv"
  { printf 'type\twebhook_config\twebhooks\tfailure_policies\tservice_namespaces\turl_based\trules\tintercepts_all\ttimeouts\tnamespace_selector\thelm_release\n'
    aw '{ print $1,$2,words($3),$4,$5,($6!=""),$7,(index($7,"*")>0),$8,($9!="" && $9!="{}"),$10 }' "$RAW/webhooks.tsv"; } > "$PRE/webhooks.tsv"
  { printf 'apiservice\tservice_namespace\tavailable\n'; aw '$2!=""{print $1,$2,$3}' "$RAW/apiservices.tsv"; } > "$PRE/apiservices.tsv"
  { printf 'namespace\tpdb\tmin_available\tmax_unavailable\texpected_pods\tdisruptions_allowed\tblocks_drain_now\n'
    aw '{ print $1,$2,$3,$4,$5,$6,($6=="0" && $5+0>0) }' "$RAW/pdbs.tsv"; } > "$PRE/pdbs.tsv"
  { printf 'namespace\ttarget_kind\tmin\tmax\tmetrics\n'; cat "$RAW/hpa.tsv"; } > "$PRE/hpa.tsv"
  { printf 'priority_class\tvalue\tglobal_default\n'; cat "$RAW/priorityclasses.tsv"; } > "$PRE/priorityclasses.tsv"

  # RBAC
  { printf 'clusterrole\tsystem\taggregated\trules\thelm_managed\n'
    aw '{ sys=($1 ~ /^system:/ || $2=="rbac-defaults" || $1 ~ /^(cluster-admin|admin|edit|view)$/)
          if(!sys) print $1,sys,($3!=""),length($4),($5!="") }' "$RAW/clusterroles.tsv"; } > "$PRE/clusterroles.tsv"
  printf '%s\n' "$(awk -F'\t' '$1 ~ /^system:/ || $2=="rbac-defaults"' "$RAW/clusterroles.tsv" | grep -c .)" > "$RAW/sys_clusterroles.txt"
  { printf 'binding_kind\tnamespace\tbinding\trole_kind\trole\tsystem\tsubject_users\tsubject_groups\tsubject_serviceaccounts\tgroups\n'
    aw '{ nu=0; ng=0; ns=0; grp=""; n=split($6,s,";"); for(i=1;i<=n;i++){ if(s[i]=="")continue; split(s[i],kv,"=");
            if(kv[1]=="User")nu++; else if(kv[1]=="ServiceAccount")ns++; else if(kv[1]=="Group"){ ng++; if(kv[2]!~/^system:/) grp=grp (grp==""?"":";") kv[2] } }
          sys=($3 ~ /^system:/ || $5 ~ /^system:/ || ($3=="cluster-admin" && $6 ~ /^Group=system:masters;?$/) || $2 ~ /^kube-/)
          if(!sys) print $1,$2,$3,$4,$5,sys,nu,ng,ns,grp }' "$RAW/bindings.tsv"; } > "$PRE/bindings.tsv"
  { printf 'secret_type\tcount\n'; cat "$RAW/secret_types.tsv"; } > "$PRE/secret_types.tsv"

  # Helm
  { printf 'namespace\trelease\trevision\tchart\tchart_version\tapp_version\tstatus\tlast_deployed\tobjects\tkinds\n'
    aw 'FILENAME==ARGV[1]{ k=$1"\t"$2; o[k]++; if(index(";" ks[k] ";", ";" $4 ";")==0) ks[k]=ks[k] (ks[k]==""?"":";") $4; next }
        { k=$1"\t"$2; print $1,$2,$3,$4,$5,$6,$7,$8,o[k]+0,ks[k] }' "$RAW/helm_kinds.tsv" "$RAW/helm.tsv"; } > "$PRE/helm_releases.tsv"
  { printf 'group\tversion\tresource\tsubresource\tremoved_release\n'; cat "$RAW/deprecated.tsv"; } > "$PRE/deprecated_apis_requested.tsv"
  { printf 'api_version\tkind\tsource\tobjects\n'
    { awk -F'\t' '{print $3"\t"$4"\thelm-manifest"}' "$RAW/helm_kinds.tsv"; awk -F'\t' '{print $1"\t"$2"\tlast-applied"}' "$RAW/lastapplied.tsv"; } \
      | sort | uniq -c | awk '{c=$1; sub(/^ *[0-9]+ /,""); print $0"\t"c}'; } > "$PRE/api_versions_in_use.tsv"

  # Tanzu / VKS
  { printf 'source\tnamespace\tcluster\tversion\tcp_replicas\tcp_vmclass\tworker_pools\tworker_replicas\tworker_vmclasses\tstorage_classes\tphase\n'
    aw '{ n=split($6,a,","); t=0; p=0; for(i=1;i<=n;i++) if(a[i]!=""){t+=a[i]; p++}; print "TKC",$1,$2,$3,$4,$5,p,t,$7,$8,$9 }' "$RAW/tkc.tsv"
    aw 'FILENAME==ARGV[1]{ seen[$1"\t"$2]=1; next } !(($1"\t"$2) in seen){ n=split($6,a,","); t=0; p=0; for(i=1;i<=n;i++) if(a[i]!=""){t+=a[i]; p++}; print "CAPI",$1,$2,$3,$4,$5,p,t,$7,$8,$9 }' "$RAW/tkc.tsv" "$RAW/capi.tsv"
  } > "$PRE/tanzu_clusters.tsv"
  { printf 'vm_class\tcpus\tmemory_gib\n'; aw '{print $1,$2,gib(q($3))}' "$RAW/vmclass.tsv"; } > "$PRE/vm_classes.tsv"
  { printf 'vm_class\tpower_state\tvms\n'; aw '{c[$1"\t"$2]++} END{for(k in c) print k,c[k]}' "$RAW/vms.tsv"; } > "$PRE/vm_counts.tsv"

  # namespaces roll-up
  { printf 'namespace\tsystem\tpsa_enforce\tpsa_warn\tlabel_keys\tquota\tquota_cpu\tquota_mem\tquota_storage\tlimitrange\tpods\tpods_running\tstandalone_pods\trestarts\tcpu_req_cores\tcpu_lim_cores\tmem_req_gib\tmem_lim_gib\tgpu\tcpu_used_cores\tmem_used_gib\tdeployments\tstatefulsets\tdaemonsets\tcronjobs\tjobs\tpvcs\tpvc_requested_gib\tpvc_used_gib\trwx_pvcs\tsvc_loadbalancer\tsvc_nodeport\tingresses\tnetworkpolicies\tage_days\n'
    aw -v SYS="$SYS_RE" '
      FILENAME==ARGV[1]{ qq[$1]=1; qc[$1]=$2; qm[$1]=$3; qs[$1]=$4; next }
      FILENAME==ARGV[2]{ lr[$1]=1; next }
      FILENAME==ARGV[3]{ p[$1]++; if($2=="Running") pr[$1]++; if($5=="") sp[$1]++; n=split($6,a," "); for(i=1;i<=n;i++) rs[$1]+=a[i]
                         if($2=="Running"||$2=="Pending"){ cr[$1]+=sumlist($7,"cpu"); mr[$1]+=sumlist($8,"mem"); cl[$1]+=sumlist($9,"cpu"); ml[$1]+=sumlist($10,"mem"); gp[$1]+=sumlist($11,"mem") } next }
      FILENAME==ARGV[4]{ uc[$1]+=mc($2); um[$1]+=q($3); used=1; next }
      FILENAME==ARGV[5]{ if(FNR>1) w[$1"\t"$2]++; next }
      FILENAME==ARGV[6]{ if(FNR>1){ pc[$1]++; pg[$1]+=$6; pu[$1]+=$9; if($4 ~ /ReadWriteMany/) rw[$1]++ } next }
      FILENAME==ARGV[7]{ if(FNR>1){ if($3=="LoadBalancer") lb[$1]++; if($3=="NodePort") np[$1]++ } next }
      FILENAME==ARGV[8]{ if(FNR>1) ig[$1]++; next }
      FILENAME==ARGV[9]{ if(FNR>1) nw[$1]=$2; next }
      { ns=$1; lk=jkeys($4); gsub(/(^|;)kubernetes\.io\/metadata\.name(;|$)/,";",lk); gsub(/^;|;$/,"",lk)
        print ns,(ns ~ SYS),$2,$3,lk,(ns in qq),qc[ns],qm[ns],qs[ns],(ns in lr),p[ns]+0,pr[ns]+0,sp[ns]+0,rs[ns]+0,
              sprintf("%.2f",cr[ns]/1000),sprintf("%.2f",cl[ns]/1000),gib(mr[ns]),gib(ml[ns]),gp[ns]+0,
              (used? sprintf("%.2f",uc[ns]/1000):""),(used? gib(um[ns]):""),
              w[ns"\tDeployment"]+0,w[ns"\tStatefulSet"]+0,w[ns"\tDaemonSet"]+0,w[ns"\tCronJob"]+0,w[ns"\tJob"]+0,
              pc[ns]+0,sprintf("%.2f",pg[ns]),sprintf("%.2f",pu[ns]),rw[ns]+0,lb[ns]+0,np[ns]+0,ig[ns]+0,nw[ns]+0,age($5) }' \
      "$RAW/quotas.tsv" "$RAW/limitranges.tsv" "$RAW/pods.tsv" "$RAW/top_pods.tsv" "$PRE/workloads.tsv" "$PRE/pvcs.tsv" \
      "$PRE/services.tsv" "$PRE/ingresses.tsv" "$PRE/networkpolicies.tsv" "$RAW/namespaces.tsv"
  } > "$PRE/namespaces.tsv"
  # workloads.tsv has a "system" flag via namespace pattern for the summary
}

summary() {
  local f="$PRE"
  aw -v SYS="$SYS_RE" -v VER="$(cut -f3 "$f/cluster.tsv" | tail -1)" -v CID="$(cut -f1 "$f/cluster.tsv" | tail -1)" -v CTXN="$(cut -f2 "$f/cluster.tsv" | tail -1)" \
     -v ERRS="$(grep -c . "$RAW/errors.tsv" 2>/dev/null || echo 0)" -v SNAP="$(cat "$RAW/volumesnapshots.txt")" -v SA="$(cat "$RAW/sa_count.txt")" '
    FNR==1{ fi++; next }
    fi==1{ nodes++; cp=($2 ~ /control-plane|master/); if(!cp){ wk++; wc+=$10; wm+=$11 } else cpn++; gpu+=$14; os[$6]=1
           n=split($18,lk,";"); for(i=1;i<=n;i++) if(lk[i]!="" && lk[i] !~ /^(kubernetes\.io|beta\.kubernetes\.io|node\.kubernetes\.io|topology\.kubernetes\.io)\//) clk[lk[i]]=1
           n=split($17,tt,";"); for(i=1;i<=n;i++) if(tt[i]!="") tnt[tt[i]]=1; next }
    fi==2{ nsall++; if(!$2){ nsapp++; podsapp+=$11; crq+=$15; mrq+=$17; cu+=$20; mu+=$21; if($20!="") met=1 } pods+=$11; np+=$34; next }
    fi==3{ if($1 ~ SYS) next; k[$2]++; if($21>0) priv++; if($27>0) hp++; if($22==1) hn++; if($6!="") op++; if($18>0) noreq++; next }
    fi==4{ pvc++; req+=$6; if($9!=""){ used+=$9; hu=1 } if($4 ~ /ReadWriteMany/) rwx++; if($5=="Block") blk++; if($12==0) unm++; next }
    fi==5{ pvt[$8]=1; next }
    fi==6{ if($3=="LoadBalancer") lb++; if($3=="NodePort") nport++; if($3=="ExternalName") en++; if($5==0 && $3!="ExternalName") nosel++; next }
    fi==7{ ing++; hosts+=$4; if($13>0) ngx++; if($15>0) snip++; next }
    fi==8{ crd++; if($10==1){ crdu++; cri+=$8 } next }
    fi==9{ wh++; if($6==1 || ($5!="" && $5 !~ SYS)) whn++; if($8==1) wha++; next }
    fi==10{ helm++; next }
    fi==11{ img++; if($5=="latest" || $5=="latest(implicit)") lat++; if(!$2) preg[$1]=1; next }
    fi==12{ dep++; next }
    fi==13{ if($7==1) pdbb++; next }
    fi==14{ tz++; next }
    fi==15{ if($6==0 && $5=="cluster-admin" && $1=="ClusterRoleBinding") ca++; next }
    fi==16{ if($2==0) crc++; next }
    END{ osl=""; for(o in os) osl=osl (osl==""?"":";") o; pt=""; for(x in pvt) pt=pt (pt==""?"":";") x
         nclk=0; for(x in clk) nclk++; ntn=0; for(x in tnt) ntn++; npr=0; for(x in preg) npr++
         split(VER,vv,"."); minor=vv[2]+0
         fl=""; if(minor && minor<32) fl=fl ";k8s<1.32"; if(k["StatefulSet"]||pvc) fl=fl ";stateful"; if(rwx) fl=fl ";rwx"; if(snip) fl=fl ";nginx-snippets"
         if(crdu>30) fl=fl ";many-crds"; if(dep) fl=fl ";deprecated-apis"; if(priv||hp) fl=fl ";privileged/hostpath"; if(gpu) fl=fl ";gpu"; if(op) fl=fl ";operators"; if(wha) fl=fl ";intercept-all-webhooks"
         sub(/^;/,"",fl)
         printf "cluster_id\tcontext\tk8s_version\tnodes\tcontrol_plane_nodes\tworker_nodes\tworker_vcpu\tworker_mem_gib\tgpus\tos_images\tnamespaces_total\tnamespaces_app\tpods_total\tpods_app\tapp_cpu_req_cores\tapp_mem_req_gib\tapp_cpu_used_cores\tapp_mem_used_gib\tdeployments\tstatefulsets\tdaemonsets_app\tcronjobs\tjobs\tpvcs\tpvc_requested_gib\tpvc_used_gib\tpvc_rwx\tpvc_block_mode\tpvc_unmounted\tpv_source_types\tvolumesnapshots\tsvc_loadbalancer\tsvc_nodeport\tsvc_externalname\tsvc_without_selector\tingresses\tingress_hosts\tingress_with_nginx_annotations\tingress_with_snippets\tnetworkpolicies\tcrds_total\tcrds_in_use\tcr_instances\twebhook_configs\twebhooks_non_system\twebhooks_intercept_all\thelm_releases\timages_distinct\timages_latest_tag\tprivate_registries\tdeprecated_api_hits\tprivileged_workloads\thostpath_workloads\thostnetwork_workloads\toperator_owned_workloads\tworkloads_without_requests\tpdbs_blocking_drain\tcustom_clusterroles\tnon_system_cluster_admin_bindings\tserviceaccounts\tnode_custom_label_keys\tnode_taints\ttanzu_clusters_seen\tcollection_errors\tcomplexity_flags\n"
         print CID,CTXN,VER,nodes+0,cpn+0,wk+0,sprintf("%.1f",wc/1000),sprintf("%.1f",wm),gpu+0,osl,nsall+0,nsapp+0,pods+0,podsapp+0,
               sprintf("%.1f",crq),sprintf("%.1f",mrq),(met?sprintf("%.1f",cu):""),(met?sprintf("%.1f",mu):""),
               k["Deployment"]+0,k["StatefulSet"]+0,k["DaemonSet"]+0,k["CronJob"]+0,k["Job"]+0,
               pvc+0,sprintf("%.1f",req),(hu?sprintf("%.1f",used):""),rwx+0,blk+0,unm+0,pt,SNAP+0,
               lb+0,nport+0,en+0,nosel+0,ing+0,hosts+0,ngx+0,snip+0,np+0,crd+0,crdu+0,cri+0,wh+0,whn+0,wha+0,helm+0,
               img+0,lat+0,npr,dep+0,priv+0,hp+0,hn+0,op+0,noreq+0,pdbb+0,crc+0,ca+0,SA+0,nclk,ntn,tz+0,ERRS+0,fl }' \
    "$f/nodes.tsv" "$f/namespaces.tsv" "$f/workloads.tsv" "$f/pvcs.tsv" "$f/pvs.tsv" "$f/services.tsv" "$f/ingresses.tsv" \
    "$f/crds.tsv" "$f/webhooks.tsv" "$f/helm_releases.tsv" "$f/images.tsv" "$f/deprecated_apis_requested.tsv" "$f/pdbs.tsv" \
    "$f/tanzu_clusters.tsv" "$f/bindings.tsv" "$f/clusterroles.tsv" > "$f/summary.tsv"
  { printf 'step\terror\n'; cat "$RAW/errors.tsv" 2>/dev/null; } > "$f/errors.tsv"
}

# ----------------------------------------------------------------------------
# Anonymisation: which column gets which treatment
#   ns    namespace (system namespaces stay clear)    h:X   always hash (prefix X)
#   h+:X  ';' list, hash each item                    s:X   hash only if sensitive / --strict
#   keys  ';' list of label/annotation keys (unknown prefixes hashed)
# ----------------------------------------------------------------------------
SYS_RE='^(kube-|default$|vmware-system|tkg-|tanzu-|svc-|cattle-|fleet-|cert-manager|ingress-nginx|nginx-ingress|monitoring|velero|istio-|linkerd|calico-|tigera-|cilium|metallb|kommander|kubecost|gatekeeper-|kyverno|local-path-storage|external-dns|flux-system|argocd|longhorn-|secretgen-controller|avi-system|antrea-|capi-|capv-|rancher-)'
PUBPFX='kubernetes.io k8s.io helm.sh cert-manager.io vmware.com cattle.io argoproj.io fluxcd.io traefik.io containo.us prometheus.io coreos.com istio.io linkerd.io cilium.io projectcalico.org antrea.io nvidia.com velero.io nutanix.com d2iq.io mesosphere.com bitnami.com elastic.co kyverno.io gatekeeper.sh openpolicyagent.org opentelemetry.io grafana.com metallb.io kube-vip.io rancher.io longhorn.io strimzi.io mongodb.com percona.com zalando.org cnpg.io min.io keda.sh knative.dev tekton.dev kubeflow.org dapr.io'
anon_spec() {
  case "$1" in
    cluster)        echo "cluster_id=h:cluster context=h:ctx" ;;
    summary)        echo "cluster_id=h:cluster context=h:ctx" ;;
    api_versions_served) echo "groupversion=s:gv" ;;
    nodes)          echo "node=h:node taints=keys label_keys=keys" ;;
    namespaces)     echo "namespace=ns label_keys=keys" ;;
    workloads)      echo "namespace=ns name=h:wl owner_kind=s:own helm_chart=s:chart managed_by=s:mb node_selector_keys=keys toleration_keys=keys priority_class=s:pc pvc_templates=s:sc" ;;
    images)         echo "registry=reg project=h:proj repository=s:img" ;;
    pvcs)           echo "namespace=ns pvc=h:pvc storage_class=s:sc" ;;
    pvs)            echo "pv=h:pv storage_class=s:sc claim_namespace=ns" ;;
    storageclasses) echo "storage_class=s:sc parameter_keys=keys" ;;
    services)       echo "namespace=ns service=h:svc external_name=h:host annotation_keys=keys" ;;
    ingresses)      echo "namespace=ns ingress=h:ing domains=h+:dom class=s:ic annotation_keys=keys" ;;
    ingressclasses) echo "ingress_class=s:ic" ;;
    networkpolicies) echo "namespace=ns" ;;
    crds)           echo "crd=s:crd group=s:grp managed_by=s:mb helm_release_namespace=ns helm_release=h:rel" ;;
    webhooks)       echo "webhook_config=s:wh service_namespaces=nsl rules=s:rule helm_release=h:rel" ;;
    apiservices)    echo "apiservice=s:apis service_namespace=ns" ;;
    pdbs)           echo "namespace=ns pdb=h:pdb" ;;
    hpa)            echo "namespace=ns" ;;
    priorityclasses) echo "priority_class=s:pc" ;;
    clusterroles)   echo "clusterrole=s:cr" ;;
    bindings)       echo "namespace=ns binding=s:bind role=s:cr groups=h+:grp" ;;
    helm_releases)  echo "namespace=ns release=h:rel chart=s:chart" ;;
    tanzu_clusters) echo "namespace=h:vns cluster=h:tkc storage_classes=s:sc" ;;
    *)              echo "" ;;
  esac
}

ANON_AWK='
function issens(v,  i,lv){ if(SENS=="") return 0; lv=tolower(v); for(i=1;i<=ns_;i++) if(index(lv,sl[i])) return 1; return 0 }
function pubpfx(p,  i,lp){ lp=tolower(p); for(i=1;i<=np_;i++){ if(lp==pp[i]) return 1; if(length(lp)>length(pp[i]) && substr(lp,length(lp)-length(pp[i]))=="." pp[i]) return 1 } return 0 }
function want(k,v){ if(v=="") return v; if(MODE=="collect"){ print k "\t" v; return v } return ((k "\t" v) in M) ? M[k "\t" v] : v }
function hh(k,v){ if(v=="") return v; if(CLEAR && !issens(v)) return v; return want(k,v) }
function tx(t,v,  a,n,i,out,p,r){
  if(v=="") return v
  if(t=="ns"){ if(v ~ SYS && !issens(v)) return v; return hh("ns",v) }
  if(t=="nsl"){ n=split(v,a," "); out=""; for(i=1;i<=n;i++) out=out (out==""?"":";") tx("ns",a[i]); return out }
  if(t=="reg"){ if(PUBREG[tolower(v)] && !issens(v)) return v; return hh("reg",v) }
  if(substr(t,1,2)=="h:") return hh(substr(t,3),v)
  if(substr(t,1,3)=="h+:"){ n=split(v,a,";"); out=""; for(i=1;i<=n;i++) out=out (out==""?"":";") hh(substr(t,4),a[i]); return out }
  if(substr(t,1,2)=="s:"){ if(STRICT || issens(v)) return want(substr(t,3),v); return v }
  if(t=="keys"){ n=split(v,a,";"); out=""
    for(i=1;i<=n;i++){ r=a[i]; if(index(r,"/")){ p=substr(r,1,index(r,"/")-1); if(!pubpfx(p) || issens(p)) r=want("pfx",p) substr(r,index(r,"/")) }
      if(issens(r)) r=want("key",r); out=out (out==""?"":";") r }
    return out }
  return v }
BEGIN{ FS=OFS="\t"; ns_=split(SENS,sl,"|"); np_=split(PUBPFX,pp," "); n=split(PUBR,a," "); for(i=1;i<=n;i++) PUBREG[a[i]]=1
       if(MODE=="apply"){ while((getline line < MAPF)>0){ split(line,f,"\t"); M[f[1] "\t" f[2]]=f[3] } } }
FNR==1{ nc=NF; for(i=1;i<=NF;i++){ H[i]=$i; TY[i]="" } n=split(SPEC,a," "); for(i=1;i<=n;i++){ split(a[i],kv,"="); for(j=1;j<=nc;j++) if(H[j]==kv[1]) TY[j]=kv[2] }
        if(MODE=="apply") emit(); next }
{ for(i=1;i<=NF;i++){ if(TY[i]!="") $i=tx(TY[i],$i)
    if(issens($i)) $i=want("str",$i) }
  if(MODE=="apply") emit() }
function emit(  i,s,v){ s=""; for(i=1;i<=NF;i++){ v=$i; gsub(/"/,"\"\"",v); if(v ~ /[",;\n]/ || v ~ / /) v="\"" v "\""; s=s (i>1?",":"") v } print s }
'
PUBREG='docker.io index.docker.io quay.io gcr.io k8s.gcr.io registry.k8s.io ghcr.io mcr.microsoft.com public.ecr.aws docker.elastic.co nvcr.io registry.gitlab.com projects.registry.vmware.com registry.access.redhat.com registry.redhat.io registry.suse.com'

anonymise() {  # $1 pre dir, $2 out dir
  local pre="$1" dst="$2" f base spec
  : > "$RAW/tohash.tsv"
  for f in "$pre"/*.tsv; do
    base="$(basename "$f" .tsv)"; spec="$(anon_spec "$base")"
    awk -v MODE=collect -v SPEC="$spec" -v SYS="$SYS_RE" -v SENS="$SENS_LIST" -v PUBPFX="$PUBPFX" -v PUBR="$PUBREG" \
        -v STRICT="$STRICT" -v CLEAR="$CLEAR" "$ANON_AWK" "$f" >> "$RAW/tohash.tsv"
  done
  sort -u "$RAW/tohash.tsv" | while IFS=$'\t' read -r k v; do
    [ -n "$v" ] || continue
    printf '%s\t%s\t%s-%s\n' "$k" "$v" "$k" "$(printf '%s|%s|%s' "$SALT" "$k" "$v" | $SHA | cut -c1-10)"
  done > "$RAW/map.tsv"
  for f in "$pre"/*.tsv; do
    base="$(basename "$f" .tsv)"; spec="$(anon_spec "$base")"
    awk -v MODE=apply -v MAPF="$RAW/map.tsv" -v SPEC="$spec" -v SYS="$SYS_RE" -v SENS="$SENS_LIST" -v PUBPFX="$PUBPFX" -v PUBR="$PUBREG" \
        -v STRICT="$STRICT" -v CLEAR="$CLEAR" "$ANON_AWK" "$f" \
      | sed -E -e 's#https?://[^",[:space:]]+#<url>#g' \
               -e 's/(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)/\1<ip>\3/g' \
               -e 's/(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)/\1<ip>\3/g' \
               -e 's/([0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}/<ip6>/g' > "$dst/$base.csv"
  done
  # merge into the private name map (hash -> real value)
  { cat "$PRIV/name-map.tsv" 2>/dev/null; awk -F'\t' '{print $3"\t"$1"\t"$2}' "$RAW/map.tsv"; } | sort -u > "$PRIV/name-map.tsv.new" \
    && mv "$PRIV/name-map.tsv.new" "$PRIV/name-map.tsv"
}

write_readme() {
cat > "$1/README.txt" <<EOF
k8s-discovery.sh v$VERSION - output for one cluster

Collected (read-only): node shape and label/taint KEYS, namespaces, workload specs
(replicas, requests/limits, security flags, volume types), aggregated pod counts,
'kubectl top' usage, PVC/PV/StorageClass metadata and used bytes (if --volume-usage),
Services, Ingresses (annotation KEYS and ingress-nginx categories), NetworkPolicy
counts, CRDs with instance counts and owning Helm release, webhooks, aggregated APIs,
ClusterRoles/bindings shape, Helm chart name/version/status and the apiVersion/kind of
objects in each release, running images (repository/tag), deprecated API requests,
Tanzu/VKS cluster inventory (when run against a Supervisor).

NOT collected: IP addresses, node/host names, Secret or ConfigMap values, env vars,
commands, annotation or label VALUES, Ingress hostnames, NFS servers/paths, private
registry hostnames. Names are salted SHA-256 hashes; system namespaces stay readable.
$( [ -n "$SENS_LIST" ] && echo "$(printf '%s' "$SENS_LIST" | awk -F'|' '{print NF}') sensitive string(s) were hashed wherever they appeared (list not written)." )

Review before sharing: images.csv (repository name + tag readable unless --strict),
helm_releases.csv (chart names readable unless --strict), crds.csv (API groups).
The private name map is NOT in this folder.
EOF
}

self_check() {
  local hits=0 s
  if grep -rEq '(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)' "$1"; then log "  WARNING: IPv4-like string found in $1"; hits=1; fi
  if [ -n "$SENS_LIST" ]; then
    printf '%s\n' "$SENS_LIST" | tr '|' '\n' | while read -r s; do
      [ -n "$s" ] && grep -rqiF -- "$s" "$1" && log "  WARNING: sensitive string found in $1 (len ${#s})"
    done
  fi
  return $hits
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
if [ "$ALL_CTX" = 1 ]; then
  mapfile -t CONTEXTS < <("$KUBECTL" ${KCFG:+--kubeconfig "$KCFG"} config get-contexts -o name 2>/dev/null)
  [ ${#CONTEXTS[@]} -gt 0 ] || die "no contexts found in kubeconfig"
fi
[ ${#CONTEXTS[@]} -gt 0 ] || CONTEXTS=("")

FLEET_HDR=0
: > "$OUT/.fleet.csv"
for CTX in "${CONTEXTS[@]}"; do
  RAW="$(mktemp -d "$PRIV/raw-XXXXXX")"; PRE="$RAW/pre"; mkdir -p "$PRE"; : > "$RAW/errors.tsv"
  log "== context ${CTX:+ctx-$(printf '%s' "$CTX" | $SHA | cut -c1-8)}"
  for step in cluster nodes namespaces workloads pods storage network extensions rbac helm deprecated tanzu; do
    t0=$SECONDS; log "  - $step"; "collect_$step"; log "    $((SECONDS - t0))s"
  done
  log "  - shaping"
  : > "$PRE/pvs.tsv.src"
  aw '{ t="other"; if($8!="")t="csi:"$8; else if($9!="")t="nfs"; else if($10!="")t="hostPath"; else if($11!="")t="local"; else if($12!="")t="vsphereVolume"; else if($13!="")t="iscsi"; print $1,"","","","","","",t }' "$RAW/pvs.tsv" > "$PRE/pvs.tsv.src"
  shape; rm -f "$PRE/pvs.tsv.src" "$RAW"/*.tmp
  summary
  log "  - anonymising"
  STAGE="$RAW/out"; mkdir -p "$STAGE"
  anonymise "$PRE" "$STAGE"
  CID="$(awk -F',' 'NR==2{print $1}' "$STAGE/summary.csv")"; [ -n "$CID" ] || CID="cluster-unknown"
  rm -rf "${OUT:?}/$CID"; mkdir -p "$OUT/$CID"; mv "$STAGE"/* "$OUT/$CID/"
  write_readme "$OUT/$CID"
  if [ "$FLEET_HDR" = 0 ]; then head -1 "$OUT/$CID/summary.csv" >> "$OUT/.fleet.csv"; FLEET_HDR=1; fi
  tail -n +2 "$OUT/$CID/summary.csv" >> "$OUT/.fleet.csv"
  self_check "$OUT/$CID" || true
  log "  -> $OUT/$CID ($(($(grep -c . "$RAW/errors.tsv" 2>/dev/null || echo 0))) collection errors - see errors.csv)"
  if [ "$KEEPRAW" = 1 ]; then log "  raw kept (NOT shareable): $RAW"; else rm -rf "$RAW"; fi
done
mv "$OUT/.fleet.csv" "$OUT/fleet_summary.csv"
chmod 600 "$PRIV/name-map.tsv" "$PRIV/salt.txt" 2>/dev/null || true

if [ "$NOTAR" = 0 ]; then
  TGZ="k8s-discovery-$(date -u +%Y%m%d-%H%M).tar.gz"
  tar --exclude='*.tar.gz' -czf "$PRIV/$TGZ.part" -C "$OUT" . && mv "$PRIV/$TGZ.part" "$OUT/$TGZ" && log "Bundle: $OUT/$TGZ"
fi
log "Private - do not share: $PRIV (salt.txt, name-map.tsv)"
