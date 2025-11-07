# --- Agentforce A2A Server ---
# File: agentforce_a2a_server.py

from fastapi import FastAPI, Request
from pydantic import BaseModel
import os
import requests
import uuid
import json

app = FastAPI()

class Part(BaseModel):
    text: str

class Message(BaseModel):
    role: str
    parts: list[Part]
    messageId: str

class TaskRequest(BaseModel):
    id: str
    message: Message

@app.get("/.well-known/agent-card.json")
def agent_card():
    base_url = os.getenv("BASE_URL", "http://localhost:9001")
    return {
        "name": "Agentforce A2A",
        "description": "Escalates to Salesforce AI support agent.",
        "url": base_url,
        "version": "1.0",
        "protocolVersion": "0.3.0",
        "preferredTransport": "HTTP+JSON",
        "additionalInterfaces": [
            {
                "url": base_url,
                "transport": "HTTP+JSON"
            }
        ],
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "case-escalation",
                "name": "Case Escalation",
                "description": "Connects to Salesforce Agentforce and fetches support case updates.",
                "tags": ["salesforce", "agentforce", "support"]
            }
        ]
    }

@app.post("/tasks/send")
def handle_task(task: TaskRequest):
    sf_instance = os.getenv("SF_INSTANCE")
    url = f"{sf_instance}/services/oauth2/token"
    token_response = requests.post(url, data={
        'grant_type': 'client_credentials',
        'client_id': os.getenv("SF_CLIENT_ID"),
        'client_secret': os.getenv("SF_CLIENT_SECRET")
    })
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]

    session_payload = {
        "externalSessionKey": str(uuid.uuid4()),
        "instanceConfig": {"endpoint": sf_instance},
        "streamingCapabilities": {"chunkTypes": ["Text"]},
        "bypassUser": True
    }
    session_res = requests.post(
        f"https://api.salesforce.com/einstein/ai-agent/v1/agents/0XxWt0000005qu1KAA/sessions",
        json=session_payload,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    )
    session_id = session_res.json()["sessionId"]

    message_payload = {
        "message": {
            "sequenceId": 1,
            "type": "Text",
            "text": task.message.parts[0].text
        }
    }
    response = requests.post(
        f"https://api.salesforce.com/einstein/ai-agent/v1/sessions/{session_id}/messages/stream",
        json=message_payload,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        stream=True
    )

    final_msg = ""
    debug_lines = []
    for line in response.iter_lines():
        if line and line.decode("utf-8").startswith("data: "):
            try:
                decoded = line.decode("utf-8")
                debug_lines.append(f"RAW: {decoded}")
                event = json.loads(decoded[6:])
                debug_lines.append(f"EVENT: {json.dumps(event)}")
                if event.get("message", {}).get("type") == "Inform":
                    final_msg = event["message"]["message"]
                    break
            except Exception as e:
                debug_lines.append(f"Error parsing line: {str(e)}")
                continue

    return {
        "id": task.id,
        "status": {"state": "completed"},
        "messages": [
            task.message.dict(),
            {"role": "agent", "parts": [{"text": final_msg}]},
        ],
        "debug": debug_lines
    }
