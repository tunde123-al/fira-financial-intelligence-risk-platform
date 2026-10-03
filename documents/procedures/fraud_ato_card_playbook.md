---
title: Account Takeover and Card Fraud Investigation Playbook
doc_type: internal_procedure
document_id: PB-FRAUD-004
version: "1.6"
effective_date: 2026-02-10
jurisdiction: internal
owner: Head of Fraud Operations
source: Lagoon Bank Plc (fictional institution) — synthetic document for FIRA development
---

# Account Takeover and Card Fraud Investigation Playbook

> SYNTHETIC DOCUMENT for FIRA development. Lagoon Bank Plc is fictional.

## 1. Account Takeover Indicators

Account takeover (ATO) occurs when a third party gains control of a customer's digital banking credentials.
Common indicators, especially in combination, are:

- login and payments from a **device never used before** by the customer;
- access from a **country not previously associated** with the customer, often through a web browser;
- a **burst of outbound transfers** within minutes, frequently to new external beneficiaries;
- transfer amounts several times larger than the customer's usual payments, draining the balance.

## 2. Investigation Steps

1. Establish the timeline: last activity from the customer's known devices and the first activity from the new
   device.
2. Compare the IP-derived location and device type with the customer's history.
3. List the beneficiaries of transfers made from the new device and check whether other customers sent funds to
   the same beneficiaries.
4. Contact the customer through a verified channel using the approved script.

## 3. Card Testing

Card testing is characterised by many low-value card-not-present payments at different online merchants within a
short period, with a high proportion of declined or failed authorisations. It often precedes larger fraudulent
purchases.

## 4. Card Cloning and Impossible Travel

Two card-present transactions whose locations are further apart than could be travelled in the elapsed time
(for example, more than 900 km per hour) indicate that the card details may have been copied. Investigators should
confirm the merchants' terminal locations before concluding.

## 5. Actions Requiring Human Authorisation

Blocking a card, suspending digital access, or recalling payments may only be performed by authorised Fraud
Operations staff. Automated systems may recommend these actions but must not perform them.

## 6. Customer Treatment

Customers who are victims of account takeover must be treated as victims. Investigation reports must not imply
that the account holder is responsible for fraudulent activity unless evidence supports that conclusion and it
has been confirmed by an investigator.
