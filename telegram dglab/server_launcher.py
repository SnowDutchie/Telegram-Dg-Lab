# server_launcher.py
import threading
import uvicorn
from tele_shocker_bot import main
import coyote_ws_server_api

def run_uvicorn():
    uvicorn.run("coyote_ws_server_api:app", host="0.0.0.0", port=8000)

if __name__ == "__main__":
    # Thread for FastAPI
    server_thread = threading.Thread(target=run_uvicorn, daemon=True)
    server_thread.start()

    # Thread for main()
    bot_thread = threading.Thread(target=main, daemon=True)
    bot_thread.start()

    # Keep main thread alive
    server_thread.join()
    bot_thread.join()
