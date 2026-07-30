{{/*
生成 chart 名称（优先 nameOverride，截断 63 字符并以连字符清理尾部）
*/}}
{{- define "intelligent-ops-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
生成完整名称（release + chart 名，截断 63 字符）
*/}}
{{- define "intelligent-ops-platform.fullname" -}}
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

{{/*
标准标签（chart 名/版本、实例、托管方等）
*/}}
{{- define "intelligent-ops-platform.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{ include "intelligent-ops-platform.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
选择器标签（Deployment/Service 的 matchLabels / selector 必须一致）
*/}}
{{- define "intelligent-ops-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ include "intelligent-ops-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
ServiceAccount 名称（create=true 时默认使用 fullname）
*/}}
{{- define "intelligent-ops-platform.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "intelligent-ops-platform.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
