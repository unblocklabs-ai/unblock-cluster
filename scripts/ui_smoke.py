#!/usr/bin/env python3
"""Local UI smoke suite for the Data Graph frontend.

Setup:
  .venv/bin/python scripts/demo_seed.py
  DATAGRAPH_DATA_DIR=output/demo-data .venv/bin/python -m datagraph.main

Then, in another shell:
  python scripts/ui_smoke.py

Set DATAGRAPH_CHROME_BIN=/path/to/chrome-headless-shell to override browser
discovery. The suite assumes a running seeded server and uses no third-party
Python packages.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ui_probe import Browser

OUT = Path("output/ui-smoke")
READOUT_RE = re.compile(r"^\d[\d,]* records? · week of .+")


class SmokeFailure(AssertionError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local CDP UI smoke checks.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--cdp-port", type=int, default=9333)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    try:
        browser = Browser(port=args.cdp_port)
    except Exception as error:  # noqa: BLE001 - startup failure is the report.
        print(f"FAIL browser-startup: {error}")
        return 1
    failures: list[tuple[str, str]] = []
    try:
        scenarios: list[tuple[str, Callable[[], None]]] = [
            ("boot", lambda: scenario_boot(browser, args.base_url)),
            ("form-field-hygiene", lambda: scenario_form_hygiene(browser)),
            ("scrubber", lambda: scenario_scrubber(browser)),
            ("date-window-reactivity", lambda: scenario_date_window(browser)),
            ("console-hygiene", lambda: scenario_console(browser)),
        ]
        for name, fn in scenarios:
            try:
                fn()
                print(f"PASS {name}")
            except Exception as error:  # noqa: BLE001 - report all scenario failures.
                failures.append((name, str(error)))
                print(f"FAIL {name}: {error}")
                safe_shot(browser, OUT / f"{name}-failure.png")
                break
    finally:
        browser.close()

    if failures:
        print("\nUI smoke failed:")
        for name, message in failures:
            print(f"- {name}: {message}")
        return 1
    print(f"UI smoke passed {len(scenarios)} scenarios. Screenshots: {OUT}")
    return 0


def scenario_boot(browser: Browser, base_url: str) -> None:
    browser.goto(base_url.rstrip("/") + "/", settle=1.0)
    wait_js(browser, "document.querySelectorAll('.picker-row').length > 0", "graph picker rows")
    browser.shot(OUT / "01-boot-picker.png")

    click_selector(browser, ".picker-row")
    wait_js(
        browser,
        "document.querySelector('#workspace') && !document.querySelector('#workspace').hidden",
        "workspace visible",
    )
    wait_js(
        browser,
        "document.querySelectorAll('#topicList [data-topic-id]').length > 0",
        "topics listed",
    )
    wait_js(
        browser,
        "document.querySelectorAll('#topicList .sparkline-small').length > 0",
        "topic sparklines",
    )
    browser.shot(OUT / "02-workspace-polished-cards.png")


def scenario_form_hygiene(browser: Browser) -> None:
    missing = browser.js(
        """
        [...document.querySelectorAll('select,input,textarea')]
          .filter((el) => !el.id && !el.name)
          .map((el) => el.outerHTML)
        """
    )
    if missing:
        raise SmokeFailure("form controls missing id/name: " + " | ".join(missing))
    browser.shot(OUT / "03-form-hygiene.png")


def scenario_scrubber(browser: Browser) -> None:
    click_selector(browser, "#topicList [data-topic-id]")
    wait_js(
        browser,
        "!!document.querySelector('[data-inspector-sparkline]')",
        "inspector sparkline",
    )
    wait_js(
        browser,
        "document.querySelector('[data-sparkline-readout]')?.dataset.defaultReadout",
        "default trend caption",
    )

    rect = element_rect(browser, "[data-inspector-sparkline]")
    readouts: list[str] = []
    for fraction in (0.2, 0.5, 0.8):
        browser.mouse_move(
            rect["left"] + rect["width"] * fraction,
            rect["top"] + rect["height"] / 2,
        )
        time.sleep(0.15)
        text = readout(browser)
        if not READOUT_RE.match(text):
            raise SmokeFailure(f"scrub readout has unexpected shape: {text!r}")
        readouts.append(text)
    if len(set(readouts)) < 2:
        raise SmokeFailure(f"scrub readout did not change across pointer positions: {readouts}")
    visible = browser.js(
        """
        (() => {
          const el = document.querySelector('[data-sparkline-crosshair]');
          return !!el && !el.classList.contains('is-hidden')
            && getComputedStyle(el).display !== 'none';
        })()
        """
    )
    if not visible:
        raise SmokeFailure("sparkline crosshair did not become visible")
    browser.shot(OUT / "04-inspector-mid-scrub.png")

    browser.js("document.querySelector('[data-inspector-sparkline]').focus()")
    send_key(browser, "Home")
    home = readout(browser)
    send_key(browser, "ArrowRight")
    right = readout(browser)
    send_key(browser, "ArrowLeft")
    left = readout(browser)
    if right == home:
        raise SmokeFailure("ArrowRight did not move scrub readout")
    if left != home:
        raise SmokeFailure("ArrowLeft did not return scrub readout to prior bucket")

    send_key(browser, "Escape")
    assert_default_readout(browser, "Escape")
    browser.mouse_move(rect["left"] + rect["width"] * 0.5, rect["top"] + rect["height"] / 2)
    time.sleep(0.15)
    browser.mouse_move(rect["right"] + 24, rect["bottom"] + 24)
    time.sleep(0.15)
    assert_default_readout(browser, "pointer leave")


def scenario_date_window(browser: Browser) -> None:
    before = trend_state(browser)
    if not before["path"] or not before["caption"] or not before["card"]:
        raise SmokeFailure(f"missing initial trend state: {before}")
    browser.shot(OUT / "05-date-all-before.png")

    # Windowing keeps any bucket that intersects the range, so a preset only
    # changes the drawing when it actually excludes buckets. The demo's first
    # topic spans ~5 weekly buckets; 7d is guaranteed to cut it, wider presets
    # may legitimately be a no-op for that topic.
    click_selector(browser, '[data-date-preset="7d"]')
    wait_for(browser, lambda: trend_state(browser)["path"] != before["path"], "7d path change")
    after_7d = trend_state(browser)
    if after_7d["caption"] == before["caption"]:
        raise SmokeFailure("7d preset did not change inspector caption")
    if after_7d["card"] == before["card"]:
        raise SmokeFailure("7d preset did not re-render card sparklines")
    browser.shot(OUT / "06-date-7d.png")

    click_selector(browser, '[data-date-preset="all"]')
    wait_for(
        browser,
        lambda: trend_state(browser)["path"] == before["path"],
        "All path restoration",
    )
    restored = trend_state(browser)
    if restored["caption"] != before["caption"]:
        raise SmokeFailure("All preset did not restore inspector caption")
    if restored["card"] != before["card"]:
        raise SmokeFailure("All preset did not restore card sparkline markup")
    browser.shot(OUT / "07-date-all-restored.png")


def scenario_console(browser: Browser) -> None:
    errors = [
        entry
        for entry in browser.console
        if str(entry.get("type", "")).lower() in {"error", "severe"}
    ]
    if errors:
        lines = [f"{entry.get('type')}: {entry.get('text')}" for entry in errors]
        raise SmokeFailure("console errors:\n" + "\n".join(lines))


def wait_js(browser: Browser, expr: str, label: str, timeout: float = 10.0) -> Any:
    return wait_for(browser, lambda: browser.js(expr), label, timeout=timeout)


def wait_for(browser: Browser, fn: Callable[[], Any], label: str, timeout: float = 10.0) -> Any:
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.1)
    raise SmokeFailure(f"timed out waiting for {label}; last={last!r}")


def click_selector(browser: Browser, selector: str) -> None:
    rect = element_rect(browser, selector)
    browser.click(rect["left"] + rect["width"] / 2, rect["top"] + rect["height"] / 2)
    time.sleep(0.2)


def element_rect(browser: Browser, selector: str) -> dict[str, float]:
    rect = browser.js(
        f"""
        (() => {{
          const el = document.querySelector({selector!r});
          if (!el) return null;
          const rect = el.getBoundingClientRect();
          return {{
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
            width: rect.width,
            height: rect.height
          }};
        }})()
        """
    )
    if not rect or rect["width"] <= 0 or rect["height"] <= 0:
        raise SmokeFailure(f"could not locate visible element {selector}")
    return rect


def readout(browser: Browser) -> str:
    expr = "document.querySelector('[data-sparkline-readout]')?.textContent.trim()"
    return str(browser.js(expr) or "")


def assert_default_readout(browser: Browser, action: str) -> None:
    state = browser.js(
        """
        (() => {
          const readout = document.querySelector('[data-sparkline-readout]');
          return {
            text: readout?.textContent.trim() || '',
            expected: readout?.dataset.defaultReadout || ''
          };
        })()
        """
    )
    if state["text"] != state["expected"]:
        raise SmokeFailure(f"{action} did not restore default caption: {state}")


def trend_state(browser: Browser) -> dict[str, str]:
    return browser.js(
        """
        (() => {
          const line = document.querySelector('.sparkline-inspector .sparkline-line');
          const readout = document.querySelector('[data-sparkline-readout]');
          const card = document.querySelector('#topicList [data-topic-id] .sparkline-small');
          return {
            path: line?.getAttribute('d') || '',
            caption: readout?.textContent.trim() || '',
            card: card?.outerHTML || ''
          };
        })()
        """
    )


def send_key(browser: Browser, key: str) -> None:
    codes = {
        "ArrowRight": ("ArrowRight", 39),
        "ArrowLeft": ("ArrowLeft", 37),
        "Home": ("Home", 36),
        "End": ("End", 35),
        "Escape": ("Escape", 27),
    }
    code, key_code = codes[key]
    for event_type in ("keyDown", "keyUp"):
        browser.cmd(
            "Input.dispatchKeyEvent",
            type=event_type,
            key=key,
            code=code,
            windowsVirtualKeyCode=key_code,
            nativeVirtualKeyCode=key_code,
        )
        time.sleep(0.05)


def safe_shot(browser: Browser, path: Path) -> None:
    with contextlib.suppress(Exception):
        browser.shot(path)


if __name__ == "__main__":
    sys.exit(main())
