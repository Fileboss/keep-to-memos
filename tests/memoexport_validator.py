"""Port Python des règles de lecture de core/memoexport (Memos v0.31.0).

Reprend validate.go et reader.go : toute violation lève ArchiveError, comme
l'import Memos qui rejette alors l'archive entière. Sert d'oracle aux tests ;
il est calibré sur l'archive de référence du dépôt Memos (golden.zip).
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

UID_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,34}[a-zA-Z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)
TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

MAX_EXPANSION_RATIO = 64
MAX_EXPANSION_SLACK = 64 << 20
KNOWN_STATES = {"NORMAL", "ARCHIVED"}
KNOWN_VISIBILITIES = {"PRIVATE", "PROTECTED", "PUBLIC", "SPACE"}


class ArchiveError(Exception):
    pass


def parse_time(value: object) -> int:
    if not isinstance(value, str):
        raise ArchiveError(f"horodatage invalide : {value!r}")
    match = TIME_RE.match(value)
    if not match:
        raise ArchiveError(f"horodatage invalide : {value!r}")
    try:
        base = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ArchiveError(f"horodatage invalide : {value!r}") from exc
    offset = match.group(3)
    if offset != "Z":
        sign = 1 if offset[0] == "+" else -1
        base -= sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
    return int(base.timestamp())


def validate_entry_name(name: str) -> None:
    if not name:
        raise ArchiveError("nom d'entrée vide")
    if name.startswith("/"):
        raise ArchiveError(f"nom d'entrée absolu : {name!r}")
    if name.endswith("/"):
        raise ArchiveError(f"nom d'entrée répertoire : {name!r}")
    for c in name:
        if ord(c) < 0x20 or c in "\\:" or ord(c) == 0x7F:
            raise ArchiveError(f"caractère interdit dans {name!r}")
    for segment in name.split("/"):
        if segment in ("", ".", ".."):
            raise ArchiveError(f"segment invalide dans {name!r}")


def validate_uid(uid: object) -> None:
    if not isinstance(uid, str) or not UID_RE.match(uid):
        raise ArchiveError(f"uid invalide : {uid!r}")


def validate_manifest(manifest: dict) -> None:
    if manifest.get("format") not in ("memos-export", "memos-archive"):
        raise ArchiveError(f"format inattendu : {manifest.get('format')!r}")
    version = manifest.get("formatVersion")
    major, sep, minor = str(version).partition(".")
    if not sep or not major.isdigit() or not minor.isdigit():
        raise ArchiveError(f"formatVersion invalide : {version!r}")
    if int(major) != 1:
        raise ArchiveError(f"version majeure non prise en charge : {version!r}")
    generator = manifest.get("generator") or {}
    if not generator.get("name") or not generator.get("version"):
        raise ArchiveError("generator.name et generator.version sont requis")
    parse_time(manifest.get("exportTime"))
    scope = manifest.get("scope") or {}
    if not scope.get("kind"):
        raise ArchiveError("scope.kind est requis")
    if scope["kind"] == "USER" and not (scope.get("user") or {}).get("username"):
        raise ArchiveError("scope.user.username est requis")
    counts = manifest.get("counts")
    if counts is not None and (counts.get("memos", 0) < 0 or counts.get("attachments", 0) < 0):
        raise ArchiveError("counts négatifs")


def validate_memo(memo: dict) -> list[str]:
    uid = memo.get("uid")
    validate_uid(uid)
    if not memo.get("creator"):
        raise ArchiveError(f"{uid} : creator requis")
    parse_time(memo.get("createTime"))
    parse_time(memo.get("updateTime"))
    if not memo.get("contentPath"):
        raise ArchiveError(f"{uid} : contentPath requis")
    validate_entry_name(memo["contentPath"])
    if memo.get("parent"):
        validate_uid(memo["parent"])
        if memo["parent"] == uid:
            raise ArchiveError(f"{uid} : parent = lui-même")
    if memo.get("space"):
        validate_uid(memo["space"].get("uid"))
    location = memo.get("location")
    if location and not (-90 <= location.get("latitude", 0) <= 90 and -180 <= location.get("longitude", 0) <= 180):
        raise ArchiveError(f"{uid} : location hors bornes")

    warnings = []
    if memo.get("state") not in KNOWN_STATES:
        warnings.append(f"{uid} : state inconnu {memo.get('state')!r}")
    if memo.get("visibility") not in KNOWN_VISIBILITIES:
        warnings.append(f"{uid} : visibility inconnue {memo.get('visibility')!r}")
    for relation in memo.get("relations") or []:
        if relation.get("type") == "REFERENCE":
            validate_uid(relation.get("memo"))
        else:
            warnings.append(f"{uid} : relation inconnue")

    seen = set()
    for attachment in memo.get("attachments") or []:
        att_uid = attachment.get("uid")
        validate_uid(att_uid)
        if att_uid in seen:
            raise ArchiveError(f"{uid} : pièce jointe {att_uid} en double")
        seen.add(att_uid)
        if not attachment.get("filename"):
            raise ArchiveError(f"{uid} : filename requis")
        if not MEDIA_TYPE_RE.match(str(attachment.get("type", ""))):
            raise ArchiveError(f"{uid} : type invalide {attachment.get('type')!r}")
        if attachment.get("size", 0) < 0:
            raise ArchiveError(f"{uid} : size négatif")
        parse_time(attachment.get("createTime"))
        path, link = attachment.get("path"), attachment.get("externalLink")
        if path and link:
            raise ArchiveError(f"{uid} : path et externalLink exclusifs")
        if path:
            validate_entry_name(path)
            if not path.startswith("attachments/"):
                raise ArchiveError(f"{uid} : path hors de attachments/")
            if not SHA256_RE.match(str(attachment.get("sha256", ""))):
                raise ArchiveError(f"{uid} : sha256 requis avec path")
        elif not link:
            raise ArchiveError(f"{uid} : path ou externalLink requis")
    for reaction in memo.get("reactions") or []:
        if not reaction.get("reactionType") or not reaction.get("creator"):
            raise ArchiveError(f"{uid} : reaction incomplète")
    return warnings


def read_archive(path: Path) -> dict:
    """Valide l'archive et renvoie {manifest, memos, contents, attachments, warnings, names}."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError("pas un fichier ZIP") from exc
    with zf:
        entries: dict[str, zipfile.ZipInfo] = {}
        compressed = uncompressed = 0
        for info in zf.infolist():
            if info.filename.endswith("/") and info.file_size == 0:
                continue
            compressed += info.compress_size
            uncompressed += info.file_size
            if uncompressed > MAX_EXPANSION_RATIO * compressed + MAX_EXPANSION_SLACK:
                raise ArchiveError("taux d'expansion excessif")
            validate_entry_name(info.filename)
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ArchiveError(f"méthode de compression interdite : {info.filename}")
            if info.flag_bits & 0x1:
                raise ArchiveError(f"entrée chiffrée : {info.filename}")
            if info.filename in entries:
                raise ArchiveError(f"entrée en double : {info.filename}")
            entries[info.filename] = info

        if "manifest.json" not in entries:
            raise ArchiveError("manifest.json absent")
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except json.JSONDecodeError as exc:
            raise ArchiveError("manifest.json invalide") from exc
        validate_manifest(manifest)

        memos: dict[str, dict] = {}
        content_uids = set()
        warnings: list[str] = []
        for name in sorted(entries):
            if not name.startswith("memos/") or "/" in name[len("memos/"):]:
                continue
            rest = name[len("memos/"):]
            if rest.endswith(".md"):
                content_uids.add(rest[:-3])
                continue
            if not rest.endswith(".json"):
                continue
            try:
                memo = json.loads(zf.read(name))
            except json.JSONDecodeError as exc:
                raise ArchiveError(f"{name} invalide") from exc
            if memo.get("uid") != rest[:-5]:
                raise ArchiveError(f"{name} : uid ne correspond pas au nom de fichier")
            warnings += validate_memo(memo)
            if memo["contentPath"] not in entries:
                raise ArchiveError(f"{memo['uid']} : contenu absent")
            for attachment in memo.get("attachments") or []:
                if attachment.get("path") and attachment["path"] not in entries:
                    raise ArchiveError(f"{memo['uid']} : pièce jointe absente")
            memos[memo["uid"]] = memo
        for uid in content_uids:
            if uid not in memos:
                raise ArchiveError(f"memos/{uid}.md sans record")
        counts = manifest.get("counts")
        if counts is not None and counts.get("memos") != len(memos):
            warnings.append("counts.memos ne correspond pas")

        contents = {uid: zf.read(memo["contentPath"]) for uid, memo in memos.items()}
        attachments = {}
        for memo in memos.values():
            for attachment in memo.get("attachments") or []:
                if not attachment.get("path"):
                    continue
                data = zf.read(attachment["path"])
                if len(data) != attachment["size"]:
                    raise ArchiveError(f"{attachment['uid']} : taille différente du record")
                if hashlib.sha256(data).hexdigest() != attachment["sha256"]:
                    raise ArchiveError(f"{attachment['uid']} : sha256 différent du record")
                attachments[attachment["uid"]] = data
        return {
            "manifest": manifest,
            "memos": memos,
            "contents": contents,
            "attachments": attachments,
            "warnings": warnings,
            "names": [info.filename for info in zf.infolist()],
            "infos": zf.infolist(),
        }
