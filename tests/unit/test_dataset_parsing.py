"""Dataset ingestion: JSON, JSONL and CSV."""

from __future__ import annotations

import pytest

from lo_core.errors import ValidationError
from lo_core.services.datasets import parse_csv_items, parse_json_items, parse_upload


class TestJSON:
    def test_explicit_shape(self) -> None:
        items = parse_json_items('[{"inputs": {"question": "q1"}, "expected_output": "a1"}]')
        assert items[0].inputs == {"question": "q1"}
        assert items[0].expected_output == "a1"

    def test_flat_shape_becomes_template_variables(self) -> None:
        """The shape a hand-written fixture or CSV export naturally has."""
        items = parse_json_items('[{"question": "q1", "context": "c1", "expected_output": "a1"}]')
        assert items[0].inputs == {"question": "q1", "context": "c1"}
        assert items[0].expected_output == "a1"

    def test_jsonl(self) -> None:
        items = parse_json_items('{"question": "q1"}\n{"question": "q2"}\n')
        assert [i.inputs["question"] for i in items] == ["q1", "q2"]

    def test_expected_context_preserved(self) -> None:
        items = parse_json_items('[{"question": "q", "expected_context": ["doc1", "doc2"]}]')
        assert items[0].expected_context == ["doc1", "doc2"]

    def test_empty_file_rejected(self) -> None:
        with pytest.raises(ValidationError, match="empty"):
            parse_json_items("   ")

    def test_malformed_json_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invalid JSON"):
            parse_json_items("[{oops}]")

    def test_item_without_inputs_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no input variables"):
            parse_json_items('[{"expected_output": "a"}]')

    def test_non_object_item_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not an object"):
            parse_json_items('["just a string"]')


class TestCSV:
    def test_recognised_answer_column(self) -> None:
        items = parse_csv_items("question,answer\nWhere is my order?,Shipped\n")
        assert items[0].inputs == {"question": "Where is my order?"}
        assert items[0].expected_output == "Shipped"

    def test_multiple_input_columns(self) -> None:
        """Two variables in a flat CSV — the case a string-only dataset can't express."""
        items = parse_csv_items("question,context,expected_output\nq1,c1,a1\n")
        assert items[0].inputs == {"question": "q1", "context": "c1"}
        assert items[0].expected_output == "a1"

    def test_json_list_in_expected_context_cell(self) -> None:
        items = parse_csv_items('question,expected_context\nq1,"[""doc1"", ""doc2""]"\n')
        assert items[0].expected_context == ["doc1", "doc2"]

    def test_plain_text_expected_context_becomes_single_passage(self) -> None:
        items = parse_csv_items("question,expected_context\nq1,just some text\n")
        assert items[0].expected_context == ["just some text"]

    def test_context_column_is_an_input_not_ground_truth(self) -> None:
        """A RAG dataset's `context` column feeds the prompt; it is not the answer.

        Treating it as ground truth would strip the prompt's main variable and
        make every render fail on an undefined `context`.
        """
        items = parse_csv_items("question,context,expected_output\nq1,retrieved,a1\n")
        assert items[0].inputs == {"question": "q1", "context": "retrieved"}
        assert items[0].expected_context is None

    def test_no_data_rows_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no data rows"):
            parse_csv_items("question,answer\n")


class TestUploadDispatch:
    def test_csv_by_extension(self) -> None:
        items = parse_upload(b"question,answer\nq1,a1\n", "data.csv")
        assert items[0].expected_output == "a1"

    def test_json_default(self) -> None:
        items = parse_upload(b'[{"question": "q1"}]', "data.json")
        assert items[0].inputs["question"] == "q1"

    def test_non_utf8_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UTF-8"):
            parse_upload(b"\xff\xfe invalid", "data.json")

    def test_oversized_upload_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exceeds"):
            parse_upload(b"x" * (11 * 1024 * 1024), "data.json")
