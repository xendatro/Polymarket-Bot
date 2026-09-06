from pm.report import status_summary
from scripts.common import boot, out


def main() -> None:
    settings, cfg, _, conn = boot(readonly=True)
    out(status_summary(conn, settings.mode))


if __name__ == "__main__":
    main()
