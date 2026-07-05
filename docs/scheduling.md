# Scheduling `finance refresh`

`finance refresh` is idempotent and scheduler-safe: it soft-skips on any provider error
and always exits 0, so a missed or partially-failed run never wedges the schedule.

The deploy target is a **Windows PC at home**, so Windows Task Scheduler is the primary
path. macOS launchd and a cron one-liner follow for other hosts. Replace every
`<...>` placeholder with a real absolute path — none are hard-coded here.

## Windows Task Scheduler (primary)

Key requirement: **catch-up semantics** — if the PC was off when a run was due, run it as
soon as the PC is back online. That is `StartWhenAvailable = true`.

### Option A: `schtasks` one-liner

```bat
schtasks /Create /TN "bankapp refresh" /SC DAILY /ST 08:00 ^
  /TR "<venv>\Scripts\finance.exe refresh >> <logdir>\refresh.log 2>&1" ^
  /RL LIMITED /F
```

`schtasks` cannot set `StartWhenAvailable` directly, so after creating the task open Task
Scheduler and tick **"Run task as soon as possible after a scheduled start is missed"**,
or import the XML below (which sets it).

### Option B: import task XML (sets StartWhenAvailable)

Save as `bankapp-refresh.xml`, edit the two placeholders, then
`schtasks /Create /TN "bankapp refresh" /XML bankapp-refresh.xml /F`.

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T08:00:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
      <Repetition>
        <Interval>PT6H</Interval>            <!-- a few times a day -->
        <Duration>P1D</Duration>
      </Repetition>
      <Enabled>true</Enabled>
    </CalendarTrigger>
  </Triggers>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>   <!-- catch up missed runs -->
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>&lt;venv&gt;\Scripts\finance.exe</Command>
      <Arguments>refresh</Arguments>
    </Exec>
  </Actions>
</Task>
```

(Logging: redirect via a wrapper `.bat` — `<venv>\Scripts\finance.exe refresh >> <logdir>\refresh.log 2>&1` — since Task XML `<Exec>` has no shell redirection.)

## macOS launchd (secondary)

`~/Library/LaunchAgents/com.bankapp.refresh.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.bankapp.refresh</string>
  <key>ProgramArguments</key>
  <array>
    <string><venv>/bin/finance</string>
    <string>refresh</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>RunAtLoad</key><true/>            <!-- launchd's own catch-up on load -->
  <key>StandardOutPath</key><string><logdir>/refresh.log</string>
  <key>StandardErrorPath</key><string><logdir>/refresh.err</string>
</dict></plist>
```
`launchctl load ~/Library/LaunchAgents/com.bankapp.refresh.plist`

## cron (other hosts)

```cron
0 8,18 * * *  <venv>/bin/finance refresh >> <logdir>/refresh.log 2>&1
```
(Plain cron has no missed-run catch-up; prefer the platform schedulers above where that matters.)

## Optional: weekly advisor digest (Phase 10)

Once the advisor skill exists you can run it headless (subscription-billed, never the API):

```
<claude-cli> -p "/advisor"     # weekly, via the same scheduler mechanism
```
