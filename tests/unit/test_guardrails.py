"""The guardrail heuristics.

These are the checks that run continuously over sampled production traffic, so
their false-positive behaviour matters as much as their detection: a check that
fires on everything gets ignored, which is the same as not having it.
"""

from __future__ import annotations

import pytest

from lo_core.guardrails import (
    CheckInput,
    check_grounding,
    check_pii,
    check_toxicity,
    luhn_valid,
    run_checks,
)
from lo_core.services.review import sample_bucket


class TestPII:
    def test_detects_an_email_in_the_output(self) -> None:
        finding = check_pii(CheckInput(output="Contact them at alice@example.com."))
        assert finding is not None
        assert "email" in finding.detail["matches"]

    def test_api_keys_are_the_highest_severity(self) -> None:
        """A leaked key in a model's output is an incident, not a quality issue."""
        key = check_pii(CheckInput(output="use sk-abcdefghijklmnop1234567890"))
        email = check_pii(CheckInput(output="mail alice@example.com"))
        assert key is not None and email is not None
        assert key.severity == 1.0
        assert key.severity > email.severity

    def test_detects_aws_keys(self) -> None:
        finding = check_pii(CheckInput(output="AKIAIOSFODNN7EXAMPLE is the key"))
        assert finding is not None
        assert "aws_key" in finding.detail["matches"]

    def test_matched_values_are_redacted(self) -> None:
        """Storing the raw value would move the leak from a transient trace into
        the control plane, where it lives far longer."""
        finding = check_pii(CheckInput(output="alice@example.com"))
        assert finding is not None
        stored = finding.detail["matches"]["email"][0]
        assert "alice@example.com" not in stored
        assert "*" in stored

    def test_a_random_16_digit_number_is_not_a_card(self) -> None:
        """Without the Luhn check every order number trips this, and a check
        that cries wolf gets muted."""
        assert check_pii(CheckInput(output="order 1234567812345678")) is None

    def test_a_valid_card_number_is_detected(self) -> None:
        # A well-known Visa test number.
        finding = check_pii(CheckInput(output="card 4111111111111111"))
        assert finding is not None
        assert "credit_card" in finding.detail["matches"]

    def test_only_the_output_is_scanned(self) -> None:
        """A user's email in the input is them telling you their address. The
        same email in the output means the model repeated someone's data back."""
        assert check_pii(CheckInput(output="ok", inputs={"q": "alice@example.com"})) is None

    def test_clean_output_produces_nothing(self) -> None:
        assert check_pii(CheckInput(output="Your order ships Tuesday.")) is None


class TestLuhn:
    @pytest.mark.parametrize("number", ["4111111111111111", "5500005555555559"])
    def test_valid_numbers(self, number: str) -> None:
        assert luhn_valid(number) is True

    @pytest.mark.parametrize("number", ["1234567812345678", "4111111111111112"])
    def test_invalid_numbers(self, number: str) -> None:
        assert luhn_valid(number) is False

    def test_rejects_wrong_length(self) -> None:
        assert luhn_valid("411111") is False


