# -*- coding: utf-8 -*-
"""
ReadDWG.pushbutton / script.py
================================
EasyBIMTools – CAD Block BOQ  (Vision Edition)
-----------------------------------------------
PIPELINE
--------
1.  בחירת CAD link ב-Revit
2.  קריאת DWG מהדיסק → מניית בלוקים (acdbmgd / ezdxf)
3.  רנדור geometry של כל block definition → PNG זמני (ezdxf + matplotlib)
4.  שליחת התמונות + תיאורי הדקל ל-Claude Vision API
5.  Claude מחזיר התאמה: block_name → dekel_code + confidence
6.  חישוב כמויות ומחירים → CSV + Legend ב-Revit + output window

CONFIG FILE
-----------
%APPDATA%\pyRevit\Extensions\EasyBIMTools.extension\
    EasyBIM_config.json
  {
    "anthropic_api_key": "sk-ant-..."
  }

IronPython 2.7 / pyRevit 4.8+ / Revit 2023-2026.
"""

# ── stdlib ────────────────────────────────────────────────────────────────────
import os
import io
import csv
import sys
import json
import base64
import tempfile
import traceback
import subprocess
from collections import OrderedDict

# ── Revit API ─────────────────────────────────────────────────────────────────
import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    ImportInstance,
    CADLinkType,
    ElementId,
    ExternalFileReference,
    ExternalFileReferenceType,
    ModelPathUtils,
    ViewFamily,
    Document,
)

clr.AddReference("System")
import System
import System.Collections.Generic
from Autodesk.Revit.UI import TaskDialog, TaskDialogCommonButtons, TaskDialogResult

# ── pyRevit ───────────────────────────────────────────────────────────────────
from pyrevit import forms, script, revit
from pyrevit import HOST_APP

# =============================================================================
#  DEKEL CATALOG  (כל הסעיפים הרלוונטיים לרכיבי חשמל)
#  מבנה: dekel_code → {desc, unit_price}
#  משמש כ-"vocabulary" שנשלח ל-Claude Vision לצורך ההתאמה
# =============================================================================
DEKEL_CATALOG = OrderedDict([
    # ── MCB 1P ───────────────────────────────────────────────────────────────
    (u"08.062.0060", {u"desc": u"מא\"ז אופיין C לזרם 10-32 אמפר חד קוטבי, כושר ניתוק 10kA",    u"unit_price": 55.10}),
    (u"08.062.0070", {u"desc": u"מא\"ז אופיין C לזרם 40 אמפר חד קוטבי, כושר ניתוק 10kA",        u"unit_price": 70.00}),
    (u"08.062.0080", {u"desc": u"מא\"ז אופיין C לזרם 50 או 63 אמפר חד קוטבי, כושר ניתוק 10kA",  u"unit_price": 104.00}),
    # ── MCB 2P ───────────────────────────────────────────────────────────────
    (u"08.062.0170", {u"desc": u"מא\"ז אופיין C לזרם 10-32 אמפר דו קוטבי, כושר ניתוק 10kA",     u"unit_price": 126.00}),
    (u"08.062.0180", {u"desc": u"מא\"ז אופיין C לזרם 40 אמפר דו קוטבי, כושר ניתוק 10kA",         u"unit_price": 155.00}),
    # ── MCB 3P ───────────────────────────────────────────────────────────────
    (u"08.062.0250", {u"desc": u"מא\"ז אופיין C לזרם 10-32 אמפר תלת קוטבי, כושר ניתוק 10kA",    u"unit_price": 220.00}),
    (u"08.062.0260", {u"desc": u"מא\"ז אופיין C לזרם 40 אמפר תלת קוטבי, כושר ניתוק 10kA",        u"unit_price": 263.00}),
    (u"08.062.0270", {u"desc": u"מא\"ז אופיין C לזרם 50 או 63 אמפר תלת קוטבי, כושר ניתוק 10kA",  u"unit_price": 360.00}),
    # ── MCB 4P ───────────────────────────────────────────────────────────────
    (u"08.062.0510", {u"desc": u"מא\"ז אופיין C לזרם 40 אמפר ארבע קוטבי, כושר ניתוק 10kA",       u"unit_price": 488.00}),
    # ── MCCB ─────────────────────────────────────────────────────────────────
    (u"08.063.0010", {u"desc": u"מאמ\"תים עד 3X40 אמפר כושר ניתוק 25kA",                         u"unit_price": 900.00}),
    (u"08.063.0020", {u"desc": u"מאמ\"תים עד 3X63 אמפר כושר ניתוק 25kA",                         u"unit_price": 910.00}),
    (u"08.063.0030", {u"desc": u"מאמ\"תים עד 3X100 אמפר כושר ניתוק 25kA",                        u"unit_price": 1280.00}),
    (u"08.063.0040", {u"desc": u"מאמ\"תים עד 3X160 אמפר כושר ניתוק 25kA",                        u"unit_price": 1950.00}),
    # ── Half-auto ────────────────────────────────────────────────────────────
    (u"08.064.0040", {u"desc": u"מפסקי זרם חצי אוטומטיים תלת קוטביים עד 40 אמפר, 15kA",         u"unit_price": 900.00}),
    # ── Disconnectors ────────────────────────────────────────────────────────
    (u"08.065.0025", {u"desc": u"מפסקי זרם חד קוטביים לזרם 40 אמפר",                              u"unit_price": 192.00}),
    (u"08.065.0115", {u"desc": u"מפסקי זרם דו קוטביים לזרם 2X40 אמפר",                            u"unit_price": 237.00}),
    (u"08.065.0220", {u"desc": u"מפסקי זרם תלת קוטביים לזרם 3X40 אמפר",                           u"unit_price": 361.00}),
    (u"08.065.0230", {u"desc": u"מפסקי זרם תלת קוטביים לזרם 3X63 אמפר",                           u"unit_price": 475.00}),
    (u"08.065.0240", {u"desc": u"מפסקי זרם תלת קוטביים לזרם 3X100 אמפר",                          u"unit_price": 681.00}),
    # ── Relays / Contactors ──────────────────────────────────────────────────
    (u"08.066.0010", {u"desc": u"ממסר פיקוד נשלף 8 פינים",                                         u"unit_price": 116.00}),
    (u"08.066.0030", {u"desc": u"ממסר פיקוד נשלף 11 פינים",                                        u"unit_price": 130.00}),
    (u"08.066.0050", {u"desc": u"ממסר צעד חד קוטבי 16A",                                           u"unit_price": 165.00}),
    (u"08.066.0110", {u"desc": u"ממסר יתרת זרם תרמי עד 24 אמפר",                                   u"unit_price": 372.00}),
    (u"08.066.0120", {u"desc": u"ממסר יתרת זרם תרמי עד 50 אמפר",                                   u"unit_price": 722.00}),
    # ── Starters ─────────────────────────────────────────────────────────────
    (u"08.067.0100", {u"desc": u"מתנע כוכב משולש עד 5 כ\"ס",                                      u"unit_price": 1180.00}),
    (u"08.067.0110", {u"desc": u"מתנע כוכב משולש עד 10 כ\"ס",                                     u"unit_price": 1180.00}),
    (u"08.067.0160", {u"desc": u"מתנע רך אלקטרוני דיגיטלי 10 כ\"ס",                               u"unit_price": 4060.00}),
    (u"08.067.0161", {u"desc": u"מתנע רך אלקטרוני דיגיטלי 20 כ\"ס",                               u"unit_price": 4250.00}),
    # ── Fuse disconnectors ───────────────────────────────────────────────────
    (u"08.068.0010", {u"desc": u"מנתק מבטיחים 3X32A לרבות נתיכי HRC",                             u"unit_price": 172.00}),
    (u"08.068.0015", {u"desc": u"מנתק מבטיחים 1X32A לרבות נתיך HRC",                              u"unit_price": 89.00}),
    (u"08.068.0020", {u"desc": u"מנתק מבטיחים בעומס 3X160 אמפר",                                  u"unit_price": 481.00}),
    # ── Control transformers ─────────────────────────────────────────────────
    (u"08.069.0010", {u"desc": u"שנאי פיקוד עד 100 VA",                                            u"unit_price": 288.00}),
    (u"08.069.0020", {u"desc": u"שנאי פיקוד עד 300 VA",                                            u"unit_price": 412.00}),
    (u"08.069.0030", {u"desc": u"שנאי פיקוד עד 500 VA",                                            u"unit_price": 721.00}),
    # ── Panel enclosures ─────────────────────────────────────────────────────
    (u"08.061.0010", {u"desc": u"מבנה ללוח 2200/500/1000 מ\"מ",                                    u"unit_price": 4500.00}),
    # ── Motor connection ─────────────────────────────────────────────────────
    (u"08.027.0010", {u"desc": u"חיבור מנוע תלת פאזי עד 10 כ\"ס",                                 u"unit_price": 196.00}),
])

