#!/usr/bin/env python3
"""
Seed complex patient scenarios into Impetus One for Helm integration testing.
Uses the REST API and direct SQL for status transitions that aren't exposed via API.

Scenarios:
1. Maria Gonzalez — Chronic pain patient with multiple controlled substance Rx, drug interactions
2. David Park — Post-surgical follow-up, pharmacy transmission failures, urgent queue
3. Catherine Shaw — Complex polypharmacy elderly patient, multiple providers, refill coordination
4. Marcus Johnson — Weight management program, GLP-1 therapy, e-commerce orders
5. Priya Patel — New patient intake, video consultations, mental health + endocrine

Run: python3 scripts/seed-impetus-scenarios.py
"""

import json
import subprocess
from datetime import datetime, timedelta

TOKEN = "09748acdcd02a9ea7216d42a8be9efa0eeb794450982b2f3b31e682ec4bebca9"
BASE = "http://localhost:8000"
HEADERS = ["-H", f"Authorization: Bearer {TOKEN}", "-H", "Content-Type: application/json"]

# Reference IDs from existing data
CLINICS = {
    "downtown": "019c7318-e8b3-7078-8bf5-e5c7c4d0e6bf",
    "westside": "019c7318-e8b3-7078-8bf5-e5c7c5a8f887",
}
PROVIDERS = {
    "sarah_johnson": "019c7318-e8b3-7078-8bf5-e5c7c653cfb4",
    "michael_chen": "019c7318-e8b3-7078-8bf5-e5c7c763dc49",
    "emily_martinez": "019c7318-e8b3-7078-8bf5-e5c7c7fec158",
    "james_wilson": "019c7319-12fd-73f0-8b4e-5636f3bb712f",
    "lisa_cuddy": "019c7319-146e-702b-b7de-baf2778dba14",
}
MEDICATIONS = {
    "lisinopril_10": "019c7318-e89a-7268-9619-e25d878f6f00",
    "lisinopril_20": "019c7318-e89a-7268-9619-e25d88623ce7",
    "metformin_500": "019c7318-e89a-7268-9619-e25d889903f3",
    "metformin_1000": "019c7318-e89a-7268-9619-e25d8923857f",
    "atorvastatin_10": "019c7318-e89a-7268-9619-e25d8a04d14c",
    "atorvastatin_20": "019c7318-e89a-7268-9619-e25d8a44caa2",
    "amlodipine_5": "019c7318-e89a-7268-9619-e25d8b26b1bd",
    "omeprazole_20": "019c7318-e89a-7268-9619-e25d8c17b36f",
    "levothyroxine_50": "019c7318-e89a-7268-9619-e25d8ceec554",
    "hydrocodone": "019c7318-e89a-7268-9619-e25d8dcf9186",
    "alprazolam": "019c7318-e89a-7268-9619-e25d8e0245a2",
    "gabapentin_300": "019c7318-e89a-7268-9619-e25d8efccbaa",
    "sertraline_50": "019c7318-e89a-7268-9619-e25d8fe4b910",
    "losartan_50": "019c7318-e89a-7268-9619-e25d9058d140",
    "prednisone_10": "019c7318-e89a-7268-9619-e25d90d100f6",
    "testosterone": "019c7318-eba2-719f-ba59-222f7de2a14f",
    "semaglutide": "019c7318-eba2-719f-ba59-222f7de5140a",
    "tirzepatide": "019c7318-eba2-719f-ba59-222f7e6314fa",
    "sildenafil": "019c7318-eba2-719f-ba59-222f8001781d",
}
PHARMACIES = {
    "medisource": "019c7318-e89a-7268-9619-e25d8675ccb4",
    "carefirst": "019c7318-e89a-7268-9619-e25d86a40d62",
    "horizon": "019c7318-e89a-7268-9619-e25d870b1ddb",
    "scriptsure": "019c7318-eba1-731d-955b-035e786ff84d",
    "rxvortex": "019c7318-eba1-731d-955b-035e78fa3bd2",
    "dispensepro": "019c7318-eba1-731d-955b-035e79f74dd3",
}