class TestGrounding:
    def test_flags_a_number_absent_from_context(self) -> None:
        """The highest-signal cheap hallucination detector: a fabricated figure."""
        finding = check_grounding(
            CheckInput(
                output="The price is 4999 dollars.",
                context=["Our products range from 20 to 80 dollars."],
            )
        )
        assert finding is not None
        assert "4999" in finding.detail["unsupported_numbers"]

    def test_numbers_present_in_context_are_fine(self) -> None:
        finding = check_grounding(
            CheckInput(output="It costs 4999.", context=["The price is 4999 dollars."])
        )
        assert finding is None

    def test_separators_are_normalised(self) -> None:
        """ "1,000" in an answer must match "1000" in a source document."""
        finding = check_grounding(
            CheckInput(output="Revenue was 1,000,000.", context=["Revenue: 1000000"])
        )
        assert finding is None

    def test_common_small_numbers_are_ignored(self) -> None:
        """ "There are 3 steps" is not evidence of anything."""
        finding = check_grounding(
            CheckInput(output="There are 3 options and 2 tiers.", context=["Some text."])
        )
        assert finding is None

    def test_no_context_means_no_opinion(self) -> None:
        """The check is meaningless for a non-RAG trace, and flagging every one
        of them would drown the queue."""
        assert check_grounding(CheckInput(output="The answer is 4999.", context=None)) is None

    def test_severity_grows_with_the_number_of_fabrications(self) -> None:
        one = check_grounding(CheckInput(output="It is 4999.", context=["nothing"]))
        many = check_grounding(
            CheckInput(output="4999 and 8888 and 7777 and 6666.", context=["nothing"])
        )
        assert one is not None and many is not None
        assert many.severity > one.severity

    def test_severity_stays_below_the_pii_ceiling(self) -> None:
        """An ungrounded number is a quality problem; a leaked key is an incident."""
        finding = check_grounding(
            CheckInput(output=" ".join(str(n) for n in range(1000, 1050)), context=["x"])
        )
        assert finding is not None
        assert finding.severity <= 0.8


class TestToxicity:
    def test_detects_an_overt_insult(self) -> None:
        finding = check_toxicity(CheckInput(output="That is a stupid question."))
        assert finding is not None
        assert "stupid" in finding.detail["terms"]

    def test_severity_grows_with_distinct_terms(self) -> None:
        one = check_toxicity(CheckInput(output="stupid"))
        two = check_toxicity(CheckInput(output="stupid and pathetic"))
        assert one is not None and two is not None
        assert two.severity > one.severity

    def test_clean_output_produces_nothing(self) -> None:
        assert check_toxicity(CheckInput(output="Happy to help with that.")) is None


class TestRunChecks:
    def test_findings_are_ordered_worst_first(self) -> None:
        findings = run_checks(
            CheckInput(
                output="stupid question, the key is sk-abcdefghijklmnop1234567890",
                context=["nothing"],
            )
        )
        assert len(findings) >= 2
        assert findings[0].check == "pii"
        assert findings == sorted(findings, key=lambda f: f.severity, reverse=True)

    def test_disabled_checks_are_skipped(self) -> None:
        findings = run_checks(
            CheckInput(output="alice@example.com is stupid"), enabled={"toxicity"}
        )
        assert [f.check for f in findings] == ["toxicity"]

    def test_a_failing_check_does_not_stop_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Losing one check's opinion beats losing the whole sampling run."""
        import lo_core.guardrails as guardrails

        def explode(sample: CheckInput) -> None:
            raise RuntimeError("regex exploded")

        monkeypatch.setitem(guardrails.CHECKS, "pii", explode)
        findings = run_checks(CheckInput(output="that is stupid"))
        assert [f.check for f in findings] == ["toxicity"]

    def test_clean_trace_produces_no_findings(self) -> None:
        assert run_checks(CheckInput(output="Your order ships Tuesday.")) == []


class TestSampling:
    def test_is_deterministic(self) -> None:
        """The property that makes "why wasn't this checked?" answerable."""
        assert sample_bucket("abc123") == sample_bucket("abc123")

    def test_is_in_range(self) -> None:
        assert all(0.0 <= sample_bucket(f"trace-{i}") < 1.0 for i in range(200))

    def test_distribution_is_roughly_uniform(self) -> None:
        """A 10% rate must actually sample about 10%."""
        sampled = sum(1 for i in range(5000) if sample_bucket(f"trace-{i}") < 0.1)
        assert 400 < sampled < 600

    def test_control_selection_is_independent_of_sampling(self) -> None:
        """Salted separately so control choice is not correlated with the
        sampling decision that preceded it."""
        trace = "some-trace-id"
        assert sample_bucket(trace) != sample_bucket(f"control:{trace}")
