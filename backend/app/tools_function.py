import json
import logging
import os
import random
import re
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import urllib.error
import urllib.parse
import urllib.request

from sqlalchemy import or_, select, func
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    User,
    Job,
    Candidate,
    JobCandidate,
    CandidateContact,
)

WHATSAPP_STAGE_NUMBERS = [
    "919883236730",
    "919064148052",
    "917439717970",
    "919330609485",
]


IST_ZONE = ZoneInfo("Asia/Kolkata")

def _to_naive_ist(dt: datetime) -> datetime:
    """Normalize a datetime to naive IST (Asia/Kolkata) wall-clock time."""
    if dt.tzinfo is not None:
        return dt.astimezone(IST_ZONE).replace(tzinfo=None)
    return dt


# ============================================================
# QUERY & DASHBOARD FUNCTIONS
# ============================================================

def get_job_candidates(
    db: Session,
    job_id: UUID,
    limit: int = 50,
) -> list[dict]:
    rows = db.execute(
        select(JobCandidate, Candidate)
        .join(
            Candidate,
            Candidate.id == JobCandidate.candidate_id,
        )
        .where(
            JobCandidate.job_id == job_id,
        )
        .order_by(
            JobCandidate.created_at.desc(),
        )
        .limit(limit)
    ).all()

    results = []
    for jc, candidate in rows:
        results.append({
            "job_candidate_id": str(jc.id),
            "candidate_id": str(candidate.id),
            "user_name": candidate.name or "",
            "name": candidate.name or "",
            "email": candidate.email or "",
            "phone_no": candidate.phone or "",
            "phone": candidate.phone or "",
            "current_company": candidate.current_company or "",
            "company": candidate.current_company or "",
            "job_role": candidate.current_role or "",
            "role": candidate.current_role or "",
            "experience": (
                float(candidate.total_experience_years)
                if candidate.total_experience_years is not None
                else None 
            ),
            "requisition_id": jc.requisition_id or candidate.requisition_id or "",
            "status": jc.recruitment_status,
            "recruitment_status": jc.recruitment_status,
            "interview_scheduled_at": (
                jc.interview_scheduled_at.isoformat()
                if jc.interview_scheduled_at
                else None
            ),
            "interview_link": jc.interview_link,
            "registration_url": jc.registration_url,
            "created_at": jc.created_at.isoformat() if jc.created_at else None,
        })

    return results

def get_user_jobs(db: Session, user_id: UUID) -> list[dict]:
    jobs = db.scalars(select(Job).where(Job.created_by == user_id).order_by(Job.created_at.desc())).all()
    return [{"job_id": str(j.id), "id": str(j.id), "title": j.title, "description": j.description, "location": j.location, "work_mode": j.work_mode, "file_name": None, "status": j.status,
             "created_at": j.created_at.isoformat() if j.created_at else None} for j in jobs]


def get_user_job_candidates(db: Session, user_id: UUID, job_id: UUID) -> list[dict]:
    if not db.scalar(select(Job.id).where(Job.id == job_id, Job.created_by == user_id)):
        raise PermissionError("Job does not belong to current user")
    return get_job_candidates(db, job_id, limit=50)


def get_user_dashboard(db: Session, user_id: UUID) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise ValueError("User not found")

    q = (
        select(JobCandidate, Candidate, Job)
        .join(Candidate, Candidate.id == JobCandidate.candidate_id)
        .join(Job, Job.id == JobCandidate.job_id)
    )

    all_jc = db.execute(q.order_by(JobCandidate.updated_at.desc())).all()

    invites_sent = 0
    interested = 0
    meeting_scheduled = 0
    not_interested = 0
    awaiting = 0
    delivered_count = 0
    failed_count = 0
    skipped_count = 0

    recent_candidates = []

    for jc, cand, job in all_jc:
        status = jc.recruitment_status or "NEW"
        has_outreach = status in {
            "CONTACTED",
            "INTERESTED",
            "INTERVIEW_LINK_SENT",
            "INTERVIEW_SCHEDULED",
            "INTERVIEW",
            "INTERVIEW_COMPLETED",
            "NOT_INTERESTED",
            "SKIPPED",
            "INVALID_PHONE",
            "SKIPPED_INVALID_PHONE",
            "FAILED",
        }

        if has_outreach:
            latest_contact = db.scalar(
                select(CandidateContact)
                .where(
                    CandidateContact.job_candidate_id == jc.id,
                    CandidateContact.message_type == "OUTBOUND",
                )
                .order_by(CandidateContact.created_at.desc())
                .limit(1)
            )

            c_status = (latest_contact.status if latest_contact else ("DELIVERED" if status in {"INTERESTED", "INTERVIEW_LINK_SENT", "INTERVIEW_SCHEDULED"} else "DELIVERED")).upper()
            if c_status in {"INVALID_PHONE", "SKIPPED", "SKIPPED_INVALID_PHONE"} or status in {"SKIPPED", "INVALID_PHONE", "SKIPPED_INVALID_PHONE"}:
                delivery_status = "INVALID_PHONE"
                display_status = "SKIPPED"
                skipped_count += 1
            elif c_status == "FAILED" or status == "FAILED":
                delivery_status = "FAILED"
                display_status = "FAILED"
                failed_count += 1
            else:
                delivery_status = "DELIVERED"
                display_status = status
                delivered_count += 1

            if status not in {"SKIPPED", "INVALID_PHONE", "SKIPPED_INVALID_PHONE"}:
                invites_sent += 1

            if status in {"INTERVIEW_SCHEDULED", "INTERVIEW", "INTERVIEW_COMPLETED"} or jc.interview_scheduled_at is not None:
                meeting_scheduled += 1
                interested += 1
            elif status in {"INTERESTED", "INTERVIEW_LINK_SENT"}:
                interested += 1
            elif status == "NOT_INTERESTED":
                not_interested += 1
            elif status == "CONTACTED":
                if delivery_status not in {"FAILED", "INVALID_PHONE"}:
                    awaiting += 1

            if len(recent_candidates) < 40:
                scheduled_str = (
                    _to_naive_ist(jc.interview_scheduled_at).strftime("%Y-%m-%dT%H:%M:%S")
                    if jc.interview_scheduled_at
                    else None
                )
                recent_candidates.append({
                    "id": str(cand.id),
                    "job_candidate_id": str(jc.id),
                    "job_id": str(job.id),
                    "name": cand.name or "Unnamed",
                    "role": cand.current_role or job.title or "Position",
                    "phone": cand.phone or "",
                    "status": display_status,
                    "delivery_status": delivery_status,
                    "interview_scheduled_at": scheduled_str,
                    "interview_link": jc.interview_link,
                    "updated_at": jc.updated_at.isoformat() if jc.updated_at else None,
                })

    return {
        "user_name": user.name,
        "metrics": {
            "invites_sent": invites_sent,
            "interested": interested,
            "meeting_scheduled": meeting_scheduled,
            "not_interested": not_interested,
            "awaiting": awaiting,
            "delivered": delivered_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "invalid_phone": skipped_count,
            "total_outreach": delivered_count + failed_count + skipped_count,
            "delivery_rate": (
                round((delivered_count / (delivered_count + failed_count + skipped_count)) * 100)
                if (delivered_count + failed_count + skipped_count) > 0
                else 100
            ),
            "active_jobs": invites_sent,
            "candidates_screened": invites_sent,
            "strong_matches": interested,
            "whatsapp_outreach": invites_sent,
        },
        "recent_candidates": recent_candidates,
        "pipeline": recent_candidates,
    }

def _clean_whatsapp_phone(raw_phone: str | None) -> str:
    if not raw_phone:
        return ""

    clean_phone = "".join(filter(str.isdigit, str(raw_phone)))
    if clean_phone.startswith("0"):
        clean_phone = clean_phone.lstrip("0")
    if clean_phone.startswith("91"):
        if len(clean_phone) == 12 and clean_phone[2] in "6789":
            return clean_phone
        return ""
    if len(clean_phone) == 10 and clean_phone[0] in "6789":
        return "91" + clean_phone
    return ""


def _whatsapp_headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    api_key = (
        getattr(settings, "WHATSAPP_MESSAGE_API_KEY", "")
        or getattr(settings, "WHATSAPP_API_KEY", "")
        or os.getenv("WHATSAPP_MESSAGE_API_KEY", "")
        or os.getenv("WHATSAPP_API_KEY", "")
    )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["api-key"] = api_key

    return headers


def _extract_provider_message_id(response_body: str | dict | None) -> str | None:
    if isinstance(response_body, dict):
        for key in (
            "messageId",
            "message_id",
            "externalMessageId",
            "external_message_id",
            "id",
        ):
            value = response_body.get(key)
            if value:
                return str(value)

        for nested_key in ("data", "result", "message"):
            nested = response_body.get(nested_key)
            if isinstance(nested, dict):
                nested_id = _extract_provider_message_id(nested)
                if nested_id:
                    return nested_id

    return None

