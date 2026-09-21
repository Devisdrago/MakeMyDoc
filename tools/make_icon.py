"""Generate assets/MakeMyDoc.ico from the tray icon drawing (run from the project root)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from makemydoc.capture import State  # noqa: E402
from makemydoc.tray import make_icon  # noqa: E402

img = make_icon(State.IDLE, 256)
os.makedirs("assets", exist_ok=True)
img.save("assets/MakeMyDoc.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote assets/MakeMyDoc.ico")
