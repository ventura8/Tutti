# The Terminal User Interface (TUI)

Tutti provides a keyboard-first terminal user interface (`tutti ui`) designed to the best-practice
UI/UX standards for terminal applications: consistent layout, 80x24 readability, discoverable help, colorblind-safe
status indicators, NO_COLOR and truecolor fallbacks, and screen-reader accessibility.

## Launching the TUI

```bash
tutti ui                # Open the TUI on Machines & Devices
tutti ui needs          # Open on Needs Attention
tutti ui dispatch       # Open on Dispatch & Queue
tutti ui chat           # Open on Chat
tutti ui setup          # Open on Setup Wizard
tutti ui --plain        # Screen-reader friendly plain text mode
tutti ui --no-color     # Force monochrome mode (also honors NO_COLOR=1)
tutti ui --once         # Render snapshot once to stdout and exit (for scripts and tests)
tutti setup --tui       # View the latest setup report
```

## The Five Screens

1. **Machines & Devices** shows configured machines, slot counts and Android devices.
2. **Needs Attention** shows open questions for the current role, with reasons and available answers.
3. **Dispatch & Queue** shows backlog tasks and their recorded status.
4. **Chat & Agents** shows previews of conversations saved by the local dashboard session.
5. **Setup Wizard** shows the latest setup report. Run `tutti setup` to start the guided configuration.

The TUI is a read-only viewer. Use `tutti needs answer`, `tutti setup` and the dashboard chat for those workflows.

## Layout and Navigation

All screens follow a consistent layout:
- **Header**: Active screen title, numbered screen tabs (`[1:Machines] 2:Needs 3:Dispatch 4:Chat 5:Setup`), and `[?] Help`.
- **Top / Bottom Dividers**: Crisp unicode box-drawing dividers (`─`).
- **Body Area**: Scrollable list with a selection marker (`> `). Enter opens a separate detail view.
- **Footer**: Key bindings always visible: `[Tab/1-5] Screens [↑/↓ j/k] Navigate [Enter] View [?] Help [q] Quit`.

### Key Bindings

| Key | Action |
| --- | --- |
| `↑` or `k` | Move focus to previous item |
| `↓` or `j` | Move focus to next item |
| `←` or `h` | Switch to previous screen |
| `→` or `l` | Switch to next screen |
| `Tab` | Cycle forward to next screen |
| `Shift-Tab` | Cycle backward to previous screen |
| `1` .. `5` | Switch directly to screen 1–5 |
| `Enter` or `Space` | Open or close details for the focused item |
| `↑` / `↓` or `k` / `j` | Scroll help and details |
| `?` | Toggle discoverable Help overlay |
| `Esc` | Close help or details, or quit from the item list |
| `q` | Quit from any view |

## Terminal Dimensions and Resizing

- **80x24 Standard**: Each screen shows its tabs, focused row and main bindings. Open details to read long values.
- **Dynamic Resizing**: When resized to smaller (e.g. 60x15) or larger (e.g. 120x40) terminals, layout adjusts automatically,
  condensing tabs and headers on narrow screens and expanding scrollable visible items on taller screens.

## Color and Accessibility

- **Colorblind-Safe Badges**: Status indicators never rely on color alone. Every badge includes both an icon and a text label
  enclosed in brackets:
  - `[✓ Ready]`, `[✓ Done]`, `[✓ Passed]`
  - `[! Needs You]`, `[! Warning]`, `[⏸ Yield]`, `[🔒 Locked]`
  - `[⟳ Running]`, `[⏳ Queued]`
  - `[✗ Error]`, `[✗ Failed]`
  - `[- Idle]`, `[- Skipped]`
- **Color Palettes and Fallbacks**:
  - `NO_COLOR=1` or `TERM=dumb`: Palette colors are disabled. Plain mode also turns off full-screen terminal controls.
  - 16-color: Standard ANSI colors.
  - 256-color: Extended terminal colors.
  - Truecolor: 24-bit RGB colors when the curses terminal description supports them. The full-screen view falls back to 256 or basic colors.
- **Screen-Reader Plain Mode (`--plain`)**:
  - Runs in plain text line-by-line format without alternate screens or cursor hiding.
  - Outputs numbered items (`Item 1 of 4: Configured. Host: local [FOCUSED]`) and explicit detail labels.
  - Type a number to switch screens, `up` or `down` to move, and `enter` to inspect. Submit each key with Enter.
