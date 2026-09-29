"""driver_uia — walk ANY Windows desktop app via UI Automation.

For apps that are not Python/Qt: launches (or attaches to) a real window on
the interactive desktop and drives it through Windows UI Automation — the
same accessibility layer screen readers use — so it works for Win32, WPF,
WinForms, Qt, and Electron/Chromium apps that expose their UI tree.

What it does per step:
  1. screenshot the window
  2. invoke the next menu item / toolbar button / toggle (never one whose
     name matches the blocklist — destructive verbs are skipped by default)
  3. wait, press Escape to dismiss any dropdown/dialog that opened,
     screenshot again

NOT headless: the app window flashes on screen and the mouse/keyboard must
be left alone during the walk. If the walk dies mid-way (some apps crash
under automation), re-run — steps are individually logged and numbered.

Usage:
  python driver_uia.py --launch "notepad.exe" --out C:/Temp/walk
  python driver_uia.py --attach "Untitled - Notepad" --out C:/Temp/walk
  python driver_uia.py --launch "app.exe" --dry-run      # list, don't click
  python driver_uia.py --launch "app.exe" --blocklist "pay|purchase"

Requires Windows PowerShell 5.1+ (built in) — no pip installs.
"""
import argparse
import json
import os
import subprocess
import tempfile
import time

