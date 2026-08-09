#!/usr/bin/env python3
"""Rebuild the 2020 Chinese voice patch from the two game versions and old patch.

The build is deliberately source-driven: unchanged resources are copied from the
2020 data archive, localized resources are selected by a three-way comparison,
and voice tags are merged into the old Chinese XSO scripts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import shutil
import struct
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "build_manifest.json"
PARSER_PATH = ROOT / "nani_parser.py"
sys.dont_write_bytecode = True
TAG_RE = re.compile(br"^(<voice:[0-9]+>|<narration:[0-9]+>)")
MESSAGE_OPCODE = 0x02051004
MAX_ENTRY_SIZE = 512 * 1024 * 1024


class BuildError(Exception):
    pass


def load_nani_parser():
    spec = importlib.util.spec_from_file_location("ysf_nani_parser", PARSER_PATH)
    if spec is None or spec.loader is None:
        raise BuildError(f"无法载入封包解析器：{PARSER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


NANI = load_nani_parser()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def parse_hex_map(values: dict[str, str]) -> dict[int, int]:
    return {int(old, 16): int(new, 16) for old, new in values.items()}


@dataclass
class Archive:
    base: Path
    entries: list[Any]
    by_path: dict[str, Any]
    na_data: bytes
    flags: int

    @classmethod
    def open(cls, base: Path) -> "Archive":
        ni_path = base.with_suffix(".ni")
        na_path = base.with_suffix(".na")
        if not ni_path.is_file() or not na_path.is_file():
            raise BuildError(f"封包不完整：{base}.ni/.na")
        na_data = na_path.read_bytes()
        entries = NANI.parse_index(ni_path, len(na_data))
        header = ni_path.read_bytes()[:16]
        if len(header) != 16:
            raise BuildError(f"NI 头损坏：{ni_path}")
        _, _, _, flags = struct.unpack("<4sIII", header)
        by_path = {entry.output_name.casefold(): entry for entry in entries}
        return cls(base, entries, by_path, na_data, flags)

    def has(self, path: str) -> bool:
        return path.casefold() in self.by_path

    def entry(self, path: str):
        key = path.casefold()
        if key not in self.by_path:
            raise BuildError(f"封包中缺少条目：{path}")
        return self.by_path[key]

    def stored(self, path: str) -> bytes:
        entry = self.entry(path)
        start = entry.offset
        end = start + entry.stored_size
        return self.na_data[start:end]

    def read(self, path: str) -> bytes:
        entry = self.entry(path)
        data = NANI.read_entry(io.BytesIO(self.na_data), entry, MAX_ENTRY_SIZE)
        if data is None:
            raise BuildError(f"不支持空占位条目：{path}")
        return data


@dataclass
class Xso:
    fixed: bytes
    code: list[int]
    strings: list[bytes]
    calls: list[tuple[int, int]]


def unpack_xso(data: bytes, description: str) -> Xso:
    if len(data) < 0x24 or data[:4] != b"XSR\0":
        raise BuildError(f"XSO 格式错误：{description}")
    instruction_count, string_count = struct.unpack_from("<II", data, 0x1C)
    code_end = 0x24 + instruction_count * 4
    table_end = code_end + string_count * 4
    if table_end > len(data):
        raise BuildError(f"XSO 表越界：{description}")
    code = list(struct.unpack_from(f"<{instruction_count}I", data, 0x24))
    offsets = struct.unpack_from(f"<{string_count}I", data, code_end)
    strings: list[bytes] = []
    for offset in offsets:
        start = table_end + offset
        if start >= len(data):
            raise BuildError(f"XSO 字符串偏移越界：{description}")
        end = data.find(b"\0", start)
        if end < 0:
            raise BuildError(f"XSO 字符串未结束：{description}")
        strings.append(data[start:end])
    calls: list[tuple[int, int]] = []
    for index, word in enumerate(code):
        operand = index + 4
        if word == MESSAGE_OPCODE and operand < len(code):
            string_index = code[operand]
            if string_index >= len(strings):
                raise BuildError(f"XSO 对白字符串索引越界：{description}")
            calls.append((operand, string_index))
    return Xso(data[:0x24], code, strings, calls)


def pack_xso_call_strings(
    base: Xso,
    call_strings: dict[int, bytes],
    version_source: Xso,
) -> tuple[bytes, int]:
    code = list(base.code)
    strings = list(base.strings)
    appended: dict[bytes, int] = {}

    for call_number, (operand, _) in enumerate(base.calls):
        value = call_strings.get(call_number)
        if value is None:
            continue
        if value not in appended:
            appended[value] = len(strings)
            strings.append(value)
        code[operand] = appended[value]

    fixed = bytearray(base.fixed)
    fixed[4:8] = version_source.fixed[4:8]
    struct.pack_into("<I", fixed, 0x20, len(strings))

    result = bytearray(fixed)
    result += struct.pack(f"<{len(code)}I", *code)
    offsets: list[int] = []
    blob = bytearray()
    for value in strings:
        offsets.append(len(blob))
        blob += value + b"\0"
    blob += b"\0" * ((-len(blob)) % 4)
    result += struct.pack(f"<{len(offsets)}I", *offsets)
    result += blob
    return bytes(result), len(appended)


def pack_xso(old: Xso, new: Xso, tags_by_call: dict[int, bytes]) -> tuple[bytes, int, int]:
    call_strings = {
        call_number: tag + old.strings[old_string_index]
        for call_number, (_, old_string_index) in enumerate(old.calls)
        if (tag := tags_by_call.get(call_number)) is not None
    }
    resource, appended = pack_xso_call_strings(old, call_strings, new)
    return resource, len(call_strings), appended


def override_chinese_calls(
    path: str,
    xso: Xso,
    overrides: dict[str, str],
) -> Xso:
    code = list(xso.code)
    strings = list(xso.strings)
    calls = list(xso.calls)
    for raw_call_number, text in overrides.items():
        call_number = int(raw_call_number)
        if call_number < 0 or call_number >= len(calls):
            raise BuildError(f"中文覆写调用序号越界：{path} #{call_number}")
        try:
            encoded = text.encode("gbk")
        except UnicodeEncodeError as error:
            raise BuildError(f"中文覆写无法编码为 GBK：{path} #{call_number}") from error
        operand, _ = calls[call_number]
        string_index = len(strings)
        strings.append(encoded)
        code[operand] = string_index
        calls[call_number] = (operand, string_index)
    return Xso(xso.fixed, code, strings, calls)


def rebuild_from_2020_calls(
    path: str,
    old: Xso,
    new: Xso,
    spec: dict[str, Any],
) -> tuple[bytes, int, int, int]:
    available_tags: list[bytes | None] = []
    for _, string_index in new.calls:
        match = TAG_RE.match(new.strings[string_index])
        available_tags.append(match.group(1) if match else None)
    available = sum(tag is not None for tag in available_tags)

    old_call_map = spec["old_call_map"]
    if len(old_call_map) != len(new.calls):
        raise BuildError(
            f"2020 重建清单长度错误：{path} "
            f"({len(old_call_map)} != {len(new.calls)})"
        )
    text_by_call = {
        int(call_number): text
        for call_number, text in spec.get("text_by_new_call", {}).items()
    }
    if any(call_number < 0 or call_number >= len(new.calls) for call_number in text_by_call):
        raise BuildError(f"2020 重建文本调用序号越界：{path}")

    call_strings: dict[int, bytes] = {}
    for new_call, old_call_value in enumerate(old_call_map):
        if new_call in text_by_call:
            if old_call_value is not None:
                raise BuildError(f"2020 重建调用同时指定新旧文本：{path} #{new_call}")
            try:
                chinese = text_by_call[new_call].encode("gbk")
            except UnicodeEncodeError as error:
                raise BuildError(f"重建文本无法编码为 GBK：{path} #{new_call}") from error
        else:
            if old_call_value is None:
                raise BuildError(f"2020 重建调用缺少中文来源：{path} #{new_call}")
            old_call = int(old_call_value)
            if old_call < 0 or old_call >= len(old.calls):
                raise BuildError(f"旧版来源调用序号越界：{path} #{old_call}")
            chinese = old.strings[old.calls[old_call][1]]
        tag = available_tags[new_call] or b""
        call_strings[new_call] = tag + chinese

    resource, appended = pack_xso_call_strings(new, call_strings, new)
    mapped = sum(available_tags[call_number] is not None for call_number in call_strings)
    tagged_appended = len({
        value
        for call_number, value in call_strings.items()
        if available_tags[call_number] is not None
    })
    if appended < tagged_appended:
        raise BuildError(f"2020 重建字符串统计异常：{path}")
    return resource, available, mapped, tagged_appended


def tags_for_xso(
    path: str,
    old: Xso,
    new: Xso,
    special: dict[str, list[list[Any]]],
) -> tuple[dict[int, bytes], int]:
    new_tags: list[bytes | None] = []
    for _, string_index in new.calls:
        match = TAG_RE.match(new.strings[string_index])
        new_tags.append(match.group(1) if match else None)
    available = sum(tag is not None for tag in new_tags)

    # Some non-voiced NPC scripts changed their call count between releases.
    # They require no alignment because there is no tag to transfer.
    if available == 0:
        return {}, 0

    if path in special:
        tags = {int(index): value.encode("ascii") for index, value in special[path]}
        for call_number, tag in tags.items():
            if call_number < 0 or call_number >= len(old.calls):
                raise BuildError(f"例外映射调用序号越界：{path} #{call_number}")
            if tag not in new_tags:
                raise BuildError(f"例外映射标签不属于 2020 脚本：{path} {tag!r}")
        return tags, available

    if len(old.calls) != len(new.calls):
        raise BuildError(
            f"XSO 对白调用数不同且没有例外映射：{path} "
            f"({len(old.calls)} != {len(new.calls)})"
        )
    return {
        index: tag
        for index, tag in enumerate(new_tags)
        if tag is not None
    }, available


def merge_opbtn(old: bytes, new: bytes) -> bytes:
    header_size = 128
    width = 1024
    split_row = 769
    expected_size = header_size + width * width
    if len(old) != expected_size or len(new) != expected_size:
        raise BuildError("OPBTN.DDS 不是预期的 1024×1024 单字节布局")
    split = header_size + split_row * width
    return new[:header_size] + old[header_size:split] + new[split:]


def packed_entry(data: bytes, compressed: bool) -> bytes:
    if not compressed:
        return data
    return (
        struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data))
        + zlib.compress(data, 9)
    )


def obfuscate(data: bytes) -> bytes:
    state = NANI.STREAM_SEED
    output = bytearray(len(data))
    for index, value in enumerate(data):
        state = (state * NANI.STREAM_FACTOR) & 0xFFFFFFFF
        output[index] = (value + ((state >> 16) & 0xFF)) & 0xFF
    return bytes(output)


def build_archive(
    output_base: Path,
    english_2017: Archive,
    chinese_2017: Archive,
    english_2020: Archive,
    manifest: dict[str, Any],
) -> dict[str, int | float]:
    old_paths = set(chinese_2017.by_path)
    base_paths = set(english_2020.by_path)
    xso_paths = {
        path for path in old_paths & base_paths if path.endswith(".xso")
    }

    localized_dds: set[str] = set()
    localized_dat: set[str] = set()
    for path in old_paths & base_paths & set(english_2017.by_path):
        if path.endswith(".dds") and chinese_2017.read(path) != english_2017.read(path):
            localized_dds.add(path)
        elif path.endswith(".dat") and chinese_2017.read(path) != english_2017.read(path):
            localized_dat.add(path)

    output_base.parent.mkdir(parents=True, exist_ok=True)
    offsets: dict[str, int] = {}
    sizes: dict[str, int] = {}
    special = manifest["special_voice_tags"]
    chinese_overrides = manifest.get("chinese_call_overrides", {})
    rebuild_scripts = manifest.get("rebuild_from_2020_calls", {})
    voice_files = 0
    voice_available = 0
    voice_mapped = 0
    tagged_strings = 0

    data_order = sorted(english_2020.entries, key=lambda entry: entry.offset)
    with output_base.with_suffix(".na").open("xb") as na_file:
        for entry in data_order:
            path = entry.output_name.casefold()
            if path in xso_paths:
                old_xso = unpack_xso(chinese_2017.read(path), f"old:{path}")
                new_xso = unpack_xso(english_2020.read(path), f"2020:{path}")
                if path in chinese_overrides:
                    old_xso = override_chinese_calls(
                        path, old_xso, chinese_overrides[path]
                    )
                if path in rebuild_scripts:
                    resource, available, mapped, appended = rebuild_from_2020_calls(
                        path, old_xso, new_xso, rebuild_scripts[path]
                    )
                else:
                    tags, available = tags_for_xso(path, old_xso, new_xso, special)
                    resource, mapped, appended = pack_xso(old_xso, new_xso, tags)
                stored = packed_entry(resource, entry.compressed)
                voice_available += available
                voice_mapped += mapped
                tagged_strings += appended
                if mapped:
                    voice_files += 1
            elif path in localized_dds:
                if path == "menu/opbtn.dds":
                    resource = merge_opbtn(
                        chinese_2017.read(path), english_2020.read(path)
                    )
                else:
                    resource = chinese_2017.read(path)
                stored = packed_entry(resource, entry.compressed)
            elif path in localized_dat:
                stored = packed_entry(chinese_2017.read(path), entry.compressed)
            else:
                stored = english_2020.stored(path)

            offsets[path] = na_file.tell()
            sizes[path] = len(stored)
            na_file.write(stored)

    # The NI uses two independent orders: TOC records retain the original hash
    # order, while the shared name table follows physical NA data order.
    names = bytearray()
    name_offsets: dict[str, int] = {}
    for entry in data_order:
        path = entry.output_name.casefold()
        # parse_index normalizes separators for safe host-side paths, while the
        # on-disk Falcom name hash was calculated from Windows backslashes.
        raw_name = entry.stored_name.replace("/", "\\").encode("cp932")
        if NANI.falcom_name_hash(raw_name) != entry.name_hash:
            raise BuildError(f"文件名哈希异常：{entry.stored_name}")
        name_offsets[path] = len(names)
        names += raw_name + b"\0"

    toc = bytearray()
    for entry in english_2020.entries:
        path = entry.output_name.casefold()
        toc += struct.pack(
            "<IIII",
            entry.name_hash,
            sizes[path],
            offsets[path],
            name_offsets[path],
        )

    header = struct.pack(
        "<4sIII",
        NANI.NNI_MAGIC,
        len(english_2020.entries),
        len(names),
        english_2020.flags,
    )
    output_base.with_suffix(".ni").write_bytes(
        header + obfuscate(bytes(toc)) + obfuscate(bytes(names))
    )

    return {
        "archive_entries": len(english_2020.entries),
        "localized_xso_files": len(xso_paths),
        "localized_dds_files": len(localized_dds),
        "localized_dat_files": len(localized_dat),
        "voice_xso_files": voice_files,
        "voice_calls_available": voice_available,
        "voice_calls_mapped": voice_mapped,
        "appended_tagged_strings": tagged_strings,
        "voice_call_coverage_percent": round(voice_mapped * 100 / voice_available, 3),
    }


def pe_checksum_offset(data: bytes) -> int:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise BuildError("不是有效的 PE 文件")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 4 + 20 + 68 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise BuildError("PE 头损坏")
    return pe_offset + 4 + 20 + 64


def calculate_pe_checksum(data: bytes) -> tuple[int, int]:
    checksum_offset = pe_checksum_offset(data)
    work = bytearray(data)
    work[checksum_offset:checksum_offset + 4] = b"\0\0\0\0"
    original_size = len(work)
    if len(work) & 1:
        work.append(0)
    total = 0
    for index in range(0, len(work), 2):
        total += work[index] | (work[index + 1] << 8)
        total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return (total + original_size) & 0xFFFFFFFF, checksum_offset


def set_pe_checksum(data: bytes) -> bytes:
    checksum, offset = calculate_pe_checksum(data)
    output = bytearray(data)
    struct.pack_into("<I", output, offset, checksum)
    return bytes(output)


def patch_game_exe(source: Path) -> bytes:
    data = source.read_bytes()
    old = b"dbghelp.dll"
    new = b"ysfcn.dll\0\0"
    if data.count(old) != 1 or len(old) != len(new):
        raise BuildError("2020 主程序的 dbghelp.dll 导入点不符合预期")
    return set_pe_checksum(data.replace(old, new, 1))


def patch_dll(source: Path, address_map: dict[int, int]) -> bytes:
    data = bytearray(source.read_bytes())
    for old, new in address_map.items():
        old_bytes = struct.pack("<I", old)
        new_bytes = struct.pack("<I", new)
        if data.count(old_bytes) != 1:
            raise BuildError(f"DLL 地址 {old:#010x} 的出现次数不为 1")
        if data.count(new_bytes) != 0:
            raise BuildError(f"DLL 中提前出现新版地址 {new:#010x}")
        offset = data.find(old_bytes)
        data[offset:offset + 4] = new_bytes
    return set_pe_checksum(bytes(data))


def patch_text_table(source: Path, address_map: dict[int, int]) -> bytes:
    data = bytearray(source.read_bytes())
    if len(data) < 4:
        raise BuildError("ysfcn.text 太短")
    count = struct.unpack_from("<I", data, 0)[0]
    position = 4
    seen: set[int] = set()
    for _ in range(count):
        if position + 8 > len(data):
            raise BuildError("ysfcn.text 记录头越界")
        address, size = struct.unpack_from("<II", data, position)
        if address not in address_map:
            raise BuildError(f"ysfcn.text 出现未映射地址：{address:#010x}")
        struct.pack_into("<I", data, position, address_map[address])
        seen.add(address)
        position += 8 + size
        position = (position + 3) & ~3
        if position > len(data):
            raise BuildError("ysfcn.text 字符串越界")
    if position != len(data) or seen != set(address_map):
        raise BuildError("ysfcn.text 记录数量或结尾不符合预期")
    return bytes(data)


def verify_input_hashes(
    game_2017: Path,
    game_2020: Path,
    old_patch: Path,
    manifest: dict[str, Any],
) -> dict[str, str]:
    actual_paths = {
        "Ys The Oath in Felghana 2017/ysf_win_dx9.exe": game_2017 / "ysf_win_dx9.exe",
        "Ys The Oath in Felghana 2017/config_dx9.exe": game_2017 / "config_dx9.exe",
        "Ys The Oath in Felghana 2017/release/data_us.ni": game_2017 / "release" / "data_us.ni",
        "Ys The Oath in Felghana 2017/release/data_us.na": game_2017 / "release" / "data_us.na",
        "Ys The Oath in Felghana 2020/ysf_win_dx9.exe": game_2020 / "ysf_win_dx9.exe",
        "Ys The Oath in Felghana 2020/config_dx9.exe": game_2020 / "config_dx9.exe",
        "Ys The Oath in Felghana 2020/release/data_us.ni": game_2020 / "release" / "data_us.ni",
        "Ys The Oath in Felghana 2020/release/data_us.na": game_2020 / "release" / "data_us.na",
        "oldPatch/ysf_win_cn_dx9.exe": old_patch / "ysf_win_cn_dx9.exe",
        "oldPatch/config_cn_dx9.exe": old_patch / "config_cn_dx9.exe",
        "oldPatch/ysfcn.dll": old_patch / "ysfcn.dll",
        "oldPatch/ysfcn.text": old_patch / "ysfcn.text",
        "oldPatch/font.ttf": old_patch / "font.ttf",
        "oldPatch/release/data_cn.ni": old_patch / "release" / "data_cn.ni",
        "oldPatch/release/data_cn.na": old_patch / "release" / "data_cn.na",
    }
    result: dict[str, str] = {}
    for name, expected in manifest["expected_sha256"].items():
        path = actual_paths[name]
        if not path.is_file():
            raise BuildError(f"缺少输入文件：{path}")
        actual = sha256_file(path)
        result[name] = actual
        if actual != expected.upper():
            raise BuildError(
                f"输入版本不匹配：{path}\n"
                f"预期 {expected.upper()}\n实际 {actual}"
            )
    return result


def verify_archive(base: Path, expected_count: int) -> None:
    archive = Archive.open(base)
    if len(archive.entries) != expected_count:
        raise BuildError(
            f"输出封包条目数错误：{len(archive.entries)} != {expected_count}"
        )
    for entry in archive.entries:
        archive.read(entry.output_name)


def output_hashes(output: Path) -> dict[str, str]:
    names = [
        "ysf_win_cn_dx9.exe",
        "config_cn_dx9.exe",
        "ysfcn.dll",
        "ysfcn.text",
        "font.ttf",
        "release/data_cn.ni",
        "release/data_cn.na",
    ]
    return {name: sha256_file(output / Path(name)) for name in names}


OUTPUT_README = """# 《伊苏：菲尔盖纳之誓》2020 语音版汉化移植

