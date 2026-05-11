"""Entry point: `python -m ytdlp_app` or the installed `ytdlp-app` script."""

from __future__ import annotations

from .gui import App
from .settings import Settings


def main() -> None:
    settings = Settings()
    app = App(settings)
    app.mainloop()


if __name__ == "__main__":
    main()
