# Sprint 1 (S1.1) - Technical Notes & Governance Clarifications

## Mentor Note: Role Assignment Guidelines

**Date:** September 10, 2026  
**Project:** AI ServiceNow Support Assistant  
**Mentor:** Ahmed Taha  

---

### Key Clarification regarding `snc_internal` Role

> **Explicit Note:**  
> Our mentor, **Ahmed Taha**, has explicitly confirmed that the **`snc_internal`** role is **not required** for our project scope.

### Role Assignment Context & Least-Privilege Standard

When configuring non-human API accounts (such as `Integration_Agent`) and end-user test accounts (such as `Test_Requester`) within our ServiceNow instance:

* **`Integration_Agent`**: Assigned specific REST API and scoped access roles (`snc_platform_rest_api_access` and `x_2215387_sprint_0.user`) without administrative privileges (`admin`).
* **`Test_Requester`**: Maintained without unnecessary system roles to represent a standard end-user / requester profile.
* **`snc_internal` Role**: Omitted per mentor explicit instruction, aligning with project scope governance requirements.