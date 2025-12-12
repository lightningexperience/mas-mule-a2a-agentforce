# --- Agentforce A2A Server (Fixed & Heroku Ready) ---
# Version 2.0.1

import os
import logging
import requests
import uuid
import json
import time
import jwt
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
    type: str = Field(..., description="MIME type, e.g., 'text/plain'")
    value: str = Field(..., description="The content string.")

class A2AInput(BaseModel):
    role: str = Field(..., description="Sender role, e.g., 'user'")
    content: List[ContentPart] = Field(..., description="List of content parts")

class A2ATaskRequest(BaseModel):
    taskId: str = Field(..., description="The mandatory unique ID for this task/session.")
    skillId: str = Field(..., description="The skill being invoked (e.g., 'case-escalation')")
    inputs: List[A2AInput] = Field(..., description="List of messages in the task thread")
    contextId: Optional[str] = Field(None, description="Optional session context ID.")


# -----------------------------
# Agent Card (JSONRPC Compatible)
# -----------------------------
@app.get("/.well-known/agent-card.json")
def get_agent_card(request: Request):

    forwarded_proto = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("host", str(request.url).split("//")[1].split("/")[0])
    base_url = f"{forwarded_proto}://{host}"

    logger.info("="*60)
    logger.info("AGENT CARD REQUEST")
    logger.info(f"Base URL: {base_url}")
    logger.info(f"x-forwarded-proto: {forwarded_proto}")
    logger.info(f"host: {host}")
    logger.info("="*60)

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
                "description": "Connects to Salesforce Agentforce and fetches support case updates.",
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "Check the status of my support case",
                    "Escalate this issue to Agentforce",
                    "What's the update on case #12345?"
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

    logger.info(f"Returning agent card with JSONRPC URL: {base_url}/json-rpc")
    return agent_card


# ------------------------------------------------------
# JSONRPC ENDPOINT (REQUIRED BY INSPECTOR)
# ------------------------------------------------------
@app.post("/json-rpc")
async def json_rpc_handler(payload: Dict[str, Any]):

    logger.info(f"JSONRPC REQUEST received")
    logger.info(f"Payload: {payload}")

    if "jsonrpc" not in payload or payload["jsonrpc"] != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32600, "message": "Invalid Request: jsonrpc version must be 2.0"}
        }

    method = payload.get("method")
    params = payload.get("params")
    rpc_id = payload.get("id")

    if method != "task":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }

    try:
        task_request = A2ATaskRequest(**params)
        logger.info(f"Task request validated: {task_request.taskId}")
    except ValidationError as e:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32602, "message": f"Invalid params: {str(e)}"}
        }

    response = await handle_a2a_task(task_request)

    return {"jsonrpc": "2.0", "id": rpc_id, "result": response}


# ------------------------------------------------------
# Main A2A Task Endpoint (Direct HTTP fallback)
# ------------------------------------------------------
@app.post("/tasks")
async def handle_a2a_task_endpoint(task_request: A2ATaskRequest):
    logger.info(f"Direct HTTP task request: {task_request.taskId}")
    return await handle_a2a_task(task_request)


# ------------------------------------------------------
# Salesforce Agentforce Integration (JWT-based)
# ------------------------------------------------------

def get_salesforce_token():
    """
    JWT Bearer OAuth flow for Salesforce / Agentforce.
    Uses:
      - SF_CLIENT_ID        → Connected App Consumer Key
      - SF_USERNAME         → Salesforce username for the user context
      - SF_JWT_PRIVATE_KEY  → PEM-encoded RSA private key (full text)
      - SF_LOGIN_HOST       → Optional, defaults to https://login.salesforce.com
    """
    login_host = os.getenv("SF_LOGIN_HOST", "https://login.salesforce.com")
    token_url = f"{login_host}/services/oauth2/token"

    client_id = os.getenv("SF_CLIENT_ID")
    username = os.getenv("SF_USERNAME")
    private_key = os.getenv("SF_JWT_PRIVATE_KEY")

    if not all([client_id, username, private_key]):
        raise ValueError("Missing SF_CLIENT_ID, SF_USERNAME, or SF_JWT_PRIVATE_KEY for JWT OAuth")

    payload = {
        "iss": client_id,
        "sub": username,
        "aud": login_host,
        "exp": int(time.time()) + 180
    }

    assertion = jwt.encode(payload, private_key, algorithm="RS256")

    response = requests.post(
        token_url,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion
        }
    )

    response.raise_for_status()
    token = response.json()["access_token"]
    return token


