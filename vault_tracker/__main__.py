"""Entry point: python -m vault_tracker"""

from vault_tracker.config import Config
from vault_tracker.logger import get_logger
from vault_tracker.service import VaultService


def main() -> None:
    cfg = Config()
    # Re-init logger with configured level
    get_logger(level=cfg.LOG_LEVEL)
    svc = VaultService(cfg)
    svc.run()


if __name__ == "__main__":
    main()
