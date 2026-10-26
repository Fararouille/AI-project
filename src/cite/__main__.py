"""Allow ``python -m cite`` as an equivalent to the ``cite`` console script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
