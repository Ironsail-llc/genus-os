{{/*
Shared workload template for the Python services (engine, bridge,
orchestrator). Pass:
  .root      — top-level dot
  .component — string key into .root.Values (e.g. "engine")

Renders:
  - Deployment
  - Service (ClusterIP)
  - Ingress (if .ingress.enabled)
  - PodDisruptionBudget (if .podDisruptionBudget.enabled)
*/}}
{{- define "genus-os.pythonWorkload" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $cv := index $root.Values $component -}}
{{- if $cv.enabled -}}
{{- $fullName := printf "%s-%s" (include "genus-os.fullname" $root) $component | trunc 63 | trimSuffix "-" -}}
{{- $port := $cv.service.port | int -}}
{{- $serviceEnv := dict -}}
{{- if eq $component "engine" -}}
{{- $_ := set $serviceEnv "ROBOTHOR_ENGINE_HOST" "0.0.0.0" -}}
{{- $_ := set $serviceEnv "ROBOTHOR_WORKSPACE" $root.Values.workspace.mountPath -}}
{{- $_ := set $serviceEnv "ROBOTHOR_MANIFEST_DIR" (printf "%s/docs/agents" $root.Values.workspace.mountPath) -}}
{{- $_ := set $serviceEnv "ROBOTHOR_ALLOW_EMPTY_FLEET" (toString $root.Values.workspace.allowEmptyFleet) -}}
{{- $_ := set $serviceEnv "ROBOTHOR_MIN_AGENT_COUNT" (toString $root.Values.workspace.minAgentCount) -}}
{{- $_ := set $serviceEnv "ROBOTHOR_REQUIRED_AGENT_IDS" (join "," $root.Values.workspace.requiredAgentIds) -}}
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
{{- end -}}
{{- $componentEnv := mergeOverwrite $serviceEnv ($cv.env | default dict) -}}
{{- if and (eq $component "engine") $root.Values.workspace.configMap.name $root.Values.workspace.persistence.enabled -}}
{{- fail "workspace.configMap.name and workspace.persistence.enabled are mutually exclusive" -}}
{{- end -}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $fullName }}
  labels:
    {{- include "genus-os.labels" (dict "root" $root "component" $component) | nindent 4 }}
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
      {{- include "genus-os.selectorLabels" (dict "root" $root "component" $component) | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "genus-os.labels" (dict "root" $root "component" $component) | nindent 8 }}
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
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- toYaml $root.Values.global.podSecurityContext | nindent 8 }}
      {{- $ns := include "genus-os.nodeSelector" (dict "root" $root "component" $component) }}
      {{- if $ns | trim }}
      nodeSelector:
        {{- $ns | nindent 8 }}
      {{- end }}
      {{- $tols := include "genus-os.tolerations" (dict "root" $root "component" $component) }}
      {{- if $tols | trim }}
      tolerations:
        {{- $tols | nindent 8 }}
      {{- end }}
      {{- $aff := include "genus-os.affinity" (dict "root" $root "component" $component) }}
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
          command:
            - sh
            - -c
            - |
              echo "Waiting for migration Job $JOB_NAME..."
              while true; do
                if [ "$(kubectl get job "$JOB_NAME" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null)" = "True" ]; then
                  echo "Migration Job $JOB_NAME complete."
                  exit 0
                fi
                if [ "$(kubectl get job "$JOB_NAME" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null)" = "True" ]; then
                  echo "Migration Job $JOB_NAME failed; aborting pod start."
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
          image: {{ include "genus-os.image" (dict "root" $root "component" $component) }}
          imagePullPolicy: {{ include "genus-os.imagePullPolicy" (dict "root" $root "component" $component) }}
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
            {{- range $k, $v := $componentEnv }}
            - name: {{ $k }}
              value: {{ $v | quote }}
            {{- end }}
          envFrom:
            {{- include "genus-os.envFromSecret" (dict "root" $root "component" $component) | nindent 12 }}
          {{- $liveness := include "genus-os.httpProbe" (dict "root" $root "component" $component "probe" "liveness" "port" "http") }}
          {{- if $liveness | trim }}
          livenessProbe:
            {{- $liveness | nindent 12 }}
          {{- end }}
          {{- $readiness := include "genus-os.httpProbe" (dict "root" $root "component" $component "probe" "readiness" "port" "http") }}
          {{- if $readiness | trim }}
          readinessProbe:
            {{- $readiness | nindent 12 }}
          {{- end }}
          volumeMounts:
            {{- if eq $component "engine" }}
            - name: workspace
              mountPath: {{ $root.Values.workspace.mountPath | quote }}
              readOnly: {{ or $root.Values.workspace.readOnly (ne $root.Values.workspace.configMap.name "") }}
            {{- end }}
            - name: tmp
              mountPath: /tmp
            - name: runtime-cache
              mountPath: /app/.cache
          resources:
            {{- toYaml $cv.resources | nindent 12 }}
      volumes:
        {{- if eq $component "engine" }}
        - name: workspace
          {{- if $root.Values.workspace.configMap.name }}
          configMap:
            name: {{ $root.Values.workspace.configMap.name }}
            {{- with $root.Values.workspace.configMap.items }}
            items:
              {{- toYaml . | nindent 14 }}
            {{- end }}
          {{- else if $root.Values.workspace.persistence.enabled }}
          persistentVolumeClaim:
            claimName: {{ include "genus-os.workspaceClaimName" $root }}
          {{- else }}
          emptyDir: {}
          {{- end }}
        {{- end }}
        - name: tmp
          emptyDir: {}
        - name: runtime-cache
          emptyDir: {}
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
    {{- include "genus-os.labels" (dict "root" $root "component" $component) | nindent 4 }}
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
    {{- include "genus-os.selectorLabels" (dict "root" $root "component" $component) | nindent 4 }}
{{- if and $cv.ingress $cv.ingress.enabled }}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ $fullName }}
  labels:
    {{- include "genus-os.labels" (dict "root" $root "component" $component) | nindent 4 }}
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
    {{- include "genus-os.labels" (dict "root" $root "component" $component) | nindent 4 }}
spec:
  minAvailable: {{ $cv.podDisruptionBudget.minAvailable }}
  unhealthyPodEvictionPolicy: AlwaysAllow
  selector:
    matchLabels:
      {{- include "genus-os.selectorLabels" (dict "root" $root "component" $component) | nindent 6 }}
{{- end }}
{{- end }}
{{- end -}}
