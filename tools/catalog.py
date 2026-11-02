"""Firmware catalog: product model code -> firmware file + programmer settings.

A catalog is a plain folder:

    <folder>/catalog.csv            one row per product model (opens in Excel)
    <folder>/<family>/<file>        the firmware files, copied in when a model is added

Share it by copying the folder, or as a single .zip (export_zip / import_zip).
Each row stores the CRC32 of its file, so a file that was replaced or edited by hand is refused.
"""

import csv
import datetime as dt
import io
import os
import re
import shutil
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, fields as dc_fields

CATALOG_CSV = "catalog.csv"
COLUMNS = ["model", "client", "description", "family", "chip", "version", "file", "file_crc", "settings", "updated"]
FAMILY_KEYS = ("mm32", "fortior", "renesas")

# Programmer settings that belong to a product model. The start mode, auto-detect settle time and the
# SWD / FICE timing depend on the station and fixture, so they stay with the programmer.
MODEL_SETTINGS = {
    "mm32": ("verify", "run", "erase", "protect"),
    "fortior": ("verify", "run", "opt"),
    "renesas": ("verify", "run", "erase", "reset"),
}
SETTING_VALUES = {
    "verify": ("0", "1"), "run": ("0", "1"), "protect": ("0", "1"), "reset": ("0", "1"),
    "erase": {"mm32": ("chip", "pages"), "renesas": ("chip", "blocks")},
}


def default_dir():
    return os.path.join(os.path.expanduser("~"), "Documents", "Firmware Catalog")


def normalize(code):
    """Model codes compare case-insensitively and ignore spaces (barcode labels vary)."""
    return re.sub(r"\s+", "", code or "").upper()


def sort_key(m):
    """Natural order: CF1_10001 before CF10_10001, CF2 before CF10."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", m.key)]


def file_crc(path):
    with open(path, "rb") as f:
        return zlib.crc32(f.read())


def parse_settings(text):
    out = {}
    for item in (text or "").split():
        if "=" in item:
            key, value = item.split("=", 1)
            out[key.strip().lower()] = value.strip()
    return out


def format_settings(settings):
    return " ".join(f"{k}={v}" for k, v in settings.items())


def check_settings(family, settings):
    """Raise ValueError if a setting does not belong to the family or has an invalid value."""
    allowed = MODEL_SETTINGS.get(family, ())
    for key, value in settings.items():
        if key not in allowed:
            raise ValueError(f"setting '{key}' does not apply to {family} (allowed: {', '.join(allowed)})")
        if key == "opt":
            if not re.fullmatch(r"[0-9A-Fa-f]{8}", value):
                raise ValueError(f"opt must be 8 hex digits, e.g. 00F000FF (got '{value}')")
            continue
        choices = SETTING_VALUES[key]
        if isinstance(choices, dict):
            choices = choices[family]
        if value not in choices:
            raise ValueError(f"{key} must be {' or '.join(choices)} (got '{value}')")


@dataclass
class Model:
    model: str
    description: str = ""
    family: str = "mm32"
    chip: str = ""
    version: str = ""
    file: str = ""        # path relative to the catalog folder, with forward slashes
    file_crc: str = ""    # CRC32 of the file, 8 hex digits
    settings: str = ""    # "key=value key=value"
    updated: str = ""
    client: str = ""      # client prefix from the file name, e.g. "wt"

    @property
    def key(self):
        return normalize(self.model)

    def settings_dict(self):
        return parse_settings(self.settings)

    def image_name(self):
        """The name stored on the programmer, so it shows which model it holds (max 47 chars, no spaces)."""
        name = f"{self.client}_{self.model}" if self.client else self.model
        if self.version:
            name += f"_v{self.version}"
        return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:47]

    def label(self):
        name = f"{self.model}  ({self.client})" if self.client else self.model
        return f"{name} — {self.description}" if self.description else name


def _safe_rel(rel):
    """A catalog-relative path that cannot escape the folder (catalogs come from other PCs)."""
    rel = (rel or "").replace("\\", "/").strip()
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or ":" in rel or rel.startswith("/"):
        raise ValueError(f"invalid file path in catalog: '{rel}'")
    return "/".join(parts)


def _read_rows(f):
    models = []
    for row in csv.DictReader(f):
        code = (row.get("model") or "").strip()
        if not code:
            continue
        models.append(Model(**{c: (row.get(c) or "").strip() for c in COLUMNS}))
    return models


def load(folder):
    """The models in a catalog folder ([] if it has no catalog yet)."""
    path = os.path.join(folder, CATALOG_CSV)
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8-sig") as f:  # -sig: Excel saves a BOM
        return _read_rows(f)


def save(folder, models):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, CATALOG_CSV)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for m in sorted(models, key=sort_key):
            w.writerow({c.name: getattr(m, c.name) for c in dc_fields(Model)})
    os.replace(tmp, path)


def find(models, code):
    key = normalize(code)
    # The label carries the model code; "<client>_<model>" is accepted too.
    return next((m for m in models if m.key == key), None) or \
        next((m for m in models if m.client and normalize(f"{m.client}_{m.model}") == key), None)


def firmware_path(folder, m):
    """The model's firmware file, checked against its CRC. Raises ValueError if missing or changed."""
    path = os.path.join(folder, *_safe_rel(m.file).split("/"))
    if not os.path.exists(path):
        raise ValueError(f"firmware file for {m.model} is missing: {path}")
    crc = f"{file_crc(path):08X}"
    if crc != m.file_crc.upper():
        raise ValueError(f"firmware file for {m.model} was changed (CRC {crc}, catalog says {m.file_crc}). "
                         f"Add the model again to accept the new file.")
    return path


