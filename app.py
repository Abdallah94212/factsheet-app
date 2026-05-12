import os
import csv
import io
import requests as _requests
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyodbc
import streamlit as st

from queries import QUERIES
from mapping import suggest_mapping, detect_changes, test_mapping, save_mapping, load_mapping, load_history

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Data Platform",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

def _build_conn_str(database="stage"):
    _db_cfg = st.secrets.get("db", {})
    _srv = _db_cfg.get("server",   "127.0.0.1,1433")
    _usr = _db_cfg.get("username", "sa")
    _pwd = _db_cfg.get("password", "Ssql!2026Test123")
    _db  = _db_cfg.get("database", database)
    return (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={_srv};"
        f"DATABASE={_db};"
        f"UID={_usr};PWD={_pwd};"
        "Encrypt=yes;TrustServerCertificate=yes;"
    )

CONN_STR = _build_conn_str("stage")

DEFAULT_OUTPUT_DIR = "/Users/abdallahborji/script_valider"
SCRIPTS_DIR = Path(os.getcwd()) / "user_scripts"
SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_connection():
    return pyodbc.connect(CONN_STR)



def _detect_sep(text: str) -> str:
    header = ""
    for line in text.splitlines():
        if line.strip():
            header = line
            break
    candidates = [",", ";", "|", "\t"]
    if header:
        counts = {d: header.count(d) for d in candidates}
        best = max(counts, key=counts.get)
        if counts[best] > 0:
            return best
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=candidates)
        return dialect.delimiter
    except Exception:
        return ","


def _coerce_value(v, sql_type: str = ""):
    """Convertit une valeur en type Python adapté au type SQL cible."""
    import math

    # NaN pandas → NULL SQL
    try:
        if v is not None and isinstance(v, float) and math.isnan(v):
            return None
    except Exception:
        pass

    if v is None or str(v).strip() in ("", "NULL", "null", "None", "NaN", "nan"):
        return None

    s = str(v).strip()
    t = sql_type.upper()

    # Types entiers
    if any(x in t for x in ("INT", "BIGINT", "SMALLINT", "TINYINT")):
        try:
            return int(float(s))
        except ValueError:
            return s

    # Types décimaux
    if any(x in t for x in ("DECIMAL", "NUMERIC", "FLOAT", "REAL", "MONEY")):
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return s

    # Types date / datetime
    if any(x in t for x in ("DATE", "TIME", "DATETIME")):
        from datetime import datetime
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d",
                    "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return s  # laisse SQL tenter la conversion

    # Type bit / boolean
    if "BIT" in t:
        return 1 if s.lower() in ("1", "true", "oui", "yes") else 0

    # Fallback : essai numérique générique puis string
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s.replace(",", "."))
    except ValueError:
        pass
    return s


def _get_column_types(cursor, schema: str, table: str) -> dict[str, str]:
    """Retourne {column_name: data_type} depuis INFORMATION_SCHEMA."""
    cursor.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        schema, table,
    )
    return {row[0]: row[1] for row in cursor.fetchall()}


def _quote_ident(name: str) -> str:
    name = str(name).strip()
    return name if (name.startswith("[") and name.endswith("]")) else f"[{name}]"


def _split_table(table_name: str) -> tuple[str, str]:
    t = str(table_name).strip().strip(";")
    if "." in t:
        schema, table = t.split(".", 1)
        return schema.strip(), table.strip()
    return "dbo", t


def list_user_tables(conn) -> list[tuple[str, str]]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT TABLE_SCHEMA, TABLE_NAME "
        "FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE = 'BASE TABLE' "
        "ORDER BY TABLE_SCHEMA, TABLE_NAME"
    )
    return cursor.fetchall()


