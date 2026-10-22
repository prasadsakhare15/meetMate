import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

import users
from auth import get_verified_user_id
from session import MeetingSession

load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI(
    title="Meeting Assistant Cloud",
    description="Cloud backend for meeting context, transcripts, and LLM suggestions.",
    version="2.0.0",
)




@app.get("/health")
async def health():
    return {"status": "ok"}


# =================================================
# AUTH
# =================================================

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class UpdatePasswordRequest(BaseModel):
    username: str
    old_password: str
    new_password: str


class TokenResponse(BaseModel):
    token: str
    expires_at: str
    user_id: str
    username: str


@app.post("/auth/register", response_model=TokenResponse)
async def auth_register(body: RegisterRequest):
    """Create an account and immediately return a token."""

    try:
        user_id = users.register(body.username, body.password)
    except users.UsernameTakenError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    token, _, expires_at = users.create_access_token(user_id)
    user = users.get_user(user_id)

    return TokenResponse(
        token=token,
        expires_at=expires_at.isoformat(),
        user_id=user_id,
        username=user["username"],
    )


@app.post("/auth/login", response_model=TokenResponse)
async def auth_login(body: LoginRequest):
    """Exchange credentials for a 12-hour token."""

    user_id = users.authenticate(body.username, body.password)

    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    token, _, expires_at = users.create_access_token(user_id)
    user = users.get_user(user_id)

    return TokenResponse(
        token=token,
        expires_at=expires_at.isoformat(),
        user_id=user_id,
        username=user["username"],
    )


@app.post("/auth/update-password")
async def auth_update_password(body: UpdatePasswordRequest):
    """Change user password, requires old password."""
    user_id = users.authenticate(body.username, body.old_password)
    
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid username or current password.")
        
    try:
        users.update_password(body.username, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
        
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    user_id = await get_verified_user_id(websocket)
    if user_id is None:
        # get_verified_user_id closed the socket with 4401; just return.
        return

    session = MeetingSession(websocket, user_id)

    try:
        async with session:
            await session.run()
    except WebSocketDisconnect:
        pass


def main():
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))

    print(f"Meeting Assistant cloud server on ws://{host}:{port}/ws")

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
