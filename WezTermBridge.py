import glob
import json
import os
import subprocess
from urllib.parse import unquote, urlparse

import sublime
import sublime_plugin


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Cached live WezTerm GUI socket.
_wezterm_socket = None

# Memory-only "last selected pane".
# This intentionally does NOT persist across Sublime restarts.
# Before reusing it, we re-fetch the current pane list and verify identity
# metadata so a recycled pane_id after a WezTerm restart cannot misroute text.
_last_pane = None


def _wezterm_executable():
    return "wezterm.exe" if os.name == "nt" else "wezterm"


def _base_env(socket_path=None):
    env = os.environ.copy()
    env.pop("WEZTERM_PANE", None)

    if socket_path:
        env["WEZTERM_UNIX_SOCKET"] = socket_path
    else:
        env.pop("WEZTERM_UNIX_SOCKET", None)

    return env


def _run_process(args, input_text=None, env=None):
    return subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        creationflags=CREATE_NO_WINDOW,
        env=env,
    )


def _socket_candidates():
    candidates = []

    inherited = os.environ.get("WEZTERM_UNIX_SOCKET")
    if inherited:
        candidates.append(inherited)

    socket_dir = os.path.join(
        os.path.expanduser("~"),
        ".local",
        "share",
        "wezterm",
    )

    discovered = glob.glob(os.path.join(socket_dir, "gui-sock-*"))

    def mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    discovered.sort(key=mtime, reverse=True)

    for path in discovered:
        if path not in candidates:
            candidates.append(path)

    return candidates


def find_wezterm_socket(force=False):
    global _wezterm_socket

    if _wezterm_socket and not force:
        return _wezterm_socket

    exe = _wezterm_executable()

    for socket_path in _socket_candidates():
        try:
            result = _run_process(
                [exe, "cli", "list", "--format", "json"],
                env=_base_env(socket_path),
            )

            panes = json.loads(result.stdout)
            if isinstance(panes, list):
                _wezterm_socket = socket_path
                return socket_path

        except (subprocess.CalledProcessError, OSError, ValueError):
            continue

    _wezterm_socket = None
    return None


def run_wezterm(args, input_text=None, retry=True):
    global _wezterm_socket

    socket_path = find_wezterm_socket()
    if not socket_path:
        raise RuntimeError(
            "실행 중인 WezTerm GUI 소켓을 찾지 못했습니다.\n"
            "WezTerm이 실행 중인지 확인하세요."
        )

    cmd = [_wezterm_executable()] + list(args)

    try:
        return _run_process(
            cmd,
            input_text=input_text,
            env=_base_env(socket_path),
        )

    except subprocess.CalledProcessError:
        # WezTerm may have restarted and received a new gui-sock number.
        _wezterm_socket = None

        if retry:
            new_socket = find_wezterm_socket(force=True)
            if new_socket:
                return _run_process(
                    cmd,
                    input_text=input_text,
                    env=_base_env(new_socket),
                )

        raise


def get_wezterm_panes(show_errors=True):
    try:
        result = run_wezterm(
            ["cli", "list", "--format", "json"]
        )

        panes = json.loads(result.stdout)

        if not isinstance(panes, list):
            raise ValueError("Unexpected WezTerm response")

        return panes

    except FileNotFoundError:
        if show_errors:
            sublime.error_message(
                "WezTerm Bridge\n\n"
                "wezterm.exe를 찾을 수 없습니다.\n"
                "WezTerm이 PATH에 등록되어 있는지 확인하세요."
            )

    except subprocess.CalledProcessError as exc:
        if show_errors:
            message = exc.stderr or exc.stdout or str(exc)
            sublime.error_message(
                "WezTerm Bridge\n\n"
                "WezTerm 세션 목록을 가져오지 못했습니다.\n\n"
                + message
            )

    except (json.JSONDecodeError, ValueError) as exc:
        if show_errors:
            sublime.error_message(
                "WezTerm Bridge\n\n"
                "WezTerm의 JSON 응답을 해석하지 못했습니다.\n\n"
                + str(exc)
            )

    except RuntimeError as exc:
        if show_errors:
            sublime.error_message(
                "WezTerm Bridge\n\n" + str(exc)
            )

    return []


def cwd_for_display(cwd):
    if not cwd:
        return ""

    try:
        parsed = urlparse(cwd)

        if parsed.scheme == "file":
            path = unquote(parsed.path)

            if (
                os.name == "nt"
                and len(path) >= 3
                and path[0] == "/"
                and path[2] == ":"
            ):
                path = path[1:]

            return path.replace("/", "\\")

    except Exception:
        pass

    return cwd