def _send_whatsapp_payload(
    payload: dict,
) -> tuple[bool, dict | None, str | None]:
    base_url = getattr(
        settings,
        "WHATSAPP_BASE_URL",
        "https://nmve.io/whatsapp/api/integrations/whatsapp/template-messages",
    )
    if payload.get("type") == "text" or "templateName" not in payload:
        url = base_url.replace("/template-messages", "/messages")
    else:
        url = base_url
    api_key = (
        getattr(settings, "WHATSAPP_MESSAGE_API_KEY", "")
        or getattr(settings, "WHATSAPP_API_KEY", "")
        or os.getenv("WHATSAPP_MESSAGE_API_KEY", "")
        or os.getenv("WHATSAPP_API_KEY", "")
    )
    if not api_key:
        print(
            "WHATSAPP_MESSAGE_API_KEY (or WHATSAPP_API_KEY) is not configured for sending messages."
        )
        return False, None, None

    # --------------------------------------------------
    # WHATSAPP TEST MODE SAFETY OVERRIDE
    # --------------------------------------------------
    if getattr(
        settings,
        "WHATSAPP_TEST_MODE",
        False,
    ):
        test_number = getattr(
            settings,
            "WHATSAPP_TEST_NUMBER",
            "",
        )    
        if not test_number:
            print(
                "WHATSAPP TEST MODE is enabled but "
                "WHATSAPP_TEST_NUMBER is missing. "
                "BLOCKING WhatsApp send."
            )
            return False, None, None

        test_phone = _clean_whatsapp_phone(
            test_number
        )

        if not test_phone:
            print(
                "WHATSAPP_TEST_NUMBER is invalid. "
                "BLOCKING WhatsApp send."
            )
            return False, None, None

        original_to = payload.get("to")
        payload["to"] = test_phone
        print(
            f"WHATSAPP TEST MODE: "
            f"{original_to} -> {test_phone}"
        )

    # --------------------------------------------------
    # SEND PAYLOAD
    # -----------------------------------------------    
    data = json.dumps(payload).encode(
        "utf-8"   
    )
    headers = _whatsapp_headers()
    print(f"[WhatsApp] Sending to {payload.get('to')} using messaging key: {api_key[:14]}... on {url}")
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            body = response.read().decode(
                "utf-8",
                errors="replace",
            )
            print(
                f"WhatsApp API response status: "
                f"{response.status}"
            )
            parsed = None
            if body.strip():
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {
                        "raw_response": body
                    }
            return (
                True,
                parsed,
                _extract_provider_message_id(
                    parsed
                ),
            )
    except urllib.error.HTTPError as http_err:
        try:
            error_body = (
                http_err.read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )
        except Exception:
            error_body = str(
                http_err
            )
        print(
            f"WhatsApp API HTTP Error: "
            f"{http_err.code}"
        )

        print(
            f"Error Details: "
            f"{error_body}"
        )
        return False, None, None
    except Exception as error:

        print(
            f"WhatsApp API Error: "
            f"{error}"
        )

        return False, None, None

def _send_whatsapp_to_candidate(
    db: Session,
    job_id: UUID,
    candidate_id: UUID,
    target_phone: str | None = None,
):
    print(
        f"--- Attempting WhatsApp Integration for candidate "
        f"{candidate_id} ---"
    )
    try:
        candidate = db.query(Candidate).filter(
            Candidate.id == candidate_id
        ).first()
        if not candidate:
            print(
                "Candidate not found in DB."
            )
            return False

        job_candidate = db.scalar(
            select(JobCandidate).where(
                JobCandidate.job_id == job_id,
                JobCandidate.candidate_id == candidate_id,
            )
        )
        if not job_candidate:
            print(
                "JobCandidate not found."
            )
            return False

        # --------------------------------------------------
        # PHONE NUMBER
        # --------------------------------------------------
        if getattr(
            settings,
            "WHATSAPP_TEST_MODE",
            False,
        ):

            # --------------------------------------------------
            # TEST MODE
            # NEVER USE CANDIDATE PHONE
            # --------------------------------------------------
            test_number = getattr(
                settings,
                "WHATSAPP_TEST_NUMBER",
                "",
            )
            if not test_number:
                print(
                    "WHATSAPP TEST MODE is enabled but "
                    "WHATSAPP_TEST_NUMBER is not configured. "
                    "Blocking WhatsApp send."
                )
                return False

            clean_phone = _clean_whatsapp_phone(
                test_number
            )

            if not clean_phone:
                print(
                    "WHATSAPP_TEST_NUMBER is invalid. "
                    "Blocking WhatsApp send."
                )
                return False

            print(
                f"WHATSAPP TEST MODE ENABLED - "
                f"Sending ONLY to test number: "
                f"{clean_phone}"
            )

        else:

            raw_phone = target_phone
            if not raw_phone:
                if WHATSAPP_STAGE_NUMBERS:
                    random_index = random.randint(
                        0, len(WHATSAPP_STAGE_NUMBERS) - 1
                    )
                    raw_phone = WHATSAPP_STAGE_NUMBERS[random_index]
                else:
                    raw_phone = (
                        str(candidate.phone)
                        if candidate.phone
                        else ""
                    )


            clean_phone = _clean_whatsapp_phone(
                raw_phone
            )

            if not clean_phone:
                print(
                    "No valid phone number available "
                    "for candidate. "
                    "Skipping WhatsApp."
                )
                return False

        # --------------------------------------------------
        # CONFIGURATION
        # --------------------------------------------------

        callback_url = getattr(
            settings,
            "WHATSAPP_CALLBACK_URL",
            "https://nmhirexv3.onrender.com/api/webhooks/whatsapp",
        )

        template_name = getattr(
            settings,
            "WHATSAPP_TEMPLATE_NAME",
            "hello_world",
        )

        template_language = getattr(
            settings,
            "WHATSAPP_TEMPLATE_LANGUAGE",
            "en_US",
        )

        reference_id = (
            f"NMHireX-{str(job_candidate.id)}"
        )

        # --------------------------------------------------
        # WHATSAPP PAYLOAD
        # --------------------------------------------------

        job = db.get(Job, job_id)
        job_title = (job.title if job else None) or "the position"
        company_name = getattr(settings, "WHATSAPP_COMPANY_NAME", "the company")
        payload = {
            "to": clean_phone,
            "templateName": template_name,
            "bodyParams": [
                candidate.name or "Candidate",
                job_title,
                company_name,
                company_name,
            ],
            "languageCode": template_language,
            "referenceId": reference_id,
            "callbackUrl": callback_url,
        }

        print(
            f"Sending WhatsApp payload to "
            f"{clean_phone}..."
        )
        success, response_body, provider_message_id = (
            _send_whatsapp_payload(
                payload
            )
        )

        if not success:
            return False

        # --------------------------------------------------
        # SAVE OUTBOUND MESSAGE
        # --------------------------------------------------
        contact = CandidateContact(
            job_candidate_id=job_candidate.id,
            channel="WHATSAPP",
            message_type="OUTBOUND",
            message="Hi",
            provider="NMVE",
            external_message_id=provider_message_id,
            status="SENT",
            sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
        )
        db.add(contact)

        # --------------------------------------------------
        # UPDATE RECRUITMENT STATUS
        # --------------------------------------------------

        if job_candidate.recruitment_status in {
            None,
            "NEW",
            "SHORTLISTED",
        }:
            job_candidate.recruitment_status = (
                "CONTACTED"
            )
        db.commit()

        print(
            "WhatsApp message saved to "
            "candidate_contacts."
        )

        return True
    except Exception as error:
        db.rollback()
        print(
            f"Error in WhatsApp integration: "
            f"{error}"
        )

        return False


def _send_whatsapp_text_message(
    raw_phone: str,
    text_message: str,
):
    try:

        clean_phone = _clean_whatsapp_phone(
            raw_phone
        )

        if not clean_phone:
            print(
                "No valid phone number supplied."
            )
            return False

        payload = {
            "to": clean_phone,
            "type": "text",
            "text": {
                "body": text_message,
                "preview_url": True,
            },
        }

        success, _, _ = _send_whatsapp_payload(
            payload
        )

        if success:
            print(
                f"WhatsApp text message sent to "
                f"{clean_phone}."
            )

        return success

    except Exception as error:

        print(
            f"Error sending WhatsApp text: "
            f"{error}"
        )

        return False


def _send_whatsapp_cta_message(
    raw_phone: str,
    job_candidate_id: str,
):
    clean_phone = _clean_whatsapp_phone(
        raw_phone
    )

    if not clean_phone:
        print(
            "No valid phone number supplied."
        )
        return False

    frontend_url = getattr(
        settings,
        "FRONTEND_URL",
        "https://nmhirexv3.vestaging.in/",
    ).rstrip("/")

    if not frontend_url or "localhost" in frontend_url or "127.0.0.1" in frontend_url:
        frontend_url = "https://nmhirexv3.vestaging.in/"
    scheduling_link = (
        f"{frontend_url}/schedule/{job_candidate_id}"
    )

    text_message = (
        "Great news! 🎉 We'd love to move forward with your application.\n\n"
        "Please select your preferred date and time for the interview using the link below:\n\n"
        f"{scheduling_link}\n\n"
        "💡 *Note:* If the link above is not clickable, please save this number to your contacts or tap 'Add to contacts' at the top of the chat.\n\n"
        "We look forward to connecting with you! 😊"
    )
    return _send_whatsapp_text_message(
        clean_phone,
        text_message,
    )


CONTACTED_RECRUITMENT_STATUSES = {
    "CONTACTED",
    "INTERESTED",
    "INTERVIEW_LINK_SENT",
    "INTERVIEW_SCHEDULED",
    "INTERVIEW",
    "INTERVIEW_COMPLETED",
    "COMPLETED",
    "NOT_INTERESTED",
    "SKIPPED",
    "INVALID_PHONE",
    "SKIPPED_INVALID_PHONE",
    "FAILED",
}


def _phone_lookup_variants(
    raw_phone: str | None = None,
    clean_phone: str | None = None,
) -> list[str]:
    variants: list[str] = []
    seen: set[str] = set()
    def _add(val: str | None) -> None:
        s = (val or "").strip()
        if s and s not in seen:
            seen.add(s)
            variants.append(s)

    _add(clean_phone)
    _add(raw_phone)
    if clean_phone and clean_phone.startswith("91") and len(clean_phone) == 12:
        local_10 = clean_phone[2:]
        _add(local_10)
        _add(f"+{clean_phone}")
        _add(f"+91 {local_10}")

    if raw_phone:
        digits = "".join(filter(str.isdigit, str(raw_phone)))
        _add(digits)

    return variants