def api_post(path, data):
    """POST to IO API, return parsed JSON."""
    cmd = ["curl", "-s", "-X", "POST", f"{BASE}{path}"] + HEADERS + ["-d", json.dumps(data)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        resp = json.loads(result.stdout)
        if "@id" in resp:
            entity_id = resp["@id"].split("/")[-1]
            print(f"  Created {path} → {entity_id}")
            return entity_id
        elif "id" in resp:
            print(f"  Created {path} → {resp['id']}")
            return resp["id"]
        else:
            print(f"  POST {path} response: {result.stdout[:200]}")
            return None
    except json.JSONDecodeError:
        print(f"  ERROR {path}: {result.stdout[:200]}")
        return None


def sql(query):
    """Execute SQL in IO postgres container."""
    cmd = [
        "sudo",
        "docker",
        "exec",
        "impetus_one_postgres",
        "psql",
        "-U",
        "impetus",
        "-d",
        "impetus_rx",
        "-c",
        query,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  SQL ERROR: {result.stderr[:200]}")
    return result.stdout


def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")


def days_ago(n):
    return (datetime.utcnow() - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def days_ahead(n):
    return (datetime.utcnow() + timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def hours_ahead(n):
    return (datetime.utcnow() + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ──────────────────────────────────────────────────────────────────────
# SCENARIO 1: Maria Gonzalez — Chronic Pain Management
# Complex: controlled substances, multiple Rx states, drug interaction flags
# ──────────────────────────────────────────────────────────────────────

print("\n═══ SCENARIO 1: Maria Gonzalez — Chronic Pain Management ═══")
maria = api_post(
    "/api/patients",
    {
        "firstName": "Maria",
        "lastName": "Gonzalez",
        "dob": "1972-08-14",
        "gender": "female",
        "email": "maria.gonzalez@email.com",
        "phone": "415-555-2001",
        "addressLine1": "1420 Mission Street",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94103",
        "allergies": "Codeine, Sulfa drugs",
        "medicalConditions": "Chronic lower back pain, Fibromyalgia, Anxiety disorder, GERD",
    },
)

if maria:
    # Rx 1: Gabapentin — transmitted to pharmacy, currently active
    rx1 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{maria}",
            "provider": f"/api/providers/{PROVIDERS['sarah_johnson']}",
            "medication": f"/api/medications/{MEDICATIONS['gabapentin_300']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['scriptsure']}",
            "quantity": 90,
            "daysSupply": 30,
            "refills": 5,
            "directions": "Take 1 capsule by mouth three times daily for nerve pain",
            "clinicalNotes": "Titrated up from 100mg. Patient tolerating well. Monitor for dizziness.",
        },
    )
    if rx1:
        sql(
            f"UPDATE prescriptions SET status='transmitted', prescribed_at='{days_ago(45)}', transmitted_at='{days_ago(44)}', transmission_method='scriptsure' WHERE id='{rx1}';"
        )

    # Rx 2: Alprazolam (Schedule IV) — draft, awaiting provider signature
    rx2 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{maria}",
            "provider": f"/api/providers/{PROVIDERS['sarah_johnson']}",
            "medication": f"/api/medications/{MEDICATIONS['alprazolam']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['scriptsure']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 0,
            "directions": "Take 0.5mg by mouth twice daily as needed for anxiety. Do not exceed 1mg/day.",
            "clinicalNotes": "⚠️ INTERACTION: Gabapentin + Alprazolam — increased CNS depression risk. Discussed with patient. Benefits outweigh risks at current doses. Monitoring closely.",
        },
    )

    # Rx 3: Omeprazole — filled, completing therapy
    rx3 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{maria}",
            "provider": f"/api/providers/{PROVIDERS['sarah_johnson']}",
            "medication": f"/api/medications/{MEDICATIONS['omeprazole_20']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['medisource']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 2,
            "directions": "Take 1 capsule by mouth once daily 30 minutes before breakfast",
        },
    )
    if rx3:
        sql(
            f"UPDATE prescriptions SET status='filled', prescribed_at='{days_ago(60)}', transmitted_at='{days_ago(59)}', filled_at='{days_ago(58)}', transmission_method='unknown' WHERE id='{rx3}';"
        )

    # Rx 4: Hydrocodone (Schedule II) — FAILED transmission, needs attention
    rx4 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{maria}",
            "provider": f"/api/providers/{PROVIDERS['sarah_johnson']}",
            "medication": f"/api/medications/{MEDICATIONS['hydrocodone']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['rxvortex']}",
            "quantity": 20,
            "daysSupply": 10,
            "refills": 0,
            "directions": "Take 1 tablet by mouth every 6 hours as needed for breakthrough pain. Max 4 tablets/day.",
            "clinicalNotes": "Short course for acute flare. Patient signed controlled substance agreement. PDMP checked — no concerns.",
        },
    )
    if rx4:
        sql(
            f"UPDATE prescriptions SET status='failed', prescribed_at='{days_ago(2)}', transmission_method='rxvortex' WHERE id='{rx4}';"
        )

    # Appointment: Follow-up in 3 days
    apt1 = api_post(
        "/api/appointments",
        {
            "patient": f"/api/patients/{maria}",
            "provider": f"/api/providers/{PROVIDERS['sarah_johnson']}",
            "clinic": f"/api/clinics/{CLINICS['downtown']}",
            "scheduledAt": days_ahead(3),
            "type": "video",
            "durationMinutes": 30,
            "reason": "follow_up",
            "patientNotes": "Pain management follow-up. Review Rx efficacy and side effects.",
        },
    )

    # Encounter from last visit
    enc1 = api_post(
        "/api/encounters",
        {
            "patient": f"/api/patients/{maria}",
            "provider": f"/api/providers/{PROVIDERS['sarah_johnson']}",
            "type": "visit",
            "occurredAt": days_ago(14),
        },
    )


