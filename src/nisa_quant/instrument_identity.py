"""Read-only resolution of explicitly linked typed instrument identities."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Iterable

from .source_records import (
    parse_retrieved_at,
    source_fact_chronology_is_valid,
    source_fact_contract_is_valid,
)


TypedIdentity = tuple[str, str]


def resolve_typed_identity(
    connection: sqlite3.Connection,
    identifier_type: str,
    identifier_value: str,
) -> frozenset[TypedIdentity]:
    """Return the explicit undirected link component for a typed identity."""
    target = (identifier_type, identifier_value)
    graph: dict[TypedIdentity, set[TypedIdentity]] = {}
    for row in connection.execute(
        """
        SELECT left_instrument.identifier_type AS left_type,
               left_instrument.identifier_value AS left_value,
               right_instrument.identifier_type AS right_type,
               right_instrument.identifier_value AS right_value
        FROM instrument_links link
        JOIN instruments left_instrument ON left_instrument.id = link.from_instrument_id
        JOIN instruments right_instrument ON right_instrument.id = link.to_instrument_id
        """
    ):
        left = (row["left_type"], row["left_value"])
        right = (row["right_type"], row["right_value"])
        graph.setdefault(left, set()).add(right)
        graph.setdefault(right, set()).add(left)

    component: set[TypedIdentity] = {target}
    pending = [target]
    while pending:
        current = pending.pop()
        for neighbor in graph.get(current, set()):
            if neighbor not in component:
                component.add(neighbor)
                pending.append(neighbor)
    return frozenset(component)


def typed_identity_matches(
    connection: sqlite3.Connection,
    identifier_type: str | None,
    identifier_value: str | None,
    expected_type: str | None,
    expected_value: str | None,
) -> bool:
    """Match exact typed identity or membership in its explicit link component."""
    if not identifier_type or not identifier_value or not expected_type or not expected_value:
        return False
    return (identifier_type, identifier_value) in resolve_typed_identity(
        connection, expected_type, expected_value,
    )


def _date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("as_of must be ISO YYYY-MM-DD")
    return parsed


def fact_value_signature(source: Any) -> tuple[float, object, object] | None:
    """Return the comparable value/unit/currency tuple for a source fact."""
    try:
        value = float(source["value"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return value, source["unit"], source["currency"]


def fact_conflict_source_ids(
    connection: sqlite3.Connection,
    *,
    as_of: str | None = None,
    rows: Iterable[Any] | None = None,
) -> set[str]:
    """Return valid same-date facts that disagree inside one link component.

    The comparison is deliberately limited to the stored value, unit, and
    currency.  Exact numeric duplicates such as ``100`` and ``100.0`` remain
    controls, while unlinked typed identities remain independent facts.
    """
    cutoff = _date(as_of) if as_of is not None else None
    candidates = list(rows) if rows is not None else connection.execute(
        "SELECT * FROM source_records ORDER BY id",
    ).fetchall()
    grouped: dict[tuple[object, ...], list[Any]] = {}
    unbound_ledger: dict[tuple[object, ...], list[Any]] = {}
    bound_ledger_groups: dict[tuple[object, ...], set[int]] = {}
    for source in candidates:
        if not source_fact_contract_is_valid(source) or not source_fact_chronology_is_valid(source):
            continue
        observation = source["observation_date"]
        retrieved = parse_retrieved_at(source["retrieved_at"]).date()
        if cutoff is not None and (date.fromisoformat(observation) > cutoff or retrieved > cutoff):
            continue
        field = source["field"]
        if field == "cash_movement":
            continue
        identity_type = source["instrument_identifier_type"]
        identity_value = source["instrument_identifier"]
        component = resolve_typed_identity(connection, identity_type, identity_value)
        component_key = tuple(sorted(component))
        base_key = (field, observation, component_key)
        if field in {"buy", "sell"}:
            event_ids = [
                row[0] for row in connection.execute(
                    "SELECT id FROM transactions WHERE source_record_id = ?",
                    (source["id"],),
                )
            ]
            if event_ids:
                for event_id in event_ids:
                    event_key = (*base_key, event_id)
                    grouped.setdefault(event_key, []).append(source)
                    bound_ledger_groups.setdefault(base_key, set()).add(event_id)
            else:
                unbound_ledger.setdefault(base_key, []).append(source)
        else:
            grouped.setdefault(base_key, []).append(source)

    for base_key, sources in unbound_ledger.items():
        event_ids = bound_ledger_groups.get(base_key)
        if event_ids:
            for event_id in event_ids:
                grouped.setdefault((*base_key, event_id), []).extend(sources)
        else:
            grouped.setdefault((*base_key, None), []).extend(sources)

    conflicting: set[str] = set()
    for source_group in grouped.values():
        signatures = {
            signature for source in source_group
            if (signature := fact_value_signature(source)) is not None
        }
        if len(signatures) > 1:
            conflicting.update(source["id"] for source in source_group)
    return conflicting


def same_date_fact_conflict(
    connection: sqlite3.Connection,
    source: Any,
    *,
    as_of: str | None = None,
) -> bool:
    """Return whether a source disagrees with a same-date linked fact."""
    source_id = source["id"] if not isinstance(source, str) else source
    return source_id in fact_conflict_source_ids(connection, as_of=as_of)
