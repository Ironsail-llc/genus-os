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
import struct
from pathlib import Path

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

# Gemini Live API endpoint
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


def mulaw_to_pcm16(mulaw_data: bytes) -> bytes:
    """Convert mu-law 8kHz to PCM 16-bit"""
    # mu-law decompression table
    MULAW_DECODE = []
    for i in range(256):
        i = ~i
        sign = i & 0x80
        exponent = (i >> 4) & 0x07
        mantissa = i & 0x0F
        sample = (mantissa << 3) + 0x84
        sample <<= exponent
        sample -= 0x84
        if sign:
            sample = -sample
        MULAW_DECODE.append(sample)

    pcm_samples = []
    for byte in mulaw_data:
        pcm_samples.append(MULAW_DECODE[byte])

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
    mulaw_bytes = bytes([encode_sample(s) for s in samples])
    return mulaw_bytes


def resample_8k_to_16k(audio_8k: bytes) -> bytes:
    """Resample 8kHz PCM to 16kHz PCM (simple linear interpolation)"""
    samples_8k = struct.unpack(f"<{len(audio_8k) // 2}h", audio_8k)
    samples_16k = []

    for i in range(len(samples_8k) - 1):
        samples_16k.append(samples_8k[i])
        # Interpolate
        mid = (samples_8k[i] + samples_8k[i + 1]) // 2
        samples_16k.append(mid)

    if samples_8k:
        samples_16k.append(samples_8k[-1])
        samples_16k.append(samples_8k[-1])

    return struct.pack(f"<{len(samples_16k)}h", *samples_16k)


def resample_16k_to_8k(audio_16k: bytes) -> bytes:
    """Resample 16kHz PCM to 8kHz PCM (decimate)"""
    samples_16k = struct.unpack(f"<{len(audio_16k) // 2}h", audio_16k)
    samples_8k = samples_16k[::2]  # Take every other sample
    return struct.pack(f"<{len(samples_8k)}h", *samples_8k)


class GeminiLiveSession:
    """Manages a Gemini Live API session"""

    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self.gemini_ws: websockets.WebSocketClientProtocol | None = None
        self.connected = False
        self.setup_complete = False

    async def connect(self):
        """Connect to Gemini Live API via Vertex AI"""
        try:
            # Get access token from gcloud
            import subprocess

            result = subprocess.run(
                ["gcloud", "auth", "print-access-token"], capture_output=True, text=True
            )
            access_token = result.stdout.strip()

            headers = {"Authorization": f"Bearer {access_token}"}
            self.gemini_ws = await websockets.connect(VERTEX_AI_URL, additional_headers=headers)
            self.connected = True
            print(f"[{self.call_sid}] Connected to Gemini Live API (Vertex AI)")

            # Send setup message
            setup_msg = {
                "setup": {
                    "model": f"projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models/{MODEL_NAME}",
                    "generation_config": {
                        "response_modalities": ["AUDIO"],
                        "speech_config": {
                            "voice_config": {
                                "prebuilt_voice_config": {
                                    "voice_name": "Charon"  # Deep male voice
                                }
                            }
                        },
                    },
                    "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                }
            }
            await self.gemini_ws.send(json.dumps(setup_msg))

            # Wait for setup response
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
            # Gemini expects base64 encoded PCM audio
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
            return

        try:
            async for message in self.gemini_ws:
                data = json.loads(message)

                # Check for audio response
                if "serverContent" in data:
                    content = data["serverContent"]
                    if "modelTurn" in content:
                        for part in content["modelTurn"].get("parts", []):
                            if "inlineData" in part:
                                inline = part["inlineData"]
                                if "audio/pcm" in inline.get("mimeType", ""):
                                    audio_b64 = inline["data"]
                                    audio_pcm = base64.b64decode(audio_b64)
                                    yield audio_pcm

                    # Check if turn is complete
                    if content.get("turnComplete"):
                        print(f"[{self.call_sid}] Gemini turn complete")

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

    def __init__(self, twilio_ws, call_sid: str, caller: str):
        self.twilio_ws = twilio_ws
        self.call_sid = call_sid
        self.caller = caller
        self.gemini = GeminiLiveSession(call_sid)
        self.stream_sid: str | None = None
        self.running = False
        print(f"[{call_sid}] New call from {caller}")

    async def start(self):
        """Start the bridge"""
        await self.gemini.connect()
        if not self.gemini.connected:
            print(f"[{self.call_sid}] Failed to connect to Gemini, call will fail")
            return

        self.running = True

        # Start tasks for bidirectional audio
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

        try:
            async for message in self.twilio_ws:
                data = json.loads(message)
                event = data.get("event")

                if event == "start":
                    self.stream_sid = data.get("streamSid")
                    print(f"[{self.call_sid}] Media stream started: {self.stream_sid}")

                elif event == "media":
                    # Twilio sends mulaw 8kHz audio
                    payload = data.get("media", {}).get("payload", "")
                    if payload:
                        mulaw_audio = base64.b64decode(payload)

                        # Convert mulaw 8kHz -> PCM 16kHz
                        pcm_8k = mulaw_to_pcm16(mulaw_audio)
                        pcm_16k = resample_8k_to_16k(pcm_8k)

                        # Buffer and send in chunks
                        audio_buffer += pcm_16k

                        # Send every 100ms worth of audio (3200 bytes at 16kHz mono 16-bit)
                        while len(audio_buffer) >= 3200:
                            chunk = audio_buffer[:3200]
                            audio_buffer = audio_buffer[3200:]
                            await self.gemini.send_audio(chunk)

                elif event == "stop":
                    print(f"[{self.call_sid}] Media stream stopped")
                    self.running = False
                    break

        except websockets.exceptions.ConnectionClosed:
            print(f"[{self.call_sid}] Twilio connection closed")
            self.running = False

    async def handle_gemini_audio(self):
        """Receive audio from Gemini, send to Twilio"""
        try:
            async for pcm_16k in self.gemini.receive_audio():
                if not self.running or not self.stream_sid:
                    break

                # Convert PCM 16kHz -> mulaw 8kHz
                pcm_8k = resample_16k_to_8k(pcm_16k)
                mulaw_audio = pcm16_to_mulaw(pcm_8k)

                # Send to Twilio
                media_msg = {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(mulaw_audio).decode("utf-8")},
                }
                await self.twilio_ws.send(json.dumps(media_msg))

        except Exception as e:
            print(f"[{self.call_sid}] Error sending audio to Twilio: {e}")


