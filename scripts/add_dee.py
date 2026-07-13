"""One-off: add Dee Valladares to the roster as an undrafted houseguest.
Her points are tracked all season but don't count toward any team, and the
beef/feud engine ignores owner-less houseguests automatically.
Safe to re-run (skips if she already exists).
"""

import json
import urllib.request

FB = "https://bb28-fantasy-default-rtdb.firebaseio.com/gameData.json"

DEE = {
    "id": "hg_dee_valladares",
    "name": "Dee Valladares",
    "age": 29,
    "hometown": "Miami, FL",
    "occupation": "Entrepreneur",
    "bio": ("Winner of Survivor 45. Julie's time-travel twist pulled her into the "
            "BB28 house at the end of premiere night as the seventeenth houseguest, "
            "replacing Rachel Reilly. Undrafted — her points are tracked for the "
            "season but don't count toward any team."),
    "photo": "photos/dee.png",
    "status": "active",
    "events": [],
}


def main():
    with urllib.request.urlopen(FB) as r:
        data = json.load(r)

    if any(h.get("id") == DEE["id"] for h in data["houseguests"]):
        print("Dee already on roster - nothing to do.")
        return

    data["houseguests"].append(DEE)
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(FB, data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        r.read()
    print("Added Dee Valladares (undrafted) to Firebase.")


if __name__ == "__main__":
    main()
