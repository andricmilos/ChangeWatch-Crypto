import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    # No workers option: prices and the exchange connection live in memory, so the app
    # must run as a single process (SPEC 16.4).
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.app_env == "development",
    )


if __name__ == "__main__":
    main()
