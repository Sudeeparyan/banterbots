"""Verify a running POC over its public REST and real agent HTTP interfaces."""
import argparse
import asyncio
import json
import sys

import httpx


async def verify(base, provider="demo"):
    async with httpx.AsyncClient(base_url=base, timeout=15, trust_env=False) as client:
        health = (await client.get("/api/health")).json()
        assert health["status"] == "ok", f"Agents are unavailable: {health}"
        if provider == "openai":
            config = (await client.get("/api/config")).json()
            if not config["openai_configured"]:
                raise RuntimeError("The running services have no OpenAI key. Add OPENAI_API_KEY to .env "
                                   "and restart all three Python services.")
        response = await client.post("/api/sessions", json={"voice_enabled": False, "provider": provider})
        response.raise_for_status()
        session = response.json()
        for step in range(2):
            response = await client.post(f"/api/sessions/{session['id']}/control", json={"action": "step"})
            response.raise_for_status()
            deadline = asyncio.get_running_loop().time() + (120 if provider == "openai" else 15)
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
                response = await client.get(f"/api/sessions/{session['id']}")
                response.raise_for_status()
                session = response.json()
                if session["status"] in {"paused", "error", "stopped", "completed"}:
                    break
            if session["status"] != "paused":
                errors = [event["data"].get("message", "") for event in session["events"]
                          if event["type"] == "error"]
                if session["status"] not in {"error", "stopped", "completed"}:
                    await client.post(f"/api/sessions/{session['id']}/control", json={"action": "stop"})
                reason = f"Commentary ended with status {session['status']} before both turns were received"
                if session["status"] not in {"error", "stopped", "completed"}:
                    reason = "Commentary did not finish within the smoke timeout"
                raise RuntimeError(errors[-1] if errors else reason)
            pair = session["turns"][-2:]
            assert len(session["turns"]) == (step + 1) * 2
            assert [turn["agent_id"] for turn in pair] == (["a", "b"] if step == 0 else ["b", "a"])
            assert pair[0]["snapshot_hash"] == pair[1]["snapshot_hash"]
            assert pair[1]["reply_to_turn_id"] == pair[0]["id"]
            assert pair[0]["task_id"] != pair[1]["task_id"]
        traces = [event["data"] for event in session["events"] if event["type"] == "trace"]
        assert any(trace.get("node") == "peer_exchange" and trace.get("matching_snapshot") for trace in traces)
        print(json.dumps({"result": "pass", "provider": provider, "session_id": session["id"], "turns": len(session["turns"]),
                          "lead_order": [session["turns"][0]["agent_id"], session["turns"][2]["agent_id"]],
                          "protocol": "A2A 1.0", "metrics": session["metrics"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--provider", choices=["demo", "openai"], default="demo",
                        help="OpenAI verifies two real text exchanges using the server's key (billed API calls)")
    args = parser.parse_args()
    try:
        asyncio.run(verify(args.base_url, args.provider))
    except (RuntimeError, AssertionError, httpx.HTTPError) as exc:
        print(json.dumps({"result": "fail", "provider": args.provider, "error": str(exc)}))
        sys.exit(1)
