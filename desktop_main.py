import sys


def main() -> int:
    if "--mcp-server" in sys.argv[1:]:
        from social_ops_agent.mcp_server import main as mcp_main

        mcp_main()
        return 0

    from social_ops_agent.desktop import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
