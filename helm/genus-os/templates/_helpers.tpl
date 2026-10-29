{{/*
Common helpers for the genus-os chart.

Component templates pass their key as $.component (string), e.g. "engine".
Most helpers resolve everything from that key + $.Values.<key>.* + $.Values.global.*.
A caller that has already built the component's values map (a tenant engine
cell, whose map is engine.* overlaid with its tenants[] entry) passes it as
$.values, which then wins over $.Values.<key>.
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

{{/*
Common labels. Pass .root (the top dot) + .component (string), and .tenant
for an object that belongs to one tenant engine cell (adds genus-os/tenant).
*/}}
{{- define "genus-os.labels" -}}
helm.sh/chart: {{ include "genus-os.chart" .root }}
app.kubernetes.io/name: {{ include "genus-os.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: genus-os
{{- with .tenant }}
genus-os/tenant: {{ . }}
{{- end }}
{{- end -}}

{{/*
Selector labels (subset of common labels — must be immutable). .tenant is
optional, as for genus-os.labels. A Deployment's selector cannot change after
creation, so no existing selector may ever gain it.
*/}}
{{- define "genus-os.selectorLabels" -}}
app.kubernetes.io/name: {{ include "genus-os.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- with .tenant }}
genus-os/tenant: {{ . }}
{{- end }}
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
{{- $cv := .values | default (index .root.Values $component) -}}
{{- $repo := default $component $cv.image.repository -}}
{{- $registry := .root.Values.global.registry -}}
{{- $tag := default .root.Values.global.imageTag $cv.image.tag -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- end -}}

{{/* Image pull policy with fallback to global. */}}
{{- define "genus-os.imagePullPolicy" -}}
{{- $component := .component -}}
{{- $cv := .values | default (index .root.Values $component) -}}
{{- default .root.Values.global.imagePullPolicy $cv.image.pullPolicy -}}
{{- end -}}

{{/*
Migration Job name: <fullname>-migrate-<hash of the rendered pod template>.
Job, RBAC and every wait-for-migrations initContainer compute it from the same
values, so they always agree.

A Job's pod template is immutable. The name used to hash only the image tag,
so a change to anything else in the template -- the shared environment, the
database CA mount, the chart/app version labels, the ServiceAccount, security
context, scheduling or pull secrets -- kept the name, and the upgrade failed
with "field is immutable", leaving the release half-applied. Hashing the whole
rendered template (genus-os.migrationsPodTemplate, in migrations/job.yaml)
means any such change yields a new Job that runs once, while an unchanged
release keeps its completed Job and is not re-run. The previous Job is pruned
on the next sync.
*/}}
{{- define "genus-os.migrationJobName" -}}
{{- printf "%s-migrate-%s" (include "genus-os.fullname" .) (include "genus-os.migrationsPodTemplate" . | sha256sum | trunc 8) -}}
{{- end -}}

{{/* The tenant the root engine operates as (global.rootTenant). */}}
{{- define "genus-os.rootTenant" -}}
{{- toString (.Values.global.rootTenant | default "default") -}}
{{- end -}}

{{/* The root engine's in-cluster URL. */}}
{{- define "genus-os.engineUrl" -}}
{{- printf "http://%s-engine:%v" (include "genus-os.fullname" .) .Values.engine.service.port -}}
{{- end -}}

{{/*
URL of any tenant engine cell, with `{tenant}` left as literal text for the
application to substitute with a tenants[].id. It matches the Service name
engine/tenants.yaml renders, which is why that template refuses an id whose
name would be truncated.
*/}}
{{- define "genus-os.tenantEngineUrlTemplate" -}}
{{- printf "http://%s-engine-{tenant}:%v" (include "genus-os.fullname" .) .Values.engine.service.port -}}
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
Is the in-chart cache deployed? Mirrors the Chart.yaml dependency condition
`valkey.enabled,redis.enabled` exactly: `valkey.enabled` when it is set,
otherwise `redis.enabled`. Helm evaluates that condition itself for the
subchart; the templates here have to agree with it or the chart would render
client wiring for a server it did not deploy (or the reverse).
*/}}
{{- define "genus-os.cacheEnabled" -}}
{{- $valkey := .Values.valkey | default dict -}}
{{- if hasKey $valkey "enabled" -}}
{{- $valkey.enabled -}}
{{- else -}}
{{- (.Values.redis | default dict).enabled | default false -}}
{{- end -}}
{{- end -}}

{{/*
Cache host resolution. An explicit `redis.host` always wins; otherwise this
matches the valkey-io subchart's Service, `{release}-valkey`. Only consulted
when valkey.enabled — see the gate in genus-os.commonEnv.
*/}}
{{- define "genus-os.redisHost" -}}
{{- if ne (.Values.redis.host | default "") "" -}}
{{- .Values.redis.host -}}
{{- else -}}
{{- printf "%s-valkey" .Release.Name -}}
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
{{- /*
Same rule as the DB above: render the cache host/port explicitly ONLY when the
in-chart valkey subchart is deployed, because only then can the chart compute
the Service name. When valkey.enabled is false the cache is external and
ROBOTHOR_REDIS_HOST/PORT come from the `cache` Secret via envFrom, where the
environment's vault.secrets.cache.path is authoritative.
*/ -}}
{{- if eq (include "genus-os.cacheEnabled" .) "true" }}
- name: ROBOTHOR_REDIS_HOST
  value: {{ include "genus-os.redisHost" . | quote }}
- name: ROBOTHOR_REDIS_PORT
  value: {{ .Values.redis.port | quote }}
{{- end }}
- name: GENUS_ENVIRONMENT
  value: {{ .Values.global.environment | quote }}
- name: GENUS_AUTH_ENFORCE
  value: {{ .Values.global.authEnforce | quote }}
- name: GENUS_LOCAL_LOGIN
  value: {{ .Values.global.localLogin | default false | quote }}
- name: GENUS_TRUSTED_PROXIES
  value: {{ .Values.global.trustedProxies | default "" | quote }}
{{- /* Defaults TRUE, so `default` is the wrong helper: it would rewrite an
       operator's explicit false back to true. */}}
{{- $ownerMfa := true }}
{{- if hasKey .Values.global "ownerMfaRequired" }}{{- $ownerMfa = .Values.global.ownerMfaRequired }}{{- end }}
- name: GENUS_OWNER_MFA_REQUIRED
  value: {{ $ownerMfa | quote }}
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
{{- $cv := .values | default (index $root.Values $component) -}}
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
{{- $cv := .values | default (index .root.Values $component) -}}
{{- $merged := merge (deepCopy ($cv.nodeSelector | default dict)) (.root.Values.global.nodeSelector | default dict) -}}
{{- toYaml $merged -}}
{{- end -}}

{{/* Merge globals + per-component tolerations. */}}
{{- define "genus-os.tolerations" -}}
{{- $component := .component -}}
{{- $cv := .values | default (index .root.Values $component) -}}
{{- $cvTol := $cv.tolerations | default list -}}
{{- $gTol := .root.Values.global.tolerations | default list -}}
{{- $merged := concat $cvTol $gTol -}}
{{- toYaml $merged -}}
{{- end -}}

{{/* Affinity with fallback to globals. */}}
{{- define "genus-os.affinity" -}}
{{- $component := .component -}}
{{- $cv := .values | default (index .root.Values $component) -}}
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
            {{- include "genus-os.selectorLabels" (dict "root" .root "component" $component "tenant" .tenant) | nindent 12 }}
{{- else -}}
{}
{{- end -}}
{{- end -}}

{{/* Probe rendering. Pass .root + .component + .probe (string: liveness|readiness) + .port. */}}
{{- define "genus-os.httpProbe" -}}
{{- $component := .component -}}
{{- $probeName := .probe -}}
{{- $cv := .values | default (index .root.Values $component) -}}
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

{{/*
Database CA bundle (database.tls). Every component that opens a database
connection includes all three: the env var, the read-only mount and the
volume. libpq reads PGSSLROOTCERT for verify-ca/verify-full, so no code
change is needed in the services. Each renders nothing when
database.tls.caConfigMap is empty.
*/}}
{{- define "genus-os.dbCaDir" -}}/etc/genus/db-ca{{- end -}}

{{- define "genus-os.dbCaEnv" -}}
{{- $tls := .Values.database.tls | default dict -}}
{{- if $tls.caConfigMap }}
- name: PGSSLROOTCERT
  value: {{ printf "%s/%s" (include "genus-os.dbCaDir" .) ($tls.caKey | default "ca.pem") | quote }}
{{- end }}
{{- end -}}

{{- define "genus-os.dbCaVolumeMount" -}}
{{- $tls := .Values.database.tls | default dict -}}
{{- if $tls.caConfigMap }}
- name: db-ca
  mountPath: {{ include "genus-os.dbCaDir" . }}
  readOnly: true
{{- end }}
{{- end -}}

{{/*
Google Workspace credentials (engine.googleWorkspace). The engine's gws_*
tools shell out to the gws CLI, which reads an authorized-user credentials
file. The `engine-google` Secret class is mounted as that file and never
reaches envFrom, so the refresh token does not become an environment variable.
The CLI rewrites a token cache next to its config; the root filesystem is
read-only, so the config dir sits on the writable runtime-cache emptyDir.
The mount is optional: a Secret that has not synced yet leaves the gws tools
reporting an auth error instead of holding the engine in ContainerCreating.
Engine only. Pass .root, .gw (the engine's or a tenant cell's googleWorkspace
map), .secretName and .checkVault; each renders nothing when .gw.enabled is
false. Only the root engine's Secret is materialized by VSO (checkVault); a
tenant cell's Secret is supplied by the deployment.
*/}}
{{- define "genus-os.googleWorkspaceDir" -}}/etc/genus/gws{{- end -}}

{{- define "genus-os.googleWorkspaceEnv" -}}
{{- if (.gw | default dict).enabled }}
{{- if and .checkVault .root.Values.vault.enabled (not (hasKey .root.Values.vault.secrets "engine-google")) -}}
{{- fail "engine.googleWorkspace.enabled requires vault.secrets.engine-google (a Vault path holding credentials.json)" -}}
{{- end }}
- name: GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE
  value: {{ printf "%s/credentials.json" (include "genus-os.googleWorkspaceDir" .) | quote }}
- name: GOOGLE_WORKSPACE_CLI_CONFIG_DIR
  value: "/app/.cache/gws"
{{- end }}
{{- end -}}

{{- define "genus-os.googleWorkspaceVolumeMount" -}}
{{- if (.gw | default dict).enabled }}
- name: google-workspace
  mountPath: {{ include "genus-os.googleWorkspaceDir" . }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "genus-os.googleWorkspaceVolume" -}}
{{- if (.gw | default dict).enabled }}
- name: google-workspace
  secret:
    secretName: {{ .secretName }}
    defaultMode: 0400
    optional: true
    items:
      - key: credentials.json
        path: credentials.json
{{- end }}
{{- end -}}

{{/*
Shared vault key (vaultKey.enabled). One master key and one fingerprint salt
for every process that reads the vault: the bridge, the root engine and every
tenant cell. The `vault-key` class is materialized by VSO, which decodes the
base64 fields stored in Vault into the raw files the code reads. NOT optional:
under GENUS_VAULT_KEY_FILE a missing key is a hard error, and a pod that can
start without it would only fail later, on the first secret it touches.
Pass the top-level dot; each renders nothing when vaultKey.enabled is false.
*/}}
{{- define "genus-os.vaultKeyDir" -}}/etc/genus/vault{{- end -}}

{{- define "genus-os.vaultKeyEnv" -}}
{{- if (.Values.vaultKey | default dict).enabled }}
{{- if and .Values.vault.enabled (not (hasKey .Values.vault.secrets "vault-key")) -}}
{{- fail "vaultKey.enabled requires vault.secrets.vault-key (a Vault path holding base64 `key` and `fingerprint-salt`)" -}}
{{- end }}
- name: GENUS_VAULT_KEY_FILE
  value: {{ printf "%s/key" (include "genus-os.vaultKeyDir" .) | quote }}
- name: GENUS_FINGERPRINT_SALT_FILE
  value: {{ printf "%s/fingerprint-salt" (include "genus-os.vaultKeyDir" .) | quote }}
{{- end }}
{{- end -}}

{{- define "genus-os.vaultKeyVolumeMount" -}}
{{- if (.Values.vaultKey | default dict).enabled }}
- name: vault-key
  mountPath: {{ include "genus-os.vaultKeyDir" . }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "genus-os.vaultKeyVolume" -}}
{{- if (.Values.vaultKey | default dict).enabled }}
- name: vault-key
  secret:
    secretName: {{ include "genus-os.fullname" . }}-vault-key
    defaultMode: 0400
    items:
      - key: key
        path: key
      - key: fingerprint-salt
        path: fingerprint-salt
{{- end }}
{{- end -}}

{{- define "genus-os.dbCaVolume" -}}
{{- $tls := .Values.database.tls | default dict -}}
{{- if $tls.caConfigMap }}
{{- $key := $tls.caKey | default "ca.pem" }}
- name: db-ca
  configMap:
    name: {{ $tls.caConfigMap }}
    items:
      - key: {{ $key }}
        path: {{ $key }}
{{- end }}
{{- end -}}