# ──────────────────────────────────────────────────────────────────────
# SCENARIO 2: David Park — Post-Surgical Recovery
# Complex: pharmacy failures, urgent queue items, time-sensitive Rx
# ──────────────────────────────────────────────────────────────────────

print("\n═══ SCENARIO 2: David Park — Post-Surgical Recovery ═══")
david = api_post(
    "/api/patients",
    {
        "firstName": "David",
        "lastName": "Park",
        "dob": "1968-03-22",
        "gender": "male",
        "email": "david.park@email.com",
        "phone": "415-555-2002",
        "addressLine1": "882 Geary Boulevard",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94115",
        "allergies": "None known",
        "medicalConditions": "Post-op right knee arthroplasty (2 weeks ago), Hypertension, Hyperlipidemia",
    },
)

if david:
    # Rx 1: Lisinopril — stable, transmitted
    rx5 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{david}",
            "provider": f"/api/providers/{PROVIDERS['michael_chen']}",
            "medication": f"/api/medications/{MEDICATIONS['lisinopril_20']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['scriptsure']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 11,
            "directions": "Take 1 tablet by mouth once daily. Monitor blood pressure.",
        },
    )
    if rx5:
        sql(
            f"UPDATE prescriptions SET status='transmitted', prescribed_at='{days_ago(180)}', transmitted_at='{days_ago(179)}', transmission_method='scriptsure' WHERE id='{rx5}';"
        )

    # Rx 2: Atorvastatin — transmitted
    rx6 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{david}",
            "provider": f"/api/providers/{PROVIDERS['michael_chen']}",
            "medication": f"/api/medications/{MEDICATIONS['atorvastatin_20']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['scriptsure']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 11,
            "directions": "Take 1 tablet by mouth at bedtime",
        },
    )
    if rx6:
        sql(
            f"UPDATE prescriptions SET status='transmitted', prescribed_at='{days_ago(180)}', transmitted_at='{days_ago(179)}', transmission_method='scriptsure' WHERE id='{rx6}';"
        )

    # Rx 3: Post-op prednisone taper — FAILED (pharmacy system down), URGENT
    rx7 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{david}",
            "provider": f"/api/providers/{PROVIDERS['michael_chen']}",
            "medication": f"/api/medications/{MEDICATIONS['prednisone_10']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['dispensepro']}",
            "quantity": 21,
            "daysSupply": 7,
            "refills": 0,
            "directions": "Take 3 tablets daily x2 days, then 2 tablets daily x2 days, then 1 tablet daily x3 days. Take with food.",
            "clinicalNotes": "URGENT — Post-surgical inflammation taper. Patient has 2 days of current supply remaining. Must transmit today.",
        },
    )
    if rx7:
        sql(
            f"UPDATE prescriptions SET status='failed', prescribed_at='{days_ago(1)}', transmission_method='dispensepro' WHERE id='{rx7}';"
        )

    # Appointment: Today, in-person post-op check — confirmed
    apt2 = api_post(
        "/api/appointments",
        {
            "patient": f"/api/patients/{david}",
            "provider": f"/api/providers/{PROVIDERS['michael_chen']}",
            "clinic": f"/api/clinics/{CLINICS['downtown']}",
            "scheduledAt": hours_ahead(2),
            "type": "in_person",
            "durationMinutes": 45,
            "reason": "follow_up",
            "patientNotes": "2-week post-op knee replacement check. Assess wound, ROM, pain levels.",
        },
    )
    if apt2:
        # Confirm the appointment
        subprocess.run(
            ["curl", "-s", "-X", "POST", f"{BASE}/api/appointments/{apt2}/confirm"] + HEADERS,
            capture_output=True,
            text=True,
        )
        print(f"  Confirmed appointment {apt2}")

    # Appointment: Physical therapy consult next week
    apt3 = api_post(
        "/api/appointments",
        {
            "patient": f"/api/patients/{david}",
            "provider": f"/api/providers/{PROVIDERS['emily_martinez']}",
            "clinic": f"/api/clinics/{CLINICS['westside']}",
            "scheduledAt": days_ahead(7),
            "type": "in_person",
            "durationMinutes": 60,
            "reason": "consultation",
            "patientNotes": "PT evaluation for post-op knee rehab protocol.",
        },
    )

    # Encounter: Surgery notes
    enc2 = api_post(
        "/api/encounters",
        {
            "patient": f"/api/patients/{david}",
            "provider": f"/api/providers/{PROVIDERS['michael_chen']}",
            "type": "procedure",
            "occurredAt": days_ago(14),
        },
    )


