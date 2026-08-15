import importlib.util
from pathlib import Path

import pytest


def _load_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_web_package.py"
    spec = importlib.util.spec_from_file_location("prepare_web_package_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_runtime_scan_finds_node_and_shared_library_payloads(tmp_path: Path) -> None:
    prepare_web_package = _load_module()
    native_node = tmp_path / "node_modules" / "sharp" / "sharp-linux-x64.node"
    shared_library = tmp_path / "node_modules" / "libvips-cpp.so.8.17.3"
    javascript = tmp_path / "server.js"
    node_named_javascript = tmp_path / "react-server.node.js"
    node_named_production_javascript = tmp_path / "react-dom-server.node.production.js"
    for path in (
        native_node,
        shared_library,
        javascript,
        node_named_javascript,
        node_named_production_javascript,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")

    assert prepare_web_package.find_native_runtime_files(tmp_path) == [shared_library, native_node]


def test_portable_standalone_rejects_platform_native_payloads(tmp_path: Path) -> None:
    prepare_web_package = _load_module()
    native_node = tmp_path / "node_modules" / "sharp-linux-x64.node"
    native_node.parent.mkdir(parents=True)
    native_node.write_bytes(b"fixture")

    with pytest.raises(SystemExit, match="platform-native runtime files"):
        prepare_web_package.assert_portable_standalone(tmp_path)


def test_portable_standalone_accepts_javascript_and_wasm(tmp_path: Path) -> None:
    prepare_web_package = _load_module()
    for name in ("server.js", "vad/silero_vad_v5.onnx", "vad/ort-wasm-simd-threaded.wasm"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")

    assert prepare_web_package.find_native_runtime_files(tmp_path) == []
    prepare_web_package.assert_portable_standalone(tmp_path)