def _copy_in(folder, m, src_path):
    """Check the model and copy its firmware into the catalog folder (sets file, file_crc, updated)."""
    if m.family not in FAMILY_KEYS:
        raise ValueError(f"unknown chip family '{m.family}'")
    if not normalize(m.model):
        raise ValueError("enter a model code")
    check_settings(m.family, m.settings_dict())
    ext = os.path.splitext(src_path)[1].lower() or ".bin"
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", m.image_name())
    dst = os.path.join(folder, m.family, stem + ext)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.abspath(src_path) != os.path.abspath(dst):
        shutil.copyfile(src_path, dst)
    m.file = f"{m.family}/{stem}{ext}"
    m.file_crc = f"{file_crc(dst):08X}"
    m.updated = dt.date.today().isoformat()


def add(folder, models, m, src_path):
    """Copy the firmware into the catalog and add or replace the model. Returns the new model list."""
    return add_many(folder, models, [(m, src_path)])


def add_many(folder, models, items):
    """add() for a list of (Model, firmware path); the catalog is written once at the end."""
    for m, src in items:
        _copy_in(folder, m, src)
    keys = {m.key for m, _ in items}
    replaced = [x for x in models if x.key in keys]
    models = [x for x in models if x.key not in keys] + [m for m, _ in items]
    save(folder, models)
    _drop_unused_files(folder, models, replaced)
    return models


def _drop_unused_files(folder, models, gone):
    """Delete the files of removed / replaced models that no remaining model uses."""
    used = {m.file for m in models}
    for m in gone:
        if m.file and m.file not in used:
            try:
                os.remove(os.path.join(folder, *_safe_rel(m.file).split("/")))
            except (OSError, ValueError):
                pass


# Bulk import file names: <client prefix>_<model code>_v<version>.<ext>, e.g. wt_cf36_10001_v1.1.3.hex
# (client wt, model cf36_10001, version 1.1.3). The client ends at the first "_"; the version starts at the
# last "_v" followed by a digit, so model codes may contain "_" and "-" themselves.
NAME_RE = re.compile(r"^(?P<client>[^_]+)_(?P<model>.+)_[vV](?P<version>\d[0-9A-Za-z.\-]*)$")


def parse_filename(path):
    """(client, model code, version) from a firmware file name, or None if it does not follow the convention."""
    m = NAME_RE.match(os.path.splitext(os.path.basename(path))[0].strip())
    if not m or not normalize(m.group("model")):
        return None
    return m.group("client").strip(), m.group("model").strip(), m.group("version")


def version_key(version):
    """Sort key so 1.10 is newer than 1.9."""
    return [int(n) for n in re.findall(r"\d+", version)]


def same_entry(old, client, family, chip, version, settings, crc):
    return (old.client.lower(), old.family, old.chip, old.version.lower(), old.settings, old.file_crc.upper()) == \
        (client.lower(), family, chip, version.lower(), settings, crc.upper())


def remove(folder, models, m):
    """Remove a model from the list. Its file stays unless no other model uses it."""
    models = [x for x in models if x.key != m.key]
    save(folder, models)
    _drop_unused_files(folder, models, [m])
    return models


def export_zip(folder, models, zip_path):
    """Write the catalog and every firmware file it uses into one .zip. Returns the number of models."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(folder, CATALOG_CSV), CATALOG_CSV)
        for rel in sorted({m.file for m in models}):
            rel = _safe_rel(rel)
            zf.write(os.path.join(folder, *rel.split("/")), rel)
    return len(models)


def read_zip(zip_path):
    """The models in an exported catalog .zip."""
    with zipfile.ZipFile(zip_path) as zf:
        if CATALOG_CSV not in zf.namelist():
            raise ValueError(f"{os.path.basename(zip_path)} is not a firmware catalog (no {CATALOG_CSV} inside)")
        with zf.open(CATALOG_CSV) as raw:
            text = raw.read().decode("utf-8-sig")
    return _read_rows(io.StringIO(text))


def compare(models, incoming):
    """(new, updated, unchanged) model lists for an import."""
    new, updated, same = [], [], []
    for m in incoming:
        old = find(models, m.model)
        if old is None:
            new.append(m)
        elif (old.file_crc, old.version, old.settings, old.chip, old.family, old.description, old.client) == \
                (m.file_crc, m.version, m.settings, m.chip, m.family, m.description, m.client):
            same.append(m)
        else:
            updated.append(m)
    return new, updated, same


def import_zip(folder, models, zip_path):
    """Merge an exported catalog into this one: new models are added, models with the same code are
    replaced by the imported version. Every file is checked against its CRC before anything changes."""
    incoming = read_zip(zip_path)
    with zipfile.ZipFile(zip_path) as zf, tempfile.TemporaryDirectory() as tmp:
        staged = []
        for m in incoming:
            if m.family not in FAMILY_KEYS:
                raise ValueError(f"{m.model}: unknown chip family '{m.family}'")
            check_settings(m.family, m.settings_dict())
            rel = _safe_rel(m.file)
            try:
                data = zf.read(rel)
            except KeyError:
                raise ValueError(f"{m.model}: {rel} is missing from the .zip") from None
            if f"{zlib.crc32(data):08X}" != m.file_crc.upper():
                raise ValueError(f"{m.model}: {rel} does not match its CRC in the catalog")
            src = os.path.join(tmp, f"{len(staged)}.bin")
            with open(src, "wb") as f:
                f.write(data)
            staged.append((m, rel, src))
        for m, rel, src in staged:
            dst = os.path.join(folder, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            m.file = rel
    keys = {m.key for m in incoming}
    replaced = [x for x in models if x.key in keys]
    models = [x for x in models if x.key not in keys] + incoming
    save(folder, models)
    _drop_unused_files(folder, models, replaced)
    return models
