# Resumes a partial download. The JDK is ~182 MB and this shell is unreliable, so
# the file is fetched in ranged chunks and appended; re-running picks up where the
# last chunk left off instead of starting over.
param(
    [string]$Url,
    [string]$Out,
    [int]$ChunkMB = 4,
    [int]$Retries = 6
)
$ErrorActionPreference = 'Stop'
$chunk = $ChunkMB * 1MB
New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null
if (-not (Test-Path $Out)) { New-Item -ItemType File -Path $Out | Out-Null }

for ($attempt = 1; $attempt -le $Retries; $attempt++) {
    $have = (Get-Item $Out).Length
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.AddRange([long]$have)
    $req.Timeout = 60000
    $req.ReadWriteTimeout = 120000
    $req.UserAgent = 'Mozilla/5.0'
    try {
        $resp = $req.GetResponse()
        $stream = $resp.GetResponseStream()
        $out = [System.IO.File]::Open($Out, 'Append', 'Write')
        $buf = New-Object byte[] 65536
        $got = 0
        while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) {
            $out.Write($buf, 0, $n)
            $got += $n
            if ($got % (10MB) -lt $n) {
                $now = (Get-Item $Out).Length
                "$(Get-Date -Format HH:mm:ss)  $([math]::Round($now/1MB,1)) MB" |
                    Out-File -FilePath ($Out + '.progress') -Append
            }
        }
        $out.Close(); $stream.Close(); $resp.Close()
        $total = (Get-Item $Out).Length
        "COMPLETE $total" | Out-File -FilePath ($Out + '.progress') -Append
        if ($resp.Headers['Content-Range']) {
            $expected = ($resp.Headers['Content-Range'] -split '/')[-1]
            if ($expected -ne '*' -and $total -ge [long]$expected) {
                Write-Output "done: $total bytes"
                exit 0
            }
        }
        Write-Output "done: $total bytes"
        exit 0
    } catch {
        $now = (Get-Item $Out).Length
        "attempt $attempt failed at $([math]::Round($now/1MB,1)) MB: $($_.Exception.Message)" |
            Out-File -FilePath ($Out + '.progress') -Append
        try { $stream.Close() } catch {}
        Start-Sleep -Seconds (3 * $attempt)
    }
}
Write-Output "gave up at $((Get-Item $Out).Length) bytes"
exit 1
