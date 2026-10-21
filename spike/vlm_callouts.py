"""Claude-vision pass that reads numbered callout markers off a figure crop."""
import base64
import io
import sys

VISION_MODEL = "claude-sonnet-5-5"  # bump to claude-opus-5-5 if callout reading is poor
MAX_VISION_DIM = 1568

CALLOUT_TOOL = {
    "name": "report_callouts",
    "description": "Report every numbered callout marker visible in this printer service-manual figure, with its pixel location in this image.",
    "input_schema": {
        "type": "object",
        "properties": {
            "callouts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "string", "description": "label exactly as printed, e.g. '14' or '3a'"},
                        "x": {"type": "integer", "description": "pixels from the left edge of this image"},
                        "y": {"type": "integer", "description": "pixels from the top edge of this image"},
                    },
                    "required": ["number", "x", "y"],
                },
            }
        },
        "required": ["callouts"],
    },
}

CALLOUT_PROMPT = (
    "This image is a cropped figure from a printer service/parts manual (an exploded parts diagram). "
    "Callouts are the reference numbers that point at individual parts, usually joined to the part by a "
    "leader line; each one refers to a row in a parts table. They may be plain numbers or drawn in a box or "
    "colored highlight, and the same number can appear more than once. Report every such callout with the "
    "label exactly as printed and the approximate pixel location of the label. Do NOT report sub-view "
    "captions or model names (e.g. 'M426/M427', 'JP', 'EXCEPT LBP6680x'), connector codes (e.g. J505, "
    "(J4)), dimensions, page text, or step numbers in procedure illustrations. You must respond by calling "
    "report_callouts; if there are no callouts, pass an empty array."
)


def prepare_image(img):
    """Downscale so the long side <= MAX_VISION_DIM. Returns (image, scale) with scale = orig/sent."""
    long_side = max(img.size)
    if long_side <= MAX_VISION_DIM:
        return img, 1.0
    f = MAX_VISION_DIM / long_side
    return img.resize((round(img.width * f), round(img.height * f))), 1 / f


def extract_callouts(img, client) -> list[dict]:
    """Returns [{number, x, y}] in original-image pixel coords; [] on any failure."""
    try:
        sent, scale = prepare_image(img.convert("RGB"))
        buf = io.BytesIO()
        sent.save(buf, format="PNG")
        resp = client.messages.create(
            model=VISION_MODEL,
            max_tokens=2048,
            tools=[CALLOUT_TOOL],
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                              "data": base64.b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": CALLOUT_PROMPT},
            ]}],
        )
        block = next(b for b in resp.content if b.type == "tool_use")
        return [{"number": str(c["number"]).strip(), "x": round(c["x"] * scale), "y": round(c["y"] * scale)}
                for c in block.input.get("callouts", [])]
    except Exception as e:  # one bad figure must not abort the run
        print(f"  vision call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None
