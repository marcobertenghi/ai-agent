import tempfile
import unittest
from pathlib import Path

from aiagent.config import load_config

ROOT = Path(__file__).resolve().parents[1]


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.config_path = self.base / "config.toml"
        self.config_path.write_text((ROOT / "config.toml").read_text(), encoding="utf-8")
        (self.base / "system_prompt.md").write_text(
            "Help with an authorized lab.", encoding="utf-8"
        )
        self.config = load_config(self.config_path)
        self.config.workspace.mkdir()

    def change_config(self, old, new):
        text = self.config_path.read_text()
        assert old in text
        self.config_path.write_text(text.replace(old, new))
        return load_config(self.config_path)
