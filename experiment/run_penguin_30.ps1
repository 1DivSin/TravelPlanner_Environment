Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$MainRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = (Get-Command python -ErrorAction Stop).Source
$KeyFile = if ($env:TRAVELPLANNER_GATEWAY_KEY_FILE) { $env:TRAVELPLANNER_GATEWAY_KEY_FILE } else { 'D:/Downloads/penguin_win_bq.txt' }
$BaseUrl = 'https://penguinapi.org'
$Indices = '1,11,14,17,28,33,38,41,46,48,70,72,77,81,83,100,110,113,116,118,123,124,138,144,146,151,159,161,162,163'
$RunId = 'cc-dynamic-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
$Runs = Join-Path $MainRoot (Join-Path 'runs/dynamic' $RunId)
$Queries = Join-Path $MainRoot 'TravelPlanner/postprocess/example_evaluation.jsonl'
$Attempts = Join-Path $Runs 'formal-30-attempts.jsonl'
$Gateway = Join-Path $Runs 'gateway.json'
$Predictions = Join-Path $Runs 'formal-30-predictions.jsonl'
$Scores = Join-Path $Runs 'formal-30-scores.json'
$Report = Join-Path $Runs 'formal-30-report.md'
$McpConfig = Join-Path $MainRoot 'experiment/mcp.json'
$TempRoot = Join-Path $Runs 'tmp'
$GatewayPreflight = Join-Path $MainRoot 'experiment/gateway_preflight.py'
$Runner = Join-Path $MainRoot 'experiment/runner.py'
$Evaluator = Join-Path $MainRoot 'experiment/evaluate_selected.py'
$SecretEnvironmentVariables = @(
    'ANTHROPIC_AUTH_TOKEN'
    'ANTHROPIC_API_KEY'
    'CLAUDE_TOKEN'
    'CLAUDE_CODE_OAUTH_TOKEN'
)

New-Item -ItemType Directory -Force -Path $Runs, $TempRoot | Out-Null

& $Python $GatewayPreflight --key-file $KeyFile --base-url $BaseUrl --output $Gateway
if ($LASTEXITCODE -ne 0) {
    throw "Gateway preflight failed with exit code $LASTEXITCODE"
}

$gatewayConfig = Get-Content -Raw -LiteralPath $Gateway | ConvertFrom-Json
if ($gatewayConfig.base_url -ne $BaseUrl -or
    $gatewayConfig.auth_mode -notin @('bearer', 'x-api-key') -or
    [string]::IsNullOrWhiteSpace($gatewayConfig.main_model) -or
    [string]::IsNullOrWhiteSpace($gatewayConfig.haiku_model)) {
    throw 'Gateway preflight returned an invalid safe configuration'
}

$key = $null
try {
    $env:CLAUDE_CONFIG_DIR = Join-Path $Runs 'claude-config'
    $env:TEMP = $TempRoot
    $env:TMP = $TempRoot
    New-Item -ItemType Directory -Force -Path $env:CLAUDE_CONFIG_DIR | Out-Null

    $key = @([System.IO.File]::ReadAllLines($KeyFile))
    if ($key.Count -ne 1 -or
        [string]::IsNullOrEmpty($key[0]) -or
        $key[0] -match '\s') {
        throw 'Key file must contain exactly one non-empty, whitespace-free line'
    }
    $key = $key[0]

    foreach ($name in $SecretEnvironmentVariables) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
    $env:ANTHROPIC_BASE_URL = $BaseUrl
    $env:ANTHROPIC_API_BASE = $BaseUrl
    $env:CLAUDE_CODE_DISABLE_NATIVE_AUTH = '1'
    $env:ANTHROPIC_DEFAULT_OPUS_MODEL = $gatewayConfig.main_model
    $env:ANTHROPIC_DEFAULT_SONNET_MODEL = $gatewayConfig.main_model
    $env:ANTHROPIC_DEFAULT_HAIKU_MODEL = $gatewayConfig.haiku_model

    switch ($gatewayConfig.auth_mode) {
        'bearer' { $env:ANTHROPIC_AUTH_TOKEN = $key }
        'x-api-key' { $env:ANTHROPIC_API_KEY = $key }
        default { throw "Unsupported gateway auth mode: $($gatewayConfig.auth_mode)" }
    }

    & $Python $Runner --queries $Queries --output $Attempts --indices '1' --expected-count 1 --model $gatewayConfig.main_model --api-base-url $BaseUrl --mcp-config $McpConfig --workdir $MainRoot --timeout 2400 --resume
    if ($LASTEXITCODE -ne 0) {
        throw "Index 1 pilot failed with exit code $LASTEXITCODE"
    }

    $lastIndexOne = Get-Content -LiteralPath $Attempts |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { $_ | ConvertFrom-Json } |
        Where-Object { $_.idx -eq 1 } |
        Select-Object -Last 1
    $planProperty = if ($null -eq $lastIndexOne) { $null } else { $lastIndexOne.PSObject.Properties['plan'] }
    if ($null -eq $planProperty -or -not $planProperty.Value -or
        $null -ne $lastIndexOne.PSObject.Properties['error']) {
        throw 'Index 1 pilot did not produce a plan without an error property'
    }

    & $Python $Runner --queries $Queries --output $Attempts --indices $Indices --expected-count 30 --model $gatewayConfig.main_model --api-base-url $BaseUrl --mcp-config $McpConfig --workdir $MainRoot --timeout 2400 --resume
    if ($LASTEXITCODE -ne 0) {
        throw "30-query run failed with exit code $LASTEXITCODE"
    }
}
finally {
    Remove-Item -LiteralPath 'Env:CLAUDE_CONFIG_DIR' -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath 'Env:TEMP' -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath 'Env:TMP' -ErrorAction SilentlyContinue
    foreach ($name in $SecretEnvironmentVariables) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
    $key = $null
}

& $Python $Evaluator --attempts $Attempts --queries $Queries --indices $Indices --travelplanner-root (Join-Path $MainRoot 'TravelPlanner') --gateway $Gateway --predictions $Predictions --scores $Scores --report $Report
if ($LASTEXITCODE -ne 0) {
    throw "Selected-query evaluation failed with exit code $LASTEXITCODE"
}
