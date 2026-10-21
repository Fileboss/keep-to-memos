#!/usr/bin/env python3
"""Convertit un export Google Takeout (Keep) en archive « Memos Export Format » 1.0.

L'archive produite s'importe dans Memos 0.31 depuis Paramètres → Export & Import.
Le format suit docs/design/memos-export-format.md et core/memoexport du dépôt
usememos/memos (tag v0.31.0) ; le script n'appelle jamais Memos.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import struct
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "1.0.0"

# Limite de contenu par défaut d'une instance Memos (store.DefaultContentLengthLimit).
DEFAULT_CONTENT_LIMIT = 8 * 1024

VISIBILITIES = {"private": "PRIVATE", "protected": "PROTECTED", "public": "PUBLIC"}

# memoexport.mediaTypePattern
MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)

# memoexport.reservedWindowsNames
RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
}


# --- Conversion d'une note (fonctions pures) ---------------------------------


def _str(note: dict, key: str) -> str:
    value = note.get(key)
    return value if isinstance(value, str) else ""


def _dicts(note: dict, key: str) -> list[dict]:
    value = note.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def normalize_tag(name: str) -> str | None:
    """Transforme un libellé Keep en tag Memos, ou None s'il ne reste rien.

    Garde les caractères XID_Continue (lettres de toutes écritures, chiffres, _)
    plus - et /, ce qui est un sous-ensemble de la grammaire des tags Memos
    (markdown/parser/tag.go). Un segment vide entre deux / couperait le tag.
    """
    name = unicodedata.normalize("NFC", name).lower()
    name = "-".join(name.split())
    kept = "".join(c for c in name if c in "-_/" or ("_" + c).isidentifier())
    kept = re.sub(r"/+", "/", re.sub(r"-+", "-", kept)).strip("/-")
    return kept or None


def build_tags(note: dict, extra_tag: str | None) -> list[str]:
    names = [_str(label, "name") for label in _dicts(note, "labels")]
    if extra_tag:
        names.append(extra_tag)
    tags: list[str] = []
    for name in names:
        tag = normalize_tag(name)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def note_body(note: dict) -> str:
    """Titre, texte et liste : le contenu propre de la note, sans liens ni tags."""
    blocks = []
    title = _one_line(_str(note, "title"))
    if title:
        blocks.append(f"# {title}")
    text = _str(note, "textContent").rstrip()
    if text.strip():
        blocks.append(text)
    items = []
    for item in _dicts(note, "listContent"):
        item_text = _one_line(_str(item, "text"))
        if item_text:
            box = "x" if item.get("isChecked") is True else " "
            items.append(f"- [{box}] {item_text}")
    if items:
        blocks.append("\n".join(items))
    return "\n\n".join(blocks)


def _link_label(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def note_to_markdown(note: dict, extra_tag: str | None = "keep") -> str:
    body = note_body(note)
    blocks = [body] if body else []
    links = []
    for annotation in _dicts(note, "annotations"):
        url = _str(annotation, "url").strip()
        if url and url not in body and not any(f"({url})" in link for link in links):
            label = _one_line(_str(annotation, "title")) or url
            links.append(f"- [{_link_label(label)}]({url})")
    if links:
        blocks.append("\n".join(links))
    tags = build_tags(note, extra_tag)
    if tags:
        blocks.append(" ".join(f"#{tag}" for tag in tags))
    return "\n\n".join(blocks)


def is_empty(note: dict) -> bool:
    """Ni titre, ni corps, ni pièce jointe (les libellés seuls ne comptent pas)."""
    return not note_body(note) and not _dicts(note, "attachments")


def usec_to_unix(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        usec = int(value)
    except ValueError:
        return None
    return usec // 1_000_000 if usec > 0 else None


def format_time(unix: int) -> str:
    """memoexport.FormatTime : RFC 3339 en UTC, à la seconde."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(unix))


def _uid(prefix: str, key: str) -> str:
    digest = hashlib.sha256(unicodedata.normalize("NFC", key).encode("utf-8")).hexdigest()
    return prefix + digest[:31]


def memo_uid(json_name: str) -> str:
    """UID déterministe : réimporter la même archive ne crée pas de doublon."""
    return _uid("k", json_name)


def attachment_uid(json_name: str, file_path: str) -> str:
    return _uid("a", f"{json_name}/{file_path}")


def safe_filename(filename: str) -> str:
    """Port de memoexport.SafeFilename (core/memoexport/path.go)."""
    chars = []
    for c in filename:
        if c in "/\\:" or ord(c) < 0x20 or ord(c) == 0x7F or c == "�":
            chars.append("_")
        else:
            chars.append(c)
    safe = "".join(chars).strip(" .")
    if not safe:
        return "file"
    if safe.split(".", 1)[0].upper() in RESERVED_WINDOWS_NAMES:
        return "_" + safe
    return safe


def media_type(declared: str, filename: str) -> str:
    if MEDIA_TYPE_RE.match(declared):
        return declared
    guessed = mimetypes.guess_type(filename)[0]
    if guessed and MEDIA_TYPE_RE.match(guessed):
        return guessed
    return "application/octet-stream"


