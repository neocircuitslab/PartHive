"""PartHive – classic pcbnew (SWIG) Action Plugin registration.

KiCad loads this file as a package on startup and, if the SWIG scripting system
is present (KiCad 7–10), registers the action so a **PartHive** entry appears in
the PCB Editor under *Tools → External Plugins* (and, if enabled, as a toolbar
button — the per-plugin "Show button" toggle lives in
*Preferences → PCB Editor → Action Plugins*).

This is the primary, zero-configuration path. KiCad 9/10 also expose the plugin
through the IPC API (``plugin.json`` -> ``parthive_action.py``), but the IPC
server is OFF by default (*Preferences → Plugins → Enable KiCad API*), so we do
not rely on it. Both routes open the same dialog.

PartHive is licensed under AGPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("parthive")

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    import pcbnew
except ImportError:  # not running inside KiCad (e.g. imported for tests)
    pcbnew = None  # type: ignore[assignment]


if pcbnew is not None:

    class PartHivePlugin(pcbnew.ActionPlugin):
        def defaults(self) -> None:
            self.name = "PartHive — EasyEDA/JLCPCB importer"
            self.category = "Import"
            self.description = (
                "Import EasyEDA/JLCPCB (LCSC) parts (symbol, footprint, 3D model) "
                "into a KiCad library."
            )
            self.show_toolbar_button = True
            icon = _HERE / "icon.png"
            self.icon_file_name = str(icon)
            self.dark_icon_file_name = str(icon)

        def Run(self) -> None:
            try:
                try:
                    from parthive_ui import show_dialog
                except ImportError:
                    from .parthive_ui import show_dialog  # type: ignore[no-redef]
                show_dialog(None)
            except Exception:  # noqa: BLE001
                logger.exception("PartHive failed to start")
                try:
                    import wx

                    wx.MessageBox(
                        "PartHive failed to start. See parthive.log in the plugin folder.",
                        "PartHive",
                        wx.OK | wx.ICON_ERROR,
                    )
                except Exception:  # noqa: BLE001
                    pass

    # Register unconditionally on every KiCad that provides the SWIG API
    # (7–10). KiCad 11 removes SWIG action plugins; on that release the IPC
    # entry (plugin.json) takes over.
    try:
        PartHivePlugin().register()
        logger.info("PartHive SWIG action plugin registered.")
    except Exception:  # noqa: BLE001
        logger.exception("PartHive registration failed")
