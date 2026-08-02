import glob
import json
import os
import subprocess
from urllib.parse import unquote, urlparse

import sublime
import sublime_plugin


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PROCESS_TIMEOUT_SECONDS = 2.0

# Cached live WezTerm GUI socket.
_wezterm_socket = None

# Memory-only "last selected pane".
# This intentionally does NOT persist across Sublime restarts.
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
    """
    Run a WezTerm CLI command.

    All callers must invoke this from Sublime's async worker thread. The
    timeout prevents a stale WezTerm socket from blocking the plugin host
    indefinitely.
    """
    return subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
        creationflags=CREATE_NO_WINDOW,
        env=env,
    )


def _run_async(worker, on_success=None, on_error=None):
    """
    Execute blocking work on Sublime's worker thread and marshal callbacks
    back to the main thread.
    """
    def task():
        try:
            result = worker()
        except Exception as exc:
            if on_error:
                sublime.set_timeout(lambda: on_error(exc), 0)
            return

        if on_success:
            sublime.set_timeout(lambda: on_success(result), 0)

    sublime.set_timeout_async(task, 0)


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

        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
        ):
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

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
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


def get_wezterm_panes():
    result = run_wezterm(
        ["cli", "list", "--format", "json"]
    )

    panes = json.loads(result.stdout)

    if not isinstance(panes, list):
        raise ValueError("Unexpected WezTerm response")

    return panes


def _error_message_for_exception(exc, action):
    if isinstance(exc, FileNotFoundError):
        return (
            "WezTerm Bridge\n\n"
            "wezterm.exe를 찾을 수 없습니다.\n"
            "WezTerm이 PATH에 등록되어 있는지 확인하세요."
        )

    if isinstance(exc, subprocess.TimeoutExpired):
        return (
            "WezTerm Bridge\n\n"
            "WezTerm이 응답하지 않아 작업을 중단했습니다.\n"
            "WezTerm이 실행 중인지 확인하세요."
        )

    if isinstance(exc, subprocess.CalledProcessError):
        details = exc.stderr or exc.stdout or str(exc)
        return "WezTerm Bridge\n\n{}\n\n{}".format(action, details)

    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return (
            "WezTerm Bridge\n\n"
            "WezTerm의 JSON 응답을 해석하지 못했습니다.\n\n"
            + str(exc)
        )

    if isinstance(exc, RuntimeError):
        return "WezTerm Bridge\n\n" + str(exc)

    return "WezTerm Bridge\n\n{}\n\n{}".format(action, str(exc))


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


class _PanePickerMixin:
    def _load_and_show_pane_picker(self, prompt_text, submit=True):
        self.prompt_text = prompt_text
        self.submit = submit

        sublime.status_message("WezTerm Bridge: pane 목록을 불러오는 중...")

        _run_async(
            get_wezterm_panes,
            on_success=self._show_pane_picker,
            on_error=self._on_load_panes_error,
        )

    def _on_load_panes_error(self, exc):
        sublime.error_message(
            _error_message_for_exception(
                exc,
                "WezTerm 세션 목록을 가져오지 못했습니다.",
            )
        )

    def _show_pane_picker(self, panes):
        self.panes = panes

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

        sublime.status_message("WezTerm Bridge: 프롬프트를 전송하는 중...")

        _run_async(
            lambda: send_text_to_pane(
                pane_id,
                self.prompt_text,
                submit=self.submit,
            ),
            on_success=lambda _: self._on_send_success(pane),
            on_error=self._on_send_error,
        )

    def _on_send_success(self, pane):
        remember_pane(pane)

        suffix = "" if self.submit else " (전송만)"
        sublime.status_message(
            "WezTerm Bridge → {}{}".format(
                pane_primary_label(pane),
                suffix,
            )
        )

    def _on_send_error(self, exc):
        sublime.error_message(
            _error_message_for_exception(
                exc,
                "프롬프트 전송에 실패했습니다.",
            )
        )


class WeztermBridgeSendPromptCommand(
    _PanePickerMixin,
    sublime_plugin.WindowCommand,
):
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

        self._load_and_show_pane_picker(
            prompt_text,
            submit=bool(submit),
        )


class WeztermBridgeSendToLastCommand(
    _PanePickerMixin,
    sublime_plugin.WindowCommand,
):
    def run(self):
        view = self.window.active_view()

        if view is None:
            sublime.status_message(
                "WezTerm Bridge: 활성 문서가 없습니다."
            )
            return

        self.prompt_text = get_prompt_text(view)
        self.submit = True

        if not self.prompt_text.strip():
            sublime.status_message(
                "WezTerm Bridge: 전송할 텍스트가 없습니다."
            )
            return

        sublime.status_message("WezTerm Bridge: 마지막 pane을 확인하는 중...")

        _run_async(
            get_wezterm_panes,
            on_success=self._on_last_panes_loaded,
            on_error=self._on_load_panes_error,
        )

    def _on_last_panes_loaded(self, panes):
        if not panes:
            sublime.status_message(
                "WezTerm Bridge: 실행 중인 pane을 찾지 못했습니다."
            )
            return

        pane = find_verified_last_pane(panes)

        if pane is None:
            clear_last_pane()
            sublime.status_message(
                "WezTerm Bridge: 마지막 세션이 없거나 변경되어 다시 선택합니다."
            )
            self._show_pane_picker(panes)
            return

        pane_id = pane.get("pane_id")
        if pane_id is None:
            clear_last_pane()
            self._show_pane_picker(panes)
            return

        sublime.status_message("WezTerm Bridge: 프롬프트를 전송하는 중...")

        _run_async(
            lambda: send_text_to_pane(
                pane_id,
                self.prompt_text,
                submit=True,
            ),
            on_success=lambda _: self._on_send_success(pane),
            on_error=self._on_send_error,
        )


class WeztermBridgeChoosePaneCommand(sublime_plugin.WindowCommand):
    def run(self):
        sublime.status_message("WezTerm Bridge: pane 목록을 불러오는 중...")

        _run_async(
            get_wezterm_panes,
            on_success=self._show_panes,
            on_error=self._on_error,
        )

    def _show_panes(self, panes):
        self.panes = panes

        if not panes:
            sublime.status_message(
                "WezTerm Bridge: 실행 중인 pane을 찾지 못했습니다."
            )
            return

        items = [
            [
                pane_primary_label(pane),
                pane_secondary_label(pane),
            ]
            for pane in panes
        ]

        self.window.show_quick_panel(
            items,
            self.on_selected,
        )

    def _on_error(self, exc):
        sublime.error_message(
            _error_message_for_exception(
                exc,
                "WezTerm 세션 목록을 가져오지 못했습니다.",
            )
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
        sublime.status_message("WezTerm Bridge: 연결을 확인하는 중...")

        _run_async(
            self._build_test_message,
            on_success=sublime.message_dialog,
            on_error=self._on_error,
        )

    def _build_test_message(self):
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

        return "\n".join(message)

    def _on_error(self, exc):
        sublime.error_message(
            _error_message_for_exception(
                exc,
                "WezTerm 연결 확인에 실패했습니다.",
            )
        )
