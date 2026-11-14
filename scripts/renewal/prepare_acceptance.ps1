[CmdletBinding()]
param([switch]$DownloadPostgres)
$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$oldRoot = Join-Path $sourceRoot '.verification\2026-09-08-acceptance'
$taskRoot = Join-Path $sourceRoot '.verification\2026-10-13-renewal'
$harnessRoot = Join-Path $taskRoot 'harness'
$toolsRoot = Join-Path $taskRoot 'tools'
foreach ($path in @($sourceRoot, (Join-Path $sourceRoot '.verification'), $taskRoot)) {
    if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Redirected acceptance path.' }
}
New-Item -ItemType Directory -Path $harnessRoot,$toolsRoot -Force | Out-Null
$replacements = [ordered]@{
    '2026-09-08-acceptance' = '2026-10-13-renewal'
    'acceptance 2026-09-08 v1' = 'acceptance 2026-10-13 v1'
    'C:\Program Files\PostgreSQL\18\bin' = (Join-Path $toolsRoot 'pgsql\bin')
    '15432' = '15433'; '16379' = '16380'; '19000' = '19010'; '19001' = '19011'
    '11025' = '11026'; '18020' = '18021'; '18100' = '18101'; '18300' = '18301'
    '13100' = '13101'; '13310' = '13311'
}
$names = @('acceptance-runtime.ps1','acceptance-services.ps1','acceptance-frontend.ps1','prepare-python-envs.ps1',
    'provision-postgres.sql','acceptance_driver.py','acceptance_browser_runner.cjs','acceptance-clamav.ps1')
$rows = @()
foreach ($name in $names) {
    $original = Join-Path $oldRoot "harness\$name"
    $destination = Join-Path $harnessRoot $name
    if (Test-Path -LiteralPath $destination) { throw "Refuse to overwrite existing harness: $name" }
    $content = [IO.File]::ReadAllText($original)
    foreach ($entry in $replacements.GetEnumerator()) { $content = $content.Replace($entry.Key, $entry.Value) }
    [IO.File]::WriteAllText($destination, $content, [Text.UTF8Encoding]::new($false))
    $rows += [ordered]@{ name=$name; originalSha256=(Get-FileHash -LiteralPath $original -Algorithm SHA256).Hash; adaptedSha256=(Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash }
}
$commonSource = Join-Path $oldRoot 'checks\common'
if (Test-Path -LiteralPath $commonSource) {
    $commonTarget = Join-Path $taskRoot 'checks\common'
    New-Item -ItemType Directory -Path $commonTarget -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $commonSource -File) {
        if ($item.Extension -notin @('.py','.js','.cjs','.mjs')) { continue }
        $text = [IO.File]::ReadAllText($item.FullName)
        foreach ($entry in $replacements.GetEnumerator()) { $text = $text.Replace($entry.Key, $entry.Value) }
        [IO.File]::WriteAllText((Join-Path $commonTarget $item.Name), $text, [Text.UTF8Encoding]::new($false))
    }
}
$minio = Get-ChildItem -LiteralPath (Join-Path $oldRoot 'tools') -Filter 'minio.*.exe' -File | Select-Object -First 1
if ($null -eq $minio -or (Get-FileHash -LiteralPath $minio.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'af709e6ba68488404e85acdd22a3030d0f5e56a108d4b27d744f18ceb50861b4') { throw 'Pinned MinIO binary unavailable.' }
Copy-Item -LiteralPath $minio.FullName -Destination (Join-Path $toolsRoot $minio.Name)
if ($DownloadPostgres) {
    $uri = 'https://get.enterprisedb.com/postgresql/postgresql-16.15-4-windows-x64-binaries.zip'
    $archive = Join-Path $toolsRoot 'postgresql-16.15-4-windows-x64-binaries.zip'
    if (Test-Path -LiteralPath $archive) { throw 'Refuse to replace PostgreSQL archive.' }
    Invoke-WebRequest -Uri $uri -OutFile $archive
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        foreach ($entry in $zip.Entries) {
            $full = [IO.Path]::GetFullPath((Join-Path $toolsRoot $entry.FullName))
            if (-not $full.StartsWith($toolsRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe archive path.' }
        }
    } finally { $zip.Dispose() }
    Expand-Archive -LiteralPath $archive -DestinationPath $toolsRoot
    $postgres = Join-Path $toolsRoot 'pgsql\bin\postgres.exe'
    $version = (& $postgres --version).ToString().Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^postgres \(PostgreSQL\) 16\.') { throw 'Unexpected PostgreSQL major version.' }
    $rows += [ordered]@{ name='postgresql16'; source=$uri; archiveSha256=(Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash; version=$version }
}
$evidence = Join-Path $taskRoot 'evidence\harness-preparation.json'
[IO.File]::WriteAllText($evidence, (($rows | ConvertTo-Json -Depth 5) + "`n"), [Text.UTF8Encoding]::new($false))
$rows | ConvertTo-Json -Depth 5
