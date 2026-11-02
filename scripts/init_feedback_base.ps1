param(
    [string]$BaseName = "布鲁问题反馈",
    [string]$TableName = "反馈记录",
    [string]$FolderToken = "",
    [ValidateSet("user", "bot")]
    [string]$As = "bot"
)

$ErrorActionPreference = "Stop"

$fields = @'
[
  {"type":"text","name":"标题"},
  {"type":"datetime","name":"反馈时间","style":{"format":"yyyy/MM/dd HH:mm"}},
  {"type":"text","name":"上传人员"},
  {"type":"text","name":"群聊"},
  {"type":"text","name":"触发消息ID"},
  {"type":"text","name":"反馈内容"},
  {"type":"select","name":"状态","multiple":false,"options":[
    {"name":"待确认","hue":"Orange","lightness":"Light"},
    {"name":"处理中","hue":"Blue","lightness":"Light"},
    {"name":"已修复","hue":"Green","lightness":"Light"},
    {"name":"不处理","hue":"Gray","lightness":"Light"}
  ]},
  {"type":"text","name":"关联能力"},
  {"type":"text","name":"上下文摘要"},
  {"type":"text","name":"本地快照路径"},
  {"type":"attachment","name":"对话快照"}
]
'@

$runtimeDir = Join-Path (Get-Location) "runtime"
if (-not (Test-Path $runtimeDir)) {
    New-Item -ItemType Directory -Path $runtimeDir | Out-Null
}

$fieldsPath = Join-Path $runtimeDir "feedback_base_fields.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($fieldsPath, $fields, $utf8NoBom)

$argsList = @(
    "base", "+base-create",
    "--as", $As,
    "--name", $BaseName,
    "--table-name", $TableName,
    "--fields", "@runtime/feedback_base_fields.json",
    "--format", "json"
)

if ($FolderToken) {
    $argsList += @("--folder-token", $FolderToken)
}

Write-Host "Creating feedback Base: $BaseName / $TableName"
$output = & lark-cli.cmd @argsList
if ($LASTEXITCODE -ne 0) {
    throw "lark-cli base create failed with exit code $LASTEXITCODE"
}

Write-Host $output
Write-Host ""
Write-Host "Next:"
Write-Host "1. Copy app_token/base_token and table_id from the JSON output."
Write-Host "2. Put them into .env:"
Write-Host "   FEEDBACK_BASE_TOKEN=<base_token>"
Write-Host "   FEEDBACK_TABLE_ID=<table_id>"
Write-Host "   FEEDBACK_ATTACHMENT_FIELD=对话快照"
Write-Host "3. Restart bot with start_bot.bat."
