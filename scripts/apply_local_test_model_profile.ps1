param(
    [string]$HostRoot = "Z:\\ai_robot\\aibot\\AstrBot-4.12.1",
    [string]$ProfilePath = "$PSScriptRoot\\..\\tests\\manual\\live_host_model_profile.json",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

function Set-JsonProperty {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Value
    )
    if ($Object.PSObject.Properties.Name -contains $Name) {
        $Object.$Name = $Value
    } else {
        $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value
    }
}

function Find-ById {
    param([object[]]$Items, [string]$Id)
    return @($Items | Where-Object { [string]$_.id -eq $Id } | Select-Object -First 1)
}

function Ensure-ProviderSource {
    param([object[]]$Sources, [hashtable]$Spec)
    $source = Find-ById -Items $Sources -Id $Spec.id
    if (-not $source) {
        $source = [pscustomobject]@{}
        $Sources += $source
    }
    foreach ($key in $Spec.Keys) {
        Set-JsonProperty -Object $source -Name $key -Value $Spec[$key]
    }
    return $source
}

function Ensure-Model {
    param(
        [object[]]$Models,
        [string]$Id,
        [string]$SourceId,
        [string]$Model,
        [string[]]$Modalities,
        [int]$MaxContextTokens
    )
    $entry = Find-ById -Items $Models -Id $Id
    if (-not $entry) {
        $entry = [pscustomobject]@{}
        $Models += $entry
    }
    Set-JsonProperty -Object $entry -Name "id" -Value $Id
    Set-JsonProperty -Object $entry -Name "enable" -Value $true
    Set-JsonProperty -Object $entry -Name "provider_source_id" -Value $SourceId
    Set-JsonProperty -Object $entry -Name "model" -Value $Model
    Set-JsonProperty -Object $entry -Name "modalities" -Value @($Modalities)
    Set-JsonProperty -Object $entry -Name "custom_extra_body" -Value ([pscustomobject]@{})
    Set-JsonProperty -Object $entry -Name "max_context_tokens" -Value $MaxContextTokens
    return $entry
}

$hostConfigPath = Join-Path $HostRoot "data\\cmd_config.json"
$pluginConfigPath = Join-Path $HostRoot "data\\config\\astrmai_config.json"
if (-not (Test-Path -LiteralPath $hostConfigPath)) { throw "Host config not found: $hostConfigPath" }
if (-not (Test-Path -LiteralPath $pluginConfigPath)) { throw "AstrMai config not found: $pluginConfigPath" }
if (-not (Test-Path -LiteralPath $ProfilePath)) { throw "Profile not found: $ProfilePath" }

$profile = Get-Content -Raw -Encoding utf8 -LiteralPath $ProfilePath | ConvertFrom-Json
$summary = [ordered]@{
    host_root = $HostRoot
    profile = (Resolve-Path -LiteralPath $ProfilePath).Path
    apply = [bool]$Apply
    models = $profile.models
    runtime_limits = $profile.runtime_limits
}

