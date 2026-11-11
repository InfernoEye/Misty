import os

os.environ["QT_WAYLAND_DISABLED_INTERFACES"] = "xdg_session_manager_v1"
os.environ["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"

from app.main import main

if __name__ == "__main__":
    main()
