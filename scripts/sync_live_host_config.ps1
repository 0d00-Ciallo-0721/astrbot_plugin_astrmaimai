param(
    [string]$HostRoot = "Z:\ai_robot\aibot\AstrBot-4.12.1"
)

$ErrorActionPreference = "Stop"
$configPath = Join-Path $HostRoot "data\cmd_config.json"
$pluginConfigPath = Join-Path $HostRoot "data\config\astrmai_config.json"
$backupPath = Join-Path $HostRoot "data\cmd_config.pre_live_secrets_20260824.json"

if (-not (Test-Path -LiteralPath $backupPath)) {
    Copy-Item -LiteralPath $configPath -Destination $backupPath
}

$config = Get-Content -Raw -Encoding utf8 -LiteralPath $configPath | ConvertFrom-Json
$sources = @($config.provider_sources)
$opencode = $sources | Where-Object { $_.id -eq "opencode" } | Select-Object -First 1
if ($null -eq $opencode) {
    throw "Provider source 'opencode' was not found."
}
$opencode.key = @('$ASTRMAI_OPENCODE_API_KEY')

$qwen = $sources | Where-Object { $_.id -eq "qwen" } | Select-Object -First 1
if ($null -eq $qwen) {
    $qwen = [pscustomobject]@{
        provider = "qwen"
        type = "openai_chat_completion"
        provider_type = "chat_completion"
        key = @('$ASTRMAI_QWEN_API_KEY')
        api_base = "https://llm-atxu4eabnm73ai8g.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        timeout = 120
        proxy = ""
        custom_headers = [pscustomobject]@{}
        id = "qwen"
        enable = $true
    }
    $sources += $qwen
} else {
    $qwen.key = @('$ASTRMAI_QWEN_API_KEY')
}
$config.provider_sources = $sources

$embedding = @($config.provider) | Where-Object { $_.id -eq "openai_embedding" } | Select-Object -First 1
if ($null -eq $embedding) {
    throw "Provider 'openai_embedding' was not found."
}
$embedding.embedding_api_key = '$ASTRMAI_OPENAI_EMBEDDING_API_KEY'

$providers = @($config.provider)
$qwenModel = $providers | Where-Object { $_.id -eq "qwen/qwen3-vl-flash" } | Select-Object -First 1
if ($null -eq $qwenModel) {
    $providers += [pscustomobject]@{
        id = "qwen/qwen3-vl-flash"
        enable = $true
        provider_source_id = "qwen"
        model = "qwen3-vl-flash"
        modalities = @("text", "image")
        custom_extra_body = [pscustomobject]@{}
        max_context_tokens = 32768
    }
}
$config.provider = $providers

$config | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $configPath -Encoding utf8

$pluginConfig = Get-Content -Raw -Encoding utf8 -LiteralPath $pluginConfigPath | ConvertFrom-Json
$pluginConfig.provider.vision_models = @("qwen/qwen3-vl-flash")
$pluginConfig | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $pluginConfigPath -Encoding utf8

$deploy = @(
    @("scripts\astrbot_live_bootstrap.py", "scripts\astrbot_live_bootstrap.py"),
    @("scripts\start_bot_live.bat", "start_bot_live.bat")
)
foreach ($item in $deploy) {
    Copy-Item -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) $item[0]) -Destination (Join-Path $HostRoot $item[1]) -Force
}

Write-Output "Updated Host provider references and deployed the live-secrets launcher."
