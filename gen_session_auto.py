"""Generate session with provided code."""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 30546814
API_HASH = "fc28c35e3b8b87fb1c5e5268d3fdd940"
PHONE = "+46721813503"
CODE = "10466"

async def main():
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    await client.send_code_request(PHONE)
    print(f"Using code: {CODE}")
    await client.sign_in(PHONE, CODE)
    session_string = client.session.save()
    
    # Write to file for safe extraction
    with open('/tmp/session.txt', 'w') as f:
        f.write(session_string)
    
    print(f"\nSession saved to /tmp/session.txt")
    print(f"Length: {len(session_string)} chars")
    print(f"\nRaw session:\n{session_string}")
    
    await client.disconnect()

asyncio.run(main())
