---
title: Model Risk Governance for Detection Models and AI Assistants
doc_type: internal_procedure
document_id: STD-MRM-008
version: "1.1"
effective_date: 2026-05-01
jurisdiction: internal
owner: Model Risk Management
source: Lagoon Bank Plc (fictional institution) — synthetic document for FIRA development
---

# Model Risk Governance for Detection Models and AI Assistants

> SYNTHETIC DOCUMENT for FIRA development. Lagoon Bank Plc is fictional.

## 1. Scope

This standard covers rule thresholds, scoring weights, machine-learning models and AI assistants used in
financial crime detection and investigation.

## 2. Change Control

2.1 Changes to thresholds, weights, prompts or models must be proposed with a rationale, tested offline against a
benchmark that includes known suspicious and known legitimate cases, and compared with the current production
configuration.

2.2 A change may be promoted only if it does not reduce recall on known suspicious cases beyond the agreed
tolerance and does not materially increase the false-positive rate.

2.3 Promotion requires approval by an authorised approver who did not author the change.

2.4 Systems must not modify their own production configuration without this approval.

## 3. Monitoring

Production performance is monitored through investigator feedback, false-positive rates, alert volumes, and the
rate at which automated report statements are found unsupported by evidence.

## 4. Documentation

Each model or configuration version must record its training data, parameters, evaluation results, approver and
date of approval.
