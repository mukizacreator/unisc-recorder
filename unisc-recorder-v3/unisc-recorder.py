#!/usr/bin/python3
"""Compact GTK3 X11 screen recorder using FFmpeg (no ScreenCast portal)."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

APP = "Unisc Recorder"
CONFIG = Path.home() / ".config/unisc-recorder/settings.json"
DEFAULTS = {"mode": "screen", "format": "mp4", "fps": "30", "delay": "0",
            "folder": str(Path.home() / "Videos" / "Screen Recordings"),
            "system_audio": False, "microphone": False, "cursor": True, "monitor": "all",
            "system_source": "default", "microphone_source": "default", "microphone_port": "default"}
FORMATS = {"webm": ".webm", "mp4": ".mp4", "gif": ".gif", "mkv": ".mkv"}
MICROPHONE_FILTERS = (
    "highpass=f=85:p=2",
    "afftdn=nr=12:nf=-35:tn=1:gs=8",
    "equalizer=f=180:t=q:w=0.8:g=-3",
    "equalizer=f=260:t=q:w=1:g=-1.5",
    "equalizer=f=2800:t=q:w=1:g=2",
)


def run_output(args, timeout=5):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL,
                                       timeout=timeout).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def load_settings(path=CONFIG):
    data = dict(DEFAULTS)
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in data})
    except (OSError, ValueError, TypeError):
        pass
    if data["mode"] not in ("screen", "region", "window"): data["mode"] = DEFAULTS["mode"]
    if data["format"] not in FORMATS: data["format"] = DEFAULTS["format"]
    if str(data["fps"]) not in ("15", "24", "30", "60"): data["fps"] = DEFAULTS["fps"]
    if str(data["delay"]) not in ("0", "3", "5", "10"): data["delay"] = DEFAULTS["delay"]
    for key in ("system_audio", "microphone", "cursor"):
        data[key] = data[key] if isinstance(data[key], bool) else DEFAULTS[key]
    for key in ("system_source", "microphone_source", "microphone_port"):
        if not isinstance(data[key], str) or len(data[key]) > 512:
            data[key] = DEFAULTS[key]
    if not isinstance(data["folder"], str) or not data["folder"]:
        data["folder"] = DEFAULTS["folder"]
    return data


def save_settings(data, path=CONFIG):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)


def screen_geometry():
    m = re.search(r"dimensions:\s*(\d+)x(\d+)\s*pixels", run_output(["xdpyinfo"]))
    if not m: raise RuntimeError("Could not read the X11 desktop size (xdpyinfo failed).")
    return 0, 0, int(m[1]), int(m[2])


def list_monitors():
    found = []
    for line in run_output(["xrandr", "--query"]).splitlines():
        m = re.match(r"^(\S+)\s+connected(?:\s+primary)?\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
        if m:
            name, w, h, x, y = m.groups()
            found.append((name, int(x), int(y), int(w), int(h)))
    return found


def active_window_details():
    wid = run_output(["xdotool", "getactivewindow"])
    if not wid: raise RuntimeError("No active X11 window was found. Focus the window you want to record.")
    if not run_output(["xwininfo", "-id", wid]): raise RuntimeError("The selected window no longer exists.")
    title = run_output(["xdotool", "getwindowname", wid])
    if title == APP:
        raise RuntimeError("The recorder itself became the active window. Select Active Window again, then focus the app to record.")
    # xdotool reports client geometry on X11; this captures the application surface.
    text = run_output(["xdotool", "getwindowgeometry", "--shell", wid])
    vals = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    try: x, y, w, h = (int(vals[k]) for k in ("X", "Y", "WIDTH", "HEIGHT"))
    except (KeyError, ValueError): raise RuntimeError("Could not read the active window geometry.")
    if w < 80 or h < 60: raise RuntimeError("The active window is too small to record.")
    return wid, (x, y, w, h)


def active_window_geometry():
    """Compatibility helper for callers that only need the sampled rectangle."""
    return active_window_details()[1]


def pulse_source_info():
    raw = run_output(["pactl", "-f", "json", "list", "sources"])
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        entries = []
    return entries if isinstance(entries, list) else []


def list_audio_sources(kind):
    """Return (source-name, description) pairs from the PulseAudio API."""
    result = []
    entries = pulse_source_info()
    for source in entries if isinstance(entries, list) else []:
        if not isinstance(source, dict):
            continue
        name = source.get("name")
        if not isinstance(name, str) or not name:
            continue
        is_monitor = name.endswith(".monitor") or source.get("monitor_of_sink") not in (None, "", 0, "0")
        if (kind == "system") != is_monitor:
            continue
        description = source.get("description")
        result.append((name, description if isinstance(description, str) and description else name))
    if result:
        return result
    # Compatibility fallback for older pactl versions without JSON output.
    raw = run_output(["pactl", "list", "short", "sources"])
    for line in raw.splitlines():
        columns = line.split()
        if len(columns) < 2:
            continue
        name = columns[1]
        is_monitor = name.endswith(".monitor")
        if (kind == "system") == is_monitor:
            result.append((name, name))
    return result


def list_audio_source_ports(source_name):
    """Return the available input ports for one PulseAudio source."""
    for source in pulse_source_info():
        if isinstance(source, dict) and source.get("name") == source_name:
            ports = source.get("ports", [])
            if not isinstance(ports, list):
                return []
            return [(port["name"], port.get("description") or port["name"])
                    for port in ports if isinstance(port, dict) and isinstance(port.get("name"), str)]
    return []


def active_audio_source_port(source_name):
    for source in pulse_source_info():
        if isinstance(source, dict) and source.get("name") == source_name:
            port = source.get("active_port")
            if isinstance(port, str):
                return port
            return port.get("name", "") if isinstance(port, dict) else ""
    return ""


def set_audio_source_port(source_name, port_name):
    try:
        result = subprocess.run(["pactl", "set-source-port", source_name, port_name],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def audio_source(kind, preferred="default"):
    available = list_audio_sources(kind)
    names = [name for name, _description in available]
    if preferred and preferred != "default" and preferred in names:
        return preferred
    if kind == "system":
        sink = run_output(["pactl", "get-default-sink"])
        monitor = sink + ".monitor" if sink else ""
        if monitor in names:
            return monitor
    else:
        default = run_output(["pactl", "get-default-source"])
        if default in names:
            return default
    return names[0] if names else ""


def build_ffmpeg(geom, outfile, settings, display=None, window_id=None):
    """Return safe argv for an X11 capture and all supported audio combinations."""
    x, y, w, h = map(int, geom)
    # X11 desktops can have monitors left/above the origin, so negative
    # coordinates are valid. FFmpeg receives them as part of the display input.
    if w < 80 or h < 60:
        raise RuntimeError("Capture geometry is invalid.")
    fmt, fps = settings["format"], str(settings["fps"])
    if fmt not in FORMATS or fps not in ("15", "24", "30", "60"):
        raise RuntimeError("Invalid recording format or frame rate.")
    outfile = Path(outfile)
    if outfile.suffix.lower() != FORMATS[fmt]:
        raise RuntimeError("Output filename extension does not match the selected format.")
    display = display or os.environ.get("DISPLAY", ":0.0")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-thread_queue_size", "1024",
           "-f", "x11grab", "-draw_mouse", "1" if settings["cursor"] else "0", "-framerate", fps]
    if window_id is not None:
        # Capture the X11 drawable itself. A root-screen crop can include
        # window-manager borders or adjacent desktop pixels when geometry APIs
        # disagree about client/frame extents.
        try: window_id = int(window_id)
        except (TypeError, ValueError): raise RuntimeError("The active X11 window ID is invalid.")
        if window_id <= 0: raise RuntimeError("The active X11 window no longer exists.")
        cmd += ["-window_id", str(window_id), "-i", display]
    else:
        cmd += ["-video_size", f"{w}x{h}", "-i", f"{display}+{x},{y}"]
    sources = []
    if fmt != "gif":
        for enabled, kind in ((settings["system_audio"], "system"), (settings["microphone"], "microphone")):
            if enabled:
                source = audio_source(kind, settings.get(f"{kind}_source", "default"))
                # An unavailable optional source should not prevent a silent
                # recording or recording from the other selected input.
                if source:
                    sources.append((kind, source))
                    cmd += ["-thread_queue_size", "1024", "-f", "pulse", "-sample_rate", "48000",
                            "-channels", "2", "-name", "Unisc Recorder", "-stream_name",
                            f"Unisc Recorder {kind}", "-i", source]
    if fmt == "gif":
        cmd += ["-filter_complex", f"[0:v]fps={min(int(fps),20)},scale='min(1280,iw)':-2:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse[v]",
                "-map", "[v]", "-loop", "0", "-f", "gif", str(outfile)]
        return cmd
    cmd += ["-map", "0:v:0"]
    if sources:
        audio_graph = []
        audio_labels = []
        for input_index, (kind, _source) in enumerate(sources, start=1):
            label = "system" if kind == "system" else "microphone"
            chain = f"[{input_index}:a]aresample=48000:async=1:first_pts=0,aformat=sample_fmts=fltp:channel_layouts=stereo"
            if kind == "microphone":
                # Reduce proximity/room bass and bring speech presence forward.
                chain += "," + ",".join(MICROPHONE_FILTERS)
            if len(sources) == 1:
                audio_graph.append(chain + ",alimiter=limit=0.80:level=false:latency=true[aout]")
            else:
                # Leave headroom before mixing system playback and speech.
                chain += ",volume=0.65" if kind == "system" else ",volume=0.80"
                chain += f"[{label}]"
                audio_graph.append(chain)
                audio_labels.append(f"[{label}]")
        if len(sources) == 2:
            audio_graph.append("".join(audio_labels) +
                               "amix=inputs=2:duration=longest:dropout_transition=2:normalize=0,"
                               "alimiter=limit=0.80:level=false:latency=true[aout]")
        cmd += ["-filter_complex", ";".join(audio_graph), "-map", "[aout]"]
    if fmt in ("mp4", "mkv"):
        # Active windows and user-selected regions often have odd dimensions.
        # libx264 with yuv420p requires even output dimensions; pad the right
        # and bottom edges by at most one pixel rather than rejecting capture.
        cmd += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    elif fmt == "webm":
        cmd += ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "5", "-b:v", "0", "-crf", "32"]
    if sources:
        cmd += ["-c:a", "libopus" if fmt == "webm" else "aac",
                "-b:a", "160k" if fmt == "webm" else "192k", "-ar", "48000", "-ac", "2"]
    else: cmd += ["-an"]
    if fmt == "mp4": cmd += ["-movflags", "+faststart"]
    cmd.append(str(outfile))
    return cmd


def recording_is_valid(path):
    """Confirm FFmpeg wrote a readable, non-empty media file after stopping."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 128:
        return False
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15, check=False)
        duration = float(result.stdout.strip())
        return result.returncode == 0 and duration > 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


