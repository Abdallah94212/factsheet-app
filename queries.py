EXPORT_ETIQUETTES = """
SELECT
    e.codeetiquette,
    SUM(tv.prixvente * dc.quantitelivree)  AS total_article_livres,
    SUM(tv.prixvente * dc.quantitecommandee) AS total_article_commandes
FROM dbo.commande AS c
INNER JOIN dbo.detailcommande AS dc
    ON c.numcommande = dc.numcommande
INNER JOIN dbo.client AS z
    ON c.numclient = z.numclient
INNER JOIN dbo.etiquette AS e
    ON z.codeetiquette = e.codeetiquette
INNER JOIN dbo.tarifvente AS tv
    ON tv.codeliste = z.codeliste
GROUP BY e.codeetiquette
ORDER BY e.codeetiquette;
"""

# =============================================================================
# REQUETES - GESTION DE PORTEFEUILLES FINANCIERS
# =============================================================================

QUERIES: dict[str, dict] = {

    # ── Niveau 1 : SELECT simples ─────────────────────────────────────────
    "Q1 — Tous les portefeuilles": {
        "description": "Code, nom, devise et profil de risque de chaque portefeuille",
        "sql": """SELECT
    portfolio_code,
    portfolio_name,
    base_currency,
    risk_profile
FROM pf.portfolio;""",
    },

    "Q2 — Instruments Equity": {
        "description": "Tous les instruments de classe Equity",
        "sql": """SELECT
    instrument_id,
    isin,
    instrument_name,
    asset_class,
    currency,
    country
FROM pf.instrument
WHERE asset_class = 'Equity';""",
    },

    "Q3 — Transactions janvier 2024": {
        "description": "Toutes les transactions du mois de janvier 2024",
        "sql": """SELECT
    transaction_id,
    trade_date,
    settlement_date,
    side,
    quantity,
    price,
    fees,
    currency,
    portfolio_id,
    instrument_id
FROM pf.[transaction]
WHERE trade_date >= '2024-01-01'
  AND trade_date <  '2024-02-01';""",
    },

    "Q4 — Positions au 2024-02-28": {
        "description": "Snapshot des positions à la date du 2024-02-28",
        "sql": """SELECT
    position_id,
    position_date,
    quantity,
    market_value,
    portfolio_id,
    instrument_id
FROM pf.position
WHERE position_date = '2024-02-28';""",
    },

    # ── Niveau 2 : Jointures ──────────────────────────────────────────────
    "Q5 — Transactions + portefeuille + instrument": {
        "description": "Transactions enrichies avec le nom du portefeuille et de l'instrument",
        "sql": """SELECT
    t.transaction_id,
    t.trade_date,
    t.side,
    t.quantity,
    t.price,
    t.fees,
    t.currency,
    p.portfolio_name,
    i.instrument_name
FROM pf.[transaction] t
JOIN pf.portfolio  p ON t.portfolio_id  = p.portfolio_id
JOIN pf.instrument i ON t.instrument_id = i.instrument_id;""",
    },

    "Q6 — Positions + portefeuille + instrument": {
        "description": "Instruments détenus par portefeuille au 2024-02-28",
        "sql": """SELECT
    p.portfolio_name,
    i.instrument_name,
    pos.quantity,
    pos.market_value
FROM pf.position pos
JOIN pf.portfolio  p ON pos.portfolio_id  = p.portfolio_id
JOIN pf.instrument i ON pos.instrument_id = i.instrument_id
WHERE pos.position_date = '2024-02-28';""",
    },

    "Q7 — NAV au 2024-02-28": {
        "description": "Valeur liquidative par portefeuille au 2024-02-28",
        "sql": """SELECT
    p.portfolio_name,
    n.nav_date,
    n.nav_value,
    n.aum,
    n.currency
FROM pf.nav n
JOIN pf.portfolio p ON n.portfolio_id = p.portfolio_id
WHERE n.nav_date = '2024-02-28';""",
    },

    "Q8 — Positions + pays + devise instrument": {
        "description": "Positions avec pays et devise de l'instrument au 2024-02-28",
        "sql": """SELECT
    pos.position_id,
    pos.position_date,
    pos.quantity,
    pos.market_value,
    i.instrument_name,
    i.country,
    i.currency AS instrument_currency
FROM pf.position   pos
JOIN pf.instrument i ON pos.instrument_id = i.instrument_id
WHERE pos.position_date = '2024-02-28';""",
    },

    # ── Niveau 3 : Agrégations ────────────────────────────────────────────
    "Q9 — Nombre de transactions par portefeuille": {
        "description": "Comptage des transactions par portefeuille",
        "sql": """SELECT
    p.portfolio_name,
    COUNT(t.transaction_id) AS nb_transactions
FROM pf.[transaction] t
JOIN pf.portfolio p ON t.portfolio_id = p.portfolio_id
GROUP BY p.portfolio_id, p.portfolio_name;""",
    },

    "Q10 — Montant brut total par portefeuille": {
        "description": "Somme des (quantity × price) par portefeuille",
        "sql": """SELECT
    p.portfolio_name,
    SUM(t.quantity * t.price) AS montant_brut_total
FROM pf.[transaction] t
JOIN pf.portfolio p ON t.portfolio_id = p.portfolio_id
GROUP BY p.portfolio_id, p.portfolio_name;""",
    },

    "Q11 — Valeur totale des positions par portefeuille": {
        "description": "Somme des market_value par portefeuille au 2024-02-28",
        "sql": """SELECT
    p.portfolio_name,
    SUM(pos.market_value) AS valeur_totale_positions
FROM pf.position pos
JOIN pf.portfolio p ON pos.portfolio_id = p.portfolio_id
WHERE pos.position_date = '2024-02-28'
GROUP BY p.portfolio_id, p.portfolio_name;""",
    },

    "Q12 — Top 3 instruments par market_value": {
        "description": "Les 3 instruments avec la plus grande valeur de marché cumulée",
        "sql": """SELECT TOP 3
    i.instrument_name,
    SUM(pos.market_value) AS total_market_value
FROM pf.position   pos
JOIN pf.instrument i ON pos.instrument_id = i.instrument_id
GROUP BY i.instrument_id, i.instrument_name
ORDER BY total_market_value DESC;""",
    },

    # ── Niveau 4 : Indices et benchmarks ─────────────────────────────────
    "Q13 — Benchmark actif par portefeuille": {
        "description": "Benchmark actuellement actif (end_date IS NULL) de chaque portefeuille",
        "sql": """SELECT
    p.portfolio_name,
    b.benchmark_name,
    b.benchmark_type,
    pb.start_date
FROM pf.portfolio_benchmark pb
JOIN pf.portfolio p ON pb.portfolio_id = p.portfolio_id
JOIN pf.benchmark b ON pb.benchmark_id = b.benchmark_id
WHERE pb.end_date IS NULL;""",
    },

    "Q14 — Composition du benchmark BM_BAL": {
        "description": "Indices et poids composant le benchmark BM_BAL",
        "sql": """SELECT
    ir.index_name,
    bc.weight
FROM pf.benchmark_component bc
JOIN pf.index_ref ir ON bc.index_id     = ir.index_id
JOIN pf.benchmark b  ON bc.benchmark_id = b.benchmark_id
WHERE b.benchmark_code = 'BM_BAL';""",
    },

    "Q15 — Indices + close_level par portefeuille": {
        "description": "Indices du benchmark de chaque portefeuille avec close_level au 2024-02-28",
        "sql": """SELECT
    p.portfolio_name,
    b.benchmark_name,
    ir.index_name,
    bc.weight,
    il.close_level
FROM pf.portfolio_benchmark pb
JOIN pf.portfolio           p   ON pb.portfolio_id  = p.portfolio_id
JOIN pf.benchmark           b   ON pb.benchmark_id  = b.benchmark_id
JOIN pf.benchmark_component bc  ON b.benchmark_id   = bc.benchmark_id
JOIN pf.index_ref           ir  ON bc.index_id      = ir.index_id
JOIN pf.index_level         il  ON ir.index_id      = il.index_id
                                AND il.level_date   = '2024-02-28'
WHERE pb.end_date IS NULL;""",
    },

    # ── Niveau 5 : Requêtes réalistes ────────────────────────────────────
    "Q16 — Portefeuilles avec position en USD": {
        "description": "Portefeuilles détenant au moins un instrument en USD",
        "sql": """SELECT DISTINCT
    p.portfolio_name
FROM pf.position   pos
JOIN pf.portfolio  p ON pos.portfolio_id  = p.portfolio_id
JOIN pf.instrument i ON pos.instrument_id = i.instrument_id
WHERE i.currency = 'USD';""",
    },

    "Q17 — Obligations à maturité 2029-2032": {
        "description": "Obligations avec une maturité entre 2029 et 2032",
        "sql": """SELECT
    instrument_name,
    isin,
    maturity_date,
    coupon_rate
FROM pf.instrument
WHERE asset_class   = 'Bond'
  AND maturity_date BETWEEN '2029-01-01' AND '2032-12-31';""",
    },

    "Q18 — Portefeuilles avec NAV > 100 au 2024-02-28": {
        "description": "Portefeuilles dont la NAV dépasse 100 au 2024-02-28",
        "sql": """SELECT
    p.portfolio_name,
    n.nav_date,
    n.nav_value,
    n.currency
FROM pf.nav       n
JOIN pf.portfolio p ON n.portfolio_id = p.portfolio_id
WHERE n.nav_date  = '2024-02-28'
  AND n.nav_value > 100;""",
    },
}