if (-not $Apply) {
    $summary.status = "dry_run"
    $summary | ConvertTo-Json -Depth 20
    exit 0
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$hostBackup = "$hostConfigPath.pre_local_test_$stamp.json"
$pluginBackup = "$pluginConfigPath.pre_local_test_$stamp.json"
Copy-Item -LiteralPath $hostConfigPath -Destination $hostBackup
Copy-Item -LiteralPath $pluginConfigPath -Destination $pluginBackup

$hostConfig = Get-Content -Raw -Encoding utf8 -LiteralPath $hostConfigPath | ConvertFrom-Json
$sources = @($hostConfig.provider_sources)
$opencodeBefore = Find-ById -Items $sources -Id "opencode"
$opencodeSource = Ensure-ProviderSource -Sources $sources -Spec @{
    id = "opencode"; provider = "openai"; type = "openai_chat_completion"; provider_type = "chat_completion"
    key = @("`$ASTRMAI_OPENCODE_API_KEY"); api_base = $profile.provider_sources.opencode.api_base
    timeout = $profile.provider_sources.opencode.timeout_sec; proxy = ""; custom_headers = [pscustomobject]@{}; enable = $true
}
if (-not $opencodeBefore) { $sources += $opencodeSource }
$qwenBefore = Find-ById -Items $sources -Id "qwen"
$qwenSource = Ensure-ProviderSource -Sources $sources -Spec @{
    id = "qwen"; provider = "qwen"; type = "openai_chat_completion"; provider_type = "chat_completion"
    key = @("`$ASTRMAI_QWEN_API_KEY"); api_base = $profile.provider_sources.qwen.api_base
    timeout = $profile.provider_sources.qwen.timeout_sec; proxy = ""; custom_headers = [pscustomobject]@{}; enable = $true
}
if (-not $qwenBefore) { $sources += $qwenSource }
Set-JsonProperty -Object $hostConfig -Name "provider_sources" -Value $sources

$models = @($hostConfig.provider)
$enabledModelIds = @(
    "opencode/deepseek-v4-flash",
    "opencode/gpt-5.6-luna",
    "opencode/mimo-v2.5-pro",
    "qwen/qwen3-vl-flash"
)
foreach ($model in $models) {
    if ($model.PSObject.Properties.Name -contains "provider_source_id" -and $enabledModelIds -notcontains [string]$model.id) {
        Set-JsonProperty -Object $model -Name "enable" -Value $false
    }
}
$requiredModels = @(
    @{ id = "opencode/deepseek-v4-flash"; source = "opencode"; model = "deepseek-v4-flash"; modalities = @("text", "tool_use"); context = 1000000 },
    @{ id = "opencode/gpt-5.6-luna"; source = "opencode"; model = "gpt-5.6-luna"; modalities = @("text", "tool_use"); context = 262144 },
    @{ id = "opencode/mimo-v2.5-pro"; source = "opencode"; model = "mimo-v2.5-pro"; modalities = @("text", "image", "tool_use"); context = 262144 },
    @{ id = "qwen/qwen3-vl-flash"; source = "qwen"; model = "qwen3-vl-flash"; modalities = @("text", "image"); context = 32768 }
)
foreach ($spec in $requiredModels) {
    $entry = Ensure-Model -Models $models -Id $spec.id -SourceId $spec.source -Model $spec.model -Modalities $spec.modalities -MaxContextTokens $spec.context
    if (-not (Find-ById -Items $models -Id $spec.id)) {
        $models += $entry
    }
}

$embedding = Find-ById -Items $models -Id "openai_embedding"
if (-not $embedding) {
    $embedding = [pscustomobject]@{}
    $models += $embedding
}
Set-JsonProperty -Object $embedding -Name "id" -Value "openai_embedding"
Set-JsonProperty -Object $embedding -Name "type" -Value "openai_embedding"
Set-JsonProperty -Object $embedding -Name "provider" -Value "openai"
Set-JsonProperty -Object $embedding -Name "provider_type" -Value "embedding"
Set-JsonProperty -Object $embedding -Name "enable" -Value $true
Set-JsonProperty -Object $embedding -Name "embedding_api_key" -Value "`$ASTRMAI_OPENAI_EMBEDDING_API_KEY"
Set-JsonProperty -Object $embedding -Name "embedding_api_base" -Value $profile.provider_sources.openai_embedding.api_base
Set-JsonProperty -Object $embedding -Name "embedding_model" -Value $profile.provider_sources.openai_embedding.model
Set-JsonProperty -Object $embedding -Name "embedding_dimensions" -Value $profile.provider_sources.openai_embedding.dimensions
Set-JsonProperty -Object $embedding -Name "timeout" -Value $profile.provider_sources.openai_embedding.timeout_sec
Set-JsonProperty -Object $hostConfig -Name "provider" -Value $models

$settings = $hostConfig.provider_settings
Set-JsonProperty -Object $settings -Name "default_provider_id" -Value "opencode/deepseek-v4-flash"
Set-JsonProperty -Object $settings -Name "request_max_retries" -Value 2

$hostConfig | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $hostConfigPath -Encoding utf8

$plugin = Get-Content -Raw -Encoding utf8 -LiteralPath $pluginConfigPath | ConvertFrom-Json
$pluginProvider = $plugin.provider
Set-JsonProperty -Object $pluginProvider -Name "task_models" -Value @($profile.models.task_models)
Set-JsonProperty -Object $pluginProvider -Name "agent_models" -Value @($profile.models.agent_models)
Set-JsonProperty -Object $pluginProvider -Name "fallback_models" -Value @($profile.models.fallback_models)
Set-JsonProperty -Object $pluginProvider -Name "vision_models" -Value @($profile.models.vision_models)
Set-JsonProperty -Object $pluginProvider -Name "embedding_models" -Value @($profile.models.embedding_models)

$infra = $plugin.infra
if ($infra.PSObject.Properties.Name -contains "api_timeout") {
    $infra.PSObject.Properties.Remove("api_timeout")
}
foreach ($name in @("max_concurrent_llm_calls", "llm_retries", "background_task_concurrency", "background_task_queue_limit")) {
    Set-JsonProperty -Object $infra -Name $name -Value $profile.runtime_limits.$name
}
$timing = $plugin.timing
Set-JsonProperty -Object $timing -Name "model_request_timeout_sec" -Value $profile.runtime_limits.model_request_timeout_sec
Set-JsonProperty -Object $timing -Name "turn_total_budget_sec" -Value $profile.runtime_limits.turn_total_budget_sec
$life = $plugin.life
Set-JsonProperty -Object $life -Name "enable_proactive" -Value ([bool]$profile.runtime_limits.proactive_enabled)
Set-JsonProperty -Object $life -Name "enable_private_proactive" -Value ([bool]$profile.runtime_limits.proactive_enabled)
Set-JsonProperty -Object $life -Name "enable_group_proactive" -Value ([bool]$profile.runtime_limits.proactive_enabled)
Set-JsonProperty -Object $life -Name "daily_schedule_ai_enabled" -Value ([bool]$profile.runtime_limits.daily_schedule_ai_enabled)
$memory = $plugin.memory
Set-JsonProperty -Object $memory -Name "maintenance_schedule_enabled" -Value ([bool]$profile.runtime_limits.memory_maintenance_schedule_enabled)
$evolution = $plugin.evolution
Set-JsonProperty -Object $evolution -Name "enable_backlog_mining" -Value ([bool]$profile.runtime_limits.backlog_mining_enabled)
$rollout = $plugin.architecture_rollout
Set-JsonProperty -Object $rollout -Name "proactive_due_enabled" -Value ([bool]$profile.runtime_limits.proactive_enabled)
$hostConfig.provider_settings.tool_call_timeout = $profile.runtime_limits.tool_call_timeout_sec
$hostConfig.provider_settings.max_agent_step = $profile.runtime_limits.max_agent_step

$plugin | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $pluginConfigPath -Encoding utf8

$summary.status = "applied"
$summary.host_backup = $hostBackup
$summary.plugin_backup = $pluginBackup
$summary | ConvertTo-Json -Depth 20