# =============================================================================
#  CONFIG  –  API key + settings
# =============================================================================

def _load_config():
    """
    Load config from EasyBIM_config.json next to the extension root.
    Returns dict.  Missing keys → defaults.

    Expected JSON:
      {
        "anthropic_api_key": "sk-ant-...",
        "vision_confidence_threshold": 0.6
      }
    """
    config_paths = []
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # Walk up to find .extension root
        d = script_dir
        for _ in range(6):
            if d.endswith(".extension"):
                config_paths.append(os.path.join(d, "EasyBIM_config.json"))
                break
            d = os.path.dirname(d)
    except Exception:
        pass
    # Also check APPDATA
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        config_paths.append(os.path.join(
            appdata, "pyRevit", "Extensions",
            "EasyBIMTools.extension", "EasyBIM_config.json"))

    for path in config_paths:
        if os.path.isfile(path):
            try:
                with io.open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


CONFIG = _load_config()
ANTHROPIC_API_KEY          = CONFIG.get("anthropic_api_key", "")
VISION_CONFIDENCE_THRESHOLD = float(CONFIG.get("vision_confidence_threshold", 0.55))

# =============================================================================
#  PYTHON FINDER  (shared by ezdxf installer + Vision renderer)
# =============================================================================

def _find_system_python():
    """Find CPython executable. IronPython sys.executable → Revit.exe."""
    try:
        import _winreg as winreg
    except ImportError:
        try:
            import winreg
        except ImportError:
            winreg = None

    if winreg:
        for hive, base_key in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Python\PythonCore"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Python\PythonCore"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Python\PythonCore"),
        ]:
            try:
                key = winreg.OpenKey(hive, base_key)
                i = 0
                while True:
                    try:
                        version = winreg.EnumKey(key, i)
                        ik = winreg.OpenKey(hive,
                                            r"{}\{}\InstallPath".format(base_key, version))
                        exe, _ = winreg.QueryValueEx(ik, "ExecutablePath")
                        if exe and os.path.isfile(exe):
                            return exe
                        bd, _ = winreg.QueryValueEx(ik, "")
                        cand = os.path.join(bd, "python.exe")
                        if os.path.isfile(cand):
                            return cand
                        i += 1
                    except OSError:
                        break
            except OSError:
                continue

    import glob
    for drive in [r"C:\\", r"D:\\"]:
        for pat in [
            os.path.join(drive, "Python*", "python.exe"),
            os.path.join(drive, "Program Files", "Python*", "python.exe"),
            os.path.join(os.path.expanduser("~"), "AppData", "Local",
                         "Programs", "Python", "Python*", "python.exe"),
        ]:
            for m in glob.glob(pat):
                if os.path.isfile(m):
                    return m
    try:
        result = subprocess.check_output(
            ["where", "python"], stderr=open(os.devnull, "w")
        ).decode("utf-8", errors="ignore").strip()
        for line in result.splitlines():
            line = line.strip()
            if line and os.path.isfile(line) and "WindowsApps" not in line:
                return line
    except Exception:
        pass
    return None


def _ensure_package(pkg_name, import_name=None):
    """
    Ensure a Python package is available to IronPython by installing via
    system CPython and injecting its site-packages into sys.path.
    """
    import_name = import_name or pkg_name
    try:
        __import__(import_name)
        return True
    except ImportError:
        pass

    py_exe = _find_system_python()
    if not py_exe:
        return False

    try:
        subprocess.check_call(
            [py_exe, "-m", "pip", "install", pkg_name,
             "--quiet", "--no-warn-script-location"],
            stdout=open(os.devnull, "w"),
            stderr=open(os.devnull, "w"),
        )
        site_out = subprocess.check_output(
            [py_exe, "-c",
             "import site; print('\\n'.join(site.getsitepackages()))"],
            stderr=open(os.devnull, "w"),
        ).decode("utf-8", errors="ignore").strip().splitlines()
        for sp in site_out:
            sp = sp.strip()
            if sp and sp not in sys.path:
                sys.path.insert(0, sp)
    except Exception:
        return False

    try:
        __import__(import_name)
        return True
    except ImportError:
        return False


# =============================================================================
#  CAD LINK SELECTION  &  DWG PATH
# =============================================================================

def _get_cad_links(doc):
    results = []
    for elem in (FilteredElementCollector(doc)
                 .OfClass(ImportInstance).ToElements()):
        try:
            label     = u""
            type_elem = doc.GetElement(elem.GetTypeId())
            if type_elem:
                try:
                    path  = ModelPathUtils.ConvertModelPathToUserVisiblePath(
                        type_elem.GetExternalFileReference().GetAbsolutePath())
                    label = os.path.basename(path)
                except Exception:
                    pass
            if not label:
                label = elem.Category.Name if elem.Category else u"CAD"
            results.append((u"{} [id {}]".format(label, elem.Id.IntegerValue), elem))
        except Exception:
            results.append((u"CAD [id {}]".format(elem.Id.IntegerValue), elem))
    results.sort(key=lambda t: t[0])
    return results


def _get_dwg_path(import_inst, doc):
    type_elem = doc.GetElement(import_inst.GetTypeId())
    if type_elem is None:
        raise RuntimeError(u"לא ניתן לאתר את ה-CADLinkType")
    try:
        abs_path = ModelPathUtils.ConvertModelPathToUserVisiblePath(
            type_elem.GetExternalFileReference().GetAbsolutePath())
        if abs_path and os.path.isfile(abs_path):
            return abs_path
    except Exception:
        pass
    try:
        from Autodesk.Revit.DB import BuiltInParameter
        p = type_elem.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        if p and p.AsString() and os.path.isfile(p.AsString()):
            return p.AsString()
    except Exception:
        pass
    raise RuntimeError(
        u"לא ניתן לאתר את קובץ ה-DWG על הדיסק.\n"
        u"ודאי שהקישור טעון ב-Manage → Manage Links."
    )


