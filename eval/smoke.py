"""Quick end-to-end conversation through the simulator API. Prints what the farmer would see.

Usage:  .venv\\Scripts\\python eval\\smoke.py [phone] "message 1" "message 2" ...
"""

import sys

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> None:
    phone, msgs = sys.argv[1], sys.argv[2:]
    with httpx.Client(timeout=180) as http:
        seen = max([m["id"] for m in http.get(f"{BASE}/api/sim/messages", params={"phone": phone}).json()] or [0])
        case_id = None
        for text in msgs:
            print(f"\n>>> FARMER: {text}")
            case_id = http.post(f"{BASE}/api/sim/send", data={"phone": phone, "text": text}).json()["case_id"]
            for m in http.get(f"{BASE}/api/sim/messages", params={"phone": phone, "after": seen}).json():
                seen = m["id"]
                if m["direction"] == "out":
                    print(f"<<< AGENT ({m['channel']}): {m['text']}")
        case = http.get(f"{BASE}/api/cases/{case_id}").json()["case"]
        print("\n--- stage:", case["stage"], "| intimation:", case["intimation"], "\n--- fields:", case["fields"])


if __name__ == "__main__":
    main()
