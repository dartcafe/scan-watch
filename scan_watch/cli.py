"""Command line front end: settings menu, arguments, terminal output."""

import argparse
import getpass
import os
import signal
import sys
import threading

from .core import Watcher, connect_paperless
from . import i18n
from .i18n import _
from .paperless import format_fingerprint
from .web import EventHub, WebUI
from .settings import (COLOR_MODES, ENV_PREFIX, PAPER_SIZES, RESOLUTIONS, SPLIT_MODES, TARGETS, TOKEN_ENV,
                       env_overrides, load_settings, parse_value, save_settings, uses_paperless)


WEB_ENV = ENV_PREFIX + "WEB"
WEB_PASSWORD_ENV = ENV_PREFIX + "WEB_PASSWORD"
DUPLEX = {True: _("Duplex (both sides)"), False: _("Simplex (one side)")}
YES_NO = {True: _("yes"), False: _("no")}


def print_event(event, message, **data):
    if message:
        print(("  " if event == "page" else "") + str(message), flush=True)


def ask(prompt):
    return input(str(prompt))


def yes(answer):
    """y/yes plus the translated words, e.g. j/ja."""
    words = {"y", "yes"} | {w.strip() for w in str(_("y, yes")).lower().split(",")}
    return answer.strip().lower() in words


def describe(s, from_env=()):
    """Menu rows as (setting key, label, value)."""
    missing = _("– missing –")
    if os.environ.get(TOKEN_ENV):
        token = _("from ${name}", name=TOKEN_ENV)
    else:
        token = _("set") if s["paperless_token"] else missing
    url = s["paperless_url"] or missing
    if s["paperless_tls_pin"]:
        url = _("{url} (certificate pinned)", url=url)
    rows = [
        ("color", _("Color mode"), COLOR_MODES[s["color"]][0]),
        ("dpi", _("Resolution"), f'{s["dpi"]} dpi'),
        ("duplex", _("Sides"), DUPLEX[s["duplex"]]),
        ("paper", _("Paper size"), PAPER_SIZES[s["paper"]][0]),
        ("skip_blank", _("Remove blank pages"), YES_NO[s["skip_blank"]]),
        ("blank_threshold", _("Blank page threshold"), _("{value} % (densest tile)", value=s["blank_threshold"])),
        ("split", _("Split"), SPLIT_MODES[s["split"]]),
        ("target", _("Target"), TARGETS[s["target"]]),
        ("out", _("Output folder"), s["out"]),
        ("paperless_url", _("Paperless URL"), url),
        ("paperless_token", _("Paperless token"), token),
        ("paperless_tags", _("Paperless tags"), s["paperless_tags"] or "–"),
        ("prefix", _("File name prefix"), s["prefix"]),
        ("host", _("Scanner"), s["host"] or missing),
        ("auto_pause", _("Auto pause"), _("after {n} min without paper", n=s["auto_pause"]) if s["auto_pause"]
         else _("off")),
        ("snmp_community", _("Wake-up detection"), _("SNMP community “{community}”", community=s["snmp_community"])
         if s["snmp_community"] else _("off")),
    ]
    # A value set in the environment wins again at the next start.
    return [(key, str(name), f"{value}  [${ENV_PREFIX}{key.upper()}]" if key in from_env else str(value))
            for key, name, value in rows]


def choose(title, options, current):
    """Let the user pick one of options = [(value, label), ...]."""
    print(f"\n{title}:")
    for i, (value, label) in enumerate(options, 1):
        mark = "*" if value == current else " "
        print(f"  {i}) {label} {mark}")
    answer = ask(_("Choice [Enter = unchanged]: ")).strip()
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1][0]
    return current


