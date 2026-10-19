"""Command-line entrypoint for Smokestack."""

import uvicorn


def main() -> None:
    """Run the development server."""
    uvicorn.run("pitboss_bridge.api:create_app", factory=True, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
