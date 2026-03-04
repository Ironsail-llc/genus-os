#!/usr/bin/env python3
"""
Robothor Voice Server v2 - Gemini Live Native Audio

Uses Gemini 2.5 Flash Native Audio for true audio-to-audio conversation.
No STT/TTS chain - direct audio processing.

Architecture:
  Twilio Media Streams <-> This Server <-> Gemini Live API
       (mulaw 8kHz)                        (PCM 16kHz)
"""

import asyncio
import base64
import json
import os
import struct
from pathlib import Path
from urllib.parse import quote

import aiohttp
import websockets
from aiohttp import web
from twilio.rest import Client as TwilioClient

# Vertex AI config
PROJECT_ID = "robothor-485903"
LOCATION = "us-central1"
MODEL_NAME = "gemini-live-2.5-flash-native-audio"
VERTEX_AI_URL = f"wss://{LOCATION}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent"

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
- You are Robothor, on a PHONE CALL. Speak naturally and conversationally.
- Keep responses concise - this is a phone call, not a text chat.
- Don't describe actions or use formatting - just speak naturally.
- Use a warm, professional tone. You're Philip's AI assistant.
- If you don't understand something, ask for clarification.
- Short, punchy responses work best. Be direct.
"""
    return context


SYSTEM_PROMPT = load_context()

# Store for outbound call context (call_sid -> context)
CALL_CONTEXTS = {}

# Twilio client for outbound calls
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE = "+14134086025"


def get_twilio_client() -> TwilioClient:
    """Get Twilio REST client (lazy init)"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def build_outbound_prompt(recipient: str, purpose: str) -> str:
    """Build system prompt for outbound calls"""
    base = load_context()

    outbound_context = f"""
# OUTBOUND CALL CONTEXT
You are making an OUTBOUND call to: {recipient}
Purpose of this call: {purpose}

IMPORTANT: 
- This is NOT a call from Philip. You are CALLING someone on Philip's behalf.
- Start by introducing yourself: "Hi, this is Robothor, Philip's AI assistant."
- Then immediately state the purpose of your call.
- Be professional and friendly.
- Keep it brief - deliver the message and ask if they have questions.
"""
    return base + outbound_context


def mulaw_to_pcm16(mulaw_data: bytes) -> bytes:
    """Convert mu-law 8kHz to PCM 16-bit"""
    MULAW_DECODE = []
    for i in range(256):
        i_inv = ~i & 0xFF
        sign = i_inv & 0x80
        exponent = (i_inv >> 4) & 0x07
        mantissa = i_inv & 0x0F
        sample = (mantissa << 3) + 0x84
        sample <<= exponent
        sample -= 0x84
        if sign:
            sample = -sample
        MULAW_DECODE.append(sample)

    pcm_samples = [MULAW_DECODE[byte] for byte in mulaw_data]
    return struct.pack(f"<{len(pcm_samples)}h", *pcm_samples)


def pcm16_to_mulaw(pcm_data: bytes) -> bytes:
    """Convert PCM 16-bit to mu-law"""
    MULAW_MAX = 0x1FFF
    MULAW_BIAS = 33

    def encode_sample(sample):
        sign = 0
        if sample < 0:
            sign = 0x80
            sample = -sample

        sample = min(sample + MULAW_BIAS, MULAW_MAX)

        exponent = 7
        for exp in range(8):
            if sample < (1 << (exp + 8)):
                exponent = exp
                break

        mantissa = (sample >> (exponent + 3)) & 0x0F
        encoded = ~(sign | (exponent << 4) | mantissa) & 0xFF
        return encoded

    samples = struct.unpack(f"<{len(pcm_data) // 2}h", pcm_data)
    return bytes([encode_sample(s) for s in samples])


def resample_8k_to_16k(audio_8k: bytes) -> bytes:
    """Resample 8kHz PCM to 16kHz PCM (linear interpolation)"""
    samples_8k = struct.unpack(f"<{len(audio_8k) // 2}h", audio_8k)
    samples_16k = []

    for i in range(len(samples_8k) - 1):
        samples_16k.append(samples_8k[i])
        mid = (samples_8k[i] + samples_8k[i + 1]) // 2
        samples_16k.append(mid)

    if samples_8k:
        samples_16k.append(samples_8k[-1])
        samples_16k.append(samples_8k[-1])

    return struct.pack(f"<{len(samples_16k)}h", *samples_16k)