def menu(s, from_env=()):
    while True:
        print("\n=== " + str(_("Scan settings")) + " ===")
        for i, (_key, name, value) in enumerate(describe(s, from_env), 1):
            print(f"  {i:>2}) {name:<24} {value}")
        answer = ask(_("\nNumber to change, Enter = start, q = quit: ")).strip().lower()
        if answer == "":
            return s
        if answer == "q":
            sys.exit(0)
        if answer == "1":
            s["color"] = choose(_("Color mode"), [(k, v[0]) for k, v in COLOR_MODES.items()], s["color"])
        elif answer == "2":
            s["dpi"] = choose(_("Resolution"), [(r, f"{r} dpi") for r in RESOLUTIONS], s["dpi"])
        elif answer == "3":
            s["duplex"] = choose(_("Sides"), list(DUPLEX.items()), s["duplex"])
        elif answer == "4":
            s["paper"] = choose(_("Paper size"), [(k, v[0]) for k, v in PAPER_SIZES.items()], s["paper"])
        elif answer == "5":
            s["skip_blank"] = choose(_("Remove blank pages"), list(YES_NO.items()), s["skip_blank"])
        elif answer == "6":
            value = ask(_("Threshold in % content [{value}]: ", value=s["blank_threshold"])).strip().replace(",", ".")
            try:
                s["blank_threshold"] = float(value) if value else s["blank_threshold"]
            except ValueError:
                print(_("Invalid number."))
        elif answer == "7":
            s["split"] = choose(_("Split"), list(SPLIT_MODES.items()), s["split"])
        elif answer == "8":
            s["target"] = choose(_("Target"), list(TARGETS.items()), s["target"])
        elif answer == "9":
            value = ask(_("Output folder [{value}]: ", value=s["out"])).strip()
            s["out"] = os.path.expanduser(value) if value else s["out"]
        elif answer == "10":
            value = ask(_("Paperless URL, e.g. https://paperless.lan [{value}]: ", value=s["paperless_url"])).strip()
            if value:
                # Re-entering the URL also re-checks the certificate, e.g. after it was renewed.
                s["paperless_url"] = value
                s["paperless_tls_pin"] = ""
        elif answer == "11":
            print(_("API token from Paperless: profile (top right) → API Auth Token."))
            value = getpass.getpass(str(_("Token (input hidden, Enter = unchanged): "))).strip()
            s["paperless_token"] = value or s["paperless_token"]
        elif answer == "12":
            value = ask(_("Tags, comma separated, “-” = none [{value}]: ", value=s["paperless_tags"])).strip()
            if value:
                s["paperless_tags"] = "" if value == "-" else value
        elif answer in ("13", "14", "15", "16"):
            key, label = {"13": ("prefix", _("Prefix")), "14": ("host", _("Scanner address")),
                          "15": ("auto_pause", _("Auto pause after minutes without paper (0 = off)")),
                          "16": ("snmp_community", _("SNMP community for the wake-up detection, Canon DR-S150 "
                                                     "only (“-” = off)"))}[answer]
            value = ask(f"{label} [{s[key]}]: ").strip()
            try:
                if key == "snmp_community" and value == "-":
                    s[key] = ""
                else:
                    s[key] = parse_value(key, value) if value else s[key]
            except ValueError as e:
                print(_("Invalid: {error}", error=e.args[0]))
        if answer in ("8", "10", "11", "12") and uses_paperless(s):
            connect_paperless(s, print_event, trust_certificate)


def trust_certificate(s, fp):
    """Ask whether to pin a self-signed certificate, like SSH does for host keys."""
    print()
    print(_("The server uses a certificate that cannot be verified automatically\n"
            "(e.g. the self-signed default certificate of a Synology)."))
    print(_("SHA-256 fingerprint:"))
    print("  " + format_fingerprint(fp))
    print(_("Compare it in the browser via the lock icon → certificate."))
    if not yes(ask(_("Trust this certificate from now on? [y/N]: "))):
        return False
    s["paperless_tls_pin"] = fp
    report_save_error(save_settings(s))
    return True


def report_save_error(error):
    if error:
        print(_("Note: {message}", message=error))


def arg_value(key):
    """argparse type that validates like the menu and the environment do."""
    def convert(raw):
        try:
            return parse_value(key, raw)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e.args[0])) from None
    return convert


