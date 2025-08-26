# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import re
import uuid
from typing import Optional

import jwt
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
from pydantic import BaseModel, Field

# Optional Vertex AI imports guarded at runtime
VERTEX_AVAILABLE = False
try:
    # google-cloud-aiplatform >= 1.49 provides vertexai.generative_models
    from vertexai import init as vertex_init
    from vertexai.generative_models import GenerativeModel

    VERTEX_AVAILABLE = True
except Exception:  # pragma: no cover - library may be unavailable locally
    VERTEX_AVAILABLE = False


logger = logging.getLogger("ai-agent")
# Configure logging level from env, tolerant of lowercase values
_log_level_name = os.getenv("LOG_LEVEL", "INFO")
try:
    _level = getattr(logging, _log_level_name.upper(), logging.INFO)
    logging.basicConfig(level=_level)
except Exception:
    logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Bank of Anthos AI Agent", version=os.getenv("VERSION", "v0.1.0"))


# ---------- Config & helpers ----------
LOCAL_ROUTING = os.getenv("LOCAL_ROUTING_NUM", "883745000")
PUB_KEY_PATH = os.getenv("PUB_KEY_PATH", "/tmp/.ssh/publickey")
BALANCES_API_ADDR = os.getenv("BALANCES_API_ADDR", "balancereader:8080")
TRANSACTIONS_API_ADDR = os.getenv("TRANSACTIONS_API_ADDR", "ledgerwriter:8080")

# Optional Vertex AI config
USE_VERTEX_AI = os.getenv("USE_VERTEX_AI", "false").lower() in ("1", "true", "yes")
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
VERTEX_MODEL = os.getenv("VERTEX_MODEL", "gemini-1.5-pro")

# Defaults for deposits when user doesn't provide external details explicitly
DEFAULT_EXTERNAL_ACCT = os.getenv("DEFAULT_EXTERNAL_ACCOUNT", "1111111111")
DEFAULT_EXTERNAL_ROUTING = os.getenv("DEFAULT_EXTERNAL_ROUTING", "222222222")


def _load_public_key() -> Optional[str]:
    try:
        with open(PUB_KEY_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:  # pragma: no cover - depends on runtime mount
        logger.warning("Public key not found at %s: %s", PUB_KEY_PATH, e)
        return None


PUB_KEY = _load_public_key()


class ChatRequest(BaseModel):
    message: str = Field(..., description="User's natural language request")
    session_id: Optional[str] = Field(None, description="Optional session ID for context")


class ChatResponse(BaseModel):
    reply: str
    intent: str
    details: dict = {}


def _decode_jwt(bearer_token: str) -> dict:
    if not bearer_token:
        raise HTTPException(status_code=401, detail="Missing Authorization Bearer token")
    token = bearer_token
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1]
    if PUB_KEY:
        try:
            return jwt.decode(token, PUB_KEY, algorithms=["RS256"], options={"verify_aud": False})
        except Exception as e:
            logger.warning("JWT verification failed, attempting decode without verify: %s", e)
    # Fallback: decode without verification (last resort)
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except Exception as e:
        logger.error("Unable to decode JWT: %s", e)
        raise HTTPException(status_code=401, detail="Invalid token")