async def handle_connection(websocket):
    """Handle WebSocket connection from Twilio"""
    bridge = None

    try:
        # First message should be connection info
        async for message in websocket:
            data = json.loads(message)
            event = data.get("event")

            if event == "connected":
                print("Twilio connected")

            elif event == "start":
                # Extract call info
                start_data = data.get("start", {})
                call_sid = start_data.get("callSid", "unknown")
                caller = start_data.get("from", "unknown")

                # Create and start bridge
                bridge = VoiceCallBridge(websocket, call_sid, caller)
                await bridge.start()
                break

    except websockets.exceptions.ConnectionClosed:
        if bridge:
            print(f"[{bridge.call_sid}] Call ended")
    except Exception as e:
        print(f"Connection error: {e}")


async def process_request(connection, request):
    """Handle HTTP requests (health checks and TwiML)"""
    upgrade_header = request.headers.get("Upgrade", "")
    if upgrade_header.lower() != "websocket":
        # Check if this is a TwiML request
        if request.path == "/twiml":
            # Get the host for WebSocket URL
            host = request.headers.get(
                "X-Forwarded-Host", request.headers.get("Host", "voice.robothor.ai")
            )
            ws_url = f"wss://{host}/media-stream"

            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Matthew">Hello! This is Robothor. Connecting you now.</Say>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="caller" value="{{{{From}}}}"/>
        </Stream>
    </Connect>
    <Say voice="Polly.Matthew">The call has ended. Goodbye!</Say>
</Response>'''
            print(f"📞 TwiML requested from {request.path}, WebSocket URL: {ws_url}")
            return Response(
                200, "OK", Headers([("Content-Type", "application/xml")]), twiml.encode()
            )

        # Health check
        return Response(200, "OK", Headers(), b"Robothor Voice Server v2 (Gemini Live)\n")
    return None


async def main(port: int = 8765):
    """Start the server"""
    print("🎙️  Robothor Voice Server v2 (Gemini Live Native Audio)")
    print(f"📍 WebSocket URL: ws://localhost:{port}")
    print("🧠 Model: gemini-2.5-flash-preview-native-audio-dialog")
    print("🎤 Voice: Charon (deep male)")
    print("-" * 50)

    async with websockets.serve(
        handle_connection, "0.0.0.0", port, process_request=process_request
    ):
        await asyncio.Future()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    asyncio.run(main(args.port))
