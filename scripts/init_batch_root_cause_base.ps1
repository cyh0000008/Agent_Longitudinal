param(
    [string]$BaseName = "",
    [string]$TableName = "",
    [string]$FolderToken = "",
    [ValidateSet("user", "bot")]
    [string]$As = "bot"
)

$ErrorActionPreference = "Stop"

function Decode-Utf8Base64([string]$Value) {
    return [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

if (-not $BaseName) {
    $BaseName = Decode-Utf8Base64 "5biD6bKB5om56YeP6KGM6L2m6Zeu6aKY5Y6f5Zug5YiG5p6Q"
}
if (-not $TableName) {
    $TableName = Decode-Utf8Base64 "5om56YeP5YiG5p6Q5Lu75Yqh"
}

$fields = @'
[
  {"type":"text","name":"Agent_\u95ee\u9898\u63cf\u8ff0","description":"\u5fc5\u586b"},
  {"type":"text","name":"Agent_h5\u6570\u636e\u8def\u5f84","description":"\u53ef\u7559\u7a7a\uff1b\u7559\u7a7a\u65f6\u6309\u8f66\u53f7/\u8f66\u578b/\u65e5\u671f/\u65f6\u95f4\u81ea\u52a8\u67e5\u627e"},
  {"type":"select","name":"Agent_\u95ee\u9898\u7c7b\u578b","description":"\u5fc5\u586b","multiple":false,"options":[
    {"name":"\u987f\u632b","hue":"Blue","lightness":"Light"},
    {"name":"\u91cd\u5236\u52a8","hue":"Red","lightness":"Light"},
    {"name":"\u8bef\u5236\u52a8","hue":"Orange","lightness":"Light"},
    {"name":"\u6e9c\u8f66","hue":"Yellow","lightness":"Light"},
    {"name":"\u7a33\u901f\u672a\u8fbe Setspeed","hue":"Purple","lightness":"Light"},
    {"name":"\u4e0d\u5408\u7406\u4f4e\u901f","hue":"Carmine","lightness":"Light"},
    {"name":"\u8ddf\u8f66\u8ddd\u79bb","hue":"Green","lightness":"Light"},
    {"name":"\u6a2a\u5411-\u731b\u6253","hue":"Purple","lightness":"Light"},
    {"name":"\u529f\u80fd\u9000\u51fa","hue":"Gray","lightness":"Light"}
  ]},
  {"type":"select","name":"Agent_\u95ee\u9898\u72b6\u6001","description":"\u5fc5\u586b","multiple":false,"options":[
    {"name":"\u5f85\u5206\u6790","hue":"Yellow","lightness":"Light"},
    {"name":"\u5206\u6790\u4e2d","hue":"Blue","lightness":"Light"},
    {"name":"\u5df2\u5206\u6790","hue":"Green","lightness":"Light"},
    {"name":"\u5206\u6790\u5931\u8d25","hue":"Red","lightness":"Light"}
  ]},
  {"type":"datetime","name":"Agent_\u95ee\u9898\u65e5\u671f","description":"\u5fc5\u586b","style":{"format":"yyyy/MM/dd"}},
  {"type":"text","name":"Agent_\u95ee\u9898\u65f6\u95f4","description":"\u5fc5\u586b"},
  {"type":"text","name":"Agent_\u8f66\u53f7","description":"H5\u8def\u5f84\u7559\u7a7a\u65f6\u7528\u4e8e\u81ea\u52a8\u67e5\u627e"},
  {"type":"text","name":"Agent_\u8f66\u578b","description":"H5\u8def\u5f84\u7559\u7a7a\u65f6\u7528\u4e8e\u81ea\u52a8\u67e5\u627e"},
  {"type":"text","name":"Agent_\u5206\u6790\u7ed3\u8bba","description":"\u81ea\u52a8\u751f\u6210"},
  {"type":"text","name":"Agent_\u7ed3\u8bba\u6982\u8981","description":"\u81ea\u52a8\u751f\u6210"},
  {"type":"attachment","name":"Agent_\u5206\u6790\u4fe1\u53f7\u56fe\u793a","description":"\u81ea\u52a8\u751f\u6210"}
]
'@

$runtimeDir = Join-Path (Get-Location) "runtime"
if (-not (Test-Path $runtimeDir)) {
    New-Item -ItemType Directory -Path $runtimeDir | Out-Null
}

$fieldsPath = Join-Path $runtimeDir "batch_root_cause_base_fields.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($fieldsPath, $fields, $utf8NoBom)

$argsList = @(
    "base", "+base-create",
    "--as", $As,
    "--name", $BaseName,
    "--table-name", $TableName,
    "--fields", "@runtime/batch_root_cause_base_fields.json",
    "--format", "json"
)

if ($FolderToken) {
    $argsList += @("--folder-token", $FolderToken)
}

Write-Host "Creating batch root-cause Base: $BaseName / $TableName"
$output = & lark-cli.cmd @argsList
if ($LASTEXITCODE -ne 0) {
    throw "lark-cli base create failed with exit code $LASTEXITCODE"
}

Write-Host $output
Write-Host ""
Write-Host "Next:"
Write-Host "1. Copy app_token/base_token and table_id from the JSON output."
Write-Host "2. Put them into .env:"
Write-Host "   BATCH_ROOT_CAUSE_BASE_TOKEN=<base_token>"
Write-Host "   BATCH_ROOT_CAUSE_TABLE_ID=<table_id>"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_PROBLEM=Agent_问题描述"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_H5_PATH=Agent_h5数据路径"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_PROBLEM_TYPE=Agent_问题类型"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_PROBLEM_DATE=Agent_问题日期"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_PROBLEM_TIME=Agent_问题时间"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_CAR_ID=Agent_车号"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_CAR_MODEL=Agent_车型"
Write-Host "   BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR=/path/to/authorized/ADAS_data/Parsed"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_CONCLUSION=Agent_分析结论"
Write-Host "   BATCH_ROOT_CAUSE_ATTACHMENT_FIELD=Agent_分析信号图示"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_STATUS=Agent_问题状态"
Write-Host "   BATCH_ROOT_CAUSE_FIELD_SUMMARY=Agent_结论概要"
Write-Host "3. Restart bot with start_bot.bat."
