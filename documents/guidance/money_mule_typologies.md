---
title: Money Mule and Network Typologies Guidance
doc_type: typology_guidance
document_id: GUIDE-MULE-005
version: "1.2"
effective_date: 2025-09-20
jurisdiction: internal
owner: Financial Intelligence Unit (internal)
source: Lagoon Bank Plc (fictional institution) — synthetic document for FIRA development
---

# Money Mule and Network Typologies Guidance

> SYNTHETIC DOCUMENT for FIRA development. Lagoon Bank Plc is fictional.

## 1. What Is a Money Mule

A money mule is a person whose account is used to receive and move funds on behalf of others, often the proceeds
of fraud. Mules may be complicit, recruited through job advertisements, or unaware that the funds are illicit.
Investigation language must therefore remain neutral about the account holder's knowledge or intent.

## 2. Single-Account Indicators

- Many inbound transfers from **different, unrelated senders** in a short period (fan-in).
- Outbound transfers of most of the received value **within hours** (pass-through), usually to a small number of
  external accounts.
- A low closing balance after the activity.
- Activity inconsistent with the customer's age, occupation or historical behaviour.

## 3. Network Indicators

- Several customers operated from the **same device** or sharing a phone number or email address.
- Transfers circulating between a small group of accounts, sometimes returning to the origin (**circular flows**).
- Common beneficiaries receiving funds from several apparently unrelated accounts.
- Accounts opened around the same time with similar characteristics.

## 4. Graph Analysis Techniques

Investigators should examine connected components around the subject, shared devices and identifiers, shortest
transfer paths between suspicious accounts, and the entities with the highest centrality in the cluster, which
may indicate coordinators or collection accounts.

## 5. Distinguishing Legitimate Patterns

- Businesses that receive many customer payments (fan-in) are expected to have a consistent history of doing so.
- Families commonly share an address and sometimes one device.
- Savings groups and rotating credit associations can create circular transfers among members; these normally
  recur on a fixed schedule with stable membership.
