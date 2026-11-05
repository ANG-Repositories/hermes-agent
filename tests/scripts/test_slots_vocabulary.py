"""Keep allocation-path edits within the vocabulary ratchet (t_56b365c3)."""
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "slots_vocabulary_lint", ROOT / "scripts/lints/slots-vocabulary-lint.py"
)
lint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint)


class SlotsVocabularyTests(unittest.TestCase):
    def test_worker_pool_stays_within_baseline(self):
        import json

        _, _, baseline = lint.load_config(
            json.loads((ROOT / lint.CONFIG_REL).read_text(encoding="utf-8"))
        )
        rel = "hermes_cli/kanban_worker_pool.py"
        count = lint.count_text((ROOT / rel).read_text(encoding="utf-8"))
        self.assertEqual(lint.grade({rel: count}, baseline), [])


if __name__ == "__main__":
    unittest.main()