def _find_matching_candidates(#
    db: Session,
    raw_phone: str | None = None,
    clean_phone: str | None = None,
    email: str | None = None,
) -> list[Candidate]:
    phone_variants = _phone_lookup_variants(raw_phone, clean_phone)
    clean_email = email.strip().lower() if email and email.strip() else None
    conditions = []
    if phone_variants:
        conditions.append(Candidate.phone.in_(phone_variants))
    if clean_email:
        conditions.append(func.lower(func.trim(Candidate.email)) == clean_email)
    if not conditions:
        return []
    matches = list(db.scalars(select(Candidate).where(or_(*conditions))).all())
    if len(matches) <= 1 or not phone_variants:
        return matches
    phone_set = set(phone_variants)
    matches.sort(key=lambda c: 0 if (c.phone or "").strip() in phone_set else 1)
    return matches


def _candidates_have_contact_history(
    db: Session,
    candidate_ids: list[UUID],
) -> bool:
    if not candidate_ids:
        return False

    has_contact_row = db.scalar(
        select(CandidateContact.id)
        .join(JobCandidate, JobCandidate.id == CandidateContact.job_candidate_id)
        .where(JobCandidate.candidate_id.in_(candidate_ids))
        .limit(1)
    ) is not None
    if has_contact_row:
        return True

    has_contacted_status = db.scalar(
        select(JobCandidate.id)
        .where(
            JobCandidate.candidate_id.in_(candidate_ids),
            func.upper(func.trim(JobCandidate.recruitment_status)).in_(
                CONTACTED_RECRUITMENT_STATUSES
            ),
        )
        .limit(1)
    ) is not None
    return has_contacted_status


def _candidate_has_contact_history(
    db: Session,
    candidate_id: UUID,
) -> bool:
    candidate = db.get(Candidate, candidate_id)
    if not candidate:
        return False

    clean_phone = _clean_whatsapp_phone(candidate.phone)
    matching_candidates = _find_matching_candidates(
        db,
        raw_phone=candidate.phone,
        clean_phone=clean_phone,
        email=candidate.email,
    )
    candidate_ids = list({candidate.id, *(c.id for c in matching_candidates)})
    return _candidates_have_contact_history(db, candidate_ids)


def _record_invalid_phone_contact(
    db: Session,
    job_candidate: JobCandidate,
) -> None:
    """Mark a JobCandidate as SKIPPED and log an INVALID_PHONE contact attempt in DB."""
    job_candidate.recruitment_status = "SKIPPED"
    db.add(
        CandidateContact(
            job_candidate_id=job_candidate.id,
            channel="WHATSAPP",
            message_type="OUTBOUND",
            message="Skipped: Invalid phone number (<10 digits or incomplete)",
            provider="NMVE",
            status="INVALID_PHONE",
            sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
        )
    )
    db.commit()



def _upsert_candidate(
    db: Session,
    existing: Candidate | None,
    *,
    name: str,
    phone: str,
    role: str,
    email: str = "",
    company: str = "",
    parsed_exp: Decimal | None = None,
    requisition_id: str = "",
) -> Candidate:
    if existing is None:
        candidate = Candidate(
            name=name,
            phone=phone,
            current_role=role,
            email=email if email else None,
            current_company=company if company else None,
            total_experience_years=parsed_exp,
            requisition_id=requisition_id if requisition_id else None,
        )
        db.add(candidate)
        db.flush()
        return candidate

    if phone and not existing.phone:
        existing.phone = phone
    if email and not existing.email:
        existing.email = email
    if company and not existing.current_company:
        existing.current_company = company
    if role and not existing.current_role:
        existing.current_role = role
    if parsed_exp is not None and existing.total_experience_years is None:
        existing.total_experience_years = parsed_exp
    if requisition_id:
        existing.requisition_id = requisition_id
    db.flush()
    return existing


def _classify_whatsapp_response(message: str) -> str:
    text_value = (message or "").strip().lower()

    negative_patterns = [
        r"\bnot interested\b",
        r"\bno thanks\b",
        r"\bdo not want\b",
        r"\bdon't want\b",
        r"\bdecline\b",
        r"\bnot looking\b",
        r"\bno\b",    
    ]
    for pattern in negative_patterns:
        if re.search(pattern, text_value):
            return "NOT_INTERESTED"

    positive_patterns = [
        r"\byes\b",
        r"\binterested\b",
        r"\bsure\b",
        r"\bok\b",
        r"\bokay\b",
        r"\bproceed\b",
        r"\binterview\b",
        r"\bschedule\b",
    ]
    for pattern in positive_patterns:
        if re.search(pattern, text_value):
            return "INTERESTED"
    # if the reply doesn't belong to negative_patterns or positive_patterns the return unknown
    return "UNKNOWN"


