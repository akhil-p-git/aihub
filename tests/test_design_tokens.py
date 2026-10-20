"""DESIGN.md documents the colours and the stylesheet uses them; this keeps the two from drifting apart."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
STATIC = ROOT / "app" / "aihub_web" / "static"
CSS = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
# Colours the stylesheet uses for shadows only, which the design document does not list.
CSS_ONLY = {"shadow-ink"}


def numbers(value: str) -> tuple[float, ...]:
    return tuple(float(n) for n in re.findall(r"-?\d*\.?\d+", value))


def documented() -> dict[str, str]:
    front = (ROOT / "DESIGN.md").read_text(encoding="utf-8").split("---")[1]
    return yaml.safe_load(front)["colors"]


def stylesheet() -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    pairs = {
        name: (light, dark)
        for name, light, dark in re.findall(
            r"--([\w-]+):\s*light-dark\((oklch\([^)]*\)),\s*(oklch\([^)]*\))\)", CSS
        )
    }
    singles = dict(re.findall(r"--([\w-]+):\s*(oklch\([^)]*\));", CSS))
    return pairs, singles


def test_every_documented_colour_is_the_one_the_stylesheet_uses() -> None:
    pairs, singles = stylesheet()
    for name, value in documented().items():
        night = name.endswith("-night")
        base = name.removesuffix("-night")
        if base in pairs:
            actual = pairs[base][1 if night else 0]
        elif name in singles:
            actual = singles[name]
        else:
            raise AssertionError(
                f"DESIGN.md names {name}, which the stylesheet does not define"
            )
        assert numbers(actual) == numbers(value), (
            f"{name}: stylesheet has {actual}, DESIGN.md has {value}"
        )


def test_every_stylesheet_colour_is_documented() -> None:
    pairs, singles = stylesheet()
    design = documented()
    missing = []
    for name in pairs.keys() - CSS_ONLY:
        missing += [n for n in (name, f"{name}-night") if n not in design]
    missing += [name for name in singles if name not in design]
    assert not missing, (
        f"colours in the stylesheet but not in DESIGN.md: {sorted(missing)}"
    )


def test_no_raw_colours_in_the_stylesheet_or_scripts() -> None:
    """Components use roles (--ink, --primary). Raw values belong in the token block, and only as oklch()."""
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", CSS)
    assert not hexes, f"raw hex colours in app.css: {hexes}"
    for script in (STATIC / "js").glob("*.js"):
        found = re.findall(r"#[0-9a-fA-F]{6}\b", script.read_text(encoding="utf-8"))
        assert not found, f"raw hex colours in {script.name}: {found}"


def test_the_stylesheet_keeps_to_the_craft_rules() -> None:
    assert "transition: all" not in CSS and "transition:all" not in CSS
    declarations = re.findall(r"!important\s*[;}]", CSS)
    assert len(declarations) == 1, (
        "the [hidden] rule is the one place !important is justified"
    )
    assert "100vh" not in CSS, "use dvh: 100vh sits behind a phone's address bar"
    assert re.search(r"@media \(prefers-reduced-motion: no-preference\)", CSS)
    for name in ("font-display: swap",):
        assert name in CSS


def test_the_theme_colours_in_the_page_match_the_palette() -> None:
    """meta theme-color cannot use oklch(), so its hex values are converted from the tokens; recompute them."""
    import math

    def to_hex(lightness: float, chroma: float, hue: float) -> str:
        a, b = (
            chroma * math.cos(math.radians(hue)),
            chroma * math.sin(math.radians(hue)),
        )
        l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
        m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
        s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
        linear = (
            4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
            -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
            -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
        )

        def encode(c: float) -> int:
            c = max(0.0, min(1.0, c))
            return round(
                255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055)
            )

        return "#" + "".join(f"{encode(c):02x}" for c in linear)

    design = documented()
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert (
        f'content="{to_hex(*numbers(design["paper"]))}" media="(prefers-color-scheme: light)"'
        in html
    )
    assert (
        f'content="{to_hex(*numbers(design["paper-night"]))}" media="(prefers-color-scheme: dark)"'
        in html
    )
    manifest = (STATIC / "manifest.webmanifest").read_text(encoding="utf-8")
    assert to_hex(*numbers(design["paper-night"])) in manifest
