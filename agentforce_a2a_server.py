# --- Agentforce A2A Server (Fixed & Heroku Ready) ---
# Version 2.0.0

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
#   Agent Card (JSONRPC Compatible)
# -----------------------------

@app.get("/.well-known/agent-card.json")
def get_agent_card(request: Request):
    """
    Returns the agent card with proper JSONRPC transport configuration.
    HEROKU COMPATIBLE: Detects HTTPS via x-forwarded-proto header
    """
    
    # Detect base URL properly for Heroku
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
        "version": "2.0.0",
        
        # Optional vendor information
        "vendor": "Salesforce",
        "apiVersion": "1.0.0",
        
        # Inspector-compatible capabilities
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
        
        # CRITICAL: A2A Inspector requires JSONRPC transport
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
#   JSONRPC ENDPOINT (REQUIRED BY INSPECTOR)
# ------------------------------------------------------

@app.post("/json-rpc")
async def json_rpc_handler(payload: Dict[str, Any]):
    """
    JSONRPC 2.0 endpoint to satisfy A2A Inspector requirements.
    Routes method "task" to the task handler.
    """
    
    logger.info(f"JSONRPC REQUEST received")
    logger.info(f"Payload: {payload}")
    
    # Validate JSONRPC structure
    if "jsonrpc" not in payload or payload["jsonrpc"] != "2.0":
        logger.warning(f"Invalid JSONRPC version: {payload.get('jsonrpc')}")
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {
                "code": -32600,
                "message": "Invalid Request: jsonrpc version must be 2.0"
            }
        }
    
    method = payload.get("method")
    params = payload.get("params")
    rpc_id = payload.get("id")
    
    # Only support "task" method
    if method != "task":
        logger.warning(f"Unknown method: {method}")
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}"
            }
        }
    
    # Validate and parse task parameters
    try:
        task_request = A2ATaskRequest(**params)
        logger.info(f"Task request validated: {task_request.taskId}")
    except ValidationError as e:
        logger.error(f"Invalid task parameters: {str(e)}")
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32602,
                "message": f"Invalid params: {str(e)}"
            }
        }
    except Exception as e:
        logger.error(f"Unexpected error parsing params: {str(e)}")
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32603,
                "message": f"Internal error: {str(e)}"
            }
        }
    
    # Process the task
    try:
        response = await handle_a2a_task(task_request)
        logger.info(f"Task {task_request.taskId} completed successfully")
        
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": response
        }
    except Exception as e:
        logger.error(f"Error processing task: {str(e)}", exc_info=True)
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32603,
                "message": f"Internal error processing task: {str(e)}"
            }
        }


# ------------------------------------------------------
#   Main A2A Task Endpoint (Direct HTTP fallback)
# ------------------------------------------------------

@app.post("/tasks")
async def handle_a2a_task_endpoint(task_request: A2ATaskRequest):
    """
    Direct HTTP endpoint for task processing (fallback).
    Most A2A clients will use JSONRPC, but this provides compatibility.
    """
    logger.info(f"Direct HTTP task request: {task_request.taskId}")
    return await handle_a2a_task(task_request)


# ------------------------------------------------------
#   Salesforce Agentforce Integration
# ------------------------------------------------------

def get_salesforce_token():
    """
    Authenticates with Salesforce and returns an access token.
    """
    sf_instance = os.getenv("SF_INSTANCE")
    client_id = os.getenv("SF_CLIENT_ID")
    client_secret = os.getenv("SF_CLIENT_SECRET")
    
    if not all([sf_instance, client_id, client_secret]):
        raise ValueError("Missing Salesforce credentials in environment variables")
    
    url = f"{sf_instance}/services/oauth2/token"
    
    logger.info(f"Authenticating with Salesforce: {sf_instance}")
    
    response = requests.post(url, data={
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret
    })
    
    response.raise_for_status()
    access_token = response.json()["access_token"]
    
    logger.info("Successfully authenticated with Salesforce")
    
    return access_token


def query_agentforce(user_message: str, access_token: str):
    """
    Sends a message to Salesforce Agentforce and returns the response.
    """
    sf_instance = os.getenv("SF_INSTANCE")
    agent_id = os.getenv("SF_AGENT_ID", "0XxWt0000005qu1KAA")  # Default or from env
    
    # Step 1: Create session
    session_payload = {
        "externalSessionKey": str(uuid.uuid4()),
        "instanceConfig": {"endpoint": sf_instance},
        "streamingCapabilities": {"chunkTypes": ["Text"]},
        "bypassUser": True
    }
    
    logger.info(f"Creating Agentforce session for agent: {agent_id}")
    
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
    
    logger.info(f"Created session: {session_id}")
    
    # Step 2: Send message
    message_payload = {
        "message": {
            "sequenceId": 1,
            "type": "Text",
            "text": user_message
        }
    }
    
    logger.info(f"Sending message to Agentforce: {user_message[:50]}...")
    
    response = requests.post(
        f"https://api.salesforce.com/einstein/ai-agent/v1/sessions/{session_id}/messages/stream",
        json=message_payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        stream=True
    )
    
    # Step 3: Parse streaming response
    final_msg = ""
    debug_lines = []
    
    for line in response.iter_lines():
        if line and line.decode("utf-8").startswith("data: "):
            try:
                decoded = line.decode("utf-8")
                debug_lines.append(f"RAW: {decoded}")
                
                event = json.loads(decoded[6:])
                debug_lines.append(f"EVENT: {json.dumps(event)}")
                
                # Look for the final Inform message
                if event.get("message", {}).get("type") == "Inform":
                    final_msg = event["message"]["message"]
                    logger.info(f"Received Agentforce response: {final_msg[:50]}...")
                    break
            except Exception as e:
                debug_lines.append(f"Error parsing line: {str(e)}")
                logger.warning(f"Error parsing stream line: {str(e)}")
                continue
    
    if not final_msg:
        final_msg = "No response received from Agentforce. Please check debug logs."
        logger.warning("No Inform message received from Agentforce")
    
    return final_msg, debug_lines


