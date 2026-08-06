"""CLI tool to create the first admin account when bootstrap is skipped.

Usage:
    python -m app.create_admin

Prompts for username and password, creates the admin account and empty settings row.
"""

import argparse
import getpass
import sys

from . import db
from .auth import hash_password


def main():
    parser = argparse.ArgumentParser(description="Create first admin account")
    parser.add_argument("--username", help="Admin username (prompt if not provided)")
    parser.add_argument("--password", help="Admin password (prompt if not provided, not recommended)")
    args = parser.parse_args()

    db.run_migrations()

    # Check if any users exist
    conn = db.get_connection()
    cursor = conn.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]
    conn.close()

    if user_count > 0:
        print("❌ Users already exist in the database. Use /api/admin/users to create additional accounts.", file=sys.stderr)
        sys.exit(1)

    # Get username
    if args.username:
        username = args.username
    else:
        username = input("Admin username [admin]: ").strip() or "admin"

    # Check username uniqueness
    if db.get_user_by_username(username):
        print(f"❌ Username '{username}' already taken.", file=sys.stderr)
        sys.exit(1)

    # Get password
    if args.password:
        password = args.password
    else:
        while True:
            password = getpass.getpass("Admin password: ")
            if not password:
                print("❌ Password cannot be empty.", file=sys.stderr)
                continue
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("❌ Passwords do not match.", file=sys.stderr)
                continue
            break

    # Create user
    try:
        user_id = db.create_user(username, hash_password(password), is_admin=True)
        print(f"✓ Admin account created: {username} (id={user_id})")
        print("You can now log in at /login.html")
    except Exception as e:
        print(f"❌ Failed to create admin: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