def handle_whatsapp_webhook(
    db: Session,
    payload: dict,
) -> dict:
    def find_value(data, keys):
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    return str(value).strip()
            for nested in data.values():
                if isinstance(nested, (dict, list)):
                    result = find_value(nested, keys)
                    if result:
                        return result
        elif isinstance(data, list):
            for nested in data:
                result = find_value(nested, keys)
                if result:
                    return result
        return None    

    phone = _clean_whatsapp_phone(
        find_value(
            payload,
            ["to", "from", "phone", "sender", "wa_id", "waId", "phoneNumber", "fromNumber", "recipient_id"],
        )    #extracts the text message from the payload
    )

    text_message = find_value(
        payload,
        [
            "text",
            "body",
            "messageText",
            "content",
            "button_text",
            "buttonText",
            "title",
            "button_payload",
            "buttonPayload",
            "selectedId",
        ],
    ) or ""
    #"" means that if find_value() returns None or another falsy value, use an empty string instead.
    #This extracts the reference ID associated with the WhatsApp message.
    reference_id = find_value(
        payload,
        ["referenceId", "reference_id", "reference"],
    )
    #This searches for the message ID assigned by NMVE or WhatsApp.
    provider_message_id = find_value(
        payload,
        ["messageId", "message_id", "externalMessageId", "external_message_id", "id"],
    )
    #Checks whether both the phone number and reference ID are missing.
    if not phone and not reference_id:
        return {
            "status": "IGNORED",
            "reason": "Missing phone and reference ID",
        }

    # Update CandidateContact on delivery status receipts (e.g. delivered, failed, read, sent)
    #This checks whether the webhook has no usable message text.
    if not text_message or not text_message.strip():
        #If payload["data"] is a dictionary, use that dictionary.
        #Otherwise, use the complete payload
        data_dict = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        status_val = (
            #This searches for the delivery status using several possible field names.
            data_dict.get("status")
            or data_dict.get("deliveryStatus")
            or data_dict.get("messageStatus")
            or data_dict.get("delivery_status")
            or payload.get("deliveryStatus")
            or payload.get("messageStatus")
            or ("failed" if (data_dict.get("errorCode") or data_dict.get("errorMessage") or payload.get("errorCode")) else None)
        )
        #if no delivery status was found then the webhook contanis  an eventid string 
        if not status_val and isinstance(payload.get("eventId"), str):
            #let eventid is eid = "status:wamid.example123:failed"
            eid = payload.get("eventId").lower()
            #as per example the status is failed
            #so status value is faild
            #if in eventvalue there is something called delivered status would be delivered
            #if in eventvalue there is something called sent status would be delivered
            if ":failed" in eid:
                status_val = "failed"
            elif ":delivered" in eid:
                status_val = "delivered"
            elif ":sent" in eid:
                status_val = "sent"
        #means a delivery status was found.
        if status_val:
            status_clean = str(status_val).strip().upper()
            target_contact = None
            #Checks whether a provider message ID is available.
            #Prepares a query to retrieve a CandidateContact object.
            #Filters records to find the one whose stored external message ID matches the webhook message ID.
            #Executes the query and returns the matching CandidateContact object, or None.
            if provider_message_id:
                target_contact = db.scalar(
                    select(CandidateContact)
                    .where(CandidateContact.external_message_id == provider_message_id)
                    .limit(1)
                )
                #No matching contact was found yet.
                #A reference ID exists.
                #The reference ID starts with "NMHireX-".
            if not target_contact and reference_id and reference_id.startswith("NMHireX-"):
                #Removes the prefix.
                #example NMHireX-123e4567-e89b-12d3-a456-426614174000
                #after strip 123e4567-e89b-12d3-a456-426614174000
                ref_jc_id = reference_id.replace("NMHireX-", "").strip()
                try:
                    #converts the reference ID string into a UUID.
                    #The query finds an outbound contact record associated with that JobCandidate.
                    #.order_by(CandidateContact.created_at.desc()) sorts the records newest first.
                    #.limit(1) retrieves the latest matching record.
                    #try/except handles invalid UUID values without crashing the function.
                    target_contact = db.scalar(
                        select(CandidateContact)
                        .where(
                            CandidateContact.job_candidate_id == UUID(ref_jc_id),
                            CandidateContact.message_type == "OUTBOUND",
                        )
                        .order_by(CandidateContact.created_at.desc())
                        .limit(1)
                    )
                except (ValueError, TypeError):
                    target_contact = None
                    #If the previous searches failed and a phone number exists, the function tries matching by phone.
            if not target_contact and phone:
                raw_digits = "".join(filter(str.isdigit, phone))
                target_contact = db.scalar(
                    select(CandidateContact)#retrieves contact records.
                    .join(JobCandidate, JobCandidate.id == CandidateContact.job_candidate_id)#connects candidate-contacts to job-candidate records.
                    .join(Candidate, Candidate.id == JobCandidate.candidate_id)#connects contacts to job-candidate records.
                    .where(
                        (Candidate.phone == phone) | #phone checks the normalized phone number.
                        (Candidate.phone == raw_digits) | #raw_digits checks the digits-only number.
                        ((Candidate.phone.like(f"%{raw_digits[-10:]}")) if len(raw_digits) >= 10 else (Candidate.phone == phone)),
                        CandidateContact.message_type == "OUTBOUND"
                    )
                    .order_by(CandidateContact.created_at.desc())
                    .limit(1)
                )
            if target_contact:
                if "DELIVER" in status_clean:
                    target_contact.status = "DELIVERED"
                    target_contact.delivered_at = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
                elif "FAIL" in status_clean or "UNDELIVER" in status_clean or data_dict.get("errorCode"):
                    target_contact.status = "FAILED"
                    err_msg = data_dict.get("errorMessage") or data_dict.get("error") or find_value(payload, ["errorMessage", "error", "error_message", "reason"]) or "Delivery failed"
                    target_contact.message = f"Failed: {err_msg}"
                    jc_rec = db.get(JobCandidate, target_contact.job_candidate_id)
                    if jc_rec and jc_rec.recruitment_status in {None, "NEW", "CONTACTED"}:
                        jc_rec.recruitment_status = "FAILED"
                elif "READ" in status_clean:
                    target_contact.status = "READ"
                    target_contact.read_at = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
                elif "SENT" in status_clean:
                    target_contact.status = "SENT"
                    target_contact.sent_at = target_contact.sent_at or datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
                db.commit()
                print(f"WEBHOOK: Updated delivery status of contact {target_contact.id} to {target_contact.status}")
                return {
                    "status": "STATUS_UPDATED",
                    "delivery_status": target_contact.status,
                }
            elif reference_id and reference_id.startswith("NMHireX-"):
                ref_jc_id = reference_id.replace("NMHireX-", "").strip()
                try:
                    jc_uuid = UUID(ref_jc_id)
                    if db.get(JobCandidate, jc_uuid):
                        err_msg = data_dict.get("errorMessage") or find_value(payload, ["errorMessage", "error", "error_message"]) or "Delivery failed"
                        is_failed = "FAIL" in status_clean or "UNDELIVER" in status_clean or data_dict.get("errorCode")
                        is_delivered = "DELIVER" in status_clean
                        new_contact = CandidateContact(
                            job_candidate_id=jc_uuid,
                            channel="WHATSAPP",
                            message_type="OUTBOUND",
                            message=f"Failed: {err_msg}" if is_failed else "Candidate outreach invitation",
                            provider="NMVE",
                            external_message_id=provider_message_id,
                            status="FAILED" if is_failed else ("DELIVERED" if is_delivered else "SENT"),
                            sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
                            delivered_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) if is_delivered else None,
                        )
                        db.add(new_contact)
                        if is_failed:
                            target_jc = db.get(JobCandidate, jc_uuid)
                            if target_jc and target_jc.recruitment_status in {None, "NEW", "CONTACTED"}:
                                target_jc.recruitment_status = "FAILED"
                        db.commit()
                        print(f"WEBHOOK: Created contact for {jc_uuid} with status {new_contact.status}")
                        return {
                            "status": "STATUS_CREATED",
                            "delivery_status": new_contact.status,
                        }
                    else:
                        print(f"WEBHOOK: JobCandidate {jc_uuid} not found in DB. Skipping contact creation.")
                except (ValueError, TypeError):
                    pass

        print(f"WEBHOOK: Delivery status or empty message received from {phone} (ignoring).")
        return {
            "status": "IGNORED",
            "reason": "Empty message body or delivery status receipt",
        }

    job_candidate = None
    if reference_id:
        candidate_prefix = "NMHireX-"
        if reference_id.startswith(candidate_prefix):
            candidate_job_id = reference_id[len(candidate_prefix):]
            try:
                job_candidate = db.get(JobCandidate, UUID(candidate_job_id))
            except (ValueError, TypeError):
                job_candidate = None

    print(f"WEBHOOK EXTRACTED PHONE: {phone}")
    print(f"WEBHOOK EXTRACTED TEXT: {text_message}")

    if job_candidate is None and phone:
        job_candidate = db.execute(
            select(JobCandidate)
            .join(Candidate, Candidate.id == JobCandidate.candidate_id)
            .where(
                Candidate.phone == phone,
                JobCandidate.recruitment_status.in_([
                    "CONTACTED",
                    "INTERESTED",
                    "INTERVIEW_LINK_SENT",
                ]),
            )
            .order_by(JobCandidate.updated_at.desc())
        ).scalars().first()
        
        if not job_candidate and phone.startswith("91"):
            alt_phone = phone[2:]
            print(f"WEBHOOK: Try fallback without country code: {alt_phone}")
            job_candidate = db.execute(
                select(JobCandidate)
                .join(Candidate, Candidate.id == JobCandidate.candidate_id)
                .where(
                    Candidate.phone == alt_phone,
                    JobCandidate.recruitment_status.in_([
                        "CONTACTED",
                        "INTERESTED",
                        "INTERVIEW_LINK_SENT",
                    ]),
                )
                .order_by(JobCandidate.updated_at.desc())
            ).scalars().first()

    if not job_candidate:
        print(f"WEBHOOK: No active JobCandidate found for phone {phone} (ignored)")
        return {
            "status": "IGNORED",
            "reason": "No active JobCandidate found",
        }

    if provider_message_id:
        duplicate = db.scalar(
            select(CandidateContact.id)
            .where(CandidateContact.external_message_id == provider_message_id)
            .limit(1)
        )
        if duplicate:
            return {
                "status": "DUPLICATE",
                "job_candidate_id": str(job_candidate.id),
            }

    inbound = CandidateContact(
        job_candidate_id=job_candidate.id,
        channel="WHATSAPP",
        message_type="INBOUND",
        message=text_message,
        response_text=text_message,
        provider="NMVE",
        external_message_id=provider_message_id,
        status="REPLIED",
        responded_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
    )
    db.add(inbound)
    db.commit()

    intent = _classify_whatsapp_response(text_message)
    print(f"WEBHOOK: Intent detected = {intent} for message = '{text_message}'")
    inbound.response_intent = intent
    db.commit()

    target_phone = phone
    if not target_phone and job_candidate:
        cand = db.get(Candidate, job_candidate.candidate_id)
        if cand and cand.phone:
            target_phone = cand.phone

    if intent == "INTERESTED":
        print(f"WEBHOOK: Candidate is INTERESTED, updating status and sending CTA to {target_phone}...")
        job_candidate.recruitment_status = "INTERESTED"
        db.commit()

        sent = _send_whatsapp_cta_message(
            raw_phone=target_phone,
            job_candidate_id=str(job_candidate.id),
        )
        print(f"WEBHOOK: CTA send result = {sent}")

        if sent:
            job_candidate.recruitment_status = "INTERVIEW_LINK_SENT"
            db.add(
                CandidateContact(
                    job_candidate_id=job_candidate.id,
                    channel="WHATSAPP",
                    message_type="OUTBOUND",
                    message="Interview scheduling link sent",
                    provider="NMVE",
                    status="SENT",
                    sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
                )
            )
            db.commit()

    elif intent == "NOT_INTERESTED":
        job_candidate.recruitment_status = "NOT_INTERESTED"
        db.commit()

        ack_msg = (
            "Thank you for the update! 😊 We truly appreciate your time and consideration."
             "We will keep your profile on record for future opportunities that align with your experience and qualifications."
             "Wishing you continued success in your professional journey! 🌟"
        )
        sent = _send_whatsapp_text_message(
            raw_phone=target_phone,
            text_message=ack_msg,
        )
        db.add(
            CandidateContact(
                job_candidate_id=job_candidate.id,
                channel="WHATSAPP",
                message_type="OUTBOUND",
                message=ack_msg,
                provider="NMVE",
                status="SENT" if sent else "FAILED",
                sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) if sent else None,
            )
        )
        db.commit()

    else:
        clarification = (
            "Thank you for your response. Please reply YES if you are interested "
            "in proceeding with the opportunity, or NO if you are not interested."
        )
        sent = _send_whatsapp_text_message(
            raw_phone=target_phone,
            text_message=clarification,
        )
        db.add(
            CandidateContact(
                job_candidate_id=job_candidate.id,
                channel="WHATSAPP",
                message_type="OUTBOUND",
                message=clarification,
                provider="NMVE",
                status="SENT" if sent else "FAILED",
                sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) if sent else None,
            )
        )
        db.commit()

    candidate = db.get(Candidate, job_candidate.candidate_id)
    return {
        "status": intent,
        "job_candidate_id": str(job_candidate.id),
        "candidate_id": str(job_candidate.candidate_id),
        "candidate_name": candidate.name if candidate else "Unknown",
        "message": text_message,
    }


