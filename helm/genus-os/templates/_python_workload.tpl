{{/*
Shared workload template for the Python services (engine, bridge,
orchestrator). Pass:
  .root      — top-level dot
  .component — string key into .root.Values (e.g. "engine")

Optional, for the engine only (all default to the root engine's own values,
so the root engine passes none of them except its tenant):
  .values        — the component's values map (default .root.Values.<component>)
  .name          — resource name (default <fullname>-<component>)
  .tenant        — tenant engine cell id: genus-os/tenant on every label and
                   selector this renders
  .podTenant     — genus-os/tenant on the POD labels only (the root engine:
                   its Deployment selector is immutable and must not gain it)
  .narrowService — also put .podTenant in the Service and PDB selectors, which
                   are mutable (the root engine, once tenant cells exist)
  .workspace     — workspace map, same shape as .root.Values.workspace
  .workspaceKey  — its values path, for error messages (default "workspace")
  .claimName     — workspace PVC name (default genus-os.workspaceClaimName)
  .pinnedEnv     — env that wins over .values.env (the tenant identity)
  .googleSecretName — Secret mounted for googleWorkspace (default
                   <fullname>-engine-google, the VSO-materialized one)

Renders:
  - Deployment
  - Service (ClusterIP)
  - Ingress (if .ingress.enabled)
  - PodDisruptionBudget (if .podDisruptionBudget.enabled)
*/}}
{{- define "genus-os.pythonWorkload" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $cv := .values | default (index $root.Values $component) -}}
{{- if $cv.enabled -}}
{{- $fullName := .name | default (printf "%s-%s" (include "genus-os.fullname" $root) $component | trunc 63 | trimSuffix "-") -}}
{{- $tenant := .tenant | default "" -}}
{{- $podTenant := .podTenant | default $tenant -}}
{{- $selectTenant := $tenant -}}
{{- if and .podTenant .narrowService -}}{{- $selectTenant = .podTenant -}}{{- end -}}
{{- $ws := .workspace | default $root.Values.workspace -}}
{{- $wsKey := .workspaceKey | default "workspace" -}}
{{- $claimName := .claimName | default (include "genus-os.workspaceClaimName" $root) -}}
{{- $gwArgs := dict "root" $root "gw" ($cv.googleWorkspace | default dict) "secretName" (.googleSecretName | default (printf "%s-engine-google" (include "genus-os.fullname" $root))) "checkVault" (not .googleSecretName) -}}
{{- /* The vault's readers: the bridge stores credentials, the engines read them. */ -}}
{{- $vaultReader := has $component (list "engine" "bridge") -}}
{{- $port := $cv.service.port | int -}}
{{- $serviceEnv := dict -}}
{{- if eq $component "engine" -}}
{{- $_ := set $serviceEnv "ROBOTHOR_ENGINE_HOST" "0.0.0.0" -}}
{{- $_ := set $serviceEnv "ROBOTHOR_WORKSPACE" $ws.mountPath -}}
{{- $_ := set $serviceEnv "ROBOTHOR_MANIFEST_DIR" (printf "%s/docs/agents" $ws.mountPath) -}}
{{- $_ := set $serviceEnv "ROBOTHOR_ALLOW_EMPTY_FLEET" (toString $ws.allowEmptyFleet) -}}
{{- $_ := set $serviceEnv "ROBOTHOR_MIN_AGENT_COUNT" (toString $ws.minAgentCount) -}}
{{- $_ := set $serviceEnv "ROBOTHOR_REQUIRED_AGENT_IDS" (join "," $ws.requiredAgentIds) -}}
{{- if $root.Values.bridge.enabled -}}
{{- $_ := set $serviceEnv "BRIDGE_URL" (printf "http://%s-bridge:%v" (include "genus-os.fullname" $root) $root.Values.bridge.service.port) -}}
{{- end -}}
{{- if $root.Values.orchestrator.enabled -}}
{{- $_ := set $serviceEnv "ORCHESTRATOR_URL" (printf "http://%s-orchestrator:%v" (include "genus-os.fullname" $root) $root.Values.orchestrator.service.port) -}}
{{- end -}}
{{- end -}}
{{- if eq $component "bridge" -}}
{{- $_ := set $serviceEnv "ROBOTHOR_BRIDGE_HOST" "0.0.0.0" -}}
{{- if $root.Values.orchestrator.enabled -}}
{{- $_ := set $serviceEnv "MEMORY_URL" (printf "http://%s-orchestrator:%v" (include "genus-os.fullname" $root) $root.Values.orchestrator.service.port) -}}
{{- end -}}
{{- if $root.Values.engine.enabled -}}
{{- $_ := set $serviceEnv "ROBOTHOR_ENGINE_URL" (include "genus-os.engineUrl" $root) -}}
{{- $_ := set $serviceEnv "GENUS_TENANT_ENGINE_URL_TEMPLATE" (include "genus-os.tenantEngineUrlTemplate" $root) -}}
{{- end -}}
{{- with ($root.Values.logs | default dict).url -}}
{{- $_ := set $serviceEnv "GENUS_LOGS_URL" (toString .) -}}
{{- end -}}
{{- /* The bridge gates and routes on the root tenant: it must agree with the
       root engine (and the dashboard) about which tenant that is. */ -}}
{{- $_ := set $serviceEnv "ROBOTHOR_DEFAULT_TENANT" (include "genus-os.rootTenant" $root) -}}
{{- end -}}
{{- /* Tenant switching: the bridge issues the acting token and every engine
       enforces it, so one value reaches all of them. Rendered only when set,
       so an unset key leaves the code's own default (`off`) in charge. */ -}}
{{- if has $component (list "engine" "bridge") -}}
{{- $tenancy := $root.Values.tenancy | default dict -}}
{{- with $tenancy.switchMode -}}
{{- if not (has (toString .) (list "off" "observe" "enforce")) -}}
{{- fail (printf "tenancy.switchMode must be one of off, observe, enforce, got %q" (toString .)) -}}
{{- end -}}
{{- $_ := set $serviceEnv "GENUS_TENANT_SWITCH_MODE" (toString .) -}}
{{- end -}}
{{- if hasKey $tenancy "allowInertRls" -}}
{{- if not (kindIs "invalid" $tenancy.allowInertRls) -}}
{{- $_ := set $serviceEnv "GENUS_TENANT_SWITCH_ALLOW_INERT_RLS" (toString $tenancy.allowInertRls) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $componentEnv := mergeOverwrite $serviceEnv ($cv.env | default dict) (.pinnedEnv | default dict) -}}
{{- if and (eq $component "engine") $ws.configMap.name $ws.persistence.enabled -}}
{{- fail (printf "%s.configMap.name and %s.persistence.enabled are mutually exclusive" $wsKey $wsKey) -}}
{{- end -}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $fullName }}
  labels:
    {{- include "genus-os.labels" (dict "root" $root "component" $component "tenant" $tenant) | nindent 4 }}