# ──────────────────────────────────────────────────────────────────────
# SCENARIO 3: Catherine Shaw — Complex Polypharmacy Elder
# Complex: 6+ medications, multiple providers, refill coordination, fall risk
# ──────────────────────────────────────────────────────────────────────

print("\n═══ SCENARIO 3: Catherine Shaw — Polypharmacy Elder ═══")
catherine = api_post(
    "/api/patients",
    {
        "firstName": "Catherine",
        "lastName": "Shaw",
        "dob": "1948-11-30",
        "gender": "female",
        "email": "catherine.shaw@email.com",
        "phone": "415-555-2003",
        "addressLine1": "77 Sunset Boulevard, Apt 4B",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94122",
        "allergies": "ACE inhibitors (angioedema), Aspirin",
        "medicalConditions": "Hypertension, Type 2 Diabetes, Hypothyroidism, Osteoporosis, Mild cognitive impairment, Fall risk",
    },
)

if catherine:
    # Rx 1: Losartan (ARB — can't take ACE inhibitors) — active, transmitted
    rx8 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{catherine}",
            "provider": f"/api/providers/{PROVIDERS['lisa_cuddy']}",
            "medication": f"/api/medications/{MEDICATIONS['losartan_50']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['medisource']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 11,
            "directions": "Take 1 tablet by mouth once daily in the morning",
            "clinicalNotes": "Switched from lisinopril due to angioedema. Monitoring potassium levels.",
        },
    )
    if rx8:
        sql(
            f"UPDATE prescriptions SET status='transmitted', prescribed_at='{days_ago(90)}', transmitted_at='{days_ago(89)}', transmission_method='unknown' WHERE id='{rx8}';"
        )

    # Rx 2: Amlodipine (add-on for BP) — transmitted
    rx9 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{catherine}",
            "provider": f"/api/providers/{PROVIDERS['lisa_cuddy']}",
            "medication": f"/api/medications/{MEDICATIONS['amlodipine_5']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['medisource']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 11,
            "directions": "Take 1 tablet by mouth once daily",
            "clinicalNotes": "Added for persistent HTN despite losartan monotherapy. BP target <140/90.",
        },
    )
    if rx9:
        sql(
            f"UPDATE prescriptions SET status='transmitted', prescribed_at='{days_ago(60)}', transmitted_at='{days_ago(59)}', transmission_method='unknown' WHERE id='{rx9}';"
        )

    # Rx 3: Metformin — dose increase, newly prescribed
    rx10 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{catherine}",
            "provider": f"/api/providers/{PROVIDERS['james_wilson']}",
            "medication": f"/api/medications/{MEDICATIONS['metformin_1000']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['medisource']}",
            "quantity": 60,
            "daysSupply": 30,
            "refills": 5,
            "directions": "Take 1 tablet by mouth twice daily with meals. Report any GI side effects.",
            "clinicalNotes": "A1C increased to 8.1% (was 7.4%). Titrating up from 500mg BID. Recheck A1C in 3 months.",
        },
    )
    if rx10:
        sql(
            f"UPDATE prescriptions SET status='signed', prescribed_at='{days_ago(1)}', signed_by_id='{PROVIDERS['james_wilson']}', signed_by_name='Dr. James Wilson' WHERE id='{rx10}';"
        )

    # Rx 4: Levothyroxine — stable, filled
    rx11 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{catherine}",
            "provider": f"/api/providers/{PROVIDERS['lisa_cuddy']}",
            "medication": f"/api/medications/{MEDICATIONS['levothyroxine_50']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['medisource']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 11,
            "directions": "Take 1 tablet by mouth every morning on an empty stomach, 30 min before eating. Separate from calcium/iron by 4 hours.",
            "clinicalNotes": "TSH stable at 2.1. Continue current dose.",
        },
    )
    if rx11:
        sql(
            f"UPDATE prescriptions SET status='filled', prescribed_at='{days_ago(30)}', transmitted_at='{days_ago(29)}', filled_at='{days_ago(27)}', transmission_method='unknown' WHERE id='{rx11}';"
        )

    # Rx 5: Atorvastatin — needs refill soon (draft for renewal)
    rx12 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{catherine}",
            "provider": f"/api/providers/{PROVIDERS['lisa_cuddy']}",
            "medication": f"/api/medications/{MEDICATIONS['atorvastatin_10']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['medisource']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 11,
            "directions": "Take 1 tablet by mouth at bedtime",
            "clinicalNotes": "Refill renewal. LDL at 98 on current dose — at goal. CK normal.",
        },
    )

    # Appointment: Endocrinology follow-up
    apt4 = api_post(
        "/api/appointments",
        {
            "patient": f"/api/patients/{catherine}",
            "provider": f"/api/providers/{PROVIDERS['james_wilson']}",
            "clinic": f"/api/clinics/{CLINICS['downtown']}",
            "scheduledAt": days_ahead(5),
            "type": "video",
            "durationMinutes": 30,
            "reason": "follow_up",
            "patientNotes": "Diabetes management check. Review new metformin dose tolerance and home glucose logs.",
        },
    )

    # Appointment: Annual wellness, next month
    apt5 = api_post(
        "/api/appointments",
        {
            "patient": f"/api/patients/{catherine}",
            "provider": f"/api/providers/{PROVIDERS['lisa_cuddy']}",
            "clinic": f"/api/clinics/{CLINICS['westside']}",
            "scheduledAt": days_ahead(28),
            "type": "in_person",
            "durationMinutes": 60,
            "reason": "new_patient",
            "patientNotes": "Annual wellness visit. Full labs ordered. Fall risk reassessment. Cognitive screening due.",
        },
    )


