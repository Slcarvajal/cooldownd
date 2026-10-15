# Run inside the HA container: token on stdin, websocket commands as JSON args.
import asyncio, json, sys, aiohttp
async def main():
    token = sys.stdin.readline().strip()
    async with aiohttp.ClientSession() as s, s.ws_connect("http://127.0.0.1:8123/api/websocket") as ws:
        await ws.receive_json(); await ws.send_json({"type": "auth", "access_token": token})
        assert (await ws.receive_json())["type"] == "auth_ok"
        for i, cmd in enumerate(sys.argv[1:], 1):
            await ws.send_json({"id": i, **json.loads(cmd)})
            while True:
                r = await ws.receive_json()
                if r.get("id") == i and r.get("type") == "result":
                    print(json.dumps(r.get("result") if r.get("success") else {"error": r.get("error")}, ensure_ascii=False)); break
asyncio.run(main())
