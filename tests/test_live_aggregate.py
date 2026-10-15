"""LIVE read-only test: aggregate real stats from the 38k-point messages collection."""
import asyncio
import pathlib as _pathlib
import sys
import time

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config
import profiles
import scraper


async def main():
    gate = scraper.FloodGate()
    t0 = time.time()
    stats = await profiles.aggregate_message_stats(gate)
    elapsed = time.time() - t0

    users = stats["users"]
    qualifying = [u for u in users.values() if u["message_count"] >= config.PROFILE_MIN_MESSAGES]
    print(f"scanned_points={stats['scanned_points']} (in {elapsed:.1f}s)")
    print(f"unique_user_senders={len(users)}  qualifying(>={config.PROFILE_MIN_MESSAGES} msgs)={len(qualifying)}")
    print(f"chats_tracked={len(stats['chat_members'])}  usernames_resolved={len(stats['username_map'])}")

    top = sorted(users.items(), key=lambda kv: kv[1]["message_count"], reverse=True)[:5]
    print("\nTop 5 senders by message count:")
    for uid, u in top:
        n_chats = len(u["chats"])
        n_replies = sum(v["count"] for v in u["reply_targets"].values())
        n_mentions = len(u["mentions"])
        print(f"  id={uid} @{u['username'] or '-'} msgs={u['message_count']} chats={n_chats} "
              f"reply_out={n_replies} mention_targets={n_mentions} last={u['last_seen'][:19]}")

    rels = profiles.assemble_relation_payloads(stats)
    kinds = {}
    for p in rels:
        kinds[p["edge_kind"]] = kinds.get(p["edge_kind"], 0) + 1
    print(f"\nrelation_edges_built={len(rels)} by_kind={kinds}")

    # ~18.4k of 38.4k points are user messages; the rest are channel broadcasts (skipped by design)
    assert stats["scanned_points"] > 15000, "expected to scan the user-message corpus"
    assert len(users) > 100, "expected many unique senders"
    assert len(rels) > 0, "expected at least some relation edges"
    print("\nLIVE AGGREGATION OK")


asyncio.run(main())
