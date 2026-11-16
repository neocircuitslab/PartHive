"""PartHive – EasyEDA/JLCPCB(LCSC) component importer.

Thin orchestration layer over the ``easyeda2kicad-ph`` converter (a separate
AGPL-3.0 package, installed at runtime — not bundled). Given an LCSC part number
it produces, inside one *library dir*:

    <library_dir>/<symbol_name>.kicad_sym    (symbol)
    <library_dir>/<footprint_name>.pretty/   (footprint)
    <library_dir>/<model_name>.3dshapes/     (3D model)

and wires the three together (symbol -> footprint via the ``Footprint`` field,
footprint -> 3D via a portable ``(model ...)`` path).

The import runs in two phases so a UI can interpose a confirmation/resolution
dialog:

    plan(component_id)      -> proposed names + which already exist (cheap: no
                               3D geometry download)
    execute(plan, resolution) -> writes the resolved subset

``import_component()`` keeps the one-shot behaviour (plan + a default resolution
derived from config, then execute) for simple mode and the module-level API.

PartHive customisations: footprint reference ``?``, configurable text sizes,
skip-3D, and (when "prefer STEP" is on) STEP-only 3D (the WRL is dropped).

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import gzip
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Make the plugin's own sibling modules importable.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# The EasyEDA->KiCad converter is the AGPL-3.0 package `easyeda2kicad_ph`, bundled
# as a git submodule under `easyeda2kicad-ph/` (NeoCircuitsLab's fork of
# uPesy/easyeda2kicad.py). Put the submodule dir on sys.path so `easyeda2kicad_ph`
# resolves both in a git checkout (plugins/easyeda2kicad-ph/easyeda2kicad_ph) and
# in the packaged plugin (the build copies it to the same relative location).
_EE_SUBMODULE = _HERE / "easyeda2kicad-ph"
if _EE_SUBMODULE.is_dir() and str(_EE_SUBMODULE) not in sys.path:
    sys.path.insert(0, str(_EE_SUBMODULE))

from easyeda2kicad_ph.easyeda.easyeda_api import EasyedaApi  # noqa: E402
from easyeda2kicad_ph.easyeda.easyeda_importer import (  # noqa: E402
    Easyeda3dModelImporter,
    EasyedaFootprintImporter,
    EasyedaSymbolImporter,
)
from easyeda2kicad_ph.kicad import parameters_kicad_symbol as _sym_params  # noqa: E402
import parthive_pintypes as pintypes  # noqa: E402
from easyeda2kicad_ph.kicad.export_kicad_3d_model import Exporter3dModelKicad  # noqa: E402
from easyeda2kicad_ph.kicad.export_kicad_footprint import ExporterFootprintKicad  # noqa: E402
from easyeda2kicad_ph.kicad.export_kicad_symbol import (  # noqa: E402
    ExporterSymbolKicad,
    id_already_in_symbol_lib,
)

from parthive_config import ParthiveConfig  # noqa: E402
from parthive_fp_fix import (  # noqa: E402
    sanitize_footprint_file,
    scan_footprint_dir,
    validate_footprint_file,
)
from parthive_lib_table import (  # noqa: E402
    find_settings_dir,
    conflict_tables,
    is_project_dir,
    nickname_conflict,
    project_relative_uri,
    scope_tables,
    register_footprint_library,
    register_symbol_library,
)
from parthive_match import ee_pad_count  # noqa: E402
from parthive_match import reason as match_reason  # noqa: E402
from parthive_match import suggest as suggest_footprints  # noqa: E402
from parthive_paths import (  # noqa: E402
    library_paths,
    model_dir_reference,
    portable_path,
    lib_dir_problem,
    lib_name_problem,
    safe_item_name,
    safe_lib_name,
)

logger = logging.getLogger("parthive.importer")

Logger = Callable[[str], None]

# Per-item resolution actions.
ACTION_IMPORT = "import"  # write the file/entry (overwriting if present)
ACTION_SKIP = "skip"  # do not write; link to the same-named existing item
ACTION_USE_EXISTING = "use_existing"  # do not write; link to a chosen existing item


@dataclass
class ItemPlan:
    """One of the three components (symbol / footprint / model) to import."""

    kind: str  # "symbol" | "footprint" | "model"
    name: str  # proposed name
    exists: bool  # already present in the target library?
    existing: list = field(default_factory=list)  # existing names of this kind
    # Footprints only: similar existing footprints, nearest first —
    # [(name, level 1..3, hover text)] (see parthive_match).
    suggestions: list = field(default_factory=list)


@dataclass
class ComponentPlan:
    component_id: str
    cad_data: dict
    symbol: Optional[ItemPlan] = None
    footprint: Optional[ItemPlan] = None
    model: Optional[ItemPlan] = None


@dataclass
class ItemResolution:
    action: str = ACTION_IMPORT
    name: str = ""
    existing: Optional[str] = None


@dataclass
class Resolution:
    symbol: Optional[ItemResolution] = None
    footprint: Optional[ItemResolution] = None
    model: Optional[ItemResolution] = None


@dataclass
class ImportResult:
    ok: bool
    component_id: str
    symbol: Optional[Path] = None
    footprint: Optional[Path] = None
    model_wrl: Optional[Path] = None
    model_step: Optional[Path] = None
    messages: list = field(default_factory=list)
    # Libraries newly added to a KiCad library table by this import:
    # [(kind "sym"|"fp", nickname, scope "global"|"project")]
    registered: list = field(default_factory=list)


class ComponentImporter:
    def __init__(self, config: ParthiveConfig, log: Logger | None = None):
        self.config = config
        self._log = log or (lambda _msg: None)
        self.api = EasyedaApi()
        self.registered: list = []

        # Keep the names exactly as chosen: plan() refuses a name that breaks the
        # rules (lib_name_problem) instead of silently importing somewhere else.
        # Nothing is written before plan() has checked them.
        self.sym_name: str = (config.symbol_lib_name or "").strip() or safe_lib_name("")
        self.fp_name: str = (config.footprint_lib_name or "").strip() or safe_lib_name("")
        self.model_name: str = (config.model_lib_name or "").strip() or safe_lib_name("")

        paths = library_paths(
            Path(config.library_dir).expanduser(),
            self.sym_name,
            self.fp_name,
            self.model_name,
            advanced=config.advanced_mode,
        )
        self.library_dir: Path = paths["library_dir"]
        self.symbol_lib: Path = paths["symbol_lib"]
        self.footprint_dir: Path = paths["footprint_dir"]
        self.model_dir: Path = paths["model_dir"]

    # ------------------------------------------------------------------ utils
    def log(self, msg: str) -> None:
        logger.info(msg)
        self._log(msg)

    def _ensure_dirs(self) -> None:
        # parents=True so advanced-mode subfolders are created as needed.
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.symbol_lib.parent.mkdir(parents=True, exist_ok=True)
        self.footprint_dir.mkdir(parents=True, exist_ok=True)
        if not self.config.skip_3d:
            self.model_dir.mkdir(parents=True, exist_ok=True)

    def _existing_footprints(self) -> list:
        if not self.footprint_dir.is_dir():
            return []
        return sorted(
            {
                p.name[:-10]
                for p in self.footprint_dir.iterdir()
                if p.name.endswith(".kicad_mod") and len(p.name) > 10
            },
            key=str.lower,
        )

    def _existing_models(self) -> list:
        if not self.model_dir.is_dir():
            return []
        names = set()
        for p in self.model_dir.iterdir():
            for ext in (".step.gz", ".step", ".stp", ".wrl"):
                if p.name.endswith(ext) and len(p.name) > len(ext):
                    names.add(p.name[: -len(ext)])
                    break
        return sorted(names, key=str.lower)

    def _detect_model_ext(self, name: str) -> Optional[str]:
        for ext in ("step.gz", "step", "wrl", "stp"):
            if (self.model_dir / f"{name}.{ext}").exists():
                return ext
        return None

    @staticmethod
    def _valid_id(component_id: str) -> bool:
        return component_id.startswith("C") and component_id[1:].isdigit()

    # ------------------------------------------------------------------ plan
    def plan(self, component_id: str) -> Optional[ComponentPlan]:
        """Fetch CAD data and work out the proposed names + collisions.

        Cheap: parses symbol + footprint metadata but does NOT download the 3D
        geometry (the model name is available from the footprint's SVGNODE).
        """
        component_id = component_id.strip().upper()
        if not self._valid_id(component_id):
            self.log(f"Invalid LCSC id '{component_id}' — expected e.g. C25804.")
            return None
        problem = lib_dir_problem(self.library_dir)
        if problem:
            self.log(f"Library folder: {problem}")
            self.log("Nothing imported.")
            return None
        for label, nick in (("Symbol", self.sym_name), ("Footprint", self.fp_name),
                            ("3D", self.model_name)):
            problem = lib_name_problem(nick)
            if problem:
                self.log(f"{label} library '{nick}': {problem}")
                self.log("Nothing imported.")
                return None
        conflicts = self.name_conflicts()
        if conflicts:
            for msg in conflicts:
                self.log(msg)
            self.log(
                "Nothing imported. KiCad needs every library name to be unique — pick "
                "another name with '+ Create new library...'."
            )
            return None
        self.log(f"Fetching EasyEDA/LCSC component: {component_id}")

        try:
            cad_data = self.api.get_cad_data_of_component(lcsc_id=component_id)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Network/API error while fetching {component_id}: {exc}")
            return None
        if not cad_data:
            err = getattr(self.api, "last_error", None)
            code = getattr(err, "code", None)
            if code in (403, 429):
                self.log(f"EasyEDA refused the request for {component_id} (HTTP {code}). It "
                         "limits how many parts can be fetched in a short time — wait a few "
                         "minutes and try again.")
            elif err is not None:
                self.log(f"Couldn't reach EasyEDA for {component_id}: {err}")
            else:
                self.log(f"No CAD data returned for {component_id} (does the part exist?).")
            return None

        plan = ComponentPlan(component_id=component_id, cad_data=cad_data)

        try:
            ee_symbol = EasyedaSymbolImporter(easyeda_cp_cad_data=cad_data).get_symbol()
            # EasyEDA names become file names / the "Item" in "Lib:Item": make
            # them safe (no : / \ etc., no Windows device names, sane length).
            sym_name = safe_item_name(ee_symbol.info.name, component_id)
            sym_exists = id_already_in_symbol_lib(str(self.symbol_lib), sym_name)
            plan.symbol = ItemPlan("symbol", sym_name, sym_exists)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not read symbol: {exc}")

        try:
            ee_footprint = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data).get_footprint()
            fp_name = safe_item_name(ee_footprint.info.name, component_id)
            fp_exists = (self.footprint_dir / f"{fp_name}.kicad_mod").exists()
            plan.footprint = ItemPlan("footprint", fp_name, fp_exists, self._existing_footprints())
            try:
                pads = ee_pad_count(ee_footprint)
                plan.footprint.suggestions = [
                    (name, level, match_reason(fp_name, name, level))
                    for name, level in suggest_footprints(fp_name, pads, self.footprint_dir)
                ]
            except Exception:  # noqa: BLE001 - suggestions are a nice-to-have
                logger.exception("footprint suggestions")

            if not self.config.skip_3d and ee_footprint.model_3d is not None:
                model_name = safe_item_name(ee_footprint.model_3d.name, fp_name)
                model_exists = self._detect_model_ext(model_name) is not None
                plan.model = ItemPlan("model", model_name, model_exists, self._existing_models())
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not read footprint: {exc}")

        return plan

    def _default_resolution(self, plan: ComponentPlan) -> Resolution:
        """One-shot behaviour: import everything, but respect config.overwrite
        (skip an item that already exists unless overwrite is on)."""
        overwrite = self.config.overwrite

        def item(p: Optional[ItemPlan]) -> Optional[ItemResolution]:
            if p is None:
                return None
            if p.exists and not overwrite:
                return ItemResolution(action=ACTION_SKIP, name=p.name)
            return ItemResolution(action=ACTION_IMPORT, name=p.name)

        sym = item(plan.symbol)
        fp = item(plan.footprint)
        mdl = item(plan.model)
        # A newly imported/updated 3D model must be referenced by the footprint,
        # so re-write the footprint even if it already exists.
        if (
            mdl is not None
            and mdl.action == ACTION_IMPORT
            and fp is not None
            and fp.action == ACTION_SKIP
        ):
            fp = ItemResolution(action=ACTION_IMPORT, name=plan.footprint.name)
        return Resolution(sym, fp, mdl)

    # --------------------------------------------------------------- writers
    def _write_3d(self, cad_data: dict, save_name: str) -> tuple[Optional[Path], Optional[Path]]:
        """Download + export the 3D model under ``save_name``. Returns (wrl, step).

        When "prefer STEP" is on and a STEP was produced, the WRL is deleted so
        only the STEP file remains.
        """
        try:
            model_3d = Easyeda3dModelImporter(
                easyeda_cp_cad_data=cad_data, download_raw_3d_model=True, api=self.api
            ).output
            if model_3d is None:
                self.log("No 3D model available for this component.")
                return None, None
            model_3d.name = save_name  # honour rename

            exporter = Exporter3dModelKicad(model_3d=model_3d)
            if not exporter.output:
                self.log("No 3D model available for this component.")
                return None, None

            self.model_dir.mkdir(parents=True, exist_ok=True)
            exporter.export(output_dir=str(self.model_dir), overwrite=True)

            wrl = self.model_dir / f"{save_name}.wrl"
            step = self.model_dir / f"{save_name}.step"
            wrl_path: Optional[Path] = wrl if wrl.exists() else None
            step_path: Optional[Path] = step if step.exists() else None

            if self.config.compress_models and step_path:
                gz = step_path.with_suffix(".step.gz")
                with open(step_path, "rb") as f_in, gzip.open(gz, "wb", compresslevel=9) as f_out:
                    f_out.write(f_in.read())
                step_path.unlink()
                step_path = gz

            # STEP-only: when STEP is preferred and available, drop the WRL.
            if self.config.prefer_step and step_path and wrl_path:
                try:
                    wrl_path.unlink()
                except OSError:
                    pass
                wrl_path = None

            if wrl_path:
                self.log(f"Saved 3D model (WRL): {wrl_path.name}")
            if step_path:
                self.log(f"Saved 3D model (STEP): {step_path.name}")
            return wrl_path, step_path
        except Exception as exc:  # noqa: BLE001
            self.log(f"3D model import failed: {exc}")
            logger.exception("3d import")
            return None, None

    @staticmethod
    def _choose_ext(prefer_step: bool, compress: bool, wrl, step) -> tuple[bool, str]:
        if prefer_step and step is not None:
            return True, "step.gz" if compress else "step"
        if wrl is not None:
            return True, "wrl"
        if step is not None:
            return True, "step.gz" if compress else "step"
        return False, "wrl"

    def _write_footprint(
        self,
        cad_data: dict,
        fp_name: str,
        model_ref_name: Optional[str],
        model_ext: Optional[str],
        has_model: bool,
    ) -> Optional[Path]:
        try:
            ee_footprint = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data).get_footprint()
            ee_footprint.info.name = fp_name  # honour rename
            if has_model and model_ref_name and ee_footprint.model_3d is not None:
                ee_footprint.model_3d.name = model_ref_name
            else:
                ee_footprint.model_3d = None

            fp_file = self.footprint_dir / f"{fp_name}.kicad_mod"
            ExporterFootprintKicad(footprint=ee_footprint).export(
                footprint_full_path=str(fp_file),
                model_3d_path=self._path_ref(self.model_dir),
                model_3d_extension=model_ext or "wrl",
                reference="?",  # PartHive: fixed footprint reference (not user-editable)
                text_size=self.config.footprint_text_size,
            )
            # The converter leaves some strings unquoted (footprint name, fp_text
            # values, ...). KiCad refuses to load the WHOLE .pretty library when it
            # meets one, so normalise and verify before reporting success.
            try:
                changed, notes = sanitize_footprint_file(fp_file, fp_name)
                if changed:
                    logger.info("footprint sanitised: %s", "; ".join(notes))
                    self.log(f"Normalised footprint for KiCad ({len(notes)} fix(es)).")
                problems = validate_footprint_file(fp_file, fp_name)
                if problems:
                    for problem in problems[:5]:
                        self.log(f"WARNING: footprint may not load in KiCad — {problem}")
                    logger.warning("footprint validation: %s", problems)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Footprint validation skipped ({exc}).")
                logger.exception("footprint sanitise/validate")

            self.log(f"Saved footprint: {fp_file.name}")
            return fp_file
        except Exception as exc:  # noqa: BLE001
            self.log(f"Footprint import failed: {exc}")
            logger.exception("footprint import")
            return None

    def _write_symbol(
        self, cad_data: dict, sym_name: str, fp_link_name: Optional[str]
    ) -> Optional[Path]:
        try:
            ee_symbol = EasyedaSymbolImporter(easyeda_cp_cad_data=cad_data).get_symbol()
            ee_symbol.info.name = sym_name  # honour rename
            if fp_link_name:
                ee_symbol.info.package = fp_link_name  # symbol -> footprint link
            _sym_params.set_property_font_size(self.config.symbol_text_size)
            exporter = ExporterSymbolKicad(symbol=ee_symbol, lib_path=str(self.symbol_lib))
            # PartHive: sensible electrical pin types (EasyEDA's are mostly
            # undefined or wrong) — see parthive_pintypes.
            mode = getattr(self.config, "pin_types", pintypes.MODE_SMART)
            counts = pintypes.apply(exporter.output, mode, _sym_params.KiPinType)
            if counts:
                label = {pintypes.MODE_SMART: "Smart", pintypes.MODE_KEEP: "EasyEDA's",
                         pintypes.MODE_PASSIVE: "all passive"}.get(mode, mode)
                self.log(f"Pin types ({label}): {pintypes.summary(counts)}")
            exporter.save_to_lib(
                lib_path=str(self.symbol_lib),
                footprint_lib_name=self.fp_name,
                overwrite=True,
            )
            self.log(f"Saved symbol: {sym_name}")
            return self.symbol_lib
        except Exception as exc:  # noqa: BLE001
            self.log(f"Symbol import failed: {exc}")
            logger.exception("symbol import")
            return None
        finally:
            _sym_params.set_property_font_size(None)

    def name_conflicts(self) -> list:
        """Messages for each target library whose name KiCad already uses for a
        DIFFERENT library. Registering would be skipped and the symbol's
        footprint link would point at the other library, so we stop instead."""
        if not self.config.auto_register:
            return []
        # Check the global table AND the open project's table, whichever card is
        # selected: inside that project KiCad shows both, and a project library
        # hides a global one with the same name.
        open_project = (self.config.project_dir or "").strip() or None
        out = []
        for kind, nick, target, label in (
            ("sym", self.sym_name, self.symbol_lib, "Symbol"),
            ("fp", self.fp_name, self.footprint_dir, "Footprint"),
        ):
            other = nickname_conflict(nick, target, conflict_tables(kind, open_project))
            if other:
                out.append(
                    f"{label} library name '{nick}' is already used in KiCad for "
                    f"another library: {other}"
                )
        return out

    def _project_dir(self) -> Optional[Path]:
        """The project folder when registering per-project, else None."""
        if self.config.register_scope != "project":
            return None
        raw = (self.config.project_dir or "").strip()
        return Path(raw).expanduser() if raw else None

    def _path_ref(self, target: Path) -> str:
        """Path string to store in KiCad files / lib tables for ``target``.

        Project scope + target inside the project -> ``${KIPRJMOD}/...`` (moves
        with the project). Otherwise the user-profile-portable path.
        """
        project = self._project_dir()
        if project is not None:
            rel = project_relative_uri(target, project)
            if rel is not None:
                return rel
        if target == self.model_dir:
            return model_dir_reference(target)
        return portable_path(target)

    def _register_libraries(self) -> None:
        if not self.config.auto_register:
            return
        if self.config.register_scope == "project":
            self._register_project_libraries()
            return
        settings_dir = find_settings_dir()
        if settings_dir is None:
            self.log("Could not locate KiCad settings; register the libraries manually.")
            return
        try:
            # Only register what exists (a skipped / failed symbol leaves no
            # .kicad_sym), and never create a missing global table.
            sym_changed = fp_changed = False
            if self.symbol_lib.is_file():
                sym_changed, sym_msg = register_symbol_library(
                    settings_dir, self.sym_name, portable_path(self.symbol_lib), create=False
                )
                self.log(f"Global symbol library table: {sym_msg}")
            if self.footprint_dir.is_dir():
                fp_changed, fp_msg = register_footprint_library(
                    settings_dir, self.fp_name, portable_path(self.footprint_dir), create=False
                )
                self.log(f"Global footprint library table: {fp_msg}")
            self._note_registered(sym_changed, fp_changed, "global")
            if sym_changed or fp_changed:
                self.log("New global library added — restart KiCad to see it.")
        except Exception as exc:  # noqa: BLE001
            self.log(f"Library registration skipped ({exc}); add the libraries manually.")
            logger.exception("lib table registration")

    def _note_registered(self, sym_changed: bool, fp_changed: bool, scope: str) -> None:
        if sym_changed:
            self.registered.append(("sym", self.sym_name, scope))
        if fp_changed:
            self.registered.append(("fp", self.fp_name, scope))

    def _register_project_libraries(self) -> None:
        project = self._project_dir()
        if project is None or not project.is_dir():
            self.log(
                "Project library: no project folder set — choose one under "
                "'Register in', or register the libraries manually."
            )
            return
        if not is_project_dir(project):
            self.log(
                f"Project library: no .kicad_pro found in {project} — "
                "pick the folder that holds your project file."
            )
            return
        try:
            sym_uri = self._path_ref(self.symbol_lib)
            fp_uri = self._path_ref(self.footprint_dir)
            sym_changed = fp_changed = False
            if self.symbol_lib.is_file():  # only register what exists
                sym_changed, sym_msg = register_symbol_library(project, self.sym_name, sym_uri)
                self.log(f"Project symbol library table ({project.name}): {sym_msg}")
            if self.footprint_dir.is_dir():
                fp_changed, fp_msg = register_footprint_library(project, self.fp_name, fp_uri)
                self.log(f"Project footprint library table ({project.name}): {fp_msg}")
            if not sym_uri.startswith("${KIPRJMOD}"):
                self.log(
                    "NOTE: the library dir is outside the project folder, so the "
                    "project tables use a fixed path. Put the library dir inside the "
                    "project to make it portable."
                )
            self._note_registered(sym_changed, fp_changed, "project")
            if sym_changed or fp_changed:
                self.log(
                    "New project library added — close and reopen the project in "
                    "KiCad to see it."
                )
        except Exception as exc:  # noqa: BLE001
            self.log(f"Project library registration skipped ({exc}); add it manually.")
            logger.exception("project lib table registration")

    # ------------------------------------------------------------- execute
    def execute(self, plan: ComponentPlan, resolution: Resolution) -> ImportResult:
        messages: list = []
        base_log = self._log
        self._log = lambda m: (messages.append(m), base_log(m)) and None

        try:
            self._ensure_dirs()
            cad_data = plan.cad_data

            # 1) Resolve the 3D model reference (and import it if requested).
            model_ref_name: Optional[str] = None
            model_ext: Optional[str] = None
            has_model = False
            model_written = False
            wrl = step = None
            rm = resolution.model
            if plan.model is not None and rm is not None:
                if rm.action == ACTION_IMPORT:
                    wrl, step = self._write_3d(cad_data, rm.name)
                    chosen, ext = self._choose_ext(
                        self.config.prefer_step, self.config.compress_models, wrl, step
                    )
                    if chosen:
                        model_ref_name, model_ext, has_model = rm.name, ext, True
                        model_written = True
                elif rm.action == ACTION_USE_EXISTING and rm.existing:
                    ext = self._detect_model_ext(rm.existing)
                    if ext:
                        model_ref_name, model_ext, has_model = rm.existing, ext, True
                        self.log(f"Using existing 3D model: {rm.existing}.{ext}")
                elif rm.action == ACTION_SKIP:
                    ext = self._detect_model_ext(rm.name)
                    if ext:
                        model_ref_name, model_ext, has_model = rm.name, ext, True
                        self.log(f"Keeping existing 3D model: {rm.name}.{ext}")
                    else:
                        self.log("Skipped 3D model.")

            # 2) Footprint.
            footprint_path = None
            rf = resolution.footprint
            if plan.footprint is not None and rf is not None:
                if rf.action == ACTION_IMPORT:
                    footprint_path = self._write_footprint(
                        cad_data, rf.name, model_ref_name, model_ext, has_model
                    )
                elif rf.action == ACTION_USE_EXISTING:
                    self.log(f"Using existing footprint: {rf.existing}")
                else:  # skip
                    self.log(f"Keeping existing footprint: {rf.name}")

            # 3) Symbol (links to the resolved footprint).
            symbol_path = None
            rs = resolution.symbol
            if plan.symbol is not None and rs is not None:
                if rs.action == ACTION_IMPORT:
                    if rf is not None:
                        fp_link = rf.existing if rf.action == ACTION_USE_EXISTING else rf.name
                    else:
                        fp_link = None
                    symbol_path = self._write_symbol(cad_data, rs.name, fp_link)
                else:  # skip
                    self.log(f"Keeping existing symbol: {rs.name}")

            # One malformed footprint stops KiCad loading the WHOLE .pretty library,
            # which would also hide the footprint just imported. Report that, but
            # never touch other people's files: only the imported footprint is ever
            # rewritten. Use "Check library" to repair the rest on request.
            try:
                _checked, broken = scan_footprint_dir(self.footprint_dir)
                broken = [b for b in broken if b[0] != f"{rf.name}.kicad_mod"] if rf else broken
                if broken:
                    self.log(
                        f"NOTE: {len(broken)} other footprint(s) in this library cannot be "
                        "loaded by KiCad, which hides the whole library (this import "
                        "included). Use 'Check library' to repair them:"
                    )
                    for fname, problem in broken[:5]:
                        self.log(f"  - {fname}: {problem}")
                    if len(broken) > 5:
                        self.log(f"  ... and {len(broken) - 5} more")
            except Exception:  # noqa: BLE001
                logger.exception("library scan")

            self._register_libraries()

            wrote_something = any(
                r is not None and r.action == ACTION_IMPORT
                for r in (resolution.symbol, resolution.footprint, resolution.model)
            )
            # Success if any intended write landed (symbol, footprint, OR 3D
            # model) or there was nothing to write.
            ok = (
                (symbol_path is not None)
                or (footprint_path is not None)
                or model_written
                or not wrote_something
            )
            self.log("Import complete." if ok else "Import failed — nothing written.")
            return ImportResult(
                ok=ok,
                component_id=plan.component_id,
                symbol=symbol_path,
                footprint=footprint_path,
                model_wrl=wrl,
                model_step=step,
                messages=messages,
                registered=list(self.registered),
            )
        finally:
            self._log = base_log

    # ------------------------------------------------------------- one-shot
    def import_component(self, component_id: str) -> ImportResult:
        plan = self.plan(component_id)
        if plan is None:
            return ImportResult(False, component_id.strip().upper())
        return self.execute(plan, self._default_resolution(plan))


def import_component(
    component_id: str, config: ParthiveConfig, log: Logger | None = None
) -> ImportResult:
    return ComponentImporter(config, log).import_component(component_id)
