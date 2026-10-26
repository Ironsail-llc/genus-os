{{/*
The Job's pod template, as a named template so genus-os.migrationJobName can
hash exactly what the Job renders. A Job's pod template is immutable: any
change to it -- image, environment, labels (chart and app version included),
ServiceAccount, security context, scheduling, pull secrets -- must produce a
new Job name, or the upgrade fails with "field is immutable". It must never
reference the Job name itself, or the name would depend on itself.

It lives in a partial, not in job.yaml, so the name helper can render it
from any template (the app workloads' wait-for-migrations and the RBAC Role
include the name too).
*/}}
{{- define "genus-os.migrationsPodTemplate" }}
    metadata:
      labels:
        {{- include "genus-os.labels" (dict "root" . "component" "migrations") | nindent 8 }}
    spec:
      serviceAccountName: {{ include "genus-os.serviceAccountName" (dict "root" . "component" "migrations") }}
      automountServiceAccountToken: false
      restartPolicy: Never
      {{- with .Values.global.imagePullSecrets }}
      imagePullSecrets:
        {{- range . }}
        - name: {{ . }}
        {{- end }}
      {{- end }}
      securityContext:
        {{- toYaml .Values.global.podSecurityContext | nindent 8 }}
      {{- $ns := include "genus-os.nodeSelector" (dict "root" . "component" "migrations") }}
      {{- if $ns | trim }}
      nodeSelector:
        {{- $ns | nindent 8 }}
      {{- end }}
      {{- $tols := include "genus-os.tolerations" (dict "root" . "component" "migrations") }}
      {{- if $tols | trim }}
      tolerations:
        {{- $tols | nindent 8 }}
      {{- end }}
      {{- if .Values.postgres.enabled }}
      initContainers:
        - name: wait-for-postgres
          image: "{{ .Values.postgres.image.registry }}/{{ .Values.postgres.image.repository }}:{{ .Values.postgres.image.tag }}"
          command:
            - sh
            - -c
            - |
              until pg_isready -h {{ include "genus-os.postgresHost" . }} -p {{ .Values.database.port }} -U {{ .Values.database.user }}; do
                echo "Waiting for postgres..."
                sleep 2
              done
          securityContext:
            {{- toYaml .Values.global.securityContext | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
          resources:
            {{- toYaml .Values.global.initContainerResources | nindent 12 }}
      {{- end }}
      containers:
        - name: migrate
          image: {{ include "genus-os.image" (dict "root" . "component" "migrations") }}
          imagePullPolicy: {{ include "genus-os.imagePullPolicy" (dict "root" . "component" "migrations") }}
          securityContext:
            {{- toYaml .Values.global.securityContext | nindent 12 }}
          command:
            {{- toYaml .Values.migrations.command | nindent 12 }}
          env:
            {{- include "genus-os.commonEnv" . | nindent 12 }}
            {{- with include "genus-os.dbCaEnv" . | trim }}{{ . | nindent 12 }}{{- end }}
          envFrom:
            {{- include "genus-os.envFromSecret" (dict "root" . "component" "migrations") | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            {{- with include "genus-os.dbCaVolumeMount" . | trim }}{{ . | nindent 12 }}{{- end }}
          resources:
            {{- toYaml .Values.migrations.resources | nindent 12 }}
      volumes:
        - name: tmp
          emptyDir: {}
        {{- with include "genus-os.dbCaVolume" . | trim }}{{ . | nindent 8 }}{{- end }}
{{- end }}
