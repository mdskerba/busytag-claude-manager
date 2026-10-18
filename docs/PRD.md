# PRD: Busy Tag × Claude Code Session Status

## Problem Statement
Claude Code users often run one or more long agent sessions while doing other work. Today, the only way to know whether a session is still working, finished, or blocked on a permission prompt or question is to switch back to the Claude app or terminal. Two costs follow:
- Blocked sessions sit idle for minutes, so parallel agent work loses its value.
- Constant checking interrupts the user's focus.

A Busy Tag on the desk is an always-visible, glanceable surface that can solve this without a context switch.

## Goals
1. **Glanceable status:** the user can tell each active session's state from the device in under 2 seconds, without opening Claude.
2. **Faster response to blocks:** median time from "needs input" to the user's reply drops by at least 50% compared with not using the device (self-measured from hook timestamps).
3. **Low latency:** the device reflects a status change within 2 seconds of the Claude Code event (p95).
4. **Zero-touch operation:** after a one-time setup, no manual steps are needed per session.
5. **Never blocks Claude:** a failure in the integration never slows down or breaks a Claude session.

## Non-Goals
- **claude.ai chat or Cowork conversations:** they have no local event surface. Revisit if an API appears.
- **Replying to Claude from the device:** the Busy Tag has no input. The device only signals.
- **Replacing the Busy Tag app's other integrations (Teams, Calendar):** the two take turns using the device.
- **Remote or multi-machine sessions (cloud, SSH):** v1 covers only local hooks on this Mac.
- **Windows or Linux support in v1:** the design stays portable, but v1 targets macOS only.

## Personas
- **Parallel agent user:** runs 2–5 Claude Code sessions at once and works in other apps meanwhile.
- **Focused single-session user:** runs one long task and wants to stay out of the Claude window until it's needed.

## User Stories
1. As a parallel agent user, I want to see each active session's short name and status on the Busy Tag, so I know which agents are working without opening Claude.
2. As any user, I want the LEDs to flash 3 times when a session needs my input, so I notice even when I'm not looking at the screen.
3. As any user, I want to turn the 3× flash on or off and choose its color, so it fits my environment, for example during meetings.
4. As a parallel agent user, I want a session that needs input to stay pinned on the screen until it's resolved, so a blocked agent is never hidden behind the rotation.
5. As any user, I want the LED color to reflect the overall state (working, needs input, all done, error), so I can read status from peripheral vision.
6. As any user, I want finished or closed sessions to disappear from the device on their own, so the display doesn't go stale.
7. As any user, I want the device to show an "idle" screen when no sessions are active, so I know there's nothing to watch.
8. **Edge case:** as any user, if the Busy Tag is unplugged or the port is busy, I want Claude to keep working normally, and the integration to reconnect when the device returns.
9. **Edge case:** as any user, if several sessions need input at once, I want to see how many are waiting and cycle through only those.

## Status Model
| Status | Triggering Claude Code hook | Screen | LEDs |
|---|---|---|---|
| Working | `UserPromptSubmit`, `PreToolUse`/`PostToolUse` | Name + "Working" + spinner/progress glyph | Solid blue |
| Needs input | `PermissionRequest` (immediate; includes questions to the user), `Notification` type `permission_prompt` | Name + "Needs input" + attention icon | 3× flash in amber (if enabled), then solid amber |
| Done / idle | `Stop` | Name + "Done" + check | Solid green |
| Error / stopped | `StopFailure` (payload has an `error` code) | Name + "Error" | Solid red |
| *(removed)* | `SessionEnd` | Session leaves the rotation | n/a |
| No sessions | n/a | "No active Claude sessions" | LEDs off or dim |

**Aggregate LED priority:** Needs input > Error > Working > Done.

## Requirements

### P0: Must-have
1. **Hook capture**
   - A Claude Code hook script is registered in `~/.claude/settings.json` for SessionStart, UserPromptSubmit, Pre/PostToolUse, Notification, PermissionRequest, Stop, and SessionEnd.
   - It sends `{session_id, event, cwd, timestamp}` to a local daemon and exits.
   - **Acceptance:**
     - The hook returns in under 100 ms.
     - The hook exits 0 even when the daemon is down.
     - The hook never prints output that changes Claude's behavior.
2. **Local daemon (device owner)**
   - A background process (launchd agent) exclusively owns the Busy Tag serial port at 460800 baud.
   - It keeps session state in memory and auto-detects the device by VID:PID 303A:81DF.
   - **Acceptance:**
     - The daemon reconnects within 5 s after the device is replugged.
     - It drops sessions that have had no events for 30 min.
3. **Short session name**
   - Resolved in this order: Claude session title, if obtainable locally; otherwise the project folder name from `cwd`, plus `#n` when it's ambiguous.
   - Shown at up to 16 characters, with an ellipsis when truncated.
   - **Acceptance:** two sessions in the same folder show distinct names.
4. **Screen rendering**
   - Render a PNG at the device's native resolution (240×280, confirmed). It shows the name, the status label, a status icon, and an "n waiting" counter.
   - Upload with `AT+UF`, then display with `AT+SP`.
   - Write to two files in turn (`cc_status_a.png` / `cc_status_b.png`). The image on screen is locked and can't be overwritten.
   - **Acceptance:** text is readable from 1 m away, and an update completes in 2 s or less.
