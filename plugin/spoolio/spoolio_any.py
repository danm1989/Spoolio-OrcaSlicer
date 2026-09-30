# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Spoolio"
# description = "A Bambu Lab inspired inventory management overview for OrcaSlicer"
# author = "Dan J Moore"
# version = "0.1.0"
# ///

import html
import json
import logging
import platform
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import orca

PLUGIN_NAME = "Spoolio"
PLUGIN_VERSION = "0.1.0"

DEFAULT_SPOOLMAN_URL = "http://raspberrypi:7912"
DEFAULT_LOW_FILAMENT_THRESHOLD = 100  # grams

FEEDBACK_URL = "https://github.com/danm1989/spoolio-orcaslicer/issues"
LATEST_RELEASE_API = "https://api.github.com/repos/danm1989/spoolio-orcaslicer/releases/latest"

# webbrowser can only open a URL; it can't use the browser's default search engine.
SEARCH_URL = "https://www.google.com/search?q={query}"

SETTINGS_FILENAME = "spoolio_settings.json"
LOG_FILENAME = "spoolio.log"
LOG_MAX_BYTES = 256 * 1024

REQUEST_TIMEOUT = 5  # seconds
MAX_QUERY_LENGTH = 200
REFRESH_INTERVAL_SECONDS = 60
MAIN_WINDOW_SIZE = (380, 600)
SETTINGS_WINDOW_SIZE = (560, 820)

PLUGIN_DIR = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """Folder for settings and the log, inside OrcaSlicer's data folder.

    Kept out of the plugin folder so updating or reinstalling the plugin doesn't reset them.
    """
    for parent in PLUGIN_DIR.parents:
        if parent.name == "OrcaSlicer":
            return parent / "spoolio"
    return PLUGIN_DIR


DATA_DIR = _data_dir()
SETTINGS_FILE = DATA_DIR / SETTINGS_FILENAME
LOG_FILE = DATA_DIR / LOG_FILENAME
# Previous location, still read as a fallback.
LEGACY_SETTINGS_FILE = PLUGIN_DIR / SETTINGS_FILENAME

log = logging.getLogger("spoolio")
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(logging.NullHandler())


def setup_logging(path: Path | None = None) -> None:
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return
    try:
        path = path or LOG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)


_reported = {}


def _report(key: str, error: str | None, exc_info: bool = False) -> None:
    """Log a failure once, then its recovery, so the 60-second internal refresh can't flood the log."""
    if error is None:
        if _reported.pop(key, None) is not None:
            log.info("%s recovered", key)
    elif _reported.get(key) != error:
        _reported[key] = error
        log.warning("%s failed: %s", key, error, exc_info=exc_info)