def parse_args():
    p = argparse.ArgumentParser(
        description=str(_("Scan automatically as soon as paper is in the feeder.")),
        epilog=str(_("Every setting can also be given as {prefix}<NAME>, e.g. {prefix}HOST or {token}.",
                     prefix=ENV_PREFIX, token=TOKEN_ENV)))
    p.add_argument("--host", type=arg_value("host"), help=str(_("IP/host name of the scanner")))
    p.add_argument("--target", choices=TARGETS, help=str(_("folder, Paperless or both")))
    p.add_argument("--out", help=str(_("output folder for the PDFs")))
    p.add_argument("--paperless-url", help=str(_("e.g. https://paperless.lan (token via ${name} or the menu)",
                                                 name=TOKEN_ENV)))
    p.add_argument("--paperless-tags", help=str(_("comma separated tag names")))
    p.add_argument("--color", choices=COLOR_MODES)
    p.add_argument("--dpi", type=int, choices=RESOLUTIONS)
    p.add_argument("--duplex", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--paper", choices=PAPER_SIZES)
    p.add_argument("--skip-blank", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--blank-threshold", type=float,
                   help=str(_("pages whose densest tile has less % content count as blank")).replace("%", "%%"))
    p.add_argument("--split", choices=SPLIT_MODES, help=str(_("one PDF per stack or per sheet")))
    p.add_argument("--prefix", type=arg_value("prefix"), help=str(_("prefix of the file name")))
    p.add_argument("--auto-pause", type=arg_value("auto_pause"), metavar=str(_("MINUTES")),
                   help=str(_("pause after this many minutes without paper, so the scanner can go to standby "
                              "(0 = never, default 30)")))
    p.add_argument("-y", "--no-menu", action="store_true",
                   help=str(_("skip the menu and start right away (automatic without a terminal)")))
    p.add_argument("--web", metavar=str(_("[ADDRESS:]PORT")),
                   help=str(_("start the web interface, e.g. 8080 or 127.0.0.1:8080 (also ${web}); "
                              "password via ${password}", web=WEB_ENV, password=WEB_PASSWORD_ENV)))
    return p.parse_args()


def main():
    # Someone at a terminal gets the menu in their language; anything that may be
    # logged (systemd, Docker, redirected output) stays English.
    if sys.stdin.isatty() and sys.stdout.isatty():
        i18n.use_locale()
    args = parse_args()
    s = load_settings()
    try:
        from_env = env_overrides()
    except ValueError as e:
        sys.exit(str(_("Error: {error}", error=e.args[0])))
    if from_env.get("paperless_url", s["paperless_url"]) != s["paperless_url"]:
        s["paperless_tls_pin"] = ""  # a pinned certificate belongs to the old server
    s.update(from_env)
    fixed = set(from_env)  # settings the web page must not change
    if args.paperless_url and args.paperless_url != s["paperless_url"]:
        s["paperless_tls_pin"] = ""
    for key, value in vars(args).items():
        if key in s and value is not None:
            s[key] = value
            fixed.add(key)

    interactive = sys.stdin.isatty()
    if interactive and not args.no_menu:
        s = menu(s, from_env)
    web = args.web or os.environ.get(WEB_ENV)
    # With the web interface the address can still be entered there.
    if not s["host"] and not web:
        sys.exit(str(_("Error: scanner address missing (menu, --host or {name}).", name=ENV_PREFIX + "HOST")))
    save_error = save_settings(s)
    report_save_error(save_error)

    hub = EventHub()

    def on_event(event, message, **data):
        print_event(event, message, **data)
        hub.add(event, message, **data)
        if event == "paper" and ui:
            ui.check_settings_file()

    watcher = Watcher(s, on_event, trust_certificate if interactive else None)
    ui = None
    if web:
        password = os.environ.get(WEB_PASSWORD_ENV)
        try:
            ui = WebUI(web, s, fixed, hub, watcher, password, save_error, emit=on_event)
            port = ui.start()
        except (OSError, ValueError) as e:
            sys.exit(str(_("Error: cannot start the web interface on “{address}” ({error})", address=web, error=e)))
        print(_("Web interface on port {port}", port=port) if password else
              _("Web interface on port {port} – without password, everyone in the network can change the "
                "settings ({name})", port=port, name=WEB_PASSWORD_ENV))
    # docker stop sends SIGTERM: finish the current stack, then exit.
    signal.signal(signal.SIGTERM, lambda *_: watcher.stop())
    # SIGUSR1 toggles the pause, e.g. `docker kill -s USR1 scan-watch`. Not in the
    # handler itself: printing there could interrupt a print of the main thread.
    signal.signal(signal.SIGUSR1, lambda *_: threading.Thread(
        target=lambda: watcher.pause(not watcher.paused), daemon=True).start())
    if interactive:
        print(_("Quit with Ctrl+C."))
        print()
    try:
        watcher.run()
    except KeyboardInterrupt:
        pass
    if ui:
        ui.stop()
    print()
    print(_("Stopped."))
