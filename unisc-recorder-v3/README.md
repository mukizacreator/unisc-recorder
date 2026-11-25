# Unisc Recorder 3

A compact, dark, rounded GTK3 screen recorder for MX Linux/Xfce on X11. Its default panel is near-square and sized to avoid excess space below the controls, with three large capture buttons in one row, a smaller audio row, and a centered medium-sized muted-red rectangular Record button. The capture-to-audio row gap is 10px; the audio-to-Record gap is 20px. It uses FFmpeg `x11grab` directly and does **not** depend on GNOME ScreenCast or xdg-desktop-portal. Format, frame rate, delay, display, and output folder are secondary controls.

## Install

Extract the archive and run:

```bash
cd ~/Downloads
unzip -o unisc-recorder-v3.zip
cd unisc-recorder-v3
chmod +x install.sh
sudo ./install.sh
```

The installer supports Debian-based MX Linux, installs the listed runtime packages, and upgrades `/opt/unisc-recorder` in place. It preserves `~/.config/unisc-recorder/settings.json`. The application appears in the Applications menu as **Unisc Recorder** under Multimedia/Video.

To remove the application while retaining preferences:

```bash
sudo ./uninstall.sh
```

## Use

- The first row's three capture buttons select Full Screen, Select Area, or Active Window. Hover for tooltips.
- The three independent toggles control system audio, microphone audio, and cursor visibility.
- The Record button starts/stops capture. F9 toggles recording while this window has focus. Escape cancels the delay or stops recording.
- Use the three-dot menu for Recording Settings, output folder, opening the output folder, and About.
- About lists the developer, Rwanda, support email, and the Instagram, Facebook, YouTube, LinkedIn, X, and GitHub links.
- Preferences persist at `~/.config/unisc-recorder/settings.json`.
- GIF uses FFmpeg palette generation and is silent by design. Audio toggles remain saved but are ignored for GIF output.
- The default output folder is `~/Videos/Screen Recordings`; filenames include date/time and gain a numeric suffix rather than overwriting an existing file.

## Capture and audio

Full Screen records the X11 root desktop; the Recording Settings dialog can select a connected display when `xrandr` reports it. Select Area uses `slop`. Active Window hides the recorder, identifies the focused X11 window, and passes its window ID directly to FFmpeg instead of estimating a desktop rectangle. This prevents adjacent desktop pixels and window-manager frame extents from leaking into the recording. FFmpeg captures the selected window drawable; if the window is resized, newly enlarged portions are not added, and closing or minimizing it ends capture. H.264 output pads odd-sized captures by at most one pixel on the bottom/right edges because yuv420p encoding requires even output dimensions.

System audio is discovered from PulseAudio-compatible monitor sources (including PipeWire's PulseAudio layer). In Recording Settings, choose a specific system source, microphone, and (when the audio server exposes it) microphone port such as Internal Microphone or Headset Microphone. The app applies an explicitly selected port before recording; “Use active port” leaves desktop auto-switching in control. The selected source and active port are written to the local FFmpeg log. Pulse inputs are captured at 48 kHz; microphone audio receives a high-pass filter, moderate adaptive noise reduction, bass control, and speech-presence EQ. Mixing leaves headroom and limits peaks to reduce clipping. AAC is encoded at 192 kbit/s and Opus at 160 kbit/s. Signal processing cannot repair a clipped, defective, or intrinsically noisy microphone, and it cannot remove room echo as reliably as a system echo-cancellation source; use headphones when desktop audio and microphone are both enabled. If a selected source disappears, capture falls back to an available source or silent video, and the selected source is logged for troubleshooting.

The application must run in an X11 session. Capture requires `ffmpeg` with `x11grab`; MP4/MKV use `libx264`, WebM uses `libvpx-vp9`, and audio uses AAC or Opus. FFmpeg diagnostics are written to `~/.cache/unisc-recorder/ffmpeg.log`.

## Dependencies

The installer uses `dependencies.txt` as the project dependency list. The runtime packages provide Python/GTK3, FFmpeg, X11 screen/window utilities, region selection, PulseAudio-compatible audio discovery, and folder opening. No browser, cloud service, GNOME session, or portal ScreenCast interface is required.

## Troubleshooting and limitations

- **X11 display unavailable:** log in to an X11/Xfce session; Wayland is not supported.
- **Area selection does not open:** install/check `slop` and ensure the X11 display is available.
- **Active Window captures the wrong window:** focus the intended application while the recorder is hidden; target geometry is sampled once and is not tracked during the recording.
- **No audio / unavailable source:** check `pactl list short sources` and `pactl get-default-sink`; disable the corresponding audio toggle for a silent recording.
- **FFmpeg errors:** inspect `~/.cache/unisc-recorder/ffmpeg.log`.
- Closing the app while recording requests a clean FFmpeg stop. If it does not exit promptly, the process is terminated to avoid an orphan.
- F9 and Escape are window shortcuts, not global shortcuts.

## Verification

When stopping, Unisc Recorder signals FFmpeg to finalize its container, then uses `ffprobe` to verify that the output is readable and has a positive duration. FFmpeg may emit an X11 startup warning that it could not estimate a rate from the first few frames; that warning alone does not mean the recording failed. FFmpeg's normal SIGINT finalization can return exit status 255; the app validates the media file instead of treating that expected status as a save failure.

The included tests validate Python syntax, FFmpeg argument construction, output validation, formats, extension checks, missing optional audio, and all four audio combinations without requiring live X11 capture. An actual desktop recording requires a working X server and could not be exercised in the build environment. This project has not been tested on the user's physical HP EliteBook/MX Linux installation.
