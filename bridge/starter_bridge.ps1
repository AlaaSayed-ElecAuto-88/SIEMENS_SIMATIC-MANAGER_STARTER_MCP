# STARTER (SINAMICS / MICROMASTER drive commissioning) scripting bridge.
#
# Must run in 32-bit Windows PowerShell: STARTER's scripting object (Simotion.Application,
# U7WSCOAX.DLL) is a 32-bit in-process COM server. starter.py starts this script and talks
# to it with one JSON object per line, same framing as s7bridge.ps1. It runs in its own
# process, separate from the STEP 7 bridge, and keeps the online connection between calls.
#
# Object model: Application -> Projects -> Project -> Devices (drive units) -> drive
# objects, which are TOs (drives, infeeds) and SubObjects (Control Unit, MICROMASTER
# drive) -> Parameters(number, index).

$ErrorActionPreference = 'Stop'

$MARK = '@@S7@@'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$stdin = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $utf8)
$stdout = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $utf8)
$stdout.AutoFlush = $true

$SEPARATOR = '|'                       # between array values in GetAllValues
$script:App = $null
$script:Index = @{}                    # parameter list per drive object, built on first search
$script:OnlineProjects = New-Object System.Collections.ArrayList

function Send($obj) { $stdout.WriteLine($MARK + (ConvertTo-Json $obj -Compress -Depth 12)) }

function Get-App {
    if ($null -eq $script:App) {
        $a = New-Object -ComObject Simotion.Application
        $a.Separator = $SEPARATOR
        $script:App = $a
    }
    return , $script:App
}

function Arg($a, $name, $default = $null) {
    if ($null -ne $a -and ($a.PSObject.Properties.Name -contains $name) -and $null -ne $a.$name) { return $a.$name }
    return $default
}

function Need($a, $name) {
    $v = Arg $a $name
    if ($null -eq $v -or "$v" -eq '') { throw "Missing required argument '$name'." }
    return $v
}

function ErrorDetails {
    # STARTER collects extra messages for the last call here.
    try {
        $e = (Get-App).ErrorDetails
        $all = @(@($e.Errors) + @($e.Warnings) | Where-Object { "$_" -ne '' })
        $e.Clear()
        return ($all -join "`n")
    } catch { return '' }
}

function Find-Project($key) {
    $app = Get-App
    $key = [string]$key
    # A project folder is accepted in place of its .s7p file.
    if (Test-Path -LiteralPath $key -PathType Container) {
        $file = Get-ChildItem -LiteralPath $key -Filter *.s7p | Select-Object -First 1
        if ($null -eq $file) { throw "No STEP 7 project file (*.s7p) in '$key'." }
        $key = $file.FullName
    }
    try { $p = $app.Projects.Item($key) } catch { $p = $null }
    if ($null -eq $p) { throw "Project '$key' not found. Use a name from list_projects or the project's path." }
    return , $p
}

function Find-Device($a) {
    $proj = Find-Project (Need $a 'project')
    $name = [string](Need $a 'device')
    try { $d = $proj.Devices.Item($name) } catch { $d = $null }
    if ($null -eq $d) {
        $names = @(); foreach ($x in $proj.Devices) { $names += $x.Name }
        throw "Drive unit '$name' not found in project '$($proj.Name)'. Available: $($names -join ', ')"
    }
    return , $d
}

function Get-DriveObjects($dev) {
    $out = New-Object System.Collections.ArrayList
    try { foreach ($s in $dev.SubObjects) { if ([int]$s.DriveId -ne 65535) { [void]$out.Add($s) } } } catch { }
    try { foreach ($t in $dev.TOs) { [void]$out.Add($t) } } catch { }
    return , $out
}

function Find-DriveObject($dev, $name) {
    $all = Get-DriveObjects $dev
    if ($null -eq $name -or "$name" -eq '') {
        if ($all.Count -eq 1) { return , $all[0] }
        throw "Drive unit '$($dev.Name)' has $($all.Count) drive objects; specify one of: $(($all | ForEach-Object { $_.Name }) -join ', ')"
    }
    foreach ($o in $all) { if ($o.Name -eq [string]$name) { return , $o } }
    throw "Drive object '$name' not found in '$($dev.Name)'. Available: $(($all | ForEach-Object { $_.Name }) -join ', ')"
}

