#!/usr/bin/env python3
"""Import any ComfyUI workflow into the MCP bridge's library, generically.

Why this exists
---------------
ComfyUI's ``/prompt`` API only accepts *API-format* graphs
(``{node_id: {class_type, inputs}}``). The bridge keeps its own pre-converted,
parameterized copies in ``config/comfyui-mcp/workflows/`` — which drift from the
workflow you actually tuned in the ComfyUI UI (wrong LoRA, stale steps, etc.).

Rather than re-implement ComfyUI's fragile UI->API conversion, we reuse
ComfyUI's *own* authoritative conversion: every image/video ComfyUI renders
embeds the exact API graph it executed (PNG ``prompt`` tEXt chunk; mp4/webm
``prompt`` metadata). Sub-graphs are already flattened in that embedded graph,
so collapsed UI groups are a non-issue.

Generic knob detection (the important part)
-------------------------------------------
Instead of assuming a ``KSampler`` topology, we scan **every** node for concrete
(non-linked) inputs whose *name* matches a known knob (``seed``/``noise_seed``,
``steps``, ``cfg``, ``sampler_name``, ``scheduler``, ``denoise``, ``width``,
``height``, ``length``/``num_frames``, ``fps``/``frame_rate``, ``prompt``,
``text``, ``image`` ...). ComfyUI input names are highly conventional across
node classes, so this generalizes across t2i, img2img, upscale, edit and video
(``SamplerCustomAdvanced`` chains, ``KSamplerAdvanced``, ``WanImageToVideo``,
``MiniMaxH3ImageToVideo`` ...).

Rules that keep it correct and general:
  * A knob is only exposed (templatized) when every node it binds to holds the
    **same** concrete value — otherwise it's left at the UI values (still
    honored, just not chat-overridable) to avoid clobbering an asymmetric
    two-stage sampler. Use a title tag to expose one specifically.
  * **Title tags** are the authoritative escape hatch: rename a node in the UI
    to include ``@steps`` / ``@seed`` / ``@width`` / ``@length`` / ``@fps`` /
    ``@prompt`` / ``@negative`` / ``@input_image`` / ``@denoise`` (optionally
    ``@steps:8`` to also pin the default). The tagged node's matching input is
    bound to that param regardless of topology or value conflicts.
  * The concrete UI value of every exposed knob is captured into the sidecar
    ``<name>.meta.json`` ``defaults`` so an omitted param falls back to the UI
    value (the launcher applies these at render time). ``prompt`` stays
    required; ``seed`` is randomized on omit; ``input_image`` uploads on use.

Usage
-----
    scripts/comfyui_workflow_import.py krea2_turbo_bf16 --from-output krea2-turbo-bf16
    scripts/comfyui_workflow_import.py minimax_h3 --from-media .../MiniMax_H3_00006_.mp4
    scripts/comfyui_workflow_import.py foo --from-api /path/exported_api.json --dry-run

Changing which params are exposed changes the advertised tool schema, so after a
first import (or a change to the exposed set) restart the bridges:
    sudo systemctl restart comfyui-mcp comfyui-mcp-secure
Content-only edits (LoRA/steps values) hot-reload with no restart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_WORKFLOW_DIR = Path(
    os.environ.get("COMFY_MCP_WORKFLOW_DIR", "/srv/ai/config/comfyui-mcp/workflows")
)
DEFAULT_OUTPUT_DIRS = [
    Path("/srv/ai/comfyui/output-open"),
    Path("/srv/ai/comfyui/output-secure"),
    Path("/srv/ai/comfyui/output"),
]

# --------------------------------------------------------------------------- #
# Knob catalog: param name -> (accepted input names, python type, placeholder).
# Detection is by INPUT NAME across ALL node classes (not node class_type),
# which is what makes this generic across sampler families and video nodes.
# --------------------------------------------------------------------------- #
KNOBS: dict[str, dict[str, Any]] = {
    "seed":            {"names": ("seed", "noise_seed"),               "type": int,   "ph": "PARAM_INT_SEED"},
    "steps":           {"names": ("steps",),                            "type": int,   "ph": "PARAM_INT_STEPS"},
    "cfg":             {"names": ("cfg", "guidance", "cfg_scale"),      "type": float, "ph": "PARAM_FLOAT_CFG"},
    "sampler_name":    {"names": ("sampler_name",),                     "type": str,   "ph": "PARAM_SAMPLER_NAME"},
    "scheduler":       {"names": ("scheduler",),                        "type": str,   "ph": "PARAM_SCHEDULER"},
    "denoise":         {"names": ("denoise",),                          "type": float, "ph": "PARAM_FLOAT_DENOISE"},
    "width":           {"names": ("width",),                            "type": int,   "ph": "PARAM_INT_WIDTH"},
    "height":          {"names": ("height",),                           "type": int,   "ph": "PARAM_INT_HEIGHT"},
    "length":          {"names": ("length", "num_frames", "frames", "video_frames"),
                                                                        "type": int,   "ph": "PARAM_INT_LENGTH"},
    "fps":             {"names": ("fps", "frame_rate"),                 "type": float, "ph": "PARAM_FLOAT_FPS"},
    "duration":        {"names": ("duration",),                         "type": float, "ph": "PARAM_FLOAT_DURATION"},
    "megapixels":      {"names": ("megapixels", "total_megapixels"),    "type": float, "ph": "PARAM_FLOAT_MEGAPIXELS"},
    "prompt":          {"names": ("prompt", "text", "positive"),        "type": str,   "ph": "PARAM_PROMPT"},
    "negative_prompt": {"names": ("negative_prompt",),                  "type": str,   "ph": "PARAM_NEGATIVE_PROMPT"},
    "input_image":     {"names": ("image",),                            "type": str,   "ph": "PARAM_INPUT_IMAGE", "kind": "image"},
}
# Knobs never auto-detected (only via an explicit title tag) because their input
# name is too generic / ambiguous to grab safely.
_TAG_ONLY = {"duration"}
# Params that must always be supplied by the caller (no sensible UI default).
REQUIRED_PARAMS = {"prompt", "input_image"}
# Params excluded from captured defaults (handled specially at render time).
_NO_DEFAULT = {"prompt", "seed", "input_image"}

# Title-tag syntax:  @name   or   @name:default   (case-insensitive name).
_TAG_RE = re.compile(r"@([a-zA-Z_][a-zA-Z0-9_]*)(?::([^\s@]+))?")
# Friendly aliases accepted inside @tags.
_TAG_ALIASES = {
    "negative": "negative_prompt",
    "neg": "negative_prompt",
    "pos": "prompt",
    "positive": "prompt",
    "image": "input_image",
    "img": "input_image",
    "input": "input_image",
    "sampler": "sampler_name",
    "frames": "length",
    "num_frames": "length",
}


# --------------------------------------------------------------------------- #
# Embedded-graph extraction (PNG tEXt/iTXt; mp4/webm/webp binary scan).
# --------------------------------------------------------------------------- #
def read_png_text_chunks(path: Path) -> dict[str, str]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    out: dict[str, str] = {}
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"tEXt":
            key, _, val = chunk.partition(b"\x00")
            out[key.decode("latin-1")] = val.decode("latin-1")
        elif ctype == b"iTXt":
            key, _, rest = chunk.partition(b"\x00")
            if len(rest) < 2:
                continue
            comp_flag = rest[0]
            body = rest[2:]
            _, _, body = body.partition(b"\x00")
            _, _, body = body.partition(b"\x00")
            try:
                text = zlib.decompress(body) if comp_flag == 1 else body
                out[key.decode("latin-1")] = text.decode("utf-8")
            except Exception:
                pass
        elif ctype == b"IEND":
            break
    return out


def _balanced_json_after(data: bytes, key: bytes) -> Optional[str]:
    """Find `key\\x00` (or `key=`) then return the first balanced {...} object."""
    for sep in (key + b"\x00", key + b"="):
        i = data.find(sep)
        if i >= 0:
            break
    else:
        return None
    j = data.find(b"{", i)
    if j < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    start = j
    while j < len(data):
        c = data[j]
        if in_str:
            if esc:
                esc = False
            elif c == 0x5C:  # backslash
                esc = True
            elif c == 0x22:  # quote
                in_str = False
        else:
            if c == 0x22:
                in_str = True
            elif c == 0x7B:  # {
                depth += 1
            elif c == 0x7D:  # }
                depth -= 1
                if depth == 0:
                    return data[start : j + 1].decode("utf-8", "replace")
        j += 1
    return None


def api_graph_from_media(path: Path) -> dict:
    """Extract the flattened API graph embedded by ComfyUI in a rendered file."""
    suffix = path.suffix.lower()
    if suffix == ".png":
        chunks = read_png_text_chunks(path)
        if "prompt" in chunks:
            return json.loads(chunks["prompt"])
        raise ValueError(f"{path}: no embedded 'prompt' graph (made by ComfyUI?)")
    # mp4 / webm / webp / gif: ComfyUI writes the API graph as a 'prompt' tag.
    data = path.read_bytes()
    raw = _balanced_json_after(data, b"prompt")
    if raw is None:
        raise ValueError(f"{path}: no embedded 'prompt' graph found")
    return json.loads(raw)


def looks_like_api_graph(obj: Any) -> bool:
    return isinstance(obj, dict) and any(
        isinstance(v, dict) and "class_type" in v for v in obj.values()
    )


def load_source_graph(
    name: str,
    from_api: Optional[str] = None,
    from_media: Optional[str] = None,
    from_output: Optional[str] = None,
    output_dirs: Optional[list[Path]] = None,
) -> tuple[dict, str]:
    if from_api:
        p = Path(from_api)
        obj = json.loads(p.read_text())
        if not looks_like_api_graph(obj):
            raise SystemExit(
                f"{p} is not an API-format graph. In ComfyUI use "
                "'Save (API Format)' / 'Export (API)', or import from a rendered "
                "PNG/MP4 which embeds the API graph."
            )
        return obj, str(p)
    if from_media:
        p = Path(from_media)
        return api_graph_from_media(p), str(p)
    prefix = from_output or name.replace("_", "-")
    p = newest_media_with_prefix(prefix, output_dirs or DEFAULT_OUTPUT_DIRS)
    return api_graph_from_media(p), str(p)


def newest_media_with_prefix(prefix: str, output_dirs: list[Path]) -> Path:
    exts = ("png", "mp4", "webm", "webp", "gif")
    candidates: list[Path] = []
    for d in output_dirs:
        if d.is_dir():
            for ext in exts:
                candidates.extend(d.rglob(f"{prefix}*.{ext}"))
    if not candidates:
        raise SystemExit(
            f"No media matching '{prefix}*' under: "
            + ", ".join(str(d) for d in output_dirs if d.is_dir())
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


# --------------------------------------------------------------------------- #
# Graph helpers
# --------------------------------------------------------------------------- #
def _is_link(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], (str, int))


def _concrete_inputs(node: dict) -> dict[str, Any]:
    ins = node.get("inputs", {})
    if not isinstance(ins, dict):
        return {}
    return {k: v for k, v in ins.items() if not _is_link(v)}


def _title(node: dict) -> str:
    return str(node.get("_meta", {}).get("title", "") or "")


def _knob_for_input(input_name: str) -> Optional[str]:
    for knob, spec in KNOBS.items():
        if knob in _TAG_ONLY:
            continue
        if input_name in spec["names"]:
            return knob
    return None


def _resolve_string_source(graph: dict, node_id: str, input_name: str):
    """Follow a link one or two hops to the node holding the concrete string.

    Handles prompt text sourced from Primitive/Text nodes feeding CLIPTextEncode.
    Returns (node_id, input_name) of the concrete string widget, or None.
    """
    node = graph.get(node_id)
    if not isinstance(node, dict):
        return None
    val = node.get("inputs", {}).get(input_name)
    if isinstance(val, str):
        return (node_id, input_name)
    if not _is_link(val):
        return None
    src_id = str(val[0])
    src = graph.get(src_id)
    if not isinstance(src, dict):
        return None
    # First concrete string input on the source node (Primitive 'value', Text, ...)
    for k, v in _concrete_inputs(src).items():
        if isinstance(v, str) and v.strip():
            return (src_id, k)
    return None


# --------------------------------------------------------------------------- #
# Templatize
# --------------------------------------------------------------------------- #
def _classify_text_encodes(graph: dict) -> dict[str, str]:
    """Map CLIPTextEncode-ish node_id -> 'prompt' | 'negative_prompt' by title."""
    roles: dict[str, str] = {}
    for nid, node in graph.items():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type", ""))
        if "TextEncode" not in ct and "CLIPTextEncode" not in ct:
            continue
        if "text" not in node.get("inputs", {}):
            continue
        title = _title(node).lower()
        if "negativ" in title:
            roles[nid] = "negative_prompt"
        elif "positiv" in title:
            roles[nid] = "prompt"
        else:
            roles[nid] = "prompt"  # default assume positive
    return roles


def _scan_title_tags(graph: dict) -> dict[str, list[tuple[str, str, Any]]]:
    """param -> list of (node_id, input_name, pinned_default_or_None) from @tags."""
    tagged: dict[str, list[tuple[str, str, Any]]] = {}
    for nid, node in graph.items():
        if not isinstance(node, dict):
            continue
        for m in _TAG_RE.finditer(_title(node)):
            raw = m.group(1).lower()
            param = _TAG_ALIASES.get(raw, raw)
            if param not in KNOBS:
                continue
            pinned = m.group(2)
            # Locate the matching input on this node.
            target = _tag_target_input(graph, nid, node, param)
            if target is None:
                continue
            t_nid, t_in = target
            tagged.setdefault(param, []).append((t_nid, t_in, pinned))
    return tagged


def _tag_target_input(graph, nid, node, param):
    spec = KNOBS[param]
    ins = node.get("inputs", {})
    # Prompt/negative may be a link to a text/primitive source.
    if param in ("prompt", "negative_prompt"):
        for cand in ("text", "prompt", "positive"):
            if cand in ins:
                r = _resolve_string_source(graph, nid, cand)
                if r:
                    return r
        # else fall through to name match
    for cand in spec["names"]:
        if cand in ins and not _is_link(ins[cand]):
            return (nid, cand)
    # Single concrete widget fallback (e.g. Primitive value).
    concrete = _concrete_inputs(node)
    if len(concrete) == 1:
        return (nid, next(iter(concrete)))
    return None


def templatize(graph: dict) -> tuple[dict, dict]:
    """Return (templatized_graph, meta). meta has defaults/params/report."""
    graph = json.loads(json.dumps(graph))  # deep copy
    text_roles = _classify_text_encodes(graph)
    tags = _scan_title_tags(graph)

    # Gather auto candidates: param -> list of (node_id, input_name, value)
    candidates: dict[str, list[tuple[str, str, Any]]] = {}

    def add_candidate(param, nid, inp, val):
        candidates.setdefault(param, []).append((nid, inp, val))

    for nid, node in graph.items():
        if not isinstance(node, dict):
            continue
        for inp, val in _concrete_inputs(node).items():
            # Prompt/negative handled via text-encode roles + name below.
            if inp in ("text",):
                role = text_roles.get(nid)
                if role and isinstance(val, str):
                    add_candidate(role, nid, inp, val)
                continue
            knob = _knob_for_input(inp)
            if not knob:
                continue
            if knob == "prompt" and inp == "positive":
                continue  # 'positive' is usually a link; skip stray matches
            # Type sanity: don't grab a string where we expect a number, etc.
            if not _value_type_ok(knob, val):
                continue
            add_candidate(knob, nid, inp, val)

    # Also catch prompt text sourced through a link on positive text-encodes.
    for nid, role in text_roles.items():
        if any(c[0] == nid for c in candidates.get(role, [])):
            continue
        r = _resolve_string_source(graph, nid, "text")
        if r:
            src_id, src_in = r
            val = graph[src_id]["inputs"][src_in]
            add_candidate(role, src_id, src_in, val)

    params: dict[str, dict] = {}
    defaults: dict[str, Any] = {}
    exposed: dict[str, dict] = {}
    skipped: dict[str, str] = {}

    all_params = set(candidates) | set(tags)
    for param in all_params:
        spec = KNOBS[param]
        tag_binds = tags.get(param, [])
        if tag_binds:
            bindings = [(n, i) for (n, i, _p) in tag_binds]
            pinned = next((p for (_n, _i, p) in tag_binds if p is not None), None)
            if pinned is not None:
                value = _coerce(pinned, spec["type"])
            else:
                # take value from first tagged binding's current graph value
                n0, i0 = bindings[0]
                value = graph[n0]["inputs"].get(i0)
            source = "tag"
        else:
            binds = candidates.get(param, [])
            values = [v for (_n, _i, v) in binds]
            bindings = [(n, i) for (n, i, _v) in binds]
            # Equal-value gate: only expose if all bound values agree.
            if len({json.dumps(v, sort_keys=True) for v in values}) > 1:
                skipped[param] = (
                    f"differing values across {len(bindings)} nodes "
                    f"({values!r}); tag one node with @{param} to expose"
                )
                continue
            value = values[0]
            source = "auto"

        # Inject placeholder at every binding.
        for (n, i) in bindings:
            if n in graph and "inputs" in graph[n]:
                graph[n]["inputs"][i] = spec["ph"]
        params[param] = {
            "type": spec["type"].__name__,
            "kind": spec.get("kind", "scalar"),
            "bindings": [list(b) for b in bindings],
            "source": source,
        }
        exposed[param] = {"value": value, "source": source, "bindings": bindings}
        if param not in _NO_DEFAULT and value is not None:
            defaults[param] = _coerce(value, spec["type"])

    meta = {
        "params": params,
        "defaults": defaults,
        "report": {
            "exposed": {k: {"value": v["value"], "source": v["source"],
                            "bindings": [list(b) for b in v["bindings"]]}
                        for k, v in exposed.items()},
            "skipped": skipped,
        },
    }
    return graph, meta


def _value_type_ok(knob: str, val: Any) -> bool:
    t = KNOBS[knob]["type"]
    if knob in ("prompt", "negative_prompt", "sampler_name", "scheduler", "input_image"):
        return isinstance(val, str)
    if t is int:
        return isinstance(val, bool) is False and isinstance(val, (int,)) and not isinstance(val, bool)
    if t is float:
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    return True


def _coerce(val: Any, t: type):
    try:
        if t is int:
            return int(float(val)) if isinstance(val, str) else int(val)
        if t is float:
            return float(val)
        if t is str:
            return str(val)
        if t is bool:
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "on")
            return bool(val)
    except (ValueError, TypeError):
        return val
    return val


# --------------------------------------------------------------------------- #
# Meta assembly + write
# --------------------------------------------------------------------------- #
def build_meta(name: str, source: str, meta_core: dict, prior: dict) -> dict:
    params = meta_core["params"]
    override_mappings = {p: [tuple(b) for b in info["bindings"]]
                         for p, info in params.items()}
    graph_hash = None  # filled by caller after templatized graph is serialized
    meta = dict(prior)  # preserve any user-added name/description/constraints
    meta.setdefault("name", name.replace("_", " ").title())
    meta.setdefault("description", f"Execute the '{name}' ComfyUI workflow.")
    meta["defaults"] = meta_core["defaults"]
    meta["params"] = params
    meta["override_mappings"] = {k: [list(b) for b in v]
                                 for k, v in override_mappings.items()}
    meta["required"] = sorted(p for p in params if p in REQUIRED_PARAMS)
    meta["source"] = source
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    meta["report"] = meta_core["report"]
    return meta


def import_workflow(
    name: str,
    graph: dict,
    source: str,
    dest_dir: Path = DEFAULT_WORKFLOW_DIR,
    dry_run: bool = False,
) -> dict:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    templ, core = templatize(graph)
    templ_json = json.dumps(templ, indent=2, sort_keys=True)

    wf_path = dest_dir / f"{safe}.json"
    meta_path = dest_dir / f"{safe}.meta.json"
    prior = {}
    if meta_path.exists():
        try:
            prior = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            prior = {}
    meta = build_meta(safe, source, core, prior)
    meta["hash"] = hashlib.sha256(templ_json.encode()).hexdigest()[:16]
    meta_json = json.dumps(meta, indent=2, sort_keys=True)

    # Detect schema (exposed-param-set) change -> needs bridge restart.
    prior_params = set((prior or {}).get("params", {}).keys())
    new_params = set(meta["params"].keys())
    schema_changed = prior_params != new_params
    old_hash = (prior or {}).get("hash")
    changed = old_hash != meta["hash"] or not wf_path.exists()

    result = {
        "name": safe,
        "source": source,
        "workflow_path": str(wf_path),
        "meta_path": str(meta_path),
        "exposed": meta["report"]["exposed"],
        "skipped": meta["report"]["skipped"],
        "defaults": meta["defaults"],
        "required": meta["required"],
        "schema_changed": schema_changed,
        "prior_params": sorted(prior_params),
        "new_params": sorted(new_params),
        "content_changed": changed,
        "restart_needed": schema_changed,
        "dry_run": dry_run,
    }
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        wf_path.write_text(templ_json + "\n")
        meta_path.write_text(meta_json + "\n")
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fmt_report(r: dict) -> str:
    lines = []
    lines.append(f"workflow : {r['name']}")
    lines.append(f"source   : {r['source']}")
    lines.append("exposed  :")
    for p, info in sorted(r["exposed"].items()):
        binds = ", ".join(f"{n}.{i}" for n, i in info["bindings"])
        lines.append(f"    {p:16} = {info['value']!r:40.40}  [{info['source']}]  ({binds})")
    if r["skipped"]:
        lines.append("skipped  :")
        for p, why in sorted(r["skipped"].items()):
            lines.append(f"    {p:16} : {why}")
    lines.append(f"required : {r['required']}")
    lines.append(f"defaults : {json.dumps(r['defaults'])}")
    if r["schema_changed"]:
        lines.append(
            f"restart  : YES — exposed set changed {r['prior_params']} -> {r['new_params']}\n"
            "           sudo systemctl restart comfyui-mcp comfyui-mcp-secure"
        )
    else:
        lines.append("restart  : no (content hot-reloads by mtime)")
    if r["dry_run"]:
        lines.append("(dry-run: nothing written)")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="bridge workflow id (file stem) to write")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--from-media", help="a rendered PNG/MP4/WEBM embedding the API graph")
    src.add_argument("--from-api", help="an API-format JSON (ComfyUI Save/Export API)")
    src.add_argument("--from-output", help="SaveImage/SaveVideo filename prefix to find newest render")
    ap.add_argument("--output-dir", action="append", type=Path,
                    help="extra ComfyUI output dir to scan (repeatable)")
    ap.add_argument("--dest-dir", type=Path, default=DEFAULT_WORKFLOW_DIR,
                    help=f"bridge workflow dir (default {DEFAULT_WORKFLOW_DIR})")
    ap.add_argument("--dry-run", action="store_true", help="analyze only, write nothing")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    graph, source = load_source_graph(
        args.name,
        from_api=args.from_api,
        from_media=args.from_media,
        from_output=args.from_output,
        output_dirs=args.output_dir or DEFAULT_OUTPUT_DIRS,
    )
    result = import_workflow(args.name, graph, source,
                             dest_dir=args.dest_dir, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_fmt_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
