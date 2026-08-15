import importlib.util
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "validate_tjm_wheel.py"
WHEEL_METADATA = """\
Wheel-Version: 1.0
Generator: test
Root-Is-Purelib: true
Tag: py3-none-any
"""
NOTICE_TEXT = """\
Project: ricky0123/vad
Copyright (c) 2022-present ricky0123
Project: Silero VAD
Copyright (c) 2020-present Silero Team
Project: ONNX Runtime
Copyright (c) Microsoft Corporation
"""
REQUIRED_FILES = {
    "deeptutor_web/server.js": b"server",
    "deeptutor_web/.next/static/chunks/tjm.js": b"static",
    "deeptutor_web/.next/server/app/(utility)/tjm/page.js": b"tjm route",
    "deeptutor_web/public/vad/vad.worklet.bundle.min.js": b"worklet",
    "deeptutor_web/public/vad/silero_vad_v5.onnx": b"model",
    "deeptutor_web/public/vad/ort-wasm-simd-threaded.mjs": b"loader",
    "deeptutor_web/public/vad/ort-wasm-simd-threaded.wasm": b"\x00asm",
    "deeptutor_web/public/vad/THIRD_PARTY_NOTICES.md": NOTICE_TEXT.encode(),
    "deeptutor/tjm/__init__.py": b"from .attempts import AttemptService\n",
    "deeptutor/tjm/attempts.py": b"class AttemptService: pass\n",
    "deeptutor/tjm/catalog.py": b"class CatalogService: pass\n",
    "deeptutor/tjm/domain.py": b"def grade_responses(): pass\n",
    "deeptutor/tjm/importer.py": b"class ImportService: pass\n",
    "deeptutor/tjm/storage.py": b"class LearningStore: pass\n",
}


