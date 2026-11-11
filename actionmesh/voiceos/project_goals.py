"""Durable, independent project registrations and Goal Contracts.

This store owns goal configuration and its append-only observation history. It does
not authorize or execute repairs; observations are dispatched by the existing
VoiceOS/ActionMesh orchestrator and linked here by execution id.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProjectGoalStore:
    """SQLite persistence with additive, backup-first schema installation."""

    def __init__(self, database: str | Path) -> None:
        self.path = str(database)
        self._database_preexisted = self.path != ":memory:" and Path(self.path).is_file()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        schema_row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_goal_schema'"
        ).fetchone()
        schema_version = 0
        if schema_row:
            version_row = self.db.execute(
                "SELECT version FROM project_goal_schema WHERE singleton=1"
            ).fetchone()
            schema_version = int(version_row[0]) if version_row else 0
            if schema_version > 4:
                raise sqlite3.DatabaseError("project goal schema is newer than this ActionMesh build")
        if schema_version < 4 and self._database_preexisted:
            backup = Path(f"{self.path}.pre-v6-2-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.bak")
            target = sqlite3.connect(str(backup))
            try:
                self.db.backup(target)
            finally:
                target.close()
            backup.chmod(0o600)
        with self.db:
            self.db.executescript(
                "CREATE TABLE IF NOT EXISTS project_goal_schema ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);"
                "INSERT OR IGNORE INTO project_goal_schema(singleton,version) VALUES(1,3);"
                "CREATE TABLE IF NOT EXISTS projects ("
                "project_id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE, "
                "kind TEXT NOT NULL DEFAULT 'unknown', kinds_json TEXT NOT NULL DEFAULT '[]', "
                "scripts_json TEXT NOT NULL DEFAULT '[]', owner_id TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);"
                "CREATE TABLE IF NOT EXISTS project_selection ("
                "selection_id INTEGER PRIMARY KEY CHECK(selection_id=1), "
                "project_id TEXT NOT NULL REFERENCES projects(project_id), selected_at TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS goal_contracts ("
                "goal_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), "
                "owner_id TEXT NOT NULL, device_id TEXT NOT NULL, description TEXT NOT NULL, "
                "resource_identity TEXT NOT NULL, "
                "contract_type TEXT NOT NULL, expected_json TEXT NOT NULL, observer TEXT NOT NULL, "
                "interval_seconds INTEGER NOT NULL, recovery_json TEXT NOT NULL, "
                "attempt_budget INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                "monitoring_enabled INTEGER NOT NULL DEFAULT 0, "
                "state TEXT NOT NULL, current_observation TEXT NOT NULL DEFAULT 'UNKNOWN', "
                "last_observed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "archived_at TEXT);"
                "CREATE INDEX IF NOT EXISTS goal_contracts_project "
                "ON goal_contracts(project_id, archived_at, created_at);"
                "CREATE TABLE IF NOT EXISTS goal_events ("
                "event_id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL "
                "REFERENCES goal_contracts(goal_id), event_type TEXT NOT NULL, "
                "occurred_at TEXT NOT NULL, detail TEXT NOT NULL, correlation_id TEXT NOT NULL DEFAULT '', "
                "execution_id TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '{}');"
                "CREATE INDEX IF NOT EXISTS goal_events_goal_time "
                "ON goal_events(goal_id, occurred_at DESC, event_id DESC);"
                "CREATE TRIGGER IF NOT EXISTS goal_events_no_update BEFORE UPDATE ON goal_events "
                "BEGIN SELECT RAISE(ABORT,'goal history is append-only'); END;"
                "CREATE TRIGGER IF NOT EXISTS goal_events_no_delete BEFORE DELETE ON goal_events "
                "BEGIN SELECT RAISE(ABORT,'goal history is append-only'); END;"
                "CREATE TABLE IF NOT EXISTS goal_observations ("
                "observation_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goal_contracts(goal_id), "
                "observer TEXT NOT NULL, target_identity TEXT NOT NULL, observed_at TEXT NOT NULL, "
                "fresh_until TEXT NOT NULL, expected_json TEXT NOT NULL, observed_json TEXT NOT NULL, "
                "verdict TEXT NOT NULL, evidence_digest TEXT NOT NULL DEFAULT '', "
                "execution_id TEXT NOT NULL DEFAULT '');"
                "CREATE INDEX IF NOT EXISTS goal_observations_goal_time "
                "ON goal_observations(goal_id, observed_at DESC);"
                "CREATE TABLE IF NOT EXISTS goal_execution_links ("
                "goal_id TEXT NOT NULL REFERENCES goal_contracts(goal_id), "
                "execution_id TEXT NOT NULL, relation TEXT NOT NULL, created_at TEXT NOT NULL, "
                "PRIMARY KEY(goal_id, execution_id, relation));"
                "CREATE TABLE IF NOT EXISTS managed_services ("
                "service_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), "
                "name TEXT NOT NULL, command TEXT NOT NULL, arguments_json TEXT NOT NULL, "
                "working_directory TEXT NOT NULL, port INTEGER NOT NULL, health_path TEXT NOT NULL, "
                "expected_status INTEGER NOT NULL, timeout_ms INTEGER NOT NULL DEFAULT 1500, "
                "owner_id TEXT NOT NULL, created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, UNIQUE(project_id,name));"
                "CREATE TABLE IF NOT EXISTS goal_repair_attempts ("
                "attempt_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goal_contracts(goal_id), "
                "service_id TEXT NOT NULL REFERENCES managed_services(service_id), "
                "status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, execution_id TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');"
            )
            goal_columns = {
                str(row[1]) for row in self.db.execute("PRAGMA table_info(goal_contracts)").fetchall()
            }
            if "monitoring_enabled" not in goal_columns:
                self.db.execute(
                    "ALTER TABLE goal_contracts ADD COLUMN monitoring_enabled INTEGER NOT NULL DEFAULT 0"
                )
            service_columns = {
                str(row[1]) for row in self.db.execute("PRAGMA table_info(managed_services)").fetchall()
            }
            if "timeout_ms" not in service_columns:
                self.db.execute(
                    "ALTER TABLE managed_services ADD COLUMN timeout_ms INTEGER NOT NULL DEFAULT 1500"
                )
            self.db.execute("UPDATE project_goal_schema SET version=4 WHERE singleton=1")

    @staticmethod
    def _project(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["kinds"] = json.loads(data.pop("kinds_json"))
        data["scripts"] = json.loads(data.pop("scripts_json"))
        return data

    @staticmethod
    def _goal(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for source, target in (("expected_json", "expected"), ("recovery_json", "recovery")):
            data[target] = json.loads(data.pop(source))
        return data

    def register_project(
        self, *, name: str, root: str, kind: str, kinds: list[str],
        scripts: list[str], owner_id: str,
    ) -> dict[str, Any]:
        project_id = uuid.uuid4().hex
        now = _now()
        with self.db:
            self.db.execute(
                "INSERT INTO projects(project_id,name,root,kind,kinds_json,scripts_json,owner_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (project_id, name, root, kind, json.dumps(kinds), json.dumps(scripts), owner_id, now, now),
            )
        return self.get_project(project_id)  # type: ignore[return-value]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        return self._project(row) if row else None

    def list_projects(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM projects"
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        query += " ORDER BY name COLLATE NOCASE, project_id"
        return [self._project(row) for row in self.db.execute(query).fetchall()]

    def select_project(self, project_id: str) -> dict[str, Any] | None:
        project = self.get_project(project_id)
        if project is None or project["archived_at"] is not None:
            return None
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO project_selection(selection_id,project_id,selected_at) "
                "VALUES(1,?,?)", (project_id, _now()),
            )
        return project

    def selected_project(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT p.* FROM projects p JOIN project_selection s USING(project_id) "
            "WHERE s.selection_id=1 AND p.archived_at IS NULL"
        ).fetchone()
        return self._project(row) if row else None

    def register_service(
        self, *, project_id: str, name: str, command: str, arguments: list[str],
        working_directory: str, port: int, health_path: str, expected_status: int,
        owner_id: str, timeout_ms: int = 1500,
    ) -> dict[str, Any]:
        service_id = uuid.uuid4().hex
        now = _now()
        with self.db:
            self.db.execute(
                "INSERT INTO managed_services(service_id,project_id,name,command,arguments_json,"
                "working_directory,port,health_path,expected_status,timeout_ms,owner_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (service_id, project_id, name, command, json.dumps(arguments),
                 working_directory, port, health_path, expected_status, timeout_ms, owner_id, now, now),
            )
        return self.get_service(service_id)  # type: ignore[return-value]

    def get_service(self, service_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM managed_services WHERE service_id=?", (service_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["arguments"] = json.loads(data.pop("arguments_json"))
        return data

    def list_services(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM managed_services WHERE project_id=? ORDER BY name COLLATE NOCASE",
            (project_id,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["arguments"] = json.loads(data.pop("arguments_json"))
            result.append(data)
        return result

    def record_service_event(
        self, service_id: str, *, event_type: str, detail: str, execution_id: str = "",
    ) -> int:
        """Append a service-control event to every goal tracking that service."""
        now = _now()
        with self.db:
            goals = self.db.execute(
                "SELECT goal_id FROM goal_contracts WHERE resource_identity=? "
                "AND contract_type='http_service' AND archived_at IS NULL",
                (service_id,),
            ).fetchall()
            for row in goals:
                goal_id = str(row[0])
                self._event(
                    goal_id, event_type, detail, execution_id=execution_id,
                    evidence={"service_id": service_id},
                )
                if execution_id:
                    self.db.execute(
                        "INSERT OR IGNORE INTO goal_execution_links VALUES(?,?,?,?)",
                        (goal_id, execution_id, "service_control", now),
                    )
        return len(goals)

    def pending_service_executions(self, goal_id: str | None = None) -> list[str]:
        """Return service-control executions whose durable event has no terminal result."""
        query = (
            "SELECT DISTINCT e.execution_id FROM goal_events e "
            "WHERE e.event_type IN ('service.control.awaiting_approval','service.control.executing',"
            "'service.control.verifying','service.control.validating','service.control.authorization') "
            "AND e.execution_id!=''"
        )
        args: list[Any] = []
        if goal_id:
            query += " AND e.goal_id=?"
            args.append(goal_id)
        query += (
            " AND NOT EXISTS (SELECT 1 FROM goal_events terminal "
            "WHERE terminal.goal_id=e.goal_id AND terminal.execution_id=e.execution_id "
            "AND terminal.event_type IN ('service.control.success','service.control.failed',"
            "'service.control.cancelled','service.control.denied','service.control.timeout',"
            "'service.control.interrupted','service.control.partially_completed'))"
        )
        return [str(row[0]) for row in self.db.execute(query, args).fetchall()]

    def reconcile_service_execution(
        self, execution_id: str, *, terminal_state: str, detail: str,
    ) -> int:
        """Append a terminal event once, preserving the original pending event."""
        allowed = {
            "success", "failed", "cancelled", "denied", "timeout", "interrupted",
            "partially_completed",
        }
        if terminal_state not in allowed:
            return 0
        event_type = f"service.control.{terminal_state}"
        now = _now()
        appended = 0
        with self.db:
            goals = self.db.execute(
                "SELECT DISTINCT goal_id FROM goal_events WHERE execution_id=? "
                "AND event_type IN ('service.control.awaiting_approval','service.control.executing',"
                "'service.control.verifying','service.control.validating','service.control.authorization')",
                (execution_id,),
            ).fetchall()
            for row in goals:
                goal_id = str(row[0])
                exists = self.db.execute(
                    "SELECT 1 FROM goal_events WHERE goal_id=? AND execution_id=? AND event_type=?",
                    (goal_id, execution_id, event_type),
                ).fetchone()
                if exists:
                    continue
                self._event(
                    goal_id, event_type, detail, execution_id=execution_id,
                    evidence={"reconciled_from_execution": True},
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO goal_execution_links VALUES(?,?,?,?)",
                    (goal_id, execution_id, "service_control", now),
                )
                appended += 1
        return appended

    def begin_repair(self, goal_id: str, service_id: str, *, budget: int) -> dict[str, Any] | None:
        """Durably reserve one recovery proposal per unhealthy period."""
        now = _now()
        attempt_id = uuid.uuid4().hex
        key = f"goal-repair:{goal_id}:{attempt_id}"
        with self.db:
            prior = self.db.execute(
                "SELECT * FROM goal_repair_attempts WHERE goal_id=? AND status IN "
                "('PENDING_APPROVAL','EXECUTING','INTERRUPTED') ORDER BY created_at DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
            if prior:
                return {**dict(prior), "created": False}
            goal = self.db.execute(
                "SELECT attempts FROM goal_contracts WHERE goal_id=? AND archived_at IS NULL",
                (goal_id,),
            ).fetchone()
            if goal is None or int(goal[0]) >= budget:
                return None
            self.db.execute(
                "INSERT INTO goal_repair_attempts(attempt_id,goal_id,service_id,status,idempotency_key,"
                "execution_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, goal_id, service_id, "PENDING_APPROVAL", key, "", now, now),
            )
            self.db.execute(
                "UPDATE goal_contracts SET attempts=attempts+1,updated_at=? WHERE goal_id=?",
                (now, goal_id),
            )
            self._event(goal_id, "recovery.pending_approval", "Restart proposal awaits ActionMesh approval")
        row = self.db.execute("SELECT * FROM goal_repair_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        return {**dict(row), "created": True} if row else None

    def list_repairs(self, goal_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM goal_repair_attempts WHERE goal_id=? ORDER BY created_at DESC LIMIT 100",
            (goal_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def finish_repair(
        self, attempt_id: str, *, status: str, execution_id: str = "", detail: str = "",
    ) -> dict[str, Any] | None:
        now = _now()
        with self.db:
            row = self.db.execute(
                "SELECT goal_id,status,execution_id FROM goal_repair_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if not row:
                return None
            goal_id = str(row[0])
            previous_status = str(row[1])
            linked_execution = execution_id or str(row[2])
            self.db.execute(
                "UPDATE goal_repair_attempts SET status=?,execution_id=?,updated_at=?,detail=? "
                "WHERE attempt_id=?",
                (status, linked_execution, now, detail[:500], attempt_id),
            )
            if linked_execution:
                self.db.execute(
                    "INSERT OR IGNORE INTO goal_execution_links VALUES(?,?,?,?)",
                    (goal_id, linked_execution, "recovery", now),
                )
            # Dispatch checkpoints and the route's response may both write
            # PENDING_APPROVAL. Keep those idempotent while still recording the
            # execution link; the timeline event belongs to an actual transition.
            if previous_status != status:
                self._event(
                    goal_id, f"recovery.{status.lower()}", detail or f"Recovery {status.lower()}",
                    execution_id=linked_execution,
                )
        result = self.db.execute(
            "SELECT * FROM goal_repair_attempts WHERE attempt_id=?", (attempt_id,),
        ).fetchone()
        return dict(result) if result else None

    def record_repair_dispatch(self, attempt_id: str, *, status: str, execution_id: str) -> None:
        """Persist the execution handle as soon as the orchestrator creates it."""
        with self.db:
            self.db.execute(
                "UPDATE goal_repair_attempts SET status=?,execution_id=?,updated_at=? WHERE attempt_id=?",
                (status, execution_id, _now(), attempt_id),
            )

    def create_goal(
        self, *, project_id: str, owner_id: str, device_id: str, description: str,
        resource_identity: str,
        contract_type: str, expected: dict[str, Any], observer: str, interval_seconds: int,
        recovery: dict[str, Any], attempt_budget: int,
        monitoring_enabled: bool = False,
        baseline_execution_id: str = "",
    ) -> dict[str, Any]:
        goal_id = uuid.uuid4().hex
        now = _now()
        with self.db:
            self.db.execute(
                "INSERT INTO goal_contracts(goal_id,project_id,owner_id,device_id,description,resource_identity,"
                "contract_type,expected_json,observer,interval_seconds,recovery_json,attempt_budget,"
                "monitoring_enabled,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (goal_id, project_id, owner_id, device_id, description[:120], resource_identity, contract_type,
                 json.dumps(expected, sort_keys=True), observer, interval_seconds,
                 json.dumps(recovery, sort_keys=True), attempt_budget, int(monitoring_enabled), "ACTIVE", now, now),
            )
            if baseline_execution_id:
                self.db.execute(
                    "INSERT INTO goal_execution_links VALUES(?,?,?,?)",
                    (goal_id, baseline_execution_id, "baseline_capture", now),
                )
            self._event(
                goal_id, "goal.created", "Goal Contract created",
                execution_id=baseline_execution_id,
                evidence={"baseline_capture": baseline_execution_id} if baseline_execution_id else None,
            )
        return self.get_goal(goal_id)  # type: ignore[return-value]

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM goal_contracts WHERE goal_id=?", (goal_id,)).fetchone()
        return self._goal(row) if row else None

    def list_goals(
        self, *, project_id: str | None = None, include_archived: bool = False,
        limit: int = 100, offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        if not include_archived:
            where.append("archived_at IS NULL")
        if project_id:
            where.append("project_id=?")
            args.append(project_id)
        query = "SELECT * FROM goal_contracts"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC, goal_id LIMIT ? OFFSET ?"
        args.extend((limit, offset))
        return [self._goal(row) for row in self.db.execute(query, args).fetchall()]

    def monitored_goals(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM goal_contracts WHERE state='ACTIVE' AND archived_at IS NULL "
            "AND monitoring_enabled=1 ORDER BY updated_at,goal_id"
        ).fetchall()
        return [self._goal(row) for row in rows]

    def set_state(self, goal_id: str, state: str) -> dict[str, Any] | None:
        now = _now()
        with self.db:
            cursor = self.db.execute(
                "UPDATE goal_contracts SET state=?,updated_at=? WHERE goal_id=? AND archived_at IS NULL",
                (state, now, goal_id),
            )
            if cursor.rowcount:
                self._event(goal_id, f"goal.{state.lower()}", f"Goal {state.lower()}")
        return self.get_goal(goal_id)

    def set_monitoring(self, goal_id: str, enabled: bool) -> dict[str, Any] | None:
        now = _now()
        with self.db:
            cursor = self.db.execute(
                "UPDATE goal_contracts SET monitoring_enabled=?,updated_at=? "
                "WHERE goal_id=? AND archived_at IS NULL",
                (int(enabled), now, goal_id),
            )
            if cursor.rowcount:
                self._event(
                    goal_id, "goal.monitoring", f"Monitoring {'enabled' if enabled else 'disabled'}"
                )
        return self.get_goal(goal_id)

    def update_goal(
        self, goal_id: str, *, description: str | None = None,
        resource_identity: str | None = None, expected: dict[str, Any] | None = None,
        interval_seconds: int | None = None, monitoring_enabled: bool | None = None,
        baseline_execution_id: str = "",
    ) -> dict[str, Any] | None:
        before = self.get_goal(goal_id)
        if before is None or before["archived_at"] is not None:
            return None
        now = _now()
        with self.db:
            self.db.execute(
                "UPDATE goal_contracts SET description=?,resource_identity=?,expected_json=?,"
                "interval_seconds=?,monitoring_enabled=?,updated_at=? WHERE goal_id=? AND archived_at IS NULL",
                (
                    (description if description is not None else before["description"])[:120],
                    resource_identity if resource_identity is not None else before["resource_identity"],
                    json.dumps(expected if expected is not None else before["expected"], sort_keys=True),
                    interval_seconds if interval_seconds is not None else before["interval_seconds"],
                    int(monitoring_enabled if monitoring_enabled is not None else before["monitoring_enabled"]),
                    now, goal_id,
                ),
            )
            if baseline_execution_id:
                self.db.execute(
                    "INSERT OR IGNORE INTO goal_execution_links VALUES(?,?,?,?)",
                    (goal_id, baseline_execution_id, "baseline_capture", now),
                )
            self._event(
                goal_id, "goal.updated", "Goal Contract updated",
                execution_id=baseline_execution_id,
                evidence={"baseline_capture": baseline_execution_id} if baseline_execution_id else None,
            )
        return self.get_goal(goal_id)

    def archive_goal(self, goal_id: str) -> dict[str, Any] | None:
        now = _now()
        with self.db:
            cursor = self.db.execute(
                "UPDATE goal_contracts SET state='ARCHIVED',archived_at=?,updated_at=? "
                "WHERE goal_id=? AND archived_at IS NULL", (now, now, goal_id),
            )
            if cursor.rowcount:
                self._event(goal_id, "goal.archived", "Goal archived")
        return self.get_goal(goal_id)

    def record_observation(
        self, goal_id: str, *, observer: str, target_identity: str, expected: dict[str, Any],
        observed: dict[str, Any], verdict: str, fresh_until: str, execution_id: str,
        evidence_digest: str = "",
    ) -> dict[str, Any] | None:
        now = _now()
        observation_id = uuid.uuid4().hex
        with self.db:
            cursor = self.db.execute(
                "UPDATE goal_contracts SET current_observation=?,last_observed_at=?,updated_at=?,"
                "attempts=CASE WHEN ?='VERIFIED' THEN 0 ELSE attempts END "
                "WHERE goal_id=? AND archived_at IS NULL",
                (verdict, now, now, verdict, goal_id),
            )
            if not cursor.rowcount:
                return None
            self.db.execute(
                "INSERT INTO goal_observations(observation_id,goal_id,observer,target_identity,observed_at,"
                "fresh_until,expected_json,observed_json,verdict,evidence_digest,execution_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (observation_id, goal_id, observer, target_identity, now, fresh_until,
                 json.dumps(expected, sort_keys=True), json.dumps(observed, sort_keys=True),
                 verdict, evidence_digest, execution_id),
            )
            if execution_id:
                self.db.execute(
                    "INSERT OR IGNORE INTO goal_execution_links VALUES(?,?,?,?)",
                    (goal_id, execution_id, "observation", now),
                )
            self._event(
                goal_id, "goal.observed", f"Observation verdict: {verdict}",
                execution_id=execution_id, evidence={"observation_id": observation_id},
            )
        return self.get_goal(goal_id)

    def _event(
        self, goal_id: str, event_type: str, detail: str, *,
        execution_id: str = "", evidence: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        self.db.execute(
            "INSERT INTO goal_events(goal_id,event_type,occurred_at,detail,correlation_id,execution_id,evidence_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (goal_id, event_type, now, detail[:500], uuid.uuid4().hex, execution_id,
             json.dumps(evidence or {}, sort_keys=True)),
        )

    def list_events(
        self, goal_id: str | None, *, limit: int = 50, offset: int = 0,
        event_type: str | None = None, project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        if goal_id:
            where.append("e.goal_id=?")
            args.append(goal_id)
        if event_type:
            where.append("e.event_type=?")
            args.append(event_type)
        if project_id:
            where.append("g.project_id=?")
            args.append(project_id)
        query = "SELECT e.* FROM goal_events e JOIN goal_contracts g USING(goal_id)"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY e.occurred_at DESC,e.event_id DESC LIMIT ? OFFSET ?"
        args.extend((limit, offset))
        rows = self.db.execute(query, args).fetchall()
        return [
            {**dict(row), "evidence": json.loads(row["evidence_json"])}
            for row in rows
        ]

    def list_observations(self, goal_id: str, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM goal_observations WHERE goal_id=? ORDER BY observed_at DESC LIMIT ? OFFSET ?",
            (goal_id, limit, offset),
        ).fetchall()
        return [
            {**dict(row), "expected": json.loads(row["expected_json"]),
             "observed": json.loads(row["observed_json"])}
            for row in rows
        ]

    def close(self) -> None:
        self.db.close()