# ──────────────────────────────────────────────────────────────────────
# SCENARIO 4: Marcus Johnson — Weight Management Program
# Complex: GLP-1 therapy, e-commerce orders, subscription model
# ──────────────────────────────────────────────────────────────────────

print("\n═══ SCENARIO 4: Marcus Johnson — Weight Management ═══")
marcus = api_post(
    "/api/patients",
    {
        "firstName": "Marcus",
        "lastName": "Johnson",
        "dob": "1991-06-08",
        "gender": "male",
        "email": "marcus.j@email.com",
        "phone": "415-555-2004",
        "addressLine1": "2200 Market Street, Unit 12",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94114",
        "allergies": "None known",
        "medicalConditions": "Obesity (BMI 34.2), Prediabetes (A1C 6.2%), Mild hypertension",
    },
)

if marcus:
    # Rx 1: Semaglutide (Wegovy) — recently transmitted, compounding pharmacy
    rx13 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{marcus}",
            "provider": f"/api/providers/{PROVIDERS['emily_martinez']}",
            "medication": f"/api/medications/{MEDICATIONS['semaglutide']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['dispensepro']}",
            "quantity": 4,
            "daysSupply": 28,
            "refills": 2,
            "directions": "Inject 0.5mL subcutaneously once weekly. Rotate injection sites. Titration month 2 — increasing from 1.7mg to 2.4mg.",
            "clinicalNotes": "Month 2 of GLP-1 therapy. Lost 8 lbs so far (268→260). Mild nausea first 3 days after injection — improving. Titrating up per protocol.",
        },
    )
    if rx13:
        sql(
            f"UPDATE prescriptions SET status='transmitted', prescribed_at='{days_ago(5)}', transmitted_at='{days_ago(4)}', transmission_method='dispensepro', ship_to='patient' WHERE id='{rx13}';"
        )

    # Rx 2: Metformin 500mg — adjunct, shipped and delivered
    rx14 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{marcus}",
            "provider": f"/api/providers/{PROVIDERS['emily_martinez']}",
            "medication": f"/api/medications/{MEDICATIONS['metformin_500']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['carefirst']}",
            "quantity": 60,
            "daysSupply": 30,
            "refills": 5,
            "directions": "Take 1 tablet by mouth twice daily with meals",
            "clinicalNotes": "Adjunct for prediabetes and weight management. GI tolerance good.",
        },
    )
    if rx14:
        sql(
            f"UPDATE prescriptions SET status='shipped', prescribed_at='{days_ago(35)}', transmitted_at='{days_ago(34)}', filled_at='{days_ago(32)}', shipped_at='{days_ago(30)}', tracking_number='1Z999AA10123456784', carrier='UPS', transmission_method='unknown' WHERE id='{rx14}';"
        )

    # Rx 3: Lisinopril — for mild HTN, transmitted
    rx15 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{marcus}",
            "provider": f"/api/providers/{PROVIDERS['emily_martinez']}",
            "medication": f"/api/medications/{MEDICATIONS['lisinopril_10']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['scriptsure']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 5,
            "directions": "Take 1 tablet by mouth once daily",
        },
    )
    if rx15:
        sql(
            f"UPDATE prescriptions SET status='transmitted', prescribed_at='{days_ago(35)}', transmitted_at='{days_ago(34)}', transmission_method='scriptsure' WHERE id='{rx15}';"
        )

    # Appointment: Monthly weight check — today, video
    apt6 = api_post(
        "/api/appointments",
        {
            "patient": f"/api/patients/{marcus}",
            "provider": f"/api/providers/{PROVIDERS['emily_martinez']}",
            "clinic": f"/api/clinics/{CLINICS['westside']}",
            "scheduledAt": hours_ahead(4),
            "type": "video",
            "durationMinutes": 20,
            "reason": "follow_up",
            "patientNotes": "Monthly weight management check-in. Review food diary, activity level, side effects.",
        },
    )

    # Encounter: Initial consultation 5 weeks ago
    enc3 = api_post(
        "/api/encounters",
        {
            "patient": f"/api/patients/{marcus}",
            "provider": f"/api/providers/{PROVIDERS['emily_martinez']}",
            "type": "consultation",
            "occurredAt": days_ago(35),
        },
    )


