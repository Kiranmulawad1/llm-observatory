"""Structured diffs between prompt versions."""

from __future__ import annotations

import uuid

from lo_core.diffing import diff_messages, diff_parameters, diff_versions
from lo_core.schemas.prompt import Message


def msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)  # type: ignore[arg-type]


class TestDiffMessages:
    def test_identical_messages_are_unchanged(self) -> None:
        before = [msg("system", "You are helpful."), msg("user", "{{ q }}")]
        diffs = diff_messages(before, list(before))
        assert [d.change for d in diffs] == ["unchanged", "unchanged"]
        assert all(d.unified == "" for d in diffs)

    def test_modified_content_produces_unified_diff(self) -> None:
        diffs = diff_messages([msg("system", "Be terse.")], [msg("system", "Be verbose.")])
        assert diffs[0].change == "modified"
        assert "-Be terse." in diffs[0].unified
        assert "+Be verbose." in diffs[0].unified

    def test_role_change_counts_as_modified(self) -> None:
        diffs = diff_messages([msg("system", "same")], [msg("user", "same")])
        assert diffs[0].change == "modified"
        assert diffs[0].role_from == "system"
        assert diffs[0].role_to == "user"

    def test_added_message(self) -> None:
        diffs = diff_messages([msg("system", "a")], [msg("system", "a"), msg("user", "b")])
        assert [d.change for d in diffs] == ["unchanged", "added"]
        assert diffs[1].content_to == "b"
        assert diffs[1].content_from is None

    def test_removed_message(self) -> None:
        diffs = diff_messages([msg("system", "a"), msg("user", "b")], [msg("system", "a")])
        assert [d.change for d in diffs] == ["unchanged", "removed"]
        assert diffs[1].content_from == "b"

    def test_reorder_is_reported_not_hidden(self) -> None:
        """Positional comparison is deliberate.

        Swapping the system and user turns changes what the model receives, so it
        must surface as two modifications — a similarity-based matcher would
        report "unchanged, moved" and hide a real behavioural change.
        """
        diffs = diff_messages(
            [msg("system", "a"), msg("user", "b")],
            [msg("user", "b"), msg("system", "a")],
        )
        assert [d.change for d in diffs] == ["modified", "modified"]

    def test_empty_to_empty(self) -> None:
        assert diff_messages([], []) == []


class TestDiffParameters:
    def test_reports_unchanged_keys(self) -> None:
        diffs = diff_parameters({"temperature": 0.0}, {"temperature": 0.0})
        assert diffs[0].change == "unchanged"
        assert diffs[0].value_from == 0.0

    def test_modified(self) -> None:
        diffs = diff_parameters({"temperature": 0.0}, {"temperature": 0.7})
        assert diffs[0].change == "modified"
        assert (diffs[0].value_from, diffs[0].value_to) == (0.0, 0.7)

    def test_added_and_removed(self) -> None:
        diffs = {d.key: d for d in diff_parameters({"a": 1}, {"b": 2})}
        assert diffs["a"].change == "removed"
        assert diffs["b"].change == "added"

    def test_keys_are_sorted(self) -> None:
        keys = [d.key for d in diff_parameters({"z": 1, "a": 2}, {"m": 3})]
        assert keys == sorted(keys)


class TestDiffVersions:
    def test_identical_versions_flagged(self) -> None:
        messages = [msg("user", "hi")]
        result = diff_versions(
            prompt_id=uuid.uuid4(),
            from_version=1,
            from_messages=messages,
            from_parameters={"temperature": 0.0},
            to_version=2,
            to_messages=list(messages),
            to_parameters={"temperature": 0.0},
        )
        assert result.identical is True

    def test_parameter_only_change_is_not_identical(self) -> None:
        """A temperature change with identical text still changes behaviour, and
        is exactly the case a naive text-only diff would call 'no change'."""
        messages = [msg("user", "hi")]
        result = diff_versions(
            prompt_id=uuid.uuid4(),
            from_version=1,
            from_messages=messages,
            from_parameters={"temperature": 0.0},
            to_version=2,
            to_messages=list(messages),
            to_parameters={"temperature": 0.9},
        )
        assert result.identical is False
        assert all(m.change == "unchanged" for m in result.messages)

    def test_version_numbers_are_carried_through(self) -> None:
        result = diff_versions(
            prompt_id=uuid.uuid4(),
            from_version=3,
            from_messages=[msg("user", "a")],
            from_parameters={},
            to_version=9,
            to_messages=[msg("user", "b")],
            to_parameters={},
        )
        assert (result.from_version, result.to_version) == (3, 9)
        assert result.identical is False