# --- Lecture du Takeout -------------------------------------------------------


def load_notes(keep_dir: Path) -> tuple[list[tuple[str, dict]], list[tuple[str, str]]]:
    """Lit les .json du dossier, triés par date de création. Renvoie (notes, erreurs)."""
    notes, errors = [], []
    for path in sorted(keep_dir.glob("*.json")):
        try:
            note = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append((path.name, f"JSON invalide ({exc})"))
            continue
        if not isinstance(note, dict):
            errors.append((path.name, "JSON invalide (un objet est attendu)"))
            continue
        notes.append((path.name, note))
    notes.sort(key=lambda item: (usec_to_unix(item[1].get("createdTimestampUsec")) or 0, item[0]))
    return notes, errors


def find_attachment(keep_dir: Path, file_path: str) -> Path | None:
    """Retrouve le fichier joint ; tolère une extension différente (.jpeg/.jpg)."""
    name = Path(file_path).name
    if not name:
        return None
    candidate = keep_dir / name
    if candidate.is_file():
        return candidate
    stem = Path(name).stem
    for other in sorted(keep_dir.iterdir()):
        if other.stem == stem and other.suffix.lower() not in (".json", ".html") and other.is_file():
            return other
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- Construction des records -------------------------------------------------


@dataclass
class Options:
    visibility: str = "PRIVATE"
    tag: str | None = "keep"
    username: str = "keep"
    attachments: bool = True
    content_limit: int = DEFAULT_CONTENT_LIMIT


