"""V4 default entry; IGNGBOT_RUNTIME=v3 is the explicit rollback path."""
import os


def main():
    runtime = os.getenv("IGNGBOT_RUNTIME", "v4").lower()
    if runtime == "v4":
        from igngbot_v4.main import main as run
    elif runtime == "v3":
        from igngbot_v3.main import main as run
    else:
        raise ValueError("IGNGBOT_RUNTIME must be v4 or v3")
    run()


if __name__ == "__main__":
    main()
