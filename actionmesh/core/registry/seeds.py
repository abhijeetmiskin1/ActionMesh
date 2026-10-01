"""Seed action definitions (C2) — MOCK capabilities for local development.

Every provider here is ``mock``. These descriptions let the platform be
exercised end-to-end without any third-party account. They describe *shape* only;
nothing in this file performs a side effect.

Risk levels are meaningful: they feed C3's authorization gate. CRITICAL actions
(``payment.*``) can never auto-execute.
"""

from __future__ import annotations

from typing import Any

from actionmesh.protocol.models import (
    ActionDefinition,
    RetryPolicy,
    RiskLevel,
    VerificationSpec,
)


def _def(
    name: str,
    risk: RiskLevel,
    permissions: list[str],
    properties: dict[str, dict[str, Any]],
    required: list[str],
    description: str,
    *,
    version: str = "1.0",
) -> ActionDefinition:
    return ActionDefinition(
        name=name,
        version=version,
        description=description,
        risk_level=risk,
        permissions=permissions,
        input_schema={"type": "object", "properties": properties, "required": required},
        output_schema={"type": "object", "properties": {}},
        verification=VerificationSpec(required=True, method="provider_verify"),
        provider="mock",
        timeout_s=30.0,
        retry_policy=RetryPolicy(max_attempts=3, backoff="exponential"),
    )


def seed_definitions() -> list[ActionDefinition]:
    """The 16 mock capabilities that ship with ActionMesh."""
    return [
        # --- messaging -------------------------------------------------
        _def(
            "message.send", RiskLevel.MEDIUM, ["message.send"],
            {"message": {"type": "string"}, "to": {"type": "string"},
             "channel": {"type": "string"}},
            ["message"],
            "MOCK: send a message to a resolved contact",
        ),
        _def(
            "message.read", RiskLevel.MEDIUM, ["message.read"],
            {"to": {"type": "string"}, "limit": {"type": "integer"}},
            [],
            "MOCK: read recent messages",
        ),
        # --- calendar --------------------------------------------------
        _def(
            "calendar.create", RiskLevel.LOW, ["calendar.write"],
            {"title": {"type": "string"}, "time": {"type": "string"},
             "duration_min": {"type": "integer"}},
            ["title"],
            "MOCK: create a calendar event",
        ),
        _def(
            "calendar.update", RiskLevel.LOW, ["calendar.write"],
            {"event_id": {"type": "string"}, "title": {"type": "string"},
             "time": {"type": "string"}},
            ["event_id"],
            "MOCK: update a calendar event",
        ),
        _def(
            "calendar.delete", RiskLevel.HIGH, ["calendar.write"],
            {"event_id": {"type": "string"}},
            ["event_id"],
            "MOCK: delete a calendar event",
        ),
        # --- reminders -------------------------------------------------
        _def(
            "reminder.create", RiskLevel.LOW, ["calendar.write"],
            {"text": {"type": "string"}, "time": {"type": "string"},
             "repeat": {"type": "string"}},
            ["text"],
            "MOCK: create a reminder (implemented on the calendar surface)",
        ),
        # --- files -----------------------------------------------------
        _def(
            "file.read", RiskLevel.MEDIUM, ["file.read"],
            {"path": {"type": "string"}}, ["path"],
            "MOCK: read a file and return a checksum",
        ),
        _def(
            "file.move", RiskLevel.MEDIUM, ["file.read", "file.share"],
            {"path": {"type": "string"}, "destination": {"type": "string"}},
            ["path", "destination"],
            "MOCK: move a file to another location",
        ),
        _def(
            "file.share", RiskLevel.HIGH, ["file.share"],
            {"path": {"type": "string"}, "target": {"type": "string"}},
            ["path"],
            "MOCK: share a file with a person or device",
        ),
        # --- devices ---------------------------------------------------
        _def(
            "device.notify", RiskLevel.LOW, ["device.notify"],
            {"device_id": {"type": "string"}, "title": {"type": "string"},
             "body": {"type": "string"}},
            ["device_id", "title"],
            "MOCK: show a notification on a registered device",
        ),
        _def(
            "device.send_file", RiskLevel.MEDIUM, ["device.notify", "file.share"],
            {"device_id": {"type": "string"}, "path": {"type": "string"}},
            ["device_id", "path"],
            "MOCK: transfer a file to another registered device",
        ),
        # --- payments (CRITICAL — always requires explicit approval) ---
        _def(
            "payment.prepare", RiskLevel.CRITICAL, ["payment.prepare"],
            {"amount": {"type": "number"}, "recipient": {"type": "string"},
             "method": {"type": "string"}},
            ["amount", "recipient"],
            "MOCK: create a payment intent (no real money moves)",
        ),
        _def(
            "payment.confirm", RiskLevel.CRITICAL, ["payment.execute"],
            {"payment_id": {"type": "string"}}, ["payment_id"],
            "MOCK: confirm a prepared payment (no real money moves)",
        ),
        _def(
            "payment.verify", RiskLevel.LOW, ["payment.prepare"],
            {"payment_id": {"type": "string"}}, ["payment_id"],
            "MOCK: verify the recorded state of a payment",
        ),
        # --- misc ------------------------------------------------------
        _def(
            "search.web", RiskLevel.LOW, ["search.web"],
            {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"],
            "MOCK: search the web",
        ),
        _def(
            "system.launch_app", RiskLevel.MEDIUM, ["system.app.launch"],
            {"app": {"type": "string"}}, ["app"],
            "MOCK: launch a local application",
        ),
    ]


__all__ = ["seed_definitions"]

