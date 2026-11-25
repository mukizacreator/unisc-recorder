import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("recorder", ROOT / "unisc-recorder.py")
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)


class RecorderBackendTests(unittest.TestCase):
    def settings(self, fmt="mp4", system=False, mic=False):
        return {"format": fmt, "fps": "30", "cursor": True,
                "system_audio": system, "microphone": mic,
                "system_source": "default", "microphone_source": "default"}

    def command(self, fmt="mp4", system=False, mic=False):
        return recorder.build_ffmpeg((0, 0, 1280, 720), Path("/tmp/capture") / ("video." + fmt),
                                     self.settings(fmt, system, mic), display=":99")

    def test_x11_modes_are_geometry_based_without_portal(self):
        # Full screen, selected region and active window each resolve to a concrete X11 rectangle.
        for geometry in ((0, 0, 1920, 1080), (50, 70, 640, 480), (200, 100, 1024, 768)):
            cmd = recorder.build_ffmpeg(geometry, Path("/tmp/v.mp4"), self.settings(), display=":99")
            self.assertIn("x11grab", cmd)
            self.assertIn(f"{geometry[2]}x{geometry[3]}", cmd)
            self.assertFalse(any("portal" in arg.lower() for arg in cmd))

    def test_odd_sized_active_window_uses_even_h264_output_padding(self):
        # This reproduces the logged 915x539 active-window encoder failure.
        cmd = recorder.build_ffmpeg((100, 50, 915, 539), Path("/tmp/window.mp4"), self.settings(), display=":99")
        self.assertIn("915x539", cmd)
        self.assertEqual(cmd[cmd.index("-vf") + 1], "pad=ceil(iw/2)*2:ceil(ih/2)*2")
        self.assertIn("libx264", cmd)

    def test_active_window_uses_ffmpeg_window_id_instead_of_root_crop(self):
        cmd = recorder.build_ffmpeg((100, 50, 915, 539), Path("/tmp/window.mp4"),
                                    self.settings(), display=":99", window_id="12345")
        self.assertEqual(cmd[cmd.index("-window_id") + 1], "12345")
        self.assertEqual(cmd[cmd.index("-i") + 1], ":99")
        self.assertNotIn("-video_size", cmd)
        self.assertNotIn(":99+100,50", cmd)

    def test_all_four_audio_combinations(self):
        for system, mic in ((True, True), (True, False), (False, True), (False, False)):
            with patch.object(recorder, "audio_source", side_effect=lambda kind, preferred="default": kind + "-source"):
                cmd = self.command(system=system, mic=mic)
            expected = int(system) + int(mic)
            self.assertEqual(cmd.count("-f"), 1 + expected)
            self.assertEqual("amix=inputs=2" in " ".join(cmd), expected == 2)
            self.assertEqual("-an" in cmd, expected == 0)
            if expected:
                graph = cmd[cmd.index("-filter_complex") + 1]
                self.assertIn("aresample=48000:async=1:first_pts=0", graph)
                self.assertIn("alimiter=limit=0.80", graph)
                self.assertIn("-ar", cmd)
            if mic and expected:
                graph = cmd[cmd.index("-filter_complex") + 1]
                self.assertIn("highpass=f=85:p=2", graph)
                self.assertIn("afftdn=nr=12:nf=-35:tn=1:gs=8", graph)
                self.assertIn("equalizer=f=180", graph)
            if expected == 2:
                self.assertIn("normalize=0", cmd[cmd.index("-filter_complex") + 1])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_ffmpeg_accepts_each_audio_filter_graph(self):
        # String assertions alone missed an unsupported high-pass option.
        for system, mic in ((True, False), (False, True), (True, True)):
            with self.subTest(system=system, mic=mic), patch.object(
                recorder, "audio_source", side_effect=lambda kind, preferred="default": kind + "-synthetic"
            ):
                cmd = self.command(system=system, mic=mic)
                graph = cmd[cmd.index("-filter_complex") + 1]
                audio_count = int(system) + int(mic)
                ffmpeg_cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=16x16:d=1:r=1",
                ]
                for index in range(audio_count):
                    frequency = 440 if index == 0 else 180
                    ffmpeg_cmd += [
                        "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:sample_rate=48000:duration=1",
                    ]
                ffmpeg_cmd += ["-filter_complex", graph, "-map", "[aout]", "-f", "null", "-"]
                result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_format_specific_encoders_extensions_and_gif_palette(self):
        for fmt, encoder in (("mp4", "libx264"), ("mkv", "libx264"), ("webm", "libvpx-vp9"), ("gif", "palettegen")):
            with patch.object(recorder, "audio_source", return_value="test.monitor"):
                cmd = self.command(fmt=fmt, system=True)
            self.assertIn(encoder, " ".join(cmd))
            self.assertTrue(cmd[-1].endswith("." + fmt) or fmt == "gif")
        with self.assertRaises(RuntimeError):
            recorder.build_ffmpeg((0, 0, 100, 100), Path("/tmp/wrong.webm"), self.settings("mp4"))

    def test_cursor_is_passed_to_x11grab(self):
        settings = self.settings(); settings["cursor"] = False
        cmd = recorder.build_ffmpeg((0, 0, 100, 100), Path("/tmp/v.mp4"), settings, display=":99")
        self.assertEqual(cmd[cmd.index("-draw_mouse") + 1], "0")

    def test_negative_x11_coordinates_are_valid_for_left_or_upper_monitors(self):
        cmd = recorder.build_ffmpeg((-1280, 0, 1280, 720), Path("/tmp/left.mp4"), self.settings(), display=":99")
        self.assertIn(":99+-1280,0", cmd)

    def test_missing_optional_audio_keeps_video_command_valid(self):
        for system, mic in ((True, False), (False, True), (True, True)):
            with patch.object(recorder, "audio_source", return_value=""):
                cmd = self.command(system=system, mic=mic)
            self.assertIn("-an", cmd)
            self.assertIn("x11grab", cmd)
            self.assertEqual(cmd[-1], "/tmp/capture/video.mp4")

    def test_finalized_media_is_checked_with_ffprobe_duration(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "recording.mp4"
            path.write_bytes(b"x" * 256)
            good = type("Result", (), {"returncode": 0, "stdout": "2.4\n"})()
            with patch.object(recorder.subprocess, "run", return_value=good) as run:
                self.assertTrue(recorder.recording_is_valid(path))
                self.assertEqual(run.call_args.args[0][0], "ffprobe")
            bad = type("Result", (), {"returncode": 1, "stdout": ""})()
            with patch.object(recorder.subprocess, "run", return_value=bad):
                self.assertFalse(recorder.recording_is_valid(path))

    def test_preferences_are_sanitized_and_saved(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text('{"format":"bogus","fps":"999","microphone":1}')
            prefs = recorder.load_settings(path)
            self.assertEqual(prefs["format"], "mp4")
            self.assertEqual(prefs["fps"], "30")
            self.assertFalse(prefs["microphone"])
            recorder.save_settings(prefs, path)
            self.assertEqual(recorder.load_settings(path), prefs)

    def test_audio_source_selection_prefers_saved_device_then_falls_back(self):
        sources = [("builtin_mic", "Built-in Microphone"), ("headset_mic", "Headset Microphone")]
        with patch.object(recorder, "list_audio_sources", return_value=sources), \
             patch.object(recorder, "run_output", return_value=""):
            self.assertEqual(recorder.audio_source("microphone", "headset_mic"), "headset_mic")
            self.assertEqual(recorder.audio_source("microphone", "missing_mic"), "builtin_mic")

    def test_pulse_source_discovery_separates_microphones_and_monitors(self):
        payload = '[{"name":"alsa_input.mic","description":"Built-in Mic","monitor_of_sink":null},' \
                  '{"name":"alsa_output.sink.monitor","description":"Speaker monitor","monitor_of_sink":1}]'
        with patch.object(recorder, "run_output", return_value=payload):
            self.assertEqual(recorder.list_audio_sources("microphone"), [("alsa_input.mic", "Built-in Mic")])
            self.assertEqual(recorder.list_audio_sources("system"), [("alsa_output.sink.monitor", "Speaker monitor")])

    def test_microphone_ports_are_discovered_and_can_be_selected(self):
        payload = '[{"name":"alsa_input.mic","active_port":"internal","ports":[' \
                  '{"name":"internal","description":"Internal Microphone"},' \
                  '{"name":"headset","description":"Headset Microphone"}]}]'
        with patch.object(recorder, "run_output", return_value=payload):
            self.assertEqual(recorder.list_audio_source_ports("alsa_input.mic"), [
                ("internal", "Internal Microphone"), ("headset", "Headset Microphone")])
            self.assertEqual(recorder.active_audio_source_port("alsa_input.mic"), "internal")
        good = type("Result", (), {"returncode": 0})()
        with patch.object(recorder.subprocess, "run", return_value=good) as run:
            self.assertTrue(recorder.set_audio_source_port("alsa_input.mic", "headset"))
            self.assertEqual(run.call_args.args[0], ["pactl", "set-source-port", "alsa_input.mic", "headset"])


if __name__ == "__main__":
    unittest.main()
