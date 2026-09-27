"""
agent/tools.py
──────────────
BARQ G4 · S2.5 — Stub / Mock Tools for the Agent Executor.

These tools are intentionally mocked so the project runs without any live
ServiceNow or vector-store connection.  Each tool is implemented as a plain
Python callable decorated with @tool so LangChain can introspect it.

To wire a real implementation, replace the body of the function while keeping
the signature and the @tool decorator intact.
"""

from langchain_core.tools import tool


# ── Tool 1: Knowledge Base Search ─────────────────────────────────────────────

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the BARQ knowledge base for articles relevant to the given query.

    Returns a formatted string containing matching knowledge chunk(s) with
    their identifiers.  In production this would call a vector store or
    full-text search index.  Currently returns a mock response for testing.

    Args:
        query: Free-text search query describing the issue to look up.

    Returns:
        Formatted string of knowledge chunks, or a message indicating no
        results were found.
    """
    # ── MOCK — replace with real retrieval logic in production ────────────────
    mock_results = {
        "vpn": (
            "[KB-101] VPN Connectivity Troubleshooting\n"
            "Step 1: Verify the user's VPN client version matches the approved "
            "version listed in the software catalogue (≥ 5.2.1).\n"
            "Step 2: Ask the user to disconnect and reconnect to ensure a fresh "
            "TLS handshake.\n"
            "Step 3: Check whether the corporate firewall policy allows UDP 4500 "
            "and TCP 443 from the user's network segment.\n"
            "Step 4: If the issue persists, collect the VPN client log from "
            "%APPDATA%\\CiscoAnyConnect\\logs and attach it to the ticket."
        ),
        "password": (
            "[KB-202] Password Reset Procedure\n"
            "Step 1: Verify the user's identity using two approved verification "
            "questions or a manager approval email.\n"
            "Step 2: Navigate to Active Directory Users and Computers, locate the "
            "account, and select 'Reset Password'.\n"
            "Step 3: Set a temporary password meeting the complexity policy and "
            "tick 'User must change password at next logon'.\n"
            "Step 4: Inform the user of the temporary password via a secure "
            "channel (encrypted email or secure messaging)."
        ),
        "printer": (
            "[KB-305] Network Printer Not Found\n"
            "Step 1: Confirm the printer is powered on and the network status "
            "light is solid green.\n"
            "Step 2: From the affected workstation run: ping <printer-IP> to "
            "verify network reachability.\n"
            "Step 3: Reinstall the printer driver using the approved package from "
            "the IT portal (\\\\fileserver\\drivers\\printers).\n"
            "Step 4: Add the printer via 'Add a printer' → 'The printer that I "
            "want isn't listed' → TCP/IP port."
        ),
    }

    query_lower = query.lower()
    for keyword, result in mock_results.items():
        if keyword in query_lower:
            return result

    return "NO_RESULTS: No knowledge base articles matched the query."


# ── Tool 2: Incident Detail Lookup ────────────────────────────────────────────

@tool
def get_incident_details(incident_id: str) -> str:
    """
    Retrieve structured fields for a ServiceNow incident by its INC number.

    In production this would call the ServiceNow REST API.  Currently returns
    a mock incident record for testing.

    Args:
        incident_id: ServiceNow incident number, e.g. 'INC0012345'.

    Returns:
        Formatted string of incident fields (caller, category, description,
        priority, state).
    """
    # ── MOCK — replace with real ServiceNow API call in production ────────────
    mock_incidents = {
        "INC0001": {
            "number": "INC0001",
            "caller": "Ahmed Hassan",
            "category": "Network",
            "subcategory": "VPN",
            "priority": "2 - High",
            "state": "In Progress",
            "short_description": "Cannot connect to corporate VPN from home",
            "description": (
                "User reports that the Cisco AnyConnect client shows error "
                "'Unable to establish VPN' when attempting to connect from "
                "their home network. Issue started after Windows update on "
                "2026-09-15. Other users on the same team are unaffected."
            ),
        },
        "INC0002": {
            "number": "INC0002",
            "caller": "Sara Mahmoud",
            "category": "Access Management",
            "subcategory": "Password",
            "priority": "3 - Moderate",
            "state": "New",
            "short_description": "Account locked after multiple failed login attempts",
            "description": (
                "User is unable to log into their Windows workstation. "
                "Active Directory shows the account is locked. User confirmed "
                "they did not share credentials with anyone."
            ),
        },
    }

    record = mock_incidents.get(incident_id.upper(), None)
    if record is None:
        return f"NOT_FOUND: Incident {incident_id} was not found in the system."

    lines = [f"{k}: {v}" for k, v in record.items()]
    return "\n".join(lines)


# ── Exported tool list ─────────────────────────────────────────────────────────

TOOLS = [search_knowledge_base, get_incident_details]
