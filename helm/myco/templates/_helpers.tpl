{{- define "myco.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "myco.fullname" -}}
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

{{- define "myco.selectorLabels" -}}
app.kubernetes.io/name: {{ include "myco.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "myco.labels" -}}
{{ include "myco.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "myco.secretName" -}}
{{- default (printf "%s-env" (include "myco.fullname" .)) .Values.existingSecret -}}
{{- end -}}

{{/* ConfigMap keys cannot contain "/": agents/default.yaml -> agents__default.yaml */}}
{{- define "myco.configKey" -}}
{{- . | replace "/" "__" -}}
{{- end -}}

{{- define "myco.configDir" -}}
{{- if eq .Values.config.source "gitSync" -}}
{{- printf "/git/current/%s" (.Values.gitSync.path | trimAll "/") | trimSuffix "/" -}}
{{- else -}}
/config
{{- end -}}
{{- end -}}

{{- define "myco.gitSyncEnv" -}}
- name: GITSYNC_REPO
  value: {{ required "gitSync.repo is required when config.source=gitSync" .Values.gitSync.repo | quote }}
- name: GITSYNC_REF
  value: {{ .Values.gitSync.ref | quote }}
- name: GITSYNC_ROOT
  value: /git
- name: GITSYNC_LINK
  value: current
- name: GITSYNC_PERIOD
  value: {{ .Values.gitSync.period | quote }}
- name: HOME
  value: /tmp
{{- end -}}
