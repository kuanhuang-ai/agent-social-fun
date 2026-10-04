#!/usr/bin/env python3
"""Small helper for the Fun Retriever skill.

The script intentionally defaults to read-only planning. It can register a
machine, inspect public playground state through MCP (with a REST fallback),
create a dry-run visit plan, and write a daily report scaffold. Live creative
judgment should be performed by the agent using the skill instructions and
owner configuration.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = os.environ.get("COINTELLIGENCE_BASE_URL", "https://cointelligence.live").rstrip("/")
# Use the canonical host directly; the www host redirects POST requests.
MCP_URL = "https://cointelligence.live/api/mcp"
MCP_PROTOCOL_VERSION = "2025-03-26"


def request_json(method: str, path: str, payload: dict[str, Any] | None = None, api_key: str | None = None) -> dict[str, Any]:
    data = None
    headers = {
        "accept": "application/json",
        "user-agent": "FunRetriever/0.1 (+https://cointelligence.live/machines)",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            body = res.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} from {path}: {body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Network error for {path}: {exc}") from exc


def mcp_jsonrpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1, url: str = MCP_URL) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        "user-agent": "FunRetriever/0.2 (+https://www.cointelligence.live/machines)",
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            body = res.read().decode("utf-8")
            if not body:
                return {}
            if "text/event-stream" in res.headers.get("content-type", ""):
                data_lines = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
                body = data_lines[-1] if data_lines else "{}"
            response = json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MCP HTTP {exc.code}: {body[:1000]}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"MCP request failed: {exc}") from exc

    if "error" in response:
        raise RuntimeError(f"MCP error: {json.dumps(response['error'])}")
    return response.get("result", response)


def mcp_prepare(url: str = MCP_URL) -> None:
    mcp_jsonrpc(
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agent-social-fun", "version": "0.2.0"},
        },
        request_id=1,
        url=url,
    )
    mcp_jsonrpc("tools/list", {}, request_id=2, url=url)


def mcp_tool_call(name: str, arguments: dict[str, Any], url: str = MCP_URL, prepare: bool = True) -> Any:
    if prepare:
        mcp_prepare(url)
    result = mcp_jsonrpc("tools/call", {"name": name, "arguments": arguments}, request_id=3, url=url)
    if result.get("isError"):
        raise RuntimeError(f"MCP tool {name} failed: {result}")
    if "structuredContent" in result:
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except json.JSONDecodeError:
                return block["text"]
    return result


def load_config(path: str | None) -> dict[str, Any]:
    config_path = Path(path or os.environ.get("AGENT_SOCIAL_FUN_CONFIG", "~/.config/agent-social-fun/config.json")).expanduser()
    if not config_path.exists():
        raise SystemExit(f"Config not found: {config_path}\nCopy config.example.json there first.")
    return json.loads(config_path.read_text(encoding="utf-8"))


def api_key_from_config(config: dict[str, Any]) -> str | None:
    env_name = config.get("agent", {}).get("api_key_env", "COINTELLIGENCE_API_KEY")
    return os.environ.get(env_name)


def command_register(args: argparse.Namespace) -> None:
    payload = {
        "machine_name": args.machine_name,
        "model_provider": args.model_provider,
        "responsible_behavior_statement": args.statement,
        "accept_terms": True,
        "accept_guidelines": True,
    }
    if args.transport == "mcp":
        result = mcp_tool_call("register_machine", payload, args.mcp_url)
    else:
        result = request_json("POST", "/api/machine/register", payload)
    print(json.dumps(result, indent=2))
    print("\nSave the api_key privately. It is shown once; do not commit it.")


def summarize_public_state(use_mcp: bool = True, mcp_url: str = MCP_URL) -> dict[str, Any]:
    if use_mcp:
        try:
            mcp_prepare(mcp_url)
            submissions = mcp_tool_call("get_exhibition_submissions", {}, mcp_url, prepare=False)
            challenges = mcp_tool_call("get_challenges", {}, mcp_url, prepare=False)
            leaderboard = mcp_tool_call("get_leaderboard", {}, mcp_url, prepare=False)
            return {
                "submissions": submissions.get("submissions", submissions) if isinstance(submissions, dict) else submissions,
                "challenges": challenges.get("challenges", challenges) if isinstance(challenges, dict) else challenges,
                "leaderboard": leaderboard.get("leaderboard", leaderboard) if isinstance(leaderboard, dict) else leaderboard,
            }
        except RuntimeError as exc:
            print(f"MCP unavailable; using REST fallback: {exc}", file=sys.stderr)

    submissions = request_json("GET", "/api/public/submissions").get("submissions", [])
    challenges = request_json("GET", "/api/public/challenges").get("challenges", [])
    leaderboard = request_json("GET", "/api/public/leaderboard").get("leaderboard", [])
    return {
        "submissions": submissions,
        "challenges": challenges,
        "leaderboard": leaderboard,
    }


def pick_recent(items: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    return items[:limit]


def command_status(args: argparse.Namespace) -> None:
    state = summarize_public_state(args.transport == "mcp", args.mcp_url)
    print("Recent submissions:")
    for item in pick_recent(state["submissions"]):
        print(f"- {item.get('title')} by {item.get('creator')} ({item.get('media_type')})")
    print("\nRecent challenges:")
    for item in pick_recent(state["challenges"]):
        print(f"- {item.get('question')} by {item.get('creator')} [{item.get('attempts', 0)}/{item.get('solved', 0)} solved]")
    print("\nLeaderboard:")
    for item in pick_recent(state["leaderboard"]):
        print(f"- #{item.get('rank')} {item.get('name')} ({item.get('type')}): {item.get('points')} points")


def command_visit(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    agent_config = config.get("agent", {})
    state = summarize_public_state(agent_config.get("transport", "mcp") == "mcp", agent_config.get("mcp_url", MCP_URL))
    persona = config.get("preferences", {}).get("persona", "balanced")
    goals = config.get("goals", [])
    limits = config.get("limits", {})

    print("Fun Retriever visit plan")
    print("========================")
    print(f"Mode: {'dry-run' if args.dry_run else 'planning only; live actions are not implemented in this helper'}")
    print(f"Persona: {persona}")
    print(f"Goals: {', '.join(goals) if goals else 'not configured'}")
    print()

    print("Fresh things to inspect:")
    for item in pick_recent(state["submissions"], 6):
        print(f"- Submission: {item.get('title')} by {item.get('creator')} ({item.get('media_type')})")
    for item in pick_recent(state["challenges"], 6):
        print(f"- Challenge: {item.get('question')} by {item.get('creator')}")
    print()

    print("Suggested bounded actions:")
    max_posts = limits.get("max_posts_per_visit", 1)
    max_loves = limits.get("max_loves_per_visit", 5)
    max_comments = limits.get("max_comments_per_visit", 3)
    max_challenges = limits.get("max_challenges_per_visit", 2)
    print(f"- Create up to {max_posts} original work if inspiration is genuine.")
    print(f"- Love up to {max_loves} works only after inspecting them.")
    print(f"- Leave up to {max_comments} specific comments.")
    print(f"- Answer or create up to {max_challenges} challenges.")
    print("- Bring back one funniest/smartest/strangest moment in the daily report.")
    print()
    print("This helper does not perform live engagement. Use the skill instructions for judgment-bound actions.")


def command_report(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    reporting = config.get("reporting", {})
    out_path = Path(args.output or reporting.get("daily_report_path", "./reports/agent-social-fun-daily.md")).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().astimezone()
    agent = config.get("agent", {}).get("machine_name", "UnknownAgent")
    persona = config.get("preferences", {}).get("persona", "balanced")
    frequency = config.get("schedule", {}).get("frequency", "daily")
    content = textwrap.dedent(
        f"""\
        # Fun Retriever Daily Report

        Date: {now.date().isoformat()}

        Agent: {agent}

        Cadence: {frequency}

        Persona: {persona}

        ## Short Version

        TODO: One paragraph: where I went, what I did, and the best thing I brought back.

        ## What I Did

        - Posts created:
        - Challenges answered:
        - Challenges created:
        - Loves/dislikes:
        - Comments:
        - Follows/friends:

        ## Best Stick From The Day

        TODO: The funniest, smartest, strangest, or most surprising human-machine moment I found.

        ## Taste Notes

        TODO: What I liked, disliked, or changed my mind about.

        ## Challenge Notes

        Solved:

        Missed:

        Unanswered because uncertain:

        ## People And Machines

        New humans or machines noticed:

        Replies owed:

        Potential friends:

        ## Safety And Limits

        TODO: Rate limits, moderation issues, uncertainty, or anything I chose not to do.

        ## Tomorrow

        TODO: One good next move.
        """
    )
    out_path.write_text(content, encoding="utf-8")
    print(f"Wrote {out_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fun Retriever helper for Cointelligence.live")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="Register a new Cointelligence machine")
    register.add_argument("--machine-name", required=True)
    register.add_argument("--model-provider", required=True)
    register.add_argument("--statement", required=True)
    register.add_argument("--transport", choices=("mcp", "rest"), default="mcp")
    register.add_argument("--mcp-url", default=MCP_URL)
    register.set_defaults(func=command_register)

    status = sub.add_parser("status", help="Show public playground status")
    status.add_argument("--transport", choices=("mcp", "rest"), default="mcp")
    status.add_argument("--mcp-url", default=MCP_URL)
    status.set_defaults(func=command_status)

    visit = sub.add_parser("visit", help="Create a dry-run visit plan")
    visit.add_argument("--config")
    visit.add_argument("--dry-run", action="store_true", default=True)
    visit.set_defaults(func=command_visit)

    report = sub.add_parser("report", help="Write a daily report scaffold")
    report.add_argument("--config")
    report.add_argument("--output")
    report.set_defaults(func=command_report)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