class Recorder(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title=APP)
        screen = Gdk.Screen.get_default()
        self.custom_chrome = bool(screen and self.is_composited() and screen.get_rgba_visual())
        if self.custom_chrome:
            self.set_visual(screen.get_rgba_visual())
            self.set_app_paintable(True)
            self.set_decorated(False)
        # Compact the excess vertical space under Record while retaining a
        # near-square panel and enough room for all three control rows.
        self.set_default_size(480, 430)
        self.set_size_request(460, 410)
        self.set_resizable(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.settings = load_settings()
        self.process = None
        self.log = None
        self.path = None
        self.recording = False
        self.timer_source = None
        self.countdown_source = None
        self.tray = None
        self.started = 0
        self.capture_window_id = None
        self.build_ui()
        self.connect("key-press-event", self.on_key)
        self.connect("delete-event", self.on_close)
        self.show_all()
        self.refresh_status()

    def build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.get_style_context().add_class("main-shell")
        root.set_border_width(20)
        root.set_margin_start(8); root.set_margin_end(8)
        root.set_margin_top(8); root.set_margin_bottom(8)
        self.add(root)
        header = Gtk.Box(spacing=6)
        menu_button = Gtk.MenuButton()
        menu_button.get_style_context().add_class("menu-button")
        menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_button.set_tooltip_text("More options")
        menu = Gtk.Menu()
        for label, callback in (("Recording Settings…", self.open_settings), ("Choose Output Folder…", self.choose_folder),
                                ("Open Output Folder", self.open_folder), ("About", self.about)):
            item = Gtk.MenuItem(label=label); item.connect("activate", callback); menu.append(item)
        menu.show_all(); menu_button.set_popup(menu)
        header.pack_start(menu_button, False, False, 0)
        identity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        identity.set_halign(Gtk.Align.CENTER)
        identity.set_valign(Gtk.Align.CENTER)
        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        brand.set_halign(Gtk.Align.CENTER)
        brand.pack_start(Gtk.Image.new_from_icon_name("video-display-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        title = Gtk.Label(label="Unisc Recorder")
        title.get_style_context().add_class("app-title")
        brand.pack_start(title, False, False, 0)
        identity.pack_start(brand, False, False, 0)
        self.summary = Gtk.Label()
        self.summary.get_style_context().add_class("app-summary")
        identity.pack_start(self.summary, False, False, 0)
        header.pack_start(identity, True, True, 0)
        if self.custom_chrome:
            close_button = Gtk.Button()
            close_button.get_style_context().add_class("menu-button")
            close_button.set_image(Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU))
            close_button.set_tooltip_text("Close")
            close_button.connect("clicked", lambda *_: self.close())
            header.pack_end(close_button, False, False, 0)
        header_area = Gtk.EventBox()
        header_area.set_visible_window(False)
        # Keep the menu and close hit areas well inside the rounded top corners.
        header_area.set_margin_start(18); header_area.set_margin_end(18)
        header_area.set_margin_top(4)
        if self.custom_chrome:
            header_area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            header_area.connect("button-press-event", self.drag_window)
        header_area.add(header)
        header_area.set_margin_bottom(5)
        root.pack_start(header_area, False, False, 0)

        self.mode_buttons = {}
        modes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, homogeneous=True)
        for mode, icon, tip in (("screen", "video-display-symbolic", "Full Screen"),
                                ("region", "edit-select-all-symbolic", "Select Area"),
                                ("window", "window-new-symbolic", "Active Window")):
            b = Gtk.ToggleButton(); b.get_style_context().add_class("mode-button")
            b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG)); b.set_tooltip_text(tip)
            b.connect("toggled", self.mode_toggled, mode)
            self.mode_buttons[mode] = b
            b.set_size_request(0, 126)
            modes.pack_start(b, True, True, 0)
        root.pack_start(modes, False, False, 0)
        self.set_mode(self.settings["mode"])

        audio = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7, homogeneous=True)
        self.audio_buttons = {}
        for key, icon, tip in (("system_audio", "audio-volume-high-symbolic", "Record system audio"),
                               ("microphone", "audio-input-microphone-symbolic", "Record microphone"),
                               ("cursor", "input-mouse-symbolic", "Show mouse cursor in recording")):
            b = Gtk.ToggleButton(); b.get_style_context().add_class("icon-toggle")
            b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)); b.set_tooltip_text(tip)
            b.set_size_request(0, 48)
            b.set_active(self.settings[key]); b.connect("toggled", self.preference_toggled, key)
            self.audio_buttons[key] = b; audio.pack_start(b, True, True, 0)
        root.pack_start(audio, False, False, 0)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        center.set_halign(Gtk.Align.FILL)
        # Root spacing keeps row 1 -> row 2 at 10px; this 10px top inset makes
        # row 2 -> Record 20px total, without the old expanding-center gap.
        center.set_margin_top(10)
        self.record_button = Gtk.Button(); self.record_button.get_style_context().add_class("record-button")
        self.record_button.set_size_request(180, 56)
        self.record_button.set_halign(Gtk.Align.CENTER)
        self.record_button.set_label("Record")
        self.record_button.set_tooltip_text("Start recording (F9)"); self.record_button.connect("clicked", self.toggle_record)
        center.pack_start(self.record_button, False, False, 0)
        self.state = Gtk.Label(label="Ready"); center.pack_start(self.state, False, False, 0)
        root.pack_start(center, False, False, 0)
        bottom = Gtk.Box(spacing=7)
        bottom.set_halign(Gtk.Align.CENTER)
        bottom.set_margin_top(3)
        bottom.set_margin_bottom(2)
        folder_icon = Gtk.Image.new_from_icon_name("folder-symbolic", Gtk.IconSize.MENU)
        bottom.pack_start(folder_icon, False, False, 0)
        self.location = Gtk.Label(); self.location.set_ellipsize(3); self.location.set_xalign(.5)
        bottom.pack_start(self.location, False, False, 0)
        self.timer = Gtk.Label(label=""); bottom.pack_start(self.timer, False, False, 0)
        root.pack_end(bottom, False, False, 0)
        self.apply_css()

    def apply_css(self):
        css = b"""
        window { background-color: transparent; color: #e5e5e9; }
        .main-shell { background-color: #1d1e22; border: 1px solid #393a40; border-radius: 40px; }
        dialog { background-color: #1d1e22; color: #e5e5e9; }
        label { color: #dedee3; }
        .app-title { font-weight: 600; font-size: 15px; }
        .app-summary { color: #96969e; font-size: 11px; }
        button { color: #dedee3; background-image: none; background-color: #2b2c31;
                 border: 1px solid #393a40; box-shadow: none; }
        button:hover { background-color: #35363d; }
        button:checked { background-color: #414047; border-color: #5a5662; color: #ffffff; }
        .mode-button { border-radius: 20px; padding: 6px; }
        .icon-toggle { border-radius: 16px; padding: 0; }
        button.mode-button:checked, button.mode-button.mode-selected,
        button.icon-toggle:checked { background-color: #41434a; background-image: none; border-color: #62656d; color: #ffffff; }
        .record-button { background-color: #743b42; border-color: #895058; border-radius: 18px;
                         min-width: 180px; min-height: 56px; padding: 2px 6px; font-weight: 600; font-size: 18px; }
        .record-button:hover { background-color: #87454d; }
        .record-button:active { background-color: #63333a; }
        .menu-button { background-color: transparent; border-color: transparent; padding: 2px 5px; }
        menu, menuitem { background-color: #27282d; color: #e5e5e9; }
        menuitem:hover { background-color: #414047; }
        entry, combobox, spinbutton { background-color: #292a2f; color: #e5e5e9; }
        .about-link { color: #c2b5d8; }
        *:focus { outline-style: none; outline-width: 0; outline-color: transparent; box-shadow: none; }
        """
        provider = Gtk.CssProvider(); provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def set_mode(self, mode):
        for key, button in self.mode_buttons.items(): button.handler_block_by_func(self.mode_toggled)
        for key, button in self.mode_buttons.items():
            button.set_active(key == mode)
            if key == mode:
                button.get_style_context().add_class("mode-selected")
            else:
                button.get_style_context().remove_class("mode-selected")
        for button in self.mode_buttons.values(): button.handler_unblock_by_func(self.mode_toggled)
        self.settings["mode"] = mode

    def drag_window(self, _header, event):
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        return False

    def mode_toggled(self, button, mode):
        if button.get_active():
            self.set_mode(mode); self.save(); self.refresh_status()

    def preference_toggled(self, button, key):
        self.settings[key] = button.get_active(); self.save()

    def save(self):
        try: save_settings(self.settings)
        except OSError as exc: self.message(f"Could not save preferences: {exc}")

    def refresh_status(self):
        mode = {"screen": "Full Screen", "region": "Select Area", "window": "Active Window"}[self.settings["mode"]]
        self.location.set_text(Path(self.settings["folder"]).name or self.settings["folder"])
        self.location.set_tooltip_text(self.settings["folder"])
        self.summary.set_text(f"{mode}  ·  {self.settings['format'].upper()}  ·  {self.settings['fps']} FPS")
        if not self.recording: self.state.set_text("")

    def message(self, text, error=False):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.OK, text=text)
        dialog.run(); dialog.destroy()

    def open_settings(self, *_):
        dialog = Gtk.Dialog(title="Recording Settings", transient_for=self, flags=Gtk.DialogFlags.MODAL)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE); dialog.set_default_size(390, 270)
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=14); dialog.get_content_area().add(grid)
        controls = {}
        items = (("format", "Video format", [(x, x.upper() if x != "mkv" else "Matroska (MKV)") for x in FORMATS]),
                 ("fps", "Frame rate", [(x, f"{x} FPS") for x in ("15", "24", "30", "60")]),
                 ("delay", "Recording delay", [(x, {"0":"No delay","3":"3 seconds","5":"5 seconds","10":"10 seconds"}[x]) for x in ("0","3","5","10")]))
        for row, (key, label, values) in enumerate(items):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            combo = Gtk.ComboBoxText()
            for ident, name in values: combo.append(ident, name)
            combo.set_active_id(str(self.settings[key])); grid.attach(combo, 1, row, 1, 1); controls[key] = combo
        gif_note = Gtk.Label(label="GIF recordings are silent (no audio).", xalign=0)
        grid.attach(gif_note, 0, 3, 2, 1)
        monitor_values = [("all", "All displays")]
        monitor_values += [(m[0], f"{m[0]} · {m[3]} × {m[4]}") for m in list_monitors()]
        grid.attach(Gtk.Label(label="Display", xalign=0), 0, 4, 1, 1)
        monitor = Gtk.ComboBoxText()
        for ident, name in monitor_values: monitor.append(ident, name)
        monitor.set_active_id(self.settings.get("monitor", "all") if self.settings.get("monitor", "all") in [x[0] for x in monitor_values] else "all")
        grid.attach(monitor, 1, 4, 1, 1)
        source_controls = {}
        for row, (key, kind, label) in enumerate((
            ("system_source", "system", "System audio source"),
            ("microphone_source", "microphone", "Microphone input"),
        ), start=5):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            combo = Gtk.ComboBoxText()
            combo.append("default", "Default (automatic)")
            available_sources = list_audio_sources(kind)
            for source, description in available_sources:
                combo.append(source, description)
            active = self.settings.get(key, "default")
            combo.set_active_id(active if active == "default" or active in [s for s, _d in available_sources] else "default")
            grid.attach(combo, 1, row, 1, 1)
            source_controls[key] = combo
        grid.attach(Gtk.Label(label="Microphone port", xalign=0), 0, 7, 1, 1)
        port_combo = Gtk.ComboBoxText()

        def populate_mic_ports(*_args, preferred="default"):
            port_combo.remove_all()
            chosen = audio_source("microphone", mic_combo.get_active_id() or "default")
            ports = list_audio_source_ports(chosen) if chosen else []
            active_port = active_audio_source_port(chosen) if chosen else ""
            active_description = next((description for name, description in ports if name == active_port), "")
            default_label = "Use active port" + (f" ({active_description})" if active_description else "")
            port_combo.append("default", default_label)
            for port, description in ports:
                port_combo.append(port, description)
            valid_ports = [p for p, _description in ports]
            port_combo.set_active_id(preferred if preferred in valid_ports else "default")

        mic_combo = source_controls["microphone_source"]
        populate_mic_ports(preferred=self.settings.get("microphone_port", "default"))
        mic_combo.connect("changed", lambda *_args: populate_mic_ports(preferred="default"))
        grid.attach(port_combo, 1, 7, 1, 1)
        dialog.show_all(); dialog.run()
        for key, widget in controls.items(): self.settings[key] = widget.get_active_id()
        self.settings["monitor"] = monitor.get_active_id()
        for key, widget in source_controls.items(): self.settings[key] = widget.get_active_id() or "default"
        self.settings["microphone_port"] = port_combo.get_active_id() or "default"
        dialog.destroy(); self.save()

    def choose_folder(self, *_):
        dialog = Gtk.FileChooserDialog(title="Choose output folder", parent=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.ACCEPT)
        if Path(self.settings["folder"]).is_dir(): dialog.set_filename(self.settings["folder"])
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            self.settings["folder"] = dialog.get_filename(); self.save(); self.refresh_status()
        dialog.destroy()

    def open_folder(self, *_):
        folder = Path(self.settings["folder"]); folder.mkdir(parents=True, exist_ok=True)
        try: subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc: self.message(f"Could not open output folder: {exc}", True)

    def about(self, *_):
        dialog = Gtk.Dialog(title="About Unisc Recorder", transient_for=self, flags=Gtk.DialogFlags.MODAL)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        grid = Gtk.Grid(column_spacing=10, row_spacing=5, margin=14)
        dialog.get_content_area().add(grid)
        details = Gtk.Label(label="Unisc Recorder 3.0\nDeveloped by Mr. Emmanuel Mukiza\nCountry: Rwanda\nContact Support:", xalign=0)
        grid.attach(details, 0, 0, 2, 1)
        self.add_about_link(grid, "Email", "mailto:mukizacreator@gmail.com", "mukizacreator@gmail.com", 1)
        social = Gtk.Label(label="Social Medias:", xalign=0)
        grid.attach(social, 0, 2, 2, 1)
        links = (
            ("Instagram", "https://instagram.com/mukiza_me", "instagram.com/mukiza_me"),
            ("Facebook", "https://facebook.com/meMukiza", "facebook.com/meMukiza"),
            ("YouTube", "https://youtube.com/@Th_estarME", "youtube.com/@Th_estarME"),
            ("LinkedIn", "https://linkedin.com/in/mukiza-creator", "linkedin.com/in/mukiza-creator"),
            ("X", "https://x.com/mukiza_me", "x.com/mukiza_me"),
            ("GitHub", "https://github.com/mukizacreator", "github.com/mukizacreator"),
        )
        for row, (name, url, label) in enumerate(links, 3):
            self.add_about_link(grid, name, url, label, row)
        dialog.show_all(); dialog.run(); dialog.destroy()

    @staticmethod
    def add_about_link(grid, name, url, label, row):
        grid.attach(Gtk.Label(label=name, xalign=0), 0, row, 1, 1)
        link = Gtk.LinkButton.new_with_label(url, label)
        link.get_style_context().add_class("about-link")
        grid.attach(link, 1, row, 1, 1)

    def capture_geometry(self):
        self.capture_window_id = None
        mode = self.settings["mode"]
        if mode == "screen":
            selected = self.settings.get("monitor", "all")
            for monitor in list_monitors():
                if monitor[0] == selected: return monitor[1:]
            return screen_geometry()
        if mode == "region":
            self.hide(); self.set_keep_above(False)
            try:
                result = subprocess.run(["slop", "-f", "%x %y %w %h"], text=True, capture_output=True, timeout=90)
            finally:
                self.show_all(); self.present()
            if result.returncode != 0: return None
            try: x, y, w, h = map(int, result.stdout.split())
            except ValueError: raise RuntimeError("Area selection returned invalid coordinates.")
            if w < 80 or h < 60: raise RuntimeError("Selected area is too small.")
            return x, y, w, h
        if mode == "window":
            # User can focus target during the configured delay; sample it only
            # after hiding this window so it cannot become the capture target.
            self.hide(); self.set_keep_above(False)
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                while Gtk.events_pending(): Gtk.main_iteration()
                time.sleep(.02)
            try:
                self.capture_window_id, geometry = active_window_details()
                return geometry
            finally: self.show_all(); self.present()
        raise RuntimeError("Unknown capture mode.")

    def output_path(self):
        folder = Path(self.settings["folder"]).expanduser(); folder.mkdir(parents=True, exist_ok=True)
        stem = datetime.now().strftime("Recording_%Y-%m-%d_%H-%M-%S")
        path = folder / (stem + FORMATS[self.settings["format"]]); n = 1
        while path.exists(): path = folder / f"{stem}_{n}{FORMATS[self.settings['format']]}"; n += 1
        return path

    def toggle_record(self, *_):
        if self.recording: self.stop_recording()
        elif self.countdown_source:
            GLib.source_remove(self.countdown_source); self.countdown_source = None
            self.state.set_text("Ready"); self.record_button.set_tooltip_text("Start recording (F9)")
        else: self.start_recording()

    def start_recording(self):
        if not shutil.which("ffmpeg"):
            self.message("FFmpeg is not installed. Install it with your system package manager.", True); return
        try: save_settings(self.settings)
        except OSError as exc: self.message(f"Cannot save settings: {exc}", True); return
        delay = int(self.settings["delay"])
        if delay:
            self.remaining = delay; self.state.set_text(f"Starting in {delay}…")
            self.countdown_source = GLib.timeout_add_seconds(1, self.countdown_tick); return
        self.begin_recording()

    def countdown_tick(self):
        self.remaining -= 1
        if self.remaining <= 0:
            self.countdown_source = None; self.begin_recording(); return GLib.SOURCE_REMOVE
        self.state.set_text(f"Starting in {self.remaining}…"); return GLib.SOURCE_CONTINUE

    def begin_recording(self):
        try:
            if not os.environ.get("DISPLAY"):
                raise RuntimeError("No X11 display is available. Start the app from an X11 desktop session.")
            geom = self.capture_geometry()
            if geom is None: self.state.set_text("Ready"); return
            self.path = self.output_path()
            free_bytes = shutil.disk_usage(self.path.parent).free
            if free_bytes < 32 * 1024 * 1024:
                raise RuntimeError("There is not enough free disk space to start a recording.")
            requested_port = self.settings.get("microphone_port", "default")
            if self.settings.get("microphone") and self.settings["format"] != "gif" and requested_port != "default":
                mic_source = audio_source("microphone", self.settings.get("microphone_source", "default"))
                if not mic_source:
                    raise RuntimeError("The selected microphone source is unavailable. Choose another input in Recording Settings.")
                if not set_audio_source_port(mic_source, requested_port):
                    raise RuntimeError("Could not select the requested microphone port. Check that the device is connected and available.")
            cmd = build_ffmpeg(geom, self.path, self.settings, window_id=self.capture_window_id)
            log_path = Path.home() / ".cache/unisc-recorder/ffmpeg.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = open(log_path, "a", buffering=1)
            for key, kind in (("system_audio", "system"), ("microphone", "microphone")):
                if self.settings.get(key) and self.settings["format"] != "gif":
                    chosen = audio_source(kind, self.settings.get(f"{kind}_source", "default"))
                    self.log.write(f"Selected {kind} source: {chosen or 'unavailable'}\n")
                    if kind == "microphone":
                        self.log.write(f"Selected microphone port: {active_audio_source_port(chosen) or 'system default'}\n")
            self.hide()
            self.process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.log, start_new_session=True)
            time.sleep(.2)
            if self.process.poll() is not None: raise RuntimeError(f"FFmpeg exited during startup. See {log_path}")
            self.recording = True; self.started = time.monotonic()
            self.record_button.set_label("Stop")
            self.record_button.set_tooltip_text("Stop recording (F9)")
            self.state.set_text("Recording"); self.timer.set_text("00:00")
            resolved_sources = {}
            fallbacks = []
            for key, kind in (("system_audio", "system"), ("microphone", "microphone")):
                if not self.settings.get(key) or self.settings["format"] == "gif":
                    continue
                pref_key = f"{kind}_source"
                selected = self.settings.get(pref_key, "default")
                resolved = audio_source(kind, selected)
                resolved_sources[kind] = resolved
                if selected != "default" and resolved != selected:
                    fallbacks.append((kind, selected, resolved))
            missing = ["system audio" if kind == "system" else "microphone"
                       for kind, source in resolved_sources.items() if not source]
            if missing and self.settings["format"] != "gif":
                self.state.set_text("Recording · audio unavailable")
            for button in self.mode_buttons.values(): button.set_sensitive(False)
            for button in self.audio_buttons.values(): button.set_sensitive(False)
            self.timer_source = GLib.timeout_add(500, self.tick)
            # Hide the recorder so a full-desktop capture does not contain its own UI.
            # Xfce's notification area exposes a stop action for the hidden window.
            self.install_tray(); self.hide()
            if missing and self.settings["format"] != "gif":
                self.message("Recording started without " + " and ".join(missing) + ". The selected source is unavailable.", True)
            elif fallbacks:
                details = "; ".join(f"{kind}: {actual or 'unavailable'}" for kind, _selected, actual in fallbacks)
                self.message("A saved audio source is no longer available; using the current default instead (" + details + ").", True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.cleanup_log(); self.process = None; self.recording = False
            self.remove_tray(); self.show_all(); self.message(str(exc), True); self.state.set_text("Ready")

    def tick(self):
        if not self.recording: return GLib.SOURCE_REMOVE
        if self.process and self.process.poll() is not None:
            self.finish_recording(False, "FFmpeg stopped unexpectedly. Check the recording and ~/.cache/unisc-recorder/ffmpeg.log.")
            return GLib.SOURCE_REMOVE
        seconds = int(time.monotonic() - self.started)
        self.timer.set_text(f"{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}")
        if self.tray: self.tray.set_tooltip_text(f"Unisc Recorder — recording {self.timer.get_text()}")
        return GLib.SOURCE_CONTINUE

    def stop_recording(self):
        if not self.process: return
        proc = self.process; self.recording = False
        if self.timer_source: GLib.source_remove(self.timer_source); self.timer_source = None
        try:
            proc.send_signal(signal.SIGINT)
            try: proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)
        except OSError: pass
        # FFmpeg commonly exits with status 255 after handling the deliberate
        # SIGINT used to stop a recording. Judge success by the finalized media.
        success = bool(self.path and recording_is_valid(self.path))
        self.finish_recording(bool(success), "Recording saved." if success else "Recording could not be finalized. Check ~/.cache/unisc-recorder/ffmpeg.log.")

    def cleanup_log(self):
        if self.log:
            try: self.log.close()
            except OSError: pass
            self.log = None

    def install_tray(self):
        self.tray = Gtk.StatusIcon.new_from_icon_name("media-record")
        self.tray.set_tooltip_text("Unisc Recorder — recording")
        self.tray.set_visible(True)
        self.tray.connect("popup-menu", self.tray_menu)

    def tray_menu(self, icon, button, activate_time):
        menu = Gtk.Menu()
        status = Gtk.MenuItem(label=f"Recording · {self.timer.get_text()}"); status.set_sensitive(False); menu.append(status)
        stop = Gtk.MenuItem(label="Stop Recording"); stop.connect("activate", lambda *_: self.stop_recording()); menu.append(stop)
        menu.show_all(); menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    def remove_tray(self):
        if self.tray:
            self.tray.set_visible(False); self.tray = None

    def finish_recording(self, success, message):
        self.recording = False; self.process = None; self.cleanup_log()
        self.remove_tray(); self.show_all(); self.present()
        if self.timer_source:
            try: GLib.source_remove(self.timer_source)
            except GLib.Error: pass
            self.timer_source = None
        self.record_button.set_label("Record")
        self.record_button.set_tooltip_text("Start recording (F9)")
        for button in self.mode_buttons.values(): button.set_sensitive(True)
        for button in self.audio_buttons.values(): button.set_sensitive(True)
        self.timer.set_text(""); self.state.set_text("Saved" if success else "Recording failed")
        self.refresh_status()
        if success: self.state.set_text("Saved"); self.location.set_text(self.path.name)
        self.message(message, not success)

    def on_key(self, _widget, event):
        key = Gdk.keyval_name(event.keyval)
        if key == "F9": self.toggle_record(); return True
        if key == "Escape":
            if self.countdown_source: self.toggle_record(); return True
            if self.recording: self.stop_recording(); return True
        return False

    def on_close(self, *_):
        if self.countdown_source: GLib.source_remove(self.countdown_source); self.countdown_source = None
        if self.recording: self.stop_recording()
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
        self.cleanup_log(); return False


if __name__ == "__main__":
    if os.environ.get("XDG_SESSION_TYPE", "x11") == "wayland":
        print("Unisc Recorder requires an X11 session.", file=sys.stderr); sys.exit(1)
    try: Recorder(); Gtk.main()
    except Exception as exc:
        print(f"Could not start Unisc Recorder: {exc}", file=sys.stderr); sys.exit(1)
