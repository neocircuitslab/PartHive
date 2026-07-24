#!/usr/bin/env python3
"""PartHive – IPC API entry point (KiCad 9/10) and standalone launcher.

KiCad's IPC plugin runner executes this file as ``__main__`` in its own process
(where wxPython is available). It is also runnable directly for testing:

    python parthive_action.py

The plugin itself only shows a dialog and writes library files — it does not
touch the open board — so no IPC/board connection is required.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s]: %(message)s",
        filename=str(_HERE / "parthive.log"),
        filemode="w",
    )
    logging.info("PartHive starting (entrypoint).")

    try:
        import wx  # noqa: F401
    except Exception:  # noqa: BLE001
        logging.exception("wxPython is not available")
        raise

    from parthive_ui import show_dialog

    app = wx.GetApp() or wx.App(False)
    show_dialog(None)  # ShowModal runs its own event loop and blocks until closed
    logging.info("PartHive closed.")


if __name__ == "__main__":
    main()
