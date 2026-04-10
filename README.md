# Lecture Slide Capture

Lecture Slide Capture is a macOS app bundle that saves only the moments when lecture slides change in a browser window.

What is included:
- `Lecture Slide Capture.app`: runnable macOS app bundle
- `Lecture Slide Capture.app/Contents/Resources/slide_capture.py`: capture engine
- `Lecture Slide Capture.app/Contents/Resources/requirements.txt`: Python dependencies installed on demand

Features:
- Select a Chrome lecture window from a live window list
- Save images only when slide transitions are detected
- Generate `slides.pdf` when the session ends
- Remember the default output directory
- Show a live preview window during capture

How to use:
1. Launch `Lecture Slide Capture.app`.
2. Allow dependency installation on first run if prompted.
3. Start capture or inspect the window list first.
4. Review the timestamped output folder created under the base save path.

Default output path:
- `~/Desktop/lecture_captures`
- Each run creates a new timestamped session folder automatically.

Notes:
- macOS Screen Recording permission may be required.
- Static checks passed for the Python entrypoint and shell launchers.
- End-to-end capture still depends on live macOS GUI permissions and an available lecture window.
