# agents/image_extraction.py — Newsletter AI Pipeline v4.0
# Agent 4: Image Extraction
#
# Responsibilities:
#   - Parse newsletter HTML for <img> tags
#   - Apply a multi-rule filter to reject tracking pixels, spacers,
#     oversized files, and unsupported formats
#   - Download accepted images with a browser-like User-Agent
#   - Convert WEBP images to GIF for Obsidian and iOS compatibility
#   - Save assets to {PROJECT_ROOT}/notes/assets/{message_slug}/
#   - Write a manifest.json alongside the assets for auditability
#   - Return a manifest list for the note writer and image_log
#
# Enabled by default. Skip entirely by passing enable_images=False
# (set via the --no-images CLI flag in the orchestrator).
#
# Zero API cost — no LLM calls. All processing is local.
#
# Usage (standalone test):
#   cd pipeline && python agents/image_extraction.py
# =============================================================================

import hashlib
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image as PILImage

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_agents_dir   = Path(__file__).resolve().parent
_pipeline_dir = _agents_dir.parent
for _p in (_pipeline_dir, _agents_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# BUGFIX: this import previously sat ABOVE the sys.path bootstrap, so running
# the module standalone (python agents/image_extraction.py — the usage its
# own header documents) failed with ModuleNotFoundError: db. It only worked
# when imported via the orchestrator, which had already added pipeline/ to
# sys.path. Local imports must come after the bootstrap.
from db import get_image_by_hash, register_image_hash, bump_image_reuse

from config import (
    PROJECT_ROOT,
    IMAGE_MIN_SIZE_BYTES,
    IMAGE_MAX_SIZE_BYTES,
    IMAGE_MIN_DIMENSION,
    IMAGE_DEDUP_ENABLED,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mimic a real browser to avoid CDN blocks on image requests.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

# Timeout for individual image downloads (seconds).
_DOWNLOAD_TIMEOUT = 15

# URL substrings that indicate a tracking or analytics image.
# Any match causes the image to be rejected regardless of other attributes.
_TRACKING_PATTERNS = [
    "track", "pixel", "beacon", "open.php", "click.php",
    "analytics", "utm_", "trk", "trck", "1x1", "spacer",
    "transparent.gif", "blank.gif",
]

# MIME types we are willing to save. SVG is accepted but not converted.
_ALLOWED_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
}

# Map from PIL format name → file extension used when saving.
_FORMAT_TO_EXT = {
    "PNG":  ".png",
    "JPEG": ".jpg",
    "JPG":  ".jpg",
    "GIF":  ".gif",
    "WEBP": ".gif",   # WEBP is saved as GIF after conversion
    "SVG":  ".svg",
}


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def should_save_image(
    url:          str,
    alt_text:     str,
    size_bytes:   int,
    width_px:     int,
    height_px:    int,
    content_type: str,
) -> tuple[bool, str]:
    """
    Decide whether an image should be saved based on a cascade of rules.

    Rules are evaluated in order — the first matching rule short-circuits.
    Returns (True, "accepted") or (False, rejection_reason).

    Args:
        url:          Full source URL of the image.
        alt_text:     The alt attribute from the <img> tag (may be empty).
        size_bytes:   Downloaded file size in bytes.
        width_px:     Image width in pixels (from PIL).
        height_px:    Image height in pixels (from PIL).
        content_type: Content-Type header from the HTTP response.

    Returns:
        (should_save: bool, reason: str)
    """
    # File size checks
    if size_bytes < IMAGE_MIN_SIZE_BYTES:
        return False, "too_small"

    if size_bytes > IMAGE_MAX_SIZE_BYTES:
        return False, "exceeds_1mb_cap"

    # Dimension checks
    if width_px <= 1 or height_px <= 1:
        return False, "1x1_tracker"

    if width_px < IMAGE_MIN_DIMENSION or height_px < IMAGE_MIN_DIMENSION:
        return False, "decorative_spacer"

    # Tracking URL check
    url_lower = url.lower()
    if any(pattern in url_lower for pattern in _TRACKING_PATTERNS):
        return False, "tracking_url"

    # Content type check
    normalised_type = content_type.split(";")[0].strip().lower()
    if normalised_type not in _ALLOWED_CONTENT_TYPES:
        return False, "unsupported_type"

    return True, "accepted"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _download_image(url: str) -> tuple[bytes | None, str, str]:
    """
    Download an image from a URL.

    Returns:
        (image_bytes, content_type, error_reason)
        On success: (bytes, content_type, "")
        On failure: (None, "", error_reason)
    """
    try:
        response = requests.get(
            url,
            headers=_HEADERS,
            timeout=_DOWNLOAD_TIMEOUT,
            stream=True,
        )

        if response.status_code != 200:
            return None, "", f"http_{response.status_code}"

        content_type = response.headers.get("Content-Type", "image/unknown")
        image_bytes  = response.content

        if not image_bytes:
            return None, "", "empty_response"

        return image_bytes, content_type, ""

    except requests.exceptions.Timeout:
        return None, "", "timeout"
    except requests.exceptions.ConnectionError:
        return None, "", "connection_error"
    except requests.exceptions.RequestException as exc:
        return None, "", f"request_error: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# Image inspection and conversion
# ---------------------------------------------------------------------------

def _inspect_image(image_bytes: bytes) -> tuple[int, int, str]:
    """
    Open image bytes with Pillow to get dimensions and format.

    Returns:
        (width_px, height_px, format_string)
        format_string is Pillow's format name e.g. "PNG", "JPEG", "GIF", "WEBP"
        Returns (0, 0, "UNKNOWN") if Pillow cannot open the bytes.
    """
    try:
        with PILImage.open(io.BytesIO(image_bytes)) as img:
            return img.size[0], img.size[1], (img.format or "UNKNOWN")
    except Exception:
        return 0, 0, "UNKNOWN"


def _convert_webp_to_gif(image_bytes: bytes) -> bytes:
    """
    Convert WEBP image bytes to GIF format using Pillow.

    GIF is used (rather than PNG) because animated WEBPs can be converted
    to animated GIFs, preserving motion. Static WEBPs produce single-frame GIFs.

    Returns:
        GIF-encoded bytes, or the original bytes if conversion fails.
    """
    try:
        with PILImage.open(io.BytesIO(image_bytes)) as img:
            buf = io.BytesIO()

            # Preserve animation if present (animated WEBP → animated GIF)
            frames = []
            try:
                while True:
                    frames.append(img.copy().convert("RGBA"))
                    img.seek(img.tell() + 1)
            except EOFError:
                pass

            if len(frames) > 1:
                # Animated
                frames[0].save(
                    buf,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    loop=0,
                    optimize=False,
                )
            else:
                # Static
                img.convert("RGBA").save(buf, format="GIF")

            return buf.getvalue()

    except Exception as exc:
        print(f"      [image] WEBP→GIF conversion failed: {exc} — keeping original")
        return image_bytes


# ---------------------------------------------------------------------------
# Filename generation
# ---------------------------------------------------------------------------

def _make_filename(url: str, saved_format: str) -> str:
    """
    Generate a deterministic filename from the image URL.

    Uses the first 10 characters of the MD5 hash of the URL, plus the
    appropriate extension for the saved format. This means:
      - The same URL always produces the same filename (idempotent)
      - Filenames are short and filesystem-safe
      - No risk of collision with other common naming schemes

    Args:
        url:          Source URL (used as hash input).
        saved_format: Pillow format name after any conversion (e.g. "GIF").

    Returns:
        Filename string, e.g. "a3f9d12e01.png"
    """
    url_hash  = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    extension = _FORMAT_TO_EXT.get(saved_format.upper(), ".bin")
    return url_hash + extension

# ---------------------------------------------------------------------------
# Image hash generation
# ---------------------------------------------------------------------------

def _hash_image_bytes(image_bytes: bytes) -> str:
    """
    Compute a SHA-256 content hash of the final (post-conversion) image
    bytes — i.e. hash AFTER WEBP→GIF conversion, so two newsletters
    serving the identical WEBP banner still hash identically once both
    are converted to GIF.
 
    Content hashing (not URL hashing) is deliberate: many ESPs append a
    per-send tracking query string to otherwise-identical image URLs
    (e.g. ?utm_campaign=2026-06-18), so the existing _make_filename()
    URL-based hash treats every send as a brand-new image. Hashing the
    actual bytes catches these as duplicates regardless of URL.
 
    Returns:
        64-character hex digest string.
    """
    return hashlib.sha256(image_bytes).hexdigest()

# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def make_message_slug(message_id: str, received_date: str = "", account_alias: str = "") -> str:
    """
    Derive a readable, date-prefixed folder slug for asset storage.

    Format: YYYY-MM-DD-{account}-{short-hash}
    Example: 2026-06-07-personal-a3f9d12e

    The short hash suffix guarantees uniqueness when multiple emails
    from the same account arrive on the same day.

    Args:
        message_id:    RFC 2822 Message-ID header — used for the hash component.
        received_date: Raw Date header string — used for the date component.
                       Falls back to today's UTC date if absent or unparseable.
        account_alias: Account alias string (e.g. "personal", "work").
                       Omitted from slug if empty.

    Returns:
        Filesystem-safe slug string.
    """
    import re
    import hashlib
    from datetime import datetime, timezone

    # --- Date component ---
    date_str = ""
    if received_date:
        # Match DD Mon YYYY pattern in RFC 2822 Date headers
        _MONTHS = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        match = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", received_date)
        if match:
            day   = match.group(1).zfill(2)
            month = _MONTHS.get(match.group(2).lower(), "01")
            year  = match.group(3)
            date_str = f"{year}-{month}-{day}"

    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Hash component ---
    clean_hash = hashlib.md5(message_id.encode("utf-8")).hexdigest()[:8]

    # --- Account component ---
    account_part = f"-{account_alias}" if account_alias else ""

    return f"{date_str}{account_part}-{clean_hash}"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def extract_images(email: dict, enable_images: bool = True) -> list[dict]:
    """
    Extract, filter, download, and save images from a newsletter email.

    This is the main entry point called by the orchestrator. It handles
    the full lifecycle: parse → filter → download → inspect → convert → save.

    Args:
        email:         Email dict from Agent 1/1.5. Uses:
                         body_html     — HTML source to parse for <img> tags
                         message_id    — used to derive the asset folder slug
                         account_alias — tagged in manifest entries
        enable_images: If False, returns an empty list immediately (--no-images flag).

    Returns:
        List of manifest entry dicts — one per image that passed the filter.
        Each entry contains:
          filename       str  — saved filename
          source_url     str  — original <img> src URL
          original_format str — Pillow format before conversion
          saved_format   str  — format actually written to disk
          size_bytes     int  — file size of the saved image
          width_px       int  — image width
          height_px      int  — image height
          alt_text       str  — <img> alt attribute
          filter_result  str  — "accepted"
          local_path     str  — full Windows path of saved image
          message_id     str  — for image_log FK
          account_alias  str  — for image_log
          processed_at   str  — ISO 8601 UTC timestamp

        Rejected images are written to manifest.json but NOT returned
        (callers only need the accepted assets for note rendering and logging).

    Side effect:
        Writes accepted image files and manifest.json to:
        {PROJECT_ROOT}/notes/assets/{slug}/
    """
    if not enable_images:
        return []

    html = email.get("body_html", "")
    if not html:
        print(f"    [image] No HTML body — skipping image extraction")
        return []

    slug = make_message_slug(
        message_id    = email.get("message_id", "unknown"),
        received_date = email.get("received_date", ""),
        account_alias = email.get("account_alias", ""),
    )
    asset_dir = Path(PROJECT_ROOT) / "notes" / "assets" / slug
    asset_dir.mkdir(parents=True, exist_ok=True)

    # Parse all <img> tags from the HTML
    soup     = BeautifulSoup(html, "html.parser")
    img_tags = soup.find_all("img")

    if not img_tags:
        print(f"    [image] No <img> tags found in HTML")
        return []

    print(f"    [image] Found {len(img_tags)} <img> tag(s) — processing...")

    full_manifest: list[dict] = []   # all entries (accepted + rejected)
    accepted:      list[dict] = []   # only accepted entries (returned to caller)
    now = datetime.now(timezone.utc).isoformat()

    for img_tag in img_tags:
        url      = img_tag.get("src", "").strip()
        alt_text = img_tag.get("alt", "").strip()

        # Skip missing, relative, or data: URIs
        if not url or not url.startswith("http"):
            continue

        # --- Download ---
        image_bytes, content_type, error = _download_image(url)
        if image_bytes is None:
            full_manifest.append({
                "source_url":    url,
                "alt_text":      alt_text,
                "filter_result": f"download_failed:{error}",
            })
            continue

        size_bytes = len(image_bytes)

        # --- Inspect ---
        width_px, height_px, original_format = _inspect_image(image_bytes)

        if original_format == "UNKNOWN":
            full_manifest.append({
                "source_url":    url,
                "alt_text":      alt_text,
                "size_bytes":    size_bytes,
                "filter_result": "unreadable_format",
            })
            continue

        # --- Filter ---
        should_save, reason = should_save_image(
            url, alt_text, size_bytes, width_px, height_px, content_type
        )

        if not should_save:
            full_manifest.append({
                "source_url":      url,
                "alt_text":        alt_text,
                "size_bytes":      size_bytes,
                "width_px":        width_px,
                "height_px":       height_px,
                "original_format": original_format,
                "filter_result":   reason,
            })
            continue

        # --- Convert WEBP → GIF ---
        saved_format = original_format
        if original_format == "WEBP":
            image_bytes  = _convert_webp_to_gif(image_bytes)
            saved_format = "GIF"
            size_bytes   = len(image_bytes)

        # --- Deduplication check (content-hash based) ---
        # Computed AFTER any format conversion so the hash reflects the bytes
        # that would actually be written to disk.
        content_hash = _hash_image_bytes(image_bytes)
        dedup_hit    = get_image_by_hash(content_hash) if IMAGE_DEDUP_ENABLED else None

        if dedup_hit:
            # Identical image already saved elsewhere — reuse it instead of
            # writing a new copy. The note still gets a working image
            # reference; the file on disk is the original canonical copy.
            bump_image_reuse(content_hash)
            entry = {
                "filename":        Path(dedup_hit["canonical_path"]).name,
                "source_url":      url,
                "source_type":     "external",
                "original_format": original_format,
                "saved_format":    saved_format,
                "size_bytes":      size_bytes,
                "width_px":        width_px,
                "height_px":       height_px,
                "alt_text":        alt_text,
                "filter_result":   "accepted_deduplicated",
                "local_path":      dedup_hit["canonical_path"],
                "message_id":      email.get("message_id", ""),
                "account_alias":   email.get("account_alias", ""),
                "processed_at":    now,
                "duplicate_of":    dedup_hit["first_message_id"],
            }
            full_manifest.append(entry)
            accepted.append(entry)
            print(
                f"    [image] Duplicate skipped — reusing "
                f"{Path(dedup_hit['canonical_path']).name} "
                f"(first seen: {dedup_hit['first_message_id'][:30]})"
            )
            continue

        # --- Save (no existing copy found — write a new file as before) ---
        filename  = _make_filename(url, saved_format)
        save_path = asset_dir / filename

        try:
            save_path.write_bytes(image_bytes)
        except OSError as exc:
            full_manifest.append({
                "source_url":    url,
                "alt_text":      alt_text,
                "filter_result": f"write_failed:{exc}",
            })
            continue

        # Record this as the canonical copy for this content hash so future
        # duplicates (in this run or any future run) are caught.
        if IMAGE_DEDUP_ENABLED:
            register_image_hash(
                content_hash   = content_hash,
                canonical_path = str(save_path),
                message_id     = email.get("message_id", ""),
            )

        entry = {
            "filename":        filename,
            "source_url":      url,
            "source_type":     "external",
            "original_format": original_format,
            "saved_format":    saved_format,
            "size_bytes":      size_bytes,
            "width_px":        width_px,
            "height_px":       height_px,
            "alt_text":        alt_text,
            "filter_result":   "accepted",
            "local_path":      str(save_path),
            "message_id":      email.get("message_id", ""),
            "account_alias":   email.get("account_alias", ""),
            "processed_at":    now,
        }
        full_manifest.append(entry)
        accepted.append(entry)

    # --- Write manifest.json ---
    manifest_path = asset_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(full_manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    rejected_count = len(full_manifest) - len(accepted)
    print(
        f"    [image] Saved: {len(accepted)} | "
        f"Rejected: {rejected_count} | "
        f"Asset folder: assets/{slug}/"
    )

    return accepted

# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Test image extraction with a synthetic email containing a mix of
    real and synthetic image URLs. Uses httpbin for a real download test.

    Run: python agents/image_extraction.py
    """
    print("=== Image Extraction Agent — standalone test ===\n")

    # Test the filter function directly with known cases
    print("Step 1: Filter rule validation")
    print(f"{'─' * 60}")

    filter_cases = [
        # (url, alt, size, w, h, content_type, expected)
        ("https://example.com/chart.png",       "chart",   50_000, 800, 400, "image/png",  True),
        ("https://example.com/track.gif",       "",         1_000,   1,   1, "image/gif", False),  # tracking URL + 1x1
        ("https://track.example.com/pixel.gif", "",           200,   1,   1, "image/gif", False),  # too small + 1x1
        ("https://example.com/spacer.png",      "",         8_000,  20,  20, "image/png", False),  # decorative spacer
        ("https://example.com/huge.jpg",        "photo", 2_000_000, 800, 600, "image/jpeg",False), # exceeds 1MB
        ("https://example.com/diagram.webp",    "diagram", 80_000, 600, 400, "image/webp", True),
        ("https://analytics.co/open.php?id=1",  "",        10_000, 100, 100, "image/png", False),  # tracking URL
        ("https://example.com/icon.gif",        "icon",    12_000,  40,  40, "image/gif", False),  # below min dimension
    ]

    all_pass = True
    for url, alt, size, w, h, ctype, expected in filter_cases:
        result, reason = should_save_image(url, alt, size, w, h, ctype)
        status = "✓" if result == expected else "✗"
        if result != expected:
            all_pass = False
        print(
            f"  {status} {'ACCEPT' if result else 'REJECT':<6} "
            f"reason={reason:<25} url=...{url[-30:]}"
        )

    print(f"\n  Filter tests: {'all passed ✓' if all_pass else 'FAILURES DETECTED ✗'}")

    # Test slug generation
    print(f"\nStep 2: Slug generation")
    print(f"{'─' * 60}")
    test_ids = [
        "<CABcd1234efgh5678@mail.gmail.com>",
        "<20260607.123456.789@newsletter.example.com>",
        "<short@x.com>",
    ]
    for mid in test_ids:
        slug = make_message_slug(mid)
        print(f"  {mid[:45]:<45} → {slug}")

    # Test with a real downloadable image (httpbin returns a PNG)
    print(f"\nStep 3: Live download test (httpbin.org PNG)")
    print(f"{'─' * 60}")

    test_email = {
        "message_id":    "<live-test-001@test.com>",
        "account_alias": "test",
        "body_html": (
            '<html><body>'
            # Real small PNG from httpbin
            '<img src="https://httpbin.org/image/png" alt="test image">'
            # Tracking pixel (should be filtered)
            '<img src="https://example.com/track/pixel.gif" alt="">'
            # Data URI (should be skipped — not http)
            '<img src="data:image/png;base64,abc123" alt="inline">'
            '</body></html>'
        ),
    }

    results = extract_images(test_email, enable_images=True)
    print(f"\n  Assets returned to caller: {len(results)}")
    for r in results:
        print(
            f"    {r['filename']} | "
            f"{r['saved_format']} | "
            f"{r['width_px']}x{r['height_px']} | "
            f"{r['size_bytes']:,} bytes"
        )

    # Clean up test asset folder
    import shutil
    test_slug = make_message_slug("<live-test-001@test.com>")
    test_dir  = Path(PROJECT_ROOT) / "notes" / "assets" / test_slug
    if test_dir.exists():
        shutil.rmtree(test_dir)
        print(f"\n  Cleaned up test asset folder: assets/{test_slug}/")

    print("\nTest complete.")
