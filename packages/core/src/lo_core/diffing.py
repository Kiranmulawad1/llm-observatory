"""Structured diffs between two prompt versions.

Pure functions over already-loaded data, kept out of the service layer so the
diff can be unit-tested without a database and reused by the eval-comparison view
in Phase 4.

The output is structured rather than a formatted string. A rendered diff blob
would force the frontend to parse text back into hunks, and would give a CI gate
no way to ask a precise question like "did the system message change, or only
the temperature?" — which is exactly the question that decides whether a prompt
change needs a fresh eval run.
"""

from __future__ import annotations

import difflib
import uuid
from collections.abc import Sequence
from typing import Any

from lo_core.schemas.prompt import (
    ChangeKind,
    Message,
    MessageDiff,
    ParameterDiff,
    PromptDiff,
)


def _unified(before: str, after: str) -> str:
    """Line-level unified diff of one message's content.

    `keepends=False` plus `lineterm=""` keeps the output free of stray blank
    lines, so the frontend can render it directly in a <pre> block.
    """
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )


def diff_messages(before: Sequence[Message], after: Sequence[Message]) -> list[MessageDiff]:
    """Positional diff over the message list.

    Messages are compared by index rather than matched by similarity. That is the
    right call for chat prompts: position *is* meaning — swapping the system and
    user turns is a semantic change, not a move — and a similarity matcher would
    hide it by reporting "unchanged, reordered".
    """
    diffs: list[MessageDiff] = []

    for index in range(max(len(before), len(after))):
        old = before[index] if index < len(before) else None
        new = after[index] if index < len(after) else None

        if old is None and new is not None:
            diffs.append(
                MessageDiff(
                    index=index,
                    change="added",
                    role_to=new.role,
                    content_to=new.content,
                    unified=_unified("", new.content),
                )
            )
        elif old is not None and new is None:
            diffs.append(
                MessageDiff(
                    index=index,
                    change="removed",
                    role_from=old.role,
                    content_from=old.content,
                    unified=_unified(old.content, ""),
                )
            )
        elif old is not None and new is not None:
            identical = old.role == new.role and old.content == new.content
            change: ChangeKind = "unchanged" if identical else "modified"
            diffs.append(
                MessageDiff(
                    index=index,
                    change=change,
                    role_from=old.role,
                    role_to=new.role,
                    content_from=old.content,
                    content_to=new.content,
                    unified="" if identical else _unified(old.content, new.content),
                )
            )

    return diffs


def diff_parameters(before: dict[str, Any], after: dict[str, Any]) -> list[ParameterDiff]:
    """Key-level diff of model parameters.

    Unchanged keys are included. A reviewer looking at a prompt change needs to
    see that temperature is 0.0 in *both* versions as much as they need to see
    what moved — omitting it invites the assumption that it was unset.
    """
    diffs: list[ParameterDiff] = []

    for key in sorted(set(before) | set(after)):
        in_before, in_after = key in before, key in after
        old_value, new_value = before.get(key), after.get(key)

        if in_before and not in_after:
            change: ChangeKind = "removed"
        elif in_after and not in_before:
            change = "added"
        elif old_value == new_value:
            change = "unchanged"
        else:
            change = "modified"

        diffs.append(
            ParameterDiff(key=key, change=change, value_from=old_value, value_to=new_value)
        )

    return diffs


def diff_versions(
    prompt_id: uuid.UUID,
    from_version: int,
    from_messages: Sequence[Message],
    from_parameters: dict[str, Any],
    to_version: int,
    to_messages: Sequence[Message],
    to_parameters: dict[str, Any],
) -> PromptDiff:
    """Full structured diff between two versions of one prompt."""
    messages = diff_messages(from_messages, to_messages)
    parameters = diff_parameters(from_parameters, to_parameters)

    identical = all(m.change == "unchanged" for m in messages) and all(
        p.change == "unchanged" for p in parameters
    )

    return PromptDiff(
        prompt_id=prompt_id,
        from_version=from_version,
        to_version=to_version,
        identical=identical,
        messages=messages,
        parameters=parameters,
    )
