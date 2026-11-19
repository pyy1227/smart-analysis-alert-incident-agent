import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.seed import seed_docs


class SeedDocsTests(unittest.TestCase):
    def test_seed_uses_active_service_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            for filename in seed_docs.MAPPING:
                (knowledge / filename).write_text(f"# {filename}\ncontent", encoding="utf-8")

            with patch.object(seed_docs, "KNOWLEDGE_DIR", knowledge), \
                    patch.object(seed_docs, "add_document") as add:
                seed_docs.main()

            service_ids = [call.kwargs["service_id"] for call in add.call_args_list]
            self.assertEqual(service_ids.count("payment-api"), 2)
            self.assertIn("runbook/prometheus-restart.md", [
                call.kwargs["source"] for call in add.call_args_list
            ])

    def test_seed_skips_missing_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(seed_docs, "KNOWLEDGE_DIR", root / "missing"), \
                    patch.object(seed_docs, "add_document") as add:
                seed_docs.main()
            add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
