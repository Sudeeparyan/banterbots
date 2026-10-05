"""Verify a running POC over its public REST and real agent HTTP interfaces."""
import argparse
import asyncio
import json

import httpx


async def verify(base):
    async with httpx.AsyncClient(base_url=base, timeout=15, trust_env=False) as client:
        health = (await client.get("/api/health")).json()
        assert health["status"] == "ok", f"Agents are unavailable: {health}"
        response = await client.post("/api/sessions", json={"voice_enabled": False, "provider": "demo"})
        response.raise_for_status()
        session = response.json()
        for step in range(2):
            response = await client.post(f"/api/sessions/{session['id']}/control", json={"action": "step"})
            response.raise_for_status()
            for _ in range(150):
                await asyncio.sleep(0.1)
                session = (await client.get(f"/api/sessions/{session['id']}")).json()
                if session["status"] in {"paused", "error"}:
                    break
            assert session["status"] == "paused", session
            pair = session["turns"][-2:]
            assert len(session["turns"]) == (step + 1) * 2
            assert [turn["agent_id"] for turn in pair] == (["a", "b"] if step == 0 else ["b", "a"])
            assert pair[0]["snapshot_hash"] == pair[1]["snapshot_hash"]
            assert pair[1]["reply_to_turn_id"] == pair[0]["id"]
            assert pair[0]["task_id"] != pair[1]["task_id"]
        traces = [event["data"] for event in session["events"] if event["type"] == "trace"]
        assert any(trace.get("node") == "peer_exchange" and trace.get("matching_snapshot") for trace in traces)
        print(json.dumps({"result": "pass", "session_id": session["id"], "turns": len(session["turns"]),
                          "lead_order": [session["turns"][0]["agent_id"], session["turns"][2]["agent_id"]],
                          "protocol": "A2A 1.0", "metrics": session["metrics"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    asyncio.run(verify(parser.parse_args().base_url))
