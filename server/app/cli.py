"""Admin commands. On the VPS run them inside the app container, e.g.
    docker compose exec app python -m app.cli create-staff jdoe "Jane Doe"

Commands:
    sync [path] [--sandbox-unpaid]     load the assessments export into the database
                                       (--sandbox-unpaid: sandbox testing only, leaves out the
                                        2025 collections so there are balances to pay)
    create-staff <username> <full name>   add an officer (prompts for a password)
    set-password <username>            reset an officer's password
    deactivate-staff <username>        block an officer and end their sessions
    list-staff                         list officers and last sign-in
    recon <YYYY-MM-DD>                 reconcile one day with Monnify now
"""
import getpass
import sys
from datetime import date

from . import config, db, staff, sync as sync_mod


def _password() -> str:
    while True:
        p1 = getpass.getpass("New password (at least 12 characters): ")
        if len(p1) < 12:
            print("Too short.")
            continue
        if p1 != getpass.getpass("Repeat password: "):
            print("The two passwords differ.")
            continue
        return p1


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    s = config.load()
    db.init(s.database_url, max_size=2)
    db.apply_schema()
    cmd, args = argv[0], argv[1:]
    if cmd == "sync":
        unpaid = "--sandbox-unpaid" in args
        paths = [a for a in args if not a.startswith("--")]
        if unpaid and s.monnify_env == "live":
            print("--sandbox-unpaid is refused in live mode.", file=sys.stderr)
            return 2
        try:
            print(sync_mod.sync(paths[0] if paths else s.data_file, zero_collections=unpaid))
        except sync_mod.SyncError as e:
            print(e, file=sys.stderr)
            return 2
    elif cmd == "create-staff" and len(args) >= 2:
        username, full_name = args[0].strip().lower(), " ".join(args[1:]).strip()
        pw = staff.hash_password(_password())
        with db.tx() as conn:
            conn.execute("INSERT INTO staff_users (username, full_name, password_hash) VALUES (%s, %s, %s)",
                         (username, full_name, pw))
        print(f"Created {username} ({full_name}).")
    elif cmd == "set-password" and len(args) == 1:
        pw = staff.hash_password(_password())
        with db.tx() as conn:
            n = conn.execute("UPDATE staff_users SET password_hash = %s WHERE username = %s",
                             (pw, args[0].lower())).rowcount
            conn.execute("DELETE FROM staff_sessions WHERE user_id = (SELECT id FROM staff_users WHERE username = %s)",
                         (args[0].lower(),))
        print("Password updated." if n else "No such user.")
    elif cmd == "deactivate-staff" and len(args) == 1:
        with db.tx() as conn:
            n = conn.execute("UPDATE staff_users SET active = false WHERE username = %s", (args[0].lower(),)).rowcount
            conn.execute("DELETE FROM staff_sessions WHERE user_id = (SELECT id FROM staff_users WHERE username = %s)",
                         (args[0].lower(),))
        print("Deactivated." if n else "No such user.")
    elif cmd == "list-staff":
        with db.tx() as conn:
            for u in conn.execute("SELECT username, full_name, active, last_login_at FROM staff_users ORDER BY username"):
                print(f"{u['username']:<20} {u['full_name']:<30} {'active' if u['active'] else 'INACTIVE':<9} "
                      f"last sign-in: {u['last_login_at'] or 'never'}")
    elif cmd == "recon" and len(args) == 1:
        from .monnify import Monnify
        from .worker import reconcile
        client = Monnify(s.monnify_base_url, s.monnify_api_key, s.monnify_secret_key, s.monnify_contract_code)
        print(reconcile(client, date.fromisoformat(args[0])))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