function Resolve-DriveObject($a) {
    $dev = Find-Device $a
    return , (Find-DriveObject $dev (Arg $a 'drive_object'))
}

function Parse-Item($text) {
    # "p1082", "1082", "r2", "p1000[1]"  ->  number, index (null when not given)
    if ("$text".Trim() -notmatch '^[pPrR]?(\d+)(?:\[(\d+)\])?$') { throw "'$text' is not a parameter (examples: p1082, r2, p1000[1])." }
    $index = $null
    if ($Matches[2]) { $index = [int]$Matches[2] }
    return @{ number = [int]$Matches[1]; index = $index }
}

function Plain($v) {
    if ($null -eq $v) { return $null }
    if ($v -is [bool] -or $v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [single] -or $v -is [decimal] -or $v -is [int16] -or $v -is [byte] -or $v -is [uint16] -or $v -is [uint32]) { return $v }
    return "$v"
}

function ParamInfo($obj, $number, $index) {
    $first = 0
    if ($null -ne $index) { $first = $index }
    $p = $obj.Parameters.Item($number, $first)
    $o = [ordered]@{ parameter = $p.Name; description = $p.Comment }
    if ($null -ne $index) {
        $o['value'] = Plain $p.ActValue
    } else {
        # No index given: return every index of the parameter.
        $all = [string]$obj.Parameters.GetAllValues($number)
        $parts = $all.Split($SEPARATOR)
        if ($parts.Length -gt 1) { $o['parameter'] = ($p.Name -replace '\[\d+\]$', ''); $o['values'] = $parts } else { $o['value'] = Plain $p.ActValue }
    }
    try { $t = "$($p.EnumValueText)"; if ($t -ne '') { $o['text'] = $t } } catch { }
    try { $u = "$($p.Dimension)"; if ($u -ne '') { $o['unit'] = $u } } catch { }
    try { $o['min'] = Plain $p.MinValue; $o['max'] = Plain $p.MaxValue } catch { }
    try { $o['type'] = $p.TypeName; $o['readOnly'] = [bool]$p.ReadOnly } catch { }
    return $o
}

# ---------------------------------------------------------------- operations

function Op-Ping($a) {
    $app = Get-App
    return [ordered]@{ starter = $app.Installation.Version; projects = $app.Projects.Count }
}

function DeviceInfo($d) {
    $objs = New-Object System.Collections.ArrayList
    foreach ($o in (Get-DriveObjects $d)) {
        [void]$objs.Add([ordered]@{ name = $o.Name; type = $o.ExternalTypeName; driveObjectNumber = [int]$o.DriveId })
    }
    $o = [ordered]@{ name = $d.Name; type = $d.ExternalTypeName; version = $d.TypeVersion; description = $d.Description }
    try { $o['online'] = [bool]$d.Online } catch { }
    $o['driveObjects'] = $objs
    return $o
}

function Op-Devices($a) {
    $proj = Find-Project (Need $a 'project')
    $out = New-Object System.Collections.ArrayList
    foreach ($d in $proj.Devices) { [void]$out.Add((DeviceInfo $d)) }
    $online = $false
    try { $online = [bool]$proj.Online } catch { }
    return [ordered]@{ project = $proj.Name; online = $online; devices = $out }
}

function Op-Read($a) {
    $obj = Resolve-DriveObject $a
    $out = New-Object System.Collections.ArrayList
    foreach ($item in @(Need $a 'items')) {
        try {
            $i = Parse-Item $item
            [void]$out.Add((ParamInfo $obj $i.number $i.index))
        } catch { [void]$out.Add([ordered]@{ parameter = "$item"; error = $_.Exception.Message }) }
    }
    return [ordered]@{ driveObject = $obj.Name; parameters = $out }
}

