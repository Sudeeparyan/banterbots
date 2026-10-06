"""Wait for the actual coordinator and both A2A services before a CI/demo run."""
import time

import httpx


def main():
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            response = httpx.get("http://127.0.0.1:8000/api/health", timeout=3, trust_env=False)
            if response.status_code == 200 and response.json().get("status") == "ok":
                print("Coordinator and both A2A agents are ready.")
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.5)
    raise SystemExit("The broadcast services did not become healthy within 45 seconds.")


if __name__ == "__main__":
    main()