# ─────────────────────────────────────────────
# SIDEBAR NAVIGATION
# ─────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📊 Data Platform")
    st.markdown("---")
    page = st.radio(
        "Navigation",
        options=["📤 Export SQL → CSV", "📥 Import & Gestion des Tables", "🧑‍💻 Scripts Python", "🤖 Automatisation n8n", "📊 Tableau de bord", "🔬 Analyse SQL → Graphique"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    st.caption("SQL Server · stage")


# ═════════════════════════════════════════════
# PAGE 1 — EXPORT SQL → CSV
# ═════════════════════════════════════════════

if page == "📤 Export SQL → CSV":
    st.title("📤 Export SQL → CSV")
    st.caption("Sélectionnez une requête, exécutez-la et téléchargez le résultat en CSV.")
    st.markdown("---")

    # ── Sélecteur de requête ─────────────────
    query_labels = ["— Requête personnalisée —"] + list(QUERIES.keys())
    selected = st.selectbox("Choisir une requête", query_labels)

    if selected == "— Requête personnalisée —":
        default_sql = ""
        description = ""
    else:
        default_sql = QUERIES[selected]["sql"]
        description = QUERIES[selected]["description"]

    if description:
        st.caption(f"📋 {description}")

    query = st.text_area(
        "SQL",
        value=default_sql,
        height=220,
        label_visibility="collapsed",
        placeholder="Écrivez votre requête SELECT ici…",
    )

    st.markdown("")

    # ── Options export ───────────────────────
    c1, c2 = st.columns(2)
    with c1:
        filename = st.text_input(
            "Nom du fichier CSV",
            value=f"{selected.split(' — ')[0].lower()}.csv" if selected != "— Requête personnalisée —" else "export.csv",
        )
    with c2:
        separator = st.selectbox("Séparateur", [";", ",", "|"])

    st.markdown("---")

    # ── Exécution ────────────────────────────
    if st.button("▶️ Exécuter et télécharger le CSV", type="primary", use_container_width=True):
        if not query.strip():
            st.warning("Veuillez saisir ou sélectionner une requête SQL.")
        else:
            try:
                with st.spinner("Exécution…"):
                    conn = get_connection()
                    df = pd.read_sql(query, conn)
                    conn.close()

                csv_bytes = df.to_csv(sep=separator, index=False, encoding="utf-8").encode("utf-8")

                st.success(f"✅ **{len(df)} lignes** · {len(df.columns)} colonnes")
                st.download_button(
                    "⬇️ Télécharger le CSV",
                    data=csv_bytes,
                    file_name=filename,
                    mime="text/csv",
                    use_container_width=True,
                )
                st.dataframe(df, use_container_width=True)

            except Exception as e:
                st.error(f"❌ Erreur : {e}")


# ═════════════════════════════════════════════
# PAGE 2 — IMPORT & GESTION DES TABLES
# ═════════════════════════════════════════════

elif page == "📥 Import & Gestion des Tables":
    st.title("📥 Import & Gestion des Tables")
    st.caption("Importez des fichiers CSV dans vos tables SQL Server et gérez vos données.")
    st.markdown("---")

    tab_import, tab_mapping, tab_tables = st.tabs(["📂 Import CSV → Table SQL", "🗺️ Mapping", "🗂️ Explorer les Tables"])

    # ── TAB 1 : Import CSV ───────────────────
    with tab_import:
        st.markdown("### Importer un fichier CSV dans une table SQL")
        st.markdown("")

        # Source du fichier
        st.markdown("**Source du fichier**")
        source_mode = st.radio(
            "Mode de sélection",
            ["Uploader un fichier", "Chemin sur le serveur"],
            horizontal=True,
            label_visibility="collapsed",
        )

        uploaded_file = None
        file_path_str = ""

        if source_mode == "Uploader un fichier":
            uploaded_file = st.file_uploader("Choisir un fichier CSV", type=["csv", "txt"])
        else:
            file_path_str = st.text_input("Chemin complet du fichier", placeholder="ex: /data/mon_fichier.csv")

        st.markdown("")

        # Options d'import
        st.markdown("**Options d'import**")
        c1, c2, c3 = st.columns(3)
        with c1:
            sep_option = st.selectbox("Séparateur de colonnes", ["Auto-détection", ";", ",", "|", "\\t"])
        with c2:
            use_header = st.selectbox(
                "Première ligne = en-tête ?",
                ["Oui — utiliser comme noms de colonnes", "Non — générer automatiquement"],
                help="Oui : la 1ʳᵉ ligne devient les noms de colonnes. Non : colonnes nommées col_0, col_1…"
            )
        with c3:
            encoding_choice = st.selectbox("Encodage", ["utf-8-sig", "utf-8", "latin-1", "cp1252"],
                help="utf-8-sig recommandé pour fichiers Windows/Excel")

        has_header = use_header.startswith("Oui")

        c1, c2 = st.columns([3, 1])
        with c1:
            table_name = st.text_input("Table cible (ex: dbo.ma_table)", placeholder="dbo.ma_table")
        with c2:
            skip_duplicates = st.checkbox(
                "Ignorer les doublons", value=True,
                help="Ignore silencieusement les lignes en conflit (doublon PK/UNIQUE)."
            )

        # Import = toujours : insère si nouvelle ligne, met à jour si elle existe déjà
        import_mode = "Mettre à jour (UPSERT)"

        st.markdown("")

        # Aperçu
        preview_df = None
        raw_text = None

        if uploaded_file is not None:
            raw_text = uploaded_file.read().decode(encoding_choice, errors="ignore")
            uploaded_file.seek(0)
        elif file_path_str and Path(file_path_str).exists():
            raw_text = Path(file_path_str).read_text(encoding=encoding_choice, errors="ignore")

        if raw_text is not None:
            auto_sep = _detect_sep(raw_text)
            sep_actual = auto_sep if sep_option == "Auto-détection" else (
                "\t" if sep_option == "\\t" else sep_option
            )

            header_arg = 0 if has_header else None

            try:
                preview_df = pd.read_csv(
                    io.StringIO(raw_text),
                    sep=sep_actual,
                    header=header_arg,
                    engine="python",
                    dtype=str,
                    on_bad_lines="skip",
                    keep_default_na=False,
                )
                if not has_header:
                    preview_df.columns = [f"col_{i}" for i in range(len(preview_df.columns))]
                else:
                    preview_df.columns = [str(c).strip() for c in preview_df.columns]

                # Avertissement si les noms de colonnes ressemblent à des données
                if has_header:
                    numeric_cols = sum(1 for c in preview_df.columns if str(c).strip().lstrip("-").isdigit())
                    if numeric_cols > 0:
                        st.warning(
                            f"⚠️ **{numeric_cols} colonne(s) ont un nom numérique** "
                            f"(`{'`, `'.join(str(c) for c in preview_df.columns)}`). "
                            "Il est probable que ce fichier **n'a pas de ligne d'en-tête**. "
                            "Passez l'option à **\"Non — générer automatiquement\"**."
                        )

                st.markdown("**Colonnes détectées**")
                st.code(", ".join(str(c) for c in preview_df.columns))

                st.markdown("**Aperçu des données** (10 premières lignes)")
                display_df = preview_df.head(10).copy()
                display_df.index = range(1, len(display_df) + 1)
                st.dataframe(display_df, use_container_width=True)
                st.caption(
                    f"Séparateur : `{repr(sep_actual)}` · "
                    f"{len(preview_df)} lignes · {len(preview_df.columns)} colonnes · "
                    f"En-tête : {'oui' if has_header else 'non'} · "
                    f"Les numéros de lignes commencent à 1 (comme Azure)"
                )
            except Exception as e:
                st.error(f"Impossible de lire le fichier : {e}")

        st.markdown("---")

        # ── Mapping colonnes ─────────────────────
        mapping: dict[str, str] = {}
        sql_columns: list[str] = []

        if preview_df is not None and table_name.strip():
            try:
                conn = get_connection()
                cursor = conn.cursor()
                schema, tbl = _split_table(table_name)
                cursor.execute(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                    schema, tbl,
                )
                sql_columns = [row[0] for row in cursor.fetchall()]
                conn.close()
            except Exception:
                pass

            csv_cols = list(preview_df.columns)
            sql_options = ["— ignorer —"] + (sql_columns if sql_columns else csv_cols)

            def _default_sql_col(i: int, csv_col: str) -> str:
                if csv_col in sql_options:
                    return csv_col
                target = sql_columns if sql_columns else csv_cols
                return target[i] if i < len(target) else "— ignorer —"

            # Calcule le mapping par défaut pour l'afficher dans le résumé
            default_mapping = {}
            for i, csv_col in enumerate(csv_cols):
                default = _default_sql_col(i, csv_col)
                if default != "— ignorer —":
                    default_mapping[csv_col] = default

            st.subheader("🔀 Mapping des colonnes")
            st.caption("Le mapping est fait automatiquement. Modifiez si besoin, ou choisissez **— ignorer —** pour exclure une colonne.")

            cols_per_row = 3
            for i in range(0, len(csv_cols), cols_per_row):
                row_cols = st.columns(cols_per_row)
                for j, csv_col in enumerate(csv_cols[i: i + cols_per_row]):
                    with row_cols[j]:
                        default = _default_sql_col(i + j, csv_col)
                        default_idx = sql_options.index(default) if default in sql_options else 0
                        chosen = st.selectbox(f"`{csv_col}`", options=sql_options, index=default_idx, key=f"map_{csv_col}")
                        if chosen != "— ignorer —":
                            mapping[csv_col] = chosen

            if not mapping:
                st.warning("⚠️ Aucune colonne mappée — l'import est désactivé.")
            else:
                st.caption(f"**{len(mapping)} colonne(s) mappée(s) :** {', '.join(f'`{k}` → `{v}`' for k, v in mapping.items())}")

        st.markdown("---")

        # Bouton d'import
        import_btn = st.button(
            "⬆️ Lancer l'import dans SQL Server",
            type="primary",
            use_container_width=True,
            disabled=(preview_df is None or not table_name.strip() or not mapping),
        )

        # Sélecteur de clé pour UPSERT
        upsert_key_cols: list[str] = []
        if import_mode == "Mettre à jour (UPSERT)" and mapping:
            sql_mapped_cols = list(mapping.values())
            upsert_key_cols = st.multiselect(
                "Colonne(s) clé — identifiant unique de la ligne",
                options=sql_mapped_cols,
                default=sql_mapped_cols[:1],
                help=(
                    "Sélectionnez la ou les colonnes qui identifient une ligne de façon unique.\n"
                    "Ex : nav_id seul, ou nav_date + portfolio_id si la clé est composite."
                ),
            )

        if import_btn and preview_df is not None and table_name.strip() and mapping:
            try:
                with st.spinner("Import en cours…"):
                    import_df = preview_df[list(mapping.keys())].copy()
                    import_df.rename(columns=mapping, inplace=True)

                    conn = get_connection()
                    cursor = conn.cursor()
                    schema, tbl = _split_table(table_name)
                    full_table = f"{_quote_ident(schema)}.{_quote_ident(tbl)}"

                    # Récupère les types SQL pour coercion précise
                    col_types = _get_column_types(cursor, schema, tbl)

                    cols_sql = ", ".join([_quote_ident(c) for c in import_df.columns])
                    placeholders = ", ".join(["?" for _ in import_df.columns])
                    insert_sql = f"INSERT INTO {full_table} ({cols_sql}) VALUES ({placeholders})"
                    rows = [
                        tuple(
                            _coerce_value(v, col_types.get(col, ""))
                            for v, col in zip(row, import_df.columns)
                        )
                        for row in import_df.itertuples(index=False, name=None)
                    ]
                    inserted, skipped, updated = 0, 0, 0

                    if import_mode == "Mettre à jour (UPSERT)" and upsert_key_cols:
                        non_key_cols = [c for c in import_df.columns if c not in upsert_key_cols]
                        update_set = ", ".join(f"{_quote_ident(c)} = ?" for c in non_key_cols)
                        where_clause = " AND ".join(f"{_quote_ident(k)} = ?" for k in upsert_key_cols)
                        update_sql = f"UPDATE {full_table} SET {update_set} WHERE {where_clause}"

                        conflict_rows = []
                        for row in rows:
                            row_dict = dict(zip(import_df.columns, row))
                            key_vals = tuple(row_dict[k] for k in upsert_key_cols)
                            cursor.execute(f"SELECT COUNT(*) FROM {full_table} WHERE {where_clause}", key_vals)
                            exists = cursor.fetchone()[0] > 0
                            if exists:
                                update_vals = tuple(row_dict[c] for c in non_key_cols) + key_vals
                                cursor.execute(update_sql, update_vals)
                                updated += 1
                            else:
                                try:
                                    cursor.execute(insert_sql, row)
                                    inserted += 1
                                except pyodbc.IntegrityError as e:
                                    err_msg = str(e)
                                    # 547 = FK violation / 2627 & 2601 = PK/UNIQUE violation
                                    if "547" in err_msg or "FOREIGN KEY" in err_msg.upper() or "REFERENCE" in err_msg.upper():
                                        conflict_rows.append(("fk", row_dict, err_msg))
                                    else:
                                        conflict_rows.append(("unique", row_dict, err_msg))
                        conn.commit()

                        fk_errors   = [(r, m) for t, r, m in conflict_rows if t == "fk"]
                        uniq_errors = [(r, m) for t, r, m in conflict_rows if t == "unique"]

                        if fk_errors:
                            for row_dict, err_msg in fk_errors:
                                # Extrait le nom de la table référencée depuis le message SQL Server
                                import re
                                match = re.search(r'table "([^"]+)"', err_msg)
                                ref_table = match.group(1) if match else "une table liée"
                                key_vals_str = ", ".join(f"{k}={row_dict.get(k)}" for k in upsert_key_cols)
                                st.error(
                                    f"❌ **Clé étrangère manquante** ({key_vals_str}) : "
                                    f"la valeur référencée n'existe pas dans `{ref_table}`. "
                                    f"Insérez d'abord la ligne correspondante dans cette table."
                                )

                        if uniq_errors:
                            vals = ", ".join(
                                str(tuple(r.get(k) for k in upsert_key_cols)) for r, _ in uniq_errors
                            )
                            st.warning(
                                f"⚠️ **{len(uniq_errors)} doublon(s)** ignoré(s) : {vals}."
                            )

                    else:  # Insérer
                        if skip_duplicates:
                            fk_errors_insert = []
                            for row in rows:
                                try:
                                    cursor.execute(insert_sql, row)
                                    inserted += 1
                                except pyodbc.IntegrityError as e:
                                    err_msg = str(e)
                                    if "547" in err_msg or "FOREIGN KEY" in err_msg.upper():
                                        fk_errors_insert.append((dict(zip(import_df.columns, row)), err_msg))
                                    else:
                                        skipped += 1
                            conn.commit()
                            if fk_errors_insert:
                                import re
                                for row_dict, err_msg in fk_errors_insert:
                                    match = re.search(r'table "([^"]+)"', err_msg)
                                    ref_table = match.group(1) if match else "une table liée"
                                    st.error(
                                        f"❌ **Clé étrangère manquante** : "
                                        f"la valeur référencée n'existe pas dans `{ref_table}`. "
                                        f"Insérez d'abord la ligne dans cette table."
                                    )
                        else:
                            cursor.fast_executemany = True
                            cursor.executemany(insert_sql, rows)
                            conn.commit()
                            inserted = len(rows)

                    conn.close()

                if inserted:
                    st.success(f"✅ **{inserted} ligne(s) insérée(s)** dans `{table_name}`.")
                if updated:
                    st.success(f"🔄 **{updated} ligne(s) mise(s) à jour** dans `{table_name}`.")
                if skipped:
                    st.info(f"ℹ️ **{skipped} ligne(s) ignorée(s)** (doublons).")

            except Exception as e:
                st.error(f"❌ Erreur lors de l'import : {e}")

    # ── TAB 2 : Mapping ──────────────────────
    with tab_mapping:
        st.markdown("### 🗺️ Mapping CSV → SQL")
        st.caption("Définissez, testez, modifiez et sauvegardez le mapping entre vos fichiers et vos tables SQL.")
        st.markdown("")

        # ── Fichier & Table ──────────────────
        with st.expander("📂 Fichier & Table", expanded=True):
            m_source = st.radio(
                "Source", ["Uploader un fichier", "Chemin sur le serveur"],
                horizontal=True, key="m_source",
            )
            m_file, m_path = None, ""
            if m_source == "Uploader un fichier":
                m_file = st.file_uploader("Fichier CSV", type=["csv", "txt"], key="m_upload")
            else:
                m_path = st.text_input("Chemin du fichier", key="m_path_input")

            c1, c2, c3 = st.columns(3)
            with c1:
                m_table = st.text_input("Table cible", placeholder="dbo.ma_table", key="m_table")
            with c2:
                m_enc = st.selectbox("Encodage", ["utf-8-sig", "utf-8", "latin-1", "cp1252"], key="m_enc")
            with c3:
                m_has_header = st.checkbox("1ʳᵉ ligne = en-tête", value=True, key="m_header")

        # Lecture du fichier
        m_raw = None
        if m_file:
            m_raw = m_file.read().decode(m_enc, errors="ignore")
            m_file.seek(0)
        elif m_path and Path(m_path).exists():
            m_raw = Path(m_path).read_text(encoding=m_enc, errors="ignore")

        m_csv_cols: list[str] = []
        m_df: pd.DataFrame | None = None

        if m_raw:
            m_sep = _detect_sep(m_raw)
            try:
                m_df = pd.read_csv(
                    io.StringIO(m_raw),
                    sep=m_sep,
                    header=0 if m_has_header else None,
                    engine="python",
                    dtype=str,
                    on_bad_lines="skip",
                    keep_default_na=False,
                )
                if not m_has_header:
                    m_df.columns = [f"col_{i}" for i in range(len(m_df.columns))]
                else:
                    m_df.columns = [str(c).strip() for c in m_df.columns]
                m_csv_cols = list(m_df.columns)
                st.caption(f"Fichier lu : **{len(m_df)} lignes · {len(m_csv_cols)} colonnes** · séparateur `{repr(m_sep)}`")
            except Exception as e:
                st.error(f"Erreur de lecture : {e}")

        # Colonnes et types SQL
        m_sql_cols: list[str] = []
        m_col_types: dict[str, str] = {}
        if m_table.strip():
            try:
                conn = get_connection()
                cursor = conn.cursor()
                m_schema, m_tbl = _split_table(m_table)
                cursor.execute(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                    m_schema, m_tbl,
                )
                m_sql_cols = [r[0] for r in cursor.fetchall()]
                m_col_types = _get_column_types(cursor, m_schema, m_tbl)
                conn.close()
            except Exception:
                pass

        # Mapping sauvegardé
        m_saved: dict[str, str] = {}
        if m_table.strip():
            try:
                conn = get_connection()
                m_saved = load_mapping(conn, m_table.strip())
                conn.close()
            except Exception:
                pass

        # ── Détection des changements ─────────
        if m_csv_cols and m_saved:
            with st.expander("🔍 Détection des changements", expanded=True):
                st.caption("Comparaison du fichier actuel avec le mapping sauvegardé.")
                changes = detect_changes(m_csv_cols, m_saved)
                c1, c2, c3 = st.columns(3)
                with c1:
                    if changes["missing"]:
                        st.warning(f"**{len(changes['missing'])} manquante(s)**")
                        for col in changes["missing"]:
                            st.markdown(f"- `{col}` *(était → `{m_saved[col]}`)*")
                    else:
                        st.success("Aucune colonne manquante")
                with c2:
                    if changes["new"]:
                        st.info(f"**{len(changes['new'])} nouvelle(s)**")
                        for col in changes["new"]:
                            st.markdown(f"- `{col}`")
                    else:
                        st.success("Aucune nouvelle colonne")
                with c3:
                    st.success(f"**{len(changes['unchanged'])} inchangée(s)**")
                    if changes["unchanged"]:
                        st.caption(", ".join(f"`{c}`" for c in changes["unchanged"]))
        elif m_csv_cols and not m_saved and m_table.strip():
            st.info("ℹ️ Aucun mapping sauvegardé pour cette table — créez-en un ci-dessous.")

        # ── Édition du mapping ────────────────
        m_edit_mapping: dict[str, str] = {}
        m_sql_options = ["— ignorer —"] + m_sql_cols

        if m_csv_cols and (m_sql_cols or m_saved):
            with st.expander("✏️ Édition du mapping", expanded=True):
                if m_saved:
                    st.caption("Mapping chargé depuis la base. `💡` = suggestion automatique, `✅` = exact.")
                else:
                    st.caption("Suggestions automatiques basées sur les noms de colonnes. `💡` = suggestion fuzzy, `✅` = exact.")

                suggestions = suggest_mapping(m_csv_cols, m_sql_cols) if m_sql_cols else {}

                def _edit_default(csv_col: str) -> str:
                    if m_saved.get(csv_col) in m_sql_options:
                        return m_saved[csv_col]
                    if suggestions.get(csv_col) in m_sql_options:
                        return suggestions[csv_col]
                    return "— ignorer —"

                cols_per_row = 3
                for i in range(0, len(m_csv_cols), cols_per_row):
                    row_c = st.columns(cols_per_row)
                    for j, csv_col in enumerate(m_csv_cols[i: i + cols_per_row]):
                        with row_c[j]:
                            default = _edit_default(csv_col)
                            default_idx = m_sql_options.index(default) if default in m_sql_options else 0
                            # Badge indicateur
                            if csv_col in m_saved:
                                label = f"`{csv_col}` ✅"
                            elif csv_col in suggestions:
                                label = f"`{csv_col}` 💡"
                            else:
                                label = f"`{csv_col}`"
                            chosen = st.selectbox(
                                label, options=m_sql_options,
                                index=default_idx, key=f"m_edit_{csv_col}",
                            )
                            if chosen != "— ignorer —":
                                m_edit_mapping[csv_col] = chosen

                if m_edit_mapping:
                    st.caption(
                        f"**{len(m_edit_mapping)} colonne(s) mappée(s) :** "
                        + ", ".join(f"`{k}` → `{v}`" for k, v in m_edit_mapping.items())
                    )
                else:
                    st.warning("⚠️ Aucune colonne mappée.")

        # ── Test du mapping ───────────────────
        if m_edit_mapping and m_df is not None:
            with st.expander("🧪 Test du mapping", expanded=False):
                if st.button("▶️ Tester sur l'échantillon", key="btn_test_map", type="primary"):
                    result = test_mapping(m_df, m_edit_mapping, m_col_types)
                    st.session_state["m_test_result"] = result

                cached = st.session_state.get("m_test_result")
                if cached:
                    if cached["errors"]:
                        st.error(f"**{len(cached['errors'])} erreur(s) de conversion :**")
                        for err in cached["errors"]:
                            st.markdown(f"- {err}")
                    else:
                        st.success("✅ Aucune erreur de conversion")

                    if cached["unmapped"]:
                        st.warning(
                            f"**{len(cached['unmapped'])} colonne(s) non mappée(s) :** "
                            + ", ".join(f"`{c}`" for c in cached["unmapped"])
                        )

                    if not cached["preview_df"].empty:
                        st.markdown("**Aperçu des données après mapping :**")
                        preview = cached["preview_df"].copy()
                        preview.index = range(1, len(preview) + 1)
                        st.dataframe(preview, use_container_width=True)

        # ── Sauvegarde ────────────────────────
        if m_edit_mapping and m_table.strip():
            with st.expander("💾 Sauvegarde", expanded=False):
                st.caption(f"Enregistre le mapping pour **{m_table}** dans `dbo.import_mapping` (versionné).")

                if st.button("💾 Sauvegarder ce mapping", key="btn_save_map", type="primary"):
                    try:
                        conn = get_connection()
                        version = save_mapping(conn, m_table.strip(), m_edit_mapping)
                        conn.close()
                        st.success(f"✅ Mapping sauvegardé — version **{version}**")
                        # Efface le cache pour forcer rechargement
                        for k in ("m_test_result", "m_history_df"):
                            st.session_state.pop(k, None)
                    except Exception as e:
                        st.error(f"❌ Erreur lors de la sauvegarde : {e}")

                st.markdown("---")
                st.markdown("**Historique des versions**")
                if st.button("🔄 Charger l'historique", key="btn_map_hist"):
                    try:
                        conn = get_connection()
                        st.session_state["m_history_df"] = load_history(conn, m_table.strip())
                        st.session_state["m_history_tbl"] = m_table.strip()
                        conn.close()
                    except Exception as e:
                        st.error(f"❌ {e}")

                if (
                    st.session_state.get("m_history_tbl") == m_table.strip()
                    and "m_history_df" in st.session_state
                ):
                    hist = st.session_state["m_history_df"]
                    if hist.empty:
                        st.info("Aucun mapping sauvegardé pour cette table.")
                    else:
                        st.dataframe(hist, use_container_width=True)
                        versions = hist["Version"].unique().tolist()
                        st.caption(f"{len(versions)} version(s) · {len(hist)} lignes totales")

    # ── TAB 3 : Explorer les Tables ──────────
    with tab_tables:
        st.markdown("### Tables disponibles dans la base de données")
        st.markdown("")

        if st.button("🔄 Rafraîchir la liste des tables", use_container_width=False):
            st.session_state["tables_loaded"] = True

        if st.session_state.get("tables_loaded", False):
            try:
                with st.spinner("Chargement des tables…"):
                    conn = get_connection()
                    tables = list_user_tables(conn)
                    conn.close()

                if not tables:
                    st.info("Aucune table trouvée dans la base de données.")
                else:
                    tables_df = pd.DataFrame([tuple(r) for r in tables], columns=["Schéma", "Table"])
                    st.dataframe(tables_df, use_container_width=True, height=400)
                    st.caption(f"{len(tables)} tables trouvées.")

                # Opérations sur une table
                st.markdown("---")
                st.markdown("### Opérations sur une table")

                table_choices = [f"{s}.{t}" for s, t in tables]
                selected_table = st.selectbox("Sélectionner une table", ["—"] + table_choices)

                if selected_table and selected_table != "—":
                    c1, c2, c3 = st.columns(3)

                    with c1:
                        if st.button("👁️ Aperçu (50 lignes)", use_container_width=True):
                            try:
                                conn = get_connection()
                                df_preview = pd.read_sql(
                                    f"SELECT TOP 50 * FROM {selected_table}", conn
                                )
                                conn.close()
                                st.session_state["preview_df"] = df_preview
                                st.session_state["preview_table"] = selected_table
                            except Exception as e:
                                st.error(str(e))

                    with c2:
                        if st.button("📊 Nombre de lignes", use_container_width=True):
                            try:
                                conn = get_connection()
                                cursor = conn.cursor()
                                cursor.execute(f"SELECT COUNT(*) FROM {selected_table}")
                                count = cursor.fetchone()[0]
                                conn.close()
                                st.session_state["row_count"] = count
                                st.session_state["row_count_table"] = selected_table
                            except Exception as e:
                                st.error(str(e))

                    with c3:
                        # Prépare le CSV en mémoire puis propose le téléchargement direct
                        try:
                            conn = get_connection()
                            df_export = pd.read_sql(f"SELECT * FROM {selected_table}", conn)
                            conn.close()
                            csv_bytes = df_export.to_csv(sep=";", index=False).encode("utf-8")
                            st.download_button(
                                "💾 Exporter en CSV",
                                data=csv_bytes,
                                file_name=f"{selected_table.replace('.', '_')}.csv",
                                mime="text/csv",
                                use_container_width=True,
                            )
                        except Exception as e:
                            st.error(str(e))

                    # Résultats persistants via session_state
                    if st.session_state.get("preview_table") == selected_table and "preview_df" in st.session_state:
                        st.dataframe(st.session_state["preview_df"], use_container_width=True)

                    if st.session_state.get("row_count_table") == selected_table and "row_count" in st.session_state:
                        st.metric("Nombre de lignes", f"{st.session_state['row_count']:,}")

            except Exception as e:
                st.error(f"Impossible de se connecter : {e}")
        else:
            st.info("Cliquez sur **Rafraîchir** pour charger la liste des tables.")


# ═════════════════════════════════════════════
# PAGE 3 — SCRIPTS PYTHON
# ═════════════════════════════════════════════

elif page == "🧑‍💻 Scripts Python":
    st.title("🧑‍💻 Scripts Python")
    st.caption("Créez et gérez vos scripts Python réutilisables.")
    st.markdown("---")

    existing_files = sorted([p.name for p in SCRIPTS_DIR.glob("*.py")])

    c1, c2 = st.columns([3, 2])
    with c1:
        new_filename = st.text_input("Nom du script", "script_custom.py")
        if not new_filename.endswith(".py"):
            new_filename += ".py"
    with c2:
        picked = st.selectbox("Ouvrir un script existant", ["(nouveau)"] + existing_files)

    target_file = SCRIPTS_DIR / (picked if picked != "(nouveau)" else new_filename)
    st.caption(f"Emplacement : `{target_file}`")

    st.markdown("")

    default_template = '# Script Python\n\nif __name__ == "__main__":\n    print("Hello depuis user_scripts !")\n'

    if target_file.exists():
        try:
            initial_code = target_file.read_text(encoding="utf-8")
        except Exception:
            initial_code = target_file.read_text(errors="ignore")
    else:
        initial_code = default_template

    code_text = st.text_area("Code", value=initial_code, height=420, label_visibility="collapsed")

    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("💾 Enregistrer", type="primary", use_container_width=True):
            target_file.write_text(code_text, encoding="utf-8")
            st.success(f"✅ Enregistré : `{target_file.name}`")

    st.markdown("---")

    if existing_files:
        st.markdown(f"**{len(existing_files)} script(s) dans `user_scripts/`**")
        for f in existing_files:
            st.markdown(f"- `{f}`")
    else:
        st.info("Aucun script enregistré pour l'instant.")


# ═════════════════════════════════════════════
# PAGE 4 — AUTOMATISATION n8n
# ═════════════════════════════════════════════

elif page == "🤖 Automatisation n8n":
    API_URL      = "http://localhost:8000"
    N8N_URL      = "http://localhost:5678"
    N8N_WEBHOOK  = "http://localhost:5678/webhook/data-pipeline"
    INBOX_DIR    = Path(__file__).resolve().parent / "inbox"
    PROCESSED    = Path(__file__).resolve().parent / "processed"

    st.title("🤖 Automatisation n8n")
    st.caption("Import automatique (Option A) · Déclenchement webhook via n8n (Option C)")
    st.markdown("---")

    # ── Statut des services ───────────────────────────────────────────────
    c1, c2 = st.columns(2)
    with c1:
        try:
            _requests.get(f"{API_URL}/health", timeout=2)
            st.success("✅ API active (port 8000)")
        except Exception:
            st.error("❌ API hors ligne — lance `uvicorn api:app --port 8000`")
    with c2:
        try:
            _requests.get(f"{N8N_URL}/healthz", timeout=2)
            st.success("✅ n8n actif (port 5678)")
        except Exception:
            st.error("❌ n8n hors ligne")

    st.markdown("---")

    # ── OPTION A — Inbox ─────────────────────────────────────────────────
    st.subheader("📂 Option A — Dépôt automatique (inbox)")
    st.caption("Dépose un CSV → n8n le détecte toutes les 2 min et l'importe automatiquement.")

    uploaded = st.file_uploader("Déposer un CSV dans inbox/", type=["csv"], key="n8n_upload")
    if uploaded:
        dest = INBOX_DIR / uploaded.name
        dest.write_bytes(uploaded.read())
        st.success(f"✅ `{uploaded.name}` déposé dans inbox/ — n8n va le traiter automatiquement.")

    inbox_files = sorted(INBOX_DIR.glob("*.csv"))
    if inbox_files:
        st.markdown(f"**{len(inbox_files)} fichier(s) en attente :**")
        for f in inbox_files:
            st.markdown(f"- `{f.name}` — {round(f.stat().st_size/1024, 1)} KB")
    else:
        st.info("Inbox vide.")

    st.markdown("---")

    # ── OPTION C — Webhook n8n ────────────────────────────────────────────
    st.subheader("⚡ Option C — Déclenchement via webhook n8n")
    st.caption(f"Streamlit → webhook n8n (`data-pipeline`) → Switch action → API → SQL")

    inbox_names = [f.name for f in inbox_files]

    tab_import, tab_list, tab_delete = st.tabs(["🚀 Importer", "📋 Voir les records", "🗑️ Supprimer"])

    with tab_import:
        if inbox_names:
            col1, col2 = st.columns(2)
            with col1:
                chosen_file = st.selectbox("Fichier à importer", inbox_names, key="wh_file")
            with col2:
                try:
                    r = _requests.get(f"{API_URL}/tables", timeout=3)
                    tables = r.json().get("tables", [])
                except Exception:
                    tables = []
                chosen_table = st.selectbox("Table cible", tables or ["(API hors ligne)"], key="wh_table")

            truncate = st.checkbox("TRUNCATE avant import", value=False, key="wh_trunc")

            if st.button("🚀 Déclencher via n8n", type="primary", use_container_width=True):
                try:
                    # 1. Envoie le signal à n8n (déclenchement du workflow)
                    _requests.post(
                        N8N_WEBHOOK,
                        json={"action": "create", "filename": chosen_file, "table": chosen_table, "truncate": truncate},
                        timeout=10,
                    )
                except Exception:
                    pass  # n8n répond vide — on continue quand même

                try:
                    # 2. L'API exécute réellement l'import
                    r = _requests.post(
                        f"{API_URL}/records",
                        json={"filename": chosen_file, "table": chosen_table, "truncate": truncate},
                        timeout=30,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        st.success(f"✅ Import terminé — **{data['rows_inserted']} lignes** insérées dans `{chosen_table}`")
                        st.caption(f"Archivé sous : `{data['archived_as']}`")
                    else:
                        st.error(f"Erreur API ({r.status_code}) : {r.text[:300]}")
                except Exception as e:
                    st.error(f"Erreur API : {e}")
        else:
            st.info("Aucun fichier dans inbox/ — dépose d'abord un CSV ci-dessus.")

    with tab_list:
        if st.button("🔄 Rafraîchir", key="refresh_records"):
            try:
                r = _requests.post(
                    N8N_WEBHOOK,
                    json={"action": "get_all"},
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    body = data.get("body", data)
                    st.markdown(f"**En attente :** {body.get('total_pending', 0)} · **Traités :** {body.get('total_processed', 0)}")
                    if body.get("inbox"):
                        st.dataframe(body["inbox"], use_container_width=True)
                else:
                    st.error(r.text[:200])
            except Exception as e:
                st.error(str(e))

    with tab_delete:
        all_inbox = [f.name for f in inbox_files]
        if all_inbox:
            to_delete = st.selectbox("Fichier à supprimer de inbox/", all_inbox, key="del_file")
            if st.button("🗑️ Supprimer", type="secondary"):
                try:
                    r = _requests.post(
                        N8N_WEBHOOK,
                        json={"action": "delete", "filename": to_delete},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        st.success(f"✅ `{to_delete}` supprimé.")
                    else:
                        st.error(r.text[:200])
                except Exception as e:
                    st.error(str(e))
        else:
            st.info("Inbox vide.")

    st.markdown("---")

    # ── Historique processed/ ─────────────────────────────────────────────
    st.subheader("📋 Historique des imports")
    processed_files = sorted(PROCESSED.glob("*.csv"), reverse=True)
    if processed_files:
        rows = []
        for f in processed_files[:20]:
            ts = f.stat().st_mtime
            rows.append({
                "Fichier": f.name,
                "Taille (KB)": round(f.stat().st_size / 1024, 1),
                "Traité le": datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M"),
            })
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("Aucun import effectué pour l'instant.")


# ═════════════════════════════════════════════
# PAGE 5 — TABLEAU DE BORD (style Power BI)
# ═════════════════════════════════════════════

elif page == "📊 Tableau de bord":
    import plotly.express as px
    import plotly.graph_objects as go
    import numpy as np
    import subprocess, sys
    from pathlib import Path
    from datetime import date

    CONN_PF = _build_conn_str("STAGEPORTFOLIO")

    @st.cache_data(ttl=60)
    def load_data():
        conn = pyodbc.connect(CONN_PF, timeout=10, autocommit=True)
        nav = pd.read_sql("""
            SELECT n.nav_date, n.nav_value, n.aum,
                   p.portfolio_name, p.base_currency, p.inception_date
            FROM pf.nav n
            JOIN pf.portfolio p ON n.portfolio_id = p.portfolio_id
        """, conn)
        positions = pd.read_sql("""
            SELECT pos.position_date, pos.quantity, pos.market_value,
                   p.portfolio_name, i.instrument_name, i.asset_class
            FROM pf.position pos
            JOIN pf.portfolio p ON pos.portfolio_id = p.portfolio_id
            JOIN pf.instrument i ON pos.instrument_id = i.instrument_id
        """, conn)
        transactions = pd.read_sql("""
            SELECT t.trade_date, t.side, t.quantity, t.price, t.fees, t.currency,
                   p.portfolio_name, i.instrument_name
            FROM pf.[transaction] t
            JOIN pf.portfolio p ON t.portfolio_id = p.portfolio_id
            JOIN pf.instrument i ON t.instrument_id = i.instrument_id
        """, conn)
        index_levels = pd.read_sql("""
            SELECT il.level_date, il.close_level, r.index_name, r.currency
            FROM pf.index_level il
            JOIN pf.index_ref r ON il.index_id = r.index_id
        """, conn)
        bench_comp = pd.read_sql("""
            SELECT b.benchmark_name, r.index_name, bc.weight
            FROM pf.benchmark_component bc
            JOIN pf.benchmark b ON bc.benchmark_id = b.benchmark_id
            JOIN pf.index_ref r ON bc.index_id = r.index_id
        """, conn)
        pf_bench = pd.read_sql("""
            SELECT p.portfolio_name, b.benchmark_name
            FROM pf.portfolio_benchmark pb
            JOIN pf.portfolio p ON pb.portfolio_id = p.portfolio_id
            JOIN pf.benchmark b ON pb.benchmark_id = b.benchmark_id
        """, conn)
        import_log = pd.read_sql("""
            SELECT target_table, csv_filename, rows_inserted, rows_skipped, started_at, status
            FROM pf.import_log ORDER BY started_at DESC
        """, conn)
        conn.close()
        nav["nav_date"]          = pd.to_datetime(nav["nav_date"])
        nav["inception_date"]    = pd.to_datetime(nav["inception_date"])
        positions["position_date"] = pd.to_datetime(positions["position_date"])
        transactions["trade_date"] = pd.to_datetime(transactions["trade_date"])
        index_levels["level_date"] = pd.to_datetime(index_levels["level_date"])
        return nav, positions, transactions, index_levels, bench_comp, pf_bench, import_log

    # ── Style ─────────────────────────────────────────────────────────────────
    st.markdown("""
    <style>
    .pbi-card { background:#1E1E2E; border-radius:10px; padding:16px 20px; margin-bottom:10px; }
    .pbi-metric-val { font-size:2rem; font-weight:700; color:#F2C94C; }
    .pbi-metric-lbl { font-size:0.85rem; color:#A0AEC0; margin-top:4px; }
    .pbi-title { font-size:1.5rem; font-weight:700; color:#F7FAFC; margin-bottom:4px; }
    .pbi-sub   { font-size:0.9rem; color:#A0AEC0; margin-bottom:16px; }
    .perf-pos  { color:#6FCF97; font-weight:700; }
    .perf-neg  { color:#EB5757; font-weight:700; }
    </style>
    """, unsafe_allow_html=True)

    PBI_COLORS = ["#F2C94C","#56CCF2","#6FCF97","#EB5757","#BB6BD9","#F2994A","#2D9CDB"]
    CHART_BG   = "#1E1E2E"
    PAPER_BG   = "#13131F"
    FONT_COLOR = "#F7FAFC"
    GRID_COLOR = "#2D2D44"

    def pbi_layout(fig, title="", height=380):
        fig.update_layout(
            title=dict(text=title, font=dict(size=15, color=FONT_COLOR), x=0.01),
            plot_bgcolor=CHART_BG, paper_bgcolor=PAPER_BG,
            font=dict(color=FONT_COLOR, family="Segoe UI, Arial"),
            height=height,
            margin=dict(l=12, r=12, t=40, b=12),
            legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
            xaxis=dict(gridcolor=GRID_COLOR, linecolor=GRID_COLOR, zeroline=False),
            yaxis=dict(gridcolor=GRID_COLOR, linecolor=GRID_COLOR, zeroline=False),
        )
        return fig

    def fmt_ret(val):
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return "—"
        color = "6FCF97" if val >= 0 else "EB5757"
        sign  = "+" if val >= 0 else ""
        return f'<span style="color:#{color};font-weight:700">{sign}{val:.2f}%</span>'

    def compute_returns_dash(nav_series, inception_date):
        """Retourne dict {1M, 3M, YTD, 1Y} en % ou None."""
        if nav_series.empty:
            return {k: None for k in ["1M","3M","YTD","1Y"]}
        today    = nav_series.index.max()
        nav_now  = nav_series.iloc[-1]
        def ret(ref):
            past = nav_series[nav_series.index <= ref]
            if past.empty: return None
            return (nav_now / past.iloc[-1] - 1) * 100
        ytd_start = pd.Timestamp(today.year, 1, 1)
        return {
            "1M":  ret(today - pd.DateOffset(months=1)),
            "3M":  ret(today - pd.DateOffset(months=3)),
            "YTD": ret(ytd_start),
            "1Y":  ret(today - pd.DateOffset(years=1)),
        }

    def compute_heatmap(nav_df):
        """Rendements mensuels : pivot année x mois."""
        df = nav_df.sort_values("nav_date").copy()
        df["year"]  = df["nav_date"].dt.year
        df["month"] = df["nav_date"].dt.month
        monthly = (
            df.groupby(["year","month"])["nav_value"]
            .agg(["first","last"])
            .reset_index()
        )
        monthly["ret"] = (monthly["last"] / monthly["first"] - 1) * 100
        pivot = monthly.pivot(index="year", columns="month", values="ret")
        pivot.columns = ["Jan","Fév","Mar","Avr","Mai","Jun",
                         "Jul","Aoû","Sep","Oct","Nov","Déc"][:len(pivot.columns)]
        return pivot

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown('<div class="pbi-title">📊 Tableau de bord Portfolio</div>', unsafe_allow_html=True)
    st.markdown('<div class="pbi-sub">STAGEPORTFOLIO · Données en temps réel</div>', unsafe_allow_html=True)

    try:
        nav, positions, transactions, index_levels, bench_comp, pf_bench, import_log = load_data()
    except Exception as e:
        st.error(f"Erreur de connexion : {e}")
        st.stop()

    pf_names = sorted(nav["portfolio_name"].unique().tolist())

    # ── KPIs globaux ──────────────────────────────────────────────────────────
    k1, k2, k3, k4 = st.columns(4)
    for col, val, lbl in [
        (k1, f"{nav['aum'].sum()/1_000_000:.1f}M",        "AUM Total"),
        (k2, f"{len(pf_names)}",                           "Portefeuilles"),
        (k3, f"{len(transactions)}",                       "Transactions"),
        (k4, f"{positions['market_value'].sum()/1_000:.0f}K", "Valeur Positions"),
    ]:
        col.markdown(f"""<div class="pbi-card">
            <div class="pbi-metric-val">{val}</div>
            <div class="pbi-metric-lbl">{lbl}</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # ── Onglets : Vue globale + un onglet par portefeuille ────────────────────
    tab_labels = ["🌐 Vue globale"] + [f"📁 {pf}" for pf in pf_names]
    tabs = st.tabs(tab_labels)

    # ════════════════════════════════════════════
    # ONGLET 0 — VUE GLOBALE
    # ════════════════════════════════════════════
    with tabs[0]:
        c1, c2 = st.columns(2)
        with c1:
            fig = px.line(nav, x="nav_date", y="nav_value", color="portfolio_name",
                          color_discrete_sequence=PBI_COLORS, markers=True)
            st.plotly_chart(pbi_layout(fig, "NAV — tous les portefeuilles"),
                            use_container_width=True)
        with c2:
            fig = px.bar(nav, x="nav_date", y="aum", color="portfolio_name",
                         color_discrete_sequence=PBI_COLORS, barmode="group")
            st.plotly_chart(pbi_layout(fig, "AUM par portefeuille"),
                            use_container_width=True)

        c3, c4 = st.columns(2)
        with c3:
            fig = px.line(index_levels, x="level_date", y="close_level",
                          color="index_name", color_discrete_sequence=PBI_COLORS)
            st.plotly_chart(pbi_layout(fig, "Performance des indices"),
                            use_container_width=True)
        with c4:
            if not bench_comp.empty:
                sel_bench = st.selectbox("Benchmark", bench_comp["benchmark_name"].unique(), key="gb_bench")
                bc_f = bench_comp[bench_comp["benchmark_name"] == sel_bench]
                fig = go.Figure(go.Pie(
                    labels=bc_f["index_name"], values=bc_f["weight"],
                    marker_colors=PBI_COLORS, hole=0.45,
                    textinfo="label+percent",
                    textfont=dict(size=13, color=FONT_COLOR),
                ))
                st.plotly_chart(pbi_layout(fig, f"Composition — {sel_bench}", height=360),
                                use_container_width=True)

        st.markdown("#### 📋 Historique des imports")
        if not import_log.empty:
            sc = {"SUCCESS": "🟢", "DUPLICATE": "🟡", "ERROR": "🔴"}
            il = import_log.copy()
            il["Statut"] = il["status"].map(lambda s: f"{sc.get(s,'⚪')} {s}")
            il["Date"]   = pd.to_datetime(il["started_at"]).dt.strftime("%d/%m %H:%M")
            st.dataframe(
                il[["Date","target_table","csv_filename","rows_inserted","rows_skipped","Statut"]].rename(columns={
                    "target_table":"Table","csv_filename":"Fichier",
                    "rows_inserted":"Insérées","rows_skipped":"Ignorées"
                }),
                use_container_width=True, hide_index=True
            )

    # ════════════════════════════════════════════
    # ONGLETS PAR PORTEFEUILLE
    # ════════════════════════════════════════════
    for tab_idx, pf_name in enumerate(pf_names, start=1):
        with tabs[tab_idx]:
            nav_pf = nav[nav["portfolio_name"] == pf_name].sort_values("nav_date")
            pos_pf = positions[positions["portfolio_name"] == pf_name]
            tx_pf  = transactions[transactions["portfolio_name"] == pf_name]

            # ── KPIs du portefeuille ──────────────────────────────────────
            pk1, pk2, pk3, pk4 = st.columns(4)
            aum_pf    = nav_pf["aum"].iloc[-1] if not nav_pf.empty else 0
            nav_cur   = nav_pf["nav_value"].iloc[-1] if not nav_pf.empty else 0
            pos_val_pf = pos_pf["market_value"].sum()
            tx_cnt_pf  = len(tx_pf)
            for col, val, lbl in [
                (pk1, f"{aum_pf/1_000_000:.2f}M",  "AUM"),
                (pk2, f"{nav_cur:.4f}",              "Dernière NAV"),
                (pk3, f"{pos_val_pf/1_000:.0f}K",   "Valeur Positions"),
                (pk4, f"{tx_cnt_pf}",                "Transactions"),
            ]:
                col.markdown(f"""<div class="pbi-card">
                    <div class="pbi-metric-val">{val}</div>
                    <div class="pbi-metric-lbl">{lbl}</div>
                </div>""", unsafe_allow_html=True)

            st.markdown("")

            # ── NAV vs Benchmark ─────────────────────────────────────────
            c1, c2 = st.columns([3, 2])
            with c1:
                # Trouver le benchmark lié à ce portefeuille
                bench_linked = pf_bench[pf_bench["portfolio_name"] == pf_name]["benchmark_name"].tolist()
                idx_names = sorted(index_levels["index_name"].unique().tolist())
                sel_idx = st.selectbox("Comparer avec l'indice", ["(aucun)"] + idx_names,
                                       key=f"idx_{pf_name}")

                fig = go.Figure()
                if not nav_pf.empty:
                    # NAV normalisée à 100
                    nav_norm = nav_pf["nav_value"] / nav_pf["nav_value"].iloc[0] * 100
                    fig.add_trace(go.Scatter(
                        x=nav_pf["nav_date"], y=nav_norm,
                        name=pf_name, line=dict(color="#F2C94C", width=2.5)
                    ))
                if sel_idx != "(aucun)":
                    idx_df = index_levels[index_levels["index_name"] == sel_idx].sort_values("level_date")
                    if not idx_df.empty:
                        idx_norm = idx_df["close_level"] / idx_df["close_level"].iloc[0] * 100
                        fig.add_trace(go.Scatter(
                            x=idx_df["level_date"], y=idx_norm,
                            name=sel_idx, line=dict(color="#56CCF2", width=2, dash="dash")
                        ))
                fig.add_hline(y=100, line_dash="dot", line_color="#555", opacity=0.5)
                st.plotly_chart(pbi_layout(fig, "NAV vs Indice (base 100)"),
                                use_container_width=True)

            # ── Tableau de performance ────────────────────────────────────
            with c2:
                st.markdown("#### 📈 Performance")
                nav_series = nav_pf.set_index("nav_date")["nav_value"]
                inception  = nav_pf["inception_date"].iloc[0] if not nav_pf.empty else None
                rets       = compute_returns_dash(nav_series, inception)

                perf_rows = []
                for period, val in rets.items():
                    perf_rows.append({"Période": period, "Rendement": fmt_ret(val)})

                if perf_rows:
                    perf_html = """<table style="width:100%;border-collapse:collapse;font-size:14px">
                    <thead><tr style="background:#1A237E;color:white">
                    <th style="padding:8px;text-align:left">Période</th>
                    <th style="padding:8px;text-align:right">Rendement</th>
                    </tr></thead><tbody>"""
                    for i, row in enumerate(perf_rows):
                        bg = "#1E1E2E" if i % 2 == 0 else "#252535"
                        perf_html += f'<tr style="background:{bg}"><td style="padding:8px;color:#F7FAFC">{row["Période"]}</td><td style="padding:8px;text-align:right">{row["Rendement"]}</td></tr>'
                    perf_html += "</tbody></table>"
                    st.markdown(perf_html, unsafe_allow_html=True)

                st.markdown("")
                # Écart-type annualisé
                if len(nav_series) > 2:
                    daily_ret = nav_series.pct_change().dropna()
                    std_ann   = daily_ret.std() * np.sqrt(252) * 100
                    sharpe    = (daily_ret.mean() * 252) / (daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0
                    r1, r2 = st.columns(2)
                    r1.metric("Écart-type (ann.)", f"{std_ann:.2f}%")
                    r2.metric("Sharpe (rf=0)",     f"{sharpe:.2f}")

            st.markdown("")

            # ── Heatmap rendements mensuels ───────────────────────────────
            c3, c4 = st.columns([3, 2])
            with c3:
                heatmap_data = compute_heatmap(nav_pf)
                if not heatmap_data.empty:
                    fig_hm = go.Figure(go.Heatmap(
                        z=heatmap_data.values.tolist(),
                        x=heatmap_data.columns.tolist(),
                        y=[str(y) for y in heatmap_data.index.tolist()],
                        colorscale=[
                            [0.0,  "#C62828"],
                            [0.5,  "#1E1E2E"],
                            [1.0,  "#2E7D32"],
                        ],
                        zmid=0,
                        text=[[f"{v:.1f}%" if not np.isnan(v) else "" for v in row]
                              for row in heatmap_data.values.tolist()],
                        texttemplate="%{text}",
                        showscale=True,
                        colorbar=dict(tickfont=dict(color=FONT_COLOR)),
                    ))
                    st.plotly_chart(
                        pbi_layout(fig_hm, "Rendements mensuels (%)", height=300),
                        use_container_width=True
                    )
                else:
                    st.info("Pas assez de données pour la heatmap.")

            # ── Top 5 positions ───────────────────────────────────────────
            with c4:
                st.markdown("#### 🏆 Top 5 positions")
                if not pos_pf.empty:
                    top5 = (pos_pf.groupby("instrument_name")["market_value"]
                                  .sum()
                                  .sort_values(ascending=False)
                                  .head(5)
                                  .reset_index())
                    fig_t5 = px.bar(top5, x="market_value", y="instrument_name",
                                    orientation="h",
                                    color_discrete_sequence=["#F2C94C"])
                    fig_t5.update_layout(yaxis=dict(autorange="reversed"))
                    st.plotly_chart(
                        pbi_layout(fig_t5, "", height=300),
                        use_container_width=True
                    )
                else:
                    st.info("Aucune position.")

            st.markdown("")

            # ── BUY/SELL + dernières transactions ─────────────────────────
            c5, c6 = st.columns([1, 2])
            with c5:
                buy  = tx_pf[tx_pf["side"] == "BUY"].shape[0]
                sell = tx_pf[tx_pf["side"] == "SELL"].shape[0]
                fig = go.Figure(go.Pie(
                    labels=["BUY","SELL"], values=[buy, sell],
                    marker_colors=["#6FCF97","#EB5757"],
                    hole=0.55, textinfo="label+percent",
                    textfont=dict(size=13, color=FONT_COLOR),
                ))
                st.plotly_chart(pbi_layout(fig, "BUY / SELL", height=300),
                                use_container_width=True)
            with c6:
                st.markdown("#### 🔁 Dernières transactions")
                if not tx_pf.empty:
                    tx_show = tx_pf.sort_values("trade_date", ascending=False).head(8).copy()
                    tx_show["trade_date"] = tx_show["trade_date"].dt.strftime("%d/%m/%Y")
                    st.dataframe(
                        tx_show[["trade_date","instrument_name","side","quantity","price"]].rename(columns={
                            "trade_date":"Date","instrument_name":"Instrument",
                            "side":"Sens","quantity":"Qté","price":"Prix"
                        }),
                        use_container_width=True, hide_index=True
                    )

    # ════════════════════════════════════════════
    # ACTIONS — Refresh + PDF avec période
    # ════════════════════════════════════════════
    st.markdown("---")
    col_refresh, col_pdf = st.columns([1, 3])

    with col_refresh:
        if st.button("🔄 Rafraîchir", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    with col_pdf:
        with st.expander("📄 Générer le rapport Factsheet PDF", expanded=False):
            pd1, pd2 = st.columns(2)
            min_date = nav["nav_date"].min().date() if not nav.empty else date(2020, 1, 1)
            max_date = nav["nav_date"].max().date() if not nav.empty else date.today()
            pdf_start = pd1.date_input("Date de début", value=min_date, min_value=min_date, max_value=max_date, key="pdf_start")
            pdf_end   = pd2.date_input("Date de fin",   value=max_date, min_value=min_date, max_value=max_date, key="pdf_end")

            all_pf_opts = ["Tous"] + pf_names
            pdf_pf = st.multiselect("Portefeuilles à inclure", pf_names, default=pf_names, key="pdf_pf")

            if st.button("🚀 Lancer la génération", type="primary", use_container_width=True):
                report_script = Path(__file__).resolve().parent / "generate_report.py"
                env_vars = {
                    **dict(__import__("os").environ),
                    "REPORT_START": str(pdf_start),
                    "REPORT_END":   str(pdf_end),
                    "REPORT_PF":    ",".join(pdf_pf) if pdf_pf else "Tous",
                }
                with st.spinner("Génération du rapport en cours…"):
                    result = subprocess.run(
                        [sys.executable, str(report_script)],
                        capture_output=True, text=True, timeout=120,
                        env=env_vars
                    )
                if result.returncode != 0:
                    st.error(f"Erreur :\n{result.stderr[-1000:]}")
                else:
                    pdf_files = sorted(
                        Path.home().glob("Desktop/rapport_portfolio_*.pdf"),
                        key=lambda p: p.stat().st_mtime, reverse=True
                    )
                    if pdf_files:
                        latest_pdf = pdf_files[0]
                        st.success(f"✅ {latest_pdf.name}")
                        st.download_button(
                            label="⬇️ Télécharger le PDF",
                            data=latest_pdf.read_bytes(),
                            file_name=latest_pdf.name,
                            mime="application/pdf",
                            use_container_width=True,
                        )
                    else:
                        st.warning("Rapport généré mais introuvable sur le bureau.")

# ═════════════════════════════════════════════
# PAGE 6 — ANALYSE SQL → GRAPHIQUE (multi-blocs)
# ═════════════════════════════════════════════

elif page == "🔬 Analyse SQL → Graphique":
    import plotly.express as px
    import plotly.graph_objects as go
    import io as _io

    CONN_PF = _build_conn_str("STAGEPORTFOLIO")

    CHART_BG   = "#1E1E2E"
    PAPER_BG   = "#13131F"
    FONT_COLOR = "#F7FAFC"
    GRID_COLOR = "#2D2D44"

    EXAMPLES = {
        "(choisir un exemple)": "",

        # ── NAV & AUM ────────────────────────────────────────────────────────
        "📈 NAV par portefeuille dans le temps":
            "SELECT p.portfolio_name, n.nav_date, n.nav_value\n"
            "FROM pf.nav n\n"
            "JOIN pf.portfolio p ON n.portfolio_id = p.portfolio_id\n"
            "ORDER BY n.nav_date",

        "📈 NAV — croissance base 100 (normalisée)":
            "SELECT p.portfolio_name, n.nav_date,\n"
            "       n.nav_value * 100.0 / FIRST_VALUE(n.nav_value)\n"
            "           OVER (PARTITION BY p.portfolio_name ORDER BY n.nav_date) AS base_100\n"
            "FROM pf.nav n\n"
            "JOIN pf.portfolio p ON n.portfolio_id = p.portfolio_id\n"
            "ORDER BY n.nav_date",

        "📊 AUM total par portefeuille (barres)":
            "SELECT p.portfolio_name, n.nav_date, SUM(n.aum) AS total_aum\n"
            "FROM pf.nav n\n"
            "JOIN pf.portfolio p ON n.portfolio_id = p.portfolio_id\n"
            "GROUP BY p.portfolio_name, n.nav_date\n"
            "ORDER BY n.nav_date, total_aum DESC",

        "📊 AUM évolution mensuelle (barres empilées)":
            "SELECT FORMAT(n.nav_date, 'yyyy-MM') AS mois,\n"
            "       p.portfolio_name,\n"
            "       AVG(n.aum) AS aum_moyen\n"
            "FROM pf.nav n\n"
            "JOIN pf.portfolio p ON n.portfolio_id = p.portfolio_id\n"
            "GROUP BY FORMAT(n.nav_date, 'yyyy-MM'), p.portfolio_name\n"
            "ORDER BY mois",

        "🗺️ Heatmap rendements mensuels (NAV)":
            "SELECT p.portfolio_name,\n"
            "       FORMAT(n.nav_date, 'yyyy-MM') AS mois,\n"
            "       ROUND(\n"
            "           (MAX(n.nav_value) - MIN(n.nav_value)) * 100.0 / NULLIF(MIN(n.nav_value), 0)\n"
            "       , 2) AS rendement_pct\n"
            "FROM pf.nav n\n"
            "JOIN pf.portfolio p ON n.portfolio_id = p.portfolio_id\n"
            "GROUP BY p.portfolio_name, FORMAT(n.nav_date, 'yyyy-MM')\n"
            "ORDER BY mois",

        # ── POSITIONS ────────────────────────────────────────────────────────
        "🥧 Répartition par classe d'actifs (camembert)":
            "SELECT i.asset_class, SUM(pos.market_value) AS valeur_totale\n"
            "FROM pf.position pos\n"
            "JOIN pf.instrument i ON pos.instrument_id = i.instrument_id\n"
            "GROUP BY i.asset_class\n"
            "ORDER BY valeur_totale DESC",

        "📊 Top 10 positions par valeur de marché":
            "SELECT TOP 10\n"
            "       i.instrument_name,\n"
            "       i.asset_class,\n"
            "       SUM(pos.market_value) AS valeur_marche\n"
            "FROM pf.position pos\n"
            "JOIN pf.instrument i ON pos.instrument_id = i.instrument_id\n"
            "GROUP BY i.instrument_name, i.asset_class\n"
            "ORDER BY valeur_marche DESC",

        "📊 Positions par portefeuille et classe d'actifs":
            "SELECT p.portfolio_name, i.asset_class,\n"
            "       SUM(pos.market_value) AS valeur\n"
            "FROM pf.position pos\n"
            "JOIN pf.portfolio p ON pos.portfolio_id = p.portfolio_id\n"
            "JOIN pf.instrument i ON pos.instrument_id = i.instrument_id\n"
            "GROUP BY p.portfolio_name, i.asset_class\n"
            "ORDER BY p.portfolio_name, valeur DESC",

        "📊 Concentration : poids de chaque instrument (%) par portefeuille":
            "SELECT p.portfolio_name, i.instrument_name,\n"
            "       ROUND(pos.market_value * 100.0 /\n"
            "           SUM(pos.market_value) OVER (PARTITION BY p.portfolio_name), 2) AS poids_pct\n"
            "FROM pf.position pos\n"
            "JOIN pf.portfolio p ON pos.portfolio_id = p.portfolio_id\n"
            "JOIN pf.instrument i ON pos.instrument_id = i.instrument_id\n"
            "ORDER BY p.portfolio_name, poids_pct DESC",

        # ── TRANSACTIONS ─────────────────────────────────────────────────────
        "📊 Transactions BUY/SELL par mois":
            "SELECT FORMAT(t.trade_date, 'yyyy-MM') AS mois, t.side, COUNT(*) AS nb\n"
            "FROM pf.[transaction] t\n"
            "GROUP BY FORMAT(t.trade_date, 'yyyy-MM'), t.side\n"
            "ORDER BY mois",

        "📊 Volume transactionné par portefeuille (quantité × prix)":
            "SELECT p.portfolio_name,\n"
            "       FORMAT(t.trade_date, 'yyyy-MM') AS mois,\n"
            "       ROUND(SUM(t.quantity * t.price), 0) AS volume\n"
            "FROM pf.[transaction] t\n"
            "JOIN pf.portfolio p ON t.portfolio_id = p.portfolio_id\n"
            "GROUP BY p.portfolio_name, FORMAT(t.trade_date, 'yyyy-MM')\n"
            "ORDER BY mois",

        "📊 Frais totaux par instrument (TOP 10)":
            "SELECT TOP 10 i.instrument_name,\n"
            "       ROUND(SUM(ISNULL(t.fees, 0)), 2) AS frais_totaux\n"
            "FROM pf.[transaction] t\n"
            "JOIN pf.instrument i ON t.instrument_id = i.instrument_id\n"
            "GROUP BY i.instrument_name\n"
            "ORDER BY frais_totaux DESC",

        "🔵 Scatter : prix vs quantité par transaction":
            "SELECT t.price, t.quantity, t.side, i.instrument_name\n"
            "FROM pf.[transaction] t\n"
            "JOIN pf.instrument i ON t.instrument_id = i.instrument_id\n"
            "ORDER BY t.trade_date DESC",

        # ── INDICES & BENCHMARKS ─────────────────────────────────────────────
        "📈 Performance des indices dans le temps":
            "SELECT r.index_name, il.level_date, il.close_level\n"
            "FROM pf.index_level il\n"
            "JOIN pf.index_ref r ON il.index_id = r.index_id\n"
            "ORDER BY il.level_date",

        "📈 Indices — base 100 (comparaison normalisée)":
            "SELECT r.index_name, il.level_date,\n"
            "       il.close_level * 100.0 / FIRST_VALUE(il.close_level)\n"
            "           OVER (PARTITION BY r.index_name ORDER BY il.level_date) AS base_100\n"
            "FROM pf.index_level il\n"
            "JOIN pf.index_ref r ON il.index_id = r.index_id\n"
            "ORDER BY il.level_date",

        "🥧 Composition des benchmarks (%)":
            "SELECT b.benchmark_name, r.index_name,\n"
            "       ROUND(bc.weight * 100, 2) AS poids_pct\n"
            "FROM pf.benchmark_component bc\n"
            "JOIN pf.benchmark b ON bc.benchmark_id = b.benchmark_id\n"
            "JOIN pf.index_ref r ON bc.index_id = r.index_id\n"
            "ORDER BY b.benchmark_name, poids_pct DESC",

        "📊 Rendement mensuel des indices (%)":
            "SELECT r.index_name,\n"
            "       FORMAT(il.level_date, 'yyyy-MM') AS mois,\n"
            "       ROUND(\n"
            "           (MAX(il.close_level) - MIN(il.close_level)) * 100.0\n"
            "           / NULLIF(MIN(il.close_level), 0)\n"
            "       , 2) AS rendement_pct\n"
            "FROM pf.index_level il\n"
            "JOIN pf.index_ref r ON il.index_id = r.index_id\n"
            "GROUP BY r.index_name, FORMAT(il.level_date, 'yyyy-MM')\n"
            "ORDER BY mois",

        # ── IMPORT LOG ───────────────────────────────────────────────────────
        "📊 Imports : lignes insérées vs ignorées par fichier":
            "SELECT csv_filename, rows_inserted, rows_skipped, status\n"
            "FROM pf.import_log\n"
            "ORDER BY started_at DESC",

        "📊 Nombre d'imports par statut":
            "SELECT status, COUNT(*) AS nb_imports\n"
            "FROM pf.import_log\n"
            "GROUP BY status\n"
            "ORDER BY nb_imports DESC",

        "📈 Lignes insérées cumulées dans le temps":
            "SELECT started_at, target_table,\n"
            "       SUM(rows_inserted) OVER (ORDER BY started_at) AS cumul_insere\n"
            "FROM pf.import_log\n"
            "WHERE status = 'SUCCESS'\n"
            "ORDER BY started_at",
    }

    CHART_TYPES = {
        "📈 Courbe":          "line",
        "📊 Barres":          "bar",
        "📊 Barres empilées": "bar_stacked",
        "🥧 Camembert":       "pie",
        "🔵 Scatter":         "scatter",
        "📊 Histogramme":     "histogram",
        "🗺️ Heatmap":        "heatmap",
    }

    PALETTES = {
        "Défaut":      ["#F2C94C","#56CCF2","#6FCF97","#EB5757","#BB6BD9","#F2994A","#2D9CDB"],
        "Bleu":        px.colors.sequential.Blues[::-1],
        "Vert":        px.colors.sequential.Greens[::-1],
        "Chaud":       px.colors.sequential.OrRd[::-1],
        "Arc-en-ciel": px.colors.qualitative.Plotly,
        "Pastel":      px.colors.qualitative.Pastel,
    }

    DEFAULT_SQL = (
        "SELECT p.portfolio_name, n.nav_date, n.nav_value\n"
        "FROM pf.nav n\n"
        "JOIN pf.portfolio p ON n.portfolio_id = p.portfolio_id\n"
        "ORDER BY n.nav_date"
    )

    # ── Session state : liste des IDs de blocs ────────────────────────────────
    if "sql_blocks" not in st.session_state:
        st.session_state["sql_blocks"] = [0]
        st.session_state["sql_block_next_id"] = 1

    # ── Style ─────────────────────────────────────────────────────────────────
    st.markdown("""
    <style>
    .sql-title  { font-size:1.4rem; font-weight:700; color:#F7FAFC; margin-bottom:4px; }
    .sql-sub    { font-size:0.9rem; color:#A0AEC0; margin-bottom:16px; }
    .block-card {
        border:1px solid #2D2D44; border-radius:12px;
        padding:20px 22px; margin-bottom:24px;
        background:#13131F;
    }
    .block-header {
        font-size:1.05rem; font-weight:700; color:#F2C94C;
        margin-bottom:12px;
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="sql-title">🔬 Analyse SQL → Graphique</div>', unsafe_allow_html=True)
    st.markdown('<div class="sql-sub">Construisez votre rapport : ajoutez autant de graphiques que vous voulez, chacun avec sa propre requête SQL.</div>', unsafe_allow_html=True)

    # ── Boutons globaux ───────────────────────────────────────────────────────
    hdr1, hdr2, hdr3 = st.columns([2, 2, 2])

    if hdr1.button("➕ Ajouter un graphique", type="primary", use_container_width=True):
        new_id = st.session_state["sql_block_next_id"]
        st.session_state["sql_blocks"].append(new_id)
        st.session_state["sql_block_next_id"] = new_id + 1
        st.rerun()

    if hdr2.button("▶▶ Tout exécuter", use_container_width=True):
        st.session_state["run_all"] = True
        st.rerun()

    if hdr3.button("🗑️ Tout effacer", use_container_width=True):
        for bid in st.session_state["sql_blocks"]:
            for suffix in ["_df", "_sql_val"]:
                st.session_state.pop(f"block_{bid}{suffix}", None)
        st.session_state["sql_blocks"] = [0]
        st.session_state["sql_block_next_id"] = 1
        st.session_state.pop("run_all", None)
        st.session_state.pop("pdf_pages", None)
        st.session_state.pop("_pdf_bytes", None)
        st.session_state.pop("_pdf_name", None)
        st.session_state.pop("_pdf_count", None)
        st.rerun()

    # ── Tout exécuter ─────────────────────────────────────────────────────────
    if st.session_state.pop("run_all", False):
        errors = []
        success = 0
        with st.spinner(f"Exécution de {len(st.session_state['sql_blocks'])} requête(s)…"):
            for bid in st.session_state["sql_blocks"]:
                k   = str(bid)
                sql = st.session_state.get(f"sql_{k}", DEFAULT_SQL)
                if not sql.strip().upper().startswith("SELECT"):
                    errors.append(f"Bloc {bid+1} : requête non SELECT ignorée")
                    continue
                try:
                    conn   = pyodbc.connect(CONN_PF, timeout=10, autocommit=True)
                    df_res = pd.read_sql(sql, conn)
                    conn.close()
                    st.session_state[f"block_{k}_df"] = df_res
                    success += 1
                except Exception as e:
                    errors.append(f"Bloc {bid+1} : {e}")
        if errors:
            for err in errors:
                st.error(err)
        st.success(f"✅ {success} requête(s) exécutée(s) avec succès.")

    st.markdown("---")

    # ── Fonction de rendu d'un bloc ───────────────────────────────────────────
    def render_block(bid):
        k = str(bid)  # préfixe unique pour toutes les clés de ce bloc

        st.markdown(f'<div class="block-header">📌 Graphique {bid + 1}</div>', unsafe_allow_html=True)

        # Exemple de requête
        ex_key  = f"ex_{k}"
        sql_key = f"sql_{k}"
        prev_ex = st.session_state.get(f"block_{k}_prev_ex", "(choisir un exemple)")

        chosen_ex = st.selectbox("💡 Exemple de requête", list(EXAMPLES.keys()), key=ex_key)

        # Dès que l'utilisateur change d'exemple → injecter directement dans le text_area
        if chosen_ex != "(choisir un exemple)" and chosen_ex != prev_ex:
            st.session_state[sql_key] = EXAMPLES[chosen_ex]
            st.session_state[f"block_{k}_prev_ex"] = chosen_ex
            st.rerun()

        # Éditeur SQL
        default_val = st.session_state.get(sql_key, DEFAULT_SQL)
        sql_query = st.text_area("Requête SQL", value=default_val, height=130, key=sql_key)

        col_run, col_del = st.columns([1, 1])
        run_ok = col_run.button("▶ Exécuter", key=f"run_{k}", type="primary", use_container_width=True)
        delete_ok = col_del.button("🗑️ Supprimer ce bloc", key=f"del_{k}", use_container_width=True)

        if delete_ok:
            st.session_state["sql_blocks"].remove(bid)
            for suffix in ["_df", "_sql_val"]:
                st.session_state.pop(f"block_{k}{suffix}", None)
            st.rerun()

        if run_ok:
            if not sql_query.strip().upper().startswith("SELECT"):
                st.error("⛔ Seules les requêtes SELECT sont autorisées.")
            else:
                try:
                    with st.spinner("Exécution…"):
                        conn = pyodbc.connect(CONN_PF, timeout=10, autocommit=True)
                        df_res = pd.read_sql(sql_query, conn)
                        conn.close()
                    st.session_state[f"block_{k}_df"] = df_res
                except Exception as e:
                    st.error(f"Erreur SQL : {e}")
                    st.session_state.pop(f"block_{k}_df", None)

        # ── Résultat + configurateur ──────────────────────────────────────────
        if f"block_{k}_df" in st.session_state:
            df = st.session_state[f"block_{k}_df"]
            st.caption(f"{len(df):,} lignes · {len(df.columns)} colonnes")

            with st.expander("📋 Données brutes", expanded=False):
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.download_button(
                    "⬇️ CSV", data=df.to_csv(index=False, sep=";").encode("utf-8-sig"),
                    file_name=f"graphique_{bid+1}.csv", mime="text/csv", key=f"csv_{k}"
                )

            all_cols  = df.columns.tolist()
            num_cols  = df.select_dtypes(include="number").columns.tolist()
            cat_cols  = df.select_dtypes(exclude="number").columns.tolist()
            date_cols = [c for c in all_cols if "date" in c.lower() or "mois" in c.lower()]

            st.markdown("**⚙️ Configuration du graphique**")
            cfg1, cfg2 = st.columns([1, 2])

            with cfg1:
                chart_label  = st.selectbox("Type", list(CHART_TYPES.keys()), key=f"ctype_{k}")
                chart_type   = CHART_TYPES[chart_label]

                auto_x = date_cols[0] if date_cols else (cat_cols[0] if cat_cols else all_cols[0])
                auto_y = num_cols[0]  if num_cols  else all_cols[-1]

                x_col = st.selectbox("Axe X", all_cols,
                    index=all_cols.index(auto_x) if auto_x in all_cols else 0, key=f"xcol_{k}")

                if chart_type not in ("pie", "histogram"):
                    y_col = st.selectbox("Axe Y", all_cols,
                        index=all_cols.index(auto_y) if auto_y in all_cols else 0, key=f"ycol_{k}")
                else:
                    y_col = None

                _color_opts = ["(aucun)"] + cat_cols
                _auto_cidx = 0
                if chart_type in ("line", "bar", "bar_stacked") and cat_cols:
                    _non_x = [c for c in cat_cols if c != x_col]
                    if _non_x and _non_x[0] in _color_opts:
                        _auto_cidx = _color_opts.index(_non_x[0])
                color_raw = st.selectbox("Couleur / Groupe", _color_opts,
                                         index=_auto_cidx, key=f"ccol_{k}")
                color_col = None if color_raw == "(aucun)" else color_raw

                if chart_type == "pie":
                    val_col = st.selectbox("Valeurs", num_cols if num_cols else all_cols, key=f"pval_{k}")
                if chart_type == "heatmap":
                    z_col = st.selectbox("Valeur Z", num_cols if num_cols else all_cols, key=f"hz_{k}")
                    y_hm  = st.selectbox("Axe Y (heatmap)", all_cols, key=f"hmy_{k}")

            with cfg2:
                chart_title  = st.text_input("Titre", value=f"Graphique {bid+1}", key=f"ctitle_{k}")
                h1, h2 = st.columns(2)
                chart_height = h1.slider("Hauteur (px)", 300, 800, 400, step=50, key=f"ch_{k}")
                pal_name     = h2.selectbox("Palette", list(PALETTES.keys()), key=f"pal_{k}")
                palette      = PALETTES[pal_name]
                o1, o2, o3   = st.columns(3)
                show_markers = o1.checkbox("Marqueurs", value=True, key=f"mk_{k}")
                show_values  = o2.checkbox("Valeurs", value=False, key=f"sv_{k}")
                log_y        = o3.checkbox("Log Y", value=False, key=f"ly_{k}")

            # ── Génération ────────────────────────────────────────────────────
            def apply_theme(fig):
                fig.update_layout(
                    title=dict(text=chart_title, font=dict(size=15, color=FONT_COLOR), x=0.01),
                    plot_bgcolor=CHART_BG, paper_bgcolor=PAPER_BG,
                    font=dict(color=FONT_COLOR, family="Segoe UI, Arial"),
                    height=chart_height,
                    margin=dict(l=12, r=12, t=44, b=12),
                    legend=dict(bgcolor="rgba(0,0,0,0)"),
                    xaxis=dict(gridcolor=GRID_COLOR, linecolor=GRID_COLOR, zeroline=False),
                    yaxis=dict(gridcolor=GRID_COLOR, linecolor=GRID_COLOR, zeroline=False,
                               type="log" if log_y else "linear"),
                )
                return fig

            try:
                fig = None
                if chart_type == "line":
                    fig = px.line(df, x=x_col, y=y_col, color=color_col,
                                  color_discrete_sequence=palette, markers=show_markers)
                elif chart_type in ("bar", "bar_stacked"):
                    fig = px.bar(df, x=x_col, y=y_col, color=color_col,
                                 color_discrete_sequence=palette,
                                 barmode="stack" if chart_type == "bar_stacked" else "group",
                                 text_auto=".2s" if show_values else False)
                    if show_values:
                        fig.update_traces(textposition="outside")
                elif chart_type == "pie":
                    fig = px.pie(df, names=x_col, values=val_col,
                                 color_discrete_sequence=palette, hole=0.4)
                elif chart_type == "scatter":
                    fig = px.scatter(df, x=x_col, y=y_col, color=color_col,
                                     color_discrete_sequence=palette)
                elif chart_type == "histogram":
                    fig = px.histogram(df, x=x_col, color=color_col,
                                       color_discrete_sequence=palette, barmode="group")
                elif chart_type == "heatmap":
                    pivot = df.pivot(index=y_hm, columns=x_col, values=z_col)
                    fig = go.Figure(go.Heatmap(
                        z=pivot.values.tolist(),
                        x=[str(c) for c in pivot.columns],
                        y=[str(r) for r in pivot.index],
                        colorscale="RdYlGn", zmid=0,
                        text=[[f"{v:.2f}" if isinstance(v, float) and not pd.isna(v) else ""
                               for v in row] for row in pivot.values.tolist()],
                        texttemplate="%{text}",
                        colorbar=dict(tickfont=dict(color=FONT_COLOR)),
                    ))

                if fig:
                    fig = apply_theme(fig)
                    st.plotly_chart(fig, use_container_width=True)
                    try:
                        img_buf = _io.BytesIO()
                        fig.write_image(img_buf, format="png", width=1200, height=chart_height, scale=2)
                        img_buf.seek(0)
                        st.download_button("⬇️ Télécharger PNG", data=img_buf,
                                           file_name=f"{chart_title.replace(' ','_')}.png",
                                           mime="image/png", key=f"png_{k}")
                    except Exception:
                        pass
            except Exception as chart_err:
                st.error(f"Erreur graphique : {chart_err}")
                st.info("Vérifiez que les colonnes choisies correspondent au type de graphique.")

    # ── Rendu de tous les blocs ───────────────────────────────────────────────
    for bid in list(st.session_state["sql_blocks"]):
        with st.container():
            st.markdown('<div class="block-card">', unsafe_allow_html=True)
            render_block(bid)
            st.markdown('</div>', unsafe_allow_html=True)

    # ── Bouton bas de page ────────────────────────────────────────────────────
    st.markdown("---")
    bot1, bot2 = st.columns([2, 2])
    if bot1.button("➕ Ajouter un graphique", type="primary", key="add_bottom", use_container_width=True):
        new_id = st.session_state["sql_block_next_id"]
        st.session_state["sql_blocks"].append(new_id)
        st.session_state["sql_block_next_id"] = new_id + 1
        st.rerun()
    if bot2.button("▶▶ Tout exécuter", key="run_all_bot", use_container_width=True):
        st.session_state["run_all"] = True
        st.rerun()


    # ══════════════════════════════════════════════════════════════════════════
    # MISE EN PAGE FACTSHEET — Style Natixis (SQL + données DB combinés)
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("## 📐 Mise en page Factsheet")
    st.caption(
        "Assignez vos **graphiques SQL** et les **données automatiques** (NAV, performances, "
        "allocation, frais…) dans chaque case. Le PDF sera rendu dans un design professionnel "
        "style Natixis avec en-tête, pied de page et palette de couleurs corporate."
    )

    # ── Chargement des listes DB ──────────────────────────────────────────────
    @st.cache_data(ttl=300)
    def _nfs_load_pf():
        try:
            _c = pyodbc.connect(CONN_PF, timeout=5, autocommit=True)
            _r = pd.read_sql("SELECT portfolio_name FROM pf.portfolio ORDER BY portfolio_name",
                             _c)["portfolio_name"].tolist()
            _c.close(); return _r
        except Exception: return []

    @st.cache_data(ttl=300)
    def _nfs_load_bm():
        try:
            _c = pyodbc.connect(CONN_PF, timeout=5, autocommit=True)
            _r = pd.read_sql("SELECT benchmark_name FROM pf.benchmark ORDER BY benchmark_name",
                             _c)["benchmark_name"].tolist()
            _c.close(); return _r
        except Exception: return []

    _nfs_pf_list = _nfs_load_pf()
    _nfs_bm_list = _nfs_load_bm()

    # ── Informations du fonds ─────────────────────────────────────────────────
    with st.expander("⚙️ Informations du fonds", expanded=False):
        _nf1, _nf2, _nf3 = st.columns(3)
        if _nfs_pf_list:
            _nf1.selectbox("Portefeuille (données auto)", _nfs_pf_list, key="nfs_pf")
        else:
            _nf1.text_input("Portefeuille / Nom du fonds", key="nfs_pf")
        _nf2.text_input("ISIN", key="nfs_isin")
        _nf3.text_input("Bloomberg Ticker", key="nfs_bloomberg")

        _nf4, _nf5, _nf6 = st.columns(3)
        _nf4.text_input("Share Class", value="N/C (EUR)", key="nfs_sc")
        _nf5.selectbox("Benchmark", ["(aucun)"] + _nfs_bm_list, key="nfs_bm")
        _nf6.text_input("Date du rapport", value=datetime.now().strftime("%B %Y"), key="nfs_rdate")

        _nf7, _nf8 = st.columns(2)
        _nf7.text_area("Objectif d'investissement",
            value="L'objectif du fonds est d'obtenir une performance supérieure à son indice de "
                  "référence sur la durée de placement recommandée, après déduction des frais.",
            height=100, key="nfs_obj")
        _nf8.text_area("Points clés (un par ligne)",
            value="Investit principalement dans des titres obligataires de haute qualité\n"
                  "Gestion active basée sur une analyse approfondie\n"
                  "Faible sensibilité au risque de taux\n"
                  "Diversification géographique et sectorielle\n"
                  "Classification SFDR : Article 8",
            height=100, key="nfs_hl")

        _nf9, _nf10, _nf11, _nf12 = st.columns(4)
        _nf9.text_input("Frais courants", value="0.40%", key="nfs_fo")
        _nf10.text_input("Frais d'entrée max", value="0.00%", key="nfs_fs_v")
        _nf11.text_input("Frais de rachat max", value="0.00%", key="nfs_fr")
        _nf12.number_input("Niveau de risque (1-7)", min_value=1, max_value=7,
                            value=3, step=1, key="nfs_risk")

        _nf13, _nf14 = st.columns(2)
        _nf13.text_input("Société de gestion", value="NATIXIS INVESTMENT MANAGERS", key="nfs_mc")
        _nf14.text_input("Gestionnaire", value="LOOMIS SAYLES (NETHERLANDS) B.V.", key="nfs_im")

    # ── Sources de contenu disponibles ───────────────────────────────────────
    _AUTO_SRC = {
        "📈 Croissance 10 000 (NAV)":          "auto:nav_growth",
        "📊 Rendements annuels (barres)":       "auto:cal_returns",
        "📋 Performances totales (%)":           "auto:perf_table",
        "📋 Performance annualisée (%)":         "auto:ann_table",
        "📋 Mesures de risque":                  "auto:risk_table",
        "🥧 Répartition d'actifs (camembert)": "auto:alloc_pie",
        "📋 Répartition d'actifs (tableau)":   "auto:alloc_table",
        "📋 Top 10 positions":                   "auto:top10",
        "📋 Répartition par devise":             "auto:ccy",
        "📋 Caractéristiques du fonds":          "auto:fund_chars",
        "🎚️ Profil de risque":                  "auto:risk_profile",
        "📝 Objectif d'investissement":         "auto:invest_obj",
        "📋 Frais":                              "auto:fees",
        "📋 Management":                         "auto:mgmt",
    }
    _CUSTOM_SRC = {}
    for _cbid in st.session_state["sql_blocks"]:
        _cbk = str(_cbid)
        if f"block_{_cbk}_df" in st.session_state:
            _cbt = st.session_state.get(f"ctitle_{_cbk}", f"Graphique {_cbid+1}")
            _CUSTOM_SRC[f"📊 SQL #{_cbid+1} — {_cbt}"] = f"custom:{_cbid}"

    _ALL_SRC_OPTS = ["(vide)"] + list(_AUTO_SRC.keys()) + list(_CUSTOM_SRC.keys())
    _SRC_KEY_MAP  = {"(vide)": "(vide)", **_AUTO_SRC, **_CUSTOM_SRC}

    # ── Layouts ───────────────────────────────────────────────────────────────
    NFS_LAYOUTS = {
        "⬛ Pleine page (1 contenu)":  {"slots": 1, "grid": (1, 1)},
        "◧ 2 colonnes côte à côte":   {"slots": 2, "grid": (1, 2)},
        "⬒ 2 lignes empilées":        {"slots": 2, "grid": (2, 1)},
        "⊞ Grille 2×2 (4 contenus)":  {"slots": 4, "grid": (2, 2)},
    }
    NFS_SLOT_LABELS = {
        (1,1): ["Centre"],
        (1,2): ["Gauche", "Droite"],
        (2,1): ["Haut", "Bas"],
        (2,2): ["Haut-gauche", "Haut-droite", "Bas-gauche", "Bas-droite"],
    }
    if "nfs_pages" not in st.session_state:
        st.session_state["nfs_pages"] = []

    # ── Tabs : Automatique vs Personnalisé ────────────────────────────────────
    _tab_auto5, _tab_cust5 = st.tabs(
        ["🏛️ Factsheet Automatique Natixis", "🎛️ Mise en page personnalisée"])

    with _tab_auto5:
        st.info(
            "Génère un factsheet complet style Natixis en **un seul clic** — "
            "NAV, performances, allocation, frais, management… "
            "Sélectionnez votre portefeuille dans les informations du fonds ci-dessus.")
        if st.button("🚀 Générer Factsheet Natixis Complet",
                     type="primary", use_container_width=True, key="gen_nfs_auto"):
            st.session_state["_gen_nfs"]  = True
            st.session_state["_nfs_mode"] = "auto"
            st.rerun()

    with _tab_cust5:
        st.caption("Ajoutez des pages, choisissez la disposition et assignez les contenus.")
        _na1, _na2 = st.columns([2, 5])
        if _na1.button("➕ Ajouter une page", use_container_width=True, key="nfs_add"):
            st.session_state["nfs_pages"].append(
                {"layout": "⬛ Pleine page (1 contenu)", "slots": ["(vide)"]})
            st.rerun()
        if _na2.button("🗑️ Réinitialiser", use_container_width=True, key="nfs_reset"):
            st.session_state["nfs_pages"] = []
            st.session_state.pop("_nfs_pdf_bytes", None)
            st.rerun()

        for _npi, _npg in enumerate(st.session_state["nfs_pages"]):
            with st.container():
                st.markdown(
                    f'<div style="border:1px solid #2D2D44;border-radius:10px;'
                    f'padding:16px 20px;margin-bottom:12px;background:#13131F">'
                    f'<span style="color:#F2C94C;font-weight:700;font-size:1rem">'
                    f'📄 Page {_npi+1}</span>',
                    unsafe_allow_html=True)
                _npc1, _npc2 = st.columns([4, 1])
                _nl_key = f"nfs_lay_{_npi}"
                _nl_val = _npc1.selectbox(
                    "Disposition", list(NFS_LAYOUTS.keys()),
                    index=list(NFS_LAYOUTS.keys()).index(_npg["layout"])
                          if _npg["layout"] in NFS_LAYOUTS else 0,
                    key=_nl_key)
                if _npc2.button("🗑️ Supprimer", key=f"nfs_del_{_npi}",
                                use_container_width=True):
                    st.session_state["nfs_pages"].pop(_npi)
                    st.rerun()
                _nl_info = NFS_LAYOUTS[_nl_val]
                _nl_nb   = _nl_info["slots"]
                _nl_grid = _nl_info["grid"]
                _nl_lbl  = NFS_SLOT_LABELS[_nl_grid]
                _ns_cols = st.columns(_nl_nb)
                _ns_vals = []
                for _nsi in range(_nl_nb):
                    _prev_s = _npg["slots"][_nsi] if _nsi < len(_npg["slots"]) else "(vide)"
                    if _prev_s not in _ALL_SRC_OPTS:
                        _prev_s = "(vide)"
                    _chosen_s = _ns_cols[_nsi].selectbox(
                        f"📌 {_nl_lbl[_nsi]}",
                        _ALL_SRC_OPTS,
                        index=_ALL_SRC_OPTS.index(_prev_s),
                        key=f"nfs_slot_{_npi}_{_nsi}"
                    )
                    _ns_vals.append(_chosen_s)
                st.session_state["nfs_pages"][_npi] = {"layout": _nl_val, "slots": _ns_vals}
                st.markdown("</div>", unsafe_allow_html=True)

        if st.session_state["nfs_pages"]:
            st.markdown("---")
            st.markdown("### 🖥️ Aperçu de la mise en page")
            st.caption("Vert = données DB automatiques  ·  Bleu = graphique SQL  ·  Gris = vide")

            def _nfs_preview_slot(src_label, col):
                if src_label == "(vide)":
                    col.markdown(
                        "<div style='border:2px dashed #BDBDBD;border-radius:6px;"
                        "padding:14px;text-align:center;color:#BDBDBD;"
                        "font-style:italic;font-size:0.75rem'>case vide</div>",
                        unsafe_allow_html=True); return
                src_k = _SRC_KEY_MAP.get(src_label, "(vide)")
                if src_k.startswith("auto:"):
                    bg="#E8F5E9"; br="#2E7D32"; tc="#1B5E20"; badge="🔄 Données DB"
                elif src_k.startswith("custom:"):
                    bg="#E3F2FD"; br="#1565C0"; tc="#0D47A1"; badge="📊 Graphique SQL"
                else:
                    bg="#F5F5F5"; br="#BDBDBD"; tc="#616161"; badge="?"
                icon = src_label.split(" ")[0] if src_label else "?"
                name = src_label[len(icon)+1:] if len(src_label) > len(icon)+1 else src_label
                col.markdown(
                    f"<div style='border:2px solid {br};border-radius:6px;background:{bg};"
                    f"padding:10px;text-align:center'>"
                    f"<div style='font-size:1.25rem'>{icon}</div>"
                    f"<div style='font-weight:700;color:{tc};font-size:0.70rem;"
                    f"margin-top:4px'>{name[:30]}</div>"
                    f"<div style='color:{br};font-size:0.60rem;font-weight:700;margin-top:5px;"
                    f"border:1px solid {br};border-radius:3px;display:inline-block;"
                    f"padding:1px 5px'>{badge}</div></div>",
                    unsafe_allow_html=True)

            _nfs_nb = len(st.session_state["nfs_pages"])
            for _rstart in range(0, _nfs_nb, 2):
                _batch = st.session_state["nfs_pages"][_rstart:_rstart+2]
                if len(_batch) == 2:
                    _pcl, _, _pcr = st.columns([10, 1, 10])
                    _pcols = [_pcl, _pcr]
                else:
                    _pcl, _ = st.columns([10, 10])
                    _pcols = [_pcl]
                for _ci, (_pcc, _pg) in enumerate(zip(_pcols, _batch)):
                    _pnum  = _rstart + _ci + 1
                    _li    = NFS_LAYOUTS.get(_pg["layout"], {"slots":1,"grid":(1,1)})
                    _nr, _nc = _li["grid"]
                    _slots = _pg["slots"]
                    _pcc.markdown(
                        f"<div style='background:#1A237E;border-radius:6px 6px 0 0;"
                        f"padding:6px 12px;display:flex;justify-content:space-between;"
                        f"align-items:center'>"
                        f"<span style='color:#F9A825;font-weight:700;font-size:0.80rem'>"
                        f"📄 Page {_pnum}</span>"
                        f"<span style='color:#A0AEC0;font-size:0.68rem'>{_pg['layout']}</span>"
                        f"<span style='color:#BDBDBD;font-size:0.64rem'>"
                        f"p.{_pnum}/{_nfs_nb}</span></div>",
                        unsafe_allow_html=True)
                    with _pcc.container():
                        if _nc == 1:
                            for _sl in _slots:
                                _nfs_preview_slot(_sl, _pcc)
                                if len(_slots) > 1:
                                    _pcc.markdown("<div style='height:4px'></div>",
                                                  unsafe_allow_html=True)
                        else:
                            if _nr == 1:
                                _sc1, _sc2 = _pcc.columns(2)
                                _nfs_preview_slot(_slots[0] if _slots else "(vide)", _sc1)
                                _nfs_preview_slot(_slots[1] if len(_slots)>1 else "(vide)", _sc2)
                            else:
                                _sc1, _sc2 = _pcc.columns(2)
                                _nfs_preview_slot(_slots[0] if _slots else "(vide)", _sc1)
                                _nfs_preview_slot(_slots[1] if len(_slots)>1 else "(vide)", _sc2)
                                _pcc.markdown("<div style='height:4px'></div>",
                                              unsafe_allow_html=True)
                                _sc3, _sc4 = _pcc.columns(2)
                                _nfs_preview_slot(_slots[2] if len(_slots)>2 else "(vide)", _sc3)
                                _nfs_preview_slot(_slots[3] if len(_slots)>3 else "(vide)", _sc4)
                    _pcc.markdown(
                        "<div style='border:1px solid #1565C0;border-top:none;"
                        "border-radius:0 0 6px 6px;text-align:center;padding:3px;"
                        "color:#BDBDBD;font-size:0.60rem'>Format A4  ·  STAGEPORTFOLIO</div>",
                        unsafe_allow_html=True)
                    _pcc.markdown("")

        st.markdown("")
        if st.button("🚀 Générer le Factsheet (mise en page personnalisée)",
                     type="primary", use_container_width=True,
                     disabled=len(st.session_state["nfs_pages"]) == 0,
                     key="gen_nfs_custom"):
            st.session_state["_gen_nfs"]  = True
            st.session_state["_nfs_mode"] = "custom"
            st.rerun()

    # ── Génération PDF ─────────────────────────────────────────────────────────
    if st.session_state.pop("_gen_nfs", False):
        _nfs_mode = st.session_state.get("_nfs_mode", "auto")
        import io as _io5
        import numpy as _np5
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors as _rlc5
        from reportlab.lib.units import cm
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Image as _RLImg5,
            Table, TableStyle, PageBreak, HRFlowable, KeepInFrame
        )
        try:
            from dateutil.relativedelta import relativedelta as _rd5
        except ImportError:
            st.error("pip install python-dateutil"); st.stop()

        _p = lambda k, d="": st.session_state.get(k, d)
        _pf5   = _p("nfs_pf");    _isin5 = _p("nfs_isin")
        _bb5   = _p("nfs_bloomberg"); _sc5 = _p("nfs_sc", "N/C (EUR)")
        _bm5   = _p("nfs_bm", "(aucun)"); _rd5v = _p("nfs_rdate", datetime.now().strftime("%B %Y"))
        _obj5  = _p("nfs_obj");   _hl5  = _p("nfs_hl")
        _fo5   = _p("nfs_fo", "0.40%"); _fsv5 = _p("nfs_fs_v", "0.00%")
        _fr5   = _p("nfs_fr", "0.00%"); _risk5 = int(_p("nfs_risk") or 3)
        _mc5   = _p("nfs_mc");    _im5  = _p("nfs_im")
        _gdt5  = datetime.now().strftime("%d/%m/%Y")

        # ── Chargement DB ──────────────────────────────────────────────────────
        if _nfs_mode == "auto":
            _has_auto5 = bool(_pf5)
        else:
            _has_auto5 = any(
                _SRC_KEY_MAP.get(s, "(vide)").startswith("auto:")
                for _pg in st.session_state["nfs_pages"] for s in _pg["slots"]
            )
        _ndf5=None; _pos5=None; _bm5df=None
        _last_nav5=None; _last_date5=None; _incep5=None
        _curr5="EUR"; _last_aum5=0.0; _total_mv5=0.0

        if _has_auto5 and _pf5:
            with st.spinner("Chargement des données…"):
                try:
                    _dbc5 = pyodbc.connect(CONN_PF, timeout=10, autocommit=True)
                    _spf5 = _pf5.replace("'","''")
                    _ndf5 = pd.read_sql(
                        "SELECT n.nav_date, n.nav_value, n.aum,"
                        " p.base_currency, p.inception_date"
                        " FROM pf.nav n"
                        " JOIN pf.portfolio p ON n.portfolio_id=p.portfolio_id"
                        f" WHERE p.portfolio_name='{_spf5}' ORDER BY n.nav_date", _dbc5)
                    _pos5 = pd.read_sql(
                        "SELECT i.instrument_name, i.asset_class, i.currency,"
                        " pos.market_value, pos.quantity, pos.position_date"
                        " FROM pf.position pos"
                        " JOIN pf.portfolio p ON pos.portfolio_id=p.portfolio_id"
                        " JOIN pf.instrument i ON pos.instrument_id=i.instrument_id"
                        f" WHERE p.portfolio_name='{_spf5}'"
                        f"   AND pos.position_date = ("
                        f"     SELECT MAX(pos2.position_date)"
                        f"     FROM pf.position pos2"
                        f"     JOIN pf.portfolio p2 ON pos2.portfolio_id=p2.portfolio_id"
                        f"     WHERE p2.portfolio_name='{_spf5}')"
                        " ORDER BY pos.market_value DESC", _dbc5)
                    if _bm5 != "(aucun)":
                        try:
                            _sbm5 = _bm5.replace("'","''")
                            _bm5df = pd.read_sql(
                                "SELECT il.level_date,"
                                " SUM(il.close_level*bc.weight) AS close_level"
                                " FROM pf.index_level il"
                                " JOIN pf.benchmark_component bc ON il.index_id=bc.index_id"
                                " JOIN pf.benchmark b ON bc.benchmark_id=b.benchmark_id"
                                f" WHERE b.benchmark_name='{_sbm5}'"
                                " GROUP BY il.level_date ORDER BY il.level_date", _dbc5)
                        except Exception:
                            pass
                    _dbc5.close()
                    if _ndf5 is not None and not _ndf5.empty:
                        _ndf5["nav_date"] = pd.to_datetime(_ndf5["nav_date"])
                        _ndf5 = _ndf5.sort_values("nav_date")
                        _last_nav5  = float(_ndf5["nav_value"].iloc[-1])
                        _last_date5 = _ndf5["nav_date"].iloc[-1]
                        _curr5 = (str(_ndf5["base_currency"].iloc[0])
                                  if "base_currency" in _ndf5.columns else "EUR")
                        _last_aum5 = (float(_ndf5["aum"].iloc[-1])
                                      if "aum" in _ndf5.columns else 0.0)
                        _ir = (_ndf5["inception_date"].iloc[0]
                               if "inception_date" in _ndf5.columns else None)
                        _incep5 = (pd.to_datetime(_ir)
                                   if _ir is not None and not pd.isna(_ir)
                                   else _ndf5["nav_date"].iloc[0])
                        _ndf5["growth_10k"] = (10000 * _ndf5["nav_value"]
                                               / _ndf5["nav_value"].iloc[0])
                        _ndf5["daily_ret"]  = _ndf5["nav_value"].pct_change()
                        if _bm5df is not None and not _bm5df.empty:
                            _bm5df["level_date"] = pd.to_datetime(_bm5df["level_date"])
                            _bm5df = _bm5df.sort_values("level_date")
                            _bm5df["g10k"] = (10000 * _bm5df["close_level"]
                                              / _bm5df["close_level"].iloc[0])
                        if _pos5 is not None and not _pos5.empty:
                            _total_mv5 = float(_pos5["market_value"].sum())
                except Exception as _de5:
                    st.warning(f"Données DB indisponibles : {_de5}")

        def _nav_at5(td):
            if _ndf5 is None: return None
            s = _ndf5[_ndf5["nav_date"] <= pd.Timestamp(td)]["nav_value"]
            return float(s.iloc[-1]) if not s.empty else None

        def _perf5(n_months=None, n_years=None, annualized=False):
            if _last_nav5 is None: return "—"
            if n_months:  tgt = _last_date5 - _rd5(months=n_months)
            elif n_years: tgt = _last_date5 - _rd5(years=n_years)
            else:         tgt = _incep5
            past = _nav_at5(tgt)
            if past is None or past == 0: return "—"
            r = _last_nav5 / past - 1
            if annualized:
                days = (_last_date5 - pd.Timestamp(tgt)).days
                yrs  = days / 365.25
                r = (1 + r) ** (1 / yrs) - 1 if yrs > 0.5 else r
            return f"{r*100:+.2f}%"

        def _ytd5():
            if _last_date5 is None: return "—"
            p = _nav_at5(pd.Timestamp(_last_date5.year, 1, 1))
            return "—" if p is None or p == 0 else f"{(_last_nav5/p-1)*100:+.2f}%"

        def _vol5(n=1):
            if _ndf5 is None or _last_date5 is None: return "—"
            s = _ndf5[_ndf5["nav_date"] >= _last_date5 - _rd5(years=n)]["daily_ret"].dropna()
            return "—" if s.empty else f"{s.std()*_np5.sqrt(252)*100:.2f}%"

        def _sharpe5(n=1):
            if _ndf5 is None or _last_date5 is None: return "—"
            s = _ndf5[_ndf5["nav_date"] >= _last_date5 - _rd5(years=n)]["daily_ret"].dropna()
            return ("—" if s.empty or s.std() == 0
                    else f"{(s.mean()*252)/(s.std()*_np5.sqrt(252)):.2f}")

        _cal5 = {}
        if _ndf5 is not None:
            for _yr5 in sorted(_ndf5["nav_date"].dt.year.unique()):
                _yd5 = _ndf5[_ndf5["nav_date"].dt.year == _yr5]
                if len(_yd5) >= 2:
                    _cal5[_yr5] = round(
                        (_yd5["nav_value"].iloc[-1] / _yd5["nav_value"].iloc[0] - 1) * 100, 1)

        # ── PDF Colors & styles ───────────────────────────────────────────────
        _CN5  = _rlc5.HexColor("#1A237E"); _CT5  = _rlc5.HexColor("#4A148C")
        _CTL5 = _rlc5.HexColor("#00ACC1")   # teal-cyan for label text (Natixis accent)
        _CW5  = _rlc5.white;               _CLG5 = _rlc5.HexColor("#F5F5F5")
        _CMG5 = _rlc5.HexColor("#9E9E9E"); _CLB5 = _rlc5.HexColor("#E3F2FD")
        _CGR5 = _rlc5.HexColor("#DDDDDD"); _CRD5 = _rlc5.HexColor("#C62828")
        _PW5  = A4[0]; _PH5 = A4[1]
        _MG5  = 1.5*cm; _UW5 = _PW5 - 2*_MG5
        _GAP5 = 0.3*cm; _HW5 = (_UW5 - _GAP5) / 2
        _LW5  = (_UW5 - _GAP5) * 0.65   # left  col (65 %)
        _RW5  = (_UW5 - _GAP5) * 0.35   # right col (35 %)
        _IH5  = _PH5 - 1.2*cm - 1.0*cm - 2*1.5*cm

        def _ps5(n, **kw):
            d = dict(fontName="Helvetica", textColor=_rlc5.HexColor("#424242"),
                     fontSize=7.5, leading=9.5)
            d.update(kw); return ParagraphStyle(n, **d)

        _Sh5  = _ps5("sh5",  fontName="Helvetica-Bold", textColor=_CW5, fontSize=7.5, leading=9)
        _Sb5  = _ps5("sb5",  fontSize=7.5, leading=10)
        _Sl5  = _ps5("sl5",  fontSize=6.5, textColor=_CMG5, leading=8.5)
        _Sv5  = _ps5("sv5",  fontSize=7.5, textColor=_CN5, fontName="Helvetica-Bold", leading=9.5)
        _Svs5 = _ps5("svs5", fontSize=7,   textColor=_CN5, fontName="Helvetica-Bold", leading=9)
        _Sth5 = _ps5("sth5", fontSize=7,   textColor=_CW5, fontName="Helvetica-Bold",
                     alignment=TA_CENTER, leading=9)
        _Stv5 = _ps5("stv5", fontSize=7,   textColor=_rlc5.HexColor("#212121"),
                     alignment=TA_RIGHT, leading=9)
        _Stl5 = _ps5("stl5", fontSize=7,   textColor=_rlc5.HexColor("#212121"), leading=9)
        _Sfn5 = _ps5("sfn5", fontSize=16,  fontName="Helvetica-Bold", textColor=_CN5, leading=20)
        _Sbg5 = _ps5("sbg5", fontSize=7.5, fontName="Helvetica-Bold", textColor=_CW5,
                     alignment=TA_CENTER, leading=9)
        _Sdc5 = _ps5("sdc5", fontSize=5.5, textColor=_CMG5,
                     fontName="Helvetica-Oblique", leading=7)
        _Sbl5 = _ps5("sbl5", fontSize=7.5, leading=10, leftIndent=6)

        def _sec5(txt, w, col=_CT5):
            t = Table([[Paragraph(f"  {txt}", _Sh5)]], colWidths=[w])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,-1), col),
                ("TOPPADDING",    (0,0), (-1,-1), 7),
                ("BOTTOMPADDING", (0,0), (-1,-1), 7),
                ("LEFTPADDING",   (0,0), (-1,-1), 6),
                ("RIGHTPADDING",  (0,0), (-1,-1), 4)]))
            return t

        def _col5(items, w):
            if not items: return Spacer(w, 1)
            t = Table([[it] for it in items], colWidths=[w])
            t.setStyle(TableStyle([
                ("TOPPADDING",    (0,0), (-1,-1), 0),
                ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                ("LEFTPADDING",   (0,0), (-1,-1), 0),
                ("RIGHTPADDING",  (0,0), (-1,-1), 0)]))
            return t

        def _std5(data, widths, hcol=_CT5):
            t = Table(data, colWidths=widths)
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0,0), (-1,0),  hcol),
                ("ROWBACKGROUNDS",(0,1), (-1,-1),  [_CW5, _CLG5]),
                ("TOPPADDING",    (0,0), (-1,-1),  2.5),
                ("BOTTOMPADDING", (0,0), (-1,-1),  2.5),
                ("LEFTPADDING",   (0,0), (-1,-1),  4),
                ("RIGHTPADDING",  (0,0), (-1,-1),  4),
                ("LINEBELOW",     (0,0), (-1,-1),  0.3, _CGR5),
                ("VALIGN",        (0,0), (-1,-1),  "MIDDLE"),
            ]))
            return t

        def _f2png5(fig, w, h, scale=2):
            buf = _io5.BytesIO()
            fig.write_image(buf, format="png", width=w, height=h, scale=scale)
            buf.seek(0); return buf

        def _cm2px5(pt): return int(pt / cm * 96)

        def _pt2px5(pt): return max(int(pt), 80)

        _CELL5 = {
            (1,1): (_UW5,        _IH5*0.86),
            (1,2): (_HW5,        _IH5*0.86),
            (2,1): (_UW5,        (_IH5-_GAP5)/2),
            (2,2): (_HW5,        (_IH5-_GAP5)/2),
        }

        _HDR_H = 2.1*cm   # header height
        _FTR_H = 0.9*cm   # footer height

        def _hdr5(cv, doc):
            cv.saveState()

            # ── HEADER BACKGROUND (full purple) ──────────────────────────────
            cv.setFillColor(_CT5)
            cv.rect(0, _PH5 - _HDR_H, _PW5, _HDR_H, fill=1, stroke=0)

            # ── TEAL BOTTOM ACCENT LINE ───────────────────────────────────────
            cv.setStrokeColor(_rlc5.HexColor("#00ACC1"))
            cv.setLineWidth(2.5)
            cv.line(0, _PH5 - _HDR_H, _PW5, _PH5 - _HDR_H)

            # ── LOGO ZONE (right) — teal stripe + text ───────────────────────
            _lbw = 3.2*cm; _lbh = _HDR_H - 0.25*cm
            _lbx = _PW5 - _MG5 - _lbw
            _lby = _PH5 - _HDR_H + 0.12*cm
            # Subtle darker purple backdrop for the logo area
            cv.setFillColor(_rlc5.HexColor("#38006b"))
            cv.roundRect(_lbx, _lby, _lbw, _lbh, 5, fill=1, stroke=0)
            # Teal accent stripe on left edge of logo
            cv.setFillColor(_rlc5.HexColor("#00ACC1"))
            cv.rect(_lbx, _lby, 0.18*cm, _lbh, fill=1, stroke=0)
            # Logo text
            cv.setFillColor(_CW5)
            cv.setFont("Helvetica-Bold", 10)
            cv.drawCentredString(_lbx + _lbw/2 + 0.09*cm,
                                 _lby + _lbh*0.60, "STAGE")
            cv.setFont("Helvetica", 7)
            cv.setFillColor(_rlc5.HexColor("#CE93D8"))
            cv.drawCentredString(_lbx + _lbw/2 + 0.09*cm,
                                 _lby + _lbh*0.28, "PORTFOLIO")

            # Thin white separator before logo
            cv.setStrokeColor(_rlc5.HexColor("#ffffff40"))
            cv.setLineWidth(0.5)
            cv.line(_lbx - 0.25*cm, _lby + 0.1*cm,
                    _lbx - 0.25*cm, _lby + _lbh - 0.1*cm)

            # ── FUND NAME (white bold) ────────────────────────────────────────
            cv.setFillColor(_CW5)
            cv.setFont("Helvetica-Bold", 13)
            cv.drawString(_MG5, _PH5 - 0.72*cm, _pf5 or "Factsheet")

            # ── SUBTITLE row ──────────────────────────────────────────────────
            cv.setFillColor(_rlc5.HexColor("#CE93D8"))
            cv.setFont("Helvetica-Bold", 7.5)
            cv.drawString(_MG5, _PH5 - 1.25*cm, "FUND FACTSHEET")
            cv.setFillColor(_rlc5.HexColor("#E1BEE7"))
            cv.setFont("Helvetica", 7.5)
            cv.drawString(_MG5 + 2.95*cm, _PH5 - 1.25*cm,
                          "·  MARKETING COMMUNICATION")

            # Date tag — teal pill badge
            _tag_w = 1.9*cm; _tag_h = 0.38*cm
            _tag_x = _lbx - 0.45*cm - _tag_w
            _tag_y = _PH5 - 1.42*cm
            cv.setFillColor(_rlc5.HexColor("#00ACC1"))
            cv.roundRect(_tag_x, _tag_y, _tag_w, _tag_h, 3, fill=1, stroke=0)
            cv.setFillColor(_CW5)
            cv.setFont("Helvetica-Bold", 7)
            cv.drawCentredString(_tag_x + _tag_w/2,
                                 _tag_y + 0.1*cm, _rd5v)

            # ── FOOTER ────────────────────────────────────────────────────────
            cv.setFillColor(_rlc5.HexColor("#F5F5F5"))
            cv.rect(0, 0, _PW5, _FTR_H, fill=1, stroke=0)
            cv.setStrokeColor(_CT5)
            cv.setLineWidth(1.5)
            cv.line(0, _FTR_H, _PW5, _FTR_H)
            cv.setFillColor(_CMG5)
            cv.setFont("Helvetica-Oblique", 5.5)
            cv.drawString(_MG5, 0.28*cm,
                "Les performances passées ne préjugent pas des résultats futurs. "
                "Document informatif. Source : STAGEPORTFOLIO.")
            cv.setFillColor(_CN5)
            cv.setFont("Helvetica-Bold", 7.5)
            cv.drawRightString(_PW5 - _MG5, 0.28*cm,
                               f"Page {doc.page}  ·  {_gdt5}")
            cv.restoreState()

        # ── Render source → flowable ──────────────────────────────────────────
        def _src_flow5(src_label, cw, ch):
            src_key = _SRC_KEY_MAP.get(src_label, "(vide)")
            if src_key == "(vide)": return Spacer(cw, ch)
            ch_px = _cm2px5(ch); cw_px = _cm2px5(cw)
            _CST = dict(plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
                        font=dict(color="#212121", family="Arial", size=8),
                        margin=dict(l=8, r=8, t=24, b=8),
                        xaxis=dict(gridcolor="#EEEEEE", linecolor="#BDBDBD", zeroline=False),
                        yaxis=dict(gridcolor="#EEEEEE", linecolor="#BDBDBD", zeroline=False),
                        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=7)))
            items = []

            if src_key == "auto:nav_growth":
                if _ndf5 is None or _ndf5.empty:
                    return Paragraph("Données NAV non disponibles", _Sb5)
                _fn = go.Figure()
                _fn.add_trace(go.Scatter(x=_ndf5["nav_date"], y=_ndf5["growth_10k"],
                    mode="lines", name=_pf5, line=dict(color="#1565C0", width=1.8)))
                if _bm5df is not None and not _bm5df.empty:
                    _fn.add_trace(go.Scatter(x=_bm5df["level_date"], y=_bm5df["g10k"],
                        mode="lines", name="Indice",
                        line=dict(color="#1A237E", width=1.2, dash="dot")))
                _fn.update_layout(
                    title=dict(
                        text=(f"Croissance de 10 000 {_curr5}"
                              f" (depuis {_incep5.strftime('%d/%m/%Y')})"),
                        font=dict(size=8, color="#1A237E"), x=0.01),
                    height=ch_px, **_CST)
                items.append(_RLImg5(_f2png5(_fn, cw_px, ch_px), width=cw, height=ch))

            elif src_key == "auto:cal_returns":
                if not _cal5: return Paragraph("Données non disponibles", _Sb5)
                _fc = go.Figure(go.Bar(
                    x=[str(y) for y in _cal5.keys()], y=list(_cal5.values()),
                    marker_color=["#1565C0" if v >= 0 else "#C62828" for v in _cal5.values()],
                    text=[f"{v:.1f}%" for v in _cal5.values()],
                    textposition="outside", textfont=dict(size=7.5)))
                _fc.update_layout(
                    title=dict(text="Rendements annuels (%)",
                               font=dict(size=8, color="#1A237E"), x=0.01),
                    height=ch_px, **_CST)
                items.append(_RLImg5(_f2png5(_fc, cw_px, ch_px), width=cw, height=ch))

            elif src_key == "auto:perf_table":
                _d = [[Paragraph(h, _Sth5) for h in ["Période","Fonds","Indice de réf."]]] + [
                    [Paragraph(r, _Stl5), Paragraph(v, _Stv5), Paragraph("—", _Stv5)]
                    for r, v in [
                        ("1 mois",        _perf5(n_months=1)),
                        ("3 mois",        _perf5(n_months=3)),
                        ("Début d'année", _ytd5()),
                        ("1 an",          _perf5(n_years=1)),
                        ("3 ans",         _perf5(n_years=3)),
                        ("5 ans",         _perf5(n_years=5)),
                        ("Depuis création",_perf5()),
                    ]]
                items.append(_std5(_d, [cw*0.44, cw*0.28, cw*0.28]))

            elif src_key == "auto:ann_table":
                _d = [[Paragraph(h, _Sth5) for h in ["Période","Fonds","Indice de réf."]]] + [
                    [Paragraph(r, _Stl5), Paragraph(v, _Stv5), Paragraph("—", _Stv5)]
                    for r, v in [
                        ("3 ans",          _perf5(n_years=3,  annualized=True)),
                        ("5 ans",          _perf5(n_years=5,  annualized=True)),
                        ("Depuis création", _perf5(annualized=True)),
                    ]]
                items.append(_std5(_d, [cw*0.44, cw*0.28, cw*0.28]))

            elif src_key == "auto:risk_table":
                _d = [[Paragraph(h, _Sth5) for h in
                       ["Indicateur","1 an","3 ans","5 ans","Depuis création"]]] + [
                    [Paragraph(r, _Stl5), Paragraph(v1, _Stv5), Paragraph(v3, _Stv5),
                     Paragraph(v5, _Stv5), Paragraph("—", _Stv5)]
                    for r, v1, v3, v5 in [
                        ("Écart-type (%)",    _vol5(1),    _vol5(3),    _vol5(5)),
                        ("Ratio de Sharpe",   _sharpe5(1), _sharpe5(3), _sharpe5(5)),
                        ("Tracking Error (%)", "—",        "—",         "—"),
                        ("Information Ratio",  "—",        "—",         "—"),
                        ("Alpha (%)",          "—",        "—",         "—"),
                        ("Bêta",               "—",        "—",         "—"),
                        ("R²",                 "—",        "—",         "—"),
                    ]]
                items.append(_std5(_d, [cw*0.40, cw*0.15, cw*0.15, cw*0.15, cw*0.15]))

            elif src_key == "auto:alloc_pie":
                if _total_mv5 <= 0: return Paragraph("Positions non disponibles", _Sb5)
                _ag = (_pos5.groupby("asset_class")["market_value"]
                       .sum().sort_values(ascending=False))
                _pie_colors = ["#EAB308","#38BDF8","#4ADE80","#F87171",
                               "#A78BFA","#FB923C","#34D399","#60A5FA"]
                _fp = go.Figure(go.Pie(
                    labels=_ag.index, values=_ag.values, hole=0.38,
                    textfont=dict(size=9, color="#1A1A1A"),
                    textinfo="percent+label",
                    hovertemplate="%{label}: %{percent}<extra></extra>",
                    marker=dict(colors=_pie_colors[:len(_ag)],
                                line=dict(color="#FFFFFF", width=2))))
                _fp.update_layout(
                    margin=dict(l=8, r=8, t=8, b=60),
                    height=_pt2px5(ch),
                    plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
                    font=dict(size=9, family="Arial, sans-serif"),
                    legend=dict(font=dict(size=9), bgcolor="rgba(0,0,0,0)",
                                orientation="h", yanchor="top",
                                y=-0.05, x=0.5, xanchor="center"))
                items.append(_RLImg5(
                    _f2png5(_fp, _pt2px5(cw), _pt2px5(ch), scale=1),
                    width=cw, height=ch))

            elif src_key == "auto:alloc_table":
                if _total_mv5 <= 0: return Paragraph("Positions non disponibles", _Sb5)
                _ag = (_pos5.groupby("asset_class")["market_value"]
                       .sum().sort_values(ascending=False))
                _d  = [[Paragraph(h, _Sth5) for h in ["Classe d'actifs","Fonds"]]]
                for _ac, _mv in _ag.items():
                    _d.append([Paragraph(str(_ac), _Stl5),
                                Paragraph(f"{_mv/_total_mv5*100:.1f}%", _Stv5)])
                _d.append([
                    Paragraph("Total", _ps5("tot5", fontSize=7,
                                            fontName="Helvetica-Bold", textColor=_CT5)),
                    Paragraph("100.0%", _ps5("totv5", fontSize=7,
                                             fontName="Helvetica-Bold",
                                             textColor=_CT5, alignment=TA_RIGHT))
                ])
                _t = Table(_d, colWidths=[cw*0.60, cw*0.40])
                _t.setStyle(TableStyle([
                    ("BACKGROUND", (0,0), (-1,0),  _CT5),
                    ("ROWBACKGROUNDS", (0,1), (-1,-2), [_CW5, _CLG5]),
                    ("BACKGROUND", (0,-1), (-1,-1), _CLB5),
                    ("FONTNAME",   (0,-1), (-1,-1), "Helvetica-Bold"),
                    ("TOPPADDING",    (0,0), (-1,-1), 2.5),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 2.5),
                    ("LEFTPADDING",   (0,0), (-1,-1), 4),
                    ("RIGHTPADDING",  (0,0), (-1,-1), 4),
                    ("LINEBELOW",     (0,0), (-1,-1), 0.3, _CGR5)]))
                items.append(_t)

            elif src_key == "auto:top10":
                if _total_mv5 <= 0: return Paragraph("Positions non disponibles", _Sb5)
                _t10 = _pos5.nlargest(10, "market_value").reset_index(drop=True)
                _d = [[Paragraph(h, _Sth5) for h in ["Instrument","Classe","Dev.","Poids"]]]
                for _, _r in _t10.iterrows():
                    _d.append([
                        Paragraph(str(_r["instrument_name"])[:22], _Stl5),
                        Paragraph(str(_r["asset_class"])[:10],     _Stl5),
                        Paragraph(str(_r["currency"]),             _Stl5),
                        Paragraph(f"{_r['market_value']/_total_mv5*100:.1f}%", _Stv5),
                    ])
                _d.append([
                    Paragraph(f"Nb de titres : {len(_pos5)}",
                              _ps5("nbs5", fontSize=6.5, textColor=_CMG5)),
                    Paragraph(""), Paragraph(""), Paragraph("")
                ])
                _t = Table(_d, colWidths=[cw*0.40, cw*0.22, cw*0.13, cw*0.25])
                _t.setStyle(TableStyle([
                    ("BACKGROUND",    (0,0), (-1,0),  _CT5),
                    ("ROWBACKGROUNDS",(0,1), (-1,-2), [_CW5, _CLG5]),
                    ("BACKGROUND",    (0,-1),(-1,-1), _CLG5),
                    ("SPAN",          (0,-1),(3,-1)),
                    ("TOPPADDING",    (0,0), (-1,-1), 2.5),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 2.5),
                    ("LEFTPADDING",   (0,0), (-1,-1), 4),
                    ("RIGHTPADDING",  (0,0), (-1,-1), 4),
                    ("LINEBELOW",     (0,0), (-1,-1), 0.3, _CGR5)]))
                items.append(_t)

            elif src_key == "auto:ccy":
                if _total_mv5 <= 0: return Paragraph("—", _Sb5)
                _ccy = (_pos5.groupby("currency")["market_value"]
                        .sum().sort_values(ascending=False))
                _d = [[Paragraph(h, _Sth5) for h in ["Devise","Fonds"]]]
                for _c5, _mv in _ccy.items():
                    _d.append([Paragraph(str(_c5), _Stl5),
                                Paragraph(f"{_mv/_total_mv5*100:.1f}%", _Stv5)])
                items.append(_std5(_d, [cw*0.55, cw*0.45]))

            elif src_key == "auto:fund_chars":
                _ch = [
                    ("Classification AMF", "FCP"),
                    ("Création", _incep5.strftime("%d/%m/%Y") if _incep5 else "—"),
                    ("Valorisation", "Quotidienne"),
                    ("Dépositaire", "CACEIS BANK"),
                    ("Devise", _curr5),
                    ("AuM", f"{_last_aum5/1e6:.1f}M {_curr5}" if _last_aum5 else "—"),
                    ("Durée rec.", "12 mois"),
                    ("Investisseur", "Retail"),
                ]
                _Slc5 = _ps5("slc5", fontSize=6.5,
                             textColor=_rlc5.HexColor("#00ACC1"), leading=8.5)
                _t = Table([[Paragraph(k, _Slc5), Paragraph(v, _Svs5)] for k, v in _ch],
                           colWidths=[cw*0.53, cw*0.47])
                _t.setStyle(TableStyle([
                    ("ROWBACKGROUNDS", (0,0), (-1,-1), [_CW5, _CLG5]),
                    ("TOPPADDING",    (0,0), (-1,-1), 3),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 3),
                    ("LEFTPADDING",   (0,0), (-1,-1), 4),
                    ("RIGHTPADDING",  (0,0), (-1,-1), 4),
                    ("LINEBELOW",     (0,0), (-1,-1), 0.3, _CGR5)]))
                items.append(_t)

            elif src_key == "auto:risk_profile":
                _cells = []; _ts = []
                for _ri in range(1, 8):
                    _sel = (_ri == _risk5)
                    _cells.append(Paragraph(
                        f"<b>{_ri}</b>" if _sel else str(_ri),
                        _ps5(f"rp5_{_ri}", fontSize=8, alignment=TA_CENTER,
                             textColor=_CW5 if _sel else _CN5,
                             fontName="Helvetica-Bold" if _sel else "Helvetica")))
                    _ts.append(("BACKGROUND", (_ri-1,0), (_ri-1,0),
                                 _CT5 if _sel else _CLB5))
                _rt = Table([_cells], colWidths=[cw/7]*7)
                _rt.setStyle(TableStyle([*_ts,
                    ("TOPPADDING",    (0,0), (-1,-1), 6),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 6),
                    ("LINEAFTER",     (0,0), (-2,-1), 0.5, _CW5)]))
                items.append(_rt)
                _rll = Table([[
                    Paragraph("Risque plus faible",
                              _ps5("rll5", fontSize=6, textColor=_CMG5, leading=7.5)),
                    Paragraph("Risque plus élevé",
                              _ps5("rlr5", fontSize=6, textColor=_CMG5,
                                   alignment=TA_RIGHT, leading=7.5)),
                ]], colWidths=[cw/2, cw/2])
                _rll.setStyle(TableStyle([
                    ("TOPPADDING",    (0,0), (-1,-1), 1),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 1),
                    ("LEFTPADDING",   (0,0), (-1,-1), 2),
                    ("RIGHTPADDING",  (0,0), (-1,-1), 2)]))
                items.append(_rll)
                items.append(Paragraph(
                    f"Catégorie {_risk5} — fondé sur données historiques.", _Sdc5))

            elif src_key == "auto:invest_obj":
                if _obj5:
                    items.append(Paragraph(_obj5, _Sb5))
                    items.append(Spacer(1, 0.08*cm))
                for _hl in [x.strip() for x in _hl5.split("\n") if x.strip()]:
                    items.append(Paragraph(f"• {_hl}", _Sbl5))

            elif src_key == "auto:fees":
                _fd = [
                    ("Frais courants",    _fo5),
                    ("Frais d'entrée max", _fsv5),
                    ("Frais de rachat max", _fr5),
                    ("Investissement min.", "5 000 EUR"),
                ]
                items.append(_std5(
                    [[Paragraph(k, _Sl5), Paragraph(v, _Svs5)] for k, v in _fd],
                    [cw*0.65, cw*0.35], hcol=_CT5))

            elif src_key == "auto:mgmt":
                _md = [
                    ("Société de gestion", _mc5),
                    ("Gestionnaire",       _im5),
                    ("Dépositaire",        "CACEIS BANK"),
                ]
                items.append(_std5(
                    [[Paragraph(k, _Sl5), Paragraph(v, _Svs5)] for k, v in _md],
                    [cw*0.42, cw*0.58], hcol=_CT5))

            elif src_key.startswith("custom:"):
                _cbid5 = int(src_key.split(":")[1])
                _ck5   = str(_cbid5)
                _cdf5  = st.session_state.get(f"block_{_ck5}_df")
                if _cdf5 is None: return Paragraph("Graphique non exécuté", _Sb5)
                _ct_l = st.session_state.get(f"ctype_{_ck5}", list(CHART_TYPES.keys())[0])
                _ct5  = CHART_TYPES.get(_ct_l, "line")
                _xc5  = st.session_state.get(f"xcol_{_ck5}", _cdf5.columns[0])
                _yc5  = st.session_state.get(f"ycol_{_ck5}", _cdf5.columns[-1])
                _cc5  = st.session_state.get(f"ccol_{_ck5}", "(aucun)")
                _cc5  = None if _cc5 == "(aucun)" else _cc5
                _pc5  = PALETTES.get(st.session_state.get(f"pal_{_ck5}", "Défaut"),
                                     PALETTES["Défaut"])
                _mk5  = st.session_state.get(f"mk_{_ck5}", True)
                _sv5  = st.session_state.get(f"sv_{_ck5}", False)
                _ly5  = st.session_state.get(f"ly_{_ck5}", False)
                _nmc5 = _cdf5.select_dtypes(include="number").columns.tolist()
                _alc5 = _cdf5.columns.tolist()
                _cf5  = None
                if _ct5 == "line":
                    _cf5 = px.line(_cdf5, x=_xc5, y=_yc5, color=_cc5,
                                   color_discrete_sequence=_pc5, markers=_mk5)
                elif _ct5 in ("bar", "bar_stacked"):
                    _cf5 = px.bar(_cdf5, x=_xc5, y=_yc5, color=_cc5,
                                  color_discrete_sequence=_pc5,
                                  barmode="stack" if _ct5=="bar_stacked" else "group",
                                  text_auto=".2s" if _sv5 else False)
                elif _ct5 == "pie":
                    _vc5 = st.session_state.get(f"pval_{_ck5}",
                                                 _nmc5[0] if _nmc5 else _alc5[-1])
                    _cf5 = px.pie(_cdf5, names=_xc5, values=_vc5,
                                  color_discrete_sequence=_pc5, hole=0.4)
                elif _ct5 == "scatter":
                    _cf5 = px.scatter(_cdf5, x=_xc5, y=_yc5, color=_cc5,
                                      color_discrete_sequence=_pc5)
                elif _ct5 == "histogram":
                    _cf5 = px.histogram(_cdf5, x=_xc5, color=_cc5,
                                        color_discrete_sequence=_pc5)
                elif _ct5 == "area":
                    _cf5 = px.area(_cdf5, x=_xc5, y=_yc5, color=_cc5,
                                   color_discrete_sequence=_pc5)
                elif _ct5 == "heatmap":
                    _zc5 = st.session_state.get(f"hz_{_ck5}",
                                                 _nmc5[0] if _nmc5 else _alc5[-1])
                    _yh5 = st.session_state.get(f"hmy_{_ck5}", _alc5[0])
                    try:
                        _pv5 = _cdf5.pivot(index=_yh5, columns=_xc5, values=_zc5)
                        _cf5 = go.Figure(go.Heatmap(
                            z=_pv5.values.tolist(),
                            x=[str(cc) for cc in _pv5.columns],
                            y=[str(rr) for rr in _pv5.index],
                            colorscale="RdYlGn", zmid=0))
                    except Exception:
                        pass
                if _cf5 is None: return Paragraph("Erreur graphique", _Sb5)
                _ttl5 = st.session_state.get(f"ctitle_{_ck5}", f"Graphique {_cbid5+1}")
                _cf5.update_layout(
                    title=dict(text=_ttl5, font=dict(size=10, color="#1A237E"), x=0.01),
                    plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
                    font=dict(color="#212121", family="Arial", size=8),
                    height=ch_px, margin=dict(l=8, r=8, t=30, b=8),
                    legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=7.5)),
                    xaxis=dict(gridcolor="#AAAAAA", gridwidth=1.5,
                               linecolor="#888888", zeroline=False),
                    yaxis=dict(gridcolor="#AAAAAA", gridwidth=1.5,
                               linecolor="#888888", zeroline=False,
                               type="log" if _ly5 else "linear"))
                try:
                    items.append(_RLImg5(_f2png5(_cf5, cw_px, ch_px), width=cw, height=ch))
                except Exception as _ie5:
                    return Paragraph(f"Erreur image : {_ie5}", _Sb5)

            if not items: return Spacer(cw, ch)
            _disp = src_label.split(" ", 1)[1] if " " in src_label else src_label
            box_rows = ([[_sec5(_disp[:45], cw)], [Spacer(1, 0.12*cm)]]
                        + [[it] for it in items]
                        + [[Spacer(1, 0.12*cm)]])
            box = Table(box_rows, colWidths=[cw])
            box.setStyle(TableStyle([
                ("TOPPADDING",    (0,0), (-1,-1), 0),
                ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                ("LEFTPADDING",   (0,0), (-1,-1), 0),
                ("RIGHTPADDING",  (0,0), (-1,-1), 0)]))
            return box

        # ── Construire le story ───────────────────────────────────────────────
        _buf5  = _io5.BytesIO()
        _doc5  = SimpleDocTemplate(_buf5, pagesize=A4,
                                   leftMargin=_MG5, rightMargin=_MG5,
                                   topMargin=2.3*cm, bottomMargin=1.3*cm)
        _story5 = []
        _cnt5   = [0]

        # ── Cover band ────────────────────────────────────────────────────────
        _bdg5 = Table([[Paragraph("FUND FACTSHEET  ·  MARKETING COMMUNICATION (1)", _Sbg5)]],
                      colWidths=[_UW5])
        _bdg5.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), _CT5),
            ("TOPPADDING",    (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3)]))
        _story5.append(_bdg5)
        _story5.append(Spacer(1, 0.15*cm))
        _story5.append(Paragraph(_pf5 or "Rapport d'Analyse", _Sfn5))
        _story5.append(Spacer(1, 0.08*cm))
        _sc5_row = Table([[
            Paragraph("SHARE CLASS", _Sl5), Paragraph(_sc5, _Sv5),
            Paragraph("ISIN", _Sl5),        Paragraph(_isin5 or "—", _Sv5),
            Paragraph("Bloomberg", _Sl5),   Paragraph(_bb5 or "—", _Sv5),
            Paragraph("", _Sl5),            Paragraph(_rd5v, _Sv5),
        ]], colWidths=[1.7*cm, 2.8*cm, 0.9*cm, 3.5*cm, 1.7*cm, 2.7*cm, 0.4*cm, 2.6*cm])
        _sc5_row.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), _CLB5),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 5),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE")]))
        _story5.append(_sc5_row)
        _story5.append(Spacer(1, 0.2*cm))

        # ══════════════════════════════════════════════════════════════════════
        # MODE AUTO : mise en page fixe style Natixis (2 pages)
        # ══════════════════════════════════════════════════════════════════════
        if _nfs_mode == "auto":

            _VG5 = 0.45*cm   # visible gap between left and right column
            _RW5adj = _RW5 - (_VG5 - _GAP5)   # keep total width constant

            # Max usable height per column (page height - margins - header - footer)
            _MAX_COL_H = (_PH5 - 2.3*cm - 1.3*cm - 1.2*cm - 1.0*cm - 0.6*cm)

            def _2col5(left_fl, right_fl):
                _lkif = KeepInFrame(
                    _LW5, _MAX_COL_H,
                    [_col5(left_fl, _LW5)],
                    mode="shrink")
                _rkif = KeepInFrame(
                    _RW5adj, _MAX_COL_H,
                    [_col5(right_fl, _RW5adj)],
                    mode="shrink")
                _tw = Table([[_lkif, Spacer(_VG5, 1), _rkif]],
                            colWidths=[_LW5, _VG5, _RW5adj])
                _tw.setStyle(TableStyle([
                    ("VALIGN",        (0,0), (-1,-1), "TOP"),
                    ("TOPPADDING",    (0,0), (-1,-1), 0),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                    ("LEFTPADDING",   (0,0), (-1,-1), 0),
                    ("RIGHTPADDING",  (0,0), (-1,-1), 0)]))
                return _tw

            # Chart styles (fallback auto charts) — 1px=1pt rendering
            _cstA = dict(
                plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
                font=dict(color="#222222", family="Arial, sans-serif", size=9),
                margin=dict(l=52, r=14, t=14, b=90),
                xaxis=dict(gridcolor="#AAAAAA", gridwidth=1.5,
                           linecolor="#888888", linewidth=1.0,
                           zeroline=False, tickfont=dict(size=9)),
                yaxis=dict(gridcolor="#AAAAAA", gridwidth=1.5,
                           linecolor="#888888", linewidth=1.0,
                           zeroline=False, tickfont=dict(size=9)),
                legend=dict(bgcolor="rgba(255,255,255,0.9)",
                            bordercolor="#CCCCCC", borderwidth=0.5,
                            font=dict(size=9, color="#333333"),
                            orientation="h", yanchor="top", y=-0.24, x=0))
            _SHd5  = _ps5("shd5",  fontSize=10, fontName="Helvetica-Bold",
                           textColor=_CT5, leading=13)
            _SChH5 = _ps5("schh5", fontSize=9, fontName="Helvetica-Bold",
                           textColor=_CT5, leading=12)
            _SDis5 = _ps5("sdis5", fontSize=5.5, textColor=_CMG5,
                           fontName="Helvetica-Oblique", leading=7)
            # Heights calculated after block count — replaced below
            _chA = 8.5*cm   # fallback auto charts
            _chS = 7.0*cm

            # ── PAGE 1 — Left column (65 %) ───────────────────────────────────
            _L1 = []

            # Fund highlights heading (no colored box — plain bold text like Natixis)
            _L1.append(Paragraph("Fund highlights", _SHd5))
            _L1.append(Spacer(1, 0.1*cm))
            if _obj5:
                _L1.append(Paragraph(_obj5, _Sb5))
                _L1.append(Spacer(1, 0.08*cm))
            for _hl in [x.strip() for x in _hl5.split("\n") if x.strip()]:
                _L1.append(Paragraph(f"• {_hl}", _Sbl5))
            _L1.append(Spacer(1, 0.1*cm))
            _L1.append(Paragraph(
                "LES DONNÉES DE PERFORMANCE REPRÉSENTENT LES PERFORMANCES "
                "PASSÉES ET NE GARANTISSENT PAS LES PERFORMANCES FUTURES.",
                _SDis5))
            _L1.append(Spacer(1, 0.18*cm))

            # ── Render user's SQL blocks as charts (same as Streamlit) ──────
            def _blk2img5(bid, col_w, col_h):
                _bdf = st.session_state.get(f"block_{bid}_df")
                if _bdf is None or _bdf.empty:
                    return None, None
                _ct_l = st.session_state.get(f"ctype_{bid}",
                                              list(CHART_TYPES.keys())[0])
                _ct   = CHART_TYPES.get(_ct_l, "line")
                _xc   = st.session_state.get(f"xcol_{bid}", _bdf.columns[0])
                _yc   = st.session_state.get(f"ycol_{bid}", _bdf.columns[-1])
                _cc   = st.session_state.get(f"ccol_{bid}", "(aucun)")
                _cc   = None if _cc == "(aucun)" else _cc
                _pc   = PALETTES.get(st.session_state.get(f"pal_{bid}", "Défaut"),
                                     PALETTES["Défaut"])
                _mk   = st.session_state.get(f"mk_{bid}", True)
                _sv   = st.session_state.get(f"sv_{bid}", False)
                _ly   = st.session_state.get(f"ly_{bid}", False)
                _ttl  = st.session_state.get(f"ctitle_{bid}",
                                              f"Graphique {bid+1}")
                _nmc  = _bdf.select_dtypes(include="number").columns.tolist()
                _fig  = None
                if _ct == "line":
                    _fig = px.line(_bdf, x=_xc, y=_yc, color=_cc,
                                   color_discrete_sequence=_pc, markers=_mk)
                elif _ct in ("bar", "bar_stacked"):
                    _fig = px.bar(_bdf, x=_xc, y=_yc, color=_cc,
                                  color_discrete_sequence=_pc,
                                  barmode="stack" if _ct == "bar_stacked"
                                  else "group",
                                  text_auto=".2s" if _sv else False)
                elif _ct == "pie":
                    _vc = st.session_state.get(
                        f"pval_{bid}", _nmc[0] if _nmc else _bdf.columns[-1])
                    _fig = px.pie(_bdf, names=_xc, values=_vc,
                                  color_discrete_sequence=_pc, hole=0.4)
                elif _ct == "scatter":
                    _fig = px.scatter(_bdf, x=_xc, y=_yc, color=_cc,
                                      color_discrete_sequence=_pc)
                elif _ct == "area":
                    _fig = px.area(_bdf, x=_xc, y=_yc, color=_cc,
                                   color_discrete_sequence=_pc)
                elif _ct == "histogram":
                    _fig = px.histogram(_bdf, x=_xc, color=_cc,
                                        color_discrete_sequence=_pc)
                if _fig is None:
                    return None, None
                if _ct in ("line", "area"):
                    _fig.update_traces(line=dict(width=2))
                elif _ct in ("bar", "bar_stacked"):
                    _fig.update_traces(textfont=dict(size=9))
                _fig.update_layout(
                    height=_pt2px5(col_h),
                    plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
                    font=dict(color="#222222", family="Arial, sans-serif",
                              size=9),
                    margin=dict(l=52, r=14, t=14, b=90),
                    xaxis=dict(
                        gridcolor="#AAAAAA", gridwidth=1.5,
                        linecolor="#888888", linewidth=1.0,
                        zeroline=False,
                        tickfont=dict(size=9),
                        title_font=dict(size=9)),
                    yaxis=dict(
                        gridcolor="#AAAAAA", gridwidth=1.5,
                        linecolor="#888888", linewidth=1.0,
                        zeroline=False,
                        tickfont=dict(size=9),
                        title_font=dict(size=9),
                        type="log" if _ly else "linear"),
                    legend=dict(
                        bgcolor="rgba(255,255,255,0.9)",
                        bordercolor="#CCCCCC", borderwidth=0.5,
                        font=dict(size=9, color="#333333"),
                        orientation="h", yanchor="top",
                        y=-0.24, x=0))
                try:
                    _img = _RLImg5(
                        _f2png5(_fig, _pt2px5(col_w), _pt2px5(col_h),
                                scale=1),
                        width=col_w, height=col_h)
                    return _ttl, _img
                except Exception:
                    return None, None

            # Collect active block IDs
            _all_bids = [i for i in range(30)
                         if st.session_state.get(f"block_{i}_df") is not None
                         and not st.session_state.get(f"block_{i}_df",
                             pd.DataFrame()).empty]
            _pie_bids = [b for b in _all_bids
                         if CHART_TYPES.get(
                             st.session_state.get(f"ctype_{b}", ""),
                             "line") == "pie"]
            _nonpie_bids = [b for b in _all_bids if b not in _pie_bids]

            # Dynamic chart height — fills available space after text (~5cm estimate)
            _nb_np = len(_nonpie_bids)
            _txt_est = 5.0*cm   # estimated text height (highlights + spacers)
            _avail   = _MAX_COL_H - _txt_est
            if   _nb_np == 0: _ch_blk = _chA
            elif _nb_np == 1: _ch_blk = min(_avail - 0.5*cm, 14.0*cm)
            elif _nb_np == 2: _ch_blk = min((_avail - 0.8*cm) / 2, 10.0*cm)
            else:             _ch_blk = min((_avail - 1.0*cm) / _nb_np, 8.0*cm)

            # Non-pie blocks → left column page 1
            if _nonpie_bids:
                for _bid in _nonpie_bids:
                    _ttl_b, _img_b = _blk2img5(_bid, _LW5, _ch_blk)
                    if _img_b is not None:
                        _L1.append(Paragraph(f"<b>{_ttl_b}</b>", _SChH5))
                        _L1.append(Spacer(1, 0.06*cm))
                        _L1.append(_img_b)
                        _L1.append(Spacer(1, 0.2*cm))
            else:
                # Fallback: auto NAV growth chart
                if _ndf5 is not None and not _ndf5.empty:
                    _L1.append(Paragraph(
                        f"<b>Croissance illustrative de 10 000 {_curr5}</b>"
                        f"  (du {_incep5.strftime('%d/%m/%Y')} au"
                        f" {_last_date5.strftime('%d/%m/%Y')})",
                        _SChH5))
                    _L1.append(Spacer(1, 0.05*cm))
                    _fn = go.Figure()
                    _fn.add_trace(go.Scatter(
                        x=_ndf5["nav_date"], y=_ndf5["growth_10k"],
                        mode="lines", name=_pf5,
                        line=dict(color="#4A148C", width=1.8)))
                    if _bm5df is not None and not _bm5df.empty:
                        _fn.add_trace(go.Scatter(
                            x=_bm5df["level_date"], y=_bm5df["g10k"],
                            mode="lines", name="Indice de référence",
                            line=dict(color="#00ACC1", width=1.2)))
                    _fn.update_layout(height=_pt2px5(_chA), **_cstA)
                    _fn.update_traces(line=dict(width=2))
                    _L1.append(_RLImg5(
                        _f2png5(_fn, _pt2px5(_LW5), _pt2px5(_chA), scale=1),
                        width=_LW5, height=_chA))
                    _L1.append(Spacer(1, 0.15*cm))
                    if _cal5:
                        _cal_x = [str(int(y)) for y in _cal5.keys()]
                        _cal_y = list(_cal5.values())
                        _L1.append(Paragraph("Rendements annuels (%)", _SChH5))
                        _L1.append(Spacer(1, 0.05*cm))
                        _fc = go.Figure(go.Bar(
                            x=_cal_x, y=_cal_y,
                            marker_color=["#4A148C" if v >= 0 else "#C62828"
                                          for v in _cal_y],
                            text=[f"{v:+.1f}%" for v in _cal_y],
                            textposition="outside",
                            textfont=dict(size=7, color="#424242")))
                        _fc.update_layout(
                            height=_pt2px5(_chS),
                            plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
                            font=dict(color="#333333", family="Arial", size=9),
                            margin=dict(l=48, r=14, t=24, b=80),
                            xaxis=dict(type="category",
                                       tickfont=dict(size=9),
                                       gridcolor="#AAAAAA",
                                       gridwidth=1.5,
                                       linecolor="#888888"),
                            yaxis=dict(gridcolor="#AAAAAA",
                                       gridwidth=1.5,
                                       linecolor="#888888",
                                       zeroline=True,
                                       zerolinecolor="#888888",
                                       ticksuffix="%",
                                       tickfont=dict(size=9)),
                            legend=dict(bgcolor="rgba(255,255,255,0.9)",
                                        bordercolor="#CCCCCC",
                                        borderwidth=0.5,
                                        font=dict(size=9),
                                        orientation="h",
                                        yanchor="top", y=-0.22, x=0))
                        _L1.append(_RLImg5(
                            _f2png5(_fc, _pt2px5(_LW5), _pt2px5(_chS), scale=1),
                            width=_LW5, height=_chS))

            # ── PAGE 1 — Right column (35 %) ──────────────────────────────────
            _R1 = []

            # ABOUT THE FUND section (Natixis style — colored box + teal headings)
            _R1.append(_sec5("ABOUT THE FUND", _RW5adj))
            _Slc5 = _ps5("slc5r", fontSize=6.5,
                          textColor=_rlc5.HexColor("#00ACC1"), leading=8.5,
                          fontName="Helvetica-Bold")
            if _obj5:
                _R1.append(Paragraph("Investment objective", _Slc5))
                _R1.append(Spacer(1, 0.03*cm))
                _R1.append(Paragraph(_obj5, _Sb5))
            if _bm5 and _bm5 != "(aucun)":
                _R1.append(Spacer(1, 0.06*cm))
                _R1.append(Paragraph("Indice de référence", _Slc5))
                _R1.append(Paragraph(_bm5, _Sb5))
            _R1.append(Spacer(1, _GAP5))

            _R1.append(_src_flow5("🎚️ Profil de risque",          _RW5adj, 2.0*cm))
            _R1.append(Spacer(1, _GAP5))
            _R1.append(_src_flow5("📋 Caractéristiques du fonds",  _RW5adj, 2.0*cm))
            _R1.append(Spacer(1, _GAP5))
            _R1.append(_src_flow5("📋 Frais",                      _RW5adj, 2.0*cm))
            _R1.append(Spacer(1, _GAP5))
            _R1.append(_src_flow5("📋 Management",                 _RW5adj, 2.0*cm))

            _story5.append(_2col5(_L1, _R1))
            _cnt5[0] += 1
            _story5.append(PageBreak())

            # ── PAGE 2 — Performance tables full width, then 2-col portfolio ──
            _story5.append(_src_flow5("📋 Performances totales (%)",   _UW5, 2.5*cm))
            _story5.append(Spacer(1, _GAP5))
            _story5.append(_src_flow5("📋 Performance annualisée (%)", _UW5, 2.0*cm))
            _story5.append(Spacer(1, _GAP5))
            _story5.append(_src_flow5("📋 Mesures de risque",          _UW5, 2.5*cm))
            _story5.append(Spacer(1, _GAP5))

            # Pie chart height — taller for readability
            _ch_pie = 12.0*cm if len(_pie_bids) <= 1 else 10.0*cm

            # Pie section: user's pie blocks first, then alloc table
            _L2 = []
            if _pie_bids:
                for _pb in _pie_bids:
                    _ttl_p, _img_p = _blk2img5(_pb, _LW5, _ch_pie)
                    if _img_p is not None:
                        _L2.append(_sec5(_ttl_p[:40] if _ttl_p else
                                         "Répartition", _LW5))
                        _L2.append(Spacer(1, 0.1*cm))
                        _L2.append(_img_p)
                        _L2.append(Spacer(1, 0.4*cm))
            else:
                _L2.append(_src_flow5("🥧 Répartition d'actifs (camembert)",
                                       _LW5, _ch_pie))
                _L2.append(Spacer(1, 0.4*cm))
            _L2.append(HRFlowable(width=_LW5, thickness=0.5,
                                   color=_rlc5.HexColor("#BDBDBD")))
            _L2.append(Spacer(1, 0.25*cm))
            _L2.append(_src_flow5("📋 Répartition d'actifs (tableau)",
                                   _LW5, 2.0*cm))
            _R2 = [
                _src_flow5("📋 Top 10 positions",       _RW5adj, 2.5*cm),
                Spacer(1, 0.4*cm),
                HRFlowable(width=_RW5adj, thickness=0.5,
                            color=_rlc5.HexColor("#BDBDBD")),
                Spacer(1, 0.25*cm),
                _src_flow5("📋 Répartition par devise", _RW5adj, 2.0*cm),
            ]
            _story5.append(_2col5(_L2, _R2))
            _cnt5[0] += 1

        # ══════════════════════════════════════════════════════════════════════
        # MODE PERSONNALISÉ : mise en page choisie par l'utilisateur
        # ══════════════════════════════════════════════════════════════════════
        else:
            _nb5 = len(st.session_state["nfs_pages"])
            for _fpi5, _fpg5 in enumerate(st.session_state["nfs_pages"]):
                _lay5   = _fpg5["layout"]
                _slots5 = _fpg5["slots"]
                _li5    = NFS_LAYOUTS.get(_lay5, {"slots":1, "grid":(1,1)})
                _gr5    = _li5["grid"]
                _nr5, _nc5 = _gr5
                _cw5, _ch5 = _CELL5.get(_gr5, (_UW5, _IH5*0.80))

                def _sf5(lbl, cw_in, ch_in):
                    try:
                        _f = _src_flow5(lbl, cw_in, ch_in)
                        if _f is not None and not isinstance(_f, Spacer):
                            _cnt5[0] += 1
                        return _f if _f is not None else Spacer(cw_in, ch_in)
                    except Exception as _se:
                        return Paragraph(f"Erreur : {_se}", _Sb5)

                _slts5 = [_sf5(_slots5[_i] if _i < len(_slots5) else "(vide)", _cw5, _ch5)
                          for _i in range(_li5["slots"])]

                if _nr5 == 1 and _nc5 == 1:
                    _story5.append(_slts5[0])
                elif _nr5 == 1 and _nc5 == 2:
                    _tw5 = Table(
                        [[_slts5[0], _slts5[1] if len(_slts5)>1 else Spacer(_HW5, _ch5)]],
                        colWidths=[_HW5, _HW5])
                    _tw5.setStyle(TableStyle([
                        ("VALIGN",        (0,0), (-1,-1), "TOP"),
                        ("TOPPADDING",    (0,0), (-1,-1), 0),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                        ("LEFTPADDING",   (0,0), (-1,-1), 0),
                        ("RIGHTPADDING",  (0,0), (0,-1),  _GAP5),
                        ("RIGHTPADDING",  (1,0), (1,-1),  0)]))
                    _story5.append(_tw5)
                elif _nr5 == 2 and _nc5 == 1:
                    _story5.append(_slts5[0])
                    _story5.append(Spacer(1, _GAP5))
                    _story5.append(_slts5[1] if len(_slts5)>1 else Spacer(_UW5, _ch5))
                elif _nr5 == 2 and _nc5 == 2:
                    _r5 = [_slts5[_i] if _i < len(_slts5) else Spacer(_HW5, _ch5)
                           for _i in range(4)]
                    _tw5 = Table([[_r5[0], _r5[1]], [_r5[2], _r5[3]]],
                                 colWidths=[_HW5, _HW5])
                    _tw5.setStyle(TableStyle([
                        ("VALIGN",        (0,0), (-1,-1), "TOP"),
                        ("TOPPADDING",    (0,0), (-1,-1), 0),
                        ("BOTTOMPADDING", (0,0), (-1,0),  _GAP5),
                        ("BOTTOMPADDING", (0,1), (-1,1),  0),
                        ("LEFTPADDING",   (0,0), (-1,-1), 0),
                        ("RIGHTPADDING",  (0,0), (0,-1),  _GAP5),
                        ("RIGHTPADDING",  (1,0), (1,-1),  0)]))
                    _story5.append(_tw5)

                if _fpi5 < _nb5 - 1:
                    _story5.append(PageBreak())

            if _cnt5[0] == 0:
                _story5.append(Spacer(1, 1.5*cm))
                _story5.append(Paragraph(
                    "Aucun contenu généré — vérifiez la mise en page.",
                    _ps5("ww5", fontSize=11, textColor=_CRD5,
                         fontName="Helvetica-Bold", alignment=TA_CENTER)))

        # ── Pied de page disclaimer ───────────────────────────────────────────
        _story5.append(Spacer(1, 0.15*cm))
        _story5.append(HRFlowable(width=_UW5, thickness=0.5,
                                  color=_rlc5.HexColor("#BDBDBD")))
        _story5.append(Spacer(1, 0.08*cm))
        _story5.append(Paragraph(
            "(1) Document à caractère informatif. Les performances passées ne préjugent "
            "pas des performances futures. Veuillez vous référer au prospectus du "
            "fonds avant toute décision d'investissement.", _Sdc5))

        try:
            with st.spinner("Compilation du factsheet Natixis…"):
                _doc5.build(_story5, onFirstPage=_hdr5, onLaterPages=_hdr5)
            _buf5.seek(0)
            _n5 = (f"factsheet_{(_pf5 or 'rapport').replace(' ','_')}"
                   f"_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf")
            st.session_state["_nfs_pdf_bytes"] = _buf5.read()
            st.session_state["_nfs_pdf_name"]  = _n5
            st.session_state["_nfs_pdf_count"] = _cnt5[0]
        except Exception as _e5:
            st.error(f"Erreur PDF : {_e5}")
            import traceback; st.code(traceback.format_exc())
            st.session_state.pop("_nfs_pdf_bytes", None)

    if "_nfs_pdf_bytes" in st.session_state:
        st.success(
            f"✅ Factsheet prêt — "
            f"{st.session_state.get('_nfs_pdf_count', 0)} contenu(s) intégré(s)")
        st.download_button(
            "⬇️ Télécharger le Factsheet Natixis PDF",
            data=st.session_state["_nfs_pdf_bytes"],
            file_name=st.session_state.get("_nfs_pdf_name", "factsheet.pdf"),
            mime="application/pdf", use_container_width=True, key="dl_nfs5",
        )
