import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

from reportlab.pdfgen import canvas

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class LinkedResourceAdapterTests(unittest.TestCase):
    def test_db_fiddle_adapter_derives_known_ramp_facts(self):
        adapter = load_module("db_fiddle_adapter", "scripts/linked_resource_adapters/db_fiddle.py")

        schema_sql = """
        CREATE TABLE transactions (
            id TEXT,
            user_transaction_time TEXT,
            card_id TEXT,
            amount REAL
        );
        CREATE TABLE cards (
            id TEXT,
            card_program_id TEXT
        );
        CREATE TABLE card_programs (
            id TEXT,
            display_name TEXT
        );
        INSERT INTO card_programs (id, display_name) VALUES
            ('p1', 'Travel'),
            ('p2', 'Operations');
        INSERT INTO cards (id, card_program_id) VALUES
            ('card_1', 'p1'),
            ('card_2', 'p2');
        INSERT INTO transactions (id, user_transaction_time, card_id, amount) VALUES
            ('t1', '2025-10-01 00:00:00', 'card_1', 100.0),
            ('t2', '2025-10-05 00:00:00', 'card_1', 80.0),
            ('t3', '2025-10-10 00:00:00', 'card_2', 10.0),
            ('t4', '2025-09-15 00:00:00', 'card_2', 5.0);
        """
        api_payload = {
            "data": [
                {
                    "attributes": {
                        "title": "Ramp SQL Challenge",
                        "engine": "sqlite",
                        "content": {
                            "schema": schema_sql,
                            "query": "SELECT * FROM transactions;",
                        },
                    }
                }
            ]
        }

        with mock.patch.object(adapter, "_capture_db_fiddle_payload", return_value=("<html></html>", api_payload)):
            result = adapter.fetch_db_fiddle_resource(
                "https://www.db-fiddle.com/f/example/1",
                question_text=(
                    "Which card has the most spend? Which card program has the most number of individual "
                    "transactions? Which card program had the most transactions in October?"
                ),
            )

        self.assertEqual(result["adapter"], "db_fiddle")
        self.assertEqual(result["derived_facts"][0]["answer"], "card_1")
        self.assertEqual(result["derived_facts"][1]["answer"], "Travel")
        self.assertEqual(result["derived_facts"][2]["answer"], "Travel")
        self.assertIn("Schema SQL", result["prompt_context"])
        self.assertEqual(
            result["deterministic_answer"],
            "Which card has the most spend?\n"
            "Card card_1 with $180.00 total spend.\n"
            "Which card program has the most number of individual transactions?\n"
            "Travel (and Operations tied) with 2 transactions each.\n"
            "Which card program had the most transactions in October?\n"
            "Travel with 2 transactions in October 2021.",
        )

    def test_db_fiddle_adapter_formats_ties_and_unlinked_top_card(self):
        adapter = load_module("db_fiddle_adapter_ties", "scripts/linked_resource_adapters/db_fiddle.py")

        schema_sql = """
        CREATE TABLE transactions (
            id TEXT,
            user_transaction_time TEXT,
            card_id TEXT,
            amount REAL
        );
        CREATE TABLE cards (
            id TEXT,
            card_program_id TEXT
        );
        CREATE TABLE card_programs (
            id TEXT,
            display_name TEXT
        );
        INSERT INTO card_programs (id, display_name) VALUES
            ('p1', 'PCARD'),
            ('p2', 'SuperUser Card'),
            ('p3', 'Primary Supplies & Materials');
        INSERT INTO cards (id, card_program_id) VALUES
            ('card_a', 'p1'),
            ('card_b', 'p2'),
            ('card_c', 'p3');
        INSERT INTO transactions (id, user_transaction_time, card_id, amount) VALUES
            ('t1', '2021-10-01 00:00:00', 'card_a', 10.0),
            ('t2', '2021-09-02 00:00:00', 'card_a', 10.0),
            ('t3', '2021-09-01 00:00:00', 'card_a', 10.0),
            ('t4', '2021-10-03 00:00:00', 'card_b', 100.0),
            ('t5', '2021-09-02 00:00:00', 'card_b', 100.0),
            ('t6', '2021-08-02 00:00:00', 'card_b', 100.0),
            ('t7', '2021-10-04 00:00:00', 'card_c', 5.0),
            ('t8', '2021-10-05 00:00:00', 'card_c', 5.0),
            ('t9', '2021-10-06 00:00:00', 'outside_card', 999.0);
        """
        api_payload = {
            "data": [
                {
                    "attributes": {
                        "title": "Ramp SQL Challenge",
                        "engine": "sqlite",
                        "content": {
                            "schema": schema_sql,
                            "query": "SELECT * FROM transactions;",
                        },
                    }
                }
            ]
        }

        with mock.patch.object(adapter, "_capture_db_fiddle_payload", return_value=("<html></html>", api_payload)):
            result = adapter.fetch_db_fiddle_resource(
                "https://www.db-fiddle.com/f/example/2",
                question_text=(
                    "Which card has the most spend? Which card program has the most number of individual "
                    "transactions? Which card program had the most transactions in October?"
                ),
            )

        self.assertEqual(
            result["deterministic_answer"],
            "Which card has the most spend?\n"
            "Card outside_card with $999.00 total spend. (Note: This card is not linked to any program in the cards table, so it doesn't count toward any program total.)\n"
            "Which card program has the most number of individual transactions?\n"
            "SuperUser Card (and PCARD tied) with 3 transactions each.\n"
            "Which card program had the most transactions in October?\n"
            "Primary Supplies & Materials with 2 transactions in October 2021.",
        )

    def test_generic_fetch_parses_json_resources(self):
        adapter = load_module("generic_fetch_json", "scripts/linked_resource_adapters/generic_fetch.py")

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._body = body
                self._content_type = content_type

            def read(self, _size: int = -1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            @property
            def headers(self):
                return {"Content-Type": self._content_type}

        with mock.patch.object(
            adapter,
            "urlopen",
            return_value=FakeResponse(b'{"rows":[{"card":"card_1","spend":180}]}', "application/json"),
        ):
            result = adapter.fetch_generic_resource("https://example.com/data.json")

        self.assertEqual(result["adapter"], "generic_json")
        self.assertEqual(result["normalized_payload"]["data"]["rows"][0]["card"], "card_1")
        self.assertIn("JSON summary", result["prompt_context"])

    def test_generic_fetch_derives_deterministic_answer_for_simple_json_aggregate(self):
        adapter = load_module("generic_fetch_json_deterministic", "scripts/linked_resource_adapters/generic_fetch.py")

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._body = body
                self._content_type = content_type

            def read(self, _size: int = -1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            @property
            def headers(self):
                return {"Content-Type": self._content_type}

        body = b'[{"card":"card_1","spend":180},{"card":"card_2","spend":15}]'
        with mock.patch.object(adapter, "urlopen", return_value=FakeResponse(body, "application/json")):
            result = adapter.fetch_generic_resource(
                "https://example.com/data.json",
                question_text="Which card has the most spend?",
            )

        self.assertEqual(result["deterministic_answer"], "card_1 with spend 180.")
        self.assertIn(
            {
                "question": "Which card has the most spend?",
                "answer": "card_1",
                "detail": "spend 180",
            },
            result["derived_facts"],
        )

    def test_generic_fetch_keeps_ambiguous_json_questions_model_assisted(self):
        adapter = load_module("generic_fetch_json_ambiguous", "scripts/linked_resource_adapters/generic_fetch.py")

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._body = body
                self._content_type = content_type

            def read(self, _size: int = -1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            @property
            def headers(self):
                return {"Content-Type": self._content_type}

        body = b'[{"card":"card_1","spend":180},{"card":"card_2","spend":15}]'
        with mock.patch.object(adapter, "urlopen", return_value=FakeResponse(body, "application/json")):
            result = adapter.fetch_generic_resource(
                "https://example.com/data.json",
                question_text="What does this dataset suggest about company spending habits?",
            )

        self.assertIsNone(result.get("deterministic_answer"))

    def test_generic_fetch_parses_csv_resources(self):
        adapter = load_module("generic_fetch_csv", "scripts/linked_resource_adapters/generic_fetch.py")

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._body = body
                self._content_type = content_type

            def read(self, _size: int = -1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            @property
            def headers(self):
                return {"Content-Type": self._content_type}

        csv_body = b"card,spend\ncard_1,180\ncard_2,15\n"
        with mock.patch.object(adapter, "urlopen", return_value=FakeResponse(csv_body, "text/csv")):
            result = adapter.fetch_generic_resource("https://example.com/data.csv")

        self.assertEqual(result["adapter"], "generic_csv")
        self.assertEqual(result["normalized_payload"]["header"], ["card", "spend"])
        self.assertEqual(result["normalized_payload"]["row_count"], 2)

    def test_generic_fetch_parses_html_tables(self):
        adapter = load_module("generic_fetch_html", "scripts/linked_resource_adapters/generic_fetch.py")

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._body = body
                self._content_type = content_type

            def read(self, _size: int = -1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            @property
            def headers(self):
                return {"Content-Type": self._content_type}

        html_body = b"""
        <html>
          <head><title>SQL Challenge</title></head>
          <body>
            <p>Use the table below.</p>
            <table>
              <tr><th>card</th><th>spend</th></tr>
              <tr><td>card_1</td><td>180</td></tr>
            </table>
          </body>
        </html>
        """
        with mock.patch.object(adapter, "urlopen", return_value=FakeResponse(html_body, "text/html")):
            result = adapter.fetch_generic_resource("https://example.com/challenge")

        self.assertEqual(result["adapter"], "generic_html")
        self.assertEqual(result["normalized_payload"]["tables"][0]["header"], ["card", "spend"])
        self.assertIn("SQL Challenge", result["prompt_context"])

    def test_generic_fetch_parses_pdf_resources(self):
        adapter = load_module("generic_fetch_pdf", "scripts/linked_resource_adapters/generic_fetch.py")

        class FakeResponse:
            def __init__(self, body: bytes, content_type: str):
                self._body = body
                self._content_type = content_type

            def read(self, _size: int = -1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            @property
            def headers(self):
                return {"Content-Type": self._content_type}

        pdf_buffer = io.BytesIO()
        pdf = canvas.Canvas(pdf_buffer)
        pdf.drawString(72, 720, "Card spend report")
        pdf.save()
        pdf_bytes = pdf_buffer.getvalue()

        with mock.patch.object(adapter, "urlopen", return_value=FakeResponse(pdf_bytes, "application/pdf")):
            result = adapter.fetch_generic_resource("https://example.com/report.pdf")

        self.assertEqual(result["adapter"], "generic_pdf")
        self.assertIn("Card spend report", result["normalized_payload"]["text"])
        self.assertIn("PDF excerpt", result["prompt_context"])
