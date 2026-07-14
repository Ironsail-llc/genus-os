{{/*
Common helpers for the genus-os chart.

Component templates pass their key as $.component (string), e.g. "engine".
Most helpers resolve everything from that key + $.Values.<key>.* + $.Values.global.*.
*/}}

{{/* Chart name, sanitized to <=63 chars. */}}
{{- define "genus-os.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Full release name. */}}
{{- define "genus-os.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Per-component resource name: <fullname>-<component>. */}}
{{- define "genus-os.componentName" -}}
{{- printf "%s-%s" (include "genus-os.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Chart label. */}}
{{- define "genus-os.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels. Pass .root (the top dot) + .component (string). */}}
{{- define "genus-os.labels" -}}
helm.sh/chart: {{ include "genus-os.chart" .root }}
app.kubernetes.io/name: {{ include "genus-os.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: genus-os
{{- end -}}

{{/* Selector labels (subset of common labels — must be immutable). */}}
{{- define "genus-os.selectorLabels" -}}
app.kubernetes.io/name: {{ include "genus-os.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Per-component ServiceAccount name. Pass .root + .component. */}}
{{- define "genus-os.serviceAccountName" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- if $root.Values.serviceAccount.create -}}
{{- printf "%s-%s" (include "genus-os.fullname" $root) $component | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $configuredName := index ($root.Values.serviceAccount.names | default dict) $component -}}
{{- if and (has $root.Values.global.environment (list "production" "prod" "staging")) (not $configuredName) -}}
{{- fail (printf "serviceAccount.names.%s is required when chart-managed accounts are disabled in staging/production" $component) -}}
{{- end -}}
{{- default "default" $configuredName -}}
{{- end -}}
{{- end -}}

{{/*
Container image reference. Pass .root + .component + .imageKey (defaults
to .component). Image repo is .Values.global.registry + "/" + repository.
*/}}
{{- define "genus-os.image" -}}
{{- $component := .component -}}
{{- $cv := index .root.Values $component -}}
{{- $repo := default $component $cv.image.repository -}}
{{- $registry := .root.Values.global.registry -}}
{{- $tag := default .root.Values.global.imageTag $cv.image.tag -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- end -}}

{{/* Image pull policy with fallback to global. */}}
{{- define "genus-os.imagePullPolicy" -}}
{{- $component := .component -}}
{{- $cv := index .root.Values $component -}}
{{- default .root.Values.global.imagePullPolicy $cv.image.pullPolicy -}}
{{- end -}}

{{/*
Migration Job name. Includes a hash of imageTag so each release gets a
fresh Job and the wait-for-migrations initContainer can reliably compute
the same name. See aws-infrastucture/docs/helm-db-migrations.md.
*/}}
{{- define "genus-os.migrationJobName" -}}
{{- printf "%s-migrate-%s" (include "genus-os.fullname" .) (.Values.global.imageTag | sha256sum | trunc 8) -}}
{{- end -}}

{{/* Workspace PVC name (created by this chart or supplied by the operator). */}}
{{- define "genus-os.workspaceClaimName" -}}
{{- default (printf "%s-workspace" (include "genus-os.fullname" .)) .Values.workspace.persistence.existingClaim -}}
{{- end -}}

{{/*
Postgres host resolution.

When postgres.enabled and database.host is empty, point at the
groundhog2k/postgres subchart's default Service name: `{release}-postgres`.
Otherwise honor the explicit database.host (RDS, CloudNativePG, etc).
*/}}
{{- define "genus-os.postgresHost" -}}
{{- if and .Values.postgres.enabled (eq .Values.database.host "") -}}
{{- printf "%s-postgres" .Release.Name -}}
{{- else -}}
{{- .Values.database.host -}}
{{- end -}}
{{- end -}}

{{/*
Redis host resolution. Matches the groundhog2k/redis subchart Service:
`{release}-redis` when redis.enabled and redis.host is empty.
*/}}
{{- define "genus-os.redisHost" -}}
{{- if and .Values.redis.enabled (eq .Values.redis.host "") -}}
{{- printf "%s-redis" .Release.Name -}}
{{- else -}}
{{- .Values.redis.host -}}
{{- end -}}
{{- end -}}

{{/*
Shared environment variables for every Python service.
Renders DB/Redis connection info plus deployment breadcrumbs.
*/}}
{{- define "genus-os.commonEnv" -}}
{{- /*
DB host/port/name/user explicitly rendered ONLY when the in-cluster
postgres subchart is enabled. In that mode the chart can compute the
service name and `database.*` values are authoritative.

When postgres.enabled is false (staging / production with external RDS),
all `ROBOTHOR_DB_*` connection variables come from the component-scoped
`database` Secret via envFrom — including HOST, PORT, NAME, USER, PASSWORD.
The environment-specific `vault.secrets.database.path` is authoritative.

Explicit `env:` entries WIN over `envFrom:` in k8s, so rendering them
here when the values are wrong would mask the Vault values silently.
*/ -}}
{{- if .Values.postgres.enabled }}
- name: ROBOTHOR_DB_HOST
  value: {{ include "genus-os.postgresHost" . | quote }}
- name: ROBOTHOR_DB_PORT
  value: {{ .Values.database.port | quote }}
- name: ROBOTHOR_DB_NAME
  value: {{ .Values.database.name | quote }}
- name: ROBOTHOR_DB_USER
  value: {{ .Values.database.user | quote }}
- name: ROBOTHOR_DB_SSLMODE
  value: {{ .Values.database.sslMode | quote }}
{{- end }}
{{- if not .Values.postgres.enabled }}
- name: ROBOTHOR_DB_SSLMODE
  value: {{ .Values.database.sslMode | quote }}
{{- end }}
{{- if .Values.redis.enabled }}
- name: ROBOTHOR_REDIS_HOST
  value: {{ include "genus-os.redisHost" . | quote }}
- name: ROBOTHOR_REDIS_PORT
  value: {{ .Values.redis.port | quote }}
{{- end }}
- name: GENUS_ENVIRONMENT
  value: {{ .Values.global.environment | quote }}
- name: GENUS_AUTH_ENFORCE
  value: {{ .Values.global.authEnforce | quote }}
- name: GENUS_OS_DEPLOYED_FROM_PR
  value: {{ .Values.global.deployedFromPR | quote }}
- name: GENUS_OS_DEPLOYED_AT
  value: {{ .Values.global.deployedAt | quote }}
- name: GENUS_OS_IMAGE_TAG
  value: {{ .Values.global.imageTag | quote }}
{{- end -}}

{{/*
Render only the named Secret classes declared by one component's secretRefs.
Pass .root + .component. Unknown references are a hard template error. The
Secret may be materialized by VSO or supplied out-of-band when Vault is off;
missing Secrets fail closed with CreateContainerConfigError.
*/}}
{{- define "genus-os.envFromSecret" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $cv := index $root.Values $component -}}
{{- $allowedByComponent := dict
      "engine" (list "database" "cache" "auth-signing" "engine-providers")
      "bridge" (list "database" "cache" "auth-signing" "bridge-sso" "bridge-oidc" "bridge-integrations")
      "orchestrator" (list "database" "cache" "auth-signing" "orchestrator-providers")
      "dashboard" (list "dashboard-auth" "bridge-sso")
      "migrations" (list "database") -}}
{{- $allowedRefs := index $allowedByComponent $component -}}
{{- range $secretName := ($cv.secretRefs | default list) -}}
{{- if not (hasKey $root.Values.vault.secrets $secretName) -}}
{{- fail (printf "%s.secretRefs contains unknown vault.secrets entry %q" $component $secretName) -}}
{{- end }}
{{- if and (eq $component "dashboard") (not (has $secretName (list "dashboard-auth" "bridge-sso"))) -}}
{{- fail (printf "dashboard.secretRefs forbids privileged secret class %q; only dashboard-auth and bridge-sso are allowed" $secretName) -}}
{{- end }}
{{- if and (eq $component "migrations") (ne $secretName "database") -}}
{{- fail (printf "migrations.secretRefs is DB-only and forbids secret class %q" $secretName) -}}
{{- end }}
{{- if not (has $secretName $allowedRefs) -}}
{{- fail (printf "%s.secretRefs forbids secret class %q" $component $secretName) -}}
{{- end }}
- secretRef:
    name: {{ include "genus-os.fullname" $root }}-{{ $secretName }}
{{- end }}
{{- end -}}

{{/* Merge globals + per-component nodeSelector. */}}
{{- define "genus-os.nodeSelector" -}}
{{- $component := .component -}}
{{- $cv := index .root.Values $component -}}
{{- $merged := merge (deepCopy ($cv.nodeSelector | default dict)) (.root.Values.global.nodeSelector | default dict) -}}
{{- toYaml $merged -}}
{{- end -}}

{{/* Merge globals + per-component tolerations. */}}
{{- define "genus-os.tolerations" -}}
{{- $component := .component -}}
{{- $cv := index .root.Values $component -}}
{{- $cvTol := $cv.tolerations | default list -}}
{{- $gTol := .root.Values.global.tolerations | default list -}}
{{- $merged := concat $cvTol $gTol -}}
{{- toYaml $merged -}}
{{- end -}}

{{/* Affinity with fallback to globals. */}}
{{- define "genus-os.affinity" -}}
{{- $component := .component -}}
{{- $cv := index .root.Values $component -}}
{{- $aff := $cv.affinity | default dict -}}
{{- if not $aff -}}{{- $aff = .root.Values.global.affinity | default dict -}}{{- end -}}
{{- if $aff -}}
{{- toYaml $aff -}}
{{- else if gt (int ($cv.replicaCount | default 1)) 1 -}}
podAntiAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        topologyKey: kubernetes.io/hostname
        labelSelector:
          matchLabels:
            {{- include "genus-os.selectorLabels" (dict "root" .root "component" $component) | nindent 12 }}
{{- else -}}
{}
{{- end -}}
{{- end -}}

{{/* Probe rendering. Pass .root + .component + .probe (string: liveness|readiness) + .port. */}}
{{- define "genus-os.httpProbe" -}}
{{- $component := .component -}}
{{- $probeName := .probe -}}
{{- $cv := index .root.Values $component -}}
{{- $cfg := index $cv.probes $probeName -}}
{{- if $cfg.enabled }}
httpGet:
  path: {{ $cfg.path | quote }}
  port: {{ .port }}
initialDelaySeconds: {{ $cfg.initialDelaySeconds | default 0 }}
periodSeconds: {{ $cfg.periodSeconds | default 10 }}
timeoutSeconds: {{ $cfg.timeoutSeconds | default 1 }}
failureThreshold: {{ $cfg.failureThreshold | default 3 }}
{{- end -}}
{{- end -}}