spec:
  replicas: {{ $cv.replicaCount }}
  revisionHistoryLimit: 5
  strategy:
    {{- if $cv.strategy }}
    {{- toYaml $cv.strategy | nindent 4 }}
    {{- else }}
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
    {{- end }}
  selector:
    matchLabels:
      {{- include "genus-os.selectorLabels" (dict "root" $root "component" $component "tenant" $tenant) | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "genus-os.labels" (dict "root" $root "component" $component "tenant" $podTenant) | nindent 8 }}
        {{- with $root.Values.global.podLabels }}{{- toYaml . | nindent 8 }}{{- end }}
        {{- with $cv.podLabels }}{{- toYaml . | nindent 8 }}{{- end }}
      annotations:
        checksum/config: {{ $root.Values.global.imageTag | sha256sum }}
        {{- with $root.Values.global.podAnnotations }}{{- toYaml . | nindent 8 }}{{- end }}
        {{- with $cv.podAnnotations }}{{- toYaml . | nindent 8 }}{{- end }}
    spec:
      serviceAccountName: {{ include "genus-os.serviceAccountName" (dict "root" $root "component" $component) }}
      # Never auto-mount the API token into the privileged application
      # container. A short-lived projected token is mounted only into the
      # migration watcher init container below.
      automountServiceAccountToken: false
      {{- with $root.Values.global.imagePullSecrets }}
      imagePullSecrets:
        {{- range . }}
        - name: {{ . }}
        {{- end }}
      {{- end }}
      securityContext:
        {{- toYaml $root.Values.global.podSecurityContext | nindent 8 }}
      {{- $ns := include "genus-os.nodeSelector" (dict "root" $root "component" $component "values" $cv) }}
      {{- if $ns | trim }}
      nodeSelector:
        {{- $ns | nindent 8 }}
      {{- end }}
      {{- $tols := include "genus-os.tolerations" (dict "root" $root "component" $component "values" $cv) }}
      {{- if $tols | trim }}
      tolerations:
        {{- $tols | nindent 8 }}
      {{- end }}
      {{- $aff := include "genus-os.affinity" (dict "root" $root "component" $component "values" $cv "tenant" $tenant) }}
      {{- if $aff | trim }}
      affinity:
        {{- $aff | nindent 8 }}
      {{- end }}
      {{- if $root.Values.migrations.enabled }}
      # Block on the migration Job for this release. Pattern doc:
      # https://github.com/Ironsail-llc/aws-infrastucture/blob/main/docs/helm-db-migrations.md
      initContainers:
        - name: wait-for-migrations
          image: "{{ $root.Values.kubectl.image.repository }}:{{ $root.Values.kubectl.image.tag }}"
          imagePullPolicy: {{ $root.Values.kubectl.image.pullPolicy }}
          securityContext:
            {{- toYaml $root.Values.global.securityContext | nindent 12 }}
          env:
            - name: JOB_NAME
              value: {{ include "genus-os.migrationJobName" $root }}
            - name: WAIT_TIMEOUT_SECONDS
              value: {{ $root.Values.migrations.waitTimeoutSeconds | quote }}
            - name: MISSING_JOB_GRACE_SECONDS
              value: {{ $root.Values.migrations.missingJobGraceSeconds | quote }}
          command:
            - sh
            - -c
            - |
              # The wait is BOUNDED and fails loudly. The old version looped
              # `while true` and swallowed kubectl's stderr, so "Job deleted by
              # TTL" and "RBAC forbids reading this Job" were indistinguishable
              # from "Job still running" -- pods sat in Init:0/1 for weeks,
              # which in turn wedged Karpenter behind their PodDisruptionBudget.
              echo "Waiting for migration Job $JOB_NAME (timeout ${WAIT_TIMEOUT_SECONDS}s)..."
              start=$(date +%s)
              while true; do
                elapsed=$(( $(date +%s) - start ))
                if status=$(kubectl get job "$JOB_NAME" \
                      -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}|{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null); then
                  case "$status" in
                    True\|*) echo "Migration Job $JOB_NAME complete."; exit 0 ;;
                    *\|True) echo "Migration Job $JOB_NAME failed; aborting pod start."; exit 1 ;;
                  esac
                elif [ "$elapsed" -ge "$MISSING_JOB_GRACE_SECONDS" ]; then
                  echo "Migration Job $JOB_NAME is not readable after ${elapsed}s."
                  echo "It was deleted (ttlSecondsAfterFinished?) or this ServiceAccount cannot read it."
                  kubectl get job "$JOB_NAME" || true
                  exit 1
                fi
                if [ "$elapsed" -ge "$WAIT_TIMEOUT_SECONDS" ]; then
                  echo "Migration Job $JOB_NAME did not finish within ${WAIT_TIMEOUT_SECONDS}s; aborting pod start."
                  exit 1
                fi
                sleep 3
              done
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            - name: migration-api-token
              mountPath: /var/run/secrets/kubernetes.io/serviceaccount
              readOnly: true
          resources:
            {{- toYaml $root.Values.global.initContainerResources | nindent 12 }}
      {{- end }}
      containers:
        - name: {{ $component }}
          image: {{ include "genus-os.image" (dict "root" $root "component" $component "values" $cv) }}
          imagePullPolicy: {{ include "genus-os.imagePullPolicy" (dict "root" $root "component" $component "values" $cv) }}
          securityContext:
            {{- toYaml $root.Values.global.securityContext | nindent 12 }}
          {{- with $cv.command }}
          command:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $cv.workingDir }}
          workingDir: {{ . | quote }}
          {{- end }}
          ports:
            - name: http
              containerPort: {{ $port }}
              protocol: TCP
          env:
            {{- include "genus-os.commonEnv" $root | nindent 12 }}
            {{- with include "genus-os.dbCaEnv" $root | trim }}{{ . | nindent 12 }}{{- end }}
            {{- if eq $component "engine" }}
            {{- with include "genus-os.googleWorkspaceEnv" $gwArgs | trim }}{{ . | nindent 12 }}{{- end }}
            {{- end }}
            {{- if $vaultReader }}
            {{- with include "genus-os.vaultKeyEnv" $root | trim }}{{ . | nindent 12 }}{{- end }}
            {{- end }}
            {{- range $k, $v := $componentEnv }}
            - name: {{ $k }}
              value: {{ $v | quote }}
            {{- end }}
          envFrom:
            {{- include "genus-os.envFromSecret" (dict "root" $root "component" $component "values" $cv "tenant" $tenant) | nindent 12 }}
          {{- $liveness := include "genus-os.httpProbe" (dict "root" $root "component" $component "values" $cv "probe" "liveness" "port" "http") }}
          {{- if $liveness | trim }}
          livenessProbe:
            {{- $liveness | nindent 12 }}
          {{- end }}
          {{- $readiness := include "genus-os.httpProbe" (dict "root" $root "component" $component "values" $cv "probe" "readiness" "port" "http") }}
          {{- if $readiness | trim }}
          readinessProbe:
            {{- $readiness | nindent 12 }}
          {{- end }}
          volumeMounts:
            {{- if eq $component "engine" }}
            - name: workspace
              mountPath: {{ $ws.mountPath | quote }}
              readOnly: {{ or $ws.readOnly (ne $ws.configMap.name "") }}
            {{- end }}
            {{- with $cv.extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
            - name: tmp
              mountPath: /tmp
            - name: runtime-cache
              mountPath: /app/.cache
            {{- with include "genus-os.dbCaVolumeMount" $root | trim }}{{ . | nindent 12 }}{{- end }}
            {{- if eq $component "engine" }}
            {{- with include "genus-os.googleWorkspaceVolumeMount" $gwArgs | trim }}{{ . | nindent 12 }}{{- end }}
            {{- end }}
            {{- if $vaultReader }}
            {{- with include "genus-os.vaultKeyVolumeMount" $root | trim }}{{ . | nindent 12 }}{{- end }}
            {{- end }}
          resources:
            {{- toYaml $cv.resources | nindent 12 }}
      volumes:
        {{- if eq $component "engine" }}
        - name: workspace
          {{- if $ws.configMap.name }}
          configMap:
            name: {{ $ws.configMap.name }}
            {{- with $ws.configMap.items }}
            items:
              {{- toYaml . | nindent 14 }}
            {{- end }}
          {{- else if $ws.persistence.enabled }}
          persistentVolumeClaim:
            claimName: {{ $claimName }}
          {{- else }}
          emptyDir: {}
          {{- end }}
        {{- end }}
        {{- with $cv.extraVolumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
        - name: tmp
          emptyDir: {}
        - name: runtime-cache
          emptyDir: {}
        {{- with include "genus-os.dbCaVolume" $root | trim }}{{ . | nindent 8 }}{{- end }}
        {{- if eq $component "engine" }}
        {{- with include "genus-os.googleWorkspaceVolume" $gwArgs | trim }}{{ . | nindent 8 }}{{- end }}
        {{- end }}
        {{- if $vaultReader }}
        {{- with include "genus-os.vaultKeyVolume" $root | trim }}{{ . | nindent 8 }}{{- end }}
        {{- end }}
        {{- if $root.Values.migrations.enabled }}
        - name: migration-api-token
          projected:
            defaultMode: 0400
            sources:
              - serviceAccountToken:
                  path: token
                  expirationSeconds: 600
              - configMap:
                  name: kube-root-ca.crt
                  items:
                    - key: ca.crt
                      path: ca.crt
              - downwardAPI:
                  items:
                    - path: namespace
                      fieldRef:
                        fieldPath: metadata.namespace
        {{- end }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ $fullName }}
  labels:
    {{- include "genus-os.labels" (dict "root" $root "component" $component "tenant" $tenant) | nindent 4 }}
  {{- with $cv.service.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  type: ClusterIP
  ports:
    - name: http
      port: {{ $port }}
      targetPort: http
      protocol: TCP
  selector:
    {{- include "genus-os.selectorLabels" (dict "root" $root "component" $component "tenant" $selectTenant) | nindent 4 }}
{{- if and $cv.ingress $cv.ingress.enabled }}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ $fullName }}
  labels:
    {{- include "genus-os.labels" (dict "root" $root "component" $component "tenant" $tenant) | nindent 4 }}
  {{- with $cv.ingress.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  ingressClassName: {{ $cv.ingress.className }}
  rules:
    - host: {{ required (printf "%s.ingress.host is required when %s.ingress.enabled" $component $component) $cv.ingress.host }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ $fullName }}
                port:
                  number: {{ $port }}
  {{- with $cv.ingress.tls }}
  tls:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
{{- if and $cv.podDisruptionBudget $cv.podDisruptionBudget.enabled }}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ $fullName }}
  labels:
    {{- include "genus-os.labels" (dict "root" $root "component" $component "tenant" $tenant) | nindent 4 }}
spec:
  minAvailable: {{ $cv.podDisruptionBudget.minAvailable }}
  unhealthyPodEvictionPolicy: AlwaysAllow
  selector:
    matchLabels:
      {{- include "genus-os.selectorLabels" (dict "root" $root "component" $component "tenant" $selectTenant) | nindent 6 }}
{{- end }}
{{- end }}
{{- end -}}
