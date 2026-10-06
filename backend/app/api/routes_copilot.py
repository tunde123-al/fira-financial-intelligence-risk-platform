"""AI Investigation Copilot endpoint: grounded, structured answers about one customer."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import audit, get_container, get_principal, present
from app.copilot.service import ask
from app.risk.engine import EntityNotFound
from app.security.principal import Principal

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=r"^CUST-\d{1,10}$")
    question: str = Field(min_length=3, max_length=500)
    investigation_id: str | None = Field(default=None, pattern=r"^INV-[A-Z0-9]{1,20}$")
    lookback_days: int = Field(default=30, ge=1, le=365)


@router.post("/ask", summary="Ask a grounded question about a customer; the answer is structured and cites FIRA entities")
def ask_copilot(body: AskBody, c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    # the question text itself is not written to the audit log; only its length and the customer are
    audit(c, p, "ai_investigation_requested", "ok", "customer", body.customer_id, question_chars=len(body.question),
          investigation_id=body.investigation_id)
    try:
        out = ask(c, body.customer_id, body.question, body.investigation_id, body.lookback_days)
    except EntityNotFound as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    audit(c, p, "ai_response_generated", out["status"], "customer", body.customer_id, ai_used=out["grounding"]["ai_used"],
          intents=out["intents"], facts=len(out["observed_facts"]), signals=len(out["derived_signals"]))
    return present(c, p, out)