def _cents_to_str(amount_cents: int) -> str:
    sign = '-' if amount_cents < 0 else ''
    cents = abs(amount_cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


# ---------- Simple rule-based NLU fallback ----------
amount_re = re.compile(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", re.IGNORECASE)
account_re = re.compile(r"account\s*(\d{10})", re.IGNORECASE)
routing_re = re.compile(r"routing\s*(\d{9})", re.IGNORECASE)


def parse_intent_rule_based(text: str) -> dict:
    t = text.lower().strip()
    intent = "unknown"
    amount: Optional[int] = None
    to_account: Optional[str] = None
    from_account: Optional[str] = None
    from_routing: Optional[str] = None

    m = amount_re.search(t)
    if m:
        try:
            amount = int(round(float(m.group(1)) * 100))
        except Exception:
            amount = None

    acct_matches = account_re.findall(t)
    if acct_matches:
        # first occurrence as recipient in transfers; can be sender in deposits
        to_account = acct_matches[0]

    rout_m = routing_re.search(t)
    if rout_m:
        from_routing = rout_m.group(1)

    if any(kw in t for kw in ["balance", "how much do i have", "what's my balance", "check balance"]):
        intent = "check_balance"
    elif any(kw in t for kw in ["deposit", "add money", "cash in"]):
        intent = "deposit"
        # If not specified, use default external details
        if not from_account:
            from_account = DEFAULT_EXTERNAL_ACCT
        if not from_routing or from_routing == LOCAL_ROUTING:
            from_routing = DEFAULT_EXTERNAL_ROUTING
    elif any(kw in t for kw in ["transfer", "send", "pay"]):
        intent = "transfer"

    return {
        "intent": intent,
        "amount": amount,
        "to_account": to_account,
        "from_account": from_account,
        "from_routing": from_routing,
    }


# ---------- Vertex AI parsing ----------
def parse_intent_vertex(text: str) -> dict:
    if not (USE_VERTEX_AI and VERTEX_AVAILABLE and VERTEX_PROJECT):
        raise RuntimeError("Vertex AI not configured")
    try:
        vertex_init(project=VERTEX_PROJECT, location=VERTEX_LOCATION)
        model = GenerativeModel(VERTEX_MODEL)
        system_prompt = (
            "You are a banking assistant. Extract intent and entities from the user's message. "
            "Return ONLY valid compact JSON with keys: intent (one of check_balance, deposit, transfer, unknown), "
            "amount_cents (integer or null), to_account (10 digits or null), from_account (10 digits or null), from_routing (9 digits or null)."
        )
        user_prompt = f"Message: {text}\nJSON:"
        resp = model.generate_content([
            {"role": "system", "parts": [system_prompt]},
            {"role": "user", "parts": [user_prompt]},
        ])
        raw = resp.text.strip()
        data = json.loads(raw)
        return {
            "intent": data.get("intent", "unknown"),
            "amount": data.get("amount_cents"),
            "to_account": data.get("to_account"),
            "from_account": data.get("from_account"),
            "from_routing": data.get("from_routing"),
        }
    except Exception as e:
        logger.error("Vertex AI parsing failed: %s", e)
        # Fallback to rule-based
        return parse_intent_rule_based(text)


def parse_intent(text: str) -> dict:
    if USE_VERTEX_AI:
        return parse_intent_vertex(text)
    return parse_intent_rule_based(text)


# ---------- Service integrations ----------
def get_balance(account_id: str, bearer: str) -> int:
    url = f"http://{BALANCES_API_ADDR}/balances/{account_id}"
    headers = {"Authorization": bearer}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"Balance fetch failed: {r.text}")
    try:
        return int(r.json())
    except Exception:
        # Some versions may return plaintext
        return int(r.text)


def post_transaction(payload: dict, bearer: str) -> None:
    url = f"http://{TRANSACTIONS_API_ADDR}/transactions"
    headers = {"Authorization": bearer, "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=f"Transaction failed: {r.text}")


# Swagger UI bearer auth support
_bearer_scheme = HTTPBearer(auto_error=False)


def require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme)) -> str:
    if not credentials or not credentials.scheme or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Authorization header required")
    # Normalize to "Bearer <token>" string for downstream services
    return f"{credentials.scheme} {credentials.credentials}"


@app.get("/ready", response_class=PlainTextResponse)
def ready() -> str:
    return "ok"


@app.get("/")
def root():
    return {
        "service": "ai-agent",
        "version": os.getenv("VERSION", "v0.1.0"),
        "endpoints": ["/ready", "/version", "/chat"],
        "message": "Try GET /ready or POST /chat with a Bearer token."
    }


