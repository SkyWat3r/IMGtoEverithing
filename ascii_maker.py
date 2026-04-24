#!/usr/bin/env python3

from imgemoji_app.ascii_art import parse_ascii_args, run_ascii_with_args


def main() -> None:
    args = parse_ascii_args()
    run_ascii_with_args(args)


if __name__ == "__main__":
    main()
