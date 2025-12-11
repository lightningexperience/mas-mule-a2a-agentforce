# --- Agentforce A2A Server (Fixed & Heroku Ready) ---
# Version 2.0.1 (ONLY authentication changed)

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

# --- Configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AgentforceA2AServer")

app = FastAPI(
    title="Agentforce A2A Server",
    description="Escalates to Salesforce AI support agent via A2A protocol."
)

# CRITICAL: Add CORS middleware for A2A Inspector compatibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- A2A Protocol Schemas (Strictly Enforced) ---
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
# Agent Card (JSONRPC Compatible)
# -----------------------------
@app.get("/.well-known/agent-card.json")
def get_agent_card(request: Request):
    forwarded_proto = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("host", str(request.url).split("//")[1].split("/")[0])
    base_url = f"{forwarded_proto}://{host}"

    agent_card = {
        "protocolVersion": "0.3.0",
        "name": "Agentforce A2A",
        "description": "Escalates to Salesforce AI support agent.",
        "url": f"{base_url}/",
        "version": "2.0.1",
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
                "description": "Connects to Salesforce Agentforce.",
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "Check the status of my case",
                    "Escalate to Agentforce"
                ],
                "tags": ["salesforce", "agentforce", "support"]
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
    return agent_card


# ------------------------------------------------------
# JSONRPC ENDPOINT (UNCHANGED)
# ------------------------------------------------------
@app.post("/json-rpc")
async def json_rpc_handler(payload: Dict[str, Any]):
    if "jsonrpc" not in payload or payload["jsonrpc"] != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32600, "message": "Invalid Request"}
        }

    method = payload.get("method")
    params = payload.get("params")
    rpc_id = payload.get("id")

    if method != "task":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Unknown method {method}"}
        }

    try:
        task_request = A2ATaskRequest(**params)
    except ValidationError as e:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32602, "message": str(e)}
        }

    response = await handle_a2a_task(task_request)

    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": response
    }


# ------------------------------------------------------
# HTTP task endpoint (UNCHANGED)
# ------------------------------------------------------
@app.post("/tasks")
async def handle_a2a_task_endpoint(task_request: A2ATaskRequest):
    return await handle_a2a_task(task_request)


# ------------------------------------------------------
# MINIMAL CHANGE #1: Username–Password OAuth
# ------------------------------------------------------
def get_salesforce_token():
    token_url = os.getenv("SF_TOKEN_URL")  # e.g., https://login.salesforce.com
    client_id = os.getenv("SF_CLIENT_ID")
    client_secret = os.getenv("SF_CLIENT_SECRET")
    username = os.getenv("SF_USERNAME")
    password = os.getenv("SF_PASSWORD")

    if not all([token_url, client_id, client_secret, username, password]):
        raise ValueError("Missing Salesforce OAuth environment variables")

    full_url = token_url.rstrip("/") + "/services/oauth2/token"

    resp = requests.post(
        full_url,
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password
        }
    )
    resp.raise_for_status()
    data = resp.json()

    # IMPORTANT: username-password returns instance_url
    return data["access_token"], data["instance_url"]


# ------------------------------------------------------
# MINIMAL CHANGE #2: use instance_url instead of api.salesforce.com
# ------------------------------------------------------
def query_agentforce(user_message: str, access_token: str, instance_url: str):
    agent_id = os.getenv("SF_AGENT_ID", "0XxWt0000005qu1KAA")

    # Create session
    session_payload = {
        "externalSessionKey": str(uuid.uuid4()),
        "instanceConfig": {"endpoint": instance_url},
        "streamingCapabilities": {"chunkTypes": ["Text"]},
        "bypassUser": True
    }

    session_url = (
        instance_url.rstrip("/")
        + f"/einstein/ai-agent/v1/agents/{agent_id}/sessions"
    )

    session_res = requests.post(
        session_url,
        json=session_payload,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    session_res.raise_for_status()
    session_id = session_res.json()["sessionId"]

    # Send message
    stream_url = (
        instance_url.rstrip("/")
        + f"/einstein/ai-agent/v1/sessions/{session_id}/messages/stream"
    )

    message_payload = {
        "message": {"sequenceId": 1, "type": "Text", "text": user_message}
    }

    response = requests.post(
        stream_url,
        json=message_payload,
        headers={"Authorization": f"Bearer {access_token}"},
        stream=True
    )

    final_msg = ""
    for line in response.iter_lines():
        if line and line.decode().startswith("data: "):
            event = json.loads(line.decode()[6:])
            if event.get("message", {}).get("type") == "Inform":
                final_msg = event["message"]["message"]
                break

    return final_msg or "No response received.", []


# ------------------------------------------------------
# MINIMAL CHANGE #3: Update function call signature
# ------------------------------------------------------
async def handle_a2a_task(task_request: A2ATaskRequest):
    task_id = task_request.taskId

    latest_message = task_request.inputs[-1].content[-1].value

    try:
        access_token, instance_url = get_salesforce_token()
        agent_resp, debug = query_agentforce(latest_message, access_token, instance_url)
        response_text = f"Agentforce Response: {agent_resp}"
    except Exception as e:
        response_text = f"⚠️ Failed to connect to Salesforce Agentforce: {str(e)}"
        debug = [str(e)]

    return {
        "status": "completed",
        "taskId": task_id,
        "outputs": [
            {
                "kind": "message",
                "role": "agent",
                "parts": [{"kind": "text", "text": response_text}],
                "contextId": task_request.contextId or task_id
            }
        ],
        "debug": debug
    }


# ------------------------------------------------------
# Root POST handler (UNCHANGED)
# ------------------------------------------------------
@app.post("/")
async def root_post_handler(request: Request):
    body = await request.json()

    try:
        task_request = A2ATaskRequest(**body)
        result = await handle_a2a_task(task_request)
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
    except Exception:
        pass

    if body.get("method") == "message/send":
        msg = body["params"]["message"]
        text = msg["parts"][0]["text"]

        task_request = A2ATaskRequest(
            taskId=str(uuid.uuid4()),
            skillId="case-escalation",
            inputs=[A2AInput(role=msg["role"], content=[ContentPart(type="text/plain", value=text)])],
            contextId=msg.get("contextId")
        )

        result = await handle_a2a_task(task_request)
        agent_text = result["outputs"][0]["parts"][0]["text"]

        return {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "kind": "message",
                "role": "agent",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": agent_text}]
            }
        }

    return JSONResponse(status_code=400, content={"error": "Invalid payload"})


# ------------------------------------------------------
# Health Check Endpoint (UNCHANGED)
# ------------------------------------------------------
@app.get("/health")
def health_check():
    return {"status": "healthy", "version": "2.0.1"}


# ------------------------------------------------------
# Root (UNCHANGED)
# ------------------------------------------------------
@app.get("/")
def root(request: Request):
    return {"service": "Agentforce A2A Server", "version": "2.0.1"}


# ------------------------------------------------------
# Heroku Entrypoint (UNCHANGED)
# ------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
