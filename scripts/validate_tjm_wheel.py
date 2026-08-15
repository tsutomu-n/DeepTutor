#!/usr/bin/env python
"""Fail-closed validation for the distributable TJM wheel."""

from __future__ import annotations

import argparse
from collections import Counter
from email.parser import Parser
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

WEB_PREFIX = "deeptutor_web/"
STATIC_PREFIX = f"{WEB_PREFIX}.next/static/"
TJM_WEB_ROUTE = f"{WEB_PREFIX}.next/server/app/(utility)/tjm/page.js"
VAD_PREFIX = f"{WEB_PREFIX}public/vad/"
VAD_RUNTIME_FILES = (
    f"{VAD_PREFIX}vad.worklet.bundle.min.js",
    f"{VAD_PREFIX}silero_vad_v5.onnx",
    f"{VAD_PREFIX}ort-wasm-simd-threaded.mjs",
    f"{VAD_PREFIX}ort-wasm-simd-threaded.wasm",
)
VAD_NOTICES = f"{VAD_PREFIX}THIRD_PARTY_NOTICES.md"
VAD_ATTRIBUTIONS = (
    ("ricky0123/vad", "Copyright (c) 2022-present ricky0123"),
    ("Silero VAD", "Copyright (c) 2020-present Silero Team"),
    ("ONNX Runtime", "Copyright (c) Microsoft Corporation"),
)
TJM_PYTHON_FILES = (
    "deeptutor/tjm/__init__.py",
    "deeptutor/tjm/attempts.py",
    "deeptutor/tjm/catalog.py",
    "deeptutor/tjm/domain.py",
    "deeptutor/tjm/importer.py",
    "deeptutor/tjm/storage.py",
)
NATIVE_FILE_NAME = re.compile(
    r"(?:\.node|\.pyd|\.dll|\.dylib|\.exe|\.so(?:\.\d+)*)$",
    re.IGNORECASE,
)
WHEEL_METADATA_PATH = re.compile(r"^[^/]+\.dist-info/WHEEL$")


class WheelValidationError(ValueError):
    """Raised when a wheel does not satisfy the TJM distribution contract."""


def _require_nonempty(
    members: dict[str, zipfile.ZipInfo], member: str, *, label: str | None = None
) -> None:
    info = members.get(member)
    if info is None or info.is_dir() or info.file_size == 0:
        raise WheelValidationError(f"missing or empty {label or member}: {member}")


def _validate_archive_names(infos: list[zipfile.ZipInfo]) -> None:
    names = [info.filename for info in infos]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise WheelValidationError(f"duplicate wheel entries: {', '.join(duplicates[:10])}")

    unsafe = []
    for name in names:
        path = PurePosixPath(name)
        if name.startswith("/") or "\\" in name or ".." in path.parts:
            unsafe.append(name)
    if unsafe:
        raise WheelValidationError(f"unsafe wheel entries: {', '.join(unsafe[:10])}")


def _validate_wheel_metadata(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    wheel_path: Path,
) -> None:
    if not wheel_path.name.endswith("-py3-none-any.whl"):
        raise WheelValidationError(f"wheel filename must declare py3-none-any: {wheel_path.name}")

    metadata_members = [name for name in members if WHEEL_METADATA_PATH.fullmatch(name)]
    if len(metadata_members) != 1:
        raise WheelValidationError(
            f"expected exactly one .dist-info/WHEEL file, found {len(metadata_members)}"
        )

    metadata_member = metadata_members[0]
    _require_nonempty(members, metadata_member)
    try:
        metadata_text = archive.read(metadata_member).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WheelValidationError(".dist-info/WHEEL is not valid UTF-8") from exc
    metadata = Parser().parsestr(metadata_text)

    tags = metadata.get_all("Tag", [])
    if tags != ["py3-none-any"]:
        raise WheelValidationError(
            f"WHEEL metadata must contain only 'Tag: py3-none-any'; found {tags or 'none'}"
        )
    purelib = metadata.get_all("Root-Is-Purelib", [])
    if len(purelib) != 1 or purelib[0].strip().lower() != "true":
        raise WheelValidationError(
            "WHEEL metadata must contain exactly one 'Root-Is-Purelib: true'"
        )


