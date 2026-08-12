"""Dataset service: immutable versions, JSON/CSV ingestion.

Versioning mirrors the prompt registry exactly (ADR 0004) — same row lock for
monotonic numbering, same content hash for change detection — because the reason
is the same: an eval run pins a dataset version, and that pin is only meaningful
if the version cannot change afterwards.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db.models.dataset import Dataset, DatasetItem, DatasetVersion
from lo_core.errors import ConflictError, NotFoundError, ValidationError
from lo_core.schemas.evaluation import (
    DatasetCreate,
    DatasetItemIn,
    DatasetRead,
    DatasetVersionCreate,
)

# Column names treated as ground truth when a CSV is uploaded. Everything else
# becomes a template variable, which is what makes a multi-variable RAG dataset
# (question + context) expressible in a flat CSV at all.
#
# Both lists are deliberately narrow and require the `expected_` prefix for
# context. `context` and `sources` are *not* included: in a RAG dataset those
# are overwhelmingly the retrieved passages fed *into* the prompt, not the
# ground truth to score against. Claiming them here would silently strip the
# prompt's main input variable and leave every render failing on an undefined
# `context` — a confusing failure a long way from its cause.
EXPECTED_OUTPUT_COLUMNS = ("expected_output", "expected", "answer")
EXPECTED_CONTEXT_COLUMNS = ("expected_context", "expected_sources")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _content_hash(items: list[DatasetItemIn]) -> str:
    payload = [
        {
            "inputs": item.inputs,
            "expected_output": item.expected_output,
            "expected_context": item.expected_context,
            "metadata": item.metadata,
        }
        for item in items
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Ingestion ------------------------------------------------------------


def parse_json_items(raw: str) -> list[DatasetItemIn]:
    """Parse a JSON array, or JSONL (one object per line).

    JSONL is accepted because eval datasets are commonly produced by appending
    to a file, and rejecting the format teams already have is a pointless
    friction point.
    """
    text = raw.strip()
    if not text:
        raise ValidationError("dataset file is empty")

    try:
        if text.startswith("["):
            records = json.loads(text)
        else:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc

    if not isinstance(records, list):
        raise ValidationError("expected a JSON array or newline-delimited JSON objects")

    items: list[DatasetItemIn] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValidationError(f"item {index} is not an object")
        items.append(_coerce_record(record, index))
    return items


def _coerce_record(record: dict[str, Any], index: int) -> DatasetItemIn:
    """Accept both the explicit shape and a flat one.

    Explicit: `{"inputs": {...}, "expected_output": "..."}`
    Flat:     `{"question": "...", "expected_output": "..."}` — every unreserved
              key becomes a template variable.

    Supporting the flat form matters because it is what a CSV export and a
    hand-written fixture naturally look like; requiring the nested shape would
    make people write a conversion script before they can use the platform.
    """
    if "inputs" in record:
        inputs = record["inputs"]
        if not isinstance(inputs, dict):
            raise ValidationError(f"item {index}: 'inputs' must be an object")
        return DatasetItemIn(
            inputs=inputs,
            expected_output=record.get("expected_output"),
            expected_context=record.get("expected_context"),
            metadata=record.get("metadata") or {},
        )

    reserved = {"expected_output", "expected_context", "metadata"}
    inputs = {k: v for k, v in record.items() if k not in reserved}
    if not inputs:
        raise ValidationError(f"item {index}: no input variables found")

    return DatasetItemIn(
        inputs=inputs,
        expected_output=record.get("expected_output"),
        expected_context=record.get("expected_context"),
        metadata=record.get("metadata") or {},
    )


def parse_csv_items(raw: str) -> list[DatasetItemIn]:
    """Parse CSV, mapping recognised columns and treating the rest as variables."""
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        raise ValidationError("CSV has no header row")

    fields = [f.strip() for f in reader.fieldnames if f]
    expected_col = next((c for c in fields if c.lower() in EXPECTED_OUTPUT_COLUMNS), None)
    context_col = next((c for c in fields if c.lower() in EXPECTED_CONTEXT_COLUMNS), None)

    items: list[DatasetItemIn] = []
    for index, row in enumerate(reader):
        inputs = {
            k.strip(): v
            for k, v in row.items()
            if k and k.strip() not in {expected_col, context_col} and v is not None
        }
        if not inputs:
            raise ValidationError(f"row {index}: no input columns found")

        expected_context: list[Any] | None = None
        if context_col and row.get(context_col):
            # A CSV cell cannot hold a list, so accept JSON in the cell and fall
            # back to treating the text as one passage.
            cell = row[context_col]
            try:
                parsed = json.loads(cell)
                expected_context = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                expected_context = [cell]

        items.append(
            DatasetItemIn(
                inputs=inputs,
                expected_output=row.get(expected_col) if expected_col else None,
                expected_context=expected_context,
            )
        )

    if not items:
        raise ValidationError("CSV contains no data rows")
    return items


def parse_upload(raw: bytes, filename: str | None) -> list[DatasetItemIn]:
    """Dispatch on file extension, defaulting to JSON."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError(f"upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("file must be UTF-8 encoded") from exc

    if filename and filename.lower().endswith(".csv"):
        return parse_csv_items(text)
    return parse_json_items(text)


