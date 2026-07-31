# Sublime WezTerm Bridge

Send text from Sublime Text to terminal sessions running in WezTerm.

Works with terminal-based agent CLIs such as Claude Code, Codex CLI, Kiro CLI,
and other TUIs that accept pasted text followed by Enter.

## Requirements

- Windows
- Sublime Text 4
- WezTerm
- `wezterm.exe` available in PATH
- Sublime plugin host Python 3.8 (`.python-version` containing `3.8`)

## Shortcuts

- `Ctrl+Enter`
  - Show the current WezTerm pane list
  - Select a pane
  - Send selected text, or the whole document if nothing is selected
  - Press Enter automatically

- `Ctrl+Shift+Enter`
  - Send directly to the pane selected most recently during the current
    Sublime session
  - Before sending, the plugin re-fetches the current pane list and verifies
    pane ID plus title/CWD/process/workspace metadata
  - If WezTerm restarted or the pane no longer matches, the remembered target
    is discarded and the pane picker is shown instead

- `Ctrl+Alt+Enter`
  - Show the pane list
  - Send text to the selected pane
  - Do **not** press Enter

`Ctrl+Enter` can also be used just to inspect the pane list: press `Esc` to
cancel without sending anything.

## Safety of "last pane"

The last pane is kept in memory only; it is not written to disk.

WezTerm can reuse pane IDs after a restart, so the plugin never trusts the
remembered pane ID by itself. It checks current metadata before sending. If the
identity no longer matches, it asks you to select a pane again.

## WezTerm socket handling on Windows

Some stable WezTerm builds can fail to auto-discover the correct GUI socket
when `wezterm cli` is launched from an external application such as Sublime.

This plugin scans `%USERPROFILE%\.local\share\wezterm\gui-sock-*`, verifies a
live socket with `wezterm cli list`, caches it, and automatically rediscovers
it after a WezTerm restart.