function Op-Search($a) {
    $dev = Find-Device $a
    $obj = Find-DriveObject $dev (Arg $a 'drive_object')
    $key = "$($dev.ObjectId)|$($obj.Name)"
    if (-not $script:Index.ContainsKey($key)) {
        $list = New-Object System.Collections.ArrayList
        foreach ($s in $obj.Symbols) { [void]$list.Add(@($s.Name, [string]$s.Comment)) }
        $script:Index[$key] = $list
    }
    $needle = ([string](Need $a 'text')).ToLower()
    $limit = [int](Arg $a 'limit' 40)
    $out = New-Object System.Collections.ArrayList
    $total = 0
    foreach ($e in $script:Index[$key]) {
        if ($e[0].ToLower().Contains($needle) -or $e[1].ToLower().Contains($needle)) {
            $total++
            if ($out.Count -lt $limit) { [void]$out.Add([ordered]@{ parameter = $e[0]; description = $e[1] }) }
        }
    }
    return [ordered]@{ driveObject = $obj.Name; matches = $total; parameters = $out }
}

function Op-Write($a) {
    $obj = Resolve-DriveObject $a
    $out = New-Object System.Collections.ArrayList
    foreach ($prop in (Need $a 'values').PSObject.Properties) {
        $r = [ordered]@{ parameter = $prop.Name }
        try {
            $i = Parse-Item $prop.Name
            $index = 0
            if ($null -ne $i.index) { $index = $i.index }
            $p = $obj.Parameters.Item($i.number, $index)
            $r['parameter'] = $p.Name
            $r['before'] = Plain $p.ActValue
            $v = $prop.Value
            # JSON numbers arrive as Int64/Decimal, which the COM side does not accept.
            if ($v -is [long] -or $v -is [int]) { $v = [int]$v } elseif ($v -is [double] -or $v -is [decimal]) { $v = [double]$v } else { $v = [string]$v }
            # Not "$p.ActValue = $v": PowerShell caches the first value type it sees for a COM property
            # and then rejects every other type with "Specified cast is not valid".
            [void][System.__ComObject].InvokeMember('ActValue', [Reflection.BindingFlags]::SetProperty, $null, $p, @(, $v))
            $r['after'] = Plain $p.ActValue
            try { $t = "$($p.EnumValueText)"; if ($t -ne '') { $r['text'] = $t } } catch { }
        } catch {
            $e = $_.Exception
            while ($null -ne $e.InnerException) { $e = $e.InnerException }
            $r['error'] = $e.Message
            if ($e.Message -match 'ACCESS_DENIED') {
                $r['hint'] = 'STARTER has locked this parameter: it is read-only, or fixed by the selected macro (p15) or telegram (p922), or only changeable in another commissioning state (p10 / p9).'
            }
            $more = ErrorDetails
            if ($more -ne '') { $r['details'] = $more }
        }
        [void]$out.Add($r)
    }
    $online = $false
    try { $online = [bool](Find-Device $a).Online } catch { }
    return [ordered]@{ driveObject = $obj.Name; writtenTo = $(if ($online) { 'drive (online)' } else { 'project (offline)' }); parameters = $out }
}

function Op-IsOnline($a) {
    $dev = Find-Device $a
    return [ordered]@{ online = [bool]$dev.Online }
}

function Op-AddDevice($a) {
    $proj = Find-Project (Need $a 'project')
    $d = $proj.Devices.Add([string](Need $a 'type'), [string](Need $a 'version'), [string](Need $a 'name'))
    return (DeviceInfo $d)
}

function Op-AddDriveObject($a) {
    $dev = Find-Device $a
    $t = $dev.TOs.Add([string](Need $a 'type'), [string](Need $a 'name'))
    return [ordered]@{ device = $dev.Name; name = $t.Name; type = $t.ExternalTypeName; driveObjectNumber = [int]$t.DriveId }
}

function Op-ExportDevice($a) {
    $dev = Find-Device $a
    $path = [string](Need $a 'path')
    $dev.Export($path, [string](Arg $a 'flags' ''))
    return [ordered]@{ device = $dev.Name; path = $path }
}