PS_TEMPLATE = r"""
param(
    [string]$Mode,          # enum | menu | controls | shot
    [int]$ProcId = 0,
    [string]$Title = "",
    [string]$ProcNames = "", # candidate process names from launch diff
    [string]$Target = "",   # menu name for $Mode 'menu'
    [int]$Index = -1,       # control index for $Mode 'controls'
    [string]$OutPng = "",
    [string]$Blocklist = "(exit|quit|close|delete|remove|format|erase|uninstall|shutdown|sign out|log ?off|pay|purchase|checkout|save)",
    [string]$Visited = ""   # comma-separated names already invoked
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Get-TargetWindow {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    # store apps launch through alias stubs that exit immediately: the
    # real window belongs to a NEW process we identified by diffing the
    # process table before/after launch (-ProcNames). Retry ~20s.
    $names = @()
    if ($ProcNames) { $names = $ProcNames.Split(",") }
    for ($try = 0; $try -lt 20; $try++) {
        if ($ProcId -gt 0) {
            $cond = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $ProcId)
            $win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
            if ($win) { return $win }
        }
        if ($Title) {
            $cond = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty, $Title)
            $win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
            if ($win) { return $win }
        }
        if ($names.Count -gt 0) {
            $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                [System.Windows.Automation.Condition]::TrueCondition)
            foreach ($w in $wins) {
                try {
                    $pn = (Get-Process -Id $w.Current.ProcessId -ErrorAction SilentlyContinue).ProcessName
                    if ($names -contains $pn) { return $w }
                } catch { }
            }
        }
        Start-Sleep -Seconds 1
    }
    throw "window not found (pid=$ProcId title='$Title' names='$ProcNames')"
}

function Save-Shot($win, $path) {
    $r = $win.BoundingRectangle
    if ($r.Width -le 0) { throw "window has no rect" }
    $b = New-Object System.Drawing.Bitmap([int]$r.Width, [int]$r.Height)
    $g = [System.Drawing.Graphics]::FromImage($b)
    $g.CopyFromScreen([int]$r.X, [int]$r.Y, 0, 0, $b.Size)
    $g.Dispose()
    $b.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $b.Dispose()
}

function Get-Controls($win) {
    $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition)
    $out = @()
    $i = 0
    foreach ($el in $all) {
        $name = ""
        try { $name = $el.Current.Name } catch { }
        $ctype = ""
        try { $ctype = $el.Current.LocalizedControlType } catch { }
        if (-not $name -or $name.Length -gt 80) { continue }
        $patterns = @()
        foreach ($pt in @(
            [System.Windows.Automation.InvokePattern]::Pattern,
            [System.Windows.Automation.TogglePattern]::Pattern,
            [System.Windows.Automation.ExpandCollapsePattern]::Pattern)) {
            try {
                if ($el.GetCurrentPattern($pt)) { $patterns += $pt.ProgrammaticName }
            } catch { }
        }
        if ($patterns.Count -gt 0 -and $ctype -notin @("menu item", "menu")) {
            $out += @{ i = $i; name = $name; type = $ctype }
        }
        $i++
    }
    return $out
}

$win = Get-TargetWindow

switch ($Mode) {
    "shot" {
        Save-Shot $win $OutPng
        Write-Output "SHOT OK"
    }
    "enum" {
        (Get-Controls $win) | ConvertTo-Json -Compress
    }
    "menu" {
        # expand one top-level menu, then walk its items: for each item,
        # invoke (unless blocklisted/visited), shoot, Escape.
        $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition)
        $menu = $null
        foreach ($el in $all) {
            try {
                if ($el.Current.LocalizedControlType -eq "menu" -and
                    $el.Current.Name -eq $Target) { $menu = $el; break }
            } catch { }
        }
        if (-not $menu) { throw "menu '$Target' not found" }
        $expand = $menu.GetCurrentPattern(
            [System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        $expand.Expand()
        Start-Sleep -Milliseconds 700
        Save-Shot $win ($OutPng -replace "\.png$", "_open.png")
        $items = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition)
        $results = @()
        foreach ($el in $items) {
            $name = ""; $ctype = ""
            try { $name = $el.Current.Name; $ctype = $el.Current.LocalizedControlType } catch { }
            if ($ctype -ne "menu item" -or -not $name) { continue }
            if ($name -match $Blocklist) {
                $results += @{ name = $name; status = "blocklisted" }
                continue
            }
            if ($Visited -split "," -contains $name) {
                $results += @{ name = $name; status = "visited" }
                continue
            }
            try {
                $inv = $el.GetCurrentPattern(
                    [System.Windows.Automation.InvokePattern]::Pattern)
                Start-Sleep -Milliseconds 400
                $inv.Invoke()
                Start-Sleep -Milliseconds 1200
                $safe = ($name -replace "[^\w\-]+", "_")
                if ($safe.Length -gt 40) { $safe = $safe.Substring(0, 40) }
                Save-Shot $win ($OutPng -replace "\.png$", "__$safe.png")
                $results += @{ name = $name; status = "invoked" }
                # re-open the dropdown for the next item
                try { $expand.Expand(); Start-Sleep -Milliseconds 500 } catch { }
            } catch {
                $results += @{ name = $name; status = "error: $($_.Exception.Message)" }
            }
        }
        [System.Windows.Forms.SendKeys]::SendWait("{ESC}")
        $results | ConvertTo-Json -Compress
    }
    "controls" {
        # invoke the Nth invokable control (by the enumeration this same
        # call performs, so indices are always fresh), shoot the result
        $controls = Get-Controls $win
        if ($Index -lt 0 -or $Index -ge $controls.Count) {
            Write-Output "DONE $($controls.Count)"
            exit 0
        }
        $c = $controls[$Index]
        if ($c.name -match $Blocklist -or ($Visited -split "," -contains $c.name)) {
            Write-Output "SKIP $($c.name)"
            exit 0
        }
        $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition)
        $matches = @()
        foreach ($el in $all) {
            $name = ""
            try { $name = $el.Current.Name } catch { }
            if ($name -eq $c.name) { $matches += $el }
        }
        if ($matches.Count -eq 0) { Write-Output "GONE $($c.name)"; exit 0 }
        $el = $matches[0]
        $acted = $false
        foreach ($pt in @(
            [System.Windows.Automation.InvokePattern]::Pattern,
            [System.Windows.Automation.TogglePattern]::Pattern,
            [System.Windows.Automation.ExpandCollapsePattern]::Pattern)) {
            if ($acted) { break }
            try {
                $p = $el.GetCurrentPattern($pt)
                if ($p -is [System.Windows.Automation.InvokePattern]) { $p.Invoke(); $acted = $true }
                elseif ($p -is [System.Windows.Automation.TogglePattern]) { $p.Toggle(); $acted = $true }
                elseif ($p -is [System.Windows.Automation.ExpandCollapsePattern]) { $p.Expand(); $acted = $true }
            } catch { }
        }
        Start-Sleep -Milliseconds 1200
        $safe = ($c.name -replace "[^\w\-]+", "_")
        if ($safe.Length -gt 40) { $safe = $safe.Substring(0, 40) }
        Save-Shot $win ($OutPng -replace "\.png$", "__$safe.png")
        [System.Windows.Forms.SendKeys]::SendWait("{ESC}")
        Start-Sleep -Milliseconds 300
        Write-Output "ACTED $($c.name)"
    }
}
"""


def ps(script_text):
    path = os.path.join(tempfile.gettempdir(), "uiwalk_uia.ps1")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(script_text)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
         "-File", path],
        capture_output=True, text=True, timeout=120)
    return proc


