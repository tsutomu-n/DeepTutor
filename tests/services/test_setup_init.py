from pathlib import Path

from deeptutor.services.setup import init as setup_init


def test_seed_default_personas_uses_supplied_admin_workspace(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "data" / "user" / "workspace"
    captured: list[Path] = []

    class FakePathService:
        def get_workspace_dir(self) -> Path:
            return workspace

    class FakePersonaService:
        def __init__(self, *, root: Path) -> None:
            captured.append(root)

        def seed_presets(self) -> list[str]:
            return []

    monkeypatch.setattr("deeptutor.services.persona.service.PersonaService", FakePersonaService)

    setup_init._seed_default_personas(FakePathService())

    assert captured == [workspace / "personas"]
