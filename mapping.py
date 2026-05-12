"""
mapping.py — Backend logique de mapping CSV → SQL
Fonctions : suggestions, détection des changements, test, persistance SQL.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import math
from datetime import datetime

import pandas as pd
import pyodbc


# ─────────────────────────────────────────────
# SUGGESTIONS AUTOMATIQUES
# ─────────────────────────────────────────────

def _sim(a: str, b: str) -> float:
    a = a.lower().replace("_", "").replace(" ", "").replace("-", "")
    b = b.lower().replace("_", "").replace(" ", "").replace("-", "")
    return SequenceMatcher(None, a, b).ratio()


def suggest_mapping(
    csv_cols: list[str],
    sql_cols: list[str],
    threshold: float = 0.55,
) -> dict[str, str]:
    """
    Retourne {csv_col: sql_col} par correspondance de noms (exact puis fuzzy).
    Ne propose pas deux fois la même colonne SQL.
    """
    result: dict[str, str] = {}
    used: set[str] = set()

    for csv_col in csv_cols:
        # Correspondance exacte
        if csv_col in sql_cols:
            result[csv_col] = csv_col
            used.add(csv_col)
            continue
        # Fuzzy
        best_sql, best_score = None, 0.0
        for sql_col in sql_cols:
            if sql_col in used:
                continue
            s = _sim(csv_col, sql_col)
            if s > best_score:
                best_score, best_sql = s, sql_col
        if best_score >= threshold and best_sql:
            result[csv_col] = best_sql
            used.add(best_sql)

    return result


# ─────────────────────────────────────────────
# DÉTECTION DES CHANGEMENTS
# ─────────────────────────────────────────────

def detect_changes(csv_cols: list[str], saved_mapping: dict[str, str]) -> dict:
    """
    Compare les colonnes du fichier courant avec un mapping sauvegardé.
    Retourne :
      missing   — étaient mappées, absentes dans le fichier actuel
      new       — présentes dans le fichier, non mappées
      unchanged — présentes et déjà mappées
    """
    saved_csv = set(saved_mapping.keys())
    current   = set(csv_cols)
    return {
        "missing":   sorted(saved_csv - current),
        "new":       sorted(current   - saved_csv),
        "unchanged": sorted(current   & saved_csv),
    }


# ─────────────────────────────────────────────
# TEST DU MAPPING (preview)
# ─────────────────────────────────────────────

def _try_coerce(v, sql_type: str):
    """Conversion légère pour le test — même logique que _coerce_value dans app.py."""
    try:
        if v is not None and isinstance(v, float) and math.isnan(v):
            return None
    except Exception:
        pass
    if v is None or str(v).strip() in ("", "NULL", "null", "None", "NaN", "nan"):
        return None
    s = str(v).strip()
    t = sql_type.upper()
    if any(x in t for x in ("INT", "BIGINT", "SMALLINT", "TINYINT")):
        return int(float(s))
    if any(x in t for x in ("DECIMAL", "NUMERIC", "FLOAT", "REAL", "MONEY")):
        return float(s.replace(",", "."))
    if any(x in t for x in ("DATE", "TIME", "DATETIME")):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
                    "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
    if "BIT" in t:
        return 1 if s.lower() in ("1", "true", "oui", "yes") else 0
    return s


def test_mapping(
    df: pd.DataFrame,
    mapping: dict[str, str],
    col_types: dict[str, str],
    n_rows: int = 10,
) -> dict:
    """
    Applique le mapping sur les n_rows premières lignes de df.
    Retourne :
      errors      — liste de messages d'erreur de conversion
      unmapped    — colonnes CSV non mappées
      preview_df  — DataFrame après mapping + coercion
    """
    unmapped = [c for c in df.columns if c not in mapping]
    errors: list[str] = []
    preview_rows: list[dict] = []

    for _, row in df.head(n_rows).iterrows():
        coerced: dict = {}
        for csv_col, sql_col in mapping.items():
            if csv_col not in df.columns:
                continue
            val = row[csv_col]
            sql_type = col_types.get(sql_col, "")
            try:
                coerced[sql_col] = _try_coerce(val, sql_type)
            except Exception as e:
                errors.append(
                    f"`{csv_col}` → `{sql_col}` (type SQL : {sql_type or '?'}) — "
                    f"valeur={repr(val)} — {e}"
                )
                coerced[sql_col] = val
        preview_rows.append(coerced)

    preview_df = pd.DataFrame(preview_rows) if preview_rows else pd.DataFrame()
    return {"errors": errors, "unmapped": unmapped, "preview_df": preview_df}


# ─────────────────────────────────────────────
# PERSISTANCE SQL
# ─────────────────────────────────────────────

_DDL = """
IF OBJECT_ID('dbo.import_mapping', 'U') IS NULL
CREATE TABLE dbo.import_mapping (
    mapping_id   INT IDENTITY(1,1) PRIMARY KEY,
    target_table NVARCHAR(200) NOT NULL,
    csv_column   NVARCHAR(200) NOT NULL,
    sql_column   NVARCHAR(200) NOT NULL,
    version      INT          NOT NULL,
    created_at   DATETIME     DEFAULT GETDATE()
);
"""


def _ensure_table(cursor) -> None:
    cursor.execute(_DDL)


def save_mapping(conn, target_table: str, mapping: dict[str, str]) -> int:
    """Insère le mapping dans dbo.import_mapping. Retourne le numéro de version."""
    cursor = conn.cursor()
    _ensure_table(cursor)
    cursor.execute(
        "SELECT ISNULL(MAX(version), 0) + 1 FROM dbo.import_mapping WHERE target_table = ?",
        target_table,
    )
    version = cursor.fetchone()[0]
    for csv_col, sql_col in mapping.items():
        cursor.execute(
            "INSERT INTO dbo.import_mapping (target_table, csv_column, sql_column, version) "
            "VALUES (?, ?, ?, ?)",
            target_table, csv_col, sql_col, version,
        )
    conn.commit()
    return version


def load_mapping(conn, target_table: str) -> dict[str, str]:
    """Retourne {csv_col: sql_col} pour la dernière version sauvegardée."""
    cursor = conn.cursor()
    _ensure_table(cursor)
    cursor.execute(
        """
        SELECT csv_column, sql_column
        FROM   dbo.import_mapping
        WHERE  target_table = ?
          AND  version = (
              SELECT MAX(version) FROM dbo.import_mapping WHERE target_table = ?
          )
        """,
        target_table, target_table,
    )
    return {r[0]: r[1] for r in cursor.fetchall()}


def load_history(conn, target_table: str) -> pd.DataFrame:
    """Retourne l'historique de tous les mappings pour une table."""
    cursor = conn.cursor()
    _ensure_table(cursor)
    cursor.execute(
        """
        SELECT version, csv_column, sql_column, created_at
        FROM   dbo.import_mapping
        WHERE  target_table = ?
        ORDER  BY version DESC, csv_column
        """,
        target_table,
    )
    rows = cursor.fetchall()
    return pd.DataFrame(
        [tuple(r) for r in rows],
        columns=["Version", "Colonne CSV", "Colonne SQL", "Créé le"],
    )
