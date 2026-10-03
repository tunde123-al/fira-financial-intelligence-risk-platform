---
title: Data Privacy and Retention Standard for Financial Crime Systems
doc_type: internal_procedure
document_id: STD-PRIV-007
version: "1.3"
effective_date: 2025-12-01
jurisdiction: internal
owner: Data Protection Officer
source: Lagoon Bank Plc (fictional institution) — synthetic document for FIRA development
---

# Data Privacy and Retention Standard for Financial Crime Systems

> SYNTHETIC DOCUMENT for FIRA development. Lagoon Bank Plc is fictional.

## 1. Data Minimisation

Financial crime systems must process only the personal data needed for monitoring and investigation. Contact
identifiers used for matching (phone, email, address) should be stored as salted hashes, with masked values for
display.

## 2. Access Control

Access is role-based. Analysts may view masked personal data. Unmasked data is restricted to authorised roles and
every access is logged.

## 3. Retention

- Investigation records and decisions: at least five years after case closure.
- Audit logs: at least five years.
- Automated agent episode records: two years, unless linked to an open or escalated case.
- Identity document images: no longer than required for verification and audit.

## 4. Use of External AI Services

Personal data sent to external model providers must be minimised. Only the evidence necessary for the specific
investigation may be transmitted, identifiers should be pseudonymised where practical, and the provider must be
approved by the Data Protection Officer.