def schedule_candidate_interview(
    db: Session,
    job_candidate_id: UUID,
    scheduled_at: datetime,
    interview_link: str | None = None,
    is_reschedule: bool = False,
) -> dict:
    """Persist the selected interview appointment slot and dispatch WhatsApp confirmation.

    Detailed Functionality:
    1. Validation:
       - Verifies JobCandidate exists in database.
       - Disallows scheduling if candidate status is 'NOT_INTERESTED'.
    2. Reschedule Token Management:
       - If initial booking (`is_reschedule=False`), generates a secure UUID4 `reschedule_token`.
       - If rescheduling (`is_reschedule=True`), invalidates the token to enforce the business rule:
         candidates may only reschedule once.
    3. Interview State Persistence:
       - Stores `interview_scheduled_at` timestamp.
       - Sets interview link (sanitizing legacy Teams URLs to `https://interview.teckvalley.com/`).
       - Updates `recruitment_status = 'INTERVIEW_SCHEDULED'`.
       - Resets reminder flags: `reminder_24h_sent = False`, `reminder_1h_sent = False`.
    4. WhatsApp Confirmation Dispatch:
       - Formulates personalized confirmation message containing date, time, interview link,
         and one-click reschedule URL (`{FRONTEND_URL}/reschedule/{token}`).
       - Dispatches via `_send_whatsapp_text_message()`.
       - Records outbound entry in `candidate_contacts` table.

    Parameters:
        db (Session): Active SQLAlchemy database session.
        job_candidate_id (UUID): JobCandidate identifier.
        scheduled_at (datetime): Confirmed appointment datetime.
        interview_link (str | None): Candidate test URL (registrationUrl) or meeting link.
        is_reschedule (bool): True if triggered from the rescheduling portal.

    Returns:
        dict: Confirmation payload with scheduled timestamp, link, status, and delivery result.

    Raises:
        ValueError: If JobCandidate is not found or candidate previously declined ('NOT_INTERESTED').
    """
    job_candidate = db.get(JobCandidate, job_candidate_id)
    if not job_candidate:
        raise ValueError("JobCandidate not found")

    if job_candidate.recruitment_status == "NOT_INTERESTED":
        raise ValueError("Candidate is not interested")

    # Always store and format interview_scheduled_at in IST (naive wall-clock time)
    scheduled_at = _to_naive_ist(scheduled_at)

    import uuid
    from .config import settings
    
    if not is_reschedule:
        if not job_candidate.reschedule_token:
            job_candidate.reschedule_token = str(uuid.uuid4())
    else:
        # Candidate can only reschedule once. Invalidate token so it cannot be used again
        job_candidate.reschedule_token = None
        
    job_candidate.interview_scheduled_at = scheduled_at
    if interview_link:
        if "teams.microsoft.com" not in interview_link:
            job_candidate.interview_link = interview_link
        else:
            job_candidate.interview_link = "https://interview.teckvalley.com/"
    elif not job_candidate.interview_link or "teams.microsoft.com" in (job_candidate.interview_link or ""):
        job_candidate.interview_link = "https://interview.teckvalley.com/"
    job_candidate.recruitment_status = "INTERVIEW_SCHEDULED"
    job_candidate.reminder_24h_sent = False
    job_candidate.reminder_1h_sent = False
    db.commit()

    candidate = db.get(Candidate, job_candidate.candidate_id)

    date_text = scheduled_at.strftime("%d %b %Y, %I:%M %p")
    frontend_url = getattr(settings, 'FRONTEND_URL', 'https://nmhirexv3.vestaging.in/').rstrip('/')
    if not frontend_url or "localhost" in frontend_url or "127.0.0.1" in frontend_url:
        frontend_url = "https://nmhirexv3.vestaging.in/"
    
    if is_reschedule:
        message = (
            "Your interview has been rescheduled successfully! 🎉\n\n"
            f"📅 New Date & Time: {date_text}\n"
        )
        if job_candidate.interview_link:
            message += f"\n🔗 Interview Link:\n{job_candidate.interview_link}\n"
        message += "\nPlease be available a few minutes before the scheduled time."
    else:
        reschedule_link = f"{frontend_url}/reschedule/{job_candidate.reschedule_token}"
        message = (
            "Your interview has been scheduled successfully! 🎉\n\n"
            f"📅 Date & Time: {date_text}\n"
        )
        if job_candidate.interview_link:
            message += f"\n🔗 Interview Link:\n{job_candidate.interview_link}\n"
        message += f"\n🔄 Need to pick a new time? Reschedule here:\n{reschedule_link}\n"
        message += "\nPlease be available a few minutes before the scheduled time."

    sent = _send_whatsapp_text_message(
        raw_phone=candidate.phone if candidate else "",
        text_message=message,
    )

    db.add(
        CandidateContact(
            job_candidate_id=job_candidate.id,
            channel="WHATSAPP",
            message_type="OUTBOUND",
            message=message,
            provider="NMVE",
            status="SENT" if sent else "FAILED",
            sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) if sent else None,
        )
    )
    db.commit()

    return {
        "job_candidate_id": str(job_candidate.id),
        "candidate_id": str(job_candidate.candidate_id),
        "scheduled_at": scheduled_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "interview_link": interview_link,
        "status": job_candidate.recruitment_status,
        "confirmation_sent": sent,
    }


def get_outreach_candidates(
    db: Session,
    user_id: UUID,
    candidate_id: UUID | None = None,
) -> list[dict]:
    """Retrieve all candidates currently engaged in the WhatsApp outreach pipeline.

    Detailed Functionality:
    - Queries `job_candidates` joined with `jobs` and `candidates`.
    - Filters by active recruitment statuses ('CONTACTED', 'INTERESTED', 'NOT_INTERESTED',
      'INTERVIEW_LINK_SENT', 'INTERVIEW_SCHEDULED').
    - For each candidate:
      * Queries the latest inbound WhatsApp response from `candidate_contacts`.
      * Evaluates response intent (`_classify_whatsapp_response()`).
      * Queries the latest outbound delivery status (SENT, DELIVERED, FAILED).
      * Gathers interview schedule timestamps and meeting links.

    Parameters:
        db (Session): Active SQLAlchemy database session.
        user_id (UUID): Authenticated recruiter user ID.
        candidate_id (UUID | None): Optional filter for a specific candidate UUID.

    Returns:
        list[dict]: Array of pipeline candidate objects with comprehensive messaging and interview states.
    """
    q = (
        select(JobCandidate, Job, Candidate)
        .join(Job, Job.id == JobCandidate.job_id)
        .join(Candidate, Candidate.id == JobCandidate.candidate_id)
    )
    if candidate_id:
        q = q.where((Candidate.id == candidate_id) | (JobCandidate.id == candidate_id))
    else:
        q = q.where(
            JobCandidate.recruitment_status.in_([
                "CONTACTED",
                "INTERESTED",
                "NOT_INTERESTED",
                "INTERVIEW_LINK_SENT",
                "INTERVIEW_SCHEDULED",
            ])
        )

    rows = db.execute(q.order_by(JobCandidate.updated_at.desc())).all()

    results = []

    for jc, job, candidate in rows:
        latest_response = db.execute(
            select(CandidateContact)
            .where(
                CandidateContact.job_candidate_id == jc.id,
                CandidateContact.channel == "WHATSAPP",
                CandidateContact.message_type == "INBOUND",
            )
            .order_by(
                CandidateContact.created_at.desc()
            )
        ).scalars().first()

        response_text = (
            latest_response.response_text
            if latest_response
            else None
        )

        response_intent = (
            _classify_whatsapp_response(response_text)
            if response_text
            else None
        )

        responded_at = (
            latest_response.responded_at
            if latest_response
            else None
        )

        latest_outbound = db.scalar(
            select(CandidateContact)
            .where(
                CandidateContact.job_candidate_id == jc.id,
                CandidateContact.channel == "WHATSAPP",
                CandidateContact.message_type == "OUTBOUND",
            )
            .order_by(CandidateContact.created_at.desc())
            .limit(1)
        )
        c_status = (latest_outbound.status if latest_outbound else ("DELIVERED" if response_text or jc.recruitment_status in {"INTERESTED", "INTERVIEW_LINK_SENT", "INTERVIEW_SCHEDULED"} else "DELIVERED")).upper()
        if c_status == "FAILED":
            delivery_status = "FAILED"
        elif c_status in {"INVALID_PHONE", "SKIPPED", "SKIPPED_INVALID_PHONE"}:
            delivery_status = "INVALID_PHONE"
        else:
            delivery_status = "DELIVERED"

        # Exclude failed outreach invitations from default outreach list,
        # but allow viewing if specifically requested by candidate_id
        if delivery_status == "FAILED" and not candidate_id:
            continue

        results.append({
            "id": str(candidate.id),
            "job_id": str(job.id),
            "job_candidate_id": str(jc.id),
            "name": candidate.name or "Unnamed Candidate",
            "phone": candidate.phone or "",
            "job": job.title or "Unknown Role",
            "status": "FAILED" if delivery_status == "FAILED" else jc.recruitment_status,
            "delivery_status": delivery_status,
            "error_message": latest_outbound.message if (latest_outbound and c_status == "FAILED") else None,
            "interview_scheduled_at": (
                _to_naive_ist(jc.interview_scheduled_at).strftime("%Y-%m-%dT%H:%M:%S")
                if jc.interview_scheduled_at
                else None
            ),
            "interview_link": jc.interview_link,
            "score": (
                float(jc.overall_score)
                if jc.overall_score is not None
                else None
            ),
            "rank": jc.ranking_position,
            "response_text": response_text,
            "response_intent": response_intent,
            "responded_at": (
                responded_at.isoformat()
                if responded_at
                else None
            ),
        })

    return results


def update_candidate_status(
    db: Session,
    job_id: UUID,
    candidate_id: UUID,
    status: str,
    target_phone: str | None = None,
):
    """Update a candidate's recruitment lifecycle status and trigger outreach if transitioning to CONTACTED.

    Detailed Functionality:
    - Queries `JobCandidate` matching `job_id` and `candidate_id`.
    - Updates `recruitment_status` to the uppercase target status.
    - If status changes to 'CONTACTED' and previous status was not 'CONTACTED', automatically
      triggers initial WhatsApp template delivery via `_send_whatsapp_to_candidate()`.

    Parameters:
        db (Session): Active SQLAlchemy database session.
        job_id (UUID): Target job UUID.
        candidate_id (UUID): Target candidate UUID.
        status (str): New recruitment status (e.g. 'CONTACTED', 'REJECTED', 'SHORTLISTED').
        target_phone (str | None): Optional phone override.

    Returns:
        dict: Status update result containing old and new status.

    Raises:
        ValueError: If JobCandidate record is not found.
    """
    job_candidate = db.scalar(
        select(JobCandidate).where(
            JobCandidate.job_id == job_id,
            JobCandidate.candidate_id == candidate_id,
        )
    )

    if not job_candidate:
        raise ValueError("JobCandidate not found")

    old_status = job_candidate.recruitment_status
    job_candidate.recruitment_status = status.upper()
    db.commit()

    if status.upper() == "CONTACTED" and old_status != "CONTACTED":
        _send_whatsapp_to_candidate(
            db,
            job_id,
            candidate_id,
            target_phone,
        )

    return {
        "job_candidate_id": str(job_candidate.id),
        "candidate_id": str(candidate_id),
        "old_status": old_status,
        "status": job_candidate.recruitment_status,
    }