# --- Persistence ----------------------------------------------------------


async def create_dataset(
    session: AsyncSession, project_id: uuid.UUID, payload: DatasetCreate
) -> Dataset:
    dataset = Dataset(
        project_id=project_id,
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
    )
    session.add(dataset)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            f"dataset slug {payload.slug!r} already exists in this project"
        ) from exc
    return dataset


async def get_dataset(session: AsyncSession, project_id: uuid.UUID, slug: str) -> Dataset:
    result = await session.execute(
        select(Dataset).where(Dataset.project_id == project_id, Dataset.slug == slug)
    )
    dataset = result.scalar_one_or_none()
    if dataset is None:
        raise NotFoundError(f"dataset {slug!r} not found")
    return dataset


async def create_version(
    session: AsyncSession, dataset: Dataset, payload: DatasetVersionCreate
) -> DatasetVersion:
    """Append an immutable version containing the full item set.

    Same `SELECT ... FOR UPDATE` serialisation as prompt versions: `max + 1` is
    a read-then-write, and the row lock is what makes concurrent uploads to the
    same dataset get distinct numbers instead of colliding on the unique index.
    """
    await session.execute(select(Dataset.id).where(Dataset.id == dataset.id).with_for_update())

    current_max = await session.scalar(
        select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset.id)
    )
    next_version = (current_max or 0) + 1

    version = DatasetVersion(
        dataset_id=dataset.id,
        version=next_version,
        item_count=len(payload.items),
        content_hash=_content_hash(payload.items),
        created_by=payload.created_by,
        change_note=payload.change_note,
    )
    session.add(version)
    await session.flush()

    session.add_all(
        [
            DatasetItem(
                dataset_version_id=version.id,
                item_index=index,
                inputs=item.inputs,
                expected_output=item.expected_output,
                expected_context=item.expected_context,
                item_metadata=item.metadata,
            )
            for index, item in enumerate(payload.items)
        ]
    )
    await session.flush()
    return version


async def get_version(
    session: AsyncSession, dataset_id: uuid.UUID, version: int | None
) -> DatasetVersion:
    """Fetch a specific version, or the latest when `version` is None."""
    stmt = select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)
    stmt = (
        stmt.where(DatasetVersion.version == version)
        if version is not None
        else stmt.order_by(DatasetVersion.version.desc()).limit(1)
    )
    found = (await session.execute(stmt)).scalar_one_or_none()
    if found is None:
        label = f"version {version}" if version is not None else "any version"
        raise NotFoundError(f"dataset has no {label}")
    return found


async def list_versions(
    session: AsyncSession, dataset_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[DatasetVersion]:
    result = await session.execute(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset_id)
        .order_by(DatasetVersion.version.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def list_items(
    session: AsyncSession, dataset_version_id: uuid.UUID, limit: int = 100, offset: int = 0
) -> list[DatasetItem]:
    result = await session.execute(
        select(DatasetItem)
        .where(DatasetItem.dataset_version_id == dataset_version_id)
        .order_by(DatasetItem.item_index)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def list_dataset_reads(
    session: AsyncSession, project_id: uuid.UUID, limit: int = 100, offset: int = 0
) -> list[DatasetRead]:
    """List datasets with their latest version number, in two queries."""
    result = await session.execute(
        select(Dataset)
        .where(Dataset.project_id == project_id)
        .order_by(Dataset.slug)
        .limit(limit)
        .offset(offset)
    )
    datasets = list(result.scalars().all())
    if not datasets:
        return []

    latest_result = await session.execute(
        select(DatasetVersion.dataset_id, func.max(DatasetVersion.version))
        .where(DatasetVersion.dataset_id.in_([d.id for d in datasets]))
        .group_by(DatasetVersion.dataset_id)
    )
    latest: dict[uuid.UUID, int] = dict(latest_result.all())  # type: ignore[arg-type]

    return [
        DatasetRead(
            id=d.id,
            project_id=d.project_id,
            slug=d.slug,
            name=d.name,
            description=d.description,
            created_at=d.created_at,
            updated_at=d.updated_at,
            latest_version=latest.get(d.id),
        )
        for d in datasets
    ]


async def delete_dataset(session: AsyncSession, dataset: Dataset) -> None:
    """Delete a dataset and its versions.

    Fails with a ConflictError if any eval run references one of those versions:
    the run -> dataset_version foreign key is RESTRICT, because a run whose
    dataset has vanished is a result nobody can interpret.
    """
    try:
        await session.delete(dataset)
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "dataset cannot be deleted because eval runs reference its versions"
        ) from exc