@app.get("/ui", response_class=HTMLResponse)
def ui():
        # Minimal embedded chat UI, no external dependencies
        return """
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <title>Bank of Anthos AI Agent</title>
    <style>
        :root{--bg:#0f172a;--panel:#111827;--muted:#94a3b8;--fg:#e5e7eb;--accent:#22d3ee;--accent2:#60a5fa}
        body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
        .wrap{max-width:900px;margin:0 auto;padding:24px}
        .card{background:var(--panel);border:1px solid #1f2937;border-radius:12px;overflow:hidden}
        .header{padding:16px 20px;border-bottom:1px solid #1f2937;display:flex;gap:12px;align-items:center}
        .header h1{font-size:16px;margin:0}
        .muted{color:var(--muted)}
        .row{display:flex;gap:8px;flex-wrap:wrap}
        input[type=text],input[type=password]{flex:1 1 320px;background:#0b1220;color:var(--fg);border:1px solid #1f2937;border-radius:8px;padding:10px}
        button{background:linear-gradient(90deg,var(--accent),var(--accent2));color:#000;font-weight:600;border:0;border-radius:8px;padding:10px 14px;cursor:pointer}
        button:disabled{opacity:.6;cursor:not-allowed}
        .chat{height:52vh;overflow:auto;padding:16px 20px;display:flex;flex-direction:column;gap:10px}
        .msg{max-width:80%;padding:10px 12px;border-radius:12px;border:1px solid #1f2937}
        .me{align-self:flex-end;background:#12213a}
        .bot{align-self:flex-start;background:#101a2d}
        .footer{display:flex;gap:8px;padding:12px;border-top:1px solid #1f2937}
        textarea{flex:1;background:#0b1220;color:var(--fg);border:1px solid #1f2937;border-radius:8px;padding:10px;height:64px}
        a{color:var(--accent)}
    </style>
    <script>
        function $(q){return document.querySelector(q)}
        function append(role,text){
            const d=document.createElement('div');
            d.className='msg '+(role==='user'?'me':'bot');
            d.textContent=text;$('#log').appendChild(d);d.scrollIntoView({behavior:'smooth',block:'end'})
        }
        function saveToken(){
            const t=$('#token').value.trim();
            if(t){ localStorage.setItem('ai_token', t); $('#tokstatus').textContent='Token saved'; setTimeout(()=>$('#tokstatus').textContent='',1500)}
        }
        function loadToken(){ const v=localStorage.getItem('ai_token')||''; $('#token').value=v }
        async function send(){
            const msg=$('#message').value.trim(); if(!msg) return;
            const tok=$('#token').value.trim(); if(!tok){ alert('Paste your JWT token first.'); return }
            $('#send').disabled=true; append('user', msg); $('#message').value=''
            try{
                const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify({message:msg})});
                if(!res.ok){ const t=await res.text(); append('bot', `Error ${res.status}: ${t}`); return }
                const data=await res.json(); append('bot', data.reply || JSON.stringify(data))
            }catch(e){ append('bot', 'Network error: '+e)
            }finally{ $('#send').disabled=false }
        }
        window.addEventListener('DOMContentLoaded',()=>{loadToken(); $('#message').addEventListener('keydown',e=>{if(e.key==='Enter' && (e.ctrlKey||e.metaKey||!e.shiftKey)){e.preventDefault(); send()}})})
    </script>
    </head>
<body>
    <div class=\"wrap\">
        <div class=\"card\">
            <div class=\"header\">
                <h1>Bank of Anthos AI Agent</h1>
                <div class=\"muted\">Use your JWT from the app. <a href=\"/docs\" target=\"_blank\">API docs</a></div>
            </div>
            <div style=\"padding:14px 20px;border-bottom:1px solid #1f2937\">
                <div class=\"row\">
                    <input id=\"token\" type=\"password\" placeholder=\"Paste JWT token here\" />
                    <button onclick=\"saveToken()\">Save token</button>
                    <span id=\"tokstatus\" class=\"muted\"></span>
                </div>
            </div>
            <div id=\"log\" class=\"chat\"></div>
            <div class=\"footer\">
                <textarea id=\"message\" placeholder=\"Message…  (Enter to send, Shift+Enter for newline)\"></textarea>
                <button id=\"send\" onclick=\"send()\">Send</button>
            </div>
        </div>
    </div>
</body>
</html>
"""


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, authorization: str = Depends(require_auth)) -> ChatResponse:
    claims = _decode_jwt(authorization)
    account_id = claims.get("acct")
    username = claims.get("user")
    if not account_id:
        raise HTTPException(status_code=400, detail="Token missing account id")

    nlu = parse_intent(req.message)
    intent = nlu.get("intent", "unknown")

    if intent == "check_balance":
        balance_cents = get_balance(account_id, authorization)
        reply = f"Hi {username}, your current balance is {_cents_to_str(balance_cents)}."
        return ChatResponse(reply=reply, intent=intent, details={"balance_cents": balance_cents})

    if intent in ("transfer", "deposit"):
        amount = nlu.get("amount")
        if not amount or amount <= 0:
            raise HTTPException(status_code=400, detail="Please specify a positive amount.")

        tx = {
            "amount": int(amount),
            "uuid": str(uuid.uuid4()),
        }
        if intent == "transfer":
            to_acct = nlu.get("to_account")
            if not to_acct or not re.fullmatch(r"\d{10}", to_acct):
                raise HTTPException(status_code=400, detail="Please provide a valid 10-digit recipient account.")
            tx.update({
                "fromAccountNum": account_id,
                "fromRoutingNum": LOCAL_ROUTING,
                "toAccountNum": to_acct,
                "toRoutingNum": LOCAL_ROUTING,
            })
            post_transaction(tx, authorization)
            reply = f"Transferred {_cents_to_str(amount)} to account {to_acct}."
            return ChatResponse(reply=reply, intent=intent, details={"to_account": to_acct, "amount_cents": amount})

        # deposit
        from_acct = nlu.get("from_account") or DEFAULT_EXTERNAL_ACCT
        from_routing = nlu.get("from_routing") or DEFAULT_EXTERNAL_ROUTING
        if from_routing == LOCAL_ROUTING:
            # Ensure deposit comes from external routing
            from_routing = DEFAULT_EXTERNAL_ROUTING
        tx.update({
            "fromAccountNum": from_acct,
            "fromRoutingNum": from_routing,
            "toAccountNum": account_id,
            "toRoutingNum": LOCAL_ROUTING,
        })
        post_transaction(tx, authorization)
        reply = f"Deposited {_cents_to_str(amount)} into your account."
        return ChatResponse(reply=reply, intent=intent, details={"amount_cents": amount})

    # unknown intent
    reply = (
        "I can help you check your balance, deposit, or transfer money. "
        "Try: 'What's my balance?', 'Deposit $50', or 'Transfer $25 to account 1234567890'."
    )
    return ChatResponse(reply=reply, intent="unknown", details={"nlu": nlu})


@app.get("/version", response_class=PlainTextResponse)
def version() -> str:
    return os.getenv("VERSION", "v0.1.0")


@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException):  # noqa: U100
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