def resample_8k_to_24k(audio_8k: bytes) -> bytes:
    """Resample 8kHz PCM to 24kHz PCM (interpolate by 3)"""
    samples_8k = struct.unpack(f"<{len(audio_8k) // 2}h", audio_8k)
    samples_24k = []

    for i in range(len(samples_8k) - 1):
        s0, s1 = samples_8k[i], samples_8k[i + 1]
        samples_24k.append(s0)
        samples_24k.append(s0 + (s1 - s0) // 3)
        samples_24k.append(s0 + 2 * (s1 - s0) // 3)

    if samples_8k:
        samples_24k.append(samples_8k[-1])
        samples_24k.append(samples_8k[-1])
        samples_24k.append(samples_8k[-1])

    return struct.pack(f"<{len(samples_24k)}h", *samples_24k)


def resample_16k_to_8k(audio_16k: bytes) -> bytes:
    """Resample 16kHz PCM to 8kHz PCM (decimate)"""
    samples_16k = struct.unpack(f"<{len(audio_16k) // 2}h", audio_16k)
    samples_8k = samples_16k[::2]
    return struct.pack(f"<{len(samples_8k)}h", *samples_8k)


def resample_24k_to_8k(audio_24k: bytes) -> bytes:
    """Resample 24kHz PCM to 8kHz PCM (decimate by 3)"""
    samples_24k = struct.unpack(f"<{len(audio_24k) // 2}h", audio_24k)
    samples_8k = samples_24k[::3]  # Take every 3rd sample
    return struct.pack(f"<{len(samples_8k)}h", *samples_8k)


class GeminiLiveSession:
    """Manages a Gemini Live API session"""

    def __init__(self, call_sid: str, system_prompt: str = None):
        self.call_sid = call_sid
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.gemini_ws: websockets.WebSocketClientProtocol | None = None
        self.connected = False
        self.setup_complete = False

    async def connect(self):
        """Connect to Gemini Live API via Vertex AI"""
        try:
            import subprocess

            result = subprocess.run(
                ["gcloud", "auth", "print-access-token"], capture_output=True, text=True
            )
            access_token = result.stdout.strip()

            headers = {"Authorization": f"Bearer {access_token}"}
            self.gemini_ws = await websockets.connect(VERTEX_AI_URL, additional_headers=headers)
            self.connected = True
            print(f"[{self.call_sid}] Connected to Gemini Live API (Vertex AI)")

            setup_msg = {
                "setup": {
                    "model": f"projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models/{MODEL_NAME}",
                    "generation_config": {
                        "response_modalities": ["AUDIO"],
                        "speech_config": {
                            "voice_config": {"prebuilt_voice_config": {"voice_name": "Charon"}}
                        },
                    },
                    "input_audio_transcription": {},
                    "output_audio_transcription": {},
                    "system_instruction": {"parts": [{"text": self.system_prompt}]},
                }
            }
            await self.gemini_ws.send(json.dumps(setup_msg))

            response = await self.gemini_ws.recv()
            data = json.loads(response)
            if "setupComplete" in data:
                self.setup_complete = True
                print(f"[{self.call_sid}] Gemini Live setup complete")
            else:
                print(f"[{self.call_sid}] Unexpected setup response: {data}")

        except Exception as e:
            print(f"[{self.call_sid}] Failed to connect to Gemini: {e}")
            self.connected = False

    async def send_audio(self, audio_pcm16_16k: bytes):
        """Send audio to Gemini"""
        if not self.connected or not self.gemini_ws:
            return

        try:
            audio_b64 = base64.b64encode(audio_pcm16_16k).decode("utf-8")
            msg = {
                "realtime_input": {
                    "media_chunks": [{"data": audio_b64, "mime_type": "audio/pcm;rate=16000"}]
                }
            }
            await self.gemini_ws.send(json.dumps(msg))
        except Exception as e:
            print(f"[{self.call_sid}] Error sending audio to Gemini: {e}")

    async def receive_audio(self):
        """Receive audio from Gemini (generator)"""
        if not self.connected or not self.gemini_ws:
            print(f"[{self.call_sid}] receive_audio: not connected")
            return

        try:
            async for message in self.gemini_ws:
                data = json.loads(message)

                # Log ALL message types for debugging
                print(f"[{self.call_sid}] Gemini msg keys: {list(data.keys())}")

                if "serverContent" in data:
                    content = data["serverContent"]

                    # Check for transcriptions (inside serverContent)
                    if "inputTranscription" in content:
                        transcript = content["inputTranscription"]
                        text = (
                            transcript.get("text", "")
                            if isinstance(transcript, dict)
                            else transcript
                        )
                        print(f"[{self.call_sid}] 📝 USER SAID: {text}")

                    if "outputTranscription" in content:
                        transcript = content["outputTranscription"]
                        text = (
                            transcript.get("text", "")
                            if isinstance(transcript, dict)
                            else transcript
                        )
                        print(f"[{self.call_sid}] 🎤 ROBOTHOR SAID: {text}")

                    if "modelTurn" in content:
                        parts = content["modelTurn"].get("parts", [])
                        for part in parts:
                            part_keys = list(part.keys())
                            if "inlineData" in part:
                                inline = part["inlineData"]
                                mime = inline.get("mimeType", "")
                                if "audio" in mime:
                                    audio_b64 = inline.get("data", "")
                                    audio_pcm = base64.b64decode(audio_b64)
                                    yield audio_pcm
                                else:
                                    print(f"[{self.call_sid}] Gemini inlineData: {mime}")
                            elif "text" in part:
                                print(f"[{self.call_sid}] 📝 MODEL TEXT: {part['text']}")
                            else:
                                print(f"[{self.call_sid}] Part keys: {part_keys}")

                    if content.get("turnComplete"):
                        print(f"[{self.call_sid}] Gemini turn complete")

                    # Check for grounding/other content
                    if "groundingMetadata" in content:
                        print(f"[{self.call_sid}] Grounding: {content['groundingMetadata']}")

        except websockets.exceptions.ConnectionClosed:
            print(f"[{self.call_sid}] Gemini connection closed")
            self.connected = False
        except Exception as e:
            print(f"[{self.call_sid}] Error receiving from Gemini: {e}")

    async def close(self):
        """Close the Gemini connection"""
        if self.gemini_ws:
            await self.gemini_ws.close()
            self.connected = False


class VoiceCallBridge:
    """Bridges Twilio Media Streams with Gemini Live"""

    def __init__(
        self, twilio_ws: web.WebSocketResponse, call_sid: str, caller: str, stream_sid: str
    ):
        self.twilio_ws = twilio_ws
        self.call_sid = call_sid
        self.caller = caller
        self.stream_sid = stream_sid  # Already known from start event
        self.running = False

        # Check for outbound call context
        call_context = CALL_CONTEXTS.get(call_sid)
        if call_context:
            recipient = call_context.get("recipient", "someone")
            purpose = call_context.get("purpose", "a call from Philip")
            system_prompt = build_outbound_prompt(recipient, purpose)
            print(f"[{call_sid}] Outbound call to {recipient}: {purpose}")
            # Clean up context
            del CALL_CONTEXTS[call_sid]
        else:
            system_prompt = SYSTEM_PROMPT
            print(f"[{call_sid}] Inbound call from {caller}")

        self.gemini = GeminiLiveSession(call_sid, system_prompt)
        print(f"[{call_sid}] Stream={stream_sid}")

    async def start(self):
        """Start the bridge"""
        await self.gemini.connect()
        if not self.gemini.connected:
            print(f"[{self.call_sid}] Failed to connect to Gemini, call will fail")
            return

        self.running = True

        twilio_task = asyncio.create_task(self.handle_twilio_audio())
        gemini_task = asyncio.create_task(self.handle_gemini_audio())

        try:
            await asyncio.gather(twilio_task, gemini_task)
        except Exception as e:
            print(f"[{self.call_sid}] Bridge error: {e}")
        finally:
            self.running = False
            await self.gemini.close()

    async def handle_twilio_audio(self):
        """Receive audio from Twilio, send to Gemini"""
        audio_buffer = b""
        print(f"[{self.call_sid}] Starting audio handler, stream={self.stream_sid}")

        try:
            async for msg in self.twilio_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    event = data.get("event")

                    if event == "media":
                        payload = data.get("media", {}).get("payload", "")
                        if payload:
                            mulaw_audio = base64.b64decode(payload)
                            pcm_8k = mulaw_to_pcm16(mulaw_audio)
                            pcm_16k = resample_8k_to_16k(pcm_8k)  # 8kHz -> 16kHz for Gemini input

                            audio_buffer += pcm_16k

                            # Send every ~100ms at 16kHz (3200 bytes)
                            while len(audio_buffer) >= 3200:
                                chunk = audio_buffer[:3200]
                                audio_buffer = audio_buffer[3200:]
                                await self.gemini.send_audio(chunk)

                    elif event == "stop":
                        print(f"[{self.call_sid}] Media stream stopped")
                        self.running = False
                        break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print(f"[{self.call_sid}] WebSocket error")
                    break

        except Exception as e:
            print(f"[{self.call_sid}] Twilio handler error: {e}")
            self.running = False

    async def handle_gemini_audio(self):
        """Receive audio from Gemini, send to Twilio"""
        print(f"[{self.call_sid}] Starting Gemini audio receiver")
        audio_chunks_sent = 0

        try:
            async for pcm_16k in self.gemini.receive_audio():
                if not self.running or not self.stream_sid:
                    print(
                        f"[{self.call_sid}] Stopping gemini handler: running={self.running}, stream={self.stream_sid}"
                    )
                    break

                print(f"[{self.call_sid}] Received {len(pcm_16k)} bytes from Gemini")

                # Gemini native audio outputs 24kHz PCM
                pcm_8k = resample_24k_to_8k(pcm_16k)  # 24kHz -> 8kHz
                mulaw_audio = pcm16_to_mulaw(pcm_8k)

                media_msg = {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(mulaw_audio).decode("utf-8")},
                }
                await self.twilio_ws.send_str(json.dumps(media_msg))
                audio_chunks_sent += 1
                print(f"[{self.call_sid}] Sent {len(mulaw_audio)} bytes to Twilio")

            print(f"[{self.call_sid}] Gemini audio loop ended, sent {audio_chunks_sent} chunks")

        except Exception as e:
            import traceback

            print(f"[{self.call_sid}] Error in Gemini handler: {e}")
            traceback.print_exc()


async def handle_twiml(request):
    """Return TwiML that connects to our WebSocket"""
    # Always use our public hostname through Cloudflare tunnel
    ws_url = "wss://voice.robothor.ai/media-stream"

    # Check for outbound call context in query params
    recipient = request.query.get("recipient", "")
    purpose = request.query.get("purpose", "")
    call_sid = request.query.get("CallSid", "")  # Twilio passes this

    # Also check POST data (Twilio sends form data)
    if request.method == "POST":
        try:
            post_data = await request.post()
            if not call_sid:
                call_sid = post_data.get("CallSid", "")
            # Query params take priority, but fall back to POST data
            if not recipient:
                recipient = post_data.get("recipient", "")
            if not purpose:
                purpose = post_data.get("purpose", "")
        except:
            pass

    # URL decode the parameters (in case they're encoded)
    from urllib.parse import unquote

    recipient = unquote(recipient) if recipient else ""
    purpose = unquote(purpose) if purpose else ""

    # Store context if this is an outbound call with purpose
    if call_sid and (recipient or purpose):
        CALL_CONTEXTS[call_sid] = {"recipient": recipient, "purpose": purpose}
        print(f"📞 Outbound call context stored: {call_sid} -> {recipient}, {purpose}")

    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Matthew">Connecting now.</Say>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="caller" value="{{{{From}}}}"/>
        </Stream>
    </Connect>
    <Say voice="Polly.Matthew">The call has ended. Goodbye!</Say>
</Response>'''

    print(f"📞 TwiML requested, WebSocket URL: {ws_url}")
    return web.Response(text=twiml, content_type="application/xml")


async def handle_call(request):
    """Initiate an outbound call via Twilio REST API.

    POST /call
    Body (JSON):
      - to: Phone number to call (E.164 format, e.g. +12125551234)
      - recipient: Name of person being called (for context)
      - purpose: Why Robothor is calling (used in system prompt)

    Returns JSON with call_sid on success.
    """
    try:
        data = await request.json()
        to_number = data.get("to", "").strip()
        recipient = data.get("recipient", "someone")
        purpose = data.get("purpose", "a general call from Philip")

        if not to_number:
            return web.json_response({"error": "Missing 'to' phone number"}, status=400)

        # Build TwiML URL with outbound context params
        twiml_url = (
            f"https://voice.robothor.ai/twiml?recipient={quote(recipient)}&purpose={quote(purpose)}"
        )

        client = get_twilio_client()
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE,
            url=twiml_url,
            status_callback="https://voice.robothor.ai/call-status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )

        print(f"📞 Outbound call initiated: {call.sid} → {to_number} ({recipient})")
        print(f"   Purpose: {purpose}")

        return web.json_response(
            {
                "status": "initiated",
                "call_sid": call.sid,
                "to": to_number,
                "from": TWILIO_PHONE,
                "recipient": recipient,
                "purpose": purpose,
            }
        )

    except RuntimeError as e:
        return web.json_response({"error": str(e)}, status=500)
    except Exception as e:
        print(f"❌ Error initiating call: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_call_status(request):
    """Receive call status callbacks from Twilio."""
    try:
        data = await request.post()
        call_sid = data.get("CallSid", "unknown")
        status = data.get("CallStatus", "unknown")
        duration = data.get("CallDuration", "0")
        print(f"📞 Call {call_sid}: {status} (duration: {duration}s)")
    except Exception as e:
        print(f"❌ Status callback error: {e}")
    return web.Response(text="OK")


async def handle_media_stream(request):
    """Handle WebSocket connection from Twilio Media Streams"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    bridge = None

    print("🎙️ New Media Stream connection")

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                event = data.get("event")

                if event == "connected":
                    print(f"📱 Twilio connected: protocol={data.get('protocol')}")

                elif event == "start":
                    start_data = data.get("start", {})
                    call_sid = start_data.get("callSid", "unknown")
                    caller = start_data.get("from", "unknown")
                    stream_sid = data.get("streamSid", "unknown")

                    bridge = VoiceCallBridge(ws, call_sid, caller, stream_sid)
                    await bridge.start()
                    break

            elif msg.type == aiohttp.WSMsgType.ERROR:
                print(f"❌ WebSocket error: {ws.exception()}")
                break

    except Exception as e:
        print(f"❌ Media stream error: {e}")
    finally:
        if bridge:
            print(f"👋 Call ended: {bridge.call_sid}")

    return ws


async def handle_health(request):
    """Health check endpoint"""
    return web.Response(text="Robothor Voice Server v2 (Gemini Live)\n")


async def main(port: int):
    """Start the HTTP + WebSocket server"""
    app = web.Application()

    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/twiml", handle_twiml)
    app.router.add_get("/twiml", handle_twiml)
    app.router.add_post("/call", handle_call)
    app.router.add_post("/call-status", handle_call_status)
    app.router.add_get("/media-stream", handle_media_stream)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print("🎙️  Robothor Voice Server v2 (Gemini Live Native Audio)")
    print(f"📍 TwiML URL: http://localhost:{port}/twiml")
    print(f"📍 WebSocket: ws://localhost:{port}/media-stream")
    print(f"🧠 Model: {MODEL_NAME}")
    print("🎤 Voice: Charon (deep male)")
    print("-" * 50)

    await asyncio.Event().wait()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Robothor Voice Server")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    args = parser.parse_args()

    asyncio.run(main(args.port))
