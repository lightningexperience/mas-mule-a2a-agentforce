# --- Agentforce A2A Server ---
# Version 2.1.2  (Removed security token requirement)

import os
import logging
import requests
import uuid
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
from typing import List, Optional, Dict, Any

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AgentforceA2AServer")

# -----------------------------
# FastAPI Initialization
# -----------------------------
app = FastAPI(
    title="Agentforce A2A Server",
    description="Escalates to Salesforce AI support agent via A2A protocol."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# A2A Schemas
# -----------------------------
class ContentPart(BaseModel):
    type: str
    value: str

class A2AInput(BaseModel):
    role: str
    content: List[ContentPart]

class A2ATaskRequest(BaseModel):
    taskId: str
    skillId: str
    inputs: List[A2AInput]
    contextId: Optional[str] = None


# -----------------------------
# Simple GET Root (restored)
# -----------------------------
@app.get("/")
def root():
    return {
        "service": "Agentforce A2A Server",
        "version": "2.1.2",
        "status": "running"
    }


# -----------------------------
# Agent Card (unchanged)
# -----------------------------
@app.get("/.well-known/agent-card.json")
def get_agent_card(request: Request):

    forwarded_proto = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("host")
    base_url = f"{forwarded_proto}://{host}"

    return {
        "protocolVersion": "0.3.0",
        "name": "Agentforce A2A",
        "description": "Escalates to Salesforce AI support agent.",
        "url": f"{base_url}/",
        "version": "2.1.2",
        "vendor": "Salesforce",
        "apiVersion": "1.0.0",
        "capabilities": {
            "pushNotifications": False,
            "streaming": False,
            "batching": False,
            "stateful": False
        },
        "securitySchemes": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "case-escalation",
                "name": "Case Escalation",
                "description": "Connects to Salesforce Agentforce and fetches support case updates.",
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "Check the status of my support case",
                    "Escalate this issue to Agentforce",
                    "What's the update on case #12345?"
                ]
            }
        ],
        "preferredTransport": "JSONRPC",
        "transports": {
            "JSONRPC": {
                "url": f"{base_url}/json-rpc",
                "version": "2.0",
                "contentTypes": ["application/json"]
            }
        }
    }


# -----------------------------
# JSONRPC Endpoint
# -----------------------------
@app.post("/json-rpc")
async def json_rpc(payload: Dict[str, Any]):

    if payload.get("jsonrpc") != "2.0":
        return {
            "jsonrpc": "2.0", "id": payload.get("id"),
            "error": {"code": -32600, "message": "Invalid JSONRPC version"}
        }

    if payload.get("method") != "task":
        return {
            "jsonrpc": "2.0", "id": payload.get("id"),
            "error": {"code": -32601, "message": "Unknown method"}
        }

    try:
        task = A2ATaskRequest(**payload["params"])
    except ValidationError as e:
        return {
            "jsonrpc": "2.0", "id": payload.get("id"),
            "error": {"code": -32602, "message": str(e)}
        }

    result = await handle_a2a_task(task)

    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}


# --------------------------------------------------------------------
# UPDATED AUTH: Username + Password only — NO SECURITY TOKEN REQUIRED
# --------------------------------------------------------------------
def get_salesforce_token():

    login_url = os.getenv("SF_TOKEN_URL", "https://login.salesforce.com")
    client_id = os.getenv("SF_CLIENT_ID")
    client_secret = os.getenv("SF_CLIENT_SECRET")
    username = os.getenv("SF_USERNAME")
    password = os.getenv("SF_PASSWORD")

    if not all([client_id, client_secret, username, password]):
        raise Exception("Missing Salesforce OAuth environment variables")

    # NOTE: password is sent *as is* — no security token appended
    response = requests.post(
        f"{login_url}/services/oauth2/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password
        }
    )

    if response.status_code != 200:
        logger.error(f"Token Error: {response.text}")
        response.raise_for_status()

    return response.json()["access_token"]


# -----------------------------
# Call Agentforce Runtime API
# -----------------------------
def call_agentforce(user_message: str, access_token: str):

    agent_id = os.getenv("SF_AGENT_ID")
    if not agent_id:
        raise Exception("Missing SF_AGENT_ID")

    # Create session
    session_payload = {
        "externalSessionKey": str(uuid.uuid4()),
        "streamingCapabilities": {"chunkTypes": ["Text"]},
        "bypassUser": False
    }

    session_res = requests.post(
        f"https://api.salesforce.com/einstein/ai-agent/v1/agents/{agent_id}/sessions",
        json=session_payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
    )
    session_res.raise_for_status()
    session_id = session_res.json().get("sessionId")

    # Send message
    message_payload = {
        "message": {"sequenceId": 1, "type": "Text", "text": user_message}
    }

    stream = requests.post(
        f"https://api.salesforce.com/einstein/ai-agent/v1/sessions/{session_id}/messages/stream",
        json=message_payload,
        headers={"Authorization": f"Bearer {access_token}"},
        stream=True
    )

    final_message = ""

    for line in stream.iter_lines():
        if not line:
            continue
        decoded = line.decode("utf-8")
        if decoded.startswith("data: "):
            data = json.loads(decoded[6:])
            msg = data.get("message", {})
            if msg.get("type") == "Inform":
                final_message = msg.get("message")
                break

    return final_message or "No response received from Agentforce."


# -----------------------------
# A2A Task Handler
# -----------------------------
async def handle_a2a_task(task: A2ATaskRequest):

    try:
        latest_msg = task.inputs[-1].content[-1].value
    except Exception:
        return {"status": "failed", "taskId": task.taskId, "error": "Invalid A2A input"}

    try:
        token = get_salesforce_token()
        agent_reply = call_agentforce(latest_msg, token)
        text = agent_reply
    except Exception as e:
        logger.error(f"Agentforce Error: {str(e)}")
        text = f"⚠️ Agentforce error: {str(e)}"

    return {
        "status": "completed",
        "taskId": task.taskId,
        "outputs": [
            {
                "kind": "message",
                "role": "agent",
                "parts": [{"kind": "text", "text": text}],
                "contextId": task.contextId or task.taskId
            }
        ]
    }


# -----------------------------
# POST "/" (unchanged)
# -----------------------------
@app.post("/")
async def root_post(request: Request):
    body = await request.json()

    # Direct A2A TaskRequest
    try:
        task = A2ATaskRequest(**body)
        result = await handle_a2a_task(task)
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "kind": "message",
                "role": "agent",
                "messageId": str(uuid.uuid4()),
                "parts": [{
                    "kind": "text",
                    "text": result["outputs"][0]["parts"][0]["text"]
                }]
            }
        }
    except:
        pass

    # Fabric "message/send"
    if body.get("method") == "message/send":
        msg = body["params"]["message"]
        text = msg["parts"][0]["text"]

        task = A2ATaskRequest(
            taskId=str(uuid.uuid4()),
            skillId="case-escalation",
            inputs=[A2AInput(
                role=msg.get("role", "user"),
                content=[ContentPart(type="text/plain", value=text)]
            )],
            contextId=msg.get("contextId")
        )

        result = await handle_a2a_task(task)

        return {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "kind": "message",
                "role": "agent",
                "messageId": str(uuid.uuid4()),
                "parts": [{
                    "kind": "text",
                    "text": result["outputs"][0]["parts"][0]["text"]
                }]
            }
        }

    return JSONResponse(status_code=400, content={"error": "Invalid payload"})


# -----------------------------
# Health
# -----------------------------
@app.get("/health")
def health():
    return {"status": "healthy", "version": "2.1.2"}


# -----------------------------
# Run locally or on Heroku
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
