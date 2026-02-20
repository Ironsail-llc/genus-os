#!/usr/bin/env python3
"""
One-time CRM cleanup script.

Deletes junk records, merges duplicates, fixes data quality issues.
Creates a JSON backup before any modifications. Idempotent — safe to run twice.

Usage:
    python crm/cleanup_crm.py              # dry-run (default)
    python crm/cleanup_crm.py --execute    # actually apply changes
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add bridge source directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent / "bridge"))
import crm_dal

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ─── Junk People to Delete ────────────────────────────────────────────

JUNK_PEOPLE_IDS = [
    # Furniture / objects
    # (UUIDs from live DB — if any don't exist, skip gracefully)
]

# We'll find junk by name pattern instead — more resilient to UUID changes
JUNK_PEOPLE_NAMES = {
    ("couch", ""),
    ("chair", ""),
    ("vision monitor", "system"),
    ("robothor vision", "monitor"),
    ("robothor", "system"),
    ("chatwoot inbox", "monitor"),
    ("chatwoot", "monitor"),
    ("email", "responder"),
    ("human", "resources"),
    ("gemini", "(google workspace)"),
    ("gemini", "notes"),
    ("google", "meet"),
    ("linkedin", "(automated)"),
    ("linkedin", "(noreply)"),
    ("linkedin", ""),
    ("gitguardian", ""),
    ("openrouter", "team"),
    ("claude", ""),
    ("claude", "team"),
    (".exec", ""),
    ("robothor", "vision monitor"),
}

# ─── Junk Companies to Delete ─────────────────────────────────────────

JUNK_COMPANY_NAMES = {
    "null",
    "robothor system",
    "chatwoot monitor",
    "google workspace",
    "google",
    "linkedin",
    "getgitguardian",
    "openrouter",
    "themerrimack",
}

# ─── Person Merge Clusters ─────────────────────────────────────────────
# Format: (keeper_id, [loser_ids], {extra_emails}, {extra_phones})

PERSON_MERGE_CLUSTERS = [
    # Philip D'Agostino — keeper has CEO info + philip@ironsail.ai + Ironsail LLC company
    ("f1767f50-dc84-4638-9745-675558f52a29", [
        "23cbf1d1-ac22-4b19-88a6-bc9df91e4396",  # philip@ironsail.ai, no company
        "123081f3-5917-48d1-a46a-96358a92929d",  # bare record
        "ca8dc8f4-a795-4c1c-9492-8b0da7db2cdf",  # linked to Thrive
        "0fe7d0a7-bb96-4be4-aace-268a6bce3b59",  # email="philip d'agostino" (name), Thrive
        "9a17ff70-a675-4760-b36a-ac166e102431",  # Thrive
        "313b854c-c552-44b8-91e2-512a273902ca",  # bare
        "726b355f-cfa9-4f7a-9844-f950c2376b3c",  # Thrive
        "4a04ee87-8359-494c-9645-923cb1bc5a7e",  # bare
    ], set(), set()),

    # Samantha D'Agostino — keeper has CCO + samantha@ironsailpharma.com
    ("5eb21ee6-e0fc-4a36-9249-8bcf9fcdae1d", [
        "58316851-bde1-4305-9f79-f61269c31196",  # email="samantha d'agostino"
        "29f41a76-0e15-4b90-91a1-fd2b20100098",  # Thrive Rx
        "9a60c46a-1080-4904-b384-e81b422b6fd2",  # Thrive
    ], set(), set()),

    # Adan Cruz — keeper has adan@ironsail.ai + Ironsail Pharma
    ("af1829a2-3ea2-4a59-9bbc-7e76a1b14d5a", [
        "58b06af5-49ed-4354-bc02-91a06c40eefd",  # email="adan cruz"
        "8151f5cc-523d-46e3-943f-5cc6d0bc5799",  # bare
    ], set(), set()),

    # Phil Krupenya — keeper has phil@getdrx.com + DRx Software
    ("c76a375b-0048-4abe-b7d9-c99efcf7ae9d", [
        "653215cf-c373-48e4-9377-e7d767643a6b",  # DRx Pharmacy Tech
        "6abec43f-bb9f-4e00-b2ee-bad93f9e7d5c",  # Philip Krupenya (bare)
    ], set(), set()),

    # Jennifer — keeper has jennifer@valhallams.com + Valhallams
    ("37d61a9b-e691-4f45-9106-561cad26925c", [
        "1858d31e-f313-4ab9-b49b-0120ced238c1",  # Jennifer Guerrero (bare)
        "b76bae6e-e714-4a93-9ca4-acad24580f28",  # jennifer@ironsail.ai + Ironsail
    ], {"jennifer@ironsail.ai"}, set()),

    # Eugene Ugolkov — keeper has eugene@ironsail.ai + Ironsail Pharma
    ("779610cf-b3a7-48da-8f16-92b8168a7acb", [
        "d336b5c3-c266-4fc9-a500-65cc25e39b4f",  # eugene@webugol.com + Webugol
    ], {"eugene@webugol.com"}, set()),

    # Craig Nicholson — keeper has craig@ironsail.ai + Ironsail Pharma
    ("04b4b51b-04e1-497b-b310-26f617cacd78", [
        "3555525c-ecc6-470b-a25a-f05f56efb46e",  # bare, Ironsail
    ], set(), set()),

    # Daniel McCarthy — keeper has daniel@valhallams.com + Valhalla MSO
    ("5ba220d5-79fe-4c12-97cd-b469f9b5c7d5", [
        "56986741-efb6-4f64-b973-c944606c1784",  # email="daniel mccarthy"
    ], set(), set()),

    # Dawn D'Agostino — keeper has ddago4@aol.com
    ("2496a20e-320d-4de6-a3e8-08041341b45b", [
        "a08b97dc-f449-42f4-8da8-3d538128ad8d",  # bare
    ], set(), set()),

    # Illia/Ilia Tutko
    ("88454c64-aa74-4ef8-8d6a-a3bdc7bb31f5", [
        "aa46fbfc-768c-4519-9e87-c123af5e10db",  # "Ilia" spelling
    ], set(), set()),

    # Joshua Quijano — keeper has joshua@ironsail.ai + Ironsail Pharma
    ("e91bbe19-5f6a-4d20-a460-6cbc811e47a5", [
        "422b96dd-bb3a-4e21-bfc7-69ac7283c9f6",  # email="joshua quijano"
    ], set(), set()),

    # Paul Edward Borja — keeper has pauledward.borja@gmail.com
    ("4ceb4df0-8ccc-4832-a43f-348c98065f1b", [
        "16c80e55-096e-4305-8a89-48f74f9303f9",  # "Paul Edward Borja" split differently
    ], set(), set()),

    # Rochelle Blaza — keeper has rochelle@ironsail.ai + Ironsail Pharma
    ("88060c1d-d9b4-4cf7-bc50-01ded7ba92b5", [
        "e8d18344-67c1-4239-8993-c32ec307c5b8",  # bare, Ironsail
    ], set(), set()),

    # Jhon Ray Angcon — keeper is the one with last_name="Ray Angcon"
    ("c007f203-0c70-4a8f-9519-3c6d7e038594", [
        "fdc1f5c0-2f95-4f8f-9b0b-004d4bffcfbb",  # "Jhon Ray" (no Angcon)
        "386bac6e-45ed-4672-b0f9-e413f1514fc1",  # "Angcon Danylo" (names reversed)
    ], set(), set()),

    # Danylo Boiko — keeper has company link
    ("b4449b74-7114-4dfa-b77b-35888dadef9a", [
        "b8420f61-3b4c-4116-8bea-2a3d54e6f5b6",  # "Danilo" (typo)
    ], set(), set()),

    # Konrad Arciszewski — keeper has konrad@valhallams.com + Valhallams
    ("241d8a6d-2fc0-45ba-a7c8-3a53fd501f60", [
        "5d65142b-840e-466e-b45c-2f794a639316",  # konrad@valhallavitality.com + Valhalla Vitality
    ], {"konrad@valhallavitality.com"}, set()),
]

# ─── Company Merge Clusters ───────────────────────────────────────────
# Format: (keeper_id, loser_id, keeper_updates)

COMPANY_MERGE_CLUSTERS = [
    # Ironsail (has domain ironsail.ai) + Ironsail LLC
    ("889e0da3-400b-4374-a80a-5526836e1a4d",
     "953a2fa9-5859-4d85-bafe-8b54664acae4",
     {}),

    # Thrive + Thrive Rx
    ("935f9bff-4ddd-4f47-9cdb-860c8bad8b04",
     "1aae39c3-d5a3-413b-9aca-6d7168c3f4dd",
     {}),

    # Valhallavitality (has domain) → rename to "Valhalla Vitality" + merge Valhalla Vitality
    ("17bf9e7d-fe19-4cc6-a5c0-2a5c4c53cde7",
     "78be4ff9-8d70-4d54-b920-a3e4eff1796d",
     {"name": "Valhalla Vitality"}),

    # Valhalla MSO + Valhallams (has domain valhallams.com)
    ("270cac10-5670-4fb2-802a-1ab74e2155fd",
     "d230e01f-1816-4133-aaa6-8a4d83fb95e4",
     {"domain_name": "valhallams.com"}),

    # DRx Pharmacy Tech + DRx Software, LLC → rename to "DRx"
    ("04d0bae6-9908-4592-a236-e0abe8b9b99d",
     "b74b6762-fe1f-4c01-aced-39c56a9f9ab8",
     {"name": "DRx"}),
]

# ─── Philip D'Agostino company delete ─────────────────────────────────
# His personal "company" created by auto-discovery
PHILIP_COMPANY_DELETE_NAMES = {"philip d'agostino", "philip d'agostino's company"}


def _backup_data(conn) -> dict:
    """Create a JSON-serializable backup of all CRM data."""
    from psycopg2.extras import RealDictCursor

    backup = {}
    cur = conn.cursor(cursor_factory=RealDictCursor)

    for table in ("crm_people", "crm_companies", "crm_notes", "crm_tasks",
                  "contact_identifiers"):
        cur.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        backup[table] = []
        for row in rows:
            clean = {}
            for k, v in dict(row).items():
                if hasattr(v, "isoformat"):
                    clean[k] = v.isoformat()
                elif isinstance(v, (dict, list)):
                    clean[k] = v
                else:
                    clean[k] = str(v) if v is not None else None
            backup[table].append(clean)

    return backup


def _find_junk_people(conn) -> list[str]:
    """Find junk people by matching against JUNK_PEOPLE_NAMES."""
    from psycopg2.extras import RealDictCursor

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, first_name, last_name FROM crm_people WHERE deleted_at IS NULL")
    rows = cur.fetchall()

    junk_ids = []
    for row in rows:
        fn = (row.get("first_name") or "").strip().lower()
        ln = (row.get("last_name") or "").strip().lower()
        full = f"{fn} {ln}".strip()

        # Check against name set
        if (fn, ln) in JUNK_PEOPLE_NAMES:
            junk_ids.append(str(row["id"]))
            continue

        # Check first name only entries
        if (fn, "") in JUNK_PEOPLE_NAMES:
            junk_ids.append(str(row["id"]))
            continue

        # Also check against "Philip D'Agostino (via Google Docs)" pattern
        if "via google docs" in full:
            junk_ids.append(str(row["id"]))

    return junk_ids


def _find_junk_companies(conn) -> list[str]:
    """Find junk companies by name match."""
    from psycopg2.extras import RealDictCursor

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, name FROM crm_companies WHERE deleted_at IS NULL")
    rows = cur.fetchall()

    junk_ids = []
    for row in rows:
        name = (row.get("name") or "").strip().lower()
        if name in JUNK_COMPANY_NAMES or name in PHILIP_COMPANY_DELETE_NAMES:
            junk_ids.append(str(row["id"]))

    return junk_ids


def _find_merge_losers(conn, keeper_id: str, first_name_pattern: str) -> list[str]:
    """Find duplicate people for a keeper by matching first name, excluding keeper."""
    from psycopg2.extras import RealDictCursor

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id FROM crm_people
        WHERE deleted_at IS NULL
          AND first_name ILIKE %s
          AND id != %s
    """, (f"%{first_name_pattern}%", keeper_id))
    return [str(r["id"]) for r in cur.fetchall()]