def bulk_update_candidate_status(
    db: Session,
    job_id: UUID,
    candidate_ids: list[UUID],
    status: str = "CONTACTED",
    target_phone: str | None = None,
) -> dict:
    """Batch-update recruitment status for a list of candidates and dispatch WhatsApp invites.

    Detailed Functionality:
    - Iterates over each candidate ID in `candidate_ids`.
    - Calls `update_candidate_status()` for each candidate.
    - Catches individual candidate failures gracefully so batch execution completes.
    - Aggregates success and failure counts.

    Parameters:
        db (Session): Active SQLAlchemy database session.
        job_id (UUID): Job requisition UUID.
        candidate_ids (list[UUID]): List of candidate UUIDs to update.
        status (str): Target status (default: 'CONTACTED').
        target_phone (str | None): Optional phone override for test runs.

    Returns:
        dict: Batch execution summary with total, sent, failed, and per-candidate results.
    """
    results = []
    sent_count = 0
    failed_count = 0

    for c_id in candidate_ids:
        try:
            res = update_candidate_status(
                db=db,
                job_id=job_id,
                candidate_id=c_id,
                status=status,
                target_phone=target_phone,
            )
            results.append({
                "candidate_id": str(c_id),
                "status": "SUCCESS",
                "detail": res,
            })
            sent_count += 1
        except Exception as exc:
            results.append({
                "candidate_id": str(c_id),
                "status": "FAILED",
                "error": str(exc),
            })
            failed_count += 1

    return {
        "job_id": str(job_id),
        "total": len(candidate_ids),
        "sent": sent_count,
        "failed": failed_count,
        "results": results,
    }


def get_all_candidates(db: Session, user_id: UUID) -> list[dict]:
    """Retrieve distinct candidates progressing through advanced interview stages.

    Detailed Functionality:
    - Queries candidates with statuses: 'INTERESTED', 'INTERVIEW_LINK_SENT', 'INTERVIEW',
      'INTERVIEW_SCHEDULED', 'INTERVIEW_COMPLETED'.
    - Performs in-memory deduplication so each candidate is listed once with their most recent job context.
    - Gathers interview appointment timestamps, test links, and contact history.

    Parameters:
        db (Session): Active SQLAlchemy database session.
        user_id (UUID): Authenticated recruiter user ID.

    Returns:
        list[dict]: Array of candidate profile dictionaries in interview stages.
    """
    rows = db.execute(
        select(Candidate, JobCandidate, Job)
        .join(
            JobCandidate,
            JobCandidate.candidate_id == Candidate.id,
        )
        .join(
            Job,
            Job.id == JobCandidate.job_id,
        )
        .where(
            JobCandidate.recruitment_status.in_([
                "INTERESTED",
                "INTERVIEW_LINK_SENT",
                "INTERVIEW",
                "INTERVIEW_SCHEDULED",
                "INTERVIEW_COMPLETED",
            ])
        )
        .order_by(
            Candidate.created_at.desc(),
            JobCandidate.created_at.desc(),
        )
    ).all()

    seen = set()
    results = []

    for candidate, jc, job in rows:
        if candidate.id in seen:
            continue
        seen.add(candidate.id)

        score = (
            float(jc.overall_score)
            if jc.overall_score is not None
            else 0
        )

        classification = (
            jc.classification
            or "External Match"
        )

        outbound_interview = (
            db.query(CandidateContact)
            .filter(
                CandidateContact.job_candidate_id == jc.id,
                CandidateContact.channel == "WHATSAPP",
                CandidateContact.message_type == "OUTBOUND",
                CandidateContact.message.contains(
                    "http"
                ),
            )
            .order_by(
                CandidateContact.created_at.desc()
            )
            .first()
        )

        interview_link = None
        if outbound_interview and outbound_interview.message:
            urls = re.findall(
                r"https?://\S+",
                outbound_interview.message,
            )
            if urls:
                interview_link = urls[0].rstrip(
                    "!.,)"
                )

        results.append({
            "id": str(candidate.id),
            "job_id": str(job.id),
            "job_candidate_id": str(jc.id),
            "name": candidate.name or "Unnamed",
            "email": candidate.email,
            "phone": candidate.phone,
            "location": candidate.location or "-",
            "exp": (
                f"{candidate.total_experience_years} yrs"
                if candidate.total_experience_years
                else "-"
            ),
            "score": score,
            "scoreLabel": classification,
            "job": job.title,
            "skills": (
                ", ".join(
                    [
                        (
                            item.get("name")
                            or item.get("skill")
                            or item.get("skill_name")
                            or str(item)
                        )
                        if isinstance(item, dict)
                        else str(item)
                        for item in (
                            candidate.normalized_profile.get(
                                "skills", []
                            )
                            if candidate.normalized_profile
                            else []
                        )[:5]
                    ]
                )
                or "-"
            ),
            "stage": jc.recruitment_status,
            "interview_link": jc.interview_link or interview_link,
            "interview_scheduled_at": (
                _to_naive_ist(jc.interview_scheduled_at).strftime("%Y-%m-%dT%H:%M:%S")
                if jc.interview_scheduled_at
                else None
            ),
            "experience_details": (
                candidate.normalized_profile.get(
                    "experiences", []
                )
                if candidate.normalized_profile
                else []
            ),
            "all_skills": (
                candidate.normalized_profile.get(
                    "skills", []
                )
                if candidate.normalized_profile
                else []
            ),
        })

    return results


# def get_outreach_campaigns(
#     db: Session,
#     user_id: UUID,
# ) -> dict:
#     """Return campaign metrics based on external ranking, not local rescoring."""
#     jobs = db.execute(
#         select(Job)
#         .where(Job.created_by == user_id)
#         .order_by(Job.created_at.desc())
#     ).scalars().all()

#     campaigns = []
#     total_contacted = 0
#     total_pending = 0
#     total_interested = 0

#     for job in jobs:
#         rows = db.execute(
#             select(JobCandidate)
#             .where(JobCandidate.job_id == job.id)
#         ).scalars().all()

#         eligible = sum(
#             1
#             for jc in rows
#             if jc.is_shortlisted
#         )

#         contacted = sum(
#             1
#             for jc in rows
#             if jc.is_shortlisted
#             and jc.recruitment_status in {
#                 "CONTACTED",
#                 "INTERESTED",
#                 "INTERVIEW_LINK_SENT",
#                 "INTERVIEW",
#                 "INTERVIEW_SCHEDULED",
#                 "INTERVIEW_COMPLETED",
#             }
#         )

#         interested = sum(
#             1
#             for jc in rows
#             if jc.recruitment_status in {
#                 "INTERESTED",
#                 "INTERVIEW_LINK_SENT",
#                 "INTERVIEW",
#                 "INTERVIEW_SCHEDULED",
#                 "INTERVIEW_COMPLETED",
#             }
#         )

#         pending = max(eligible - contacted, 0)

#         if contacted == 0:
#             status = "Draft"
#         elif contacted >= eligible and eligible > 0:
#             status = "Completed"
#         else:
#             status = "Active"

#         campaigns.append({
#             "id": str(job.id),
#             "name": job.title,
#             "job": job.title,
#             "created": (
#                 job.created_at.strftime("%b %d, %Y")
#                 if job.created_at
#                 else ""
#             ),
#             "rule": f"Top {eligible} ranked candidates",
#             "eligible": eligible,
#             "contacted": contacted,
#             "interested": interested,
#             "status": status,
#             "searchStr": f"{job.title} {status}".lower(),
#         })

#         total_contacted += contacted
#         total_pending += pending
#         total_interested += interested

#     return {
#         "metrics": {
#             "contacted": total_contacted,
#             "pending": total_pending,
#             "interested": total_interested,
#             "tests_assigned": 0,
#         },
#         "campaigns": campaigns,
#     }


# ============================================================
# NAUKRI-STYLE CANDIDATE ARRAY OUTREACH
# Accepts a plain list of {no, name, role, phone} from the
# recruiter, creates Candidate + JobCandidate rows if they
# don't exist, sends the shortlisting WhatsApp template, and
# records every message in candidate_contacts.
# ============================================================

def _is_automate_yes(val) -> bool:
    """Evaluate whether the recruiter opted for automated WhatsApp message dispatch.

    Detailed Functionality:
    - Interprets the `automate` flag passed to `/api/outreach/send-array` or functions.
    - True/yes values: True, "yes", "y", "true", "1", "auto" -> triggers immediate WhatsApp sending.
    - False/no values: False, "no", "n", "false", "0" -> queues candidates as 'PENDING_OUTREACH'
      for manual recruiter review and batch selection on `/outreach/pending`.
    - Defaults to True if value is None.

    Parameters:
        val (Any): Input flag value (string, boolean, integer, or None).

    Returns:
        bool: True if automated sending is active, False if queued for manual review.
    """
    if val is None:
        return True
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in ("yes", "y", "true", "1", "auto")


