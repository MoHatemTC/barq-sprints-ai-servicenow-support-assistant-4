"""
S3.6 — Fixture KB article seeder.

Populates the Qdrant collection with 24 fixture knowledge-base articles —
one per unique KB number referenced in benchmark_dataset.json's answerable
cases (read off each case's `notes` field, e.g. "Matches KB0010010 'No
internet connection'"). This lets the existing, unmodified dataset run
against a real, populated collection without needing real ServiceNow KB
content, which isn't available in this environment. Negative-control
cases need no matching article and are unaffected.

Reuses the real chunking pipeline (ingestion/chunker.py) and the SAME
embedding-backend selection as pipeline_benchmark.py / run_benchmark.py
(real Gemini key, or the Sprints LiteLLM proxy fallback) rather than the
raw-GEMINI_API_KEY-only ingestion/embedding.py, so this works with
whichever real credential is configured in .env. Requires GEMINI_API_KEY
or LITELLM_BASE_URL + LITELLM_API_KEY to be set — stub embeddings cannot
populate this collection meaningfully (see pipeline_benchmark.py header).

Safe to re-run: point IDs are deterministic (article + chunk index), so
re-running updates existing points instead of duplicating them.

Usage:
    uv run python benchmark/seed_fixtures.py
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# WORKAROUND: barq_ai_support.ingestion.embedding constructs a genai.Client
# unconditionally at import time using os.environ["GEMINI_API_KEY"], and
# raises if it's empty -- even though we never call that module's function
# here (we use run_benchmark's embedding-fn selection instead, which
# correctly supports the LiteLLM proxy fallback). Importing qdrant_store
# below transitively imports embedding.py, so we set a throwaway value
# just long enough for that import to succeed, then remove it immediately
# so the REAL backend selection below (Gemini vs. LiteLLM proxy) still
# reflects what's actually configured in .env. This placeholder is never
# sent to any API. The real fix is to make embedding.py's client lazy;
# worth doing in a follow-up PR, not touched here to keep this script
# self-contained.
_had_gemini_key = bool(os.environ.get("GEMINI_API_KEY"))
if not _had_gemini_key:
    os.environ["GEMINI_API_KEY"] = "unused-import-time-placeholder"

from qdrant_client.models import PointStruct  # noqa: E402
from qdrant_client import models as qmodels  # noqa: E402

from barq_ai_support.ingestion.chunker import chunk_articles  # noqa: E402
from barq_ai_support.ingestion.qdrant_store import (  # noqa: E402
    COLLECTION_NAME,
    client,
    create_collection,
    generate_point_id,
)

from run_benchmark import get_embedding_fn, embedding_model_name  # noqa: E402

if not _had_gemini_key:
    os.environ.pop("GEMINI_API_KEY", None)


def _article(sys_id: str, number: str, category: str, title: str, body_html: str) -> dict:
    return {
        "sys_id": sys_id,
        "number": number,
        "category": category,
        "short_description": title,
        "text": f"<h1>{title}</h1>{body_html}",
    }


FIXTURE_ARTICLES = [
    _article("fx-0010010", "KB0010010", "Network & Remote Access", "No internet connection",
             "<p>Check the physical or Wi-Fi connection first. Restart the router/modem. "
             "Run network troubleshooter. Flush DNS with ipconfig /flushdns. "
             "Reconnect to the network and confirm browser and cloud apps load.</p>"),
    _article("fx-0010082", "KB0010082", "Network & Remote Access",
             "VPN authentication failure after password reset",
             "<p>After a domain password reset, the VPN client caches the old credentials. "
             "Sign out of the VPN client fully, clear saved credentials, restart the client, "
             "and sign in again with the new password. Reconnect and confirm access.</p>"),
    _article("fx-0010015", "KB0010015", "Email & Collaboration", "Outlook or mail client not opening",
             "<p>Start Outlook in safe mode (outlook.exe /safe) to rule out a bad add-in. "
             "If it opens, disable recent add-ins from Outlook Options. "
             "If it still fails, repair the Office installation via Control Panel.</p>"),
    _article("fx-0010077", "KB0010077", "Email & Collaboration", "Email not sending or receiving",
             "<p>Confirm webmail shows the same issue (rules out just the desktop client). "
             "Check mailbox storage quota isn't full. Verify outbound/inbound mail flow status "
             "on the mail server. Have the user resend a test message after each check.</p>"),
    _article("fx-0010021", "KB0010021", "Printing & Peripherals", "Printer offline or cannot print",
             "<p>Confirm the printer is powered on and network-connected. Restart the print "
             "spooler service. Remove and re-add the printer. Print a test page to confirm.</p>"),
    _article("fx-0010025", "KB0010025", "Files & Cloud", "OneDrive or SharePoint sync failure",
             "<p>Check available disk space and internet connectivity. Pause and resume OneDrive "
             "sync from the tray icon. If files stay stuck, sign out of OneDrive and back in to "
             "force a fresh sync handshake.</p>"),
    _article("fx-0010026", "KB0010026", "Security", "Phishing email reported",
             "<p>Do not click any links or open attachments in the reported message. Use the "
             "'Report Phishing' add-in to forward it to the security team. Delete the message "
             "after reporting. If a link was already clicked, escalate immediately.</p>"),
    _article("fx-0010027", "KB0010027", "Security", "Suspected malware infection",
             "<p>Disconnect the device from the network immediately to contain spread. Do not "
             "shut down (preserves memory for analysis). Escalate to security for a full scan "
             "and remediation before reconnecting the device.</p>"),
    _article("fx-0010030", "KB0010030", "Applications", "Teams or Zoom audio-video issue",
             "<p>Check OS-level microphone/camera permissions for the app. Select the correct "
             "input/output device in the app's audio settings. Restart the app. If on a call, "
             "leave and rejoin the meeting to reset the media connection.</p>"),
    _article("fx-0010031", "KB0010031", "Applications", "Software update failure",
             "<p>Check available disk space (updates fail silently when the disk is full). "
             "Clear the update cache and retry. If it still fails, download the standalone "
             "installer and run it directly instead of the automatic updater.</p>"),
    _article("fx-0010033", "KB0010033", "Devices & Performance", "Low disk space",
             "<p>Run Disk Cleanup to clear temp files and old update caches. Empty the Recycle "
             "Bin. Check for large files in Downloads. Move infrequently used files to cloud "
             "storage or an external drive.</p>"),
    _article("fx-0010034", "KB0010034", "Devices & Performance", "Computer freezes or crashes",
             "<p>Check Event Viewer for the error around the crash time. Update graphics and "
             "chipset drivers. Run a memory diagnostic (mdsched.exe). If it persists, boot in "
             "safe mode to rule out a third-party driver conflict.</p>"),
    _article("fx-0010084", "KB0010084", "Devices & Performance", "Laptop slow since recent update",
             "<p>Check Task Manager for a runaway process right after the update (common cause). "
             "Disable unnecessary startup apps. Confirm the update fully finished installing "
             "rather than being stuck mid-install.</p>"),
    _article("fx-0010080", "KB0010080", "Identity & Access", "Access denied to resource",
             "<p>Confirm the user's current group membership matches the resource's required "
             "permission group — this commonly breaks after a team transfer when old group "
             "membership is removed before new access is granted. Request the correct group "
             "via the access management workflow.</p>"),
    _article("fx-0010088", "KB0010088", "Identity & Access", "Access denied to resource (duplicate)",
             "<p>Confirm the user's current group membership matches the resource's required "
             "permission group — this commonly breaks after a team transfer. Request the "
             "correct group via the access management workflow.</p>"),
    _article("fx-0010081", "KB0010081", "Identity & Access", "Locked out, unable to sign in",
             "<p>Repeated failed logins trigger an account lockout after a threshold. Verify "
             "identity, unlock the account in the directory, and have the user sign in with "
             "the correct credentials. Suggest a password reset if they're unsure of it.</p>"),
    _article("fx-0010092", "KB0010092", "Identity & Access", "Locked out, unable to sign in (duplicate)",
             "<p>Repeated failed logins trigger a lockout after a threshold. Verify identity, "
             "unlock the account, and confirm sign-in with correct credentials.</p>"),
    _article("fx-0010085", "KB0010085", "Identity & Access", "Forgotten password",
             "<p>Verify the user's identity via the standard challenge. Trigger a self-service "
             "password reset link to their recovery email/phone. Once reset, confirm they can "
             "sign in to both the workstation and the web portal/mailbox.</p>"),
    _article("fx-0010087", "KB0010087", "Identity & Access", "Forgotten password (duplicate)",
             "<p>Verify identity, then trigger a self-service password reset link. Confirm "
             "sign-in works afterward for both workstation and mailbox access.</p>"),
    _article("fx-0010079", "KB0010079", "Files & Cloud", "Mapped network drive missing after login",
             "<p>Mapped drives can fail to reconnect after a reboot if the login script ran "
             "before the network was fully up. Manually remap the drive, or sign out and back "
             "in once the network connection is confirmed stable.</p>"),
    _article("fx-0010093", "KB0010093", "Files & Cloud", "Mapped network drive missing after login (duplicate)",
             "<p>Mapped drives can fail to reconnect after a reboot. Manually remap the drive, "
             "or sign out and back in once network connectivity is confirmed.</p>"),
    _article("fx-0010094", "KB0010094", "Identity & Access", "MFA failure after phone change",
             "<p>A new phone means the old MFA authenticator app no longer has the registered "
             "token. Re-enroll MFA using a backup code or admin-assisted reset, then register "
             "the authenticator app on the new device.</p>"),
    _article("fx-0010074", "KB0010074", "Applications", "SAP RFC_ERROR_COMMUNICATION timeout",
             "<p>This error indicates the SAP GUI can't reach the backend application server. "
             "Confirm VPN/network path to the SAP server is up. Check the SAP router service "
             "status. Retry the connection after confirming network path is healthy.</p>"),
    _article("fx-0010097", "KB0010097", "Applications", "SAP RFC_ERROR_COMMUNICATION timeout (duplicate)",
             "<p>Indicates the SAP GUI can't reach the backend server. Confirm the network path "
             "and SAP router service status before retrying the connection.</p>"),
]


def main() -> None:
    embedding_fn = get_embedding_fn(use_real=True)
    print(f"Using embedding backend: {embedding_model_name(embedding_fn)}")

    create_collection()

    # retrieve()'s category filter requires a payload index on "category";
    # create_collection() doesn't set one up. Safe to re-run: Qdrant no-ops
    # (or errors harmlessly, which we swallow) if the index already exists.
    try:
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="category",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )
        print("Created payload index on 'category'.")
    except Exception as exc:  # noqa: BLE001
        print(f"(payload index on 'category' likely already exists: {exc})")

    chunks = chunk_articles(FIXTURE_ARTICLES, chunk_size=400, overlap=50)
    print(f"Generated {len(chunks)} chunks from {len(FIXTURE_ARTICLES)} fixture articles.")

    points = []
    for i, chunk in enumerate(chunks, 1):
        vector = embedding_fn(chunk["text"])
        points.append(
            PointStruct(id=generate_point_id(chunk), vector=vector, payload=chunk["metadata"])
        )
        print(f"  embedded {i}/{len(chunks)}: {chunk['metadata']['number']} (chunk {chunk['metadata']['chunk_index']})")

    client.upsert(collection_name=COLLECTION_NAME, points=points)
    count = client.count(collection_name=COLLECTION_NAME)
    print(f"\nUpserted {len(points)} points. Collection '{COLLECTION_NAME}' now has {count.count} points total.")


if __name__ == "__main__":
    main()
