#!/usr/bin/env python3
"""Safe reader/extractor for Falcom NNI/NA resource archives.

This implementation is intentionally read-only: it never changes an archive
and refuses to overwrite extracted files.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import struct
import sys
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


HEADER = struct.Struct("<4sIII")
TOC_ENTRY = struct.Struct("<IIII")
NNI_MAGIC = b"NNI\0"
STREAM_SEED = 0x7C53F961
STREAM_FACTOR = 0x3D09
HASH_MODULUS = 0xFFF1
DEFAULT_MAX_UNPACKED = 512 * 1024 * 1024


class ArchiveError(Exception):
    """Raised when an archive violates the expected format."""


@dataclass(frozen=True)
class Entry:
    index: int
    stored_name: str
    output_name: str
    name_hash: int
    offset: int
    stored_size: int
    compressed: bool


def deobfuscate(data: bytes) -> bytes:
    """Decode one independently seeded NI byte stream."""
    state = STREAM_SEED
    output = bytearray(len(data))
    for index, value in enumerate(data):
        state = (state * STREAM_FACTOR) & 0xFFFFFFFF
        output[index] = (value - ((state >> 16) & 0xFF)) & 0xFF
    return bytes(output)


def falcom_name_hash(name: bytes) -> int:
    """Calculate the 16-bit-ish lookup hash stored in each TOC record."""
    total = 0
    for index, value in enumerate(name):
        total += (value - 32) * (1 << ((index % 5) * 5))
    return total % HASH_MODULUS


def archive_paths(argument: Path) -> tuple[Path, Path]:
    suffix = argument.suffix.lower()
    if suffix in {".ni", ".na"}:
        return argument.with_suffix(".ni"), argument.with_suffix(".na")
    return Path(f"{argument}.ni"), Path(f"{argument}.na")


def checked_read(file: BinaryIO, size: int, description: str) -> bytes:
    data = file.read(size)
    if len(data) != size:
        raise ArchiveError(
            f"truncated {description}: expected {size} bytes, got {len(data)}"
        )
    return data


def parse_index(ni_path: Path, na_size: int) -> list[Entry]:
    try:
        ni_size = ni_path.stat().st_size
    except OSError as exc:
        raise ArchiveError(f"cannot stat index {ni_path}: {exc}") from exc

    with ni_path.open("rb") as file:
        magic, count, names_size, flags = HEADER.unpack(
            checked_read(file, HEADER.size, "NI header")
        )
        if magic != NNI_MAGIC:
            raise ArchiveError(f"bad NI magic: {magic!r}")
        if flags & 1:
            raise ArchiveError("incremental-link NI archives are not supported")

        toc_size = count * TOC_ENTRY.size
        expected_size = HEADER.size + toc_size + names_size
        if expected_size != ni_size:
            raise ArchiveError(
                f"NI size mismatch: header describes {expected_size}, file has {ni_size}"
            )

        toc = deobfuscate(checked_read(file, toc_size, "encrypted TOC"))
        names = deobfuscate(
            checked_read(file, names_size, "encrypted name table")
        )
        if file.read(1):
            raise ArchiveError("unexpected trailing data in NI index")

    entries: list[Entry] = []
    output_names: set[str] = set()
    for index, (name_hash, size, offset, name_offset) in enumerate(
        TOC_ENTRY.iter_unpack(toc)
    ):
        if name_offset >= len(names):
            raise ArchiveError(f"entry {index}: name offset is outside name table")
        terminator = names.find(b"\0", name_offset)
        if terminator < 0:
            raise ArchiveError(f"entry {index}: unterminated name")
        raw_name = names[name_offset:terminator]
        if not raw_name:
            raise ArchiveError(f"entry {index}: empty name")
        if falcom_name_hash(raw_name) != name_hash:
            raise ArchiveError(f"entry {index}: filename hash mismatch")
        try:
            stored_name = raw_name.decode("cp932")
        except UnicodeDecodeError as exc:
            raise ArchiveError(f"entry {index}: invalid CP932 filename") from exc

        normalized = stored_name.replace("\\", "/")
        compressed = normalized.lower().endswith(".z")
        output_name = normalized[:-2] if compressed else normalized
        validate_member_name(output_name, index)

        if (offset, size) != (0, 0) and offset + size > na_size:
            raise ArchiveError(
                f"entry {index}: data range 0x{offset:x}+0x{size:x} exceeds NA size"
            )
        key = output_name.casefold()
        if key in output_names:
            raise ArchiveError(f"entry {index}: duplicate output path {output_name!r}")
        output_names.add(key)

        entries.append(
            Entry(
                index=index,
                stored_name=normalized,
                output_name=output_name,
                name_hash=name_hash,
                offset=offset,
                stored_size=size,
                compressed=compressed,
            )
        )
    return entries


def validate_member_name(name: str, index: int) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts:
        raise ArchiveError(f"entry {index}: absolute or empty output path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveError(f"entry {index}: unsafe path component in {name!r}")
    if any(":" in part for part in path.parts):
        raise ArchiveError(f"entry {index}: drive/stream syntax in {name!r}")


def read_entry(
    na_file: BinaryIO, entry: Entry, max_unpacked_size: int
) -> bytes | None:
    if (entry.offset, entry.stored_size) == (0, 0):
        return None
    na_file.seek(entry.offset)
    stored = checked_read(na_file, entry.stored_size, f"entry {entry.index}")
    if not entry.compressed:
        if len(stored) > max_unpacked_size:
            raise ArchiveError(
                f"entry {entry.index}: raw size exceeds configured safety limit"
            )
        return stored

    if len(stored) < 8:
        raise ArchiveError(f"entry {entry.index}: compressed wrapper is too short")
    expected_crc, unpacked_size = struct.unpack_from("<II", stored)
    if unpacked_size > max_unpacked_size:
        raise ArchiveError(
            f"entry {entry.index}: unpacked size {unpacked_size} exceeds safety limit"
        )

    decoder = zlib.decompressobj()
    try:
        unpacked = decoder.decompress(stored[8:], max_unpacked_size + 1)
        unpacked += decoder.flush()
    except zlib.error as exc:
        raise ArchiveError(f"entry {entry.index}: invalid zlib stream: {exc}") from exc
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ArchiveError(f"entry {entry.index}: incomplete or trailing zlib data")
    if len(unpacked) != unpacked_size:
        raise ArchiveError(
            f"entry {entry.index}: unpacked size mismatch "
            f"({len(unpacked)} != {unpacked_size})"
        )
    actual_crc = zlib.crc32(unpacked) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise ArchiveError(
            f"entry {entry.index}: CRC32 mismatch "
            f"({actual_crc:08x} != {expected_crc:08x})"
        )
    return unpacked


def selected(entries: Iterable[Entry], pattern: str | None) -> Iterable[Entry]:
    for entry in entries:
        if pattern is None or fnmatch.fnmatchcase(entry.output_name, pattern):
            yield entry


def safe_output_path(root: Path, member: str) -> Path:
    root = root.resolve()
    target = root.joinpath(*PurePosixPath(member).parts).resolve()
    if target != root and root not in target.parents:
        raise ArchiveError(f"output path escapes extraction root: {member!r}")
    return target


def command_list(entries: list[Entry], as_json: bool) -> None:
    if as_json:
        print(json.dumps([asdict(entry) for entry in entries], ensure_ascii=False, indent=2))
        return
    print(f"entries: {len(entries)}")
    print("index  offset      stored     type  path")
    for entry in entries:
        kind = "zlib" if entry.compressed else "raw "
        print(
            f"{entry.index:5d}  0x{entry.offset:08x}  "
            f"{entry.stored_size:9d}  {kind}  {entry.output_name}"
        )


def command_verify(
    na_path: Path, entries: list[Entry], pattern: str | None, max_size: int
) -> None:
    count = 0
    with na_path.open("rb") as na_file:
        for entry in selected(entries, pattern):
            read_entry(na_file, entry, max_size)
            count += 1
    print(f"verified {count} entries")


def command_extract(
    na_path: Path,
    entries: list[Entry],
    output_dir: Path,
    pattern: str | None,
    max_size: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with na_path.open("rb") as na_file:
        for entry in selected(entries, pattern):
            data = read_entry(na_file, entry, max_size)
            if data is None:
                continue
            target = safe_output_path(output_dir, entry.output_name)
            if target.exists():
                raise ArchiveError(f"refusing to overwrite {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            count += 1
    print(f"extracted {count} entries to {output_dir.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List, verify, or extract a Falcom NNI/NA archive pair."
    )
    parser.add_argument(
        "command", choices=("list", "verify", "extract"), help="operation"
    )
    parser.add_argument(
        "archive", type=Path, help="archive base path, .ni path, or .na path"
    )
    parser.add_argument("output", type=Path, nargs="?", help="extract destination")
    parser.add_argument("--pattern", help="case-sensitive glob for logical paths")
    parser.add_argument("--json", action="store_true", help="JSON output for list")
    parser.add_argument(
        "--max-unpacked-size",
        type=int,
        default=DEFAULT_MAX_UNPACKED,
        help="per-entry safety limit in bytes (default: 512 MiB)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "extract" and args.output is None:
        raise ArchiveError("extract requires an output directory")
    if args.command != "extract" and args.output is not None:
        raise ArchiveError("an output directory is valid only for extract")
    if args.max_unpacked_size <= 0:
        raise ArchiveError("--max-unpacked-size must be positive")

    ni_path, na_path = archive_paths(args.archive)
    if not ni_path.is_file():
        raise ArchiveError(f"missing NI index: {ni_path}")
    if not na_path.is_file():
        raise ArchiveError(f"missing NA data file: {na_path}")
    entries = parse_index(ni_path, na_path.stat().st_size)

    if args.command == "list":
        command_list(entries, args.json)
    elif args.command == "verify":
        command_verify(na_path, entries, args.pattern, args.max_unpacked_size)
    else:
        command_extract(
            na_path, entries, args.output, args.pattern, args.max_unpacked_size
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ArchiveError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