def process_for_display(pane):
    value = (
        pane.get("foreground_process_name")
        or pane.get("foreground_process")
        or pane.get("process_name")
        or ""
    )

    if not value:
        return ""

    return os.path.basename(str(value).replace("\\", "/"))


def pane_primary_label(pane):
    title = (pane.get("title") or "").strip()
    process = process_for_display(pane)

    generic_titles = {
        "",
        "windows powershell",
        "powershell",
        "powershell.exe",
        "cmd",
        "cmd.exe",
        "wezterm.exe",
    }

    if title.lower() in generic_titles and process:
        return "{}  —  {}".format(title or "(제목 없음)", process)

    return title or process or "(제목 없음)"


def pane_secondary_label(pane):
    details = []

    pane_id = pane.get("pane_id")
    if pane_id is not None:
        details.append("Pane {}".format(pane_id))

    cwd = cwd_for_display(pane.get("cwd") or "")
    if cwd:
        details.append(cwd)

    workspace = pane.get("workspace")
    if workspace and workspace != "default":
        details.append("Workspace: {}".format(workspace))

    tab_id = pane.get("tab_id")
    if tab_id is not None:
        details.append("Tab {}".format(tab_id))

    return "  ·  ".join(details)


def pane_identity(pane):
    """
    Metadata used to verify that a remembered pane_id still refers to the
    same logical pane.

    pane_id alone is NOT sufficient because WezTerm may reuse small IDs after
    a restart. We therefore require stable metadata to match as well.
    """
    return {
        "pane_id": pane.get("pane_id"),
        "title": (pane.get("title") or "").strip(),
        "cwd": pane.get("cwd") or "",
        "process": process_for_display(pane),
        "workspace": pane.get("workspace") or "",
    }


def remember_pane(pane):
    global _last_pane
    _last_pane = pane_identity(pane)


def clear_last_pane():
    global _last_pane
    _last_pane = None


def find_verified_last_pane(panes):
    """
    Return the current pane matching the remembered pane only if both pane_id
    and identity metadata still match.

    This prevents a recycled pane_id after WezTerm restart from receiving a
    prompt intended for an old session.
    """
    if not _last_pane:
        return None

    for pane in panes:
        current = pane_identity(pane)

        if current["pane_id"] != _last_pane.get("pane_id"):
            continue

        if (
            current["title"] == _last_pane.get("title")
            and current["cwd"] == _last_pane.get("cwd")
            and current["process"] == _last_pane.get("process")
            and current["workspace"] == _last_pane.get("workspace")
        ):
            return pane

    return None


def get_prompt_text(view):
    if view is None:
        return ""

    selections = [
        view.substr(region)
        for region in view.sel()
        if not region.empty()
    ]

    if selections:
        return "\n".join(selections)

    return view.substr(sublime.Region(0, view.size()))


def send_text_to_pane(pane_id, text, submit=True):
    try:
        # Prompt body: use bracketed paste for multiline safety.
        run_wezterm(
            [
                "cli",
                "send-text",
                "--pane-id",
                str(pane_id),
            ],
            input_text=text,
        )

        if submit:
            # Send a raw Enter separately, outside bracketed paste.
            run_wezterm(
                [
                    "cli",
                    "send-text",
                    "--pane-id",
                    str(pane_id),
                    "--no-paste",
                ],
                input_text="\r",
            )

        return True

    except FileNotFoundError:
        sublime.error_message(
            "WezTerm Bridge\n\n"
            "wezterm.exe를 찾을 수 없습니다."
        )

    except subprocess.CalledProcessError as exc:
        message = exc.stderr or exc.stdout or str(exc)
        sublime.error_message(
            "WezTerm Bridge\n\n"
            "프롬프트 전송에 실패했습니다.\n\n"
            + message
        )

    except RuntimeError as exc:
        sublime.error_message(
            "WezTerm Bridge\n\n" + str(exc)
        )

    return False