本目录是由 2017 英文版、2017 汉化补丁和 2020 语音版重新生成的补丁成品。

## 安装

1. 把本目录中除说明文档外的文件和 `release` 文件夹复制到 2020 游戏根目录。
2. 启动 `ysf_win_cn_dx9.exe`。
3. 如需确认语音开关，运行 `config_cn_dx9.exe`，在 Sound 页面启用 **Play Voice**。

补丁使用独立的 `release/data_cn.na` 与 `release/data_cn.ni`，不会覆盖官方 `data_us`。
设置程序保留 2020 的 Play Voice/BGM Type 功能，因此设置窗口仍为英文。

构建输入、资源数量、语音覆盖率和成品 SHA-256 见 `移植报告.json`。

本版以日文脚本和日语原音为准修正已人工核对的语音文本：恢复
`<voice:06016>`、`<voice:06017>` 的独立分句，重译 `<voice:07036>`、
`<voice:25009>`；对剩余语音按试听结果补入四句、拆分一处旧合句，并把
Fran 的五句旧中文复制到两个有声分支。1920 个语音／旁白调用均有独立
中文对应。
"""


def build(args: argparse.Namespace) -> Path:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1:
        raise BuildError("不支持的构建清单版本")

    game_2017 = args.game_2017.resolve()
    game_2020 = args.game_2020.resolve()
    old_patch = args.old_patch.resolve()
    output = args.output.resolve()
    protected = {ROOT.resolve(), game_2017, game_2020, old_patch}
    if output in protected:
        raise BuildError("输出目录不能是工作区根目录或任何输入目录")
    if output.exists():
        raise BuildError(f"输出目录已经存在，为避免覆盖已停止：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    print("[1/6] 校验两版游戏和原汉化补丁的 SHA-256……")
    source_hashes = verify_input_hashes(game_2017, game_2020, old_patch, manifest)

    print("[2/6] 读取三套资源封包……")
    english_2017 = Archive.open(game_2017 / "release" / "data_us")
    english_2020 = Archive.open(game_2020 / "release" / "data_us")
    chinese_2017 = Archive.open(old_patch / "release" / "data_cn")

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent)
    )
    print(f"      临时构建目录：{temporary}")
    (temporary / "release").mkdir()

    print("[3/6] 移植主程序、DLL、程序内文字和字体……")
    (temporary / "ysf_win_cn_dx9.exe").write_bytes(
        patch_game_exe(game_2020 / "ysf_win_dx9.exe")
    )
    shutil.copyfile(game_2020 / "config_dx9.exe", temporary / "config_cn_dx9.exe")
    (temporary / "ysfcn.dll").write_bytes(
        patch_dll(
            old_patch / "ysfcn.dll",
            parse_hex_map(manifest["dll_address_map"]),
        )
    )
    (temporary / "ysfcn.text").write_bytes(
        patch_text_table(
            old_patch / "ysfcn.text",
            parse_hex_map(manifest["text_address_map"]),
        )
    )
    shutil.copyfile(old_patch / "font.ttf", temporary / "font.ttf")

    print("[4/6] 三方选择中文资源并合并 2020 语音标签……")
    stats = build_archive(
        temporary / "release" / "data_cn",
        english_2017,
        chinese_2017,
        english_2020,
        manifest,
    )
    for key, expected in manifest["expected_stats"].items():
        actual = stats.get(key)
        if actual != expected:
            raise BuildError(f"构建统计异常：{key} = {actual}，预期 {expected}")

    print("[5/6] 完整解压验证 1785 个输出条目……")
    verify_archive(
        temporary / "release" / "data_cn",
        manifest["expected_stats"]["archive_entries"],
    )
    (temporary / "README_移植说明.md").write_text(OUTPUT_README, encoding="utf-8")

    print("[6/6] 计算成品哈希并生成报告……")
    hashes = output_hashes(temporary)
    expected_hashes = {
        name: value.upper()
        for name, value in manifest["expected_output_sha256"].items()
    }
    mismatches = {
        name: {"expected": expected_hashes[name], "actual": value}
        for name, value in hashes.items()
        if expected_hashes.get(name) != value
    }
    report = {
        "build_format_version": 1,
        "source_sha256": source_hashes,
        "stats": stats,
        "dll_address_ports": manifest["dll_address_map"],
        "text_patch_records": len(manifest["text_address_map"]),
        "output_sha256": hashes,
        "reference_byte_match": not mismatches,
        "reference_mismatches": mismatches,
        "verification": {
            "archive_full_decode": True,
            "input_versions_exact": True,
            "pe_checksums_recalculated": True,
        },
    }
    (temporary / "移植报告.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    temporary.rename(output)
    print(f"构建完成：{output}")
    if mismatches:
        print("注意：语义验证通过，但部分压缩字节与参考成品哈希不同。")
    else:
        print("全部七个二进制/封包文件与参考成品逐字节一致。")
    return output


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 2017／XSEED 语音版游戏文件和原汉化补丁重建中文语音补丁"
    )
    parser.add_argument(
        "--game-2017",
        type=Path,
        required=True,
        help="2017 游戏根目录",
    )
    parser.add_argument(
        "--game-2020",
        type=Path,
        required=True,
        help="2020 游戏根目录",
    )
    parser.add_argument(
        "--old-patch",
        type=Path,
        required=True,
        help="原汉化补丁目录",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="新的输出目录；必须不存在",
    )
    return parser.parse_args()


def main() -> int:
    try:
        build(parse_arguments())
        return 0
    except (BuildError, NANI.ArchiveError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"构建失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
