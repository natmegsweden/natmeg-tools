"""Local administrator recovery utilities."""

from __future__ import annotations

import argparse

from .config import app_paths
from .notebook import connect, reset_admin_password


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Lab Notebook local administrator recovery")
    parser.add_argument("command", choices=["reset-password"])
    parser.add_argument("--confirm-reset", action="store_true",
                        help="clear only admin credentials and require password setup on next launch")
    args = parser.parse_args()
    if args.command == "reset-password" and not args.confirm_reset:
        parser.error("reset-password requires --confirm-reset")
    paths = app_paths()
    db = connect(paths.database_path)
    try:
        reset_admin_password(db, confirmed=args.confirm_reset)
    finally:
        db.close()
    print("Admin credentials cleared. Existing settings and notebook data were retained.")


if __name__ == "__main__":
    main()
