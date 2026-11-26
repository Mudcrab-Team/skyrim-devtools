"""Stop then immediately start the game three times on the shared session; each start must reach a playable menu."""
import time


async def run(ctx):
    manager = ctx.manager
    starts = []
    for index in range(3):
        began = time.monotonic()
        await manager.stop()
        await manager.start(headless=False)
        snapshot = await ctx.call("Events.snapshot")
        names = [m["name"] for m in snapshot["menus"]]
        starts.append({"round": index + 1, "seconds": round(time.monotonic() - began, 1),
                       "mainMenu": "Main Menu" in names})
        ctx.mark(f"restart {index + 1} ok")
    return {"pass": all(item["mainMenu"] for item in starts), "details": {"starts": starts}}