# ──────────────────────────────────────────────────────────────────────
# SCENARIO 5: Priya Patel — New Patient, Mental Health + Endocrine
# Complex: multi-specialty, new patient intake, video visits, sensitive Rx
# ──────────────────────────────────────────────────────────────────────

print("\n═══ SCENARIO 5: Priya Patel — Mental Health + Endocrine ═══")
priya = api_post(
    "/api/patients",
    {
        "firstName": "Priya",
        "lastName": "Patel",
        "dob": "1995-12-03",
        "gender": "female",
        "email": "priya.patel@email.com",
        "phone": "415-555-2005",
        "addressLine1": "555 Castro Street",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94114",
        "allergies": "Latex",
        "medicalConditions": "Major depressive disorder, Generalized anxiety, Hypothyroidism, PCOS",
    },
)

if priya:
    # Rx 1: Sertraline — stable, recently refilled
    rx16 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{priya}",
            "provider": f"/api/providers/{PROVIDERS['sarah_johnson']}",
            "medication": f"/api/medications/{MEDICATIONS['sertraline_50']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['scriptsure']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 5,
            "directions": "Take 1 tablet by mouth once daily in the morning. Do not stop abruptly.",
            "clinicalNotes": "PHQ-9 improved from 18 to 11 over 3 months. Continue current dose. Monitoring for sexual side effects.",
        },
    )
    if rx16:
        sql(
            f"UPDATE prescriptions SET status='filled', prescribed_at='{days_ago(10)}', transmitted_at='{days_ago(9)}', filled_at='{days_ago(7)}', transmission_method='scriptsure' WHERE id='{rx16}';"
        )

    # Rx 2: Levothyroxine — dose adjustment needed, draft
    rx17 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{priya}",
            "provider": f"/api/providers/{PROVIDERS['james_wilson']}",
            "medication": f"/api/medications/{MEDICATIONS['levothyroxine_50']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['scriptsure']}",
            "quantity": 30,
            "daysSupply": 30,
            "refills": 5,
            "directions": "Take 1 tablet by mouth every morning on empty stomach",
            "clinicalNotes": "TSH elevated at 6.8 (was 3.2). May need dose increase to 75mcg. Awaiting repeat labs before finalizing. DO NOT TRANSMIT YET.",
        },
    )

    # Rx 3: Metformin — for PCOS, signed and ready
    rx18 = api_post(
        "/api/prescriptions",
        {
            "patient": f"/api/patients/{priya}",
            "provider": f"/api/providers/{PROVIDERS['james_wilson']}",
            "medication": f"/api/medications/{MEDICATIONS['metformin_500']}",
            "pharmacy": f"/api/pharmacies/{PHARMACIES['rxvortex']}",
            "quantity": 60,
            "daysSupply": 30,
            "refills": 5,
            "directions": "Take 1 tablet by mouth twice daily with meals. Start with 1 tablet daily for first week.",
            "clinicalNotes": "For PCOS insulin resistance. Gradual titration to minimize GI side effects.",
        },
    )
    if rx18:
        sql(
            f"UPDATE prescriptions SET status='signed', prescribed_at=NOW(), signed_by_id='{PROVIDERS['james_wilson']}', signed_by_name='Dr. James Wilson' WHERE id='{rx18}';"
        )

    # Appointment: Psychiatry follow-up tomorrow
    apt7 = api_post(
        "/api/appointments",
        {
            "patient": f"/api/patients/{priya}",
            "provider": f"/api/providers/{PROVIDERS['sarah_johnson']}",
            "clinic": f"/api/clinics/{CLINICS['downtown']}",
            "scheduledAt": days_ahead(1),
            "type": "video",
            "durationMinutes": 45,
            "reason": "follow_up",
            "patientNotes": "Psychiatric follow-up. Review sertraline efficacy, sleep quality, and anxiety levels.",
        },
    )

    # Appointment: Endocrinology — next week
    apt8 = api_post(
        "/api/appointments",
        {
            "patient": f"/api/patients/{priya}",
            "provider": f"/api/providers/{PROVIDERS['james_wilson']}",
            "clinic": f"/api/clinics/{CLINICS['downtown']}",
            "scheduledAt": days_ahead(8),
            "type": "video",
            "durationMinutes": 30,
            "reason": "follow_up",
            "patientNotes": "Thyroid management + PCOS follow-up. Repeat TSH and metabolic panel results due.",
        },
    )

    # Encounter: New patient intake 3 months ago
    enc4 = api_post(
        "/api/encounters",
        {
            "patient": f"/api/patients/{priya}",
            "provider": f"/api/providers/{PROVIDERS['sarah_johnson']}",
            "type": "visit",
            "occurredAt": days_ago(90),
        },
    )


# ──────────────────────────────────────────────────────────────────────
# SUMMARY
# ──────────────────────────────────────────────────────────────────────

print("\n═══════════════════════════════════════════════════════")
print("SEED COMPLETE — 5 patient scenarios with:")
print("  • 5 new patients with complex medical histories")
print("  • ~18 prescriptions in various states:")
print("      draft, signed, transmitted, filled, shipped, failed")
print("  • ~8 appointments (today, tomorrow, this week, next month)")
print("  • ~4 encounters (historical)")
print("  • Drug interactions, controlled substances, polypharmacy")
print("  • Pharmacy transmission failures needing attention")
print("  • Multi-provider, multi-pharmacy coordination")
print("═══════════════════════════════════════════════════════")
print("\nGo to app.robothor.ai and try:")
print('  • "Show me the prescription pipeline"')
print('  • "What appointments do we have today?"')
print('  • "Show me patients with failed prescriptions"')
print('  • "Give me an overview of the provider queue"')
print('  • "Show me Catherine Shaw\'s medication list"')