def query_agentforce(user_message: str, access_token: str):
    sf_instance = os.getenv("SF_INSTANCE")
    agent_id = os.getenv("SF_AGENT_ID", "0XxWt0000005qu1KAA")

    session_payload = {
        "externalSessionKey": str(uuid.uuid4()),
        "instanceConfig": {"endpoint": sf_instance},
        "streamingCapabilities": {"chunkTypes": ["Text"]},
        "bypassUser": True
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
    session_id = session_res.json()["sessionId"]

    message_payload = {
        "message": {
            "sequenceId": 1,
            "type": "Text",
            "text": user_message
        }
    }

    response = requests.post(
        f"https://api.salesforce.com/einstein/ai-agent/v1/sessions/{session_id}/messages/stream",
        json=message_payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        stream=True
    )

    final_msg = ""

    for line in response.iter_lines():
        if line and line.decode("utf-8").startswith("data: "):
            event = json.loads(line.decode("utf-8")[6:])
            if event.get("message", {}).get("type") == "Inform":
                final_msg = event["message"]["message"]
                break

    if not final_msg:
        final_msg = "No response received from Agentforce."

    return final_msg, []


# ------------------------------------------------------
# Core Task Handler
# ------------------------------------------------------
async def handle_a2a_task(task_request: A2ATaskRequest):

    task_id = task_request.taskId

    try:
        latest_message_content = task_request.inputs[-1].content[-1].value
    except Exception:
        return {
            "status": "failed",
            "taskId": task_id,
            "error": "Invalid or missing message inputs in A2A payload."
        }

    try:
        access_token = get_salesforce_token()
        agentforce_response, debug_info = query_agentforce(latest_message_content, access_token)
        response_text = f"Agentforce Response: {agentforce_response}"
    except Exception as e:
        response_text = f"⚠️ Failed to connect to Salesforce Agentforce: {str(e)}"
        debug_info = [str(e)]

    response = {
        "status": "completed",
        "taskId": task_id,
        "outputs": [
            {
                "kind": "message",
                "role": "agent",
                "parts": [
                    {"kind": "text", "text": response_text}
                ],
                "contextId": task_request.contextId or task_id
            }
        ]
    }

    if debug_info:
        response["debug"] = debug_info

    return response


# ------------------------------------------------------
# Health Check Endpoint
# ------------------------------------------------------
@app.get("/health")
def health_check():
    sf_configured = all([
        os.getenv("SF_CLIENT_ID"),
        os.getenv("SF_USERNAME"),
        os.getenv("SF_JWT_PRIVATE_KEY")
    ])

    return {
        "status": "healthy",
        "version": "2.0.1",
        "service": "Agentforce A2A Server",
        "salesforce_configured": sf_configured
    }


# ------------------------------------------------------
# Root Endpoint
# ------------------------------------------------------
@app.get("/")
def root(request: Request):

    forwarded_proto = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("host", "localhost")
    base_url = f"{forwarded_proto}://{host}"

    return {
        "service": "Agentforce A2A Server",
        "version": "2.0.1",
        "status": "running",
        "deployment": "Heroku-ready",
        "base_url": base_url,
        "endpoints": {
            "agent_card": f"{base_url}/.well-known/agent-card.json",
            "jsonrpc": f"{base_url}/json-rpc",
            "tasks": f"{base_url}/tasks",
            "health": f"{base_url}/health"
        },
        "salesforce_configured": all([
            os.getenv("SF_CLIENT_ID"),
            os.getenv("SF_USERNAME"),
            os.getenv("SF_JWT_PRIVATE_KEY")
        ]),
        "instructions": "Use the agent_card URL in A2A Inspector to connect"
    }


# ------------------------------------------------------
# FIXED root_post_handler (UNCHANGED)
# ------------------------------------------------------
@app.post("/")
async def root_post_handler(request: Request):
    body = await request.json()
    logger.info(f"Root POST raw payload: {body}")

    # Case 1: Already a proper A2A TaskRequest → process normally
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
                "parts": [
                    {"kind": "text", "text": result["outputs"][0]["parts"][0]["text"]}
                ]
            }
        }
    except Exception:
        pass  # Not task-mode → continue

    # Case 2: Fabric "message/send" envelope
    if body.get("method") == "message/send" and "params" in body:

        msg = body["params"]["message"]
        text = msg["parts"][0]["text"]

        task_request = A2ATaskRequest(
            taskId=str(uuid.uuid4()),
            skillId="case-escalation",
            inputs=[
                A2AInput(
                    role=msg.get("role", "user"),
                    content=[ContentPart(type="text/plain", value=text)]
                )
            ],
            contextId=msg.get("contextId")
        )

        logger.info(f"Converted Fabric message → A2ATaskRequest: {task_request}")

        result = await handle_a2a_task(task_request)
        agent_response = result["outputs"][0]["parts"][0]["text"]

        return {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "kind": "message",
                "role": "agent",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": agent_response}]
            }
        }

    # Fallback
    return JSONResponse(
        status_code=400,
        content={"error": "Unrecognized payload format", "body": body}
    )


# ------------------------------------------------------
# HEROKU SPECIFIC ENTRYPOINT (UNCHANGED)
# ------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
