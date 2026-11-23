"""Smoke test: prove the whole YouTube chain works.

usage: python -m app.tools.test_upload [file]
"""

import sys

from app.youtube import channel, upload

DEFAULT = "/app/work/test.mp4"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT

    print("1. minting access token from refresh token ...")
    c = channel()
    print("   channel :", c["snippet"]["title"])
    print("   id      :", c["id"])

    print("2. uploading", path, "as private ...")
    res = upload(
        path,
        title="YT Factory - pipeline test (private)",
        description=(
            "Automated private test upload from the YT Factory pipeline.\n\n"
            "This verifies OAuth scopes, channel binding and upload quota.\n"
            "Safe to delete."
        ),
        privacy="private",
        tags=["test", "pipeline"],
    )
    print("   video id:", res["id"])
    print("   url     : https://youtu.be/" + res["id"])
    print("   status  :", res.get("status", {}).get("uploadStatus"))
    print("   privacy :", res.get("status", {}).get("privacyStatus"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