class _PanePickerMixin:
    def _show_pane_picker(self, prompt_text, submit=True):
        self.prompt_text = prompt_text
        self.submit = submit
        self.panes = get_wezterm_panes()

        if not self.panes:
            sublime.status_message(
                "WezTerm Bridge: 실행 중인 pane을 찾지 못했습니다."
            )
            return

        items = [
            [
                pane_primary_label(pane),
                pane_secondary_label(pane),
            ]
            for pane in self.panes
        ]

        self.window.show_quick_panel(
            items,
            self._on_pane_selected,
        )

    def _on_pane_selected(self, index):
        if index < 0:
            return

        pane = self.panes[index]
        pane_id = pane.get("pane_id")

        if pane_id is None:
            sublime.error_message(
                "WezTerm Bridge\n\n"
                "선택한 pane의 ID를 확인할 수 없습니다."
            )
            return

        if send_text_to_pane(
            pane_id,
            self.prompt_text,
            submit=self.submit,
        ):
            remember_pane(pane)

            suffix = "" if self.submit else " (전송만)"
            sublime.status_message(
                "WezTerm Bridge → {}{}".format(
                    pane_primary_label(pane),
                    suffix,
                )
            )


class WeztermBridgeSendPromptCommand(
    _PanePickerMixin,
    sublime_plugin.WindowCommand,
):
    """
    Ctrl+Enter:
      pane 목록 -> 선택 -> 전송 + Enter

    Ctrl+Alt+Enter:
      pane 목록 -> 선택 -> 전송만 (submit=false)
    """

    def run(self, submit=True):
        view = self.window.active_view()

        if view is None:
            sublime.status_message(
                "WezTerm Bridge: 활성 문서가 없습니다."
            )
            return

        prompt_text = get_prompt_text(view)

        if not prompt_text.strip():
            sublime.status_message(
                "WezTerm Bridge: 전송할 텍스트가 없습니다."
            )
            return

        self._show_pane_picker(
            prompt_text,
            submit=bool(submit),
        )


class WeztermBridgeSendToLastCommand(
    _PanePickerMixin,
    sublime_plugin.WindowCommand,
):
    """
    Ctrl+Shift+Enter:
      - Re-fetch the current pane list.
      - Reuse the remembered pane ONLY if pane_id + metadata still match.
      - Otherwise clear the stale target and fall back to the pane picker.
    """

    def run(self):
        view = self.window.active_view()

        if view is None:
            sublime.status_message(
                "WezTerm Bridge: 활성 문서가 없습니다."
            )
            return

        prompt_text = get_prompt_text(view)

        if not prompt_text.strip():
            sublime.status_message(
                "WezTerm Bridge: 전송할 텍스트가 없습니다."
            )
            return

        panes = get_wezterm_panes()
        if not panes:
            return

        pane = find_verified_last_pane(panes)

        if pane is None:
            clear_last_pane()
            sublime.status_message(
                "WezTerm Bridge: 마지막 세션이 없거나 변경되어 다시 선택합니다."
            )
            self._show_pane_picker(prompt_text, submit=True)
            return

        pane_id = pane.get("pane_id")

        if send_text_to_pane(
            pane_id,
            prompt_text,
            submit=True,
        ):
            # Refresh remembered metadata from the current pane snapshot.
            remember_pane(pane)
            sublime.status_message(
                "WezTerm Bridge → {}".format(
                    pane_primary_label(pane)
                )
            )


class WeztermBridgeChoosePaneCommand(sublime_plugin.WindowCommand):
    """
    Helper/debug command. Ctrl+Enter already doubles as a pane-list viewer
    because Esc cancels without sending.
    """

    def run(self):
        self.panes = get_wezterm_panes()

        if not self.panes:
            return

        items = [
            [
                pane_primary_label(pane),
                pane_secondary_label(pane),
            ]
            for pane in self.panes
        ]

        self.window.show_quick_panel(
            items,
            self.on_selected,
        )

    def on_selected(self, index):
        if index < 0:
            return

        pane = self.panes[index]
        sublime.status_message(
            "선택됨: {} ({})".format(
                pane_primary_label(pane),
                pane_secondary_label(pane),
            )
        )


class WeztermBridgeForgetLastCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        clear_last_pane()
        sublime.status_message(
            "WezTerm Bridge: 마지막 세션 기억을 지웠습니다."
        )


class WeztermBridgeTestCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        import sys

        socket_path = find_wezterm_socket()

        message = [
            "WezTerm Bridge가 정상적으로 로드되었습니다.",
            "",
            "Python {}".format(sys.version.split()[0]),
            "Socket: {}".format(socket_path or "(찾지 못함)"),
        ]

        if _last_pane:
            message.extend([
                "",
                "Last pane:",
                "{}".format(_last_pane),
            ])

        sublime.message_dialog("\n".join(message))
