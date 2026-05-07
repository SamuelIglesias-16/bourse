"""FastAPI router for price alert CRUD — data lives in SQLite (bourse.db)."""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from bourse.alerts import _ensure_price_alerts_table
from bourse.db import DB_PATH, get_connection

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _ensure_schema() -> None:
    """Create price_alerts table and add query column if missing."""
    _ensure_price_alerts_table()
    with get_connection(DB_PATH) as conn:
        try:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN query TEXT")
        except Exception:
            pass  # column already exists


class AlertCreate(BaseModel):
    query: str
    max_price_sek: int
    category: Optional[str] = None
    size: Optional[str] = None


@router.get("")
def get_alerts() -> list[dict[str, Any]]:
    _ensure_schema()
    with get_connection(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, query, category, size, max_price, active, created_at"
            "  FROM price_alerts WHERE active = 1 ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
def create_alert(alert: AlertCreate) -> dict[str, Any]:
    _ensure_schema()
    with get_connection(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO price_alerts (query, category, size, max_price) VALUES (?, ?, ?, ?)",
            (alert.query, alert.category, alert.size, alert.max_price_sek),
        )
        new_id = cur.lastrowid
    return {
        "id": new_id,
        "query": alert.query,
        "category": alert.category,
        "size": alert.size,
        "max_price_sek": alert.max_price_sek,
        "active": True,
    }


@router.delete("/{alert_id}")
def delete_alert(alert_id: int) -> dict[str, Any]:
    _ensure_schema()
    with get_connection(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE price_alerts SET active = 0 WHERE id = ? AND active = 1",
            (alert_id,),
        )
        affected = cur.rowcount
    if affected == 0:
        raise HTTPException(404, "Alert not found or already inactive")
    return {"id": alert_id, "active": False}
