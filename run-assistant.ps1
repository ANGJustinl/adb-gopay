param(
    [Parameter(Position = 0)]
    [string]$Command = "assist",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Expected: $python"
}

& $python -m adb_accessibility_assistant $Command @RemainingArgs
exit $LASTEXITCODE