def _fix_data_quality(conn, dry_run: bool) -> int:
    """Fix null strings and bad email fields. Returns count of fixes."""
    from psycopg2.extras import RealDictCursor

    cur = conn.cursor(cursor_factory=RealDictCursor)
    fixes = 0

    # Fix literal "null" in city and job_title
    for field in ("city", "job_title"):
        cur.execute(f"""
            SELECT id, {field} FROM crm_people
            WHERE deleted_at IS NULL
              AND lower(trim({field})) IN ('null', 'none', 'n/a')
        """)
        rows = cur.fetchall()
        for row in rows:
            if not dry_run:
                cur.execute(
                    f"UPDATE crm_people SET {field} = '', updated_at = NOW() WHERE id = %s",
                    (row["id"],),
                )
            fixes += 1
            logger.info("  Fix %s.%s: '%s' → '' (id: %s)",
                        "crm_people", field, row[field], row["id"])

    # Fix email fields that contain names instead of emails (no @)
    cur.execute("""
        SELECT id, email FROM crm_people
        WHERE deleted_at IS NULL
          AND email IS NOT NULL
          AND email != ''
          AND email NOT LIKE '%%@%%'
    """)
    rows = cur.fetchall()
    for row in rows:
        if not dry_run:
            cur.execute(
                "UPDATE crm_people SET email = NULL, updated_at = NOW() WHERE id = %s",
                (row["id"],),
            )
        fixes += 1
        logger.info("  Fix email-as-name: '%s' → NULL (id: %s)", row["email"], row["id"])

    if not dry_run:
        conn.commit()

    return fixes