def send_naukri_outreach_array( 
    db: Session,
    job_id: UUID | None = None,
    candidates: list[dict] | None = None,
    company_name: str | None = None,
    user_id: UUID | None = None,
    automate: str | bool | None = "yes",
) -> dict:
    """Process an array of candidate records imported from Naukri, ATS, or recruiter spreadsheets.
    Detailed End-to-End Workflow:
    1. Requisition & Entity Upsert:
       - If `job_id` is provided, attaches candidates to that job; otherwise dynamically finds or
         creates a Job record matching each candidate's applied role title.
       - Matches candidates by phone or email to prevent duplicate candidate records.
       - Stores rich metadata: name, phone, email, current company, role, and experience years.
       - Upserts `JobCandidate` association record.
    2. Branching Logic based on `automate`:
       - If `automate="yes"` (or True):
         * Formulates WhatsApp template (`candidate_shortlisted_outreach`) with candidate name, role, and company.
         * Dispatches WhatsApp message immediately via Key 1 (`WHATSAPP_MESSAGE_API_KEY`).
         * On success: sets `recruitment_status = 'CONTACTED'` and logs outbound in `candidate_contacts`.
         * On failure: sets `recruitment_status = 'FAILED'`.
       - If `automate="no"` (or False):
         * Bypasses immediate WhatsApp dispatch.
         * Sets `recruitment_status = 'PENDING_OUTREACH'`.
         * Candidate appears on the `/outreach/pending` frontend page for recruiter selection.
    3. Resilient Error Handling:
       - Skips entries missing mandatory name or phone numbers.
       - Catches individual candidate exceptions without interrupting batch processing.
    4. Execution Summary:
       - Returns counts of sent, queued, skipped, and failed candidates along with per-candidate details.

    Parameters:
        db (Session): Active SQLAlchemy database session.
        job_id (UUID | None): Optional target job UUID.
        candidates (list[dict] | None): Array of candidate input dicts (keys: name, phone/no/contact_no, role, email, etc.).
        company_name (str | None): Recruiting company name displayed in template text.
        user_id (UUID | None): Authenticated recruiter user ID.
        automate (str | bool | None): 'yes' for instant WhatsApp send; 'no' for pending queue.

    Returns:
        dict: Execution summary containing counts (total, sent, queued, skipped, failed) and candidate items.
    """
    _company = company_name or getattr(settings, "WHATSAPP_COMPANY_NAME", "the company")

    job = db.get(Job, job_id) if job_id else None

    callback_url = getattr(
        settings,
        "WHATSAPP_CALLBACK_URL",
        "https://nmhirex.onrender.com/api/webhooks/whatsapp",
    )
    template_name = getattr(settings, "WHATSAPP_TEMPLATE_NAME", "candidate_shortlisted_outreach")
    template_language = getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en")

    candidate_list = candidates or []
    results = []
    sent_count = 0
    queued_count = 0
    failed_count = 0
    skipped_count = 0

    for idx, item in enumerate(candidate_list):
        name = str(item.get("user_name") or item.get("name") or "").strip()
        email = str(item.get("email") or "").strip()
        company = str(item.get("current_company") or item.get("company") or "").strip()
        role = str(item.get("job_role") or item.get("role") or (job.title if job else "")).strip()
        requisition_id = str(item.get("requisition_id") or (job.requisition_id if job and job.requisition_id else "") or "").strip()
        raw_exp = item.get("experience") or item.get("total_experience_years")
        parsed_exp = None
        if raw_exp is not None and str(raw_exp).strip() != "":
            try:
                parsed_exp = Decimal(str(raw_exp).strip())
            except Exception:
                parsed_exp = None

        # Phone can be provided as phone_no, "phone no", contact_no, phone, phone_number, mobile, or legacy 'no'
        raw_phone = str(
            item.get("phone_no")
            or item.get("phone no")
            or item.get("contact_no")
            or item.get("phone")
            or item.get("phone_number")
            or item.get("mobile")
            or ""
        ).strip()
        serial_no = item.get("no", idx + 1)
        if not raw_phone and item.get("no") is not None:
            raw_no = item.get("no")
            no_val = str(raw_no).strip()
            digits = "".join(filter(str.isdigit, no_val))
            if (isinstance(raw_no, str) and no_val) or len(digits) >= 4:
                raw_phone = no_val
                serial_no = idx + 1

        missing_fields = []
        if not name:
            missing_fields.append("user_name")
        if not email:
            missing_fields.append("email")
        if not raw_phone:
            missing_fields.append("phone_no")
        if not company:
            missing_fields.append("current_company")
        if not role:
            missing_fields.append("job_role")
        if not requisition_id:
            missing_fields.append("requisition_id")

        if missing_fields:
            results.append({
                "no": serial_no,
                "name": name,
                "phone": raw_phone,
                "status": "SKIPPED_MISSING_FIELDS",
                "missing_fields": missing_fields,
                "error": f"Missing mandatory fields: {', '.join(missing_fields)}",
            })
            skipped_count += 1
            continue

        # ----------------------------------------------------------------
        # Database-only global check by phone OR email (valid / failed / invalid)
        # ----------------------------------------------------------------
        clean_phone = _clean_whatsapp_phone(raw_phone)
        matching_candidates = _find_matching_candidates(
            db,
            raw_phone=raw_phone,
            clean_phone=clean_phone,
            email=email,
        )
        primary_match = matching_candidates[0] if matching_candidates else None

        if matching_candidates and _candidates_have_contact_history(
            db,
            [c.id for c in matching_candidates],
        ):
            results.append({
                "no": serial_no,
                "name": name or (primary_match.name if primary_match else ""),
                "phone": clean_phone or raw_phone or (primary_match.phone if primary_match else ""),
                "status": "SKIPPED_ALREADY_CONTACTED",
                "candidate_id": str(primary_match.id),
            })
            skipped_count += 1
            continue

        effective_raw_phone = raw_phone or (primary_match.phone if primary_match and primary_match.phone else "")
        clean_phone = _clean_whatsapp_phone(effective_raw_phone)

        # ----------------------------------------------------------------
        # Find or create Job for this candidate if job_id was not provided
        # ----------------------------------------------------------------
        current_job_id = job.id if job else None
        if not current_job_id:
            role_job = db.scalar(select(Job).where(Job.title == role).limit(1))
            if not role_job:
                creator_id = user_id
                if not creator_id:
                    u = db.scalar(select(User).limit(1))
                    if not u:
                        from .auth import hash_password
                        u = User(
                            name="NM-HireX Admin",
                            email="admin@nmhirex.com",
                            password_hash=hash_password("Admin@123456"),
                            role="ADMIN",
                            status="APPROVED",
                            is_active=True,
                        )
                        db.add(u)
                        db.flush()
                    creator_id = u.id

                role_job = Job(
                    title=role,
                    status="ACTIVE",
                    created_by=creator_id,
                )
                db.add(role_job)
                db.flush()
            current_job_id = role_job.id

        if not clean_phone:
            # Persist candidate so they appear in Dashboard with Invalid Phone / Skipped status
            cand_skipped = _upsert_candidate(
                db,
                primary_match,
                name=name,
                phone=effective_raw_phone,
                role=role,
                email=email,
                company=company,
                parsed_exp=parsed_exp,
                requisition_id=requisition_id,
            )

            jc_skipped = db.scalar(
                select(JobCandidate).where(
                    JobCandidate.job_id == current_job_id,
                    JobCandidate.candidate_id == cand_skipped.id,
                )
            )
            if jc_skipped is None:
                jc_skipped = JobCandidate(
                    job_id=current_job_id,
                    candidate_id=cand_skipped.id,
                    is_shortlisted=True,
                    recruitment_status="SKIPPED",
                    requisition_id=requisition_id,
                )
                db.add(jc_skipped)
                db.flush()
            elif requisition_id and not jc_skipped.requisition_id:
                jc_skipped.requisition_id = requisition_id

            _record_invalid_phone_contact(db, jc_skipped)

            results.append({
                "no": serial_no,
                "name": name,
                "phone": effective_raw_phone,
                "status": "SKIPPED_INVALID_PHONE",
                "candidate_id": str(cand_skipped.id),
                "job_candidate_id": str(jc_skipped.id),
            })
            skipped_count += 1
            continue


        # ----------------------------------------------------------------
        # Find or create Candidate (matched by phone or email; only 1 record)
        # ----------------------------------------------------------------
        candidate = _upsert_candidate(
            db,
            primary_match,
            name=name,
            phone=clean_phone,
            role=role,
            email=email,
            company=company,
            parsed_exp=parsed_exp,
            requisition_id=requisition_id,
        )

        # ----------------------------------------------------------------
        # Find or create JobCandidate
        # ----------------------------------------------------------------
        job_candidate = db.scalar(
            select(JobCandidate).where(
                JobCandidate.job_id == current_job_id,
                JobCandidate.candidate_id == candidate.id,
            )
        )
        if job_candidate is None:
            job_candidate = JobCandidate(
                job_id=current_job_id,
                candidate_id=candidate.id,
                is_shortlisted=True,
                recruitment_status="NEW",
                requisition_id=requisition_id,
            )
            db.add(job_candidate)
            db.flush()
        elif requisition_id:
            job_candidate.requisition_id = requisition_id

        # ----------------------------------------------------------------
        # Check automation setting ('automate': 'yes' | 'no')
        # If 'no': Candidate is stored in DB for manual review & outreach
        # on the Pending Outreach page. Do NOT send WhatsApp message now.
        # ----------------------------------------------------------------
        item_automate = item.get("automate", automate)
        if not _is_automate_yes(item_automate):
            job_candidate.recruitment_status = "PENDING_OUTREACH"
            db.commit()

            results.append({
                "no": serial_no,
                "name": name,
                "phone": clean_phone,
                "status": "QUEUED_MANUAL",
                "candidate_id": str(candidate.id),
                "job_candidate_id": str(job_candidate.id),
            })
            queued_count += 1
            continue

        # ----------------------------------------------------------------
        # Determine actual destination phone (test mode override)
        # ----------------------------------------------------------------
        dest_phone = clean_phone
        if getattr(settings, "WHATSAPP_TEST_MODE", False):
            test_number = getattr(settings, "WHATSAPP_TEST_NUMBER", "")
            dest_phone = _clean_whatsapp_phone(test_number) or ""
            if not dest_phone:
                results.append({
                    "no": serial_no,
                    "name": name,
                    "phone": clean_phone,
                    "status": "FAILED_TEST_MODE_NO_NUMBER",
                })
                failed_count += 1
                continue

        # ----------------------------------------------------------------
        # Build and send the WhatsApp template payload
        # ----------------------------------------------------------------
        reference_id = f"NMHireX-{str(job_candidate.id)}"

        payload = {
            "to": dest_phone,
            "templateName": template_name,
            "bodyParams": [name, role, _company, _company],
            "languageCode": template_language,
            "referenceId": reference_id,
            "callbackUrl": callback_url,
        }

        print(f"[NaukriOutreach] Sending to {dest_phone} for candidate '{name}' ...")
        success, response_body, provider_message_id = _send_whatsapp_payload(payload)

        if not success:
            failed_contact = CandidateContact(
                job_candidate_id=job_candidate.id,
                channel="WHATSAPP",
                message_type="OUTBOUND",
                message=f"Failed to send template {template_name}: {response_body}",
                provider="NMVE",
                status="FAILED",
                sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
            )
            db.add(failed_contact)
            job_candidate.recruitment_status = "FAILED"
            db.commit()

            results.append({
                "no": serial_no,
                "name": name,
                "phone": clean_phone,
                "status": "FAILED_SEND",
                "candidate_id": str(candidate.id),
                "job_candidate_id": str(job_candidate.id),
            })
            failed_count += 1
            continue

        # ----------------------------------------------------------------
        # Persist the outbound message
        # ----------------------------------------------------------------
        contact = CandidateContact(
            job_candidate_id=job_candidate.id,
            channel="WHATSAPP",
            message_type="OUTBOUND",
            message=f"Shortlisting template sent: {template_name}",
            provider="NMVE",
            external_message_id=provider_message_id,
            status="DELIVERED",
            sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
            delivered_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
        )
        db.add(contact)

        if job_candidate.recruitment_status in {None, "NEW", "SHORTLISTED", "PENDING_OUTREACH", "FAILED", "SKIPPED"}:
            job_candidate.recruitment_status = "CONTACTED"

        db.commit()

        results.append({
            "no": serial_no,
            "name": name,
            "phone": clean_phone,
            "status": "SENT",
            "candidate_id": str(candidate.id),
            "job_candidate_id": str(job_candidate.id),
        })
        sent_count += 1

    return {
        "job_id": str(job_id) if job_id else None,
        "total": len(candidate_list),
        "sent": sent_count,
        "queued": queued_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "results": results,
    }


