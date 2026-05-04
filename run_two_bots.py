from __future__ import annotations

import threading

from run_bot import run_live


def main() -> None:
    threads = [
        threading.Thread(target=run_live, args=(1, True), daemon=False),
        threading.Thread(target=run_live, args=(2, False), daemon=False),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