# =============================================================================
#  DWG PARSING  –  ENGINE A: acdbmgd.dll
# =============================================================================

def _find_acdbmgd():
    candidates = []
    try:
        revit_dir = os.path.dirname(HOST_APP.proc_path)
        candidates += [os.path.join(revit_dir, "acdbmgd.dll"),
                       os.path.join(revit_dir, "AcDbMgd.dll")]
    except Exception:
        pass
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for vd in ["Autodesk", "AutoCAD"]:
        base = os.path.join(program_files, vd)
        if os.path.isdir(base):
            for root, dirs, files in os.walk(base):
                for fname in files:
                    if fname.lower() == "acdbmgd.dll":
                        candidates.append(os.path.join(root, fname))
                dirs[:] = []
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _count_blocks_acdbmgd(dwg_path):
    """Engine A: count + extract block geometry via acdbmgd.dll."""
    dll_path = _find_acdbmgd()
    if not dll_path:
        raise ImportError(u"acdbmgd.dll לא נמצא")
    clr.AddReference(dll_path)
    clr.AddReference("System")
    import System.IO
    from Autodesk.AutoCAD.DatabaseServices import (
        Database, BlockTable, BlockTableRecord, OpenMode)

    counts = {}
    db = Database(False, True)
    try:
        db.ReadDwgFile(dwg_path, System.IO.FileShare.Read, True, u"")
        db.CloseInput(True)
        tr = db.TransactionManager.StartTransaction()
        try:
            bt = tr.GetObject(db.BlockTableId, OpenMode.ForRead)
            for btr_id in bt:
                btr = tr.GetObject(btr_id, OpenMode.ForRead)
                if btr.IsAnonymous or btr.IsLayout:
                    continue
                ref_ids = btr.GetBlockReferenceIds(True, True)
                cnt     = ref_ids.Count if ref_ids else 0
                if cnt > 0:
                    counts[btr.Name] = counts.get(btr.Name, 0) + cnt
            tr.Commit()
        except Exception:
            tr.Abort()
            raise
    finally:
        db.Dispose()
    return counts


def _count_blocks_ezdxf(dwg_path):
    """Engine B: count INSERT entities in modelspace using ezdxf."""
    if not _ensure_package("ezdxf"):
        raise ImportError(u"ezdxf לא זמין")
    import ezdxf
    doc    = ezdxf.readfile(dwg_path)
    counts = {}
    for entity in doc.modelspace():
        if entity.dxftype() == "INSERT":
            name = entity.dxf.name
            if name and not name.startswith("*"):
                counts[name] = counts.get(name, 0) + 1
    return counts


def _count_blocks(dwg_path):
    errors = []
    try:
        return _count_blocks_acdbmgd(dwg_path), u"acdbmgd.dll"
    except Exception as e:
        errors.append(u"acdbmgd: {}".format(e))
    try:
        return _count_blocks_ezdxf(dwg_path), u"ezdxf"
    except Exception as e:
        errors.append(u"ezdxf: {}".format(e))
    raise RuntimeError(u"שני מנועי הקריאה נכשלו:\n" + u"\n".join(errors))


# =============================================================================
#  BLOCK RENDERER  –  DWG geometry → PNG  (runs via system Python subprocess)
# =============================================================================

# This helper script runs in SYSTEM Python (not IronPython).
# It reads block definitions from the DWG, renders each one as a small
# white-on-black PNG, and writes a JSON manifest to stdout.
_RENDER_SCRIPT = r'''
import sys, json, os, base64, io
dwg_path  = sys.argv[1]
out_dir   = sys.argv[2]
max_blocks = int(sys.argv[3]) if len(sys.argv) > 3 else 80

try:
    import ezdxf
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
except ImportError as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(1)

try:
    doc = ezdxf.readfile(dwg_path)
except Exception as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(1)

# Count inserts in modelspace to get usage count per block
counts = {}
for ent in doc.modelspace():
    if ent.dxftype() == "INSERT":
        n = ent.dxf.name
        if n and not n.startswith("*"):
            counts[n] = counts.get(n, 0) + 1

# Render each block definition that is actually used
manifest = {}
for block in doc.blocks:
    name = block.name
    if name.startswith("*"):
        continue
    if name not in counts:
        continue
    if len(manifest) >= max_blocks:
        break

    try:
        # Create a temporary document with this block as the model space
        tmp_doc    = ezdxf.new("R2010")
        tmp_msp    = tmp_doc.modelspace()
        # Copy all entities from block definition into tmp modelspace
        for ent in block:
            try:
                tmp_msp.add_entity(ent.copy())
            except Exception:
                pass
        if not list(tmp_msp):
            continue

        fig = plt.figure(figsize=(1.5, 1.5), dpi=96)
        ax  = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor("white")
        fig.patch.set_facecolor("white")

        ctx      = RenderContext(tmp_doc)
        backend  = MatplotlibBackend(ax)
        frontend = Frontend(ctx, backend)
        frontend.draw_layout(tmp_msp, finalize=True)

        ax.set_aspect("equal")
        ax.margins(0.15)
        ax.axis("off")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=96,
                    bbox_inches="tight", facecolor="white")
        plt.close(fig)
        buf.seek(0)
        img_b64 = base64.b64encode(buf.read()).decode("ascii")
        manifest[name] = img_b64
    except Exception:
        plt.close("all")
        continue

print(json.dumps({"blocks": manifest, "counts": counts}))
'''


def _render_block_images(dwg_path, out):
    """
    Run _RENDER_SCRIPT via system Python to render all used block definitions.
    Returns {block_name: base64_png_string} or {} on failure.
    """
    py_exe = _find_system_python()
    if not py_exe:
        out.print_html(
            u"<p style='color:#e67e22;direction:rtl;'>⚠ Python לא נמצא — "
            u"וידוא גרפי מושבת.</p>"
        )
        return {}

    # Ensure dependencies in system Python
    for pkg in ["ezdxf", "matplotlib"]:
        subprocess.call(
            [py_exe, "-m", "pip", "install", pkg,
             "--quiet", "--no-warn-script-location"],
            stdout=open(os.devnull, "w"),
            stderr=open(os.devnull, "w"),
        )

    # Write the render script to a temp file
    tmp_script = os.path.join(tempfile.gettempdir(), "easybim_render.py")
    with open(tmp_script, "w") as f:
        f.write(_RENDER_SCRIPT)

    out_dir = tempfile.mkdtemp(prefix="easybim_blocks_")

    out.print_html(
        u"<p style='direction:rtl;color:#555;font-size:12px;'>"
        u"🎨 מרנדר תמונות בלוקים…</p>"
    )

    try:
        result = subprocess.check_output(
            [py_exe, tmp_script, dwg_path, out_dir, "120"],
            stderr=open(os.devnull, "w"),
            timeout=120,
        )
        data = json.loads(result.decode("utf-8", errors="ignore"))
        if "error" in data:
            return {}
        return data.get("blocks", {}), data.get("counts", {})
    except Exception:
        return {}, {}


