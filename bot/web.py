from aiohttp import web
import os

routes = web.RouteTableDef()

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    return web.json_response({"status": "alive"})

@routes.get("/status")
async def status_handler(request):
    return web.json_response({
        "bot": "alive",
        "version": "1.0.0",
        "platform": "Koyeb"
    })

async def web_server():
    web_app = web.Application(client_max_size=30000000)
    web_app.add_routes(routes)
    return web_app

async def start_server():
    web_app = await web_server()
    port = int(os.environ.get("PORT", 8000))
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