# ------------------------------------------------------
#   Core Task Handler Logic
# ------------------------------------------------------

async def handle_a2a_task(task_request: A2ATaskRequest):
    """
    Core logic for handling A2A tasks.
    Routes to Salesforce Agentforce and returns the response.
    """
    
    task_id = task_request.taskId
    skill_id = task_request.skillId
    
    logger.info(f"Processing Task ID: {task_id}, Skill ID: {skill_id}")
    
    # Extract the latest message
    try:
        latest_message_content = task_request.inputs[-1].content[-1].value
    except (IndexError, AttributeError) as e:
        error_msg = "Invalid or missing message inputs in A2A payload."
        logger.error(f"{error_msg}: {str(e)}")
        return {
            "status": "failed",
            "taskId": task_id,
            "error": error_msg
        }
    
    logger.info(f"User message: {latest_message_content[:80]}...")
    
    # Call Salesforce Agentforce
    try:
        access_token = get_salesforce_token()
        agentforce_response, debug_info = query_agentforce(latest_message_content, access_token)
        
        response_text = f"Agentforce Response: {agentforce_response}"
        
    except ValueError as e:
        # Missing credentials
        logger.error(f"Configuration error: {str(e)}")
        response_text = (
            "⚠️ Agentforce integration not configured. "
            "Please set SF_INSTANCE, SF_CLIENT_ID, and SF_CLIENT_SECRET environment variables."
        )
        debug_info = [str(e)]
        
    except requests.exceptions.RequestException as e:
        # API call failed
        logger.error(f"Salesforce API error: {str(e)}")
        response_text = (
            f"⚠️ Failed to connect to Salesforce Agentforce: {str(e)}"
        )
        debug_info = [str(e)]
        
    except Exception as e:
        # Unexpected error
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        response_text = f"⚠️ An error occurred: {str(e)}"
        debug_info = [str(e)]
    
    # Return properly formatted A2A response
    response = {
        "status": "completed",
        "taskId": task_id,
        "outputs": [
            {
                "kind": "message",
                "role": "agent",
                "parts": [
                    {
                        "kind": "text",
                        "text": response_text
                    }
                ],
                "contextId": task_request.contextId or task_id
            }
        ]
    }
    
    # Add debug info if available (for troubleshooting)
    if debug_info:
        response["debug"] = debug_info
    
    return response


# ------------------------------------------------------
#   Health Check Endpoint
# ------------------------------------------------------

@app.get("/health")
def health_check():
    """Health check endpoint - Heroku uses this to verify the app is running"""
    
    # Check if Salesforce credentials are configured
    sf_configured = all([
        os.getenv("SF_INSTANCE"),
        os.getenv("SF_CLIENT_ID"),
        os.getenv("SF_CLIENT_SECRET")
    ])
    
    return {
        "status": "healthy",
        "version": "2.0.0",
        "service": "Agentforce A2A Server",
        "salesforce_configured": sf_configured
    }


# ------------------------------------------------------
#   Root Endpoint
# ------------------------------------------------------

@app.get("/")
def root(request: Request):
    """Root endpoint with service information"""
    
    forwarded_proto = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("host", "localhost")
    base_url = f"{forwarded_proto}://{host}"
    
    return {
        "service": "Agentforce A2A Server",
        "version": "2.0.0",
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
            os.getenv("SF_INSTANCE"),
            os.getenv("SF_CLIENT_ID"),
            os.getenv("SF_CLIENT_SECRET")
        ]),
        "instructions": "Use the agent_card URL in A2A Inspector to connect"
    }

@app.post("/")
async def root_post_handler(request: Request):
    body = await request.json()
    logger.info(f"Root POST raw payload: {body}")

    # If this is already an A2ATaskRequest, process normally
    try:
        task_request = A2ATaskRequest(**body)
        return await handle_a2a_task(task_request)
    except Exception:
        pass  # Not A2ATaskRequest, try Fabric format

    # Fabric sends message envelope → convert to A2ATaskRequest
    if "message" in body:
        msg = body["message"]
        text = msg["parts"][0]["text"]

        task_request = A2ATaskRequest(
            taskId=str(uuid.uuid4()),
            skillId="case-escalation",
            inputs=[
                A2AInput(
                    role=msg.get("role", "user"),
                    content=[
                        ContentPart(type="text/plain", value=text)
                    ]
                )
            ],
            contextId=msg.get("contextId", None)
        )

        logger.info(f"Converted Fabric message → A2ATaskRequest: {task_request}")
        return await handle_a2a_task(task_request)

    return JSONResponse(
        status_code=400,
        content={"error": "Unrecognized payload format", "body": body}
    )


# HEROKU SPECIFIC: Application must bind to the PORT environment variable
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Agentforce A2A Server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