@dataclass
class Prepared:
    source: str
    uid: str
    content: str
    record: dict
    files: list[tuple[str, Path]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ignored_attachments: int = 0


def prepare_note(keep_dir: Path, json_name: str, note: dict, opts: Options, export_time: int) -> Prepared:
    uid = memo_uid(json_name)
    content = note_to_markdown(note, opts.tag)
    created = usec_to_unix(note.get("createdTimestampUsec"))
    updated = usec_to_unix(note.get("userEditedTimestampUsec"))
    create_time = format_time(created or updated or export_time)
    update_time = format_time(updated or created or export_time)
    archived = note.get("isArchived") is True or note.get("isTrashed") is True
    record = {
        "uid": uid,
        "creator": opts.username,
        "createTime": create_time,
        "updateTime": update_time,
        "state": "ARCHIVED" if archived else "NORMAL",
        "visibility": opts.visibility,
        "pinned": note.get("isPinned") is True,
        "contentPath": f"memos/{uid}.md",
    }
    prepared = Prepared(json_name, uid, content, record)

    size = len(content.encode("utf-8"))
    if size > opts.content_limit:
        prepared.warnings.append(
            f"contenu de {size} octets > {opts.content_limit} : refusé par Memos "
            "sauf si la limite de l'instance a été relevée"
        )

    entries = _dicts(note, "attachments")
    if not opts.attachments:
        prepared.ignored_attachments = len(entries)
        return prepared
    attachments = []
    for entry in entries:
        file_path = _str(entry, "filePath")
        found = find_attachment(keep_dir, file_path)
        if found is None:
            prepared.warnings.append(f"pièce jointe introuvable : {file_path or '(sans nom)'}")
            continue
        att_uid = attachment_uid(json_name, file_path)
        archive_path = f"attachments/{att_uid}/{safe_filename(found.name)}"
        attachments.append({
            "uid": att_uid,
            "filename": found.name,
            "type": media_type(_str(entry, "mimetype"), found.name),
            "size": found.stat().st_size,
            "sha256": _sha256_file(found),
            "path": archive_path,
            "createTime": create_time,
        })
        prepared.files.append((archive_path, found))
    if attachments:
        record["attachments"] = attachments
    return prepared


# --- Écriture de l'archive ----------------------------------------------------


def _zipinfo(name: str, mtime: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=time.gmtime(mtime)[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 0
    # Champ « extended timestamp » (0x5455), comme les exports de Memos.
    info.extra = struct.pack("<HHBl", 0x5455, 5, 1, mtime)
    return info


def _json_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_manifest(username: str, export_time: int, prepared: list[Prepared]) -> dict:
    return {
        "format": "memos-export",
        "formatVersion": "1.0",
        "generator": {"name": "keep-to-memos", "version": VERSION},
        "exportTime": format_time(export_time),
        "scope": {"kind": "USER", "user": {"username": username}},
        "counts": {"memos": len(prepared), "attachments": sum(len(p.files) for p in prepared)},
    }


def write_archive(output: Path, manifest: dict, prepared: list[Prepared], export_time: int) -> None:
    tmp = output.with_name(output.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w") as zf:
            zf.writestr(_zipinfo("manifest.json", export_time), _json_bytes(manifest))
            for p in prepared:
                zf.writestr(_zipinfo(f"memos/{p.uid}.json", export_time), _json_bytes(p.record))
                zf.writestr(_zipinfo(p.record["contentPath"], export_time), p.content.encode("utf-8"))
                for archive_path, source in p.files:
                    with source.open("rb") as src, zf.open(_zipinfo(archive_path, export_time), "w") as dst:
                        shutil.copyfileobj(src, dst)
        os.replace(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)


# --- Ligne de commande --------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convertit un export Google Takeout (Keep) en archive importable dans Memos 0.31.",
    )
    parser.add_argument("keep_dir", type=Path, metavar="TAKEOUT_KEEP_DIR", help="dossier Takeout/Keep décompressé")
    parser.add_argument("-o", "--output", type=Path, help="archive produite (défaut : memos-export-<username>-<horodatage>.zip)")
    parser.add_argument("--visibility", choices=sorted(VISIBILITIES), default="private", help="visibilité des memos (défaut : private)")
    parser.add_argument("--tag", default="keep", help='tag ajouté à toutes les notes (défaut : keep, "" pour désactiver)')
    parser.add_argument("--include-trashed", action="store_true", help="inclut la corbeille (en état archivé)")
    archived = parser.add_mutually_exclusive_group()
    archived.add_argument("--include-archived", dest="skip_archived", action="store_false", help="inclut les notes archivées (défaut)")
    archived.add_argument("--skip-archived", dest="skip_archived", action="store_true", help="ignore les notes archivées")
    parser.set_defaults(skip_archived=False)
    parser.add_argument("--no-attachments", dest="attachments", action="store_false", help="n'inclut pas les pièces jointes")
    parser.add_argument("--username", default="keep", help="nom d'utilisateur inscrit dans l'archive (défaut : keep) ; "
                        "sans effet sur le propriétaire des memos, qui est toujours le compte qui importe")
    parser.add_argument("--limit", type=int, metavar="N", help="ne garde que les N premières notes (par date de création)")
    parser.add_argument("--dry-run", action="store_true", help="affiche le markdown de chaque note sans écrire d'archive")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit doit être un entier positif")
    if not args.username.strip():
        parser.error("--username ne peut pas être vide")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    keep_dir: Path = args.keep_dir
    if not keep_dir.is_dir():
        print(f"Dossier introuvable : {keep_dir}", file=sys.stderr)
        return 2

    export_time = int(time.time())
    opts = Options(
        visibility=VISIBILITIES[args.visibility],
        tag=args.tag or None,
        username=args.username.strip(),
        attachments=args.attachments,
    )
    notes, errors = load_notes(keep_dir)
    if not notes and not errors:
        print(f"Aucune note .json dans {keep_dir}", file=sys.stderr)
        return 2

    for name, reason in errors:
        print(f"[fail] {name} → {reason}")

    prepared: list[Prepared] = []
    skipped = {"vide": 0, "corbeille": 0, "archivée": 0}
    for name, note in notes:
        if args.limit is not None and len(prepared) >= args.limit:
            break
        if note.get("isTrashed") is True and not args.include_trashed:
            reason = "corbeille"
        elif note.get("isArchived") is True and args.skip_archived:
            reason = "archivée"
        elif is_empty(note):
            reason = "vide"
        else:
            reason = None
        if reason:
            skipped[reason] += 1
            print(f"[skip] {name} → {reason}")
            continue
        p = prepare_note(keep_dir, name, note, opts, export_time)
        prepared.append(p)
        details = list(p.warnings)
        if p.ignored_attachments:
            details.append(f"{p.ignored_attachments} pièce(s) jointe(s) ignorée(s)")
        status = "warn" if p.warnings else "ok"
        suffix = f" ({' ; '.join(details)})" if details else ""
        print(f"[{status}] {name} → {p.uid}{suffix}")
        if args.dry_run:
            print(f"{'-' * 60}\n{p.content}\n{'-' * 60}")

    attachments_included = sum(len(p.files) for p in prepared)
    attachments_missing = sum(1 for p in prepared for w in p.warnings if w.startswith("pièce jointe introuvable"))
    attachments_ignored = sum(p.ignored_attachments for p in prepared)
    warned = sum(1 for p in prepared if p.warnings)
    print()
    print(f"Notes lues : {len(notes) + len(errors)} (JSON invalides : {len(errors)})")
    print(f"Notes incluses : {len(prepared)} (avec avertissement : {warned})")
    print("Notes ignorées : " + ", ".join(f"{reason} {count}" for reason, count in skipped.items()))
    print(f"Pièces jointes : {attachments_included} incluses, {attachments_missing} introuvables, "
          f"{attachments_ignored} ignorées")

    if not prepared:
        print("Aucune note à exporter : pas d'archive écrite.", file=sys.stderr)
        return 1 if errors else 2

    if args.dry_run:
        print("Simulation (--dry-run) : aucune archive écrite.")
    else:
        output = args.output or Path(
            f"memos-export-{opts.username}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(export_time))}.zip"
        )
        manifest = build_manifest(opts.username, export_time, prepared)
        write_archive(output, manifest, prepared, export_time)
        print(f"Archive : {output}")
        print("À importer dans Memos : Paramètres → Export & Import → Import memos.")
    return 1 if errors or warned else 0


if __name__ == "__main__":
    sys.exit(main())