# =============================================================================
#  CLAUDE VISION  –  block image → dekel match
# =============================================================================

def _catalog_summary_text():
    """Build a compact text list of all Dekel items to pass to Claude."""
    lines = []
    for code, entry in DEKEL_CATALOG.items():
        lines.append(u"  {} – {}  (₪{:.0f})".format(
            code, entry[u"desc"], entry[u"unit_price"]))
    return u"\n".join(lines)


def _vision_match_batch(block_images, out):
    """
    Send ALL block images to Claude Vision in ONE API call.
    Returns {block_name: {"dekel_code": ..., "desc": ..., "unit_price": ...,
                          "confidence": 0.0-1.0, "vision_desc": "..."}}

    The prompt asks Claude to:
      - Look at each image (an electrical schematic symbol)
      - Return JSON mapping block_name → best Dekel match
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            u"לא נמצא Anthropic API key.\n"
            u"הוסף את המפתח לקובץ EasyBIM_config.json:\n"
            u'  { "anthropic_api_key": "sk-ant-..." }'
        )
    if not block_images:
        return {}

    # Build content list: alternate image + label pairs
    content = []
    block_names = sorted(block_images.keys())

    for name in block_names:
        b64 = block_images[name]
        content.append({
            "type": "text",
            "text": u"Block name: {}".format(name)
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64,
            }
        })

    catalog_text = _catalog_summary_text()

    system_prompt = (
        u"You are an expert electrical engineer specialising in Israeli "
        u"construction BOQ (כתב כמויות).\n"
        u"You will receive a series of AutoCAD block names and their rendered "
        u"schematic symbol images.\n"
        u"For each block, identify the electrical component it represents and "
        u"match it to the best entry in the Dekel (דקל) price catalogue below.\n\n"
        u"DEKEL CATALOGUE:\n" + catalog_text + u"\n\n"
        u"Rules:\n"
        u"1. Return ONLY a valid JSON object — no prose, no markdown fences.\n"
        u"2. Keys = the exact block names provided.\n"
        u"3. Each value = object with:\n"
        u'   "dekel_code": string (e.g. "08.062.0250") or null if no match,\n'
        u'   "confidence": float 0.0-1.0,\n'
        u'   "vision_desc": brief Hebrew description of what you see in the image\n'
        u"4. If confidence < 0.4 set dekel_code to null.\n"
        u"5. Consider: number of poles, symbol shape (square=relay, circle=motor, "
        u"zigzag=resistor/fuse, box-with-slash=breaker, coil=contactor, etc.)."
    )

    user_text = (
        u"Here are {} electrical schematic symbols. "
        u"Match each to the Dekel catalogue.".format(len(block_names))
    )
    content.append({"type": "text", "text": user_text})

    payload = json.dumps({
        "model":      "claude-opus-4-5",
        "max_tokens": 4096,
        "system":     system_prompt,
        "messages":   [{"role": "user", "content": content}],
    })

    # Use system Python to make the HTTPS request (IronPython has no ssl)
    py_exe = _find_system_python()
    if not py_exe:
        raise RuntimeError(u"Python לא נמצא לשליחת בקשת Vision API")

    requester = r'''
import sys, json, ssl
py_ver = sys.version_info[0]
if py_ver >= 3:
    from urllib.request import urlopen, Request
    from urllib.error   import HTTPError
else:
    from urllib2 import urlopen, Request, HTTPError

payload_path = sys.argv[1]
api_key      = sys.argv[2]

with open(payload_path, "rb") as f:
    body = f.read()

req = Request(
    "https://api.anthropic.com/v1/messages",
    data=body,
    headers={
        "Content-Type":      "application/json",
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
    }
)
try:
    resp = urlopen(req)
    print(resp.read().decode("utf-8"))
except HTTPError as e:
    print(json.dumps({"error": e.read().decode("utf-8")}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
'''

    # Write payload to temp file (may be large with images)
    payload_path   = os.path.join(tempfile.gettempdir(), "easybim_vision_payload.json")
    requester_path = os.path.join(tempfile.gettempdir(), "easybim_vision_req.py")

    with open(payload_path,   "wb") as f:
        f.write(payload.encode("utf-8"))
    with open(requester_path, "w") as f:
        f.write(requester)

    out.print_html(
        u"<p style='direction:rtl;color:#555;font-size:12px;'>"
        u"🤖 שולח {} סמלים ל-Claude Vision…</p>".format(len(block_names))
    )

    try:
        raw = subprocess.check_output(
            [py_exe, requester_path, payload_path, ANTHROPIC_API_KEY],
            stderr=open(os.devnull, "w"),
            timeout=180,
        ).decode("utf-8", errors="ignore")
    except Exception as exc:
        raise RuntimeError(u"קריאת API נכשלה: {}".format(exc))

    resp_obj = json.loads(raw)
    if "error" in resp_obj:
        raise RuntimeError(u"API שגיאה: {}".format(resp_obj["error"]))

    # Extract JSON from Claude's text response
    text_content = u""
    for block in resp_obj.get("content", []):
        if block.get("type") == "text":
            text_content += block.get("text", "")

    # Parse the JSON Claude returned
    # Strip markdown fences if present
    text_content = text_content.strip()
    if text_content.startswith("```"):
        text_content = text_content.split("```")[1]
        if text_content.startswith("json"):
            text_content = text_content[4:]

    matches_raw = json.loads(text_content)

    # Enrich with price data from DEKEL_CATALOG
    result = {}
    for name, match in matches_raw.items():
        code       = match.get("dekel_code")
        confidence = float(match.get("confidence", 0.0))
        vision_d   = match.get("vision_desc", u"")

        if code and code in DEKEL_CATALOG and confidence >= VISION_CONFIDENCE_THRESHOLD:
            entry = DEKEL_CATALOG[code]
            result[name] = {
                u"dekel_code":  code,
                u"desc":        entry[u"desc"],
                u"unit_price":  entry[u"unit_price"],
                u"confidence":  confidence,
                u"vision_desc": vision_d,
                u"matched":     True,
            }
        else:
            result[name] = {
                u"dekel_code":  code or u"—",
                u"desc":        vision_d or u"לא זוהה",
                u"unit_price":  0.0,
                u"confidence":  confidence,
                u"vision_desc": vision_d,
                u"matched":     False,
            }

    return result


# =============================================================================
#  BOQ ASSEMBLY  –  merge counts + vision matches
# =============================================================================

def _build_boq(raw_counts, vision_matches):
    """
    Combine block counts with Claude Vision results.

    Returns (rows, unmapped, grand_total).
    rows: list of dicts with keys:
      block, dekel_code, desc, count, unit_price, total_price,
      confidence, vision_desc, matched
    """
    rows        = []
    unmapped    = []
    grand_total = 0.0

    for block_name, count in sorted(raw_counts.items()):
        if block_name in vision_matches:
            m          = vision_matches[block_name]
            total_p    = float(count) * m[u"unit_price"]
            grand_total += total_p
            rows.append({
                u"block":       block_name,
                u"dekel_code":  m[u"dekel_code"],
                u"desc":        m[u"desc"],
                u"count":       count,
                u"unit_price":  m[u"unit_price"],
                u"total_price": total_p,
                u"confidence":  m[u"confidence"],
                u"vision_desc": m[u"vision_desc"],
                u"matched":     m[u"matched"],
            })
            if not m[u"matched"]:
                unmapped.append((block_name, count))
        else:
            unmapped.append((block_name, count))

    return rows, unmapped, grand_total


# =============================================================================
#  CSV EXPORT
# =============================================================================

def _desktop_path():
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _fmt_price(v):
    if v is None or v == 0:
        return u"—"
    return u"₪ {:,.2f}".format(float(v))


def _export_csv(rows, unmapped, grand_total, dwg_path, engine):
    dwg_name = os.path.splitext(os.path.basename(dwg_path))[0]
    out_path = os.path.join(_desktop_path(), u"BOQ_{}.csv".format(dwg_name))

    with io.open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            u"שם בלוק",
            u"תיאור ויזואלי (Claude)",
            u"סעיף דקל",
            u"תיאור דקל",
            u"כמות",
            u"מחיר יחידה",
            u'סה"כ מחיר',
            u"ביטחון",
        ])
        for row in rows:
            if not row[u"matched"]:
                continue
            writer.writerow([
                row[u"block"],
                row[u"vision_desc"],
                row[u"dekel_code"],
                row[u"desc"],
                row[u"count"],
                u"{:.2f}".format(row[u"unit_price"]) if row[u"unit_price"] else u"—",
                u"{:.2f}".format(row[u"total_price"]) if row[u"total_price"] else u"—",
                u"{:.0f}%".format(row[u"confidence"] * 100),
            ])

        mapped_rows = [r for r in rows if r[u"matched"]]
        writer.writerow([])
        writer.writerow([u'סה"כ פריטים', u"", u"", u"",
                         str(sum(r[u"count"] for r in mapped_rows)),
                         u"", u"", u""])
        writer.writerow([u'סה"כ עלות (₪)', u"", u"", u"", u"", u"",
                         u"{:.2f}".format(grand_total), u""])

        if unmapped:
            writer.writerow([])
            writer.writerow([u"── לא זוהו ──"] + [u""] * 7)
            for name, cnt in sorted(unmapped, key=lambda x: -x[1]):
                writer.writerow([name, u"", u"", u"", str(cnt),
                                  u"", u"", u""])

        writer.writerow([])
        writer.writerow([u"קובץ מקור", dwg_path] + [u""] * 6)
        writer.writerow([u"מנוע קריאה", engine]  + [u""] * 6)

    return out_path


# =============================================================================
#  OUTPUT WINDOW  –  side-by-side: schematic screenshot + BOQ table
# =============================================================================

def _export_schematic_screenshot(doc, import_inst):
    """
    Export the active view containing the DWG as a PNG using
    Revit's ImageExportOptions.  Returns base64 string or None.
    """
    try:
        from Autodesk.Revit.DB import (
            ImageExportOptions, ImageFileType,
            ImageResolution, ExportRange,
            ViewSet,
        )
        tmp_path = os.path.join(tempfile.gettempdir(), "easybim_schematic.png")
        opts = ImageExportOptions()
        opts.ExportRange          = ExportRange.CurrentView
        opts.FilePath             = tmp_path
        opts.HLRandWFViewsFileType = ImageFileType.PNG
        opts.ShadowViewsFileType   = ImageFileType.PNG
        opts.ImageResolution       = ImageResolution.DPI_72
        opts.ZoomType              = 1   # FitToPage
        doc.ExportImage(opts)
        # Revit appends the view name; find the exported file
        base = os.path.splitext(tmp_path)[0]
        for fname in os.listdir(tempfile.gettempdir()):
            full = os.path.join(tempfile.gettempdir(), fname)
            if fname.startswith(os.path.basename(base)) and fname.endswith(".png"):
                with open(full, "rb") as f:
                    return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        pass
    return None


def _confidence_badge(conf):
    """Return colored HTML badge for confidence level."""
    pct = int(conf * 100)
    if conf >= 0.8:
        color, bg = u"#1b5e20", u"#c8e6c9"
    elif conf >= 0.6:
        color, bg = u"#e65100", u"#fff3e0"
    else:
        color, bg = u"#b71c1c", u"#ffebee"
    return (
        u"<span style='background:{bg};color:{c};border-radius:3px;"
        u"padding:1px 5px;font-size:11px;font-weight:bold;'>{p}%</span>"
    ).format(bg=bg, c=color, p=pct)


def _print_output(out, rows, unmapped, grand_total, dwg_path, engine,
                  csv_path, schematic_b64, block_images):
    """
    Render the complete output window:
      Section 1 – header
      Section 2 – side-by-side: schematic image | BOQ table
      Section 3 – financial summary cards
      Section 4 – visual block gallery (symbol grid)
      Section 5 – unmapped blocks
      Section 6 – save confirmation
    """
    out.set_width(1300)
    out.set_height(800)

    dwg_name = os.path.basename(dwg_path)

    # ── Section 1: Header ─────────────────────────────────────────────────────
    out.print_html(
        u"<div style='font-family:Arial;direction:rtl;text-align:right;"
        u"border-bottom:2px solid #1565c0;padding-bottom:8px;margin-bottom:12px;'>"
        u"<h2 style='margin:0;color:#1565c0;'>📋 כתב כמויות – ניתוח ויזואלי (Claude Vision)</h2>"
        u"<span style='font-size:12px;color:#888;'>"
        u"קובץ: {file}  |  מנוע: {eng}  |  סף ביטחון: {thr:.0f}%"
        u"</span></div>".format(
            file=dwg_name, eng=engine,
            thr=VISION_CONFIDENCE_THRESHOLD * 100)
    )

    # ── Section 2: Side-by-side ───────────────────────────────────────────────
    # Left: schematic screenshot
    if schematic_b64:
        img_html = (
            u"<div style='background:#f5f5f5;border:1px solid #ddd;"
            u"border-radius:6px;padding:8px;text-align:center;'>"
            u"<div style='font-size:11px;color:#888;margin-bottom:4px;"
            u"direction:rtl;'>📐 סכמה</div>"
            u"<img src='data:image/png;base64,{b64}' "
            u"style='max-width:100%;max-height:420px;"
            u"object-fit:contain;'/></div>"
        ).format(b64=schematic_b64)
    else:
        img_html = (
            u"<div style='background:#f5f5f5;border:2px dashed #ccc;"
            u"border-radius:6px;padding:24px;text-align:center;color:#aaa;"
            u"height:200px;display:flex;align-items:center;"
            u"justify-content:center;'>"
            u"<span style='font-size:13px;'>תמונת סכמה<br>לא זמינה</span>"
            u"</div>"
        )

    # Right: BOQ table
    matched_rows = [r for r in rows if r[u"matched"]]
    if not matched_rows:
        table_html = (
            u"<p style='color:red;direction:rtl;'>"
            u"⚠ לא נמצאו בלוקים ממופים.</p>"
        )
    else:
        hdr = u"".join(
            u"<th style='padding:5px 8px;background:#1565c0;color:white;"
            u"border:1px solid #1976d2;white-space:nowrap;'>{}</th>".format(h)
            for h in [u"שם בלוק", u"תיאור ויזואלי", u"סעיף דקל",
                      u"כמות", u"מחיר יחידה", u'סה"כ', u"ביטחון"]
        )
        body_rows = []
        for ri, row in enumerate(matched_rows):
            bg = u"#f8f9fa" if ri % 2 == 0 else u"white"
            body_rows.append(
                u"<tr style='background:{bg};'>"
                u"<td style='padding:4px 8px;border:1px solid #eee;"
                u"font-family:monospace;direction:ltr;'>{block}</td>"
                u"<td style='padding:4px 8px;border:1px solid #eee;"
                u"direction:rtl;font-size:12px;'>{vd}</td>"
                u"<td style='padding:4px 8px;border:1px solid #eee;"
                u"font-family:monospace;color:#1565c0;'>{code}</td>"
                u"<td style='padding:4px 8px;border:1px solid #eee;"
                u"text-align:center;font-weight:bold;'>{cnt}</td>"
                u"<td style='padding:4px 8px;border:1px solid #eee;"
                u"text-align:right;'>{up}</td>"
                u"<td style='padding:4px 8px;border:1px solid #eee;"
                u"text-align:right;font-weight:bold;'>{tp}</td>"
                u"<td style='padding:4px 8px;border:1px solid #eee;"
                u"text-align:center;'>{badge}</td>"
                u"</tr>".format(
                    bg=bg,
                    block=row[u"block"],
                    vd=row[u"vision_desc"][:35],
                    code=row[u"dekel_code"],
                    cnt=row[u"count"],
                    up=_fmt_price(row[u"unit_price"]),
                    tp=_fmt_price(row[u"total_price"]),
                    badge=_confidence_badge(row[u"confidence"]),
                )
            )
        table_html = (
            u"<div style='max-height:460px;overflow-y:auto;"
            u"border-radius:6px;border:1px solid #ddd;'>"
            u"<table style='width:100%;border-collapse:collapse;"
            u"font-size:12px;font-family:Arial;'>"
            u"<thead><tr>{hdr}</tr></thead>"
            u"<tbody>{body}</tbody>"
            u"</table></div>"
        ).format(hdr=hdr, body=u"".join(body_rows))

    out.print_html(
        u"<div style='display:flex;gap:14px;align-items:flex-start;"
        u"margin-bottom:14px;'>"
        u"<div style='flex:0 0 320px;min-width:0;'>{img}</div>"
        u"<div style='flex:1;min-width:0;'>{tbl}</div>"
        u"</div>".format(img=img_html, tbl=table_html)
    )

    # ── Section 3: Financial summary ─────────────────────────────────────────
    total_items = sum(r[u"count"] for r in matched_rows)
    out.print_html(
        u"<div style='display:flex;gap:10px;margin-bottom:16px;"
        u"font-family:Arial;'>"
        u"<div style='flex:1;background:#e3f2fd;border:1px solid #90caf9;"
        u"border-radius:6px;padding:10px 14px;text-align:right;direction:rtl;'>"
        u"<div style='font-size:11px;color:#555;'>✅ זוהו בוודאות</div>"
        u"<div style='font-size:20px;font-weight:bold;color:#1565c0;'>"
        u"{items}</div></div>"
        u"<div style='flex:1;background:#fce4ec;border:1px solid #f48fb1;"
        u"border-radius:6px;padding:10px 14px;text-align:right;direction:rtl;'>"
        u"<div style='font-size:11px;color:#555;'>❓ לא זוהו</div>"
        u"<div style='font-size:20px;font-weight:bold;color:#880e4f;'>"
        u"{unmap}</div></div>"
        u"<div style='flex:2;background:#e8f5e9;border:1px solid #a5d6a7;"
        u"border-radius:6px;padding:10px 14px;text-align:right;direction:rtl;'>"
        u"<div style='font-size:11px;color:#555;'>💰 עלות כוללת (₪)</div>"
        u"<div style='font-size:24px;font-weight:bold;color:#1b5e20;'>"
        u"₪ {total}</div></div>"
        u"</div>".format(
            items=total_items,
            unmap=len(unmapped),
            total=u"{:,.2f}".format(grand_total),
        )
    )

    # ── Section 4: Visual block gallery ──────────────────────────────────────
    if block_images:
        out.print_html(
            u"<details style='margin-bottom:14px;' open>"
            u"<summary style='cursor:pointer;font-family:Arial;"
            u"font-weight:bold;font-size:13px;padding:6px 0;"
            u"color:#1565c0;'>🖼 גלריית סמלים ({n} בלוקים)</summary>"
            u"<div style='display:flex;flex-wrap:wrap;gap:8px;"
            u"padding:10px 0;'>".format(n=len(block_images))
        )
        # Build match lookup for gallery colouring
        match_map = {r[u"block"]: r for r in rows}

        for name, b64 in sorted(block_images.items()):
            row    = match_map.get(name)
            matched = row and row[u"matched"]
            conf   = row[u"confidence"] if row else 0.0

            if matched and conf >= 0.8:
                border, label_bg = u"#4caf50", u"#e8f5e9"
            elif matched and conf >= 0.6:
                border, label_bg = u"#ff9800", u"#fff3e0"
            elif matched:
                border, label_bg = u"#f44336", u"#ffebee"
            else:
                border, label_bg = u"#9e9e9e", u"#f5f5f5"

            dekel_code = row[u"dekel_code"] if row else u"—"
            conf_str   = u"{:.0f}%".format(conf * 100) if row else u"—"

            out.print_html(
                u"<div style='border:2px solid {brd};border-radius:6px;"
                u"padding:4px;text-align:center;width:110px;"
                u"background:{lbg};font-family:Arial;'>"
                u"<img src='data:image/png;base64,{b64}' "
                u"style='width:90px;height:70px;object-fit:contain;"
                u"background:white;border-radius:3px;'/>"
                u"<div style='font-size:9px;color:#333;margin-top:3px;"
                u"word-break:break-all;direction:ltr;'>{name}</div>"
                u"<div style='font-size:9px;color:#1565c0;'>{code}</div>"
                u"<div style='font-size:9px;color:#555;'>{conf}</div>"
                u"</div>".format(
                    brd=border, lbg=label_bg, b64=b64,
                    name=name[:16], code=dekel_code, conf=conf_str)
            )

        out.print_html(u"</div></details>")

    # ── Section 5: Unmapped ───────────────────────────────────────────────────
    if unmapped:
        un_rows = u"".join(
            u"<tr><td style='padding:4px 8px;font-family:monospace;"
            u"border:1px solid #eee;direction:ltr;'>{}</td>"
            u"<td style='padding:4px 8px;text-align:center;"
            u"border:1px solid #eee;'>{}</td></tr>".format(nm, cnt)
            for nm, cnt in sorted(unmapped, key=lambda x: -x[1])
        )
        out.print_html(
            u"<details style='margin-bottom:12px;'>"
            u"<summary style='cursor:pointer;color:#c62828;"
            u"font-family:Arial;font-size:13px;padding:4px 0;'>"
            u"❌ לא זוהו ({n}) – לחץ להרחבה</summary>"
            u"<table style='border-collapse:collapse;margin-top:6px;'>"
            u"<thead><tr>"
            u"<th style='padding:5px 8px;background:#fafafa;"
            u"border:1px solid #ddd;text-align:left;'>שם בלוק</th>"
            u"<th style='padding:5px 8px;background:#fafafa;"
            u"border:1px solid #ddd;'>כמות</th>"
            u"</tr></thead><tbody>{}</tbody></table>"
            u"</details>".format(n=len(unmapped), un_rows=un_rows)  # noqa
            .replace(u"</details>", u"</tbody></table></details>")
        )
        out.print_html(
            u"<details style='margin-bottom:12px;'>"
            u"<summary style='cursor:pointer;color:#c62828;"
            u"font-family:Arial;font-size:13px;padding:4px 0;'>"
            u"❌ לא זוהו ({n}) – לחץ להרחבה</summary>"
            u"<table style='border-collapse:collapse;margin-top:6px;'>"
            u"<thead><tr>"
            u"<th style='padding:5px 8px;background:#fafafa;"
            u"border:1px solid #ddd;text-align:left;'>שם בלוק</th>"
            u"<th style='padding:5px 8px;background:#fafafa;"
            u"border:1px solid #ddd;'>כמות</th>"
            u"</tr></thead><tbody>{body}</tbody></table>"
            u"</details>".format(n=len(unmapped), body=un_rows)
        )

    # ── Section 6: Save confirmation ─────────────────────────────────────────
    out.print_html(
        u"<p style='direction:rtl;color:#2e7d32;font-family:Arial;"
        u"font-size:13px;margin-top:10px;'>"
        u"✅ הקובץ נשמר: <b>{}</b></p>".format(csv_path)
    )


# =============================================================================
#  REVIT LEGEND CREATION  (6 columns + confidence column)
# =============================================================================

def _create_revit_schedule(doc, rows, unmapped, grand_total, dwg_path):
    from Autodesk.Revit.DB import (
        ViewType, View, ViewFamilyType, ViewFamily,
        FilteredElementCollector, Transaction,
        TextNote, TextNoteOptions, TextNoteType,
        ElementId, XYZ, HorizontalTextAlignment,
    )

    def _mm_to_ft(mm):
        return mm / 304.8

    COL_WIDTHS  = [50, 30, 95, 18, 26, 32, 18]   # mm
    # שם בלוק | תיאור ויזואלי | תיאור דקל | כמות | מחיר יחידה | סה"כ | %
    HEADER_LABELS = [
        u"שם בלוק", u"תיאור ויזואלי", u"תיאור דקל",
        u"כמות", u"מחיר יחידה", u'סה"כ מחיר', u"ביטחון",
    ]
    ROW_H  = 8.0
    COL_X  = []
    cx     = 0.0
    for w in COL_WIDTHS:
        COL_X.append(cx)
        cx += w

    def _get_legend(view_name):
        existing = (FilteredElementCollector(doc)
                    .OfClass(View).ToElements())
        for v in existing:
            try:
                if v.ViewType == ViewType.Legend and v.Name == view_name:
                    return v, True
            except Exception:
                pass

        legend_vft_id = None
        for vft in (FilteredElementCollector(doc)
                    .OfClass(ViewFamilyType).ToElements()):
            try:
                if vft.ViewFamily == ViewFamily.Legend:
                    legend_vft_id = vft.Id
                    break
            except Exception:
                pass
        if legend_vft_id is None:
            for v in existing:
                try:
                    if v.ViewType == ViewType.Legend:
                        legend_vft_id = v.GetTypeId()
                        break
                except Exception:
                    pass
        if legend_vft_id is None:
            raise RuntimeError(u"אין Legend View בפרויקט. צור אחד ידנית.")

        new_view = None
        try:
            view_clr_type = clr.GetClrType(View)
            arg_types = System.Array[System.Type]([
                clr.GetClrType(Document),
                clr.GetClrType(ElementId),
            ])
            mi = view_clr_type.GetMethod("CreateLegend", arg_types)
            if mi is not None:
                new_view = mi.Invoke(None, System.Array[System.Object](
                    [doc, legend_vft_id]))
        except Exception:
            pass
        if new_view is None:
            try:
                new_view = View.CreateLegend(doc, legend_vft_id)
            except Exception:
                pass
        if new_view is None:
            try:
                from Autodesk.Revit.DB import ElementTransformUtils, Transform
                src = None
                for v in existing:
                    try:
                        if v.ViewType == ViewType.Legend:
                            src = v
                            break
                    except Exception:
                        pass
                if src:
                    ids = ElementTransformUtils.CopyElements(
                        doc,
                        System.Collections.Generic.List[ElementId]([src.Id]),
                        doc, Transform.Identity, None)
                    if ids and ids.Count > 0:
                        new_view = doc.GetElement(list(ids)[0])
                        for tn in (FilteredElementCollector(doc, new_view.Id)
                                   .OfClass(TextNote).ToElements()):
                            doc.Delete(tn.Id)
            except Exception as exc:
                raise RuntimeError(u"יצירת Legend נכשלה: {}".format(exc))
        if new_view is None:
            raise RuntimeError(u"יצירת Legend נכשלה.")
        try:
            new_view.Name = view_name
        except Exception:
            pass
        return new_view, False

    def _get_tn_type():
        types    = (FilteredElementCollector(doc)
                    .OfClass(TextNoteType).ToElements())
        pref = fb = None
        for t in types:
            try:
                n = t.Name.lower()
                if any(s in n for s in ["2.5", "3mm", "3.0"]):
                    pref = t
                    break
                fb = t
            except Exception:
                pass
        return pref or fb

    dwg_name  = os.path.splitext(os.path.basename(dwg_path))[0]
    view_name = u"BOQ Vision – {}".format(dwg_name)

    t = Transaction(doc, u"יצירת BOQ Vision Legend")
    t.Start()
    try:
        legend_view, existed = _get_legend(view_name)
        if existed:
            for tn in (FilteredElementCollector(doc, legend_view.Id)
                       .OfClass(TextNote).ToElements()):
                doc.Delete(tn.Id)

        tn_type = _get_tn_type()
        if tn_type is None:
            raise RuntimeError(u"אין TextNoteType בפרויקט.")
        opts = TextNoteOptions(tn_type.Id)
        opts.HorizontalAlignment = HorizontalTextAlignment.Left

        def _place(col, row_i, text):
            x  = _mm_to_ft(COL_X[col])
            y  = _mm_to_ft(-row_i * ROW_H)
            TextNote.Create(doc, legend_view.Id,
                            XYZ(x, y, 0.0),
                            text if text else u" ", opts)

        for ci, lbl in enumerate(HEADER_LABELS):
            _place(ci, 0, lbl)

        matched_rows = [r for r in rows if r[u"matched"]]
        for ri, row in enumerate(matched_rows, start=1):
            _place(0, ri, row[u"block"])
            _place(1, ri, (row[u"vision_desc"] or u"")[:30])
            _place(2, ri, row[u"desc"][:45])
            _place(3, ri, str(row[u"count"]))
            _place(4, ri, _fmt_price(row[u"unit_price"]))
            _place(5, ri, _fmt_price(row[u"total_price"]))
            _place(6, ri, u"{:.0f}%".format(row[u"confidence"] * 100))

        total_row = len(matched_rows) + 2
        _place(0, total_row,     u'סה"כ פריטים')
        _place(3, total_row,     str(sum(r[u"count"] for r in matched_rows)))
        _place(0, total_row + 1, u'סה"כ עלות (₪)')
        _place(5, total_row + 1, u"₪ {:,.2f}".format(grand_total))

        t.Commit()
    except Exception:
        t.RollBack()
        raise

    return legend_view


# =============================================================================
#  MAIN
# =============================================================================

def main():
    out = script.get_output()
    out.close_others()
    doc = revit.doc

    # ── 0. API key check ──────────────────────────────────────────────────────
    if not ANTHROPIC_API_KEY:
        forms.alert(
            u"לא נמצא Anthropic API key.\n\n"
            u"צור קובץ:\n"
            u"%APPDATA%\\pyRevit\\Extensions\\"
            u"EasyBIMTools.extension\\EasyBIM_config.json\n\n"
            u'תוכן:\n{ "anthropic_api_key": "sk-ant-..." }',
            title=u"חסר API Key", warn_icon=True
        )
        script.exit()
        return

    # ── 1. CAD link selection ─────────────────────────────────────────────────
    import_inst = None
    try:
        sel = revit.get_selection()
        for elem in sel.elements:
            if isinstance(elem, ImportInstance):
                import_inst = elem
                break
    except Exception:
        pass

    if import_inst is None:
        cad_links = _get_cad_links(doc)
        if not cad_links:
            forms.alert(u"לא נמצאו קישורי CAD.", warn_icon=True)
            script.exit()
            return
        label_map = {lbl: elem for lbl, elem in cad_links}
        choice = forms.SelectFromList.show(
            sorted(label_map.keys()),
            title=u"בחר קובץ CAD",
            button_name=u"סרוק",
            multiselect=False,
        )
        if not choice:
            script.exit()
            return
        chosen = choice[0] if isinstance(choice, (list, tuple)) else choice
        import_inst = label_map.get(chosen)

    if import_inst is None:
        forms.alert(u"לא נבחר קובץ.", warn_icon=True)
        script.exit()
        return

    # ── 2. DWG path ───────────────────────────────────────────────────────────
    try:
        dwg_path = _get_dwg_path(import_inst, doc)
    except RuntimeError as exc:
        forms.alert(str(exc), title=u"שגיאת נתיב", warn_icon=True)
        script.exit()
        return

    out.print_html(
        u"<p style='direction:rtl;color:#555;font-family:Arial;'>"
        u"⏳ קורא: <b>{}</b></p>".format(os.path.basename(dwg_path))
    )

    # ── 3. Count blocks ───────────────────────────────────────────────────────
    try:
        raw_counts, engine = _count_blocks(dwg_path)
    except Exception as exc:
        forms.alert(u"שגיאת קריאת DWG:\n{}".format(exc), warn_icon=True)
        script.exit()
        return

    if not raw_counts:
        forms.alert(u"לא נמצאו בלוקים בקובץ.", warn_icon=True)
        script.exit()
        return

    # ── 4. Render block images ────────────────────────────────────────────────
    block_images_result = _render_block_images(dwg_path, out)
    if isinstance(block_images_result, tuple):
        block_images, rendered_counts = block_images_result
    else:
        block_images, rendered_counts = block_images_result, {}

    # Merge counts — acdbmgd counts are authoritative
    if rendered_counts and not raw_counts:
        raw_counts = rendered_counts

    # ── 5. Claude Vision matching ─────────────────────────────────────────────
    vision_matches = {}
    vision_err     = None
    try:
        if block_images:
            vision_matches = _vision_match_batch(block_images, out)
        else:
            out.print_html(
                u"<p style='direction:rtl;color:#e67e22;font-family:Arial;'>"
                u"⚠ לא הופקו תמונות — בדוק ש-matplotlib ו-ezdxf מותקנים.</p>"
            )
    except Exception as exc:
        vision_err = u"{}".format(exc)
        out.print_html(
            u"<p style='direction:rtl;color:#c62828;font-family:Arial;'>"
            u"⚠ Claude Vision נכשל: <code>{}</code></p>".format(vision_err)
        )

    # ── 6. Build BOQ ──────────────────────────────────────────────────────────
    rows, unmapped, grand_total = _build_boq(raw_counts, vision_matches)

    # ── 7. Export CSV ─────────────────────────────────────────────────────────
    try:
        csv_path = _export_csv(rows, unmapped, grand_total, dwg_path, engine)
    except Exception as exc:
        forms.alert(u"שגיאת שמירת CSV:\n{}".format(exc), warn_icon=True)
        script.exit()
        return

    # ── 8. Export schematic screenshot ───────────────────────────────────────
    schematic_b64 = None
    try:
        schematic_b64 = _export_schematic_screenshot(doc, import_inst)
    except Exception:
        pass

    # ── 9. Revit Legend ───────────────────────────────────────────────────────
    schedule_view = None
    schedule_err  = None
    try:
        schedule_view = _create_revit_schedule(
            doc, rows, unmapped, grand_total, dwg_path)
    except Exception as exc:
        schedule_err = u"{}".format(exc)

    # ── 10. Output window ─────────────────────────────────────────────────────
    _print_output(out, rows, unmapped, grand_total, dwg_path, engine,
                  csv_path, schematic_b64, block_images)

    if schedule_view:
        out.print_html(
            u"<p style='direction:rtl;color:#2e8b57;font-family:Arial;'>"
            u"📐 Legend נוצר: <b>{}</b></p>".format(schedule_view.Name)
        )
    elif schedule_err:
        out.print_html(
            u"<p style='direction:rtl;color:#e67e22;font-family:Arial;'>"
            u"⚠ Legend לא נוצר: <code>{}</code></p>".format(schedule_err)
        )

    # ── 11. TaskDialog ────────────────────────────────────────────────────────
    matched_rows = [r for r in rows if r[u"matched"]]
    td = TaskDialog(u"BOQ Vision הושלם ✓")
    td.MainContent = (
        u"ניתוח ויזואלי הושלם!\n\n"
        u"📄 {csv}\n"
        u"🤖 זוהו ע\"י Claude: {matched}/{total} בלוקים\n"
        u"💰 עלות כוללת: ₪ {grand:,.2f}".format(
            csv=csv_path,
            matched=len(matched_rows),
            total=len(raw_counts),
            grand=grand_total,
        )
    )
    td.CommonButtons = TaskDialogCommonButtons.Ok
    td.Show()


# =============================================================================
try:
    main()
except Exception as _top_exc:
    _out = script.get_output()
    _out.print_html(
        u"<pre style='color:red;direction:ltr;'>"
        u"UNHANDLED EXCEPTION:\n{}</pre>".format(traceback.format_exc())
    )
    forms.alert(
        u"שגיאה לא צפויה – ראה חלון הפלט.\n\n{}".format(str(_top_exc)),
        title=u"שגיאה", warn_icon=True
    )
