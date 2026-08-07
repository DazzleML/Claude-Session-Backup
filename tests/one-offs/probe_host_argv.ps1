# probe_host_argv.ps1 -- which claude process hosts THIS shell, and what
# does its frozen argv claim to be hosting?
#
# Diagnostic for issue #72 (liveness argv-matching mis-attributes forked
# and switched sessions). Run from INSIDE any Claude Code session:
#
#   ! powershell -NoProfile -File tests/one-offs/probe_host_argv.ps1
#
# It walks the parent-process chain from this shell up to the first
# claude CLI process and prints that process's command line. If the
# --resume identifier shown is NOT the session you are sitting in, you
# are looking at #72: an argv-based scan would attribute this process to
# the wrong session (in-app switch) or fail to attribute it (fork /
# fresh launch). Resolve a printed UUID with:  csb show <uuid>
#
# ASCII-only on purpose (Windows codepage rules).

$id = $PID
$chain = @()
while ($true) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $id"
    if ($null -eq $p) { break }
    $chain += ("{0,6}  {1}" -f $p.ProcessId, $p.Name)
    $cmd = $p.CommandLine
    $isDesktop = $cmd -match '--type=|WindowsApps|crashpad|--user-data-dir'
    if ($p.Name -match 'claude' -and -not $isDesktop) {
        "Parent chain (shell -> host):"
        $chain | ForEach-Object { "  $_" }
        ""
        "Hosting claude process : PID $($p.ProcessId)"
        "Frozen argv            : $cmd"
        ""
        "Compare the --resume identifier above against the session you KNOW"
        "you are sitting in. A mismatch (or a --fork-session flag) is #72."
        exit 0
    }
    if ($p.ParentProcessId -eq $p.ProcessId) { break }
    $id = $p.ParentProcessId
}
"No claude CLI ancestor found -- run this from inside a Claude Code session."
"Chain walked:"
$chain | ForEach-Object { "  $_" }
exit 1