def _is_sharp_payload(member: str) -> bool:
    parts = [part.casefold() for part in PurePosixPath(member).parts]
    for index, part in enumerate(parts):
        if part != "node_modules" or index + 1 >= len(parts):
            continue
        package = parts[index + 1]
        if package == "sharp":
            return True
        if package == "@img" and index + 2 < len(parts):
            scoped_package = parts[index + 2]
            if scoped_package == "sharp" or scoped_package.startswith("sharp-"):
                return True
    return any(part.startswith("sharp@") or part.startswith("@img+sharp-") for part in parts)


def _validate_web_payload(members: dict[str, zipfile.ZipInfo]) -> None:
    web_files = {
        name: info
        for name, info in members.items()
        if name.startswith(WEB_PREFIX) and not info.is_dir()
    }
    native_files = sorted(
        name for name in web_files if NATIVE_FILE_NAME.search(PurePosixPath(name).name)
    )
    if native_files:
        raise WheelValidationError(
            "deeptutor_web contains native runtime files: " + ", ".join(native_files[:10])
        )

    sharp_files = sorted(name for name in web_files if _is_sharp_payload(name))
    if sharp_files:
        raise WheelValidationError(
            "deeptutor_web contains forbidden Sharp/@img Sharp payloads: "
            + ", ".join(sharp_files[:10])
        )

    _require_nonempty(members, f"{WEB_PREFIX}server.js")
    if not any(
        name.startswith(STATIC_PREFIX) and info.file_size > 0 for name, info in web_files.items()
    ):
        raise WheelValidationError("missing non-empty deeptutor_web/.next/static asset")
    _require_nonempty(members, TJM_WEB_ROUTE, label="TJM Web route")
    for member in VAD_RUNTIME_FILES:
        _require_nonempty(members, member)


def _validate_vad_notices(archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo]) -> None:
    _require_nonempty(members, VAD_NOTICES)
    try:
        notice_text = archive.read(VAD_NOTICES).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WheelValidationError(f"{VAD_NOTICES} is not valid UTF-8") from exc

    missing = [
        project
        for project, copyright_line in VAD_ATTRIBUTIONS
        if project not in notice_text or copyright_line not in notice_text
    ]
    if missing:
        raise WheelValidationError(
            "THIRD_PARTY_NOTICES.md is missing required attribution: " + ", ".join(missing)
        )


def _validate_tjm_python(members: dict[str, zipfile.ZipInfo]) -> None:
    for member in TJM_PYTHON_FILES:
        _require_nonempty(members, member)


def validate_tjm_wheel(wheel_path: Path) -> None:
    """Validate the complete TJM runtime contract inside one wheel archive."""
    wheel_path = Path(wheel_path)
    if not wheel_path.is_file():
        raise WheelValidationError(f"wheel does not exist or is not a file: {wheel_path}")

    try:
        with zipfile.ZipFile(wheel_path) as archive:
            infos = archive.infolist()
            _validate_archive_names(infos)
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise WheelValidationError(f"corrupt wheel member: {corrupt_member}")
            members = {info.filename: info for info in infos}
            _validate_wheel_metadata(archive, members, wheel_path)
            _validate_web_payload(members)
            _validate_vad_notices(archive, members)
            _validate_tjm_python(members)
    except zipfile.BadZipFile as exc:
        raise WheelValidationError(f"invalid or corrupt wheel ZIP: {wheel_path}") from exc
    except OSError as exc:
        raise WheelValidationError(f"could not read wheel: {wheel_path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="Path to the DeepTutor .whl file")
    args = parser.parse_args(argv)

    try:
        validate_tjm_wheel(args.wheel)
    except WheelValidationError as exc:
        print(f"TJM wheel validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"TJM wheel validation passed: {args.wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
