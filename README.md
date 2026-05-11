# Lecture Slide Capture

Lecture Slide Capture is a macOS app bundle that saves only the moments when lecture slides change in a browser window.

What is included:
- `Lecture Slide Capture.app`: runnable macOS app bundle
- `Lecture Slide Capture.app/Contents/Resources/slide_capture_gui.py`: GUI frontend
- `Lecture Slide Capture.app/Contents/Resources/slide_capture.py`: capture engine
- `Lecture Slide Capture.app/Contents/Resources/requirements.txt`: Python dependencies installed on demand
- `design/lecture-slide-capture-redesign-mockup.png`: reference mockup for the redesigned GUI

Features:
- Select a Chrome lecture window or screen region from the GUI
- Pick a slide ROI, review the latest saved slide, browse recent saved slides, and inspect session logs
- Save images only when slide transitions are detected
- Generate `slides.pdf` when the session ends
- Remember the default output directory
- Pause/resume capture and finish safely

How to use:
1. Launch `Lecture Slide Capture.app`.
2. If required Python packages are missing, run the install command shown by the app.
3. Choose the target window or screen region, then select the slide area.
4. Press `Start Capture` and review the timestamped output folder created under the base save path.

Default output path:
- `~/Desktop/lecture_captures`
- Each run creates a new timestamped session folder automatically.

Notes:
- macOS Screen Recording permission may be required.
- Static checks passed for the capture engine, GUI entrypoint, and shell launchers.
- The app launcher now resolves a usable Python runtime and starts the GUI directly.
- End-to-end capture still depends on live macOS GUI permissions and an available lecture window.