def get_settings() -> dict:
    defaults = {"spoolman_url": "", "low_stock_grams": DEFAULT_LOW_FILAMENT_THRESHOLD}
    for path in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
        try:
            return {**defaults, **json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            continue
    return defaults


def parse_low_stock(value: object) -> float:
    try:
        grams = float(value)
    except (TypeError, ValueError):
        return DEFAULT_LOW_FILAMENT_THRESHOLD
    if grams < 0:
        return DEFAULT_LOW_FILAMENT_THRESHOLD
    return int(grams) if grams == int(grams) else grams


def save_settings(settings: dict) -> bool:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return True
    except OSError:
        log.exception("Could not write %s", SETTINGS_FILE)
        return False


def fetch_spools(spoolman_url: str) -> dict:
    """Return ``{"ok": True, "spools": [...]}`` or ``{"ok": False, "error": "..."}``."""
    url = f"{spoolman_url.rstrip('/')}/api/v1/spool"
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        error = f"Could not reach Spoolman at {spoolman_url} ({exc.reason})"
        _report("Spool list", error)
        return {"ok": False, "error": error}
    except Exception as exc:
        _report("Spool list", str(exc), exc_info=True)
        return {"ok": False, "error": str(exc)}
    _report("Spool list", None)
    return {"ok": True, "spools": data}


def normalize_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/")


def fetch_spoolman_info(spoolman_url: str) -> dict:
    """Ask Spoolman for its version, which doubles as a connection test.

    Returns ``{"ok": True, "version": "..."}`` or ``{"ok": False, "error": "..."}``.
    """
    if not spoolman_url:
        return {"ok": False, "error": "No Spoolman URL configured yet"}
    url = f"{spoolman_url.rstrip('/')}/api/v1/info"
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        error = f"Could not reach Spoolman at {spoolman_url} ({exc.reason})"
        log.warning("Connection test failed: %s", error)
        return {"ok": False, "error": error}
    except Exception as exc:
        log.exception("Connection test failed")
        return {"ok": False, "error": str(exc)}
    version = data.get("version", "unknown")
    log.info("Connected to Spoolman %s at %s", version, spoolman_url)
    return {"ok": True, "version": version}


def is_newer(latest: str, current: str) -> bool:
    def parts(version):
        return tuple(int(n) for n in re.findall(r"\d+", version)[:3])

    return parts(latest) > parts(current)


def fetch_latest_release() -> dict:
    """Return ``{"ok": True, "version": ..., "url": ...}`` or ``{"ok": False, "error": ...}``."""
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Spoolio/{PLUGIN_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            error = "No published release found yet"
        else:
            error = f"GitHub returned HTTP {exc.code}"
    except urllib.error.URLError as exc:
        error = f"Could not reach GitHub ({exc.reason})"
    except Exception as exc:
        log.exception("Update check failed")
        return {"ok": False, "error": str(exc)}
    else:
        version = str(data.get("tag_name", "")).lstrip("v")
        log.info("Latest release is %s", version)
        return {"ok": True, "version": version, "url": data.get("html_url", "")}
    log.warning("Update check failed: %s", error)
    return {"ok": False, "error": error}


LOGO_DATA_URI = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNTYg"
    "MjU2Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImItdGUiIHgxPSIwIiB5MT0iMCIgeDI9IjEi"
    "IHkyPSIxIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiM0QkUzQkMiLz48c3RvcCBvZmZz"
    "ZXQ9IjEiIHN0b3AtY29sb3I9IiMxN0E5OEEiLz48L2xpbmVhckdyYWRpZW50PjxsaW5lYXJHcmFk"
    "aWVudCBpZD0iYi1mbCIgeDE9IjAiIHkxPSIwIiB4Mj0iMSIgeTI9IjEiPjxzdG9wIG9mZnNldD0i"
    "MCIgc3RvcC1jb2xvcj0iI0VFRjBGNCIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzhG"
    "OTlBOCIvPjwvbGluZWFyR3JhZGllbnQ+PGxpbmVhckdyYWRpZW50IGlkPSJiLWh1YiIgeDE9IjAi"
    "IHkxPSIwIiB4Mj0iMSIgeTI9IjEiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzQ1NEU1"
    "RCIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzFFMjMyQiIvPjwvbGluZWFyR3JhZGll"
    "bnQ+PGxpbmVhckdyYWRpZW50IGlkPSJiLWdyIiB4MT0iMCIgeTE9IjAiIHgyPSIxIiB5Mj0iMCI+"
    "PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjNUVFQUI0Ii8+PHN0b3Agb2Zmc2V0PSIxIiBz"
    "dG9wLWNvbG9yPSIjMTBCOTgxIi8+PC9saW5lYXJHcmFkaWVudD48L2RlZnM+PGcgdHJhbnNmb3Jt"
    "PSJyb3RhdGUoMTM1IDEyOCAxMjgpIiBmaWxsPSJub25lIiBzdHJva2Utd2lkdGg9IjEyIiBzdHJv"
    "a2UtbGluZWNhcD0icm91bmQiPjxjaXJjbGUgY3g9IjEyOCIgY3k9IjEyOCIgcj0iMTEyIiBzdHJv"
    "a2U9IiM4RTk4QTgiIHN0cm9rZS1vcGFjaXR5PSIuMyIgc3Ryb2tlLWRhc2hhcnJheT0iNTI3Ljgg"
    "NzAzLjciLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIxMjgiIHI9IjExMiIgc3Ryb2tlPSJ1cmwoI2It"
    "Z3IpIiBzdHJva2UtZGFzaGFycmF5PSIzODAuMCA3MDMuNyIvPjwvZz48Y2lyY2xlIGN4PSIxMjgi"
    "IGN5PSIxMjgiIHI9Ijg0IiBmaWxsPSJ1cmwoI2ItZmwpIi8+PGNpcmNsZSBjeD0iMTI4IiBjeT0i"
    "MTI4IiByPSI4MCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utb3BhY2l0eT0iLjQ1"
    "IiBzdHJva2Utd2lkdGg9IjIiLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIxMjgiIHI9IjU0IiBmaWxs"
    "PSJub25lIiBzdHJva2U9InVybCgjYi10ZSkiIHN0cm9rZS13aWR0aD0iNDAiLz48Y2lyY2xlIGN4"
    "PSIxMjgiIGN5PSIxMjgiIHI9IjQ1LjIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzAwMCIgc3Ryb2tl"
    "LW9wYWNpdHk9Ii4xMyIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIx"
    "MjgiIHI9IjYyLjgiIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzAwMCIgc3Ryb2tlLW9wYWNpdHk9Ii4x"
    "MyIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIxMjgiIHI9Ijc0LjAi"
    "IGZpbGw9Im5vbmUiIHN0cm9rZT0iIzAwMCIgc3Ryb2tlLW9wYWNpdHk9Ii4yIiBzdHJva2Utd2lk"
    "dGg9IjIuNSIvPjxwYXRoIGZpbGwtcnVsZT0iZXZlbm9kZCIgZmlsbD0idXJsKCNiLWh1YikiIGQ9"
    "Ik05NCAxMjggQTM0IDM0IDAgMSAwIDE2MiAxMjggQTM0IDM0IDAgMSAwIDk0IDEyOCBaIE0xMTQg"
    "MTI4IEExNCAxNCAwIDEgMCAxNDIgMTI4IEExNCAxNCAwIDEgMCAxMTQgMTI4IFoiLz48Y2lyY2xl"
    "IGN4PSIxMjgiIGN5PSIxMjgiIHI9IjE0IiBmaWxsPSJub25lIiBzdHJva2U9IiMwMDAiIHN0cm9r"
    "ZS1vcGFjaXR5PSIuNDUiIHN0cm9rZS13aWR0aD0iMiIvPjwvc3ZnPg=="
)

LOGO_IMG = f'<img class="logo" src="{LOGO_DATA_URI}" alt="">'


def _fill(template: str, **values: object) -> str:
    """Substitute ``__NAME__`` placeholders.

    Plain substitution rather than an f-string or str.format, so the CSS and
    JavaScript in the templates keep their normal single braces.
    """
    return re.sub(r"__([A-Z][A-Z_]*)__", lambda match: str(values[match.group(1)]), template)


# Filtering, sorting and grouping run client-side on the last spool list pushed in.
MAIN_PAGE_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="icon" href="__LOGODATA__">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * { font-family: Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", "Helvetica Neue", Helvetica, Arial, sans-serif; }
  body { margin: 0; padding: 12px; font-size: 13px; }
  .header { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
  .header .title { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; }
  .header img.logo { width: 32px; height: 32px; flex-shrink: 0; }
  .header-actions { display: flex; gap: 6px; flex-shrink: 0; }
  h2 { font-size: 14px; margin: 0; }
  #status { color: var(--orca-muted); margin-bottom: 8px; }
  #status.error { color: #d9534f; }
  button {
    padding: 4px 10px; cursor: pointer;
    border: 1px solid var(--orca-border); border-radius: 4px;
    background: transparent; color: var(--orca-fg);
  }
  button:hover { border-color: var(--orca-accent); }
  .filters { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .filters input, .filters select {
    flex: 1; min-width: 110px; padding: 4px 6px;
    border: 1px solid var(--orca-border); border-radius: 4px;
    background: var(--orca-bg); color: var(--orca-fg);
  }
  .filters select option {
    background: var(--orca-bg); color: var(--orca-fg);
  }
  .sort-dir { flex: 0 0 auto; min-width: 32px; }
  .empty { color: var(--orca-muted); padding: 12px 4px; }

  .spool-card {
    position: relative;
    background: var(--orca-border);
    background: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%);
    border: 1px solid var(--orca-border);
    border-radius: 14px;
    padding: 13px 14px 11px;
    margin-bottom: 9px;
  }
  .spool-tag {
    position: absolute; top: -8px; left: 14px;
    display: flex; align-items: center; gap: 4px;
    max-width: calc(100% - 28px);
    padding: 2px 8px 2px 6px;
    border-radius: 4px 9px 9px 0;
    background: var(--orca-accent);
    background: linear-gradient(135deg, color-mix(in srgb, var(--orca-accent) 80%, black), var(--orca-accent));
    color: var(--orca-accent-fg);
    font-size: 10px; font-weight: 700; letter-spacing: 0.02em;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
  }
  .spool-tag span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .spool-tag svg { width: 9px; height: 9px; flex-shrink: 0; }
  .spool-body { display: flex; gap: 11px; align-items: flex-start; }
  .spool-swatch-col {
    display: flex; flex-direction: column; align-items: center; gap: 4px;
    flex-shrink: 0; padding-top: 1px;
  }
  .spool-swatch {
    width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0;
    border: 2px solid rgba(255, 255, 255, 0.1);
    box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.2);
  }
  .spool-rfid {
    display: flex; align-items: center; gap: 3px; flex-shrink: 0;
    padding: 2px 5px; border-radius: 5px; color: #3a3d42;
    background: linear-gradient(135deg, #f4f5f7 0%, #cdd1d6 35%, #8b9099 55%, #cdd1d6 75%, #f4f5f7 100%);
    border: 1px solid rgba(0, 0, 0, 0.25);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.55), inset 0 -1px 0 rgba(0, 0, 0, 0.15), 0 1px 2px rgba(0, 0, 0, 0.25);
  }
  .spool-rfid span { font-size: 8px; font-weight: 800; letter-spacing: 0.04em; }
  .spool-rfid svg { width: 11px; height: 11px; flex-shrink: 0; }
  .spool-main { flex: 1; min-width: 0; }
  .spool-top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .spool-title {
    font-size: 14px; font-weight: 700; color: var(--orca-fg);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .spool-weight { font-size: 11px; font-weight: 400; color: var(--orca-fg); flex-shrink: 0; display: flex; align-items: center; gap: 6px; }
  .spool-cart {
    display: inline-flex; align-items: center; justify-content: center;
    padding: 2px 4px; cursor: pointer; border-radius: 5px; line-height: 0;
    background: transparent; color: #e0a030; border: 1px solid currentColor;
  }
  .spool-cart:hover { background: rgba(224, 160, 48, 0.18); }
  .spool-cart svg { width: 13px; height: 13px; }
  .spool-bar-track {
    height: 5px; border-radius: 3px; margin: 6px 0 5px;
    background: rgba(127, 127, 127, 0.25); overflow: hidden;
  }
  .spool-bar-fill { height: 100%; border-radius: 3px; }
  .spool-subtitle {
    font-size: 12px; color: var(--orca-muted); font-style: italic;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  .group { margin-bottom: 4px; }
  .group-card {
    display: flex; align-items: center; gap: 10px; cursor: pointer;
    background: var(--orca-border);
    background: color-mix(in srgb, var(--orca-fg) 8%, var(--orca-bg) 92%);
    border: 1px solid var(--orca-border);
    border-radius: 10px; padding: 8px 12px; margin-bottom: 10px;
  }
  .group-card .arrow { width: 12px; flex-shrink: 0; color: var(--orca-muted); }
  .group-card .group-info { flex: 1; min-width: 0; }
  .group-card .group-name { font-weight: 700; font-size: 13px; }
  .group-card .group-meta { color: var(--orca-muted); font-size: 11px; margin-top: 1px; }
  .group-card .group-bar { width: 90px; flex-shrink: 0; }
  .group-card .group-bar-track { height: 5px; border-radius: 3px; background: rgba(127, 127, 127, 0.25); overflow: hidden; }
  .group-card .group-bar-fill { height: 100%; border-radius: 3px; }
  .group-card .group-bar-fill.high { background: #3fb950; }
  .group-card .group-bar-fill.mid { background: #d29922; }
  .group-card .group-bar-fill.low { background: #d9534f; }
  .group-items { padding-left: 4px; }
  .group.collapsed .group-items { display: none; }
</style>
</head>
<body>
  <div class="header">
    <div class="title">__LOGO__<h2>__PLUGIN_NAME__</h2></div>
    <div class="header-actions">
      <button id="settings-btn" title="Settings &amp; about">Settings</button>
    </div>
  </div>
  <div id="status">Loading...</div>

  <div class="filters">
    <input id="search" type="text" placeholder="Filter by name...">
    <select id="material-filter"><option value="">All materials</option></select>
    <select id="vendor-filter"><option value="">All manufacturers</option></select>
  </div>
  <div class="filters">
    <select id="group-key">
      <option value="none">Group: None</option>
      <option value="location">Group: Location</option>
      <option value="material">Group: Material</option>
      <option value="vendor">Group: Manufacturer</option>
    </select>
    <select id="sort-key">
      <option value="name">Sort: Name</option>
      <option value="material">Sort: Material</option>
      <option value="vendor">Sort: Manufacturer</option>
      <option value="remaining" selected>Sort: Remaining weight</option>
      <option value="used">Sort: Used weight</option>
      <option value="first_used">Sort: First used</option>
      <option value="last_used">Sort: Last used</option>
    </select>
    <button class="sort-dir" id="sort-dir" title="Toggle ascending/descending">&#9650;</button>
  </div>

  <div id="list"></div>

  <script>
    const statusEl = document.getElementById("status");
    const listEl = document.getElementById("list");
    const searchEl = document.getElementById("search");
    const materialEl = document.getElementById("material-filter");
    const vendorEl = document.getElementById("vendor-filter");
    const groupKeyEl = document.getElementById("group-key");
    const sortKeyEl = document.getElementById("sort-key");
    const sortDirBtn = document.getElementById("sort-dir");

    let allSpools = [];
    let lowStockGrams = __LOW_STOCK_DEFAULT__;
    let sortDir = "asc";

    function populateFilterOptions() {
      const materials = new Set();
      const vendors = new Set();
      allSpools.forEach(function (s) {
        const filament = s.filament || {};
        const vendor = filament.vendor || {};
        if (filament.material) materials.add(filament.material);
        if (vendor.name) vendors.add(vendor.name);
      });
      function fillOptions(select, values, currentValue) {
        const placeholder = select.options[0];
        select.innerHTML = "";
        select.appendChild(placeholder);
        [...values].sort().forEach(function (value) {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = value;
          select.appendChild(option);
        });
        select.value = values.has(currentValue) ? currentValue : "";
      }
      fillOptions(materialEl, materials, materialEl.value);
      fillOptions(vendorEl, vendors, vendorEl.value);
    }

    function progressClass(percent) {
      if (percent === null) return "mid";
      if (percent >= 50) return "high";
      if (percent >= 20) return "mid";
      return "low";
    }

    function formatWeight(grams) {
      if (typeof grams !== "number") return "?";
      return Math.abs(grams) >= 1000
        ? (grams / 1000).toFixed(2) + " kg"
        : Math.round(grams) + " g";
    }

    function tagUids(s) {
      const tags = s.tags;
      if (!Array.isArray(tags)) return [];
      return tags.map(function (t) {
        return typeof t === "string" ? t : (t && (t.uid || t.id)) || null;
      }).filter(Boolean);
    }

    function sortValue(s, key) {
      const filament = s.filament || {};
      const vendor = filament.vendor || {};
      switch (key) {
        case "name": return (filament.name || "").toLowerCase() || null;
        case "material": return (filament.material || "").toLowerCase() || null;
        case "vendor": return (vendor.name || "").toLowerCase() || null;
        case "remaining": return (typeof s.remaining_weight === "number") ? s.remaining_weight : null;
        case "used": return (typeof s.used_weight === "number") ? s.used_weight : null;
        case "first_used": return s.first_used ? Date.parse(s.first_used) : null;
        case "last_used": return s.last_used ? Date.parse(s.last_used) : null;
        default: return null;
      }
    }

    // Matches how a spool missing this field (e.g. never used yet, for
    // "last used") should sort: always to the end, not to the front on desc.
    function compareSpools(a, b, key, dir) {
      const av = sortValue(a, key);
      const bv = sortValue(b, key);
      const aNull = av === null || av === undefined;
      const bNull = bv === null || bv === undefined;
      if (aNull && bNull) return 0;
      if (aNull) return 1;
      if (bNull) return -1;
      if (av < bv) return dir === "asc" ? -1 : 1;
      if (av > bv) return dir === "asc" ? 1 : -1;
      return 0;
    }

    function originalWeight(s) {
      const filament = s.filament || {};
      return (s.initial_weight !== undefined && s.initial_weight !== null)
        ? s.initial_weight : filament.weight;
    }

    function spoolRowHtml(s) {
      const filament = s.filament || {};
      const vendor = filament.vendor || {};
      const color = filament.color_hex ? "#" + filament.color_hex.replace(/^#/, "") : "#888";
      const name = filament.name || ("Spool #" + s.id);
      const material = filament.material || "";
      const vendorName = vendor.name || "";
      const subtitleParts = [];
      if (material) subtitleParts.push(material);
      if (filament.color_hex) subtitleParts.push("#" + filament.color_hex.replace(/^#/, "").toUpperCase());
      if (typeof filament.diameter === "number") subtitleParts.push(filament.diameter + "mm");
      const subtitle = subtitleParts.join(" \\u00b7 ");
      const tags = tagUids(s);
      const original = originalWeight(s);
      const remaining = s.remaining_weight;
      let percent = null;
      if (typeof original === "number" && original > 0 && typeof remaining === "number") {
        percent = Math.max(0, Math.min(100, Math.round((remaining / original) * 100)));
      }
      const remainingLabel = formatWeight(remaining);
      const percentTitle = percent === null ? "Remaining unknown" : percent + "% remaining";
      const tagHtml = vendorName
        ? '<div class="spool-tag">' +
            '<svg viewBox="0 0 12 12" fill="currentColor">' +
              '<rect x="0" y="0" width="5" height="5" rx="1"></rect>' +
              '<rect x="7" y="0" width="5" height="5" rx="1"></rect>' +
              '<rect x="0" y="7" width="5" height="5" rx="1"></rect>' +
              '<rect x="7" y="7" width="5" height="5" rx="1"></rect>' +
            '</svg>' +
            '<span>' + vendorName + '</span>' +
          '</div>'
        : '';
      const searchQuery = [vendorName, s.lot_nr || name].filter(Boolean).join(" ");
      const cartHtml = (typeof remaining === "number" && remaining < lowStockGrams)
        ? '<button class="spool-cart" data-q="' + encodeURIComponent(searchQuery) + '" ' +
            'title="Low stock - search online for ' + searchQuery.replace(/"/g, "") + '">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
              '<circle cx="9" cy="20" r="1.4"></circle><circle cx="18" cy="20" r="1.4"></circle>' +
              '<path d="M2 3h3l2.4 12.2a1.5 1.5 0 0 0 1.5 1.2h8.6a1.5 1.5 0 0 0 1.5-1.1L21 8H6"></path>' +
            '</svg></button>'
        : '';
      const rfidHtml = tags.length
        ? '<div class="spool-rfid" title="RFID: ' + tags.join(", ") + '">' +
            '<span>RFID</span>' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">' +
              '<circle cx="5" cy="19" r="1.8" fill="currentColor" stroke="none"></circle>' +
              '<path d="M5,14 A5,5 0 0 1 10,19"></path>' +
              '<path d="M5,10.5 A8.5,8.5 0 0 1 13.5,19"></path>' +
              '<path d="M5,7 A12,12 0 0 1 17,19"></path>' +
            '</svg>' +
          '</div>'
        : '';
      return (
        '<div class="spool-card">' +
          tagHtml +
          '<div class="spool-body">' +
            '<div class="spool-swatch-col">' +
              '<div class="spool-swatch" style="background:' + color + '"></div>' +
              rfidHtml +
            '</div>' +
            '<div class="spool-main">' +
              '<div class="spool-top">' +
                '<div class="spool-title">' + name + '</div>' +
                '<div class="spool-weight">' + remainingLabel + cartHtml + '</div>' +
              '</div>' +
              '<div class="spool-bar-track" title="' + percentTitle + '">' +
                '<div class="spool-bar-fill" style="width:' + (percent === null ? 0 : percent) + '%;background:' + color + '"></div>' +
              '</div>' +
              '<div class="spool-subtitle">' + subtitle + '</div>' +
            '</div>' +
          '</div>' +
        '</div>'
      );
    }

    function groupKeyFor(s, mode) {
      const filament = s.filament || {};
      const vendor = filament.vendor || {};
      if (mode === "material") {
        const label = filament.material || "Unknown material";
        return { key: label, label: label };
      }
      if (mode === "vendor") {
        const label = vendor.name || "Unknown manufacturer";
        return { key: label, label: label };
      }
      const label = s.location || "No location";
      return { key: label, label: label };
    }

    function groupRowHtml(group, collapsed) {
      let totalOriginal = 0, totalRemaining = 0, hasOriginal = false, hasRemaining = false;
      group.items.forEach(function (s) {
        const original = originalWeight(s);
        if (typeof original === "number") { totalOriginal += original; hasOriginal = true; }
        if (typeof s.remaining_weight === "number") { totalRemaining += s.remaining_weight; hasRemaining = true; }
      });
      const percent = (hasOriginal && hasRemaining && totalOriginal > 0)
        ? Math.max(0, Math.min(100, Math.round((totalRemaining / totalOriginal) * 100))) : null;
      const remainingLabel = hasRemaining ? formatWeight(totalRemaining) : "?";
      const percentLabel = percent === null ? "?" : percent + "%";
      const count = group.items.length + " spool" + (group.items.length === 1 ? "" : "s");
      const subLabel = [group.sub, count].filter(Boolean).join(" \\u00b7 ");
      return (
        '<div class="group' + (collapsed ? " collapsed" : "") + '" data-key="' + group.key + '">' +
          '<div class="group-card">' +
            '<div class="arrow">' + (collapsed ? "\\u25b8" : "\\u25be") + '</div>' +
            '<div class="group-info"><div class="group-name">' + group.label + '</div><div class="group-meta">' + subLabel + '</div></div>' +
            '<div class="group-bar">' +
              '<div class="group-meta" style="text-align:right;margin-bottom:2px;">' + remainingLabel + ' \\u00b7 ' + percentLabel + '</div>' +
              '<div class="group-bar-track"><div class="group-bar-fill ' + progressClass(percent) +
                '" style="width:' + (percent === null ? 0 : percent) + '%"></div></div>' +
            '</div>' +
          '</div>' +
          '<div class="group-items">' + group.items.map(spoolRowHtml).join("") + '</div>' +
        '</div>'
      );
    }

    const collapsedGroups = new Set();

    function renderGrouped(spools, mode) {
      const groups = new Map();
      spools.forEach(function (s) {
        const info = groupKeyFor(s, mode);
        if (!groups.has(info.key)) groups.set(info.key, { key: info.key, label: info.label, sub: info.sub, items: [] });
        groups.get(info.key).items.push(s);
      });
      const ordered = [...groups.values()].sort(function (a, b) {
        if (mode === "location" && b.items.length !== a.items.length) {
          return b.items.length - a.items.length;
        }
        return a.label.toLowerCase().localeCompare(b.label.toLowerCase());
      });
      listEl.innerHTML = ordered.map(function (group) {
        return groupRowHtml(group, collapsedGroups.has(group.key));
      }).join("");
      listEl.querySelectorAll(".group-card").forEach(function (header) {
        header.addEventListener("click", function () {
          const groupEl = header.closest(".group");
          const key = groupEl.dataset.key;
          if (collapsedGroups.has(key)) collapsedGroups.delete(key); else collapsedGroups.add(key);
          groupEl.classList.toggle("collapsed");
          header.querySelector(".arrow").innerHTML = collapsedGroups.has(key) ? "&#9656;" : "&#9662;";
        });
      });
    }

    function applyFilters() {
      const query = searchEl.value.trim().toLowerCase();
      const material = materialEl.value;
      const vendor = vendorEl.value;
      const filtered = allSpools.filter(function (s) {
        const filament = s.filament || {};
        const vendorObj = filament.vendor || {};
        if (material && filament.material !== material) return false;
        if (vendor && vendorObj.name !== vendor) return false;
        if (query) {
          const haystack = [filament.name, filament.material, vendorObj.name]
            .filter(Boolean).join(" ").toLowerCase();
          if (!haystack.includes(query)) return false;
        }
        return true;
      });
      statusEl.className = "";
      statusEl.textContent = filtered.length + " of " + allSpools.length + " spool" +
        (allSpools.length === 1 ? "" : "s");
      filtered.sort(function (a, b) {
        return compareSpools(a, b, sortKeyEl.value, sortDir);
      });
      if (filtered.length === 0) {
        listEl.innerHTML = '<div class="empty">No spools match this filter.</div>';
        return;
      }
      const groupMode = groupKeyEl.value;
      if (groupMode === "none") {
        listEl.innerHTML = filtered.map(spoolRowHtml).join("");
      } else {
        renderGrouped(filtered, groupMode);
      }
    }

    function render(payload) {
      if (!payload.ok) {
        statusEl.textContent = payload.error || "Unknown error";
        statusEl.className = "error";
        listEl.innerHTML = "";
        return;
      }
      allSpools = payload.spools || [];
      if (typeof payload.low_stock_grams === "number") lowStockGrams = payload.low_stock_grams;
      populateFilterOptions();
      applyFilters();
    }

    orca.onMessage(function (data) {
      if (data && data.type === "spools") {
        render(data);
      }
    });

    listEl.addEventListener("click", function (e) {
      const btn = e.target.closest(".spool-cart");
      if (btn) {
        orca.postMessage({ type: "order", query: decodeURIComponent(btn.dataset.q) });
      }
    });

    searchEl.addEventListener("input", applyFilters);
    materialEl.addEventListener("change", applyFilters);
    vendorEl.addEventListener("change", applyFilters);
    groupKeyEl.addEventListener("change", applyFilters);
    sortKeyEl.addEventListener("change", applyFilters);
    sortDirBtn.addEventListener("click", function () {
      sortDir = sortDir === "asc" ? "desc" : "asc";
      sortDirBtn.innerHTML = sortDir === "asc" ? "&#9650;" : "&#9660;";
      applyFilters();
    });

    document.getElementById("settings-btn").addEventListener("click", function () {
      orca.postMessage({ type: "settings" });
    });

    // Ask for data on load rather than relying on a push landing in time.
    orca.postMessage({ type: "ready" });

    setInterval(function () {
      orca.postMessage({ type: "refresh" });
    }, __REFRESH_MS__);
  </script>
</body>
</html>
"""

SETTINGS_PAGE_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="icon" href="__LOGODATA__">
<style>
  * { font-family: Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", "Helvetica Neue", Helvetica, Arial, sans-serif; }
  html, body { height: 100%; }
  body {
    margin: 0; padding: 24px 28px; box-sizing: border-box; font-size: 13px;
    display: flex; flex-direction: column; line-height: 1.4;
    background: var(--orca-bg); color: var(--orca-fg);
  }
  .header {
    display: flex; align-items: center; gap: 12px;
    padding-bottom: 14px; margin-bottom: 18px;
    border-bottom: 1px solid var(--orca-border);
  }
  .header img.logo { width: 52px; height: 52px; flex-shrink: 0; }
  h2 { margin: 0; font-size: 20px; font-weight: 700; color: var(--orca-accent); }
  .step { display: flex; gap: 12px; margin-bottom: 16px; }
  .step-num {
    flex-shrink: 0; width: 22px; height: 22px; margin-top: 1px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: var(--orca-accent); color: var(--orca-accent-fg); font-size: 12px; font-weight: 700;
  }
  .step h4 { margin: 0 0 3px 0; font-size: 14px; font-weight: 700; }
  .step p { margin: 0; color: var(--orca-muted); }
  .step strong { color: var(--orca-fg); }
  .divider { border-top: 1px solid var(--orca-border); margin: 4px 0 16px 0; }
  h3 { margin: 0 0 8px 0; font-size: 14px; font-weight: 700; color: var(--orca-accent); }
  h3 .emoji { margin-right: 6px; font-size: 22px; vertical-align: -3px; display: inline-block; }
  label { display: block; margin-bottom: 8px; }
  input {
    width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 13px;
    background: var(--orca-bg); color: var(--orca-fg);
    border: 1px solid var(--orca-border); border-radius: 6px;
  }
  input:focus { outline: none; border-color: var(--orca-accent); }
  .status { margin-top: 8px; font-size: 12px; }
  .divider.spaced { margin-top: 18px; }
  .hint { margin-top: 6px; font-size: 12px; color: var(--orca-muted); }
  .status.ok { color: #3fb950; }
  .status.error { color: #d9534f; }
  .footer {
    margin-top: auto; padding-top: 14px; display: flex; align-items: center;
    gap: 10px; justify-content: flex-end; border-top: 1px solid var(--orca-border);
  }
  .about { margin-top: 16px; font-size: 11px; color: var(--orca-muted); line-height: 1.7; }
  .about .logpath { word-break: break-all; user-select: text; }
  .version-row { margin-right: auto; min-width: 0; font-size: 11px; line-height: 1.7; color: var(--orca-muted); }
  .header h2 { flex: 1; min-width: 0; }
  button {
    padding: 9px 18px; cursor: pointer; border-radius: 6px; font-size: 13px;
    font-weight: 700; border: 1px solid transparent;
    background: transparent; color: var(--orca-fg);
  }
  button.primary { background: var(--orca-accent); color: var(--orca-accent-fg); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .url-row { display: flex; gap: 8px; }
  .url-row input { flex: 1; min-width: 0; }
  button.test { border-color: var(--orca-border); white-space: nowrap; }
  .status.pending { color: var(--orca-muted); }
  button.primary:hover { filter: brightness(1.08); }
  button.icon-btn { padding: 6px; line-height: 0; }
  button.link { padding: 0 0 0 6px; border: none; font-size: 11px; font-weight: 400; text-decoration: underline; }
  /* The host's own button:hover styling would otherwise repaint these; keep them static. */
  button.icon-btn, button.icon-btn:hover, button.icon-btn:focus, button.icon-btn:active {
    color: var(--orca-accent-fg) !important; background: var(--orca-accent) !important;
    border-color: var(--orca-accent) !important; box-shadow: none !important;
  }
  button.link, button.link:hover, button.link:focus, button.link:active {
    color: var(--orca-accent) !important; background: none !important; box-shadow: none !important;
  }
</style>
</head>
<body>
  <div class="header">
    __LOGO__<h2>__PLUGIN_NAME__ Settings</h2>
    <button class="icon-btn" id="feedback" title="Send feedback or report a bug" aria-label="Send feedback">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path></svg>
    </button>
  </div>

  <div class="step">
    <div class="step-num">1</div>
    <div>
      <h4>Connect to Spoolman</h4>
      <p>Enter the address of your self-hosted Spoolman server below. The plugin only
      <strong>reads</strong> your spool list from Spoolman and doesn't make any changes.</p>
    </div>
  </div>
  <div class="step">
    <div class="step-num">2</div>
    <div>
      <h4>Browse your Spools</h4>
      <p>Each card shows the filament color, remaining weight, and an RFID badge where a
      tag is linked. Filter, sort, or group the list by material, manufacturer, or location.</p>
    </div>
  </div>
  <div class="divider"></div>

  <h3><span class="emoji">🛒</span>Configure Filament Reorder</h3>
  <label for="low-stock" class="field-label">Low stock warning (grams):</label>
  <input id="low-stock" type="number" min="0" step="10" value="__LOW_STOCK__">
  <div class="hint">Spools with this much filament remaining show a cart button for reordering.</div>

  <div class="divider spaced"></div>

  <h3><span class="emoji">🔌</span>Configure Spoolman Server</h3>
  <label for="url">Enter the URL of your self-hosted Spoolman server:</label>
  <div class="url-row">
    <input id="url" type="text" value="__URL__" placeholder="__DEFAULT_URL__">
    <button class="test" id="test">Test</button>
  </div>
  <div class="status __STATUS_CLASS__" id="conn-status">__STATUS_TEXT__</div>

  <div class="about">
    <div class="logpath">Log file: __LOG_PATH__</div>
  </div>

  <div class="footer">
    <div class="version-row">
      <span>Plugin version __VERSION__</span>
      <button class="link" id="check-update">Check for updates</button>
      <span id="update-status"></span>
    </div>
    <button id="cancel">Cancel</button>
    <button class="primary" id="save" disabled>Save &amp; Close</button>
  </div>
  <script>
    document.getElementById("cancel").addEventListener("click", function () {
      orca.postMessage({ type: "cancel" });
    });
    const urlEl = document.getElementById("url");
    const saveEl = document.getElementById("save");
    const statusEl = document.getElementById("conn-status");
    const norm = function (u) { return u.trim().replace(/\\/+$/, ""); };

    // Save stays disabled until the URL has passed a test; a saved URL that
    // is already connected counts as passed so other settings can change alone.
    let verifiedUrl = __VERIFIED_URL__;
    let verifiedText = statusEl.textContent;

    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = "status " + cls;
    }
    function refreshSave() {
      const url = norm(urlEl.value);
      saveEl.disabled = !(url && url === verifiedUrl);
    }

    urlEl.addEventListener("input", function () {
      if (norm(urlEl.value) === verifiedUrl) {
        setStatus(verifiedText, "ok");
      } else {
        setStatus("Press Test to check the connection before saving.", "pending");
      }
      refreshSave();
    });

    document.getElementById("test").addEventListener("click", function () {
      setStatus("Testing connection...", "pending");
      orca.postMessage({ type: "test", url: urlEl.value.trim() });
    });

    document.getElementById("feedback").addEventListener("click", function () {
      orca.postMessage({ type: "feedback" });
    });
    const updateEl = document.getElementById("update-status");
    document.getElementById("check-update").addEventListener("click", function () {
      updateEl.textContent = "Checking...";
      orca.postMessage({ type: "check_update" });
    });
    function showUpdate(data) {
      updateEl.textContent = "";
      if (!data.ok) {
        updateEl.textContent = data.error;
      } else if (data.newer) {
        const link = document.createElement("button");
        link.className = "link";
        link.textContent = "Version " + data.version + " is available - view release";
        link.addEventListener("click", function () { orca.postMessage({ type: "release" }); });
        updateEl.appendChild(link);
      } else {
        updateEl.textContent = "You're up to date.";
      }
    }

    orca.onMessage(function (data) {
      if (data && data.type === "update_result") {
        showUpdate(data);
        return;
      }
      if (!data || data.type !== "test_result") return;
      if (data.ok) {
        verifiedUrl = norm(data.url || "");
        verifiedText = "Connected - Spoolman v" + data.version;
        if (norm(urlEl.value) === verifiedUrl) setStatus(verifiedText, "ok");
      } else {
        setStatus(data.error || "Connection failed", "error");
      }
      refreshSave();
    });

    // Confirms the message listener above is registered before Python sends
    // anything unprompted; see _on_settings_message's "ready" handler.
    orca.postMessage({ type: "ready" });

    saveEl.addEventListener("click", function () {
      const url = urlEl.value.trim();
      const lowStock = document.getElementById("low-stock").value;
      orca.postMessage({ type: "save", url: url, low_stock: lowStock });
    });
    refreshSave();
  </script>
</body>
</html>
"""


def render_page() -> str:
    return _fill(
        MAIN_PAGE_TEMPLATE,
        LOGO=LOGO_IMG,
        LOGODATA=LOGO_DATA_URI,
        PLUGIN_NAME=PLUGIN_NAME,
        LOW_STOCK_DEFAULT=DEFAULT_LOW_FILAMENT_THRESHOLD,
        REFRESH_MS=REFRESH_INTERVAL_SECONDS * 1000,
    )


def render_settings_dialog(current_url: str, spoolman_info: dict, low_stock_grams: float) -> str:
    """Return the HTML for the Settings & About window.

    ``spoolman_info`` describes the saved URL, not whatever is typed in the field.
    """
    verified_url = normalize_url(current_url) if spoolman_info.get("ok") else ""
    if spoolman_info.get("ok"):
        status_text = f"Connected - Spoolman v{spoolman_info['version']}"
        status_class = "ok"
    elif spoolman_info.get("pending"):
        status_text = spoolman_info.get("error", "Checking connection...")
        status_class = "pending"
    else:
        status_text = spoolman_info.get("error", "Not connected")
        status_class = "error"
    return _fill(
        SETTINGS_PAGE_TEMPLATE,
        LOGO=LOGO_IMG,
        LOGODATA=LOGO_DATA_URI,
        PLUGIN_NAME=PLUGIN_NAME,
        VERSION=PLUGIN_VERSION,
        DEFAULT_URL=html.escape(DEFAULT_SPOOLMAN_URL),
        URL=html.escape(current_url or DEFAULT_SPOOLMAN_URL),
        LOW_STOCK=low_stock_grams,
        LOG_PATH=html.escape(str(LOG_FILE)),
        STATUS_TEXT=html.escape(status_text),
        STATUS_CLASS=status_class,
        # "</" is escaped so a URL can't close the script tag.
        VERIFIED_URL=json.dumps(verified_url).replace("</", "<\\/"),
    )


class SpoolioWindow(orca.script.ScriptPluginCapabilityBase):
    def __init__(self):
        super().__init__()
        self._panel = None
        self._settings_window = None
        self._verified_url = ""
        self._latest_release_url = ""
        self._refresh_in_flight = False
        self._test_in_flight = False
        self._update_check_in_flight = False

    def get_name(self):
        return PLUGIN_NAME

    def _spoolman_url(self):
        return get_settings().get("spoolman_url", "")

    def _save_settings(self, url, low_stock):
        settings = get_settings()
        settings["spoolman_url"] = url
        settings["low_stock_grams"] = parse_low_stock(low_stock)
        if save_settings(settings):
            log.info("Settings saved (url=%s, low stock=%s g)", url, settings["low_stock_grams"])
            return True
        orca.host.ui.message(
            f"Could not write settings to {SETTINGS_FILE}. Check that this folder is writable.",
            title=PLUGIN_NAME,
            icon="error",
        )
        return False

    def _settings_saved(self):
        self._open_panel()
        self._push_data()

    def _open_settings_window(self):
        if self._settings_window is not None and self._settings_window.is_open():
            return
        saved_url = self._spoolman_url()
        self._settings_window = orca.host.ui.create_window(
            html=render_settings_dialog(
                saved_url,
                spoolman_info={"ok": False, "pending": True},
                low_stock_grams=parse_low_stock(get_settings().get("low_stock_grams")),
            ),
            title=f"{PLUGIN_NAME} - Settings & About",
            width=SETTINGS_WINDOW_SIZE[0],
            height=SETTINGS_WINDOW_SIZE[1],
            on_message=self._on_settings_message,
            on_close=self._on_settings_close,
        )

    def _check_connection(self, url, window):
        if self._test_in_flight:
            return
        self._test_in_flight = True

        def worker():
            info = fetch_spoolman_info(url)
            self._test_in_flight = False
            if info.get("ok"):
                self._verified_url = normalize_url(url)
            if window.is_open():
                window.post({"type": "test_result", "url": url, **info})

        threading.Thread(target=worker, daemon=True).start()

    def _on_settings_message(self, data):
        msg_type = data.get("type") if isinstance(data, dict) else None
        if msg_type == "ready":
            # Only check the connection once the page confirms its message
            # listener is registered, so the result can never arrive too early.
            if self._settings_window is not None:
                self._check_connection(self._spoolman_url(), self._settings_window)
        elif msg_type == "test":
            url = (data.get("url") or "").strip()
            if self._settings_window is not None:
                self._check_connection(url, self._settings_window)
        elif msg_type == "save":
            # The page already gates Save; enforce it here too.
            url = (data.get("url") or "").strip()
            if normalize_url(url) != self._verified_url:
                if self._settings_window is not None:
                    self._settings_window.post({
                        "type": "test_result",
                        "ok": False,
                        "url": url,
                        "error": "Test the connection successfully before saving.",
                    })
                return
            if self._save_settings(url, data.get("low_stock")):
                self._settings_saved()
                if self._settings_window is not None:
                    self._settings_window.close()
        elif msg_type == "feedback":
            self._open_url(FEEDBACK_URL)
        elif msg_type == "check_update":
            if self._update_check_in_flight or self._settings_window is None:
                return
            self._update_check_in_flight = True
            window = self._settings_window

            def worker():
                result = fetch_latest_release()
                self._update_check_in_flight = False
                if result["ok"]:
                    self._latest_release_url = result["url"]
                    result["newer"] = is_newer(result["version"], PLUGIN_VERSION)
                if window.is_open():
                    window.post({"type": "update_result", **result})

            threading.Thread(target=worker, daemon=True).start()
        elif msg_type == "release" and self._latest_release_url.startswith("https://"):
            self._open_url(self._latest_release_url)
        elif msg_type == "cancel" and self._settings_window is not None:
            self._settings_window.close()

    def _on_settings_close(self):
        self._settings_window = None

    def on_unload(self):
        # Without this, open windows would outlive OrcaSlicer's shutdown.
        log.info("Unloading")
        if self._panel is not None and self._panel.is_open():
            self._panel.close()
        if self._settings_window is not None and self._settings_window.is_open():
            self._settings_window.close()

    def on_message(self, data):
        msg_type = data.get("type") if isinstance(data, dict) else None
        if msg_type in ("ready", "refresh"):
            self._push_data()
        elif msg_type == "settings":
            self._open_settings_window()
        elif msg_type == "order":
            self._open_search(data.get("query"))

    def _open_search(self, query):
        if not isinstance(query, str) or not query.strip():
            return
        terms = urllib.parse.quote_plus(query.strip()[:MAX_QUERY_LENGTH])
        self._open_url(SEARCH_URL.format(query=terms))

    def _open_url(self, url):
        log.info("Opening %s", url)
        try:
            webbrowser.open(url)
        except Exception as exc:
            log.exception("Could not open the browser")
            orca.host.ui.message(
                f"Could not open the browser: {exc}", title=PLUGIN_NAME, icon="error"
            )

    def _push_data(self):
        if self._panel is None or not self._panel.is_open():
            return
        url = self._spoolman_url()
        if not url:
            self._panel.post({
                "type": "spools",
                "ok": False,
                "error": "No Spoolman URL configured yet - click Settings to set one.",
            })
            return
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        panel = self._panel
        low_stock_grams = parse_low_stock(get_settings().get("low_stock_grams"))

        def worker():
            result = fetch_spools(url)
            self._refresh_in_flight = False
            if panel.is_open():
                panel.post({"type": "spools", "low_stock_grams": low_stock_grams, **result})

        threading.Thread(target=worker, daemon=True).start()

    def _on_panel_close(self):
        self._panel = None

    def _open_panel(self):
        if self._panel is not None and self._panel.is_open():
            self._push_data()
            return
        self._panel = orca.host.ui.create_window(
            html=render_page(),
            title=PLUGIN_NAME,
            width=MAIN_WINDOW_SIZE[0],
            height=MAIN_WINDOW_SIZE[1],
            on_message=self.on_message,
            on_close=self._on_panel_close,
        )

    def _open(self):
        if not self._spoolman_url():
            self._open_settings_window()
            return
        self._open_panel()

    def on_load(self):
        setup_logging()
        log.info(
            "%s %s loaded (Python %s, %s)",
            PLUGIN_NAME,
            PLUGIN_VERSION,
            platform.python_version(),
            platform.platform(),
        )
        self._open()

    def execute(self):
        self._open()
        return orca.ExecutionResult.success(f"Opened {PLUGIN_NAME}")


@orca.plugin
class SpoolioPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(SpoolioWindow)