def get_pending_outreach_candidates(
    db: Session,
    user_id: UUID | None = None,
) -> list[dict]:
    """Retrieve candidates queued for manual outreach review on the frontend portal.

    Detailed Functionality:
    - Queries all candidates where `JobCandidate.recruitment_status == 'PENDING_OUTREACH'`.
    - Double checks that no prior outbound contact history exists (`_candidate_has_contact_history()`).
    - Formats candidates with contact details, current company, applied role, experience,
      and job requisition metadata.
    - Used directly by `GET /api/outreach/pending` to populate the table on `/outreach/pending`.

    Parameters:
        db (Session): Active SQLAlchemy database session.
        user_id (UUID | None): Optional recruiter ID filter.

    Returns:
        list[dict]: Array of candidates awaiting recruiter outreach selection.
    """
    query = (
        select(Candidate, JobCandidate, Job)
        .join(JobCandidate, JobCandidate.candidate_id == Candidate.id)
        .outerjoin(Job, Job.id == JobCandidate.job_id)
        .where(JobCandidate.recruitment_status == "PENDING_OUTREACH")
        .order_by(JobCandidate.created_at.desc(), Candidate.created_at.desc())
    )

    rows = db.execute(query).all()
    results = []
    seen = set()

    for candidate, jc, job in rows:
        if jc.id in seen:
            continue
        seen.add(jc.id)

        # Confirm no outbound contact has been made globally (by ID, phone, or email)
        has_outbound = _candidate_has_contact_history(db, candidate.id)
        if has_outbound:
            continue

        results.append({
            "job_candidate_id": str(jc.id),
            "candidate_id": str(candidate.id),
            "job_id": str(job.id) if job else None,
            "name": candidate.name or "Unnamed Candidate",
            "phone": candidate.phone or "",
            "email": candidate.email or "",
            "company": candidate.current_company or "",
            "role": candidate.current_role or (job.title if job else "Software Engineer"),
            "experience": float(candidate.total_experience_years) if candidate.total_experience_years is not None else None,
            "requisition_id": jc.requisition_id or candidate.requisition_id or (str(job.requisition_id) if job and job.requisition_id else None),
            "status": jc.recruitment_status,
            "created_at": jc.created_at.isoformat() if jc.created_at else None,
        })

    return results


def send_pending_outreach_candidates(
    db: Session,
    job_candidate_ids: list[UUID],
) -> dict:
    """Manually dispatch WhatsApp template invitations to recruiter-selected pending candidates.

    Detailed Functionality:
    - Called when a recruiter selects one, multiple, or all candidates on `/outreach/pending`
      and clicks the WhatsApp icon action button (`POST /api/outreach/send-pending`).
    - For each selected `JobCandidate`:
      * Verifies candidate exists and has not already received an outreach invite.
      * Normalizes candidate phone number via `_clean_whatsapp_phone()`.
      * Builds template payload for `candidate_shortlisted_outreach` with candidate name,
        applied role, and company name.
      * Dispatches template via Key 1 (`WHATSAPP_MESSAGE_API_KEY`).
      * On delivery success: updates `recruitment_status = 'CONTACTED'`, logs outbound in `candidate_contacts`.
      * The subsequent flow (candidate WhatsApp reply -> interest classification ->
        slot booking -> gateway add_candidate via Key 2 -> confirmation WhatsApp via Key 1)
        continues identically to automated mode.

    Parameters:
        db (Session): Active SQLAlchemy database session.
        job_candidate_ids (list[UUID]): List of selected `JobCandidate` UUIDs to contact.

    Returns:
        dict: Dispatch summary with total, sent, failed, and skipped counts.
    """
    callback_url = getattr(
        settings,
        "WHATSAPP_CALLBACK_URL",
        "https://nmhirex.onrender.com/api/webhooks/whatsapp",
    )
    template_name = getattr(settings, "WHATSAPP_TEMPLATE_NAME", "candidate_shortlisted_outreach")
    template_language = getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en")
    default_company = getattr(settings, "WHATSAPP_COMPANY_NAME", "the company")

    results = []
    sent_count = 0
    failed_count = 0
    skipped_count = 0

    for jc_id in job_candidate_ids:
        jc = db.get(JobCandidate, jc_id)
        if not jc:
            results.append({
                "job_candidate_id": str(jc_id),
                "status": "NOT_FOUND",
                "error": "Job candidate record not found",
            })
            failed_count += 1
            continue

        candidate = db.get(Candidate, jc.candidate_id)
        if not candidate:
            results.append({
                "job_candidate_id": str(jc_id),
                "status": "NOT_FOUND",
                "error": "Candidate record not found",
            })
            failed_count += 1
            continue

        job = db.get(Job, jc.job_id) if jc.job_id else None

        # Check if already contacted globally (by candidate ID, phone, or email)
        if _candidate_has_contact_history(db, candidate.id):
            results.append({
                "job_candidate_id": str(jc.id),
                "name": candidate.name,
                "phone": candidate.phone,
                "status": "SKIPPED_ALREADY_CONTACTED",
            })
            skipped_count += 1
            continue

        clean_phone = _clean_whatsapp_phone(candidate.phone)
        if not clean_phone:
            _record_invalid_phone_contact(db, jc)
            results.append({
                "job_candidate_id": str(jc.id),
                "name": candidate.name,
                "phone": candidate.phone,
                "status": "INVALID_PHONE",
            })
            failed_count += 1
            continue

        # Test mode override
        dest_phone = clean_phone
        if getattr(settings, "WHATSAPP_TEST_MODE", False):
            test_number = getattr(settings, "WHATSAPP_TEST_NUMBER", "")
            dest_phone = _clean_whatsapp_phone(test_number) or ""
            if not dest_phone:
                results.append({
                    "job_candidate_id": str(jc.id),
                    "name": candidate.name,
                    "phone": clean_phone,
                    "status": "FAILED_TEST_MODE_NO_NUMBER",
                })
                failed_count += 1
                continue

        name = candidate.name or "Candidate"
        role = candidate.current_role or (job.title if job else "the position")
        company = default_company or "Virtual Employee Pvt Ltd"

        reference_id = f"NMHireX-{str(jc.id)}"
        payload = {
            "to": dest_phone,
            "templateName": template_name,
            "bodyParams": [name, role, company, company],
            "languageCode": template_language,
            "referenceId": reference_id,
            "callbackUrl": callback_url,
        }

        print(f"[PendingOutreach] Sending WhatsApp to {dest_phone} for candidate '{name}' ...")
        success, response_body, provider_message_id = _send_whatsapp_payload(payload)

        if not success:
            failed_contact = CandidateContact(
                job_candidate_id=jc.id,
                channel="WHATSAPP",
                message_type="OUTBOUND",
                message=f"Failed to send template {template_name}: {response_body}",
                provider="NMVE",
                status="FAILED",
                sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
            )
            db.add(failed_contact)
            jc.recruitment_status = "FAILED"
            db.commit()

            results.append({
                "job_candidate_id": str(jc.id),
                "name": name,
                "phone": clean_phone,
                "status": "FAILED_SEND",
                "error": str(response_body),
            })
            failed_count += 1
            continue

        # Success: persist outbound contact
        contact = CandidateContact(
            job_candidate_id=jc.id,
            channel="WHATSAPP",
            message_type="OUTBOUND",
            message=f"Shortlisting template sent: {template_name}",
            provider="NMVE",
            external_message_id=provider_message_id,
            status="DELIVERED",
            sent_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
            delivered_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
        )
        db.add(contact)
        jc.recruitment_status = "CONTACTED"
        db.commit()

        results.append({
            "job_candidate_id": str(jc.id),
            "name": name,
            "phone": clean_phone,
            "status": "SENT",
        })
        sent_count += 1

    return {
        "total": len(job_candidate_ids),
        "sent": sent_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "results": results,
    }
