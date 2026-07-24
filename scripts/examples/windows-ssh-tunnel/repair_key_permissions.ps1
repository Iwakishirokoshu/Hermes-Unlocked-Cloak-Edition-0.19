[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$KeyPath
)

$ErrorActionPreference = "Stop"
$resolvedPath = (Resolve-Path -LiteralPath $KeyPath).Path
if ((Get-Item -LiteralPath $resolvedPath -Force).PSIsContainer) {
    throw "SSH key path must point to a file."
}

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentIdentity = $currentUser.Name
$currentSid = $currentUser.User

# Only the DACL is written; SACL metadata requires SeSecurityPrivilege.
$acl = [Security.AccessControl.FileSecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$readRule = [Security.AccessControl.FileSystemAccessRule]::new(
    $currentSid,
    [Security.AccessControl.FileSystemRights]::Read,
    [Security.AccessControl.AccessControlType]::Allow
)
$acl.AddAccessRule($readRule)
[IO.File]::SetAccessControl($resolvedPath, $acl)

$verified = Get-Acl -LiteralPath $resolvedPath
$otherAllowRules = @($verified.Access | Where-Object {
    $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
    $_.IdentityReference.Value -ne $currentIdentity -and
    $_.IdentityReference.Value -ne $currentSid.Value
})
if ($otherAllowRules.Count -gt 0) {
    throw "SSH key permissions still allow another identity."
}
Write-Host "SSH key access is limited to $currentIdentity."