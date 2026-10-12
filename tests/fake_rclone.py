#!/usr/bin/env python3
"""Ersatz für rclone in den Tests: spielt Anmeldung mit 2FA und eine Kopie nach."""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
conf = Path(args[args.index("--config") + 1])
Path(os.environ["FAKE_RCLONE_CALLS"]).open("a").write(json.dumps(args) + "\n")

if "create" in args:
    if "password=falsch" in args:
        print("2026/10/12 NOTICE: Config file not found\nError: failed to create config: Apple-ID oder Passwort falsch", file=sys.stderr)
        sys.exit(1)
    conf.write_text("[icloud]\ntype = iclouddrive\n")
    print(json.dumps({"State": "2fa_do", "Option": {"Name": "config_2fa", "Help": "Enter code"}, "Error": ""}))
elif "update" in args:
    result = args[args.index("--result") + 1]
    if result == "000000":
        print(json.dumps({"State": "authenticate", "Option": None, "Error": "Code falsch"}))
    else:
        print(json.dumps({"State": "", "Option": None, "Error": ""}))
elif "copy" in args:
    dest = Path(args[args.index("copy") + 2])
    if os.environ.get("FAKE_RCLONE_FAIL"):
        print(json.dumps({"level": "error", "msg": "trust token abgelaufen"}))
        sys.exit(7)
    (dest / "datei.txt").write_text("x")
    print(json.dumps({"level": "notice", "msg": "Transferred", "stats": {"bytes": 1, "totalBytes": 1, "transfers": 1, "totalTransfers": 1, "errors": 0}}))
    print("keine JSON-Zeile")
