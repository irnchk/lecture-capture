# Contributing

Thanks for helping improve Lecture Slide Capture.

## Good First Contributions

- Report capture failures with OS version, browser, and permission state.
- Improve Windows packaging and window-capture behavior.
- Add small sample fixtures for slide-transition detection regressions.
- Improve first-run diagnostics for missing Python packages or screen permissions.
- Improve GUI accessibility, keyboard navigation, and localization.

## Development

The current app bundle keeps the Python sources under:

```text
Lecture Slide Capture.app/Contents/Resources/
```

Useful checks:

```sh
python3 -m py_compile "Lecture Slide Capture.app/Contents/Resources/slide_capture_gui.py" \
  "Lecture Slide Capture.app/Contents/Resources/slide_capture.py"
bash -n "Lecture Slide Capture.app/Contents/Resources/run_capture_in_terminal.sh"
```

Windows packaging is maintained through:

```text
packaging/windows/LectureSlideCapture.windows.spec
scripts/build_windows.ps1
```

## Privacy Expectations

Lecture Slide Capture should remain local-first:

- Do not upload lecture video, screenshots, or generated slides.
- Do not add background telemetry.
- Keep saved outputs under user-selected local folders.
- Document any new permission requirement clearly.

## Pull Request Notes

Please include:

- what changed
- why it changed
- platform tested
- browser or capture mode tested
- before/after behavior for UI or detection changes