def run_cleanup(dry_run: bool = True):
    """Execute the full cleanup. dry_run=True only reports what would happen."""
    import psycopg2
    conn = psycopg2.connect(crm_dal.config.PG_DSN)

    # ─── Backup ───────────────────────────────────────────────────────
    backup_path = Path(__file__).parent / "cleanup_backup.json"
    if not backup_path.exists():
        logger.info("Creating backup at %s ...", backup_path)
        backup = _backup_data(conn)
        backup["timestamp"] = datetime.now(timezone.utc).isoformat()
        with open(backup_path, "w") as f:
            json.dump(backup, f, indent=2, default=str)
        logger.info("Backup complete: %d people, %d companies",
                     len(backup.get("crm_people", [])),
                     len(backup.get("crm_companies", [])))
    else:
        logger.info("Backup already exists at %s, skipping", backup_path)

    # ─── Pre-counts ───────────────────────────────────────────────────
    from psycopg2.extras import RealDictCursor
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT COUNT(*) AS c FROM crm_people WHERE deleted_at IS NULL")
    pre_people = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM crm_companies WHERE deleted_at IS NULL")
    pre_companies = cur.fetchone()["c"]
    logger.info("Pre-cleanup: %d people, %d companies", pre_people, pre_companies)

    mode = "DRY RUN" if dry_run else "EXECUTING"
    logger.info("=== %s ===", mode)

    # ─── 1a. Delete junk people ───────────────────────────────────────
    junk_people = _find_junk_people(conn)
    logger.info("Step 1a: Found %d junk people to delete", len(junk_people))
    deleted_people = 0
    for pid in junk_people:
        if not dry_run:
            crm_dal.delete_person(pid)
        deleted_people += 1
        logger.info("  Delete person: %s", pid)

    # ─── 1b. Delete junk companies ────────────────────────────────────
    junk_companies = _find_junk_companies(conn)
    logger.info("Step 1b: Found %d junk companies to delete", len(junk_companies))
    deleted_companies = 0
    for cid in junk_companies:
        if not dry_run:
            crm_dal.delete_company(cid)
        deleted_companies += 1
        logger.info("  Delete company: %s", cid)

    # ─── 1c. Merge person clusters ────────────────────────────────────
    merged_people = 0
    logger.info("Step 1c: Merging person clusters")
    for keeper_id, explicit_losers, extra_emails, extra_phones in PERSON_MERGE_CLUSTERS:
        # Check if keeper exists
        keeper = crm_dal.get_person(keeper_id)
        if not keeper:
            logger.warning("  Keeper %s not found, skipping cluster", keeper_id)
            continue

        keeper_name = f"{keeper['name']['firstName']} {keeper['name']['lastName']}".strip()

        # Use explicit losers if provided, otherwise find by name
        losers = explicit_losers
        if not losers:
            first_name = keeper["name"]["firstName"]
            losers = _find_merge_losers(conn, keeper_id, first_name)

        for loser_id in losers:
            if not dry_run:
                result = crm_dal.merge_people(keeper_id, loser_id)
                if result:
                    merged_people += 1
                    logger.info("  Merged %s into %s (%s)", loser_id, keeper_id, keeper_name)
                else:
                    logger.warning("  Failed to merge %s into %s", loser_id, keeper_id)
            else:
                merged_people += 1
                logger.info("  Would merge %s into %s (%s)", loser_id, keeper_id, keeper_name)

        # Add extra emails/phones to keeper
        if extra_emails and not dry_run:
            existing = keeper.get("additionalEmails") or []
            new_emails = [e for e in extra_emails if e not in existing]
            if new_emails:
                crm_dal.update_person(keeper_id, additional_emails=existing + new_emails)
                logger.info("  Added extra emails to %s: %s", keeper_name, new_emails)

        if extra_phones and not dry_run:
            existing = keeper.get("additionalPhones") or []
            new_phones = [p for p in extra_phones if p not in existing]
            if new_phones:
                crm_dal.update_person(keeper_id, additional_phones=existing + new_phones)

    # ─── 1d. Merge company clusters ───────────────────────────────────
    merged_companies = 0
    logger.info("Step 1d: Merging company clusters")
    for keeper_id, loser_id, keeper_updates in COMPANY_MERGE_CLUSTERS:
        keeper = crm_dal.get_company(keeper_id)
        if not keeper:
            logger.warning("  Company keeper %s not found, skipping", keeper_id)
            continue

        if not dry_run:
            # Apply name/domain updates first
            if keeper_updates:
                crm_dal.update_company(keeper_id, **keeper_updates)

            result = crm_dal.merge_companies(keeper_id, loser_id)
            if result:
                merged_companies += 1
                logger.info("  Merged company %s into %s (%s)",
                            loser_id, keeper_id, keeper.get("name", ""))
            else:
                logger.warning("  Failed to merge company %s into %s", loser_id, keeper_id)
        else:
            merged_companies += 1
            logger.info("  Would merge company %s into %s (%s)",
                        loser_id, keeper_id, keeper.get("name", ""))

    # ─── 1e. Data quality fixes ───────────────────────────────────────
    logger.info("Step 1e: Data quality fixes")
    quality_fixes = _fix_data_quality(conn, dry_run)
    logger.info("  %d data quality fixes", quality_fixes)

    # ─── Post-counts ──────────────────────────────────────────────────
    if not dry_run:
        cur.execute("SELECT COUNT(*) AS c FROM crm_people WHERE deleted_at IS NULL")
        post_people = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM crm_companies WHERE deleted_at IS NULL")
        post_companies = cur.fetchone()["c"]
        logger.info("Post-cleanup: %d people (-%d), %d companies (-%d)",
                     post_people, pre_people - post_people,
                     post_companies, pre_companies - post_companies)
    else:
        logger.info("DRY RUN complete. Would delete %d people, %d companies; "
                     "merge %d people, %d companies; fix %d data quality issues",
                     deleted_people, deleted_companies,
                     merged_people, merged_companies, quality_fixes)

    conn.close()

    return {
        "deleted_people": deleted_people,
        "deleted_companies": deleted_companies,
        "merged_people": merged_people,
        "merged_companies": merged_companies,
        "quality_fixes": quality_fixes,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CRM Cleanup")
    parser.add_argument("--execute", action="store_true",
                        help="Actually apply changes (default is dry-run)")
    args = parser.parse_args()

    result = run_cleanup(dry_run=not args.execute)
    print(json.dumps(result, indent=2))
