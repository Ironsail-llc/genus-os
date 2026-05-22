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
      serviceAccountName: {{ include "genus-os.serviceAccountName" $root }}
      # wait-for-migrations initContainer needs the SA token to call kubectl.
      automountServiceAccountToken: {{ $root.Values.migrations.enabled }}
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
            {{- range $k, $v := $cv.env }}
            - name: {{ $k }}
              value: {{ $v | quote }}
            {{- end }}
          envFrom:
            {{- include "genus-os.envFromSecret" $root | nindent 12 }}
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
          resources:
            {{- toYaml $cv.resources | nindent 12 }}
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
  selector:
    matchLabels:
      {{- include "genus-os.selectorLabels" (dict "root" $root "component" $component) | nindent 6 }}
{{- end }}
{{- end }}
{{- end -}}