def _load_module():
    spec = importlib.util.spec_from_file_location("validate_tjm_wheel_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_wheel(
    tmp_path: Path,
    *,
    name: str = "deeptutor-1.5.8-py3-none-any.whl",
    omit: set[str] | None = None,
    replacements: dict[str, bytes] | None = None,
    extra: dict[str, bytes] | None = None,
    wheel_metadata: str = WHEEL_METADATA,
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    wheel = tmp_path / name
    members = dict(REQUIRED_FILES)
    members["deeptutor-1.5.8.dist-info/WHEEL"] = wheel_metadata.encode()
    for member in omit or set():
        members.pop(member, None)
    members.update(replacements or {})
    members.update(extra or {})
    with zipfile.ZipFile(wheel, "w", compression=compression) as archive:
        for member, payload in members.items():
            archive.writestr(member, payload)
    return wheel


def test_cli_accepts_complete_tjm_wheel(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(wheel)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "TJM wheel validation passed" in result.stdout


def test_rejects_corrupt_runtime_asset(tmp_path: Path) -> None:
    validator = _load_module()
    marker = b"STATIC_ASSET_PAYLOAD_TO_CORRUPT"
    wheel = _write_wheel(
        tmp_path,
        replacements={"deeptutor_web/.next/static/chunks/tjm.js": marker},
        compression=zipfile.ZIP_STORED,
    )
    wheel_bytes = bytearray(wheel.read_bytes())
    marker_offset = wheel_bytes.index(marker)
    wheel_bytes[marker_offset] ^= 0xFF
    wheel.write_bytes(wheel_bytes)

    with pytest.raises(validator.WheelValidationError, match="corrupt"):
        validator.validate_tjm_wheel(wheel)


@pytest.mark.parametrize(
    ("missing_member", "expected"),
    [
        ("deeptutor_web/server.js", "deeptutor_web/server.js"),
        ("deeptutor_web/.next/static/chunks/tjm.js", ".next/static"),
        (
            "deeptutor_web/.next/server/app/(utility)/tjm/page.js",
            "TJM Web route",
        ),
        ("deeptutor_web/public/vad/vad.worklet.bundle.min.js", "vad.worklet.bundle.min.js"),
        ("deeptutor_web/public/vad/silero_vad_v5.onnx", "silero_vad_v5.onnx"),
        (
            "deeptutor_web/public/vad/ort-wasm-simd-threaded.mjs",
            "ort-wasm-simd-threaded.mjs",
        ),
        (
            "deeptutor_web/public/vad/ort-wasm-simd-threaded.wasm",
            "ort-wasm-simd-threaded.wasm",
        ),
        (
            "deeptutor_web/public/vad/THIRD_PARTY_NOTICES.md",
            "THIRD_PARTY_NOTICES.md",
        ),
        ("deeptutor/tjm/__init__.py", "deeptutor/tjm/__init__.py"),
        ("deeptutor/tjm/attempts.py", "deeptutor/tjm/attempts.py"),
        ("deeptutor/tjm/catalog.py", "deeptutor/tjm/catalog.py"),
        ("deeptutor/tjm/domain.py", "deeptutor/tjm/domain.py"),
        ("deeptutor/tjm/importer.py", "deeptutor/tjm/importer.py"),
        ("deeptutor/tjm/storage.py", "deeptutor/tjm/storage.py"),
    ],
)
def test_rejects_missing_runtime_contract_member(
    tmp_path: Path, missing_member: str, expected: str
) -> None:
    validator = _load_module()
    wheel = _write_wheel(tmp_path, omit={missing_member})

    with pytest.raises(validator.WheelValidationError, match=expected):
        validator.validate_tjm_wheel(wheel)


@pytest.mark.parametrize(
    "forbidden_member",
    [
        "deeptutor_web/node_modules/addon/build/Release/addon.node",
        "deeptutor_web/node_modules/libvips/lib/libvips-cpp.so.8.17.3",
        "deeptutor_web/node_modules/sharp/package.json",
        "deeptutor_web/node_modules/@img/sharp-linux-x64/package.json",
    ],
)
def test_rejects_native_and_sharp_payloads(tmp_path: Path, forbidden_member: str) -> None:
    validator = _load_module()
    wheel = _write_wheel(tmp_path, extra={forbidden_member: b"forbidden"})

    with pytest.raises(validator.WheelValidationError, match="native|Sharp"):
        validator.validate_tjm_wheel(wheel)


@pytest.mark.parametrize(
    "attribution",
    [
        "ricky0123/vad",
        "Copyright (c) 2020-present Silero Team",
        "ONNX Runtime",
    ],
)
def test_rejects_incomplete_vad_notices(tmp_path: Path, attribution: str) -> None:
    validator = _load_module()
    notice = NOTICE_TEXT.replace(attribution, "missing attribution").encode()
    wheel = _write_wheel(
        tmp_path,
        replacements={"deeptutor_web/public/vad/THIRD_PARTY_NOTICES.md": notice},
    )

    with pytest.raises(validator.WheelValidationError, match="attribution"):
        validator.validate_tjm_wheel(wheel)


@pytest.mark.parametrize(
    ("name", "wheel_metadata"),
    [
        ("deeptutor-1.5.8-py3-none-linux_x86_64.whl", WHEEL_METADATA),
        (
            "deeptutor-1.5.8-py3-none-any.whl",
            WHEEL_METADATA.replace("Tag: py3-none-any", "Tag: cp313-cp313-linux_x86_64"),
        ),
        (
            "deeptutor-1.5.8-py3-none-any.whl",
            WHEEL_METADATA.replace("Root-Is-Purelib: true", "Root-Is-Purelib: false"),
        ),
    ],
)
def test_rejects_non_universal_wheel_claims(tmp_path: Path, name: str, wheel_metadata: str) -> None:
    validator = _load_module()
    wheel = _write_wheel(tmp_path, name=name, wheel_metadata=wheel_metadata)

    with pytest.raises(validator.WheelValidationError, match="py3-none-any|Purelib"):
        validator.validate_tjm_wheel(wheel)
