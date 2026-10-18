"""Start and quit the official Busy Tag app (macOS) so it can share the device with us."""
import logging
import subprocess

log = logging.getLogger(__name__)


class BusyTagApp:
    def __init__(self, name="BusyTag"):
        self.name = name

    def quit(self):
        # Guarded with `is running` so AppleScript doesn't launch the app just to quit it.
        script = f'if application "{self.name}" is running then tell application "{self.name}" to quit'
        self._run(["osascript", "-e", script])

    def launch(self):
        # -g: open in the background without stealing focus.
        self._run(["open", "-g", "-a", self.name])

    def _run(self, argv):
        try:
            subprocess.run(argv, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("%s failed: %s", argv[0], e)
