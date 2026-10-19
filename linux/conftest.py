"""Keep tests away from the user's real per-adapter settings (~/.config/jua254/adapters.json)."""
import pytest

import jua254_monitor


@pytest.fixture(autouse=True)
def isolated_adapter_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(jua254_monitor, "ADAPTER_SETTINGS", tmp_path / "adapters.json")