5. **Priority + rotation**
   - Sessions that need input are pinned. If several are waiting, rotate among only those.
   - Otherwise rotate through all active sessions every N seconds (default 5).
   - **Acceptance:** a session that needs input appears on screen within 2 s, whatever the rotation position.
6. **3× flash on needs input (configurable)**
   - On entering "needs input", define a custom on/off pattern (`AT+CP`, steps sent back to back) and play it 3 times (`AT+PP=1,3`). Switch to solid amber on `+evn:PP,0`.
   - Settings: `flash_on_input` (default true) and the flash color.
   - **Acceptance:**
     - The flash fires once per transition into "needs input", not on each rotation.
     - When disabled, the LEDs go to solid amber without flashing.
7. **Aggregate LED color** according to the priority rule above.
8. **Coexistence with the Busy Tag app**
   - On startup, if the port is busy, the daemon logs a clear message ("Quit the Busy Tag app to enable Claude status") and retries every 10 s.
   - **Acceptance:** no crash, and no effect on Claude sessions.
9. **Config file** (`~/.config/busytag-claude/config.json`): rotation interval, flash on/off, colors, brightness, device port override.

### P1: Nice-to-have
- **Auto-handback (done 2026-10-16):**
  - After 3 min with no session working or needing input, the daemon releases the port and reopens the Busy Tag app.
  - When a session starts working or needs input, the daemon quits the app and takes the port back. The flash is kept through the takeover.
- Elapsed-time display, such as "Working · 4m" or "Waiting · 1m".
- A "Done" screen that lingers for 60 s, then falls back to the next session or the idle screen.
- Repeat the flash reminder every 5 min while input is still pending (configurable).
- A menu-bar status icon and a pause toggle (for example, during meetings).
- A one-command installer that registers the hooks, the launchd agent, and the config.

### P2: Future
- claude.ai, Cowork, and cloud sessions, if a status API becomes available.
- Wi-Fi local-server transport, so the Busy Tag app can keep USB.
- Windows support (COM port; the same hook model).
- Multiple Busy Tags, one per session.
- Publishing as an official Busy Tag "custom integration" or Claude Code plugin.

## Proposed Architecture (for engineering)
```
Claude Code session ──hook (stdin JSON)──► busytag-hook (tiny script, fire-and-forget)
                                               │ HTTP POST localhost:<port> / unix socket
                                               ▼
                                 busytag-claude daemon (launchd)
                                 ├─ session store + priority/rotation
                                 ├─ PNG renderer (fonts, icons)
                                 └─ serial driver (AT+UF / AT+SP / AT+SC / AT+CP / AT+PP)
                                               ▼
                                         Busy Tag (USB CDC)
```
- **Stack:** Python with pyserial and Pillow. This matches the vendor's own example integrations; C#/BusyTag.Lib is the alternative.
- **Sources:** hook input fields come from the Claude Code hooks docs. Device commands come from the Busy Tag AT Command Manual FW 2.0.

## Success Metrics
**Leading (first 2 weeks):**
- Status-change-to-display latency p95 ≤ 2 s.
- Zero instances of Claude sessions slowed down or broken by the hook.
- Daemon uptime ≥ 99% while the device is connected.

**Lagging (1 month):**
- Median wait between needs-input and the user's reply drops ≥ 50% against a baseline week. Measured from Notification → UserPromptSubmit timestamps in the daemon log.
- The user keeps it enabled on ≥ 80% of workdays.

## Open Questions
1. ~~**[Engineering, blocking]** Can the desktop app's session title be read locally?~~ **Resolved (spike):** yes. Read the last `custom-title` line in `transcript_path`, with the folder name as fallback.
2. ~~**[Engineering, blocking]** Which hook signals "error/stopped"?~~ **Resolved (spike):** `StopFailure` fires with an `error` code. PermissionRequest fires immediately for permission prompts *and* questions (`tool_name: AskUserQuestion`). Notification `idle_prompt` comes about 60 s after Stop. Original question: Which hook signals "error/stopped"? Check whether `StopFailure` exists in the installed Claude Code version, and how interrupts appear.
3. ~~**[Engineering]** Display resolution?~~ **Resolved (spike):** 240 × 280. Upload is about 1.0 s and show is about 0.1 s for a ~9 KB PNG.
4. **[Engineering]** What is the flash-storage wear budget for repeated `AT+UF` overwrites? Alternative: pre-render one image per status and overlay text only when needed.
5. **[Stakeholder/user]** When no sessions are active, should the daemon release the device and relaunch the Busy Tag app? This is P1 auto-handback.
6. **[Design]** Final colors and icons, and whether to use a GIF spinner for "Working" or a static image.

## Timeline / Phasing
- **Phase 0, spike (½ day):**
  - Talk to the device directly: upload a PNG, show it, play a 3× flash.
  - Confirm the resolution and hook payloads.
- **Phase 1, MVP (1–2 days):** P0 requirements 1–9.
- **Phase 2 (1–2 days):** P1 items, especially auto-handback and the installer.
- **Dependency:** the Busy Tag app must be quit while the integration owns USB.
- **Phase 0 done (2026-10-16):** see `spike/FINDINGS.md`. Note the BusyTag app has its own Claude Code *usage* integration, which competes for the device.