function Op-SetOnline($a) {
    $proj = Find-Project (Need $a 'project')
    $want = [bool](Need $a 'online')
    if ($want) {
        $dev = Find-Device $a
        $dev.EnableOnline = $true          # every other drive unit is deselected automatically
        $proj.Online = $true
        [void]$script:OnlineProjects.Add($proj)
        $proj.Devices.UpdateOnlineStates()
        if (-not [bool]$dev.Online) { throw "Could not go online with '$($dev.Name)'. Check its connection settings and the PG/PC interface. $(ErrorDetails)" }
        return [ordered]@{ project = $proj.Name; device = $dev.Name; online = $true }
    }
    $proj.Online = $false
    return [ordered]@{ project = $proj.Name; online = [bool]$proj.Online }
}

function Op-DeviceAction($a) {
    $dev = Find-Device $a
    $action = [string](Need $a 'action')
    if (-not [bool]$dev.Online) { throw "'$($dev.Name)' is offline; go online first." }
    switch ($action) {
        'download' { $dev.Download() }
        'upload' { $dev.Upload() }
        'ram_to_rom' { $dev.Ram2Rom() }
        default { throw "Unknown action '$action'." }
    }
    return [ordered]@{ device = $dev.Name; action = $action; done = $true; messages = (ErrorDetails) }
}

function Op-GetConnection($a) {
    $dev = Find-Device $a
    $router = $null; $routerType = $null; $routerAddr = $null; $subnet = $null; $type = $null; $iface = $null; $addr = $null
    $dev.GetDeviceAddressSettings([ref]$router, [ref]$routerType, [ref]$routerAddr, [ref]$subnet, [ref]$type, [ref]$iface, [ref]$addr)
    return [ordered]@{
        device = $dev.Name; accessPoint = $dev.DeviceAccessPoint
        addressType = "$type"; interface = "$iface"; address = "$addr"; s7SubnetId = "$subnet"
        viaRouter = [bool]$router; routerAddressType = "$routerType"; routerAddress = "$routerAddr"
    }
}

function Op-SetConnection($a) {
    $dev = Find-Device $a
    $cur = Op-GetConnection $a
    $ap = Arg $a 'access_point'
    if ($null -ne $ap) { $dev.DeviceAccessPoint = [string]$ap }
    if ($null -ne (Arg $a 'address') -or $null -ne (Arg $a 'address_type') -or $null -ne (Arg $a 'interface')) {
        [void]$dev.SetDeviceAddressSettings($cur.viaRouter, $cur.routerAddressType, $cur.routerAddress, $cur.s7SubnetId,
            [string](Arg $a 'address_type' $cur.addressType), [string](Arg $a 'interface' $cur.interface), [string](Arg $a 'address' $cur.address))
    }
    return (Op-GetConnection $a)
}

$OPS = @{
    ping = 'Op-Ping'; devices = 'Op-Devices'; read = 'Op-Read'; search = 'Op-Search'; write = 'Op-Write'
    is_online = 'Op-IsOnline'; add_device = 'Op-AddDevice'; add_drive_object = 'Op-AddDriveObject'
    export_device = 'Op-ExportDevice'; set_online = 'Op-SetOnline'; device_action = 'Op-DeviceAction'
    get_connection = 'Op-GetConnection'; set_connection = 'Op-SetConnection'
}

try {
    while ($true) {
        $line = $stdin.ReadLine()
        if ($null -eq $line) { break }
        if ($line.Trim() -eq '') { continue }
        $id = $null
        try {
            $req = ConvertFrom-Json $line
            $id = $req.id
            if (-not $OPS.ContainsKey([string]$req.op)) { throw "Unknown op '$($req.op)'." }
            $result = & $OPS[[string]$req.op] $req.args
            Send @{ id = $id; ok = $true; result = $result }
        } catch {
            Send @{ id = $id; ok = $false; error = $_.Exception.Message; log = (ErrorDetails) }
        }
    }
} finally {
    if ($null -ne $script:App) {
        # Leave no project online when the server stops. Only the projects this worker took online are
        # touched; walking the whole Projects collection would open every project on the PC.
        foreach ($p in $script:OnlineProjects) { try { $p.Online = $false } catch { } }
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($script:App) } catch { }
    }
}