def launch(command):
    """Launches the app and identifies the window's process by diffing the
    process table: store-app alias stubs exit instantly and the real window
    belongs to a fresh process."""

    def snapshot():
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
        procs = {}
        for line in out.splitlines():
            parts = line.split('","')
            if len(parts) >= 2:
                name = parts[0].strip('"')
                try:
                    procs[int(parts[1].strip('"'))] = name
                except ValueError:
                    pass
        return procs

    before = snapshot()
    subprocess.Popen(command, shell=True)
    time.sleep(4.0)
    after = snapshot()
    new_names = sorted({name for pid, name in after.items()
                        if pid not in before and not name.startswith("uiwalk")})
    pids = [pid for pid in after if pid not in before]
    return pids, new_names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", help="command line to launch")
    ap.add_argument("--attach", help="exact window title to attach to")
    ap.add_argument("--out", default="C:/Temp/uiwalk")
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--blocklist", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.launch and not args.attach:
        ap.error("need --launch or --attach")
    os.makedirs(args.out, exist_ok=True)
    if args.launch:
        pids, proc_names = launch(args.launch)
        pid = pids[0] if pids else 0
    else:
        pid, proc_names = 0, []
    title = args.attach or ""
    n = {"i": 0}

    # The PS template is parameterized via its own param() block; invoke it
    # with -File + named parameters (avoids all shell-quoting pitfalls).
    def call2(mode, target="", index=-1, png="", visited=""):
        path = os.path.join(tempfile.gettempdir(), "uiwalk_uia.ps1")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(PS_TEMPLATE)
        cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy",
               "Bypass", "-File", path, "-Mode", mode, "-OutPng", png,
               "-ProcNames", ",".join(proc_names),
               "-Blocklist",
               args.blocklist or
               "(exit|quit|close|delete|remove|format|erase|uninstall|shutdown|sign out|log ?off|pay|purchase|checkout|save)"]
        if pid:
            cmd += ["-ProcId", str(pid)]
        if title:
            cmd += ["-Title", title]
        if target:
            cmd += ["-Target", target]
        if index >= 0:
            cmd += ["-Index", str(index)]
        if visited:
            cmd += ["-Visited", visited]
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=180)

    # boot shot
    boot_png = os.path.join(args.out, "000_boot.png")
    r = call2("shot", png=boot_png)
    status = "PASS boot" if "SHOT OK" in r.stdout else f"FAIL boot {r.stderr[:150]}"
    print(status, flush=True)

    if args.dry_run:
        r = call2("enum")
        try:
            controls = json.loads(r.stdout) if r.stdout.strip() else []
        except json.JSONDecodeError:
            controls = []
        for c in (controls if isinstance(controls, list) else
                  [controls])[:80]:
            print(f"WOULD CLICK [{c['i']}] {c['type']}: {c['name']}")
        print(f"{len(controls)} clickable controls found (dry run)")
        return

    # 1. menu walk: every top-level menu
    enum = call2("enum")
    try:
        names = [c["name"] for c in json.loads(enum.stdout or "[]")
                 if isinstance(c, dict)]
    except json.JSONDecodeError:
        names = []
    visited = set()
    for name in dict.fromkeys(names):
        png = os.path.join(args.out, f"{n['i']:03d}_menu.png")
        r = call2("menu", target=name, png=png,
                  visited=",".join(sorted(visited)))
        n["i"] += 1
        try:
            results = json.loads(r.stdout) if r.stdout.strip() else []
        except json.JSONDecodeError:
            results = []
        if not isinstance(results, list):
            results = [results]
        for res in results:
            if res.get("status") == "invoked":
                visited.add(res["name"])
            print(f"PASS menu:{name}>{res.get('name')} "
                  f"({res.get('status')})", flush=True)
        if n["i"] >= args.max_steps:
            break

    # 2. controls walk: invoke remaining buttons/toggles by fresh index
    for i in range(args.max_steps):
        png = os.path.join(args.out, f"{n['i']:03d}_control.png")
        r = call2("controls", index=i, png=png,
                  visited=",".join(sorted(visited)))
        out = r.stdout.strip()
        if out.startswith("DONE"):
            break
        n["i"] += 1
        print(f"PASS control[{i}]: {out[:100]}", flush=True)

    print(json.dumps({"steps": n["i"], "out": args.out}), flush=True)


if __name__ == "__main__":
    main()
