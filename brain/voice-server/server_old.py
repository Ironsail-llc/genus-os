#!/usr/bin/env python3
"""
Robothor Voice Server
Real-time phone conversations via Twilio ConversationRelay + Claude

Usage:
  python server.py [--port PORT]
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

import google.generativeai as genai
import websockets
from openai import OpenAI
from websockets.datastructures import Headers
from websockets.http11 import Response

# Configure Gemini as primary
genai.configure(api_key=os.environ.get("GOOGLE_API_KEY", ""))

# Load soul and context
WORKSPACE = Path(__file__).parent.parent
SOUL_PATH = WORKSPACE / "SOUL.md"
USER_PATH = WORKSPACE / "USER.md"


def load_context():
    """Load Robothor's soul and user context"""
    context = ""

    if SOUL_PATH.exists():
        context += f"# Your Identity\n{SOUL_PATH.read_text()}\n\n"

    if USER_PATH.exists():
        context += f"# About the Person You're Speaking With\n{USER_PATH.read_text()}\n\n"

    context += """
# Voice Conversation Guidelines
- You are on a PHONE CALL with Philip. Speak naturally and conversationally.
- Keep responses concise - this is a phone call, not a text chat.
- Don't use markdown, bullet points, or formatting - just natural speech.
- You can use filler words occasionally to sound natural (well, hmm, etc.)
- If you don't understand something, ask for clarification.
- Remember: short, punchy responses work best on phone calls.
"""
    return context


SYSTEM_PROMPT = load_context()


class VoiceSession:
    """Manages a single voice call session"""

    def __init__(self, websocket, call_sid: str, caller: str):
        self.websocket = websocket
        self.call_sid = call_sid
        self.caller = caller
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.gemini_model = genai.GenerativeModel(
            model_name="gemini-2.0-flash", system_instruction=SYSTEM_PROMPT
        )
        self.gemini_chat = self.gemini_model.start_chat(history=[])
        self.openai_client = OpenAI()
        self.using_fallback = False
        print(f"[{call_sid}] New session from {caller}")

    async def handle_prompt(self, voice_prompt: str):
        """Handle incoming speech from caller"""
        print(f"[{self.call_sid}] Caller: {voice_prompt}")

        # Add to conversation history for fallback
        self.messages.append({"role": "user", "content": voice_prompt})

        assistant_message = None

        # Try Gemini first (unless already using fallback)
        if not self.using_fallback:
            try:
                response = self.gemini_chat.send_message(voice_prompt)
                assistant_message = response.text
                print(f"[{self.call_sid}] Robothor (Gemini): {assistant_message}")
            except Exception as e:
                print(f"[{self.call_sid}] Gemini failed: {e}, trying GPT-4o...")
                self.using_fallback = True

        # Fallback to GPT-4o
        if assistant_message is None:
            try:
                response = self.openai_client.chat.completions.create(
                    model="gpt-4o", max_tokens=300, messages=self.messages
                )
                assistant_message = response.choices[0].message.content
                print(f"[{self.call_sid}] Robothor (GPT-4o): {assistant_message}")
            except Exception as e:
                print(f"[{self.call_sid}] GPT-4o also failed: {e}")
                assistant_message = "I'm having trouble thinking right now. Can you repeat that?"

        # Update history and send response
        self.messages.append({"role": "assistant", "content": assistant_message})
        await self.send_text(assistant_message)

    async def send_text(self, text: str):
        """Send text response to be spoken"""
        message = {"type": "text", "token": text, "last": True}
        await self.websocket.send(json.dumps(message))


async def handle_connection(websocket):
    """Handle a WebSocket connection from Twilio"""
    session = None

    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "setup":
                # New call connected
                call_sid = data.get("callSid", "unknown")
                caller = data.get("from", "unknown")
                session = VoiceSession(websocket, call_sid, caller)

            elif msg_type == "prompt" and session:
                # Caller said something
                voice_prompt = data.get("voicePrompt", "")
                if voice_prompt and data.get("last", True):
                    await session.handle_prompt(voice_prompt)

            elif msg_type == "interrupt" and session:
                # Caller interrupted - could handle this
                print(f"[{session.call_sid}] Interrupted")

            elif msg_type == "dtmf" and session:
                # Keypad press
                digit = data.get("digit", "")
                print(f"[{session.call_sid}] DTMF: {digit}")

            elif msg_type == "error":
                print(f"Error from Twilio: {data.get('description', 'unknown')}")

    except websockets.exceptions.ConnectionClosed:
        if session:
            print(f"[{session.call_sid}] Call ended")
    except Exception as e:
        print(f"Connection error: {e}")


async def process_request(connection, request):
    """Handle non-WebSocket HTTP requests gracefully"""
    # Check if this is a WebSocket upgrade request
    upgrade_header = request.headers.get("Upgrade", "")
    if upgrade_header.lower() != "websocket":
        # Return 200 OK for health checks
        return Response(200, "OK", Headers(), b"Robothor Voice Server OK\n")
    # Return None to proceed with WebSocket handshake
    return None


async def main(port: int):
    """Start the WebSocket server"""
    print(f"🎙️  Robothor Voice Server starting on port {port}")
    print(f"📍 WebSocket URL: ws://localhost:{port}")
    print(f"🧠 Soul loaded from: {SOUL_PATH}")
    print("-" * 50)

    async with websockets.serve(
        handle_connection, "0.0.0.0", port, process_request=process_request
    ):
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Robothor Voice Server")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    args = parser.parse_args()

    asyncio.run(main(args.port))
