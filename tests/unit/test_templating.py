"""Template compilation, sandboxing, analysis and rendering."""

from __future__ import annotations

import pytest

from lo_core.errors import TemplateRenderError, TemplateSyntaxError
from lo_core.schemas.prompt import Message
from lo_core.templating import (
    compile_template,
    content_hash,
    extract_variables,
    render_messages,
)


class TestCompile:
    def test_accepts_valid_template(self) -> None:
        compile_template("Hello {{ name }}, you have {{ count }} messages.")

    def test_rejects_unclosed_block(self) -> None:
        with pytest.raises(TemplateSyntaxError):
            compile_template("{% for d in docs %}{{ d }}")

    def test_rejects_malformed_expression(self) -> None:
        with pytest.raises(TemplateSyntaxError):
            compile_template("{{ unclosed ")


class TestExtractVariables:
    def test_finds_simple_variables(self) -> None:
        names = [v.name for v in extract_variables(["{{ question }} and {{ context }}"])]
        assert names == ["context", "question"]

    def test_excludes_loop_locals(self) -> None:
        """The whole reason for using Jinja2's AST instead of a regex.

        `d` is bound by the for-loop, so it is not an input the caller supplies.
        A regex over `{{ ... }}` would wrongly report it as a required variable.
        """
        names = [
            v.name for v in extract_variables(["{% for d in documents %}{{ d.text }}{% endfor %}"])
        ]
        assert names == ["documents"]

    def test_unions_across_messages(self) -> None:
        names = [v.name for v in extract_variables(["{{ persona }}", "{{ question }}"])]
        assert names == ["persona", "question"]

    def test_is_deterministic_regardless_of_order(self) -> None:
        """Ordering is persisted and diffed, so it must not depend on input order."""
        first = extract_variables(["{{ b }}", "{{ a }}"])
        second = extract_variables(["{{ a }}", "{{ b }}"])
        assert [v.name for v in first] == [v.name for v in second] == ["a", "b"]

    def test_no_variables(self) -> None:
        assert extract_variables(["a static prompt"]) == []


class TestRender:
    def test_renders_each_message(self) -> None:
        rendered = render_messages(
            [
                Message(role="system", content="You are {{ persona }}."),
                Message(role="user", content="{{ question }}"),
            ],
            {"persona": "a triage bot", "question": "Where is my order?"},
        )
        assert [m.content for m in rendered] == ["You are a triage bot.", "Where is my order?"]
        assert [m.role for m in rendered] == ["system", "user"]

    def test_renders_loops(self) -> None:
        rendered = render_messages(
            [
                Message(
                    role="user",
                    content="{% for d in docs %}[{{ loop.index }}] {{ d }}\n{% endfor %}",
                )
            ],
            {"docs": ["alpha", "beta"]},
        )
        assert rendered[0].content == "[1] alpha\n[2] beta\n"

    def test_missing_variable_raises_instead_of_rendering_blank(self) -> None:
        """StrictUndefined is the point: a typo must fail, not silently degrade.

        Without it, this renders "Context: " and the resulting quality drop looks
        like a model regression rather than a template bug.
        """
        with pytest.raises(TemplateRenderError):
            render_messages([Message(role="user", content="Context: {{ contxt }}")], {})

    def test_extra_variables_are_ignored(self) -> None:
        rendered = render_messages(
            [Message(role="user", content="{{ a }}")], {"a": "x", "unused": "y"}
        )
        assert rendered[0].content == "x"

    @pytest.mark.parametrize(
        "hostile",
        [
            # The canonical SSTI chain: reach a base class, enumerate subclasses,
            # and from there get to os/subprocess.
            "{{ ''.__class__.__mro__[1].__subclasses__() }}",
            "{{ ''.__class__.__base__ }}",
            "{{ self.__init__.__globals__ }}",
            "{{ ().__class__.__bases__[0].__subclasses__() }}",
        ],
    )
    def test_sandbox_blocks_attribute_escapes(self, hostile: str) -> None:
        """Anyone who can author a prompt must not thereby get code execution."""
        with pytest.raises(TemplateRenderError):
            render_messages([Message(role="user", content=hostile)], {})

    def test_sandbox_bounds_range(self) -> None:
        """The sandbox caps `range()` on its own, before any output is produced."""
        with pytest.raises(TemplateRenderError, match="Range too big"):
            render_messages(
                [Message(role="user", content="{% for i in range(200000) %}x{% endfor %}")],
                {},
            )

    def test_oversized_output_is_rejected(self) -> None:
        """The character cap catches what the range guard cannot.

        Iterating a caller-supplied list is unbounded by the sandbox — the size
        comes from request data, not from a literal in the template — so the
        output limit is what stops one request from allocating gigabytes.
        """
        with pytest.raises(TemplateRenderError, match="exceeds"):
            render_messages(
                [Message(role="user", content="{% for d in docs %}{{ d }}{% endfor %}")],
                {"docs": ["x" * 100] * 20_000},
            )


class TestContentHash:
    def _messages(self) -> list[Message]:
        return [Message(role="user", content="hi")]

    def test_is_stable_across_calls(self) -> None:
        assert content_hash(self._messages(), {"temperature": 0.0}) == content_hash(
            self._messages(), {"temperature": 0.0}
        )

    def test_ignores_parameter_key_order(self) -> None:
        """Without sort_keys, insertion order would change the hash and every
        write would look like new content."""
        a = content_hash(self._messages(), {"model": "claude-opus-5", "temperature": 0.0})
        b = content_hash(self._messages(), {"temperature": 0.0, "model": "claude-opus-5"})
        assert a == b

    def test_changes_with_content(self) -> None:
        a = content_hash([Message(role="user", content="hi")], {})
        b = content_hash([Message(role="user", content="hello")], {})
        assert a != b

    def test_changes_with_role(self) -> None:
        a = content_hash([Message(role="user", content="hi")], {})
        b = content_hash([Message(role="system", content="hi")], {})
        assert a != b

    def test_changes_with_parameters(self) -> None:
        a = content_hash(self._messages(), {"temperature": 0.0})
        b = content_hash(self._messages(), {"temperature": 0.7})
        assert a != b

    def test_message_order_matters(self) -> None:
        a = content_hash(
            [Message(role="system", content="a"), Message(role="user", content="b")], {}
        )
        b = content_hash(
            [Message(role="user", content="b"), Message(role="system", content="a")], {}
        )
        assert a != b
