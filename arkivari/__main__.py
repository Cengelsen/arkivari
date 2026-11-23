"""Entry point for the Postgres-backed archiver daemon."""

from arkivari.daemon import main

if __name__ == "__main__":
    raise SystemExit(main())