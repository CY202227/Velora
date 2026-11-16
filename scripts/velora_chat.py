"""Chat to a running Velora server."""

import argparse
import json
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", help="Send one message instead of opening an interactive chat")
    parser.add_argument("--url", default="http://127.0.0.1:8030")
    parser.add_argument("--persona", help="Persona ID for a new session")
    parser.add_argument("--session", help="Resume an existing session")
    parser.add_argument("--tts", action="store_true", help="Enable Vertin synthesis for this session")
    args = parser.parse_args()
    if args.session and args.persona:
        parser.error("--persona applies to new sessions only")
    try:
        with httpx.Client(base_url=args.url, timeout=240) as client:
            session = args.session
            if not session:
                response = client.post("/api/sessions", json={"tts_enabled": args.tts, "persona_id": args.persona})
                response.raise_for_status()
                session = response.json()["id"]
            elif args.tts:
                client.patch(f"/api/sessions/{session}", json={"tts_enabled": True}).raise_for_status()
            print(f"Session: {session}", file=sys.stderr)
            while True:
                try:
                    text = args.text if args.text is not None else input("You> ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if args.text is None and text.strip() == "/exit":
                    break
                if not text.strip():
                    if args.text is not None:
                        parser.error("message must not be empty")
                    continue
                stream_failed = False
                terminal_received = False
                with client.stream(
                    "POST", "/api/chat/stream", json={"session_id": session, "text": text}
                ) as response:
                    response.raise_for_status()
                    event = ""
                    for line in response.iter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            payload = json.loads(line[5:])
                            if event == "turn_end":
                                terminal_received = True
                            if event == "message" and payload.get("role") == "assistant":
                                print(payload["content"])
                            elif event == "tts_audio":
                                print(f"Audio: {args.url.rstrip('/')}{payload['url']}", file=sys.stderr)
                            elif event == "turn_end" and payload.get("result", {}).get("status") not in (None, "completed"):
                                print(payload["result"]["message"], file=sys.stderr)
                                if args.text is not None:
                                    return 1
                            elif event == "error":
                                print(payload.get("message", "Turn failed"), file=sys.stderr)
                                stream_failed = True
                if not terminal_received:
                    print("Turn ended without a terminal result; completion is unknown.", file=sys.stderr)
                    stream_failed = True
                if args.text is not None:
                    return 1 if stream_failed else 0
    except httpx.HTTPError as exc:
        print(f"Velora request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
