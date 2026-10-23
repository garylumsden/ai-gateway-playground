param([Parameter(Mandatory = $true)][string]$Destination)
$ErrorActionPreference = 'Stop'
if (-not $env:PRESENTER_CERT_PASSWORD) {
    throw 'The certificate export password must be supplied by the presenter process.'
}
$trusted = @(Get-ChildItem Cert:\CurrentUser\Root)
$certificate = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object {
        $_.HasPrivateKey -and $_.Subject -eq 'CN=localhost' -and
        $_.NotBefore -le (Get-Date) -and
        $_.NotAfter -gt (Get-Date).AddHours(8) -and
        $_.Thumbprint -in $trusted.Thumbprint
    } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 1
if (-not $certificate) {
    throw 'No existing trusted localhost certificate is available. No trust settings were changed.'
}
$password = ConvertTo-SecureString $env:PRESENTER_CERT_PASSWORD -AsPlainText -Force
Export-PfxCertificate -Cert $certificate -FilePath $Destination -Password $password | Out-Null
